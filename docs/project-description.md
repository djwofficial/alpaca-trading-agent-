# Theta Warden — project description

*Fits lablab's 2,000-character limit (~1,600). Covers the required one-page
write-up: AI logic, risk gates, Alpaca infrastructure. Fuller version in
[`docs/writeup.md`](writeup.md).*

---

Theta Warden is an autonomous options agent built on Alpaca. One testable strategy: sell defined-risk put credit spreads on SPY and keep the premium whenever SPY stays above a short strike set below spot. A long protective leg caps the loss on every trade, so the worst case is known before entry. ~8-day expiries resolve in-window.

Judgment is separated from control. Strike-picking isn't an edge — the market prices each spread so its break-even win rate ≈ true probability. Edge is selectivity (trade only when premium overpays for risk) plus cutting losers early, since a max loss is 7–10× a win.

AI logic: a finder enumerates every legal spread in the live chain and prices it conservatively; risk gates reject unsafe ones before the model runs; a Claude Opus-class brain then picks one pre-approved candidate or skips, committing a written thesis and a checkable invalidation ("SPY closes below 758") it re-checks each cycle to manage exits. Model output is clamped to legal values, and a rule-based fallback runs with no API key, so an outage degrades to inaction, not a blind trade.

Risk gates — enforced in code, not prompts: 5%-of-equity max loss per trade; a −3%/day kill-switch that halts entries; ≤2 spreads per name and ≤5 open; ≤10 contracts/order; every short leg needs a long leg. Closing orders bypass the gates so a halted agent can always flatten.

Alpaca: Trading API for account, positions, the market clock, and multi-leg (MLEG) limit orders (a credit is a negative limit price, verified on a real fill); Market Data API for option-chain quotes, greeks and IV. Paper-only, dry-run by default. Every decision is logged to a JSONL journal feeding a Streamlit dashboard. CLI-driven, 120 tests, live on a fresh $100k account since Aug 31.
