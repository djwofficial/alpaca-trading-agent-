"""Candidate construction must price honestly and hand the gates something valid."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from risk.gates import RiskConfig, RiskGate
from strategy.spreads import (
    contracts_from_chain,
    find_put_credit_spreads,
    parse_occ,
)


@dataclass
class FakeQuote:
    bid_price: float
    ask_price: float


@dataclass
class FakeSnapshot:
    latest_quote: FakeQuote | None


@dataclass
class FakeAccount:
    equity: str = "100000"


def occ(strike: float, option_type: str = "P", expiry: str = "260904") -> str:
    return f"SPY{expiry}{option_type}{int(strike * 1000):08d}"


def chain(prices: dict[float, tuple[float, float]], expiry: str = "260904") -> dict:
    return {
        occ(strike, "P", expiry): FakeSnapshot(FakeQuote(bid, ask))
        for strike, (bid, ask) in prices.items()
    }


SPOT = 769.28
MARKET = chain({765: (0.74, 0.76), 760: (0.48, 0.50), 757: (0.30, 0.32), 752: (0.18, 0.20)})


# --- parsing --------------------------------------------------------------

def test_parse_occ_symbol():
    assert parse_occ("SPY260904P00765000") == ("SPY", date(2026, 9, 4), "put", 765.0)


def test_parse_occ_call_and_multichar_root():
    assert parse_occ("AAPL260904C00230000") == ("AAPL", date(2026, 9, 4), "call", 230.0)


def test_parse_occ_rejects_equity_symbol():
    with pytest.raises(ValueError):
        parse_occ("SPY")


def test_contracts_without_a_two_sided_market_are_dropped():
    """Prevents: pricing a spread against a quote that does not exist."""
    raw = {occ(765): FakeSnapshot(FakeQuote(0.74, 0.76)),
           occ(760): FakeSnapshot(FakeQuote(0, 0)),
           occ(755): FakeSnapshot(None)}
    assert [c.strike for c in contracts_from_chain(raw)] == [765.0]


# --- pricing --------------------------------------------------------------

def test_credit_sells_the_bid_and_buys_the_ask():
    """Prevents: a candidate looking better on paper than it fills."""
    spread = find_put_credit_spreads(MARKET, SPOT, min_cushion_pct=0.0)[0]
    assert spread.short.strike == 765
    assert spread.credit == pytest.approx(0.74 - 0.50)


def test_max_loss_is_width_minus_credit_times_multiplier():
    spread = find_put_credit_spreads(MARKET, SPOT, min_cushion_pct=0.0)[0]
    assert spread.max_loss_per_contract == pytest.approx((5 - 0.24) * 100)
    assert spread.credit_dollars == pytest.approx(24)


def test_breakeven_and_cushion():
    spread = find_put_credit_spreads(MARKET, SPOT, min_cushion_pct=0.0)[0]
    assert spread.breakeven == pytest.approx(765 - 0.24)
    assert spread.cushion_pct(SPOT) == pytest.approx((769.28 - 765) / 769.28)


def test_required_win_rate_tracks_risk():
    spread = find_put_credit_spreads(MARKET, SPOT, min_cushion_pct=0.0)[0]
    assert spread.required_win_rate() == pytest.approx(4.76 / 5)


# --- filtering ------------------------------------------------------------

def test_spreads_without_a_matching_long_leg_are_skipped():
    assert find_put_credit_spreads(chain({765: (0.74, 0.76)}), SPOT) == []


def test_strikes_above_spot_are_excluded():
    """The strategy bets the underlying stays up; it does not bet it rises."""
    above = chain({775: (6.0, 6.1), 770: (3.0, 3.1)})
    assert find_put_credit_spreads(above, SPOT) == []


def test_cushion_window_is_respected():
    found = find_put_credit_spreads(MARKET, SPOT, min_cushion_pct=0.01)
    assert all(c.cushion_pct(SPOT) >= 0.01 for c in found)
    assert 765 not in [c.short.strike for c in found]


def test_thin_credit_is_rejected():
    assert find_put_credit_spreads(MARKET, SPOT, min_credit=1.00) == []


def test_credit_wider_than_the_spread_is_rejected():
    """Prevents: a stale quote presenting as risk-free money."""
    absurd = chain({765: (9.0, 9.1), 760: (0.10, 0.12)})
    assert find_put_credit_spreads(absurd, SPOT, min_cushion_pct=0.0) == []


# --- sizing and handoff ---------------------------------------------------

def test_sizing_respects_the_position_cap():
    spread = find_put_credit_spreads(MARKET, SPOT, min_cushion_pct=0.0)[0]
    # $5,000 cap / $476 max loss per contract = 10 whole contracts
    assert spread.max_contracts(100_000, 0.05) == 10


def test_sizing_returns_zero_on_a_tiny_account():
    spread = find_put_credit_spreads(MARKET, SPOT, min_cushion_pct=0.0)[0]
    assert spread.max_contracts(1_000, 0.05) == 0


def test_candidate_becomes_a_proposal_the_gates_approve():
    """The two halves must agree: what strategy builds, risk must accept."""
    spread = find_put_credit_spreads(MARKET, SPOT, min_cushion_pct=0.0)[0]
    config = RiskConfig()
    size = min(spread.max_contracts(100_000, config.max_position_pct),
               config.max_contracts_per_order)

    proposal = spread.to_proposal(size, thesis="holds", invalidation="breaks 766")
    approved, reason = RiskGate(config).check(proposal, FakeAccount(), [])

    assert approved, reason
    assert proposal.contracts == 10
    assert len(proposal.legs) == 2


def test_generated_proposal_is_never_naked():
    """The gates would catch it, but strategy must not produce one at all."""
    spread = find_put_credit_spreads(MARKET, SPOT, min_cushion_pct=0.0)[0]
    proposal = spread.to_proposal(1)
    sides = {leg.side for leg in proposal.legs}
    assert sides == {"sell", "buy"}
    short, long = proposal.legs
    assert long.strike < short.strike
    assert long.expiration == short.expiration
