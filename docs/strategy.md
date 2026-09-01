# Strategy — Put Credit Spreads on Liquid ETFs

*The trading strategy behind the agent, in enough depth to build the pitch and
the one-page write-up from. Numbers below are illustrative unless labelled as
code defaults.*

---

## In one sentence

The agent sells **defined-risk put credit spreads** on a liquid ETF (default
**SPY**), collects the premium, and wins as long as the underlying stays above a
strike it deliberately places *below* the current price — while a layer of
**code-enforced risk gates** and an **LLM that is only allowed to be selective
and to cut losers early** keep any single trade from doing real damage.

---

## 1. What a put credit spread is

A put credit spread (a "bull put spread") is two option legs on the same
expiration:

| Leg | Action | Strike | Purpose |
|---|---|---|---|
| Short put | **Sell** | Higher (closer to spot) | Collects premium |
| Long put | **Buy** | Lower (default $5 below) | Caps the loss |

You receive a **net credit** up front. At expiration:

- **Underlying stays above the short strike** → both puts expire worthless, you
  keep the full credit. *This is the win, and it's the common case.*
- **Underlying falls below the short strike** → you start losing, but the long
  put caps the damage. The most you can lose is the **width minus the credit**.

Because the loss is capped by the long leg, this is a **defined-risk** trade —
you know the worst case before you enter. That is the entire reason the strategy
is safe enough to run autonomously.

### Worked example (illustrative)

SPY at **$765**. The agent sells the **758 put** and buys the **753 put**, same
expiry ~8 days out, for a net credit of **$0.60/share = $60/contract**.

| Quantity | Value |
|---|---|
| Width | $5.00 |
| Credit collected | $60 / contract |
| Max loss | (5.00 − 0.60) × 100 = **$440 / contract** |
| Breakeven | 758 − 0.60 = **757.40** |
| Cushion (room to fall before the short strike) | (765 − 758) / 765 = **0.92%** |
| Win rate needed just to break even | 440 / 500 = **88%** |

That last row is the crux of the whole strategy — see §3.

---

## 2. Why this strategy for a one-week competition

- **Defined, known-in-advance risk** on every trade — no naked exposure, no
  surprise blow-ups. Essential when an LLM is in the loop and P&L is scored.
- **High base win rate.** Most days SPY does *not* fall ~1% in a week, so most
  spreads expire worthless and the credit is kept.
- **Positive time decay (theta).** With ~8 days to expiry, every day the
  underlying holds, the spread bleeds toward the agent's favour.
- **Liquidity.** SPY options are among the most liquid instruments in the world
  — tight bid/ask, honest fills, reliable quotes for the agent to price against.
- **Short cycle fits the event.** ~8-day expiries mean positions actually
  resolve inside the competition window instead of hanging unrealised.

---

## 3. Where the edge actually is (and isn't)

This is the intellectually honest core of the pitch, and it comes straight from
the agent's own decision prompt.

Every candidate the agent sees reports the **win rate it needs just to break
even** (88% in the example above). The options market prices these spreads so
that this break-even win rate sits **very close to the true probability**. That
has a sharp consequence:

> **Picking the "best-looking" spread is a coin flip with extra steps.** Strike
> selection alone is not an edge.

Real edge comes from exactly two places, and the agent is built around both:

1. **Selectivity.** Trade only when the premium *overpays* for the actual risk,
   and stand aside otherwise. **A skip costs nothing.** A day with no position
   is a perfectly good day — the agent is explicitly told not to trade just to
   look busy.
2. **Exits.** Cut a position when the reasoning that opened it breaks, *before*
   the loss reaches its maximum. In this structure a max loss is **~7–10× the
   size of a win** ($440 risked vs. $60 collected in the example), so **one
   avoided maximum loss is worth many collected credits.**

Everything in the architecture below exists to let the agent be *selective* and
*disciplined about exits* — and to make it structurally incapable of the things
that blow up option sellers.

---

## 4. How the agent works

Mental model (from `CLAUDE.md`): the **code is the supervisor**, the **LLM is
the judgment**, and neither can override the other's job.

```
 market data ──▶ candidate finder ──▶ risk gates ──▶ LLM brain ──▶ executor ──▶ journal
 (alpaca-py)     (enumerate legal    (veto unsafe    (pick one or   (gated,      (every
                  spreads, priced     trades)         skip; write    dry-run      decision
                  conservatively)                     a thesis)      default)     logged)
```

### 4a. Candidate finder — `src/strategy/spreads.py`
Enumerates *every legal* put credit spread in the live chain and prices each one
**conservatively — selling at the bid, buying at the ask** — so a candidate can
never look better on paper than it would actually fill. It only considers
strikes **below spot** (this strategy is a bet the underlying *holds*, not a
directional punt), and filters to a sane band:

| Filter | Code default | Meaning |
|---|---|---|
| Spread width | `5.0` | $5 between the two strikes |
| Min credit | `$5 / contract` | Ignore spreads that pay almost nothing |
| Credit < width | — | Reject stale/mispriced quotes (not "free money") |
| Cushion band | `0.5% – 3%` below spot | How far the underlying can fall before the short strike is touched |
| Days to expiry | `~8` | Short cycle, resolves in-window |

It holds *no opinion* about which candidate is wise — it just reports the honest
menu (credit, max loss, breakeven, cushion, required win rate, max size).

### 4b. Risk gates — `src/risk/gates.py` (enforced in code, not in the prompt)
Every proposed trade passes `RiskGate.check()` before it can execute. **These
are functions that return `False`, not instructions an LLM can be argued out
of.**

