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
from datetime import date, datetime, timezone
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
from risk.stops import (  # noqa: E402
    StopConfig,
    group_into_spreads,
    should_stop_out,
    summarize_spread,
)
from risk.gates import (  # noqa: E402
    OptionLeg,
    RiskConfig,
    RiskGate,
    TradeProposal,
)
from strategy.spreads import contracts_from_chain, find_put_credit_spreads  # noqa: E402

STATE_PATH = Path(__file__).resolve().parent.parent / "logs" / "state.json"
LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "agent.log"


def log(message: str) -> None:
    """Write to the terminal and to logs/agent.log.

    The file matters because the terminal does not survive the session. A
    live run whose stdout goes to a console leaves agent.log frozen at
    whatever the last redirected run wrote, which reads as a dead agent.
    """
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
    print(line, flush=True)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a") as handle:
            handle.write(line + "\n")
    except OSError:
        pass  # logging must never take the loop down


# --- daily baseline -------------------------------------------------------

def read_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def write_state(**fields) -> None:
    """Merge fields into the state file. Never raises: state is bookkeeping."""
    try:
        state = read_state() | fields
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state))
    except OSError:
        pass


def session_baseline(account) -> float:
    """Equity at the prior session's close, which the daily loss limit is
    measured against.

    Alpaca reports this as last_equity. Capturing equity at the agent's own
    first cycle instead makes the reference an arbitrary intraday mark: an
    overnight gap, or any move before the first cycle ran, is invisible to
    the kill switch, and a restart mid-drawdown re-baselines the loss away.
    Falls back to current equity only when the broker omits the field.
    """
    try:
        baseline = float(account.last_equity)
    except (AttributeError, TypeError, ValueError):
        baseline = 0.0
    return baseline if baseline > 0 else float(account.equity)


