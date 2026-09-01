"""The daily loss limit must measure from the right place and survive a restart.

Two failures hide here, and both make the kill switch quietly weaker than it
reads: a baseline taken at the agent's first cycle rather than the prior
close, and a halt flag that lives only in memory.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import main
from risk.gates import RiskConfig, RiskGate


@dataclass
class FakeAccount:
    equity: str = "99920.28"
    last_equity: str = "100085.28"


@pytest.fixture(autouse=True)
def state_file(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    monkeypatch.setattr(main, "STATE_PATH", path)
    return path


@pytest.fixture
def gate() -> RiskGate:
    return RiskGate(RiskConfig())


def today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


# --- the baseline ---------------------------------------------------------

def test_baseline_is_the_prior_session_close():
    """Prevents: an overnight gap being invisible to the daily loss limit."""
    assert main.session_baseline(FakeAccount()) == pytest.approx(100085.28)


def test_baseline_falls_back_to_current_equity_when_absent():
    assert main.session_baseline(FakeAccount(last_equity="0")) == pytest.approx(99920.28)
    assert main.session_baseline(FakeAccount(last_equity=None)) == pytest.approx(99920.28)


def test_a_gap_down_open_counts_against_the_limit(gate):
    """Equity gapped 4% below yesterday's close before the first cycle ran.

    Baselining on the first cycle would call this a flat day and keep trading.
    """
    main.sync_halt(gate, baseline=100_000, equity=96_000)
    assert gate.halted
    assert "Daily loss limit" in gate.halt_reason


# --- the halt latch -------------------------------------------------------

def test_halt_is_written_to_disk(gate, state_file):
    main.sync_halt(gate, baseline=100_000, equity=96_000)

    import json
    state = json.loads(state_file.read_text())
    assert state["halted"] is True
    assert state["date"] == today()
    assert "Daily loss limit" in state["halt_reason"]


def test_halt_survives_a_restart(gate, state_file):
    """Prevents: restarting the process clearing the day's kill switch.

    The recovered equity is well inside the limit, so only the persisted flag
    can keep the agent halted.
    """
    main.sync_halt(gate, baseline=100_000, equity=96_000)
    assert gate.halted

    fresh = RiskGate(RiskConfig())            # as if the process just started
    main.sync_halt(fresh, baseline=100_000, equity=99_500)

    assert fresh.halted, "a restart cleared the daily halt"
    assert "Daily loss limit" in fresh.halt_reason


def test_a_new_day_clears_the_halt(gate, state_file):
    main.sync_halt(gate, baseline=100_000, equity=96_000)

    state_file.write_text(state_file.read_text().replace(today(), "2020-01-01"))
    fresh = RiskGate(RiskConfig())
    main.sync_halt(fresh, baseline=100_000, equity=99_500)

    assert not fresh.halted


def test_a_quiet_day_never_halts(gate):
    main.sync_halt(gate, baseline=100_085.28, equity=99_920.28)   # today, -0.16%
    assert not gate.halted


# --- the heartbeat --------------------------------------------------------

def test_every_cycle_stamps_the_state_file(state_file):
    """Prevents: a healthy idle agent and a wedged one leaving the same trail."""
    main.heartbeat("no_candidates")

    import json
    state = json.loads(state_file.read_text())
    assert state["last_status"] == "no_candidates"
    assert state["last_cycle"].startswith(today())


def test_state_survives_an_unreadable_file(state_file):
    state_file.write_text("{ not json")
    assert main.read_state() == {}
    main.write_state(date=today())
    assert main.read_state()["date"] == today()
