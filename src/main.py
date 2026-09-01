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
from zoneinfo import ZoneInfo
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))

load_dotenv()

from agent.brain import MarketContext, RuleBasedBrain, SingleAnalystBrain  # noqa: E402
from data.client import (  # noqa: E402
    fetch_account,
    fetch_clock,
    fetch_daily_bars,
    fetch_open_orders,
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
from strategy.spreads import find_put_credit_spreads  # noqa: E402

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


MARKET_TZ = ZoneInfo("America/New_York")


def market_date(clock) -> date:
    """Today's date on the exchange's calendar, not on this machine's.

    Expiry is counted against the trading calendar, so the machine's clock is
    the wrong authority — this laptop runs eight hours ahead of the exchange.
    The broker's own timestamp is authoritative and is already fetched every
    cycle; US/Eastern is the fallback for a clock that comes back without a
    usable timestamp.

    A UTC date agrees with Eastern during the session and runs a day ahead
    for the four hours after midnight UTC, which is enough for an overnight
    --ignore-hours run to count an expiry a day early and flatten a position
    a session before it needed to.
    """
    stamp = getattr(clock, "timestamp", None)
    if stamp is not None and getattr(stamp, "tzinfo", None) is not None:
        return stamp.date()
    return datetime.now(MARKET_TZ).date()


def heartbeat(status: str) -> None:
    """Stamp the state file every cycle, whatever the outcome.

    Cycles that find nothing to do return without journalling, so a healthy
    idle agent and a wedged one leave the same trail. This is the liveness
    signal: logs/state.json always carries the last cycle's time and result.
    """
    write_state(last_cycle=datetime.now(timezone.utc).isoformat(), last_status=status)


# --- position review ------------------------------------------------------

def last_entry_for(
    journal: Journal,
    underlying: str,
    expiration: date,
    short_strike: float | None = None,
) -> dict | None:
    """The thesis that opened this spread, so its exit can be judged against it.

    Matched on the short strike as well as the expiration. Two spreads can
    share an underlying and an expiration, and matching more loosely hands
    the model the other spread's thesis and invalidation condition — which it
    will then faithfully evaluate against the wrong position.
    """
    wanted = expiration.isoformat()

    def is_this_spread(entry: dict) -> bool:
        legs = entry.get("legs", [])
        if not any(leg.get("expiration") == wanted for leg in legs):
            return False
        if short_strike is None:
            return True
        return any(
            leg.get("side") == "sell"
            and leg.get("strike") is not None
            and abs(float(leg["strike"]) - short_strike) < 1e-6
            for leg in legs
        )

    matches = [
        entry
        for entry in journal.read()
        if entry.get("underlying") == underlying
        and entry.get("event") in {"submitted", "dry_run"}
        and entry.get("thesis")
        and is_this_spread(entry)
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


def closing_limit_price(legs, chain) -> float | None:
    """Net debit to flatten: buy back the short at the ask, sell the long at the bid.

    Returns None when any leg has no usable quote. Falling back to 0.0 sends a
    limit order offering to pay nothing, which never fills — and the caller
    then logs a successful close over a position that is still open. A stop
    that cannot be priced has to say so, not report success.
    """
    total = 0.0
    for position in legs:
        snapshot = chain.get(position.symbol)
        quote = getattr(snapshot, "latest_quote", None)
        if quote is None:
            return None
        short = float(position.qty) < 0
        price = quote.ask_price if short else quote.bid_price
        if not price:
            return None
        total += float(price) if short else -float(price)
    return round(total, 2)


def record_cost(journal, brain, **fields) -> None:
    """Log what the model just cost, when a model was actually called.

    RuleBasedBrain, a cached skip, and a failed call all report None — the
    absence of a cost is not zero cost, it is no call, and logging it as a
    $0.00 row would make the trail lie about how often the model ran.
    """
    call = getattr(brain, "last_call", None)
    if call is None:
        return
    journal.record(event="model_call", **call.as_record(), **fields)
    log(
        f"  cost: ${call.usd:.3f} "
        f"({call.input_tokens:,} in / {call.output_tokens:,} out, {call.effort}) "
        f"· session ${brain.session_usd:.2f}"
    )


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
    open_orders = fetch_open_orders(trading)
    spot = fetch_spot(stocks, args.symbol)
    bars = fetch_daily_bars(stocks, args.symbol, days=6)
    chain = fetch_option_chain(options, args.symbol, days_out=args.days_out)

    closes = [float(bar.close) for bar in bars]
    minutes_to_close = max(
        0, int((clock.next_close - datetime.now(timezone.utc)).total_seconds() // 60)
    )
    open_spreads = group_into_spreads(positions)
    today = market_date(clock)

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
    for (underlying, expiration, _type, key_strike), legs in sorted(
        open_spreads.items(), key=lambda item: (item[0][1], item[0][3] or 0.0)
    ):
        tag = f"{underlying} {expiration}"
        if key_strike is not None:
            tag += f" {key_strike:g}"

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

        forced, stop_reason = should_stop_out(state, spot, today, stops)

        if forced:
            review_action, reasoning = "close", stop_reason
            thesis_valid = False
        else:
            entry = last_entry_for(journal, underlying, expiration, state.short_strike)
            review = brain.review_exit(describe_position(legs, entry), context)
            record_cost(
                journal, brain,
                underlying=underlying, expiration=expiration.isoformat(),
            )
            review_action, reasoning = review.action, review.reasoning
            thesis_valid = review.thesis_still_valid
            log(f"  {tag}: {review_action} — {reasoning[:90]}")

        outcome = ""
        if review_action == "close":
            limit = closing_limit_price(legs, chain)
            if limit is None:
                outcome = "no usable quote on every leg — the close could not be priced"
                log(f"  {tag}: CLOSE BLOCKED — {outcome}")
            else:
                proposal = closing_proposal(legs, underlying, state.contracts)
                decision = executor.execute(
                    proposal,
                    account,
                    positions,
                    limit_price=limit,
                    opening=False,
                    open_orders=open_orders,
                )
                outcome = decision.reason
                log(f"  {tag}: CLOSE — {reasoning[:70]} | {decision.reason}")

        # Recorded for holds too. The model was consulted and billed either
        # way, and an exit review that decided to hold is a decision — the
        # trail is meant to show the reasoning, not only the trades.
        journal.record(
            event="exit_review",
            underlying=underlying,
            expiration=expiration.isoformat(),
            short_strike=state.short_strike,
            contracts=state.contracts,
            action=review_action,
            mechanical=forced,
            thesis_still_valid=thesis_valid,
            reasoning=reasoning,
            outcome=outcome,
        )

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

    # The open-position caps depend only on the book, so when they are full
    # every candidate is rejected no matter which one the model picks. Asking
    # anyway is a bill for a judgement that cannot be acted on — and it is the
    # reason a capped agent looks expensive to leave running on the real brain.
    room, capacity_reason = gate.has_capacity(args.symbol, positions)
    if not room:
        log(f"  {capacity_reason}")
        journal.record(
            event="at_capacity",
            underlying=args.symbol,
            spot=spot,
            candidates=len(candidates),
            reason=capacity_reason,
        )
        heartbeat("at_capacity")
        return

    decision = brain.decide_entry(candidates, context, sizer)
    log(f"  brain: {decision.action} ({decision.confidence}) — {decision.reasoning[:90]}")
    record_cost(journal, brain, underlying=args.symbol, candidates=len(candidates))

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
        proposal, account, positions,
        limit_price=-round(candidate.credit, 2),
        open_orders=open_orders,
    )
    log(f"  {result.reason}")
    heartbeat("entry_attempted")


# --- entry point ----------------------------------------------------------

def build_args():
    parser = argparse.ArgumentParser(description="Autonomous options trading agent")
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument(
        "--interval", type=int, default=900,
        help="seconds between cycles; the biggest driver of model spend",
    )
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
