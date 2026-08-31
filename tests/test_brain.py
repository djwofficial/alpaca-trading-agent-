"""The brain is the least trustworthy component, so it is fenced on both sides.

Whatever the model returns, the loop must end up with a legal action or none.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agent.brain import (
    EntryDecision,
    ExitDecision,
    MarketContext,
    RuleBasedBrain,
    SingleAnalystBrain,
)
from strategy.spreads import Contract, SpreadCandidate


def candidate(short_strike: float = 760, bid: float = 0.60, ask: float = 0.20) -> SpreadCandidate:
    return SpreadCandidate(
        underlying="SPY",
        expiration=date(2026, 9, 4),
        short=Contract(f"SPY260904P00{int(short_strike*1000):06d}", "SPY",
                       date(2026, 9, 4), "put", short_strike, bid, bid + 0.02),
        long=Contract(f"SPY260904P00{int((short_strike-5)*1000):06d}", "SPY",
                      date(2026, 9, 4), "put", short_strike - 5, ask - 0.02, ask),
    )


CONTEXT = MarketContext(
    symbol="SPY", spot=769.28, prior_close=771.10,
    recent_closes=[765.9, 766.1, 771.1, 769.4], minutes_to_close=180,
    open_position_count=0,
)


@dataclass
class StubResponse:
    parsed_output: object


class StubClient:
    """Returns whatever we want the model to have said."""

    def __init__(self, output=None, error: Exception | None = None):
        self.output = output
        self.error = error
        self.calls = 0

    @property
    def messages(self):
        return self

    def parse(self, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return StubResponse(self.output)


def brain_returning(output=None, error=None) -> SingleAnalystBrain:
    return SingleAnalystBrain(client=StubClient(output, error))


def enter(index=0, contracts=4) -> EntryDecision:
    return EntryDecision(
        action="enter", candidate_index=index, contracts=contracts,
        thesis="holds above 760", invalidation="closes below 758",
        confidence="medium", reasoning="cushion is adequate",
    )


# --- context --------------------------------------------------------------

def test_session_change_is_measured_from_the_prior_close():
    assert CONTEXT.session_change_pct == pytest.approx((769.28 - 771.10) / 771.10)


def test_context_description_mentions_the_key_numbers():
    described = CONTEXT.describe()
    assert "769.28" in described and "SPY" in described


# --- the model cannot exceed its budget -----------------------------------

def test_oversized_pick_is_clamped_to_the_cap():
    """Prevents: the model talking itself into a larger position than allowed."""
    decision = brain_returning(enter(contracts=500)).decide_entry(
        [candidate()], CONTEXT, lambda c: 7
    )
    assert decision.action == "enter"
    assert decision.contracts == 7


def test_in_range_size_is_left_alone():
    decision = brain_returning(enter(contracts=3)).decide_entry(
        [candidate()], CONTEXT, lambda c: 7
    )
    assert decision.contracts == 3


def test_out_of_range_index_becomes_a_skip():
    """Prevents: an index error taking down the loop, or picking a nonexistent trade."""
    decision = brain_returning(enter(index=99)).decide_entry(
        [candidate()], CONTEXT, lambda c: 5
    )
    assert decision.action == "skip"


def test_zero_capacity_becomes_a_skip():
    decision = brain_returning(enter(contracts=5)).decide_entry(
        [candidate()], CONTEXT, lambda c: 0
    )
    assert decision.action == "skip"


def test_no_candidates_skips_without_calling_the_model():
    client = StubClient(enter())
    decision = SingleAnalystBrain(client=client).decide_entry([], CONTEXT, lambda c: 5)
    assert decision.action == "skip"
    assert client.calls == 0, "spent a model call on an empty menu"


# --- failure must degrade to inaction -------------------------------------

def test_model_failure_skips_rather_than_crashing():
    """Prevents: an API outage at 2am ending the overnight session."""
    decision = brain_returning(error=RuntimeError("503")).decide_entry(
        [candidate()], CONTEXT, lambda c: 5
    )
    assert decision.action == "skip"
    assert "Model unavailable" in decision.reasoning


def test_model_failure_holds_open_positions():
    """Closing on bad information is itself a decision — default to holding."""
    review = brain_returning(error=RuntimeError("timeout")).review_exit("pos", CONTEXT)
    assert review.action == "hold"


def test_exit_decision_passes_through():
    output = ExitDecision(action="close", thesis_still_valid=False, reasoning="broke 758")
    assert brain_returning(output).review_exit("pos", CONTEXT).action == "close"


# --- the deterministic fallback -------------------------------------------

def test_rule_brain_skips_when_nothing_clears_the_floor():
    brain = RuleBasedBrain(min_cushion_pct=0.50)
    assert brain.decide_entry([candidate()], CONTEXT, lambda c: 5).action == "skip"


def test_rule_brain_picks_the_richest_qualifying_credit():
    near, far = candidate(760, bid=0.60), candidate(755, bid=0.90)
    decision = RuleBasedBrain(min_cushion_pct=0.005, min_credit_dollars=10).decide_entry(
        [near, far], CONTEXT, lambda c: 5
    )
    assert decision.action == "enter"
    assert decision.candidate_index == 1


def test_rule_brain_always_states_an_invalidation():
    decision = RuleBasedBrain(min_cushion_pct=0.005, min_credit_dollars=10).decide_entry(
        [candidate()], CONTEXT, lambda c: 5
    )
    assert decision.invalidation and decision.thesis


def test_rule_brain_never_closes_early():
    assert RuleBasedBrain().review_exit("anything", CONTEXT).action == "hold"