| Gate | Limit | What it prevents |
|---|---|---|
| Position size | ≤ **5%** of equity per trade | One trade can't sink the book |
| Daily loss kill-switch | Halt entries after **−3%** on the day | A bad day can't compound |
| Open positions | ≤ **5** distinct underlyings | Concentration |
| Order size | ≤ **10** contracts / order | A fat finger can't size up |
| Defined-risk only | Every short leg **must** have a protective long leg | No naked short options, ever |

Two deliberate asymmetries make the safety real:
- **Closing orders bypass the entry gates and the halt** — a halted agent must
  still be able to *flatten* the position that halted it (reducing risk is
  always allowed).
- **Position sizing requires a stated, positive max-loss** — a trade whose
  downside can't be named is not a trade the agent takes.

### 4c. The LLM brain — `src/agent/brain.py`
The judgment layer has two narrow jobs:

- **`decide_entry`** — choose **one** candidate from the pre-approved menu, or
  **skip**. It writes two things it must commit to up front:
  - a **thesis** (why the underlying should stay above the short strike), and
  - an **invalidation** — a *specific, checkable* condition that would prove the
    thesis wrong (e.g. "SPY closes below 757.40, or falls more than 1.2%
    intraday"), so it can't rationalise later.
- **`review_exit`** — for each open position, ask the one question that matters:
  *has the invalidation condition occurred?* If yes, close. If no, hold — an
  unrealised loss alone is not a reason to close; the pre-committed line is.

The brain is **boxed in by code**. It picks from a menu the gates already
approved; `_sanitize_entry` clamps whatever the model returns to something legal
(a strike it can't invent, a size it can't exceed), and an out-of-range choice
becomes a skip. **The worst a confused model can do is take a legal trade or
decline to trade.**

- **Judgment model:** a frontier Claude model (Opus-class, `claude-opus-5`)
  via the Anthropic structured-output API.
- **Deterministic fallback:** `RuleBasedBrain` runs with no API key and no
  network, so the loop can be exercised end-to-end and, critically, **a model
  outage degrades to something safe rather than to nothing** — entries skip,
  open positions hold. An overnight outage never crashes the loop or trades
  blindly.
- `Brain` is an interface: a debating committee could replace the single analyst
  without touching the loop, gates, executor, or dashboard.

### 4d. Executor & journal — `src/execution/orders.py`, `src/journal.py`
The only code allowed to transmit an order. It **re-runs the gate**, logs the
verdict, and places the trade only if approved. `dry_run=True` is the default,
so a forgotten flag can never cost money — live trading requires `--live`,
explicitly, every run. Every decision (entered, skipped, rejected, closed,
errored) is written to a JSONL **decision journal**, which is both the audit
trail for the P&L score and the data source for the dashboard.

### 4e. The loop — `src/main.py`
Each cycle, in order:
1. Load account state; trip the **kill-switch** if the day has gone badly
   (baseline persisted to disk, so it survives a restart).
2. Fetch clock, spot, recent daily bars, and the options chain.
3. **Review exits first** — freeing risk beats adding it.
4. Find candidates → brain decides entry → gate → execute.
5. Log everything.

---

## 5. Current status (honest)

- **Dry-run only. Nothing has traded yet.** Alpaca connection, gates, executor,
  candidate finder, brain, loop and dashboard are built; **70 tests pass**.
- Default symbol **SPY**; the design generalises to other liquid ETFs.
- **Not yet on the competition account.** Per the hackathon rules the scored
  account must be brand-new and funded to exactly $100k, and P&L history starts
  the moment it goes live — so getting it live early is the priority.

### Open items to close before going live
- **Confirm Alpaca's sign convention for multi-leg credit limit prices** against
  a real fill on the dev account (flagged in `CLAUDE.md`).
- Point the agent at the **fresh, dedicated competition account**, not the
  current dev keys.

---

## 6. How this maps to the judging criteria

| Criterion | How the strategy addresses it |
|---|---|
| **P&L performance** | Defined-risk structure + selectivity + early exits — engineered to avoid the max losses that sink option sellers, not to chase every credit. |
| **Technology implementation** | Risk gates enforced in code (own module), gated executor, JSONL decision journal, and a deterministic rule-based fallback for safe degradation. |
| **Creativity & originality** | The honest-edge thesis: strike-picking is priced-in, so the agent competes on *selectivity and exit discipline* — and pre-commits an invalidation it can't argue around. |
| **Presentation & execution** | A visible decision trail (dashboard reads the journal) makes the agent's reasoning legible in the demo. |

---

## 7. Suggested slide outline (for the deck / video)

1. **The problem** — an unsupervised LLM placing multi-leg options trades can
   lose money fast. How do you get autonomy *and* safety?
2. **The strategy** — put credit spreads on SPY: defined risk, high base win
   rate, positive theta. (One diagram: sell 758P / buy 753P.)
3. **The honest edge** — strike-picking is a coin flip; edge = selectivity +
   exits. Losses are ~10× wins, so one avoided max-loss beats many credits.
4. **The architecture** — code is the supervisor, LLM is the judgment. Show the
   pipeline; emphasise gates are *functions that return False*.
5. **The risk gates** — the table from §4b. This is the slide judges asked about.
6. **Live demo** — the loop running (dry-run) and the decision trail on the
   dashboard.
7. **Results & what's next** — P&L on the competition account; committee brain,
   more underlyings.

> Keep the demo understandable in the first 30 seconds, and show the thing
> *working* before polishing the narrative.
