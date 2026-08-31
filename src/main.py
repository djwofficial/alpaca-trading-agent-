"""Agent entry point — the trading loop.

Each cycle:
  1. Load account state and trip the kill switch if the day has gone badly
  2. Fetch market + options chain data
  3. Review open positions against the thesis that opened them
  4. Agent proposes a trade
  5. RiskGate.check() approves or rejects
  6. Execute if approved
  7. Log the decision and reasoning

Runs dry by default. Live trading requires --live, explicitly, every time.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))

load_dotenv()

from agent.brain import MarketContext, RuleBasedBrain, SingleAnalystBrain  # noqa: E402
from data.client import (  # noqa: E402
    fetch_account,
    fetch_clock,
    fetch_daily_bars,
    fetch_option_chain,
    fetch_positions,
    fetch_spot,
    load_credentials,
    option_data_client,
    stock_data_client,
    trading_client,
)
from execution.orders import OrderExecutor  # noqa: E402
from journal import Journal  # noqa: E402
from risk.gates import (  # noqa: E402
    OptionLeg,
    RiskConfig,
    RiskGate,
    TradeProposal,
    underlying_from_occ,
)
from strategy.spreads import contracts_from_chain, find_put_credit_spreads  # noqa: E402

STATE_PATH = Path(__file__).resolve().parent.parent / "logs" / "state.json"


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


# --- daily baseline -------------------------------------------------------

def starting_equity_for_today(current_equity: float) -> float:
    """Equity at the session's start, so the kill switch survives a restart."""
    today = datetime.now(timezone.utc).date().isoformat()
    state = {}
    if STATE_PATH.exists():
        try:
            state = json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            state = {}

    if state.get("date") != today:
        state = {"date": today, "starting_equity": current_equity}
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state))

    return float(state["starting_equity"])


# --- position review ------------------------------------------------------

def option_positions_by_underlying(positions) -> dict[str, list]:
    grouped: dict[str, list] = {}
    for position in positions:
        if len(position.symbol) <= 15:
            continue  # equity, not an option
        grouped.setdefault(underlying_from_occ(position.symbol), []).append(position)
    return grouped


def last_entry_for(journal: Journal, underlying: str) -> dict | None:
    """The thesis that opened this position, so exits can be judged against it."""
    matches = [
        entry
        for entry in journal.read()
        if entry.get("underlying") == underlying
        and entry.get("event") in {"submitted", "dry_run"}
        and entry.get("thesis")
    ]
    return matches[-1] if matches else None


def describe_position(legs, entry: dict | None) -> str:
    lines = []
    for position in legs:
        lines.append(
            f"  {position.symbol} qty {position.qty} "
            f"entry {position.avg_entry_price} now {position.current_price} "
            f"unrealized P&L {position.unrealized_pl}"
        )
    body = "\n".join(lines)
    if entry:
        return (
            f"{body}\n"
            f"Thesis at entry: {entry['thesis']}\n"
            f"Invalidation condition: {entry['invalidation']}"
        )
    return f"{body}\nNo recorded thesis — opened outside this agent."


def closing_proposal(legs, underlying: str) -> TradeProposal:
    """Invert an open spread into the order that flattens it."""
    option_legs = []
    for position in legs:
        quantity = float(position.qty)
        _, expiration, option_type, strike = _parse(position.symbol)
        option_legs.append(
            OptionLeg(
                symbol=position.symbol,
                side="buy" if quantity < 0 else "sell",
                option_type=option_type,
                strike=strike,
                expiration=expiration,
            )
        )
    return TradeProposal(
        underlying=underlying,
        legs=tuple(option_legs),
        contracts=int(abs(float(legs[0].qty))),
        max_loss=0.0,
        closing=True,
    )


def _parse(symbol: str):
    from strategy.spreads import parse_occ

    underlying, expiration, option_type, strike = parse_occ(symbol)
    return underlying, expiration.isoformat(), option_type, strike


def closing_limit_price(legs, chain) -> float:
    """Net debit to flatten: buy back the short at the ask, sell the long at the bid."""
    total = 0.0
    for position in legs:
        snapshot = chain.get(position.symbol)
        quote = getattr(snapshot, "latest_quote", None)
        if quote is None:
            return 0.0
        if float(position.qty) < 0:
            total += float(quote.ask_price or 0)
        else:
            total -= float(quote.bid_price or 0)
    return round(total, 2)


# --- one cycle ------------------------------------------------------------

