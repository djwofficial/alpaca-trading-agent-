"""The gates are the one thing that must not be wrong.

Each test names the disaster it prevents.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from risk.gates import OptionLeg, RiskConfig, RiskGate, TradeProposal, underlying_from_occ


@dataclass
class FakeAccount:
    equity: str = "100000"


@dataclass
class FakePosition:
    symbol: str


def spread(
    *,
    underlying: str = "SPY",
    contracts: int = 1,
    max_loss: float = 465.0,
    short_strike: float = 765,
    long_strike: float = 760,
    expiration: str = "2026-09-04",
    option_type: str = "put",
    legs: tuple | None = None,
) -> TradeProposal:
    """A standard defined-risk put credit spread unless told otherwise."""
    if legs is None:
        legs = (
            OptionLeg("SPY260904P00765000", "sell", option_type, short_strike, expiration),
            OptionLeg("SPY260904P00760000", "buy", option_type, long_strike, expiration),
        )
    return TradeProposal(
        underlying=underlying,
        legs=legs,
        contracts=contracts,
        max_loss=max_loss,
    )


@pytest.fixture
def gate() -> RiskGate:
    return RiskGate(RiskConfig())


def test_valid_spread_is_approved(gate):
    approved, reason = gate.check(spread(), FakeAccount(), [])
    assert approved, reason
    assert reason == "approved"


# --- naked shorts ---------------------------------------------------------

def test_naked_short_put_is_rejected(gate):
    """Prevents: unlimited downside if the underlying gaps down overnight."""
    naked = spread(
        legs=(OptionLeg("SPY260904P00765000", "sell", "put", 765, "2026-09-04"),)
    )
    approved, reason = gate.check(naked, FakeAccount(), [])
    assert not approved
    assert "naked short put" in reason.lower()


def test_naked_short_call_is_rejected(gate):
    """Prevents: theoretically unlimited loss on an uncovered call."""
    naked = spread(
        legs=(OptionLeg("SPY260904C00775000", "sell", "call", 775, "2026-09-04"),)
    )
    approved, reason = gate.check(naked, FakeAccount(), [])
    assert not approved
    assert "naked short call" in reason.lower()


def test_long_leg_on_wrong_side_does_not_protect(gate):
    """Prevents: a 'long' leg above a short put, which caps nothing."""
    bad = spread(
        legs=(
            OptionLeg("SPY260904P00765000", "sell", "put", 765, "2026-09-04"),
            OptionLeg("SPY260904P00770000", "buy", "put", 770, "2026-09-04"),
        )
    )
    approved, reason = gate.check(bad, FakeAccount(), [])
    assert not approved
    assert "naked" in reason.lower()


def test_long_leg_with_different_expiry_does_not_protect(gate):
    """Prevents: protection that expires before the short leg does."""
    bad = spread(
        legs=(
            OptionLeg("SPY260904P00765000", "sell", "put", 765, "2026-09-04"),
            OptionLeg("SPY260902P00760000", "buy", "put", 760, "2026-09-02"),
        )
    )
    approved, reason = gate.check(bad, FakeAccount(), [])
    assert not approved


def test_naked_short_allowed_when_explicitly_configured():
    gate = RiskGate(RiskConfig(allow_naked_short_options=True))
    naked = spread(
        legs=(OptionLeg("SPY260904P00765000", "sell", "put", 765, "2026-09-04"),)
    )
    approved, _ = gate.check(naked, FakeAccount(), [])
    assert approved


# --- position sizing ------------------------------------------------------

def test_oversized_trade_is_rejected(gate):
    """Prevents: one trade risking more than 5% of the account."""
    approved, reason = gate.check(spread(max_loss=5001), FakeAccount(), [])
    assert not approved
    assert "exceeds" in reason

def test_trade_at_exactly_the_cap_is_approved(gate):
    approved, reason = gate.check(spread(max_loss=5000), FakeAccount(), [])
    assert approved, reason


def test_unstated_max_loss_is_rejected(gate):
    """Prevents: taking a trade whose downside the agent never computed."""
    approved, reason = gate.check(spread(max_loss=0), FakeAccount(), [])
    assert not approved
    assert "max_loss" in reason


def test_missing_equity_is_rejected(gate):
    """Prevents: sizing a trade against an account we failed to read."""
    approved, reason = gate.check(spread(), FakeAccount(equity=None), [])
    assert not approved
    assert "equity" in reason.lower()


# --- order size -----------------------------------------------------------

def test_too_many_contracts_is_rejected(gate):
    approved, reason = gate.check(spread(contracts=11), FakeAccount(), [])
    assert not approved
    assert "per-order limit" in reason


def test_max_contracts_is_approved(gate):
    approved, reason = gate.check(spread(contracts=10), FakeAccount(), [])
    assert approved, reason


def test_zero_contracts_is_rejected(gate):
    approved, _ = gate.check(spread(contracts=0), FakeAccount(), [])
    assert not approved


# --- open positions -------------------------------------------------------

def test_position_limit_counts_underlyings_not_legs(gate):
    """Prevents: a spread counting twice and locking the agent out early."""
    positions = [
        FakePosition("SPY260904P00765000"),
        FakePosition("SPY260904P00760000"),
    ]
    approved, reason = gate.check(spread(underlying="QQQ"), FakeAccount(), positions)
    assert approved, reason


def test_sixth_underlying_is_rejected(gate):
    positions = [
        FakePosition(f"{sym}260904P00100000")
        for sym in ("SPY", "QQQ", "IWM", "DIA", "AAPL")
    ]
    approved, reason = gate.check(spread(underlying="TSLA"), FakeAccount(), positions)
    assert not approved
    assert "limit is 5" in reason


def test_adjusting_an_existing_underlying_is_allowed(gate):
    positions = [
        FakePosition(f"{sym}260904P00100000")
        for sym in ("SPY", "QQQ", "IWM", "DIA", "AAPL")
    ]
    approved, reason = gate.check(spread(underlying="SPY"), FakeAccount(), positions)
    assert approved, reason


# --- kill switch ----------------------------------------------------------

def test_kill_switch_trips_at_the_daily_loss_limit(gate):
    gate.update_daily_pnl(equity=97000, starting_equity=100000)
    assert gate.halted
    approved, reason = gate.check(spread(), FakeAccount(), [])
    assert not approved
    assert "Daily loss limit" in reason


def test_kill_switch_does_not_trip_above_the_limit(gate):
    gate.update_daily_pnl(equity=97500, starting_equity=100000)
    assert not gate.halted


def test_kill_switch_ignores_gains(gate):
    gate.update_daily_pnl(equity=105000, starting_equity=100000)
    assert not gate.halted


def test_kill_switch_survives_zero_starting_equity(gate):
    """Prevents: a divide-by-zero crash taking down the whole loop."""
    gate.update_daily_pnl(equity=0, starting_equity=0)
    assert not gate.halted


def test_halt_blocks_every_trade_until_reset(gate):
    gate.update_daily_pnl(equity=90000, starting_equity=100000)
    assert not gate.check(spread(), FakeAccount(), [])[0]
    gate.reset_for_new_day()
    assert gate.check(spread(), FakeAccount(), [])[0]


# --- helper ---------------------------------------------------------------

@pytest.mark.parametrize(
    "symbol,expected",
    [
        ("SPY260904P00765000", "SPY"),
        ("AAPL260904C00230000", "AAPL"),
        ("SPY", "SPY"),
    ],
)
def test_underlying_parsing(symbol, expected):
    assert underlying_from_occ(symbol) == expected


# --- closing orders -------------------------------------------------------

def closing_spread() -> TradeProposal:
    """Exiting a put credit spread: the legs invert."""
    return TradeProposal(
        underlying="SPY",
        legs=(
            OptionLeg("SPY260904P00765000", "buy", "put", 765, "2026-09-04"),
            OptionLeg("SPY260904P00760000", "sell", "put", 760, "2026-09-04"),
        ),
        contracts=10,
        max_loss=0.0,
        closing=True,
    )


def test_closing_order_is_not_blocked_by_the_naked_short_gate(gate):
    """Prevents: the agent being unable to exit its own defined-risk spread."""
    approved, reason = gate.check(closing_spread(), FakeAccount(), [])
    assert approved, reason
    assert "closing" in reason


def test_closing_order_is_allowed_while_halted(gate):
    """Prevents: a halted agent trapped in the position that halted it."""
    gate.update_daily_pnl(equity=90_000, starting_equity=100_000)
    assert gate.halted
    approved, _ = gate.check(closing_spread(), FakeAccount(), [])
    assert approved


def test_closing_order_ignores_the_position_limit(gate):
    positions = [
        FakePosition(f"{sym}260904P00100000")
        for sym in ("SPY", "QQQ", "IWM", "DIA", "AAPL", "TSLA")
    ]
    approved, _ = gate.check(closing_spread(), FakeAccount(), positions)
    assert approved


def test_opening_order_still_blocked_when_halted(gate):
    """The bypass must not leak to entries."""
    gate.update_daily_pnl(equity=90_000, starting_equity=100_000)
    approved, _ = gate.check(spread(), FakeAccount(), [])
    assert not approved
