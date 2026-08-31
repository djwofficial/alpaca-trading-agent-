# CLAUDE.md

Project context for the Alpaca AI Trading Agents Hackathon submission.

---

## The event

**Alpaca AI Trading Agents Hackathon** — hosted by lablab.ai with Alpaca.

- **Dates**: Aug 28 – Sep 4, 2026
- **Submission deadline**: **Sep 4, 11:00 PM TST** (local time — no timezone conversion needed)
- **Format**: fully online, worldwide, free, 18+
- **Prize pool**: $5,000 — $2,500 / $1,500 / $1,000 for 1st / 2nd / 3rd
  (the landing page shows $6,000; the three-way split above is the more specific figure — verify before relying on it)
- **Participants**: ~3,275 approved

### One challenge, not four

The challenge is **01 — Options Alpha Agents**, and it is the only one:

> Build an autonomous AI trading agent designed to generate P&L using Alpaca's
> trading platform. Develop a clear, testable trading strategy and demonstrate
> how your agent identifies opportunities, makes trading decisions, manages
> positions, and performs over the course of the competition. You may explore
> options, trading agents, portfolio income, or other approaches supported by Alpaca.

The numbered sections 01–04 on the dashboard (Start Here / Developer Tools /
AI & Agent Development / Documentation) are the **resource library**, not
challenge tracks. Third-party listings claiming four tracks
(options alpha / volatility / hedging / portfolio overlays) are wrong.

---

## Hard requirements — disqualifying if missed

- **Autonomous agent** built on Alpaca's Trading API
- **MCP server OR CLI** — the project must use one of them
- **Options trading** — all strategies must incorporate options
- **Paper trading only** — no real capital

### Account rules (easy to get wrong)

| | Dev account | Competition account |
|---|---|---|
| Which | Any paper account | **Brand-new**, dedicated to this hackathon |
| Purpose | Prototyping, testing | The scored run |
| Balance | Anything | **Must be set to exactly $100,000** |
| Eligibility | — | Reused/existing accounts are **not eligible for judging** |

**Scheduling consequence**: P&L is a scored criterion and the track record
starts when the competition account goes live. Get the fresh account trading
as early as possible — every idle hour is lost performance history. Do not
leave account creation until submission day.

---

## Judging

Four criteria:

1. **P&L performance** — the agent's actual returns
2. **Technology implementation**
3. **Creativity & originality**
4. **Presentation & execution**

P&L being scored is unusual for a lablab event and drives the "get live early"
constraint above.

---

## Deliverables

- Working prototype at a public URL (Streamlit, Replit, or Vercel)
- Public GitHub repo (MIT license unless stated otherwise)
- Pitch video — MP4, ≤5 minutes, under 300MB
- Slide deck — PDF
- Cover image — PNG or JPG, 16:9
- Title, short and long descriptions (character limits are enforced)
- Technology and category tags
- **One-page write-up** covering: AI logic, **risk gates**, Alpaca
  infrastructure implementation

Submission happens through the **Submit Project** button on the lablab team
dashboard. Manual submission exists for 6 hours post-deadline but requires
prior organizer approval — do not plan around it.

---

## Architecture principles

Mental model: the **MCP server is the agent's hands**, the **LLM is its
judgment**, and **your Python code is the supervisor standing behind it** — the
one who can veto a trade the agent wants to make.

- **Risk gates must be enforced in code, not prompt instructions.** Judges
  asked about them explicitly. A reviewer opening the repo should find them in
  seconds, which is why `src/risk/` is its own module.
- **Log every agent decision with its reasoning.** Both the P&L score and the
  demo depend on a visible decision trail.
- **Build risk gates before order execution.** Never have working order code
  sitting alongside a stubbed gate.
- Options can lose money fast and an unsupervised LLM placing multi-leg trades
  is a real risk to the P&L score.

---

## Stack

- **Alpaca MCP Server** — `github.com/alpacahq/alpaca-mcp-server`
  - Defaults to paper trading (`ALPACA_PAPER_TRADE=true`) — keep it that way
  - Exposes ~65 tools; use `ALPACA_TOOLSETS` to limit scope, since too many
    tools degrades agent reasoning
