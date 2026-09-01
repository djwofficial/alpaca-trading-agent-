"""The judgment layer.

The brain chooses from a menu the code already built and the gates already
vetted. It cannot invent strikes, size itself up, or go naked — the worst a
confused model can do here is pick a legal trade or decline to trade.

Two jobs:
  decide_entry  — take one of these candidates, or none
  review_exit   — the thesis you wrote earlier: does it still hold?

Brain is an interface. SingleAnalystBrain is one implementation; a committee
of debating agents is another, and swapping it changes nothing downstream.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

MODEL = "claude-opus-5"

# US dollars per million tokens, input then output. Thinking tokens bill as
# output, which is why effort is the lever that moves this number. Kept here
# so the journal can record what a decision actually cost instead of an
# estimate; an unknown model prices at zero rather than guessing.
PRICING = {
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}


@dataclass(frozen=True)
class CallCost:
    """What one model call cost. None whenever no model was called."""

    purpose: str  # "entry" or "exit"
    model: str
    effort: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    usd: float

    def as_record(self) -> dict:
        return {
            "purpose": self.purpose,
            "model": self.model,
            "effort": self.effort,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "usd": round(self.usd, 4),
        }


@dataclass
class MarketContext:
    """What the agent knows about right now."""

    symbol: str
    spot: float
    prior_close: float
    recent_closes: list[float]
    minutes_to_close: int
    open_position_count: int

    @property
    def session_change_pct(self) -> float:
        if not self.prior_close:
            return 0.0
        return (self.spot - self.prior_close) / self.prior_close

    def describe(self) -> str:
        closes = ", ".join(f"{c:.2f}" for c in self.recent_closes)
        return (
            f"{self.symbol} is at {self.spot:.2f}, "
            f"{self.session_change_pct:+.2%} from the prior close of {self.prior_close:.2f}.\n"
            f"Last daily closes (oldest first): {closes}\n"
            f"Minutes until the market closes: {self.minutes_to_close}\n"
            f"Open positions right now: {self.open_position_count}"
        )


class EntryDecision(BaseModel):
    action: Literal["enter", "skip"]
    candidate_index: int | None = Field(
        default=None, description="Index of the chosen candidate, or null when skipping"
    )
    contracts: int = Field(default=0, description="0 when skipping")
    thesis: str = Field(description="Why this trade should work, in one or two sentences")
    invalidation: str = Field(
        description="The specific, checkable condition that would prove the thesis wrong"
    )
    confidence: Literal["low", "medium", "high"]
    reasoning: str = Field(description="Brief explanation of the decision, including why not the alternatives")


class ExitDecision(BaseModel):
    action: Literal["hold", "close"]
    thesis_still_valid: bool
    reasoning: str


ENTRY_SYSTEM = """You are the decision layer of an autonomous options trading agent \
running on a paper account during a one-week competition. You trade put credit \
spreads on liquid ETFs: selling a put and buying a lower-strike put of the same \
expiry, which profits when the underlying stays above the short strike.

You will be given candidate spreads that have already passed every risk check. \
Your only choices are to take one of them or to take none.

Understand the economics honestly. Each candidate lists the win rate it needs \
just to break even, and the market prices these close to their true probability. \
Strike selection alone is therefore not an edge — picking the "best looking" row \
is a coin flip with extra steps. Real edge comes from two places only:

1. Selectivity: trading when the premium overpays for the actual risk, and \
   standing aside otherwise.
2. Exits: cutting a position when the reasoning breaks, before the loss reaches \
   its maximum. Losses here are roughly ten times the size of wins, so one \
   avoided maximum loss is worth many collected credits.

Because of this, SKIP is frequently the correct answer, and a skip costs nothing. \
Do not trade to look busy. Do not trade merely because candidates exist. A day \
with no position is a perfectly good day.

When you do enter, you must write two things:
- thesis: why the underlying should stay above the short strike
- invalidation: a specific, checkable condition that would prove you wrong

The invalidation must be concrete enough that a later check can evaluate it \
mechanically — a price level, a percentage move, a time. "Market conditions \
worsen" is useless. "SPY closes below 758, or falls more than 1.2% intraday" is \
usable. You are committing in advance to what would change your mind, so that \
later you cannot rationalize.

