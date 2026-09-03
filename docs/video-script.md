# Theta Warden — Pitch Video Script

**Format:** screen recording of the local Streamlit dashboard, you scrolling and
narrating. Target **3:30–3:45**. Hard limits: MP4, ≤5 min, ≤300 MB.

**Before you hit record**
- Start the agent so the liveness pill is green:
  `.venv\Scripts\python.exe src\main.py --interval 600` (leave it running).
- `streamlit run dashboard/app.py`, browser zoom **110–125%** so text survives compression.
- Record at **1920×1080**. Do one silent scroll-through first to learn the stops.
- Numbers below are from **Sep 3, ~15:00 UTC** (equity $100,283.83, +0.28%, SPY $772.98).
  **Read whatever is on your screen when you record** — the shape of the story doesn't change.
- Scroll slowly. Stop moving while you talk about a section; only scroll between them.

---

## 0:00 – 0:15 · Open (top of dashboard on screen, not scrolling)

> "This is **Theta Warden** — an autonomous agent that trades options on a live
> Alpaca paper account. It sells put credit spreads on SPY. What makes it
> different is the split you're about to see: the language model has the
> judgment, but the **code** decides what it's even allowed to consider."

*(Point at the header pill.)*

> "Top right — the agent's alive, last cycle a few minutes ago. It's been running
> on a brand-new hundred-thousand-dollar competition account since August 31."

---

## 0:15 – 0:40 · The money line (the four tiles)

*(Scroll so the four tiles fill the screen.)*

> "The account's at a hundred thousand two-eighty — up about a quarter of a
> percent since it went live, and green on the day. Two spreads open right now,
> both on SPY. Mode says Paper — it never touches real money, and that's
> enforced in code, not just a config flag."

---

## 0:40 – 1:05 · Performance against SPY

*(Scroll to the comparison card + chart. Let the chart sit still.)*

> "Here's the account against SPY over the same window. SPY's had a strong week,
> up nearly a percent. The agent's up a quarter percent — and that gap is the
> strategy working exactly as designed, not failing. A credit spread
> deliberately gives up the upside in exchange for a hard floor under the
> downside. We're not trying to out-run the index on a green week. We're trying
> to collect premium with a known worst case on every trade."

---

## 1:05 – 1:45 · Open positions

*(Scroll to the two position cards. Settle on the first one.)*

> "The two open positions. Take the first — sold the 750 put, bought the 745 put,
> three contracts, five days to expiry. We collected ninety dollars of premium up
> front."

*(Point at the horizontal strip.)*

> "This bar is the picture that matters. The blue dot is where SPY is trading.
> The red zone on the left is where we start losing money. Right now there's a
> **three percent cushion** between the price and our short strike. Green means
> the thesis is still intact."

*(Point at the line under it.)*

> "And underneath — the mechanical stop is set at minus a hundred eighty, twice
> the credit we took in, and the agent flattens one day before expiry no matter
> what the model thinks. Both positions are green and in profit."

---

## 1:45 – 2:30 · Risk gates in force

*(Scroll to the six gate cards.)*

> "This is the part the hackathon judges asked about directly, and it's the core
> of the design. These aren't instructions in a prompt — they're a module of
> functions that return **false**. An LLM can be talked out of an instruction.
> It can't be talked out of a function that rejects its trade."

*(Move across the cards as you name them.)*

> "Max five percent of equity at risk on any single trade. A daily-loss
> kill-switch — down three percent on the day and new entries stop cold, but
> exits are never blocked, so a halted agent can always close what it's holding.
> Max two spreads per name, because five spreads on SPY is one bet at five times
> the size. Ten contracts per order, hard cap. And naked short options —
> **blocked**, no exceptions. Every short leg has to have a long leg behind it
> capping the loss."

*(Point at the caption.)*

> "These read live from the risk config file. What's on screen is what's
> running."

---

## 2:30 – 2:55 · Decision record

*(Scroll to the four scorecard tiles.)*

> "The record so far: three trades placed, one of them already closed on the
> agent's own call. And twenty-six times it looked at the menu of candidates and
> **stood aside** — more than half its entry decisions. That's deliberate. A skip
> costs nothing, and the agent is told not to trade just to look busy."

*(Point at the model-spend caption.)*

> "Every decision — taken or not — is written to a JSONL audit trail. Ninety
> events so far. Total model spend to run all of it: eighteen cents."

---

## 2:55 – 3:30 · Decision trail

*(Scroll to the trail. Settle on the top entry with the Thesis / Wrong-if boxes.)*

> "And this is where the judgment actually shows. On every entry the agent has to
> commit two things in advance — a **thesis**, and an **invalidation**: a
> specific, checkable condition that would prove it wrong."

*(Read from the top card, paraphrasing what's on screen.)*

> "This one took the 748/743 spread because realized volatility has been under
> half a percent a day, and the credit overpays for that much risk. And it wrote
> its own exit trigger right here — close if SPY breaks 758, or drops one and a
> half percent intraday. Every cycle after that, the model's only job on this
> position is to check: did that happen yet? It can't move the line after the
> fact to justify holding a loser."

*(Optional — click the "Declined" filter.)*

> "Filter to the declines and you can read why it passed on every one."

---

## 3:30 – 3:45 · Close

*(Scroll back to the top, or stop on the trail.)*

> "So that's Theta Warden: a language model placing real multi-leg options
> trades, on a real account, with a track record you can audit line by line —
> and a code layer standing behind it that can always say no."

---

## If you want a title card instead of opening cold

Hold a static frame for 3 seconds before the dashboard:

> **THETA WARDEN**
> An autonomous options agent its own code can veto
> Alpaca AI Trading Agents Hackathon · [team name]

Then cut to the dashboard and start at "It sells put credit spreads on SPY…"

## Trims if you run long (cut in this order)

1. The optional "Declined" filter click (2:55–3:30).
2. The performance-vs-SPY section down to one sentence: *"SPY's up on the week,
   the agent's up less — a credit spread trades upside for a hard downside
   floor."*
3. The title card.

## Don't say

- "It beats the market" — it doesn't, and doesn't try to on a green week.
- "It can't lose money" — it can; the loss is *capped and known*, which is the point.
- "Fully hands-off forever" — it's supervised autonomy; the whole pitch is the supervision.
