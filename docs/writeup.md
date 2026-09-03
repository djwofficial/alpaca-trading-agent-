# Theta Warden — Technical Write-up

*Autonomous options trading agent for the Alpaca AI Trading Agents Hackathon.
One page: the AI logic, the risk gates, and the Alpaca infrastructure.*

---

## What it does

Theta Warden sells **defined-risk put credit spreads on SPY** on a live Alpaca
paper account. It sells a put and buys a lower-strike put of the same expiry
(~8 days out), collecting a net credit that it keeps whenever SPY stays above the
short strike. The long leg caps the loss on every trade, so the worst case is
known before entry. The agent is driven by a **command-line interface**; it runs
a fixed loop — review exits, then consider one new entry — every cycle.

## AI logic

The design separates **judgment** from **control**. Deterministic Python builds
and prices the opportunity set and enforces every limit; a **Claude Opus-class
model** does the one thing code cannot — decide whether an opportunity is worth
taking.

1. **Candidate finder** (`src/strategy/spreads.py`) enumerates *every* legal put
   credit spread in the live option chain and prices each one conservatively —
   selling at the bid, buying at the ask — so nothing looks better on paper than
   it would fill. It reports an honest menu: credit, max loss, breakeven,
   cushion, and the **win rate the spread needs just to break even**.
2. **Risk gates** (below) reject any candidate that violates a hard limit.
3. **The brain** (`src/agent/brain.py`) receives only the pre-approved menu. Its
   entry prompt states the honest economics: the market prices each spread so its
   break-even win rate sits close to the true probability, so strike selection
   alone is not an edge. Edge comes from **selectivity** (trade only when premium
   overpays for risk; a skip costs nothing) and **exits** (cut a loser before max
   loss, since losses run ~7–10× the size of wins). The model picks one candidate
   by index or skips. On entry it must commit a **thesis** and an
   **invalidation** — a specific, checkable condition ("SPY closes below 758, or
   −1.5% intraday") that it cannot revise later.
4. **Exit reviews** run once per open position per cycle and ask only: *has the
   invalidation occurred?* An unrealised loss alone is not a reason to close.
5. **`_sanitize_entry`** clamps whatever the model returns to something legal —
   an invented strike or an out-of-range index becomes a skip; an oversized
   position is cut to the cap. A **deterministic `RuleBasedBrain`** runs with no
   API key or network, so a model outage degrades to inaction (entries skip,
   positions hold) rather than a crash or a blind trade.

## Risk gates — enforced in code, not in the prompt

`src/risk/gates.py` runs `RiskGate.check()` before any order. These are
functions that return `False`, not instructions a model can be argued out of.

| Gate | Limit |
|---|---|
| Position size | max loss ≤ **5%** of equity per trade |
| Daily loss kill-switch | halt new entries after **−3%** on the day (persisted across restarts) |
| Spreads per underlying | ≤ **2** on one name |
| Open spreads | ≤ **5** total |
| Order size | ≤ **10** contracts |
| Defined-risk only | every short leg must have a protective long leg — **no naked options** |

Two deliberate asymmetries: **closing orders bypass every gate and the halt**
(a halted agent must still be able to flatten), and a proposal with **no stated
positive max-loss is rejected**. Separately, mechanical stops
(`src/risk/stops.py`) run *before* the model each cycle and fire without it —
close at 2× the credit lost, close within one day of expiry, close if spot
reaches the short strike — so a position is still managed during an API outage.

## Alpaca infrastructure

- **Trading API** (`alpaca-py`): account and equity, open positions, working
  orders, market clock, **multi-leg (MLEG) limit orders**, and portfolio history
  for the dashboard curve. Multi-leg credit prices are signed from the account's
  perspective — **negative is a credit received** — verified against a real fill
  (limit 0.46 → filled −0.40).
- **Market Data API**: live option-chain snapshots (quotes, greeks, implied vol)
  filtered to the ~8-day window; stock latest trade and daily bars.
- **Paper trading only.** `ALPACA_PAPER_TRADE` defaults on and only an explicit
  `false` turns it off; the executor's `dry_run` defaults on and live execution
  requires `--live` on every run.
- **Decision journal** (`src/journal.py`): one JSONL line per decision — entered,
  skipped, rejected, closed, errored, plus model token cost. The **Streamlit
  dashboard** reads only that file and the live Alpaca book; it never imports the
  loop, so it is safe to leave open while the agent trades.

## Status

Live on a brand-new $100,000 competition account since Aug 31, 2026. 120 tests
passing. `Brain` is an interface — a debating committee can replace the single
analyst without touching the loop, gates, executor, or dashboard.
