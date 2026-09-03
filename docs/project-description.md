# Theta Warden — project description

*Doubles as the required one-page write-up: the **AI logic**, **risk gates**, and
**Alpaca infrastructure** sections below cover all three. ~560 words.*

---

Theta Warden is an autonomous options-trading agent built on Alpaca for the
Alpaca AI Trading Agents Hackathon. It runs one clear, testable strategy: sell
defined-risk **put credit spreads on SPY**, collect the premium, and win whenever
the underlying holds above a short strike deliberately placed below the current
price. Roughly eight-day expiries let positions resolve inside the competition
window, and a long protective leg caps the loss on every trade — there is never
naked exposure.

The design separates **judgment from control**. Strike selection is not an edge:
the market prices each spread so the win rate it needs just to break even sits
within a hair of the true probability. Real edge comes from two things only, and
the agent is built around both — **selectivity** (trade only when the premium
overpays for the risk; a skip costs nothing) and **exits** (cut a loser before it
reaches its maximum, since losses here run seven to ten times the size of a win).

**AI logic.** A candidate finder enumerates every legal spread in the live
options chain and prices each one conservatively — selling at the bid, buying at
the ask — then reports an honest menu: credit, capped loss, breakeven, cushion,
and the break-even win rate. The risk gates reject anything unsafe before the
model is consulted. Only then does a Claude Opus-class brain, using structured
output, choose a single pre-approved candidate or skip. On entry it must commit a
written **thesis** and a specific, checkable **invalidation condition** ("SPY
closes below 758, or −1.5% intraday"); on every later cycle its only job is to
check whether that condition has occurred — an unrealised loss alone is not a
reason to close. Whatever the model returns is clamped to something legal, so an
invented strike or an oversized position becomes a skip. A deterministic
rule-based brain runs with no API key or network, so a model outage degrades to
inaction rather than a crash or a blind trade.

**Risk gates.** Enforced in code (`src/risk/gates.py`) as functions that return
`False`, not as instructions in a prompt. Every proposed trade passes through
them before execution: a 5%-of-equity cap on the maximum loss of any single
trade; a daily-loss kill-switch that halts new entries after a 3% drawdown on the
day and persists across restarts; a limit of two spreads per underlying and five
open spreads in total; a ten-contract cap per order; and a hard rule that every
short leg must carry a protective long leg. Two deliberate asymmetries: closing
orders bypass every gate and the halt, because a halted agent must still be able
to flatten; and a trade whose maximum loss cannot be named is rejected outright.
Separately, mechanical stops run before the model each cycle and fire without
it — closing at twice the credit taken in, within a day of expiry, or if price
reaches the short strike.

**Alpaca infrastructure.** The agent uses Alpaca's **Trading API** for account
and equity, open positions, working orders, the market clock, multi-leg (MLEG)
limit orders, and portfolio history, and the **Market Data API** for live
option-chain snapshots — quotes, greeks, implied volatility — plus the
underlying's latest trade and daily bars. Multi-leg credit limit prices are
signed from the account's perspective (negative is a credit received), verified
against a real fill. It is **paper-only**: paper trading is the default and the
executor dry-runs unless `--live` is passed on every run. Every decision is
written to a JSONL journal that feeds a Streamlit dashboard, which reads only that
file and the live book and never imports the trading loop.

The agent is driven from a command-line interface, backed by 120 automated tests,
and has been trading on a brand-new $100,000 competition account since August 31.
