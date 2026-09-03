# Theta Warden — Slide Deck Spec

For rebuilding the deck in Claude web / Slides / PowerPoint. lablab requires a
**PDF**. 11 slides. Also delivered as an HTML artifact (present in browser or
export to PDF).

**Design system**
- Dark deck. Background `#0d0d0d`, surface `#1a1a19`, hairline borders `rgba(255,255,255,.10)`.
- Text: white `#ffffff` headings, `#c3c2b7` body, `#898781` captions.
- Accents: blue `#3987e5` (the agent / primary), amber `#d95926` (SPY / the benchmark, never a "good/bad" signal).
- Status hues only where they mean state: green `#0ca30c`, amber `#fab219`, red `#d03b3b`.
- Font: a clean grotesque (Inter, Söhne, or system sans). Generous margins. One idea per slide.
- Diagrams as **vector**, not AI images. AI images only for the cover and optional section dividers (see prompts at the bottom).

**Numbers** are from Sep 3, ~15:00 UTC. Refresh from the dashboard before you export.

---

## Slide 1 — Title

- **Theta Warden**
- Subhead: *An autonomous options agent its own code can veto*
- Footer line: Alpaca AI Trading Agents Hackathon · [team name] · [repo URL] · [live app URL]
- Visual: the cover image (prompt below), or a plain dark slide with the wordmark.

**Speaker note:** "Theta Warden trades options autonomously on a live Alpaca
paper account. The name is the thesis — theta is the time decay a premium seller
earns; the warden is the code layer that can veto the model."

---

## Slide 2 — The problem

- Heading: **Autonomy and options don't mix by default**
- Three points:
  - An unsupervised LLM placing multi-leg options trades can lose money fast — and irreversibly.
  - This competition **scores P&L**. A blow-up isn't a bug report, it's the final score.
  - Prompt instructions are not a control. A model can be argued out of an instruction.
- Bottom, emphasised: *How do you get real autonomy **and** a hard safety floor?*

**Speaker note:** "Most 'AI trading agent' demos either aren't autonomous or
aren't safe. We wanted both, and options make the safety problem sharp because
the losses are fast and asymmetric."

---

## Slide 3 — The strategy

- Heading: **Defined-risk put credit spreads on SPY**
- Left: the mechanics
  - Sell a put, buy a lower-strike put, same expiry (~8 days).
  - Collect a net **credit** up front.
  - Win if SPY stays above the short strike — the common case.
  - Max loss = width − credit. **Known before entry.**
- Right: a small diagram — a price axis with `long 745` · `breakeven ~749.70` ·
  `short 750` · `SPY 773` marked, the loss zone shaded left of the short strike,
  an arrow showing "cushion ≈ 3%".
- Caption: *Liquid, positive time decay, resolves inside the competition window.*

**Speaker note:** "Every trade has a worst case we know at entry. That's the
entire reason it's safe enough to hand to a model."

---

## Slide 4 — Where the edge is (and isn't)

- Heading: **Strike-picking is a coin flip. We don't compete there.**
- Body:
  - Every candidate reports the win rate it needs just to break even.
  - The market prices that **very close to the true probability** — so picking the
    "best-looking" spread is a coin flip with extra steps.
  - Real edge comes from exactly two places:
    1. **Selectivity** — trade only when the premium overpays for the risk. A skip costs nothing.
    2. **Exits** — cut a loser before it reaches max loss. Losses here are ~7–10× the size of wins,
       so one avoided max-loss is worth many collected credits.
- Pull quote: *The agent competes on discipline, not on cleverness about strikes.*

**Speaker note:** "This is the honest core of the pitch. We're not claiming a
magic signal. We're claiming that a disciplined operator who skips most days and
cuts losers early makes money selling defined-risk premium — and that's a job an
LLM inside a hard cage can actually do."

---

## Slide 5 — Architecture

- Heading: **Code is the supervisor. The model is the judgment.**
- Horizontal pipeline diagram (boxes, left to right, arrow between each):

  `Market data` → `Candidate finder` → `Risk gates` → `LLM brain` → `Executor` → `Journal`

  Sub-labels under each:
  - Market data — alpaca-py: account, chain, quotes, clock
  - Candidate finder — enumerate every legal spread, price it conservatively
  - Risk gates — veto anything unsafe (functions that return False)
  - LLM brain — pick one pre-approved candidate, or skip
  - Executor — re-checks the gate, dry-run by default
  - Journal — every decision + reasoning, JSONL
- Caption: *The brain only ever chooses from a menu the gates already approved.
  It cannot invent a strike, size past the cap, or go naked.*

**Speaker note:** "The model never places a trade. It picks an index off a list
the code built and the gates already cleared. Anything it returns is clamped to
something legal before it goes anywhere near Alpaca."

---

## Slide 6 — The risk gates

- Heading: **Enforced in code, not in the prompt** — `src/risk/gates.py`
- Table:

  | Gate | Limit | Prevents |
  |---|---|---|
  | Position size | ≤ 5% of equity max loss per trade | One trade sinking the book |
  | Daily loss kill-switch | Halt entries after −3% on the day | A bad day compounding |
  | Spreads per underlying | ≤ 2 on one name | Concentration the size cap can't see |
  | Open spreads | ≤ 5 total | Book-wide concentration |
  | Order size | ≤ 10 contracts | A fat finger sizing up |
  | Defined-risk only | Every short leg needs a protective long | Naked short options, ever |

- Two asymmetries, in a highlighted box:
  - **Closing orders bypass every gate and the halt** — reducing risk is always allowed.
  - **A trade with no stated max-loss is rejected** — downside you can't name isn't a trade.

**Speaker note:** "A reviewer can open this file and see the whole safety model
in about thirty seconds. That was deliberate."

---

