"""Alpaca order placement — the only code allowed to transmit an order.

Nothing here places a trade without passing RiskGate.check() first, and
dry_run defaults to True so a forgotten flag cannot cost money.
"""

from __future__ import annotations

from dataclasses import dataclass

from alpaca.trading.enums import (
    OrderClass,
    OrderSide,
    PositionIntent,
    TimeInForce,
)
from alpaca.trading.requests import LimitOrderRequest, OptionLegRequest


@dataclass
class Decision:
    approved: bool
    reason: str
    submitted: bool = False
    order_id: str | None = None


def _leg_request(leg, opening: bool = True) -> OptionLegRequest:
    selling = leg.side == "sell"
    if opening:
        intent = PositionIntent.SELL_TO_OPEN if selling else PositionIntent.BUY_TO_OPEN
    else:
        intent = PositionIntent.BUY_TO_CLOSE if selling else PositionIntent.SELL_TO_CLOSE

    return OptionLegRequest(
        symbol=leg.symbol,
        ratio_qty=1,
        side=OrderSide.SELL if selling else OrderSide.BUY,
        position_intent=intent,
    )


def build_order(proposal, limit_price: float, opening: bool = True) -> LimitOrderRequest:
    """Turn a proposal into a multi-leg limit order.

    limit_price is the net price for the whole spread. Alpaca's sign
    convention for credits must be confirmed against a real fill before
    this runs unsupervised — verify in the dev account first.
    """
    return LimitOrderRequest(
        qty=proposal.contracts,
        order_class=OrderClass.MLEG,
        time_in_force=TimeInForce.DAY,
        limit_price=limit_price,
        legs=[_leg_request(leg, opening) for leg in proposal.legs],
    )


class OrderExecutor:
    def __init__(self, trading_client, gate, journal, dry_run: bool = True):
        self.trading_client = trading_client
        self.gate = gate
        self.journal = journal
        self.dry_run = dry_run

    def execute(
        self,
        proposal,
        account,
        positions,
        limit_price: float,
        opening: bool = True,
    ) -> Decision:
        """Gate the proposal, log the verdict, and place it only if allowed."""
        approved, reason = self.gate.check(proposal, account, positions)

        entry = {
            "underlying": proposal.underlying,
            "contracts": proposal.contracts,
            "max_loss": proposal.max_loss,
            "limit_price": limit_price,
            "thesis": proposal.thesis,
            "invalidation": proposal.invalidation,
            "legs": [
                {
                    "symbol": leg.symbol,
                    "side": leg.side,
                    "type": leg.option_type,
                    "strike": leg.strike,
                    "expiration": leg.expiration,
                }
                for leg in proposal.legs
            ],
            "approved": approved,
            "reason": reason,
            "dry_run": self.dry_run,
        }

        if not approved:
            self.journal.record(event="rejected", **entry)
            return Decision(approved=False, reason=reason)

        if self.dry_run:
            self.journal.record(event="dry_run", **entry)
            return Decision(approved=True, reason=f"{reason} (dry run — not sent)")

        try:
            order = self.trading_client.submit_order(
                build_order(proposal, limit_price, opening)
            )
        except Exception as exc:
            self.journal.record(event="submit_failed", error=str(exc), **entry)
            return Decision(approved=True, reason=f"Approved but submit failed: {exc}")

        self.journal.record(event="submitted", order_id=str(order.id), **entry)
        return Decision(
            approved=True, reason=reason, submitted=True, order_id=str(order.id)
        )
