# Theta Warden — project description

*~1,530 characters, well under lablab's 2,000 limit. Covers the required
one-page write-up: AI logic, risk gates, Alpaca infrastructure. Fuller version
in [`docs/writeup.md`](writeup.md).*

---

Theta Warden is an autonomous options agent built on Alpaca. The strategy: sell defined-risk put credit spreads on SPY, keeping the premium while SPY holds above a short strike below spot. A long protective leg caps every trade's loss — the worst case is known before entry.

Judgment is separated from control. Strike-picking isn't an edge — the market prices each spread's break-even win rate ≈ true probability. Edge is selectivity (trade only when premium overpays for risk) plus cutting losers early, since a max loss is 7–10× a win.

AI logic: code enumerates and conservatively prices every legal spread; risk gates cut the unsafe ones before the model runs; a Claude Opus-class brain then picks one pre-approved candidate or skips, committing a thesis and a checkable invalidation it re-checks each cycle for exits. Its output is clamped to legal values; a keyless rule-based fallback means an outage degrades to inaction, not a blind trade.

Risk gates — in code, not prompts: 5%-of-equity max loss per trade; a −3%/day kill-switch that halts entries; ≤2 spreads per name and ≤5 open; ≤10 contracts/order; every short leg needs a long leg. Closing orders bypass the gates, so a halted agent can always flatten.

Alpaca: Trading API for positions, the clock and multi-leg limit orders (credit = negative limit price, verified on a real fill); Market Data API for chain quotes, greeks and IV. Paper-only, dry-run by default. Every decision lands in a JSONL journal behind a Streamlit dashboard. CLI-driven, 120 tests, live on a fresh $100k account since Aug 31.