## Slide 7 — The brain, boxed in

- Heading: **A confused model can take a legal trade or decline. Nothing else.**
- Points:
  - Judgment model: **Claude Opus-class**, structured output, adaptive thinking.
    Entry decisions run at higher effort than exit checks — they're rarer and irreversible.
  - On entry it must write a **thesis** and an **invalidation** — a concrete,
    checkable condition ("SPY closes below 758, or −1.5% intraday").
  - On every later cycle, the exit check asks one question: *has the invalidation
    occurred?* An unrealised loss alone is not a reason to close.
  - `_sanitize_entry` clamps whatever the model returns; an out-of-range pick becomes a skip.
  - **Deterministic fallback** (`RuleBasedBrain`): no API key, no network. A model
    outage degrades to inaction — entries skip, open positions hold — never to a crash or a blind trade.

**Speaker note:** "The invalidation is a pre-commitment. The model writes down
what would change its mind before it has a position, so it can't rationalise its
way into holding a loser later."

---

## Slide 8 — Alpaca infrastructure

- Heading: **Built on Alpaca's Trading + Market Data APIs**
- Two columns:
  - **Trading API** — account & equity, positions, working orders, market clock,
    multi-leg (MLEG) limit orders, portfolio history for the dashboard curve.
  - **Market Data API** — live option chain snapshots: quotes, greeks, implied vol,
    filtered to the ~8-day window; stock latest trade and daily bars.
- Notes:
  - Multi-leg credit limit prices are signed from the account's perspective:
    **negative = credit received**. Verified against a real fill (limit 0.46 → filled −0.40).
  - Interface is a **CLI** (`python src/main.py --live` / `--once` / `--brain rules`).
    Paper trading by default; live execution requires an explicit flag every run.
  - Dry-run is the default in the executor — a forgotten flag can't cost money.

**Speaker note:** "Everything deterministic — data, gates, sizing, logging, the
loop — is Python against alpaca-py. The model is the only non-deterministic part,
and it's the most boxed-in."

---

## Slide 9 — Live results

- Heading: **Trading since Aug 31 on a fresh $100,000 account**
- Stat row (refresh before export):
  - Equity **$100,283.83** · **+0.28%** since inception
  - **3** trades placed · **1** closed on the agent's call · **2** open, both green
  - Stood aside on **~57%** of entry decisions · **17** blocked by the gates
  - Model spend: **$0.18** total
- A dashboard screenshot (money line + open positions).
- Caption: *SPY +~0.9% over the same window — the spread trades that upside for a
  capped, known downside.*

**Speaker note:** "Small, positive, low-drawdown, and every number here is
reconstructable from the journal and the Alpaca account."

---

## Slide 10 — The decision trail

- Heading: **Every trade and every skip, auditable**
- Screenshot of the decision trail with a Thesis / "Wrong if" card visible.
- Points:
  - One JSONL line per decision — entered, skipped, rejected, closed, errored, model cost.
  - The dashboard reads only that file and the live Alpaca book. It never imports the loop.
  - A judge can reconstruct why the agent did — or didn't do — anything.

**Speaker note:** "The decision trail is the deliverable we're proudest of. The
P&L is what it is, but you can see the reasoning behind every point of it."

---

## Slide 11 — What's next / close

- Heading: **Theta Warden**
- Short roadmap:
  - Debating-committee brain in place of the single analyst (the `Brain` interface already allows it).
  - More liquid underlyings beyond SPY.
  - Richer exit logic (partial profit-taking).
- Links block: repo · live dashboard · this deck · one-page write-up
- Footer: [team name] · Alpaca AI Trading Agents Hackathon 2026

**Speaker note:** "The architecture was built to grow — swapping the brain
changes nothing downstream. But the safety floor stays exactly where it is."

---

# Image generation prompts

Only the cover needs an AI image. Diagrams (slides 3, 5) should be drawn as
vector shapes in the deck tool — they're simple and text-heavy.

## Cover image — 16:9, also usable as the hackathon cover image (PNG/JPG)

**Primary prompt (Midjourney / DALL·E / Ideogram / Imagen):**

> Minimal editorial tech illustration, 16:9. A single luminous geometric
> shield-gate form, constructed from thin precise lines, standing upright at
> center-right over a calm dark field of faint financial chart lines and a
> subtle rising curve. Deep near-black background (#0d0d0d). The shield glows
> cool blue (#3987e5); one warm amber (#d95926) line threads through the chart
> field behind it. Lots of negative space, no text, no characters, no robots, no
> human faces, no stock-photo traders. Abstract, restrained, confident.
> Flat-vector aesthetic with a faint grain. Financial-technology, guardian
> concept.

**Style keywords to append if the tool takes them:** `editorial illustration,
geometric line art, dark mode UI aesthetic, generous negative space, no text,
--ar 16:9`

**Negative prompt (if supported):** `text, watermark, letters, human figures,
faces, robot, android, 3D render, photorealistic, busy, cluttered, neon
cyberpunk, gold coins, dollar signs, bull, bear`

**Fallback / simplest version:** a dark slide, the word **THETA WARDEN** in a
clean sans, and a thin blue arc (the "cushion") drawn between two tick marks
labelled *short* and *spot*. No AI image needed.

## Optional section dividers (only if you want them)

> Same style as the cover — thin blue line-art on near-black, heavy negative
> space, no text. A single motif: [a gate half-open] / [two parallel rails with a
> gap between them] / [a curve meeting a horizontal floor line]. Flat vector,
> faint grain, 16:9.

## What NOT to generate

- Diagrams with text baked in (AI image models mangle text — build those as vector).
- Anything implying real returns, a real brokerage, or a real person.
- Literal "AI brain" / glowing-android imagery — it fights the whole "boxed-in, supervised" message.
