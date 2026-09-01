"""The decision trail, as a judge sees it.

Reads logs/decisions.jsonl for the reasoning and Alpaca for the live book. It
never places orders and never imports the loop, so it is safe to leave open
while the agent trades — every call in here is read-only.

Layout follows what a reviewer needs, in order: is the agent alive, what is it
holding, what stops it, then how it has been thinking.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

import altair as alt
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
    fetch_spot,
    load_credentials,
    stock_data_client,
    trading_client,
)
from journal import Journal  # noqa: E402
from risk.gates import RiskConfig  # noqa: E402
from risk.stops import (  # noqa: E402
    StopConfig,
    group_into_spreads,
    should_stop_out,
    summarize_spread,
)

st.set_page_config(
    page_title="Options Agent",
    page_icon="◆",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# --- palette --------------------------------------------------------------
# The validated dark set. Status hues are reserved: they mean state, never
# series identity, and each one ships with a text label so colour never
# carries the meaning alone.
INK, INK2, MUTED = "#ffffff", "#c3c2b7", "#898781"
SURFACE, LINE = "#1a1a19", "rgba(255,255,255,0.10)"
GRID, BASELINE = "#2c2c2a", "#383835"
GOOD, WARN, SERIOUS, CRIT, BLUE = "#0ca30c", "#fab219", "#ec835a", "#d03b3b", "#3987e5"
ORANGE = "#d95926"  # categorical slot 2 — the benchmark, never a status

STYLE = """
<style>
.block-container {padding-top: 2rem; padding-bottom: 3rem; max-width: 1240px;}
#MainMenu, footer {visibility: hidden;}
.card {background:#1a1a19; border:1px solid rgba(255,255,255,.10);
       border-radius:10px; padding:14px 16px; height:100%;}
.grid {display:grid; gap:12px; margin-bottom:12px;}
.g4 {grid-template-columns:repeat(auto-fit, minmax(190px,1fr));}
.g3 {grid-template-columns:repeat(auto-fit, minmax(250px,1fr));}
.g2 {grid-template-columns:repeat(auto-fit, minmax(330px,1fr));}
.lbl {color:#898781; font-size:.72rem; letter-spacing:.07em;
      text-transform:uppercase; margin-bottom:6px;}
