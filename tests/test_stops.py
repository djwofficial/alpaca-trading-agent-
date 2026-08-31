"""Stops must fire with no model, no API key, and no network.

Every test here runs without touching the brain. That is the point: this is
the protection that survives an outage, an expired key, or an exhausted
billing limit.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from risk.stops import StopConfig, SpreadState, should_stop_out, summarize_spread


@dataclass
class FakeLeg:
    symbol: str
    qty: str
    avg_entry_price: str
    unrealized_pl: str


TODAY = date(2026, 8, 31)


def legs(short_entry="0.79", long_entry="0.39", short_pl="-40", long_pl="0") -> list:
    return [
        FakeLeg("SPY260908P00757000", "-5", short_entry, short_pl),
        FakeLeg("SPY260908P00752000", "5", long_entry, long_pl),
    ]


def state(credit=200.0, pl=0.0, expiration=date(2026, 9, 8), short_strike=757.0):
    return SpreadState(
        underlying="SPY", expiration=expiration, short_strike=short_strike,
        contracts=5, credit_received=credit, unrealized_pl=pl,
    )


# --- assembling a spread from position rows -------------------------------

def test_credit_and_pl_are_folded_from_the_legs():
    summary = summarize_spread(legs())
    assert summary.short_strike == 757.0
    assert summary.contracts == 5
    assert summary.credit_received == pytest.approx((0.79 - 0.39) * 100 * 5)
    assert summary.unrealized_pl == pytest.approx(-40.0)


def test_a_lone_leg_is_not_a_spread():
    """Prevents: misreading an unpaired position as something we can manage."""
    assert summarize_spread([legs()[0]]) is None


# --- the loss stop --------------------------------------------------------

def test_stop_fires_past_the_loss_multiple():
    triggered, reason = should_stop_out(
        state(credit=200, pl=-401), 765, TODAY, StopConfig(loss_multiple_of_credit=2.0)
    )
    assert triggered
    assert "Stop loss" in reason


def test_stop_holds_just_inside_the_multiple():
    triggered, _ = should_stop_out(
        state(credit=200, pl=-399), 765, TODAY, StopConfig(loss_multiple_of_credit=2.0)
    )
    assert not triggered


def test_a_winning_position_is_never_stopped_out():
    triggered, _ = should_stop_out(state(credit=200, pl=+150), 765, TODAY, StopConfig())
    assert not triggered


def test_the_multiple_is_configurable():
    tight = StopConfig(loss_multiple_of_credit=1.0)
    assert should_stop_out(state(credit=200, pl=-201), 765, TODAY, tight)[0]


# --- expiry ---------------------------------------------------------------

def test_position_is_flattened_before_expiry():
    """Prevents: carrying assignment risk through expiration."""
    triggered, reason = should_stop_out(
        state(expiration=date(2026, 9, 1)), 765, TODAY, StopConfig(close_within_days=1)
    )
    assert triggered
    assert "Expiry" in reason


def test_distant_expiry_is_left_alone():
    triggered, _ = should_stop_out(
        state(expiration=date(2026, 9, 30)), 765, TODAY, StopConfig()
    )
    assert not triggered


# --- the short strike being reached ---------------------------------------

def test_reaching_the_short_strike_closes_the_position():
    triggered, reason = should_stop_out(state(short_strike=757), 756.5, TODAY, StopConfig())
    assert triggered
    assert "short strike" in reason


def test_comfortably_above_the_short_strike_holds():
    triggered, _ = should_stop_out(state(short_strike=757), 770, TODAY, StopConfig())
    assert not triggered


# --- the whole point ------------------------------------------------------

def test_stops_need_no_model_no_key_and_no_network(monkeypatch):
    """If this passes with the API sabotaged, an outage cannot strand a position."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def explode(*args, **kwargs):
        raise AssertionError("stops must not call out to anything")

    monkeypatch.setattr("anthropic.Anthropic", explode)

    triggered, reason = should_stop_out(
        state(credit=200, pl=-500), 765, TODAY, StopConfig()
    )
    assert triggered and reason