def run_cycle(args, clients, gate, brain, executor, journal) -> None:
    trading, options, stocks = clients

    clock = fetch_clock(trading)
    if not clock.is_open and not args.ignore_hours:
        log(f"Market closed. Next open {clock.next_open}.")
        return

    account = fetch_account(trading)
    equity = float(account.equity)
    baseline = starting_equity_for_today(equity)
    gate.update_daily_pnl(equity, baseline)

    log(f"Equity ${equity:,.2f} (day {(equity - baseline) / baseline:+.2%})")
    if gate.halted:
        log(f"HALTED: {gate.halt_reason}")

    positions = fetch_positions(trading)
    spot = fetch_spot(stocks, args.symbol)
    bars = fetch_daily_bars(stocks, args.symbol, days=6)
    chain = fetch_option_chain(options, args.symbol, days_out=args.days_out)

    closes = [float(bar.close) for bar in bars]
    minutes_to_close = max(
        0, int((clock.next_close - datetime.now(timezone.utc)).total_seconds() // 60)
    )
    open_by_underlying = option_positions_by_underlying(positions)

    context = MarketContext(
        symbol=args.symbol,
        spot=spot,
        prior_close=closes[-2] if len(closes) > 1 else spot,
        recent_closes=closes,
        minutes_to_close=minutes_to_close,
        open_position_count=len(open_by_underlying),
    )
    log(f"{args.symbol} ${spot:,.2f} ({context.session_change_pct:+.2%}), "
        f"{len(open_by_underlying)} open, {minutes_to_close}m to close")

    # --- exits first: freeing risk beats adding it ---
    for underlying, legs in open_by_underlying.items():
        entry = last_entry_for(journal, underlying)
        review = brain.review_exit(describe_position(legs, entry), context)
        log(f"  {underlying}: {review.action} — {review.reasoning[:90]}")

        if review.action == "close":
            proposal = closing_proposal(legs, underlying)
            decision = executor.execute(
                proposal,
                account,
                positions,
                limit_price=closing_limit_price(legs, chain),
                opening=False,
            )
            journal.record(
                event="exit_review",
                underlying=underlying,
                thesis_still_valid=review.thesis_still_valid,
                reasoning=review.reasoning,
                outcome=decision.reason,
            )
            log(f"  {underlying}: CLOSE — {decision.reason}")

    if gate.halted:
        return

    # --- entries ---
    candidates = find_put_credit_spreads(
        chain,
        spot,
        width=args.width,
        min_cushion_pct=args.min_cushion,
        max_cushion_pct=args.max_cushion,
    )
    log(f"  {len(candidates)} candidates")
    if not candidates:
        return

    config = gate.config

    def sizer(candidate) -> int:
        return min(
            candidate.max_contracts(equity, config.max_position_pct),
            config.max_contracts_per_order,
        )

    decision = brain.decide_entry(candidates, context, sizer)
    log(f"  brain: {decision.action} ({decision.confidence}) — {decision.reasoning[:90]}")

    if decision.action != "enter":
        journal.record(
            event="skipped",
            underlying=args.symbol,
            spot=spot,
            candidates=len(candidates),
            reasoning=decision.reasoning,
            confidence=decision.confidence,
        )
        return

    candidate = candidates[decision.candidate_index]
    proposal = candidate.to_proposal(
        decision.contracts, thesis=decision.thesis, invalidation=decision.invalidation
    )
    result = executor.execute(
        proposal, account, positions, limit_price=round(candidate.credit, 2)
    )
    log(f"  {result.reason}")


# --- entry point ----------------------------------------------------------

def build_args():
    parser = argparse.ArgumentParser(description="Autonomous options trading agent")
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--interval", type=int, default=300, help="seconds between cycles")
    parser.add_argument("--once", action="store_true", help="run a single cycle and exit")
    parser.add_argument("--live", action="store_true", help="actually transmit orders")
    parser.add_argument("--brain", choices=("claude", "rules"), default="claude")
    parser.add_argument("--width", type=float, default=5.0)
    parser.add_argument("--days-out", type=int, default=8)
    parser.add_argument("--min-cushion", type=float, default=0.005)
    parser.add_argument("--max-cushion", type=float, default=0.03)
    parser.add_argument(
        "--ignore-hours", action="store_true", help="run even when the market is closed"
    )
    return parser.parse_args()


def main():
    args = build_args()
    creds = load_credentials()

    clients = (trading_client(creds), option_data_client(creds), stock_data_client(creds))
    gate = RiskGate(RiskConfig())
    journal = Journal()
    brain = SingleAnalystBrain() if args.brain == "claude" else RuleBasedBrain()
    executor = OrderExecutor(clients[0], gate, journal, dry_run=not args.live)

    mode = "LIVE" if args.live else "DRY RUN"
    log(f"{mode} · {args.brain} brain · {args.symbol} · paper={creds.paper}")
    if args.live:
        log("Orders WILL be transmitted.")

    while True:
        try:
            run_cycle(args, clients, gate, brain, executor, journal)
        except KeyboardInterrupt:
            log("Stopped.")
            return
        except Exception as exc:  # a bad cycle must not end the session
            log(f"Cycle failed: {type(exc).__name__}: {exc}")
            journal.record(event="cycle_error", error=str(exc))

        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
