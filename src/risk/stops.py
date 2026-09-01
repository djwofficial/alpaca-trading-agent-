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


def _strike_of(position) -> float:
    from strategy.spreads import parse_occ

    return parse_occ(position.symbol)[3]


def _pair_by_strike(option_type: str, shorts: list, longs: list) -> tuple[list, list]:
    """Match each short leg to the long that actually protects it.

    A put is protected by a lower strike, a call by a higher one. Innermost
    shorts are matched first and claim their nearest protection, so two
    spreads sharing an expiration resolve to the pairs that were actually
    opened rather than to a crossed pairing that spans both.

    Anything left over — a naked short, a stray long, an inverted pair — is
    returned unpaired. It is not this function's job to invent a spread that
    is not there.
    """
    is_put = option_type == "put"
    unclaimed = list(longs)
    pairs: list = []
    orphans: list = []

    for short in sorted(shorts, key=_strike_of, reverse=is_put):
        strike = _strike_of(short)
        eligible = [
            leg for leg in unclaimed
            if (_strike_of(leg) < strike if is_put else _strike_of(leg) > strike)
        ]
        if not eligible:
            orphans.append(short)
            continue
        partner = max(eligible, key=_strike_of) if is_put else min(eligible, key=_strike_of)
        unclaimed.remove(partner)
        pairs.append([short, partner])

    orphans.extend(unclaimed)
    return pairs, orphans


def group_into_spreads(positions) -> dict[tuple[str, date, str, float | None], list]:
    """Group option position rows into individual spreads.

    Keyed by underlying, expiration, option type and short strike. The strike
    is what makes the key unique per spread: the risk gates permit two spreads
    on one underlying and the candidate finder enumerates every expiration in
    the chain, so two spreads sharing an expiration is a supported state, not
    an edge case. Keyed without the strike they merge into one four-legged
    phantom that summarizes to None — which reads as "unmanaged" and quietly
    takes both spreads out of the mechanical stops.

    Legs that do not pair off are grouped under a None strike so they still
    surface as unmanaged rather than vanishing.
    """
    from strategy.spreads import parse_occ

    buckets: dict[tuple[str, date, str], list] = {}
    for position in positions:
        if len(position.symbol) <= _OCC_SUFFIX_LENGTH:
            continue  # equity, not an option
        underlying, expiration, option_type, _ = parse_occ(position.symbol)
        buckets.setdefault((underlying, expiration, option_type), []).append(position)

    grouped: dict[tuple[str, date, str, float | None], list] = {}
    for (underlying, expiration, option_type), legs in buckets.items():
        shorts = [leg for leg in legs if float(leg.qty) < 0]
        longs = [leg for leg in legs if float(leg.qty) > 0]
        pairs, orphans = _pair_by_strike(option_type, shorts, longs)

        for short, long in pairs:
            key = (underlying, expiration, option_type, _strike_of(short))
            grouped[key] = [short, long]
        if orphans:
            grouped[(underlying, expiration, option_type, None)] = orphans

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

    underlying, expiration, option_type, short_strike = parse_occ(short.symbol)
    long_strike = parse_occ(long.symbol)[3]

    # A long leg on the wrong side of the short caps nothing. Summarizing it
    # as a spread would hand the stops a max loss that does not exist.
    protective = long_strike < short_strike if option_type == "put" else long_strike > short_strike
    if not protective:
        return None

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
