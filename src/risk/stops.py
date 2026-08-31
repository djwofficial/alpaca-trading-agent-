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


def summarize_spread(legs) -> SpreadState | None:
    """Fold a spread's position rows into one state object.

    Returns None when the rows do not form a recognisable spread — a lone leg,
    or something opened outside this agent.
    """
    from strategy.spreads import parse_occ

    short = next((leg for leg in legs if float(leg.qty) < 0), None)
    long = next((leg for leg in legs if float(leg.qty) > 0), None)
    if short is None or long is None:
        return None

    underlying, expiration, _, short_strike = parse_occ(short.symbol)
    contracts = int(abs(float(short.qty)))

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
