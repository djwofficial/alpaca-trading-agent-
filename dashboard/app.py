"""Connection check — proves the Alpaca plumbing works.

This is deliberately not the demo dashboard. It answers four questions:
do the keys authenticate, does the account come back, do positions render,
and can we pull an options chain. Charts and decision history come later,
once there is something real to put in them.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from data.client import (  # noqa: E402
    MissingCredentials,
    fetch_account,
    fetch_option_chain,
    fetch_option_contracts,
    fetch_positions,
    load_credentials,
    option_data_client,
    trading_client,
)

st.set_page_config(page_title="Agent — Connection Check", layout="wide")
st.title("Connection check")
st.caption("Step 1 of the build: prove the plumbing before anything trades.")


def money(value) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "—"


# --- 1. Credentials -------------------------------------------------------
try:
    creds = load_credentials()
except MissingCredentials as exc:
    st.warning(str(exc))
    st.code("cp .env.example .env", language="bash")
    st.stop()

if creds.paper:
    st.success("Paper trading — ALPACA_PAPER_TRADE is on.")
else:
    st.error("LIVE TRADING. The hackathon is paper-only. Set ALPACA_PAPER_TRADE=true.")

# --- 2. Account -----------------------------------------------------------
st.subheader("Account")

if st.button("Refresh"):
    st.rerun()

try:
    account = fetch_account(trading_client(creds))
except Exception as exc:
    st.error(f"Account fetch failed: {exc}")
    st.stop()

left, middle, right = st.columns(3)
left.metric("Equity", money(account.equity))
middle.metric("Buying power", money(account.buying_power))
right.metric("Options buying power", money(account.options_buying_power))

st.write(
    f"Account `{account.account_number}` · status **{account.status}** · "
    f"options level **{account.options_trading_level}** "
    f"(approved: {account.options_approved_level})"
)

# Spreads need level 3. Finding this out now beats finding out at the open.
try:
    if int(account.options_trading_level or 0) < 3:
        st.warning(
            "Options level is below 3, so multi-leg spreads will be rejected. "
            "Raise the level in the Alpaca dashboard before going live."
        )
except (TypeError, ValueError):
    pass

for flag in ("trading_blocked", "account_blocked", "trade_suspended_by_user"):
    if getattr(account, flag, False):
        st.error(f"{flag} is set — orders will not go through.")

# --- 3. Positions ---------------------------------------------------------
st.subheader("Positions")

try:
    positions = fetch_positions(trading_client(creds))
except Exception as exc:
    st.error(f"Positions fetch failed: {exc}")
    positions = []

if positions:
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "symbol": p.symbol,
                    "qty": p.qty,
                    "side": p.side,
                    "avg entry": money(p.avg_entry_price),
                    "current": money(p.current_price),
                    "market value": money(p.market_value),
                    "unrealized P&L": money(p.unrealized_pl),
                }
                for p in positions
            ]
        ),
        width="stretch",
    )
else:
    st.info("No open positions. Expected — nothing trades yet.")

# --- 4. Options chain -----------------------------------------------------
st.subheader("Options chain")

symbol = st.text_input("Underlying", value="SPY").strip().upper()
days_out = st.slider("Days to expiration", 1, 45, 10)

if symbol:
    try:
        contracts = fetch_option_contracts(
            trading_client(creds), symbol, days_out=days_out
        )
    except Exception as exc:
        st.error(f"Contract fetch failed: {exc}")
        contracts = []

    if not contracts:
        st.info(f"No contracts returned for {symbol} within {days_out} days.")
    else:
        st.write(f"{len(contracts)} contracts (metadata — served outside market hours)")

        quotes: dict = {}
        try:
            quotes = fetch_option_chain(
                option_data_client(creds), symbol, days_out=days_out
            )
        except Exception as exc:
            st.caption(f"Live snapshots unavailable: {exc}")

        rows = []
        for c in contracts:
            snapshot = quotes.get(c.symbol)
            quote = getattr(snapshot, "latest_quote", None)
            rows.append(
                {
                    "symbol": c.symbol,
                    "type": getattr(c.type, "value", c.type),
                    "strike": float(c.strike_price),
                    "expiration": c.expiration_date,
                    "open interest": c.open_interest,
                    "bid": getattr(quote, "bid_price", None),
                    "ask": getattr(quote, "ask_price", None),
                    "IV": getattr(snapshot, "implied_volatility", None),
                }
            )

        st.dataframe(
            pd.DataFrame(rows).sort_values(["expiration", "type", "strike"]),
            width="stretch",
        )

        if not quotes:
            st.caption(
                "Bid/ask/IV are empty because the market is closed. "
                "Contract metadata returning is what proves the plumbing works."
            )