Never exceed the max_contracts shown for a candidate."""

EXIT_SYSTEM = """You are reviewing an open options position that you opened earlier.

You were given a thesis and an invalidation condition at entry. Your job now is \
narrow and specific: has the invalidation condition occurred?

Be honest rather than hopeful. The whole value of writing an invalidation down in \
advance is that you cannot argue your way around it afterwards. If the condition \
has triggered, close the position — do not reason about how it might recover.

If the condition has not triggered, hold. Do not close a position simply because \
it is showing an unrealized loss; these positions are expected to fluctuate, and \
the invalidation is the line that matters, not the mark."""


def _candidate_lines(candidates, spot: float, max_contracts_fn) -> str:
    rows = []
    for index, candidate in enumerate(candidates):
        rows.append(
            f"[{index}] {candidate.expiration.isoformat()} "
            f"sell {candidate.short.strike:g}P / buy {candidate.long.strike:g}P | "
            f"credit ${candidate.credit_dollars:.0f} | "
            f"max loss ${candidate.max_loss_per_contract:.0f}/contract | "
            f"cushion {candidate.cushion_pct(spot):.2%} | "
            f"breakeven {candidate.breakeven:.2f} | "
            f"needs {candidate.required_win_rate():.0%} win rate | "
            f"max_contracts {max_contracts_fn(candidate)}"
        )
    return "\n".join(rows)


class Brain(ABC):
    """Swap in a committee, a single analyst, or a rule — the loop does not care.

    Every brain reports what its last call cost. A brain that calls no model
    reports None and zero, so the loop logs cost the same way regardless.
    """

    last_call: CallCost | None = None
    session_usd: float = 0.0

    @abstractmethod
    def decide_entry(self, candidates, context: MarketContext, max_contracts_fn) -> EntryDecision:
        ...

    @abstractmethod
    def review_exit(self, position_summary: str, context: MarketContext) -> ExitDecision:
        ...


class SingleAnalystBrain(Brain):
    """One model, one opinion, structured output."""

    def __init__(
        self,
        client=None,
        model: str = MODEL,
        entry_effort: str = "xhigh",
        exit_effort: str = "high",
    ):
        """Effort is split because the two calls are not the same problem.

        Entries are rare, irreversible, and worth thinking hard about. Exits
        run once per open position every cycle and ask a narrower question —
        has a condition written down in advance occurred — so they get the
        cheaper setting.
        """
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self.client = client
        self.model = model
        self.entry_effort = entry_effort
        self.exit_effort = exit_effort
        self.last_call: CallCost | None = None
        self.session_usd = 0.0

    def _parse(self, system: str, prompt: str, schema, purpose: str, effort: str):
        response = self.client.messages.parse(
            model=self.model,
            max_tokens=16000,
            system=system,
            thinking={"type": "adaptive"},
            output_config={"effort": effort},
            messages=[{"role": "user", "content": prompt}],
            output_format=schema,
        )
        self.last_call = self._price(response, purpose, effort)
        if self.last_call is not None:
            self.session_usd += self.last_call.usd
        return response.parsed_output

    def _price(self, response, purpose: str, effort: str) -> CallCost | None:
        """Cost accounting must never be the thing that breaks a trading loop."""
        usage = getattr(response, "usage", None)
        if usage is None:
            return None

        def count(name: str) -> int:
            return int(getattr(usage, name, 0) or 0)

        read_in = count("input_tokens")
        out = count("output_tokens")
        cache_read = count("cache_read_input_tokens")
        cache_write = count("cache_creation_input_tokens")

        in_rate, out_rate = PRICING.get(self.model, (0.0, 0.0))
        usd = (
            read_in * in_rate
            + cache_read * in_rate * 0.10  # cached reads bill at a tenth
            + cache_write * in_rate * 1.25  # writing the cache costs a premium
            + out * out_rate
        ) / 1_000_000

        return CallCost(
            purpose=purpose,
            model=self.model,
            effort=effort,
            input_tokens=read_in + cache_read + cache_write,
            output_tokens=out,
            cache_read_tokens=cache_read,
            usd=usd,
        )

    def decide_entry(self, candidates, context, max_contracts_fn) -> EntryDecision:
        self.last_call = None  # a skip below costs nothing; do not log the last one twice
        if not candidates:
            return EntryDecision(
                action="skip",
                thesis="",
                invalidation="",
                confidence="high",
                reasoning="No candidates cleared the risk filters.",
            )

        prompt = (
            f"{context.describe()}\n\n"
            f"Candidate spreads on {context.symbol} (all pre-approved by the risk gates):\n"
            f"{_candidate_lines(candidates, context.spot, max_contracts_fn)}\n\n"
            "Choose one candidate by index, or skip. If you enter, size it at or "
            "below that candidate's max_contracts."
        )
        try:
            decision = self._parse(ENTRY_SYSTEM, prompt, EntryDecision, "entry", self.entry_effort)
        except Exception as exc:
            return EntryDecision(
                action="skip",
                thesis="",
                invalidation="",
                confidence="low",
                reasoning=f"Model unavailable ({type(exc).__name__}: {exc}); skipping.",
            )
        return _sanitize_entry(decision, candidates, max_contracts_fn)

    def review_exit(self, position_summary: str, context: MarketContext) -> ExitDecision:
        self.last_call = None
        prompt = (
            f"{context.describe()}\n\n"
            f"The open position and what you said at entry:\n{position_summary}\n\n"
            "Has the invalidation condition occurred? Answer hold or close."
        )
        try:
            return self._parse(EXIT_SYSTEM, prompt, ExitDecision, "exit", self.exit_effort)
        except Exception as exc:
            return ExitDecision(
                action="hold",
                thesis_still_valid=True,
                reasoning=f"Model unavailable ({type(exc).__name__}: {exc}); holding.",
            )


class RuleBasedBrain(Brain):
    """Deterministic fallback — no API key, no network, no judgement.

    Exists so the loop can be exercised end to end without spending tokens,
    and so a model outage degrades to something safe rather than to nothing.
    """

    def __init__(self, min_cushion_pct: float = 0.012, min_credit_dollars: float = 15.0):
        self.min_cushion_pct = min_cushion_pct
        self.min_credit_dollars = min_credit_dollars

    def decide_entry(self, candidates, context, max_contracts_fn) -> EntryDecision:
        viable = [
            (index, candidate)
            for index, candidate in enumerate(candidates)
            if candidate.cushion_pct(context.spot) >= self.min_cushion_pct
            and candidate.credit_dollars >= self.min_credit_dollars
        ]
        if not viable:
            return EntryDecision(
                action="skip",
                thesis="",
                invalidation="",
                confidence="high",
                reasoning="No candidate met the cushion and credit thresholds.",
            )

        index, candidate = max(viable, key=lambda pair: pair[1].credit_dollars)
        return EntryDecision(
            action="enter",
            candidate_index=index,
            contracts=max(1, max_contracts_fn(candidate)),
            thesis=(
                f"{context.symbol} holds above {candidate.short.strike:g} "
                f"with a {candidate.cushion_pct(context.spot):.2%} cushion."
            ),
            invalidation=f"{context.symbol} trades below {candidate.breakeven:.2f}.",
            confidence="low",
            reasoning="Rule-based fallback: highest credit meeting the cushion floor.",
        )

    def review_exit(self, position_summary: str, context: MarketContext) -> ExitDecision:
        return ExitDecision(
            action="hold",
            thesis_still_valid=True,
            reasoning="Rule-based fallback does not close early.",
        )


def _sanitize_entry(decision: EntryDecision, candidates, max_contracts_fn) -> EntryDecision:
    """Never trust the model's arithmetic. Clamp it to what is actually legal."""
    if decision.action != "enter":
        return decision

    index = decision.candidate_index
    if index is None or not 0 <= index < len(candidates):
        return EntryDecision(
            action="skip",
            thesis=decision.thesis,
            invalidation=decision.invalidation,
            confidence="low",
            reasoning=f"Model chose an out-of-range candidate ({index}); skipping.",
        )

    ceiling = max_contracts_fn(candidates[index])
    contracts = max(0, min(decision.contracts, ceiling))
    if contracts < 1:
        return EntryDecision(
            action="skip",
            candidate_index=index,
            thesis=decision.thesis,
            invalidation=decision.invalidation,
            confidence="low",
            reasoning="Sizing collapsed to zero contracts under the risk cap; skipping.",
        )

    return decision.model_copy(update={"contracts": contracts})
