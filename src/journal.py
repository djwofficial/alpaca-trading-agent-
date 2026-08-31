"""The decision trail.

Every gate verdict is appended here, approved or not. The rejections matter
as much as the fills: an agent that declined a trade and said why is the
part of this project a judge cannot get from a P&L screenshot.

One JSON object per line, so the dashboard can tail it without locking.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "logs" / "decisions.jsonl"


class Journal:
    def __init__(self, path: Path | str = DEFAULT_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, **fields) -> dict:
        entry = {"timestamp": datetime.now(timezone.utc).isoformat(), **fields}
        with self.path.open("a") as handle:
            handle.write(json.dumps(entry, default=str) + "\n")
        return entry

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self.path.open() as handle:
            return [json.loads(line) for line in handle if line.strip()]