- **alpaca-py** — Python SDK for deterministic parts: risk gates, scheduling, logging
- **Market Data API** — options chains, pricing, greeks
- **Streamlit** — dashboard and demo surface
- **Reference**: the "Multi-Agent AI Trading System" guide in section 03 of the
  hackathon dashboard is Alpaca's own blueprint — read before designing.
  The "Alpaca Skills" resource in section 02 includes backtesting and
  paper-trading skills that may be usable as pre-built components.

---

## Repo layout

```
alpaca-options-agent/
├── README.md
├── LICENSE                 # MIT
├── .gitignore              # must include .env, .venv/, logs/
├── .env.example            # committed, no real values
├── requirements.txt
├── src/
│   ├── agent/              # LLM reasoning, prompts
│   ├── risk/               # risk gates — separate on purpose
│   ├── execution/          # Alpaca order placement
│   ├── data/               # market + options chain fetching
│   └── main.py             # the loop
├── dashboard/              # Streamlit app
├── docs/
│   └── writeup.md          # the one-pager — write as you build
└── logs/                   # gitignored
```

### Environment

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env           # add Alpaca paper keys
```

`.env` (the secrets file) and `.venv/` (the packages folder) are different
things and both belong in `.gitignore` — one because it's secret, one because
it's large and regenerable.

### Env vars

```
ALPACA_API_KEY=
ALPACA_SECRET_KEY=
ALPACA_PAPER_TRADE=true
ANTHROPIC_API_KEY=
```

Never commit real keys. If one slips into a public repo, rotate it immediately
rather than trying to scrub history.

---

## Current state

Working agent, dry-run only. Nothing has traded yet.

**Done**: Alpaca connection (account, positions, chains, stock bars, clock);
risk gate bodies with real enforcement; gated order executor with
`dry_run=True` default; JSONL decision journal; put-credit-spread candidate
finder; LLM brain (`claude-opus-5`, structured output) with a deterministic
`RuleBasedBrain` fallback; the trading loop; decision-trail dashboard.
70 tests passing.

**Not started**: MCP wiring, live trading on a competition account, Streamlit
Cloud deploy, video, deck, write-up.

### Architecture notes worth keeping

- The brain picks from a menu the gates already approved. It cannot invent
  strikes, size past the cap, or go naked — `_sanitize_entry` clamps whatever
  the model returns, and an out-of-range index becomes a skip.
- Model failures degrade to inaction: entries skip, open positions hold.
  An outage overnight must never crash the loop or trade blindly.
- Closing orders bypass the entry gates (`TradeProposal.closing`). An inverted
  spread trips the naked-short check, and a halted agent must still be able to
  flatten the position that halted it.
- `Brain` is an ABC. A debating committee can replace `SingleAnalystBrain`
  without touching the loop, gates, executor, or dashboard.
- The dashboard reads only the journal file. It never imports the loop.

### Known unknowns

- **Alpaca's sign convention for multi-leg credit limit prices is unverified.**
  Confirm against a real fill on the dev account before going live.
- The `.env` currently holds a teammate's paper keys. The competition account
  must be brand-new and dedicated; that is unresolved.

### Running it

```bash
python src/main.py --once --brain rules --ignore-hours   # safe, no API key
python src/main.py --once                                # Claude brain, dry run
python src/main.py --live                                # transmits orders
streamlit run dashboard/app.py
pytest tests/ -q
```

## Build order

1. Alpaca connection + account fetch — prove the plumbing works first
2. Options chain retrieval and filtering
3. Risk gate bodies — **before** any order code
4. Agent reasoning layer
5. Order execution
6. Loop + logging
7. Streamlit dashboard
8. Fresh competition account, $100k balance, go live
9. Video, deck, write-up, submit

Feature-freeze a full day before the deadline. Budget roughly 60% of remaining
time on the build and 40% on deploy + video + deck — most teams do 95/5 and
lose on presentation.

---

## Repo hygiene

- Small commits with real messages, spread across days. Judges check history;
  one large final push is a red flag.
- Deploy early — a demo that only runs locally is worth nothing.
- Never pivot after the halfway mark.
- The demo must be understandable in the first 30 seconds.
- A 4-minute video that states the problem, shows the thing working, and makes
  the case beats a polished 5-minute one that buries the demo.
