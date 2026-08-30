"""Agent entry point — the trading loop."""

import os
from dotenv import load_dotenv

load_dotenv()


def main():
    # 1. Load account state
    # 2. Fetch market + options chain data
    # 3. Agent proposes a trade
    # 4. RiskGate.check() approves or rejects
    # 5. Execute if approved
    # 6. Log the decision and reasoning
    pass


if __name__ == "__main__":
    main()
