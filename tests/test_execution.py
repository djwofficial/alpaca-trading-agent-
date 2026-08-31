"""The executor must never transmit an order the gates rejected.

Every test here is really one assertion: submit_order was not called.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from execution.orders import OrderExecutor, build_order
from journal import Journal
from risk.gates import OptionLeg, RiskConfig, RiskGate, TradeProposal


@dataclass
class FakeAccount:
    equity: str = "100000"


@dataclass
class FakeOrder:
    id: str = "order-123"


@dataclass
class SpyTradingClient:
    """Records every submit so tests can assert it never happened."""

    calls: list = field(default_factory=list)

    def submit_order(self, request):
        self.calls.append(request)
        return FakeOrder()


def spread(max_loss: float = 465.0, contracts: int = 1) -> TradeProposal:
    return TradeProposal(
        underlying="SPY",
        legs=(
            OptionLeg("SPY260904P00765000", "sell", "put", 765, "2026-09-04"),
            OptionLeg("SPY260904P00760000", "buy", "put", 760, "2026-09-04"),
        ),
        contracts=contracts,
        max_loss=max_loss,
        thesis="SPY holds above 765 into Friday",
        invalidation="A close below 766 or VIX above 25",
    )


@pytest.fixture
def journal(tmp_path) -> Journal:
    return Journal(tmp_path / "decisions.jsonl")


@pytest.fixture
def client() -> SpyTradingClient:
    return SpyTradingClient()


def executor(client, journal, *, dry_run, config=None) -> OrderExecutor:
    return OrderExecutor(
        client, RiskGate(config or RiskConfig()), journal, dry_run=dry_run
    )


# --- the order must not go out --------------------------------------------

def test_rejected_proposal_is_never_submitted(client, journal):
    ex = executor(client, journal, dry_run=False)
    decision = ex.execute(spread(max_loss=99_999), FakeAccount(), [], limit_price=0.35)

    assert not decision.approved
    assert client.calls == [], "gate rejected the trade but it was sent anyway"
    assert not decision.submitted


def test_dry_run_never_submits(client, journal):
    ex = executor(client, journal, dry_run=True)
    decision = ex.execute(spread(), FakeAccount(), [], limit_price=0.35)

    assert decision.approved
    assert not decision.submitted
    assert client.calls == [], "dry run transmitted a live order"
    assert "dry run" in decision.reason


def test_halted_gate_never_submits(client, journal):
    ex = executor(client, journal, dry_run=False)
    ex.gate.update_daily_pnl(equity=95_000, starting_equity=100_000)

    decision = ex.execute(spread(), FakeAccount(), [], limit_price=0.35)

    assert not decision.approved
    assert client.calls == []
    assert "Daily loss limit" in decision.reason


def test_dry_run_is_the_default(client, journal):
    """A forgotten flag must fail safe, not place a trade."""
    ex = OrderExecutor(client, RiskGate(RiskConfig()), journal)
    ex.execute(spread(), FakeAccount(), [], limit_price=0.35)
    assert client.calls == []


# --- the order goes out when it should ------------------------------------

def test_approved_live_trade_is_submitted(client, journal):
    ex = executor(client, journal, dry_run=False)
    decision = ex.execute(spread(), FakeAccount(), [], limit_price=0.35)

    assert decision.approved and decision.submitted
    assert decision.order_id == "order-123"
    assert len(client.calls) == 1


def test_submit_failure_is_caught_not_raised(client, journal):
    """A broker error must not kill the overnight loop."""

    class Failing(SpyTradingClient):
        def submit_order(self, request):
            raise RuntimeError("connection reset")

    ex = executor(Failing(), journal, dry_run=False)
    decision = ex.execute(spread(), FakeAccount(), [], limit_price=0.35)

    assert not decision.submitted
    assert "submit failed" in decision.reason


# --- the trail ------------------------------------------------------------

def test_rejections_are_journalled_with_the_reason(client, journal):
    ex = executor(client, journal, dry_run=False)
    ex.execute(spread(max_loss=99_999), FakeAccount(), [], limit_price=0.35)

    entries = journal.read()
    assert len(entries) == 1
    assert entries[0]["event"] == "rejected"
    assert entries[0]["approved"] is False
    assert "exceeds" in entries[0]["reason"]


def test_thesis_and_invalidation_are_recorded(client, journal):
    ex = executor(client, journal, dry_run=True)
    ex.execute(spread(), FakeAccount(), [], limit_price=0.35)

    entry = journal.read()[0]
    assert entry["thesis"] == "SPY holds above 765 into Friday"
    assert entry["invalidation"] == "A close below 766 or VIX above 25"
    assert len(entry["legs"]) == 2


# --- order construction ---------------------------------------------------

def test_order_is_multi_leg_with_matching_quantity():
    order = build_order(spread(contracts=3), limit_price=0.35)
    assert order.order_class.value == "mleg"
    assert order.qty == 3
    assert len(order.legs) == 2


def test_opening_legs_use_open_position_intents():
    order = build_order(spread(), limit_price=0.35)
    intents = {leg.side.value: leg.position_intent.value for leg in order.legs}
    assert intents["sell"] == "sell_to_open"
    assert intents["buy"] == "buy_to_open"


def test_closing_legs_use_close_position_intents():
    order = build_order(spread(), limit_price=0.35, opening=False)
    intents = {leg.side.value: leg.position_intent.value for leg in order.legs}
    assert intents["sell"] == "buy_to_close"
    assert intents["buy"] == "sell_to_close"
