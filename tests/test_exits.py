"""The exit path, end to end, with two spreads open on one underlying.

The risk gates permit two spreads per underlying, so that is a state the
agent reaches routinely — and it is the state that broke every stage of the
exit path at once: grouping merged both spreads, the summary mixed one
spread's credit with both spreads' P&L, and the close order was sized from
whichever leg sorted first.

These tests run the real position rows from 2026-08-31 through grouping,
summarising, proposal and order construction.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from execution.orders import build_order
from journal import Journal
from main import closing_proposal, last_entry_for
from risk.stops import StopConfig, group_into_spreads, should_stop_out, summarize_spread


@dataclass
class FakePosition:
    symbol: str
    qty: str
    avg_entry_price: str
    unrealized_pl: str


# The account as it actually stood: a 9/3 757/752 spread in 5 contracts and a
# 9/8 750/745 spread in 3, returned in Alpaca's ordering.
# Keyed by short strike as well, so two spreads sharing an expiration stay
# distinct instead of merging into one four-legged phantom.
SPREAD_A = ("SPY", date(2026, 9, 3), "put", 757.0)
SPREAD_B = ("SPY", date(2026, 9, 8), "put", 750.0)


def live_positions() -> list[FakePosition]:
    return [
        FakePosition("SPY260903P00752000", "5", "0.39", "-30"),
        FakePosition("SPY260903P00757000", "-5", "0.79", "-25"),
        FakePosition("SPY260908P00745000", "3", "0.54", "-3"),
        FakePosition("SPY260908P00750000", "-3", "0.84", "-21"),
    ]


# --- grouping -------------------------------------------------------------

def test_two_spreads_on_one_underlying_stay_separate():
    """Prevents: merging every SPY leg into one phantom position."""
    groups = group_into_spreads(live_positions())

    assert set(groups) == {SPREAD_A, SPREAD_B}
    assert len(groups[SPREAD_A]) == 2
    assert len(groups[SPREAD_B]) == 2


def test_equity_rows_are_not_mistaken_for_options():
    groups = group_into_spreads(live_positions() + [FakePosition("SPY", "100", "760", "0")])
    assert set(groups) == {SPREAD_A, SPREAD_B}


# --- summarising ----------------------------------------------------------

def test_each_spread_reports_only_its_own_credit_and_pl():
    """Prevents: measuring one spread's stop against both spreads' losses."""
    groups = group_into_spreads(live_positions())

    a = summarize_spread(groups[SPREAD_A])
    assert a.contracts == 5
    assert a.short_strike == 757.0
    assert a.credit_received == pytest.approx((0.79 - 0.39) * 100 * 5)   # $200
    assert a.unrealized_pl == pytest.approx(-55.0)                        # not -79

    b = summarize_spread(groups[SPREAD_B])
    assert b.contracts == 3
    assert b.short_strike == 750.0
    assert b.credit_received == pytest.approx((0.84 - 0.54) * 100 * 3)   # $90
    assert b.unrealized_pl == pytest.approx(-24.0)


def test_an_unbalanced_pair_is_not_a_spread():
    """Prevents: closing 5 against a 3-lot and leaving a naked short leg."""
    legs = [
        FakePosition("SPY260903P00757000", "-5", "0.79", "-25"),
        FakePosition("SPY260903P00752000", "3", "0.39", "-30"),
    ]
    assert summarize_spread(legs) is None


def test_more_than_two_legs_is_not_a_spread():
    assert summarize_spread(live_positions()) is None


# --- the stop fires on the right spread -----------------------------------

def test_expiry_stop_touches_only_the_expiring_spread():
    """Prevents: the 9/3 expiry rule flattening the 9/8 spread with it."""
    groups = group_into_spreads(live_positions())
    the_day_before = date(2026, 9, 2)

    fires_a, reason = should_stop_out(
        summarize_spread(groups[SPREAD_A]), 763.37, the_day_before, StopConfig()
    )
    fires_b, _ = should_stop_out(
        summarize_spread(groups[SPREAD_B]), 763.37, the_day_before, StopConfig()
    )

    assert fires_a and "Expiry" in reason
    assert not fires_b, "the 9/8 spread was closed by the 9/3 spread's expiry"


def test_the_loss_stop_uses_the_right_spread_credit():
    """Spread B is down $24 on $90 collected — nowhere near 2x."""
    groups = group_into_spreads(live_positions())
    fires, _ = should_stop_out(
        summarize_spread(groups[SPREAD_B]), 763.37, date(2026, 9, 1), StopConfig()
    )
    assert not fires


# --- the order that actually goes out -------------------------------------

def test_close_order_matches_the_spread_it_closes():
    """Prevents: a 4-leg order at the wrong quantity.

    Every MLEG leg carries ratio_qty 1, so the order quantity applies to all
    of them. Closing the 9/8 spread at qty 5 would buy back 5 of a 3-lot
    short and sell 5 of a 3-lot long, opening a naked short put.
    """
    groups = group_into_spreads(live_positions())
    held = {p.symbol: int(p.qty) for p in live_positions()}

    for key, expected_qty in ((SPREAD_A, 5), (SPREAD_B, 3)):
        state = summarize_spread(groups[key])
        order = build_order(
            closing_proposal(groups[key], "SPY", state.contracts),
            limit_price=0.51,
            opening=False,
        )

        assert order.qty == expected_qty
        assert len(order.legs) == 2, "a close order must touch only its own spread"
        for leg in order.legs:
            assert abs(held[leg.symbol]) == order.qty


def test_close_order_intents_agree_with_the_positions_held():
    """A long is sold to close, a short is bought to close."""
    groups = group_into_spreads(live_positions())
    held = {p.symbol: int(p.qty) for p in live_positions()}
    state = summarize_spread(groups[SPREAD_A])

    order = build_order(
        closing_proposal(groups[SPREAD_A], "SPY", state.contracts),
        limit_price=0.51,
        opening=False,
    )

    for leg in order.legs:
        want = "sell_to_close" if held[leg.symbol] > 0 else "buy_to_close"
        assert leg.position_intent.value == want
        assert leg.side.value == want.split("_")[0]


# --- the right thesis reaches the model -----------------------------------

def test_each_spread_is_judged_against_its_own_thesis(tmp_path):
    """Prevents: reviewing the 9/8 spread against the 9/3 spread's exit rule."""
    journal = Journal(tmp_path / "decisions.jsonl")
    for expiry, thesis in (("2026-09-03", "757 holds"), ("2026-09-08", "750 holds")):
        journal.record(
            event="submitted", underlying="SPY", thesis=thesis,
            invalidation=f"below {expiry}", legs=[{"expiration": expiry}],
        )

    assert last_entry_for(journal, "SPY", date(2026, 9, 3))["thesis"] == "757 holds"
    assert last_entry_for(journal, "SPY", date(2026, 9, 8))["thesis"] == "750 holds"
    assert last_entry_for(journal, "SPY", date(2026, 9, 30)) is None