.val {font-size:1.85rem; font-weight:600; line-height:1.15;}
.sub {color:#c3c2b7; font-size:.8rem; margin-top:4px;}
.pill {display:inline-flex; align-items:center; gap:7px; padding:5px 12px;
       border-radius:999px; font-size:.82rem; font-weight:500;
       border:1px solid rgba(255,255,255,.10);}
.dot {width:8px; height:8px; border-radius:50%; flex:none;}
.hd {display:flex; align-items:center; justify-content:space-between;
     gap:16px; flex-wrap:wrap; margin-bottom:6px;}
h1.t {font-size:1.6rem; font-weight:600; margin:0; color:#fff;}
.sec {color:#898781; font-size:.72rem; letter-spacing:.09em;
      text-transform:uppercase; margin:26px 0 10px;}
.meter {height:4px; border-radius:2px; background:rgba(255,255,255,.09);
        margin-top:9px; overflow:hidden;}
.fill {height:100%; border-radius:2px;}
.row {background:#1a1a19; border:1px solid rgba(255,255,255,.10);
      border-left:3px solid #898781; border-radius:8px;
      padding:11px 14px; margin-bottom:8px;}
.rh {display:flex; justify-content:space-between; gap:12px; align-items:baseline;}
.rt {font-weight:600; font-size:.92rem;}
.tm {color:#898781; font-size:.75rem; font-variant-numeric:tabular-nums; flex:none;}
.leg {color:#c3c2b7; font-size:.84rem; margin-top:5px;
      font-variant-numeric:tabular-nums;}
.qt {color:#c3c2b7; font-size:.85rem; margin-top:7px; line-height:1.5;}
.clamp {display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical;
        overflow:hidden;}
.tag {color:#898781; font-size:.7rem; letter-spacing:.06em;
      text-transform:uppercase;}
.strip {position:relative; height:8px; border-radius:4px;
        background:rgba(255,255,255,.06); margin:14px 0 7px;}
.zone {position:absolute; top:0; bottom:0; border-radius:4px;}
.tick {position:absolute; top:-4px; bottom:-4px; width:2px; border-radius:1px;}
.spot {position:absolute; top:50%; width:11px; height:11px; border-radius:50%;
       transform:translate(-50%,-50%); border:2px solid #1a1a19;}
.scale {display:flex; justify-content:space-between; color:#898781;
        font-size:.7rem; font-variant-numeric:tabular-nums;}
</style>
"""
st.markdown(STYLE, unsafe_allow_html=True)


def money(value, sign: bool = False) -> str:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "—"
    lead = "+" if sign and amount >= 0 else ""
    return f"{lead}${amount:,.2f}"


def tile(label: str, value: str, sub: str = "", colour: str = INK) -> str:
    return (
        f'<div class="card"><div class="lbl">{escape(label)}</div>'
        f'<div class="val" style="color:{colour}">{escape(value)}</div>'
        f'<div class="sub">{sub}</div></div>'
    )


def meter(used: float, limit: float, colour: str) -> str:
    pct = 0.0 if limit <= 0 else max(0.0, min(1.0, used / limit))
    return (
        f'<div class="meter"><div class="fill" '
        f'style="width:{pct * 100:.0f}%; background:{colour}"></div></div>'
    )


@st.cache_data(ttl=90, show_spinner=False)
def performance_series() -> pd.DataFrame | None:
    """Account equity and SPY on one axis, both indexed to their first point.

    Two measures of different scale never belong on two y-axes; indexing both
    to a common base puts them on one, and it happens to be the comparison
    that matters — the agent's return against simply holding the thing it
    trades. The window opens at the agent's first session, because a flat line
    from before it had a position is not performance.
    """
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from alpaca.trading.requests import GetPortfolioHistoryRequest

    keys = load_credentials()
    history = trading_client(keys).get_portfolio_history(
        GetPortfolioHistoryRequest(period="1W", timeframe="15Min")
    )
    points = [
        (datetime.fromtimestamp(stamp, timezone.utc), float(value))
        for stamp, value in zip(history.timestamp, history.equity)
        if value
    ]
    if len(points) < 2:
        return None
    equity = pd.Series([v for _, v in points], index=[t for t, _ in points])

    bars = stock_data_client(keys).get_stock_bars(
        StockBarsRequest(
            symbol_or_symbols="SPY",
            timeframe=TimeFrame(15, TimeFrameUnit.Minute),
            start=points[0][0] - timedelta(hours=2),
        )
    )
    quotes = pd.Series({bar.timestamp: float(bar.close) for bar in bars["SPY"]})
    if quotes.empty:
        return None

    # Carry the last print onto each equity stamp rather than interpolating:
    # the two series are sampled on different clocks, and a straight line
    # between prints invents a price the market never showed.
    quotes = quotes.sort_index()
    aligned = quotes.reindex(quotes.index.union(equity.index)).ffill()
    frame = pd.DataFrame({"Agent": equity, "SPY": aligned.reindex(equity.index)})
    frame = frame.dropna()
    if len(frame) < 2:
        return None
    frame = frame / frame.iloc[0] - 1.0
    return frame.reset_index(names="when").melt(
        "when", var_name="series", value_name="change"
    )


def performance_chart(frame: pd.DataFrame) -> alt.LayerChart:
    """Two lines, one axis, a legend, and a hover readout on every point."""
    hue = alt.Color(
        "series:N",
        scale=alt.Scale(domain=["Agent", "SPY"], range=[BLUE, ORANGE]),
        legend=alt.Legend(
            title=None, orient="top-left", labelColor=INK2,
            symbolStrokeWidth=3, symbolSize=110, labelFontSize=12,
        ),
    )
    position = {
        "x": alt.X(
            "when:T",
            axis=alt.Axis(
                title=None, format="%a %H:%M", tickCount=5, labelColor=MUTED,
                domainColor=BASELINE, tickColor=BASELINE, grid=False,
                labelFontSize=11,
            ),
        ),
        "y": alt.Y(
            "change:Q",
            axis=alt.Axis(
                title=None, format="+.1%", labelColor=MUTED, gridColor=GRID,
                domainOpacity=0, tickOpacity=0, labelFontSize=11,
            ),
        ),
    }
    flat = (
        alt.Chart(pd.DataFrame({"y": [0.0]}))
        .mark_rule(color=BASELINE, strokeDash=[3, 3])
        .encode(y=alt.Y("y:Q"))
    )
    line = alt.Chart(frame).mark_line(strokeWidth=2, interpolate="monotone").encode(
        color=hue, **position
    )
    # An invisible fat mark on every point: the hit target for the tooltip is
    # meant to be bigger than the line it reads from.
    hover = alt.Chart(frame).mark_circle(size=90, opacity=0).encode(
        color=hue,
        tooltip=[
            alt.Tooltip("when:T", title="Time", format="%b %d, %H:%M"),
            alt.Tooltip("series:N", title="Series"),
            alt.Tooltip("change:Q", title="Change", format="+.2%"),
        ],
        **position,
    )
    return (
        (flat + line + hover)
        .properties(height=250)
        .configure_view(strokeWidth=0, fill=SURFACE)
        .configure(background=SURFACE)
        .configure_legend(orient="top-left", offset=6)
    )


def position_strip(long_strike, short_strike, breakeven, spot, colour) -> str:
    """Where price sits between the strikes — the cushion, drawn to scale.

    The whole trade is a bet that spot stays right of the short strike, so the
    one number that matters is a distance. A reader gets that from a picture
    faster than from four numbers in a row.
    """
    if not spot:
        return ""
    low = long_strike - 1.5
    high = max(spot, short_strike) + 3.0
    span = high - low or 1.0

    def place(value: float) -> float:
        return max(0.0, min(100.0, (value - low) / span * 100))

    danger = place(short_strike)
    return (
        '<div class="strip">'
        f'<div class="zone" style="left:0; width:{danger:.1f}%; '
        f'background:{CRIT}; opacity:.16"></div>'
        f'<div class="tick" style="left:{place(long_strike):.1f}%; '
        f'background:{MUTED}"></div>'
        f'<div class="tick" style="left:{place(breakeven):.1f}%; '
        f'background:{WARN}; opacity:.75"></div>'
        f'<div class="tick" style="left:{danger:.1f}%; background:{colour}"></div>'
        f'<div class="spot" style="left:{place(spot):.1f}%; background:{BLUE}"></div>'
        "</div>"
        f'<div class="scale"><span>{long_strike:g} long</span>'
        f"<span>{breakeven:,.2f} breakeven</span>"
        f"<span>{short_strike:g} short</span>"
        f'<span style="color:{BLUE}">{spot:,.2f} spot</span></div>'
    )


# --- live book ------------------------------------------------------------

account = None
positions: list = []
creds = None
spot = None
data_error = ""
try:
    creds = load_credentials()
    client = trading_client(creds)
    account = fetch_account(client)
    positions = fetch_positions(client)
    try:
        spot = fetch_spot(stock_data_client(creds), "SPY")
    except Exception:
        spot = None  # quotes go quiet out of hours; the book still renders
except MissingCredentials:
    data_error = "No Alpaca keys configured — showing the decision trail only."
except Exception as exc:
    data_error = f"Alpaca unreachable ({exc}). Showing the decision trail only."

# --- is the agent actually alive? ----------------------------------------
# The loop stamps logs/state.json every cycle. Nothing else reports liveness,
# and a stopped loop looks exactly like a quiet one without it — which matters
# because the mechanical stops only run while the process does.
age_min = None
halted = False
try:
    state = json.loads((ROOT / "logs" / "state.json").read_text())
    halted = bool(state.get("halted"))
    stamp = state.get("last_cycle")
    if stamp:
        age_min = (
            datetime.now(timezone.utc) - datetime.fromisoformat(stamp)
        ).total_seconds() / 60
except Exception:
    pass

if age_min is None:
    live_colour, live_text = MUTED, "No cycle recorded"
elif age_min <= 20:
    live_colour, live_text = GOOD, f"Agent live · last cycle {age_min:.0f}m ago"
elif age_min <= 90:
    live_colour, live_text = WARN, f"Quiet for {age_min:.0f}m — check the loop"
else:
    live_colour, live_text = CRIT, f"STOPPED · no cycle in {age_min / 60:.1f}h"

st.markdown(
    '<div class="hd"><h1 class="t">Autonomous options agent</h1>'
    f'<span class="pill" style="color:{live_colour}">'
    f'<span class="dot" style="background:{live_colour}"></span>'
    f"{escape(live_text)}</span></div>",
    unsafe_allow_html=True,
)
st.markdown(
    '<div class="sub" style="margin-bottom:18px">Put credit spreads on SPY. '
    "The model chooses; the code decides what it is allowed to choose from.</div>",
    unsafe_allow_html=True,
)

if data_error:
    st.warning(data_error)
if halted:
    st.error("Kill switch tripped — entries halted for the day. Exits still run.")

# --- the money line -------------------------------------------------------

START = 100_000.0
if account:
    equity = float(account.equity)
    try:
        prior = float(account.last_equity)
    except (TypeError, ValueError):
        prior = equity
    prior = prior or equity
    day = equity - prior
    total = equity - START
    day_colour = GOOD if day >= 0 else CRIT
    total_colour = GOOD if total >= 0 else CRIT

    st.markdown(
        '<div class="grid g4">'
        + tile(
            "Equity",
            money(equity),
            f'<span style="color:{total_colour}">{money(total, sign=True)}'
            f" ({total / START:+.2%}) since $100k</span>",
        )
        + tile(
            "Today",
            money(day, sign=True),
            f"{day / prior:+.2%} vs prior close",
            day_colour,
        )
        + tile(
            "Open spreads",
            str(len(group_into_spreads(positions))),
            f"{len(positions)} option legs held",
        )
        + tile(
            "Mode",
            "Paper" if (creds and creds.paper) else "LIVE",
            f"SPY ${spot:,.2f}" if spot else "quotes unavailable",
            BLUE,
        )
        + "</div>",
        unsafe_allow_html=True,
    )

# --- the agent against the thing it trades -------------------------------

st.markdown('<div class="sec">Performance against SPY</div>', unsafe_allow_html=True)

try:
    series = performance_series()
except Exception as exc:  # history is a nice-to-have, never a blocker
    series = None
    st.caption(f"Performance history unavailable ({exc}).")

if series is None or series.empty:
    st.markdown(
        '<div class="card"><div class="sub">Not enough history yet — the curve '
        "appears once the account has traded through a session.</div></div>",
        unsafe_allow_html=True,
    )
else:
    final = series.sort_values("when").groupby("series")["change"].last()
    agent_change = float(final.get("Agent", 0.0))
    spy_change = float(final.get("SPY", 0.0))
    edge = agent_change - spy_change
    edge_colour = GOOD if edge >= 0 else CRIT
    st.markdown(
        '<div class="card" style="margin-bottom:12px"><div class="sub" '
        'style="font-size:.95rem; color:#fff; margin:0">SPY '
        f'<b style="color:{ORANGE}">{spy_change:+.2%}</b> over the same window · '
        f'the agent <b style="color:{BLUE}">{agent_change:+.2%}</b> · '
        f'<b style="color:{edge_colour}">{abs(edge) * 100:.2f}pp '
        f'{"ahead of" if edge >= 0 else "behind"}</b> the underlying it trades.'
        "</div></div>",
        unsafe_allow_html=True,
    )
    st.altair_chart(performance_chart(series))
    st.caption(
        "Both series indexed to their value at the agent's first session, so a "
        "single axis carries both. A defined-risk spread is not built to track "
        "SPY — it is built to give up less of it when SPY falls."
    )

# --- open positions, up front --------------------------------------------

st.markdown('<div class="sec">Open positions</div>', unsafe_allow_html=True)

spreads = group_into_spreads(positions)
today = datetime.now(ZoneInfo("America/New_York")).date()
stops = StopConfig()

if not spreads:
    st.markdown(
        '<div class="card"><div class="sub">Flat — no spreads open. '
        "A day with no position is a valid outcome, not a failure.</div></div>",
        unsafe_allow_html=True,
    )
else:
    cards = []
    for key, legs in sorted(spreads.items(), key=lambda kv: (kv[0][1], kv[0][3] or 0)):
        underlying, expiration, _kind, _strike = key
        state = summarize_spread(legs)

        if state is None:
            cards.append(
                f'<div class="card" style="border-left:3px solid {CRIT}">'
                f'<div class="lbl">{escape(str(underlying))} · {expiration}</div>'
                '<div class="val" style="font-size:1.15rem">Unmanaged</div>'
                '<div class="sub">Legs do not form a balanced spread, so the '
                "mechanical stops cannot act on it. Needs manual review.</div></div>"
            )
            continue

        days = (state.expiration - today).days
        ceiling = -stops.loss_multiple_of_credit * state.credit_received
        pnl = state.unrealized_pl
        forced, why = should_stop_out(
            state, spot if spot else state.short_strike * 2, today, stops
        )
        toward = abs(pnl / ceiling) if ceiling and pnl < 0 else 0.0

        if forced:
            colour, status = CRIT, why.split(".")[0].strip()
        elif toward >= 0.5 or days <= 2:
            colour, status = WARN, "Watching — approaching a stop"
        else:
            colour, status = GOOD, "Holding, thesis intact"

        long_strike = state.short_strike - 5
        for leg in legs:
            if float(leg.qty) > 0:
                try:
                    long_strike = float(leg.symbol[-8:]) / 1000
                except ValueError:
                    pass

        # Breakeven sits below the short strike by the credit taken in: the
        # spread is still whole until price eats through the premium.
        per_share = (
            state.credit_received / (100 * state.contracts) if state.contracts else 0.0
        )
        breakeven = state.short_strike - per_share

        cushion = f"{(spot - state.short_strike) / spot:+.2%} cushion" if spot else "—"
        pnl_colour = GOOD if pnl >= 0 else CRIT

        cards.append(
            f'<div class="card" style="border-left:3px solid {colour}">'
            f'<div class="lbl">{escape(str(underlying))} · exp {expiration} · '
            f"{days}d left</div>"
            f'<div class="val" style="font-size:1.2rem">sell {state.short_strike:g}P'
            f" / buy {long_strike:g}P × {state.contracts}</div>"
            f'<div class="sub" style="font-variant-numeric:tabular-nums">'
            f"credit {money(state.credit_received)} · "
            f'<span style="color:{pnl_colour}">{money(pnl, sign=True)} open</span> · '
            f"{escape(cushion)}</div>"
            + meter(abs(pnl) if pnl < 0 else 0.0, abs(ceiling) or 1.0, colour)
            + position_strip(long_strike, state.short_strike, breakeven, spot, colour)
            + f'<div class="sub" style="color:{colour}">{escape(status)}</div>'
            f'<div class="sub" style="font-size:.75rem">stop at {money(ceiling)}'
            f" · flattens {stops.close_within_days}d before expiry · breach at "
            f"{state.short_strike:g}</div></div>"
        )
    st.markdown(
        '<div class="grid g2">' + "".join(cards) + "</div>", unsafe_allow_html=True
    )

# --- risk gates, in force, not hidden ------------------------------------

st.markdown('<div class="sec">Risk gates in force</div>', unsafe_allow_html=True)

cfg = RiskConfig()
short_legs = [p for p in positions if len(p.symbol) > 15 and float(p.qty) < 0]
spy_spreads = sum(1 for p in short_legs if p.symbol.startswith("SPY"))
equity_now = float(account.equity) if account else START
day_pct = 0.0
if account:
    try:
        base = float(account.last_equity)
        if base > 0:
            day_pct = (float(account.equity) - base) / base
    except (TypeError, ValueError):
        day_pct = 0.0


def gate(label: str, value: str, used: float, limit: float, note: str) -> str:
    ratio = 0.0 if limit <= 0 else used / limit
    colour = CRIT if ratio >= 1 else WARN if ratio >= 0.6 else GOOD
    return (
        f'<div class="card"><div class="lbl">{escape(label)}</div>'
        f'<div class="val" style="font-size:1.15rem">{escape(value)}</div>'
        + meter(used, limit, colour)
        + f'<div class="sub" style="font-size:.75rem">{escape(note)}</div></div>'
    )


st.markdown(
    '<div class="grid g3">'
    + gate(
        "Spreads per underlying",
        f"{spy_spreads} / {cfg.max_spreads_per_underlying}",
        spy_spreads,
        cfg.max_spreads_per_underlying,
        "Five spreads on SPY are one bet at five times the size.",
    )
    + gate(
        "Open spreads total",
        f"{len(short_legs)} / {cfg.max_open_positions}",
        len(short_legs),
        cfg.max_open_positions,
        "Concentration cap across all names.",
    )
    + gate(
        "Risk per position",
        f"{cfg.max_position_pct:.0%} of equity",
        0,
        1,
        f"Max loss capped at {money(equity_now * cfg.max_position_pct)} per trade.",
    )
    + gate(
        "Daily loss kill switch",
        f"{day_pct:+.2%} of −{cfg.max_daily_loss_pct:.0%}",
        abs(min(day_pct, 0.0)),
        cfg.max_daily_loss_pct,
        "Entries halt for the day. Exits are never blocked.",
    )
    + gate(
        "Contracts per order",
        f"max {cfg.max_contracts_per_order}",
        0,
        1,
        "A fat finger cannot size up the book.",
    )
    + gate(
        "Naked short options",
        "Blocked" if not cfg.allow_naked_short_options else "ALLOWED",
        0,
        1,
        "Every short leg needs a protective long. No exceptions.",
    )
    + "</div>",
    unsafe_allow_html=True,
)
st.caption(
    "Read live from `RiskConfig` in `src/risk/gates.py` — these are functions "
    "that return False, not instructions the model can be argued out of."
)

# --- scorecard ------------------------------------------------------------

journal = Journal()
entries = journal.read()

if not entries:
    st.info("No decisions logged yet. Run `python src/main.py --once --brain rules`.")
    st.stop()

ENTERED = {"submitted", "dry_run"}
decisions = [e for e in entries if e.get("event") in ENTERED | {"rejected", "skipped"}]
taken = [e for e in decisions if e.get("event") in ENTERED]
skipped = [e for e in decisions if e.get("event") == "skipped"]
rejected = [e for e in decisions if e.get("event") == "rejected"]
calls = [e for e in entries if e.get("event") == "model_call"]
spend = sum(float(e.get("usd") or 0) for e in calls)

st.markdown('<div class="sec">Decision record</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="grid g4">'
    + tile("Decisions", str(len(decisions)), "logged with reasoning")
    + tile("Trades taken", str(len(taken)), "entries transmitted", GOOD)
    + tile(
        "Declined by the agent",
        str(len(skipped)),
        f"{len(skipped) / len(decisions):.0%} of decisions" if decisions else "",
        MUTED,
    )
    + tile("Blocked by risk gates", str(len(rejected)), "vetoed in code", WARN)
    + "</div>",
    unsafe_allow_html=True,
)
if calls:
    st.caption(
        f"Model spend **${spend:.2f}** across {len(calls)} calls "
        f"(${spend / len(calls):.3f} each). Standing aside is a decision, and a "
        "cheap one — a cycle with no candidates calls the model not at all."
    )

# --- decision trail -------------------------------------------------------

st.markdown('<div class="sec">Decision trail</div>', unsafe_allow_html=True)

STYLES = {
    "submitted": (GOOD, "Entered"),
    "dry_run": (BLUE, "Entered (dry run)"),
    "skipped": (MUTED, "Declined"),
    "rejected": (WARN, "Blocked by risk gate"),
    "exit_review": (BLUE, "Exit review"),
    "at_capacity": (WARN, "At capacity"),
    "no_candidates": (MUTED, "No candidates"),
    "duplicate_suppressed": (WARN, "Duplicate suppressed"),
    "unmanaged_position": (CRIT, "Unmanaged position"),
    "submit_failed": (CRIT, "Submit failed"),
    "cycle_error": (SERIOUS, "Cycle error"),
}
GROUPS = {
    "Trades": ENTERED,
    "Declined": {"skipped", "no_candidates", "at_capacity"},
    "Gate vetoes": {"rejected", "duplicate_suppressed"},
    "Exits": {"exit_review"},
    "Errors": {"cycle_error", "submit_failed", "unmanaged_position"},
}

left, right = st.columns([3, 1])
with left:
    chosen = st.segmented_control(
        "Filter",
        ["All"] + list(GROUPS),
        default="All",
        label_visibility="collapsed",
    )
with right:
    show_all = st.toggle("View all", value=False)

wanted = None if chosen in (None, "All") else GROUPS[chosen]
visible = [
    e
    for e in entries
    if e.get("event") != "model_call" and (wanted is None or e.get("event") in wanted)
]
visible.reverse()
total_matching = len(visible)
if not show_all:
    visible = visible[:8]

for entry in visible:
    event = entry.get("event", "?")
    colour, title = STYLES.get(event, (MUTED, str(event)))
    when = str(entry.get("timestamp", ""))[:19].replace("T", " ")
    underlying = entry.get("underlying", "")

    parts = [
        f'<div class="row" style="border-left-color:{colour}">'
        f'<div class="rh"><span class="rt" style="color:{colour}">{escape(title)}'
        f'</span><span class="tm">{escape(when)}</span></div>'
    ]

    legs = entry.get("legs") or []
    short = next((leg for leg in legs if leg.get("side") == "sell"), None)
    long = next((leg for leg in legs if leg.get("side") == "buy"), None)
    if short and long:
        parts.append(
            f'<div class="leg">{escape(str(underlying))} · '
            f"sell {short['strike']:g}P / buy {long['strike']:g}P · "
            f"{entry.get('contracts', '?')} contracts · exp "
            f"{escape(str(short['expiration']))} · max loss "
            f"{money(entry.get('max_loss'))}</div>"
        )
    elif underlying:
        parts.append(f'<div class="leg">{escape(str(underlying))}</div>')

    if event in ENTERED and entry.get("thesis"):
        parts.append(
            f'<div class="qt"><span class="tag">Thesis</span><br>'
            f"{escape(str(entry['thesis']))}</div>"
        )
        if entry.get("invalidation"):
            parts.append(
                f'<div class="qt"><span class="tag">Wrong if</span><br>'
                f"{escape(str(entry['invalidation']))}</div>"
            )
    else:
        text = entry.get("reasoning") or entry.get("reason") or entry.get("error") or ""
        if text:
            klass = "qt" if show_all else "qt clamp"
            parts.append(f'<div class="{klass}">{escape(str(text))}</div>')

    parts.append("</div>")
    st.markdown("".join(parts), unsafe_allow_html=True)

if not show_all and total_matching > len(visible):
    st.caption(
        f"Showing the {len(visible)} most recent of {total_matching}. "
        "Flip **View all** for the full trail and untruncated reasoning."
    )
