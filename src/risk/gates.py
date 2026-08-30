"""Hard limits the agent cannot override.

Every proposed trade passes through check() before execution.
If any gate fails, the trade is rejected and logged.
"""

from dataclasses import dataclass


@dataclass
class RiskConfig:
    max_position_pct: float = 0.05      # max 5% of equity per position
    max_daily_loss_pct: float = 0.03    # halt trading after -3% day
    max_open_positions: int = 5
    max_contracts_per_order: int = 10
    allow_naked_short_options: bool = False


class RiskGate:
    def __init__(self, config: RiskConfig):
        self.config = config
        self.halted = False

    def check(self, proposal, account, positions) -> tuple[bool, str]:
        """Return (approved, reason)."""
        if self.halted:
            return False, "Trading halted for the day"

        # TODO: implement each gate
        return True, "approved"

    def update_daily_pnl(self, equity, starting_equity):
        """Trip the kill switch if the daily loss limit is breached."""
        loss_pct = (starting_equity - equity) / starting_equity
        if loss_pct >= self.config.max_daily_loss_pct:
            self.halted = True
