"""Hard limits the agent cannot override.

Every proposed trade passes through check() before execution.
If any gate fails, the trade is rejected and logged.

These limits live in code, not in the prompt. An LLM can be argued out of an
instruction; it cannot be argued out of a function that returns False.
"""

from __future__ import annotations

from dataclasses import dataclass

# OCC option symbols end with YYMMDD + C/P + 8-digit strike = 15 characters.
_OCC_SUFFIX_LENGTH = 15


@dataclass
class RiskConfig:
    max_position_pct: float = 0.05      # max 5% of equity per position
    max_daily_loss_pct: float = 0.03    # halt trading after -3% day
    max_open_positions: int = 5
    max_spreads_per_underlying: int = 2
    max_contracts_per_order: int = 10
    allow_naked_short_options: bool = False


@dataclass(frozen=True)
class OptionLeg:
    """One leg of a proposed order."""

    symbol: str          # OCC symbol, e.g. SPY260831P00765000
    side: str            # "buy" or "sell"
    option_type: str     # "call" or "put"
    strike: float
    expiration: str      # YYYY-MM-DD


@dataclass(frozen=True)
class TradeProposal:
    """What the agent wants to do, and why.

    max_loss is the worst case in dollars for the whole order, not per
    contract. The agent must state it up front: a trade whose downside
    cannot be named is not a trade we take.
    """

    underlying: str
    legs: tuple[OptionLeg, ...]
    contracts: int
    max_loss: float
    thesis: str = ""
    invalidation: str = ""
    closing: bool = False   # closing reduces risk; entry gates do not apply


def _is_short(position) -> bool:
    try:
        return float(position.qty) < 0
    except (AttributeError, TypeError, ValueError):
        return False


def underlying_from_occ(symbol: str) -> str:
    """Pull the underlying out of an OCC symbol (SPY260831P00765000 -> SPY)."""
    if len(symbol) > _OCC_SUFFIX_LENGTH:
        return symbol[:-_OCC_SUFFIX_LENGTH].strip()
    return symbol.strip()


class RiskGate:
    def __init__(self, config: RiskConfig):
        self.config = config
        self.halted = False
        self.halt_reason = ""

    def check(self, proposal, account, positions) -> tuple[bool, str]:
        """Return (approved, reason).

        Closing orders bypass the entry gates, including the halt. A closing
        spread inverts its legs — the long becomes a sale — so the naked-short
        check would otherwise reject the agent's own exit, and a halted agent
        could never flatten the position that halted it.
        """
        if getattr(proposal, "closing", False):
            return True, "approved — closing order, risk reducing"

        if self.halted:
            return False, self.halt_reason or "Trading halted for the day"

        gates = (
            self._check_order_size,
            self._check_defined_risk,
            self._check_position_size,
            self._check_open_positions,
        )
        for gate in gates:
            approved, reason = gate(proposal, account, positions)
            if not approved:
                return False, reason

        return True, "approved"

    # --- individual gates -------------------------------------------------

    def _check_order_size(self, proposal, account, positions) -> tuple[bool, str]:
        """Cap contracts per order so one fat finger cannot size up the book."""
        if proposal.contracts < 1:
            return False, "Contract count must be at least 1"

        limit = self.config.max_contracts_per_order
        if proposal.contracts > limit:
            return False, (
                f"{proposal.contracts} contracts exceeds the per-order limit of {limit}"
            )
        return True, ""

    def _check_defined_risk(self, proposal, account, positions) -> tuple[bool, str]:
        """Every short leg needs a long leg capping its loss.

        A short put is protected by a long put at a lower strike; a short call
        by a long call at a higher strike. Same expiration, or the protection
        expires first and leaves the short exposed.
        """
        if self.config.allow_naked_short_options:
            return True, ""

        for short in (leg for leg in proposal.legs if leg.side == "sell"):
            protected = any(
                leg.side == "buy"
                and leg.option_type == short.option_type
                and leg.expiration == short.expiration
                and (
                    leg.strike < short.strike
                    if short.option_type == "put"
                    else leg.strike > short.strike
                )
                for leg in proposal.legs
            )
            if not protected:
                return False, (
                    f"Naked short {short.option_type} at {short.strike:g} "
                    f"({short.expiration}) — no protective long leg"
                )
        return True, ""

    def _check_position_size(self, proposal, account, positions) -> tuple[bool, str]:
        """No single trade may risk more than max_position_pct of equity."""
        try:
            equity = float(account.equity)
        except (AttributeError, TypeError, ValueError):
            return False, "Account equity unavailable — cannot size the trade"

        if equity <= 0:
            return False, "Account equity is zero or negative"

        if proposal.max_loss <= 0:
            return False, "Proposal must state a positive max_loss"

        cap = equity * self.config.max_position_pct
        if proposal.max_loss > cap:
            return False, (
                f"Max loss ${proposal.max_loss:,.2f} exceeds the "
                f"{self.config.max_position_pct:.0%} cap of ${cap:,.2f}"
            )
        return True, ""

    def _check_open_positions(self, proposal, account, positions) -> tuple[bool, str]:
        return self.has_capacity(proposal.underlying, positions)

    def has_capacity(self, underlying: str, positions) -> tuple[bool, str]:
        """Bound both the number of open spreads and how many sit on one name.

        Each defined-risk spread has exactly one short leg, so counting short
        option positions counts spreads — a spread occupies two rows in Alpaca
        and would otherwise double-count.

        The per-underlying cap is the one that matters. Five spreads on five
        different names are five bets; five spreads on SPY are one bet at five
        times the size, and the per-position risk cap cannot see that.

        Public because it depends only on the book, never on the proposal. The
        loop checks it before consulting the model: when there is no room, every
        candidate is rejected regardless of which one the model would pick, so
        asking is spending money on a judgement that cannot be acted on.
        """
        short_legs = [
            position
            for position in positions
            if len(position.symbol) > _OCC_SUFFIX_LENGTH and _is_short(position)
        ]

        total = len(short_legs)
        if total >= self.config.max_open_positions:
            return False, (
                f"Already holding {total} open spreads — limit is "
                f"{self.config.max_open_positions}"
            )

        same_name = [
            position
            for position in short_legs
            if underlying_from_occ(position.symbol) == underlying
        ]
        limit = self.config.max_spreads_per_underlying
        if len(same_name) >= limit:
            return False, (
                f"Already holding {len(same_name)} spreads on {underlying} — "
                f"limit is {limit} per underlying. Stacking one name concentrates "
                f"risk the per-position cap cannot see."
            )

        return True, ""

    # --- kill switch ------------------------------------------------------

    def update_daily_pnl(self, equity, starting_equity) -> None:
        """Trip the kill switch if the daily loss limit is breached."""
        try:
            equity = float(equity)
            starting_equity = float(starting_equity)
        except (TypeError, ValueError):
            return

        if starting_equity <= 0:
            return

        loss_pct = (starting_equity - equity) / starting_equity
        if loss_pct >= self.config.max_daily_loss_pct:
            self.halted = True
            self.halt_reason = (
                f"Daily loss limit hit: down {loss_pct:.2%} "
                f"(${starting_equity:,.2f} to ${equity:,.2f}). Trading halted."
            )

    def reset_for_new_day(self) -> None:
        """Clear the halt at the start of a new session."""
        self.halted = False
        self.halt_reason = ""
