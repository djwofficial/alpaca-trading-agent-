"""The agent's reasoning trail.

Deliberately not a rebuild of Alpaca's position blotter — Alpaca already
does that better. What Alpaca cannot show is *why* the agent acted, what it
committed to in advance, and whether that reasoning held up. That is the
whole product, so that is what this page is.

Reads logs/decisions.jsonl. It never places orders and never imports the
loop, so a crash here cannot touch trading.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# Locally the keys come from .env; on Streamlit Cloud they arrive as secrets.
# Bridge them into the environment so the same client code works in both.
for _name in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ALPACA_PAPER_TRADE"):
    if not os.getenv(_name):
        try:
            if _name in st.secrets:
                os.environ[_name] = str(st.secrets[_name])
        except Exception:
            pass  # no secrets.toml locally, which is fine

from data.client import (  # noqa: E402
    MissingCredentials,
    fetch_account,
    fetch_positions,
    load_credentials,
    trading_client,
)
from journal import Journal  # noqa: E402

st.set_page_config(page_title="Options Agent", layout="wide")

ENTERED = {"submitted", "dry_run"}


def money(value) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "—"


# --- account line ---------------------------------------------------------

st.title("Autonomous options agent")

account = None
positions: list = []
try:
    creds = load_credentials()
    client = trading_client(creds)
    account = fetch_account(client)
    positions = fetch_positions(client)
except MissingCredentials:
    st.warning("No Alpaca keys yet — showing the decision trail only.")
except Exception as exc:
    st.warning(f"Alpaca unreachable ({exc}). Showing the decision trail only.")

if account:
    equity = float(account.equity)
    start = 100_000.0
    left, middle, right = st.columns(3)
    left.metric("Equity", money(equity), f"{(equity - start) / start:+.2%}")
    middle.metric("Open positions", len(positions))
    right.metric("Mode", "Paper" if creds.paper else "LIVE")

st.divider()

# --- the scorecard --------------------------------------------------------

journal = Journal()
entries = journal.read()

if not entries:
    st.info(
        "No decisions logged yet. Run the agent:\n\n"
        "`python src/main.py --once --brain rules`"
    )
    st.stop()

decisions = [e for e in entries if e.get("event") in ENTERED | {"rejected", "skipped"}]
taken = [e for e in decisions if e.get("event") in ENTERED]
skipped = [e for e in decisions if e.get("event") == "skipped"]
rejected = [e for e in decisions if e.get("event") == "rejected"]

st.subheader("Scorecard")
a, b, c, d = st.columns(4)
a.metric("Decisions", len(decisions))
b.metric("Trades taken", len(taken))
c.metric("Declined by the agent", len(skipped))
d.metric("Blocked by risk gates", len(rejected))

calls = [e for e in entries if e.get("event") == "model_call"]
if calls:
    spend = sum(float(e.get("usd", 0)) for e in calls)
    per_decision = spend / len(decisions) if decisions else 0.0
    st.caption(
        f"Model spend: **${spend:.2f}** across {len(calls)} calls "
        f"(${per_decision:.3f} per decision). Entries think harder than exit "
        "reviews, and a skip with no candidates costs nothing at all."
    )

if decisions:
    discipline = len(skipped) / len(decisions)
    st.caption(
        f"The agent chose not to trade {discipline:.0%} of the time. "
        "Standing aside is a decision, and a cheap one — every skip below "
        "records the reasoning that produced it."
    )

st.divider()

# --- the decision trail ---------------------------------------------------

st.subheader("Decision trail")

label = {
    "submitted": ("🟢", "Entered"),
    "dry_run": ("🔵", "Entered (dry run)"),
    "skipped": ("⚪", "Declined"),
    "rejected": ("🔴", "Blocked by risk gate"),
    "exit_review": ("🟠", "Exit review"),
    "submit_failed": ("🔴", "Submit failed"),
    "cycle_error": ("🔴", "Cycle error"),
    "model_call": ("💭", "Model call"),
}

show_all = st.checkbox("Include exit reviews and errors", value=False)
visible = entries if show_all else decisions

for entry in reversed(visible[-40:]):
    event = entry.get("event", "?")
    icon, title = label.get(event, ("•", event))
    when = str(entry.get("timestamp", ""))[:19].replace("T", " ")
    underlying = entry.get("underlying", "")

    with st.container(border=True):
        header, meta = st.columns([3, 1])
        header.markdown(f"**{icon} {title}** · {underlying}")
        meta.caption(when)

        if entry.get("legs"):
            legs = entry["legs"]
            short = next((l for l in legs if l["side"] == "sell"), None)
            long = next((l for l in legs if l["side"] == "buy"), None)
            if short and long:
                header.markdown(
                    f"Sell {short['strike']:g}P / buy {long['strike']:g}P · "
                    f"{entry.get('contracts', '?')} contracts · "
                    f"exp {short['expiration']} · max loss {money(entry.get('max_loss'))}"
                )

        if entry.get("thesis"):
            st.markdown(f"**Thesis** — {entry['thesis']}")
        if entry.get("invalidation"):
            st.markdown(f"**Would be wrong if** — {entry['invalidation']}")
        if entry.get("reasoning"):
            st.markdown(f"_{entry['reasoning']}_")
        if event == "rejected":
            st.error(f"Gate: {entry.get('reason', '')}")
        elif entry.get("reason") and event in ENTERED:
            st.caption(entry["reason"])

st.divider()

# --- positions, kept small on purpose -------------------------------------

with st.expander(f"Open positions ({len(positions)})"):
    if positions:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "symbol": p.symbol,
                        "qty": p.qty,
                        "avg entry": money(p.avg_entry_price),
                        "current": money(p.current_price),
                        "unrealized P&L": money(p.unrealized_pl),
                    }
                    for p in positions
                ]
            ),
            width="stretch",
        )
    else:
        st.caption("Flat.")

with st.expander("Risk gates in force"):
    st.markdown(
        "- Max **5%** of equity at risk per position\n"
        "- Max **10** contracts per order\n"
        "- Max **5** open underlyings\n"
        "- **No naked short options** — every short leg needs a protective long\n"
        "- Trading **halts for the day** at −3%\n\n"
        "Enforced in `src/risk/gates.py`, in code, not in the prompt. "
        "The model picks from a menu these rules already approved."
    )