def sync_halt(gate, baseline: float, equity: float) -> None:
    """Carry the day's halt across restarts.

    RiskGate.halted lives in memory, so without this a halted agent resumes
    trading the moment the process is restarted — which is exactly what
    tends to happen after a bad day.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    state = read_state()

    if state.get("date") != today:
        gate.reset_for_new_day()
        write_state(date=today, halted=False, halt_reason="")
    elif state.get("halted") and not gate.halted:
        gate.halted = True
        gate.halt_reason = state.get("halt_reason", "Trading halted earlier today")

    was_halted = gate.halted
    gate.update_daily_pnl(equity, baseline)
    if gate.halted and not was_halted:
        write_state(date=today, halted=True, halt_reason=gate.halt_reason)


def heartbeat(status: str) -> None:
    """Stamp the state file every cycle, whatever the outcome.

    Cycles that find nothing to do return without journalling, so a healthy
    idle agent and a wedged one leave the same trail. This is the liveness
    signal: logs/state.json always carries the last cycle's time and result.
    """
    write_state(last_cycle=datetime.now(timezone.utc).isoformat(), last_status=status)


# --- position review ------------------------------------------------------

def last_entry_for(
    journal: Journal, underlying: str, expiration: date
) -> dict | None:
    """The thesis that opened this spread, so its exit can be judged against it.

    Matched on expiration as well as underlying. With two spreads open on one
    name, matching the underlying alone hands the model the wrong spread's
    thesis and invalidation condition.
    """
    wanted = expiration.isoformat()
    matches = [
        entry
        for entry in journal.read()
        if entry.get("underlying") == underlying
        and entry.get("event") in {"submitted", "dry_run"}
        and entry.get("thesis")
        and any(leg.get("expiration") == wanted for leg in entry.get("legs", []))
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


def closing_proposal(legs, underlying: str, contracts: int) -> TradeProposal:
    """Invert an open spread into the order that flattens it.

    contracts comes from the summarized spread, not from whichever leg sorted
    first. Every leg of an MLEG order carries ratio_qty 1, so the order's
    quantity applies to all of them: sizing from the wrong leg over-closes
    the other side and opens a fresh naked position in the process.
    """
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
        contracts=contracts,
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

def run_cycle(args, clients, gate, brain, executor, journal, stops) -> None:
    trading, options, stocks = clients

    clock = fetch_clock(trading)
    if not clock.is_open and not args.ignore_hours:
        log(f"Market closed. Next open {clock.next_open}.")
        heartbeat("market_closed")
        return

    account = fetch_account(trading)
    equity = float(account.equity)
    baseline = session_baseline(account)
    sync_halt(gate, baseline, equity)

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
    open_spreads = group_into_spreads(positions)

    context = MarketContext(
        symbol=args.symbol,
        spot=spot,
        prior_close=closes[-2] if len(closes) > 1 else spot,
        recent_closes=closes,
        minutes_to_close=minutes_to_close,
        open_position_count=len(open_spreads),
    )
    log(f"{args.symbol} ${spot:,.2f} ({context.session_change_pct:+.2%}), "
        f"{len(open_spreads)} open, {minutes_to_close}m to close")

    # --- exits first: freeing risk beats adding it ---
    for (underlying, expiration, _type), legs in sorted(
        open_spreads.items(), key=lambda item: item[0][1]
    ):
        tag = f"{underlying} {expiration}"

        # Mechanical stops run before the model and fire without it. If the
        # API is down or out of credit, this is the only thing standing
        # between an open position and its maximum loss.
        state = summarize_spread(legs)
        if state is None:
            # Not a balanced pair, so there is no close order we can build
            # that matches what is held. Say so loudly rather than guess.
            journal.record(
                event="unmanaged_position",
                underlying=underlying,
                expiration=expiration.isoformat(),
                symbols=[position.symbol for position in legs],
                reason="legs do not form a balanced spread — stops cannot manage it",
            )
            log(f"  {tag}: UNMANAGED — not a balanced spread, needs manual review")
            continue

        forced, stop_reason = should_stop_out(
            state, spot, datetime.now(timezone.utc).date(), stops
        )

        if forced:
            review_action, reasoning = "close", stop_reason
            thesis_valid = False
        else:
            entry = last_entry_for(journal, underlying, expiration)
            review = brain.review_exit(describe_position(legs, entry), context)
            review_action, reasoning = review.action, review.reasoning
            thesis_valid = review.thesis_still_valid
            log(f"  {tag}: {review_action} — {reasoning[:90]}")

        if review_action == "close":
            proposal = closing_proposal(legs, underlying, state.contracts)
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
                expiration=expiration.isoformat(),
                contracts=state.contracts,
                mechanical=forced,
                thesis_still_valid=thesis_valid,
                reasoning=reasoning,
                outcome=decision.reason,
            )
            log(f"  {tag}: CLOSE — {reasoning[:70]} | {decision.reason}")

    if gate.halted:
        heartbeat("halted")
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
        journal.record(
            event="no_candidates",
            underlying=args.symbol,
            spot=spot,
            reason="chain produced no spread inside the cushion band",
        )
        heartbeat("no_candidates")
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
        heartbeat("skipped")
        return

    candidate = candidates[decision.candidate_index]
    proposal = candidate.to_proposal(
        decision.contracts, thesis=decision.thesis, invalidation=decision.invalidation
    )
    # Alpaca signs multi-leg limit prices from the account's perspective:
    # negative is a credit received, positive is a debit paid. Verified against
    # real fills on 2026-08-31 (limit 0.46 -> filled -0.40). Sending a positive
    # number here would tell the broker we are willing to PAY that much to open
    # a credit spread.
    result = executor.execute(
        proposal, account, positions, limit_price=-round(candidate.credit, 2)
    )
    log(f"  {result.reason}")
    heartbeat("entry_attempted")


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
        "--stop-multiple", type=float, default=2.0,
        help="close a spread once it loses this multiple of the credit taken in",
    )
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
    stops = StopConfig(loss_multiple_of_credit=args.stop_multiple)

    mode = "LIVE" if args.live else "DRY RUN"
    log(f"{mode} · {args.brain} brain · {args.symbol} · paper={creds.paper}")
    if args.live:
        log("Orders WILL be transmitted.")

    while True:
        try:
            run_cycle(args, clients, gate, brain, executor, journal, stops)
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
