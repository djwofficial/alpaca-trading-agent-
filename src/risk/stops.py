"""Deterministic exits that do not need the model.

The brain handles judgement: has the thesis broken? But judgement requires a
working API, a funded account, and a network. None of those are guaranteed at
3am, and a position that cannot be closed because a billing limit was reached
is not a risk-managed position.

So the same rule as the entry gates applies here: the protection that matters
lives in code. These stops run before the model is consulted and fire whether
or not it answers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

CONTRACT_MULTIPLIER = 100
_OCC_SUFFIX_LENGTH = 15


@dataclass
class StopConfig:
    loss_multiple_of_credit: float = 2.0   # close once losing 2x the credit taken in
    close_within_days: int = 1             # flatten before expiry rather than gamble
    breach_buffer_pct: float = 0.0         # close if spot reaches the short strike


@dataclass
class SpreadState:
    """One open spread, assembled from its Alpaca position rows."""

    underlying: str
    expiration: date
    short_strike: float
    contracts: int
    credit_received: float      # dollars taken in at open, for the whole position
    unrealized_pl: float        # dollars, negative when losing


def group_into_spreads(positions) -> dict[tuple[str, date, str], list]:
    """Group option position rows into individual spreads.

    Keyed by underlying, expiration and option type — the three things that
    define a vertical.

    Grouping by underlying alone merges every spread on that name into one
    phantom position: the credit of whichever leg sorted first measured
    against the P&L of all of them, and a close order sized from a single
    leg's quantity. The risk gates explicitly permit two spreads per
    underlying, so that collision is a supported state, not an edge case.
    """
    from strategy.spreads import parse_occ

    grouped: dict[tuple[str, date, str], list] = {}
    for position in positions:
        if len(position.symbol) <= _OCC_SUFFIX_LENGTH:
            continue  # equity, not an option
        underlying, expiration, option_type, _ = parse_occ(position.symbol)
        grouped.setdefault((underlying, expiration, option_type), []).append(position)
    return grouped


def summarize_spread(legs) -> SpreadState | None:
    """Fold a spread's position rows into one state object.

    Returns None when the rows do not form a recognisable spread — a lone leg,
    an unbalanced pair, or something opened outside this agent. Refusing to
    summarise is the safe failure: it leaves the position to the model and to
    manual review, where guessing would build a close order that does not
    match what is actually held.
    """
    from strategy.spreads import parse_occ

    shorts = [leg for leg in legs if float(leg.qty) < 0]
    longs = [leg for leg in legs if float(leg.qty) > 0]
    if len(shorts) != 1 or len(longs) != 1:
        return None

    short, long = shorts[0], longs[0]
    contracts = int(abs(float(short.qty)))
    if contracts != int(abs(float(long.qty))):
        return None  # unbalanced: closing at either size would leave a naked leg

    underlying, expiration, _, short_strike = parse_occ(short.symbol)

    credit_per_share = float(short.avg_entry_price) - float(long.avg_entry_price)
    credit_received = credit_per_share * CONTRACT_MULTIPLIER * contracts
    unrealized = sum(float(leg.unrealized_pl) for leg in legs)

    return SpreadState(
        underlying=underlying,
        expiration=expiration,
        short_strike=short_strike,
        contracts=contracts,
        credit_received=credit_received,
        unrealized_pl=unrealized,
    )


def should_stop_out(
    state: SpreadState, spot: float, today: date, config: StopConfig
) -> tuple[bool, str]:
    """Return (close_now, reason). Runs with no model and no network."""
    if state.credit_received > 0:
        ceiling = -config.loss_multiple_of_credit * state.credit_received
        if state.unrealized_pl <= ceiling:
            return True, (
                f"Stop loss: down ${abs(state.unrealized_pl):,.2f} against "
                f"${state.credit_received:,.2f} collected "
                f"({config.loss_multiple_of_credit:g}x). Closing."
            )

    days_left = (state.expiration - today).days
    if days_left <= config.close_within_days:
        return True, (
            f"Expiry in {days_left} day(s) — closing rather than carrying "
            f"assignment risk into expiration."
        )

    threshold = state.short_strike * (1 + config.breach_buffer_pct)
    if spot <= threshold:
        return True, (
            f"{state.underlying} at {spot:.2f} has reached the short strike "
            f"{state.short_strike:g}. Closing."
        )

    return False, ""
