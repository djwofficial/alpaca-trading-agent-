"""Regressions for four bugs found on 2026-09-02, all of which failed quietly.

Each one let the agent report success while the position was actually
unmanaged, unpriced, or duplicated. They are grouped here rather than spread
across the suite so the failure modes stay legible together.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field

import pytest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from execution.orders import OrderExecutor, working_order_symbols
from journal import Journal
from main import closing_limit_price, last_entry_for
from risk.gates import OptionLeg, RiskConfig, RiskGate, TradeProposal
from risk.stops import group_into_spreads, summarize_spread


@dataclass
class FakePosition:
    symbol: str
    qty: str
    avg_entry_price: str = "1.00"
    unrealized_pl: str = "-10"


@dataclass
class FakeQuote:
    bid_price: float | None
    ask_price: float | None


@dataclass
class FakeSnapshot:
    latest_quote: object


@dataclass
class FakeLeg:
    symbol: str


@dataclass
class FakeOrder:
    symbol: str | None = None
    legs: list = field(default_factory=list)


# --- Bug 1: two spreads at one expiration disabled every mechanical stop ---

def test_two_spreads_sharing_an_expiration_stay_separate():
    """Keyed without the strike these merged into one four-legged group that
    summarized to None, which read as 'unmanaged' and took both spreads out
    of the mechanical stops entirely."""
    book = [
        FakePosition("SPY260908P00750000", "-3", "0.84"),
        FakePosition("SPY260908P00745000", "3", "0.54"),
        FakePosition("SPY260908P00740000", "-2", "0.40"),
        FakePosition("SPY260908P00735000", "2", "0.22"),
    ]
    groups = group_into_spreads(book)

    assert len(groups) == 2, "the two spreads must not merge"
    assert set(groups) == {
        ("SPY", date(2026, 9, 8), "put", 750.0),
        ("SPY", date(2026, 9, 8), "put", 740.0),
    }
    for key, legs in groups.items():
        state = summarize_spread(legs)
        assert state is not None, f"{key} must remain managed"

    inner = summarize_spread(groups[("SPY", date(2026, 9, 8), "put", 750.0)])
    assert inner.short_strike == 750.0
    assert inner.contracts == 3
    assert inner.credit_received == pytest.approx(90.0)  # (0.84 - 0.54) * 100 * 3


def test_each_short_claims_its_nearest_protection():
    """A crossed pairing (750/735 and 740/745) would misstate both max losses."""
    book = [
        FakePosition("SPY260908P00750000", "-1"),
        FakePosition("SPY260908P00745000", "1"),
        FakePosition("SPY260908P00740000", "-1"),
        FakePosition("SPY260908P00735000", "1"),
    ]
    groups = group_into_spreads(book)
    pairs = {
        key[3]: sorted(leg.symbol for leg in legs) for key, legs in groups.items()
    }
    assert pairs[750.0] == ["SPY260908P00745000", "SPY260908P00750000"]
    assert pairs[740.0] == ["SPY260908P00735000", "SPY260908P00740000"]


def test_a_naked_short_surfaces_as_unmanaged():
    book = [FakePosition("SPY260908P00750000", "-3")]
    groups = group_into_spreads(book)
    assert list(groups) == [("SPY", date(2026, 9, 8), "put", None)]
    assert summarize_spread(groups[("SPY", date(2026, 9, 8), "put", None)]) is None


def test_a_long_on_the_wrong_side_is_not_a_spread():
    """A long put ABOVE the short caps nothing; summarizing it would hand the
    stops a max loss that does not exist."""
    inverted = [
        FakePosition("SPY260908P00750000", "-3"),
        FakePosition("SPY260908P00755000", "3"),
    ]
    assert summarize_spread(inverted) is None


# --- Bug 2: no open-order awareness meant duplicate submissions -----------

def _executor(tmp_path, dry_run=True):
    journal = Journal(tmp_path / "j.jsonl")
    return OrderExecutor(None, RiskGate(RiskConfig()), journal, dry_run=dry_run), journal


def _closing_proposal():
    return TradeProposal(
        underlying="SPY",
        legs=(
            OptionLeg("SPY260908P00750000", "buy", "put", 750.0, "2026-09-08"),
            OptionLeg("SPY260908P00745000", "sell", "put", 745.0, "2026-09-08"),
        ),
        contracts=3,
        max_loss=0.0,
        closing=True,
    )


def test_working_order_symbols_reads_both_order_shapes():
    orders = [
        FakeOrder(legs=[FakeLeg("SPY260908P00750000"), FakeLeg("SPY260908P00745000")]),
        FakeOrder(symbol="AAPL260908P00200000"),
    ]
    assert working_order_symbols(orders) == {
        "SPY260908P00750000",
        "SPY260908P00745000",
        "AAPL260908P00200000",
    }
    assert working_order_symbols(()) == set()
    assert working_order_symbols(None) == set()


def test_a_second_close_is_suppressed_while_the_first_is_working(tmp_path):
    """Closing orders bypass every risk gate by design, so without this a stop
    firing each cycle submits a fresh close every cycle — and if two fill, the
    second opens the inverted spread."""
    executor, journal = _executor(tmp_path)
    working = [FakeOrder(legs=[FakeLeg("SPY260908P00750000"), FakeLeg("SPY260908P00745000")])]

    decision = executor.execute(
        _closing_proposal(), account=None, positions=[],
        limit_price=0.30, opening=False, open_orders=working,
    )

    assert decision.approved is False
    assert decision.submitted is False
    assert "already working" in decision.reason
    assert journal.read()[-1]["event"] == "duplicate_suppressed"


def test_an_unrelated_working_order_does_not_block(tmp_path):
    executor, _ = _executor(tmp_path)
    elsewhere = [FakeOrder(legs=[FakeLeg("QQQ260908P00500000")])]

    decision = executor.execute(
        _closing_proposal(), account=None, positions=[],
        limit_price=0.30, opening=False, open_orders=elsewhere,
    )
    assert decision.approved is True


# --- Bug 3: an unpriceable close was sent as a $0.00 limit ----------------

LEGS = [FakePosition("SPY260903P00757000", "-5"), FakePosition("SPY260903P00752000", "5")]


def test_closing_price_is_a_positive_debit_when_both_legs_quote():
    chain = {
        "SPY260903P00757000": FakeSnapshot(FakeQuote(0.80, 0.90)),
        "SPY260903P00752000": FakeSnapshot(FakeQuote(0.30, 0.40)),
    }
    assert closing_limit_price(LEGS, chain) == 0.60


def test_a_missing_quote_refuses_to_price_rather_than_returning_zero():
    """0.0 is an order offering to pay nothing to close. It never fills, and
    the caller logged it as a successful close over a still-open position."""
    assert closing_limit_price(LEGS, {"SPY260903P00757000": FakeSnapshot(FakeQuote(0.80, 0.90))}) is None
    assert closing_limit_price(LEGS, {s.symbol: FakeSnapshot(None) for s in LEGS}) is None


def test_a_one_sided_quote_refuses_to_price():
    chain = {
        "SPY260903P00757000": FakeSnapshot(FakeQuote(0.80, None)),
        "SPY260903P00752000": FakeSnapshot(FakeQuote(0.30, 0.40)),
    }
    assert closing_limit_price(LEGS, chain) is None


# --- Bug 1 tail: the thesis must follow the right spread ------------------

def test_the_thesis_lookup_distinguishes_two_spreads_at_one_expiration(tmp_path):
    journal = Journal(tmp_path / "j.jsonl")
    for strike, thesis in ((750.0, "750 holds"), (740.0, "740 holds")):
        journal.record(
            event="submitted", underlying="SPY", thesis=thesis, invalidation="x",
            legs=[
                {"symbol": f"SPY260908P00{int(strike)*1000:08d}", "side": "sell",
                 "strike": strike, "expiration": "2026-09-08"},
                {"symbol": "l", "side": "buy", "strike": strike - 5, "expiration": "2026-09-08"},
            ],
        )

    assert last_entry_for(journal, "SPY", date(2026, 9, 8), 750.0)["thesis"] == "750 holds"
    assert last_entry_for(journal, "SPY", date(2026, 9, 8), 740.0)["thesis"] == "740 holds"
    assert last_entry_for(journal, "SPY", date(2026, 9, 8), 999.0) is None


# --- Bug 5: the machine's date is not the exchange's date -----------------

def test_market_date_prefers_the_broker_clock():
    """This laptop runs eight hours ahead of the exchange; the broker's own
    timestamp is the authority on what day the market thinks it is."""
    from datetime import datetime, timezone as tz
    from main import market_date

    @dataclass
    class FakeClock:
        timestamp: object

    eastern = tz(__import__("datetime").timedelta(hours=-4))
    # 22:00 on Sep 1 in New York is already Sep 2 in UTC.
    clock = FakeClock(datetime(2026, 9, 1, 22, 0, tzinfo=eastern))
    assert market_date(clock) == date(2026, 9, 1)
    assert clock.timestamp.astimezone(tz.utc).date() == date(2026, 9, 2)


def test_market_date_falls_back_when_the_clock_has_no_usable_stamp():
    from main import market_date

    @dataclass
    class Naive:
        timestamp: object

    assert isinstance(market_date(Naive(None)), date)
    assert isinstance(market_date(object()), date)


# --- Cost: never pay for a decision the gates will reject -----------------

def test_capacity_is_a_book_only_question():
    """has_capacity takes an underlying, not a proposal, because that is the
    point: when the caps are full every candidate is rejected regardless of
    which one the model would have picked."""
    gate = RiskGate(RiskConfig(max_spreads_per_underlying=2))
    book = [
        FakePosition("SPY260908P00750000", "-3"),
        FakePosition("SPY260908P00745000", "3"),
        FakePosition("SPY260903P00757000", "-5"),
        FakePosition("SPY260903P00752000", "5"),
    ]
    room, why = gate.has_capacity("SPY", book)
    assert room is False and "limit is 2 per underlying" in why

    room, _ = gate.has_capacity("QQQ", book)
    assert room is True, "a different underlying still has room"

    room, _ = gate.has_capacity("SPY", [])
    assert room is True, "an empty book always has room"


def test_the_gate_still_enforces_capacity_on_the_full_check():
    """The pre-check is an optimisation, not a replacement — a proposal that
    reaches the gates must still be rejected."""
    gate = RiskGate(RiskConfig(max_spreads_per_underlying=2))
    book = [
        FakePosition("SPY260908P00750000", "-3"),
        FakePosition("SPY260903P00757000", "-5"),
    ]

    @dataclass
    class FakeAccount:
        equity: str = "100000"

    proposal = TradeProposal(
        underlying="SPY",
        legs=(
            OptionLeg("SPY260910P00740000", "sell", "put", 740.0, "2026-09-10"),
            OptionLeg("SPY260910P00735000", "buy", "put", 735.0, "2026-09-10"),
        ),
        contracts=1,
        max_loss=400.0,
    )
    approved, why = gate.check(proposal, FakeAccount(), book)
    assert approved is False and "per underlying" in why
