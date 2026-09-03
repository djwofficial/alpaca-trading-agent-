# Theta Warden

An autonomous options agent its own code can veto. Built on Alpaca for the
**Alpaca AI Trading Agents Hackathon**.

## What it does

Theta Warden sells **defined-risk put credit spreads on SPY** on a live Alpaca
paper account. It sells a put and buys a lower-strike put of the same expiry
(~8 days out), collects a net credit, and keeps it whenever SPY stays above the
short strike. The long leg caps the loss on every trade — the worst case is
known before entry.

The design splits **judgment** from **control**: deterministic Python builds and
prices the opportunity set and enforces every limit; a Claude Opus-class model
decides only whether an already-approved opportunity is worth taking, or whether
to stand aside.

## Strategy

Strike-picking is not an edge — the market prices each spread so the win rate it
needs to break even sits within a hair of the true probability. Edge comes from
two things only, and the agent is built around both:

1. **Selectivity** — trade only when the premium overpays for the risk. A skip
   costs nothing, and most days are a skip.
2. **Exits** — cut a loser before it reaches max loss. Losses run 7–10× the size
   of a win, so one avoided max-loss is worth many collected credits.

Full detail: [`docs/strategy.md`](docs/strategy.md) ·
one-page write-up: [`docs/writeup.md`](docs/writeup.md)

## Architecture

```
market data ──▶ candidate finder ──▶ risk gates ──▶ LLM brain ──▶ executor ──▶ journal
 (alpaca-py)     (enumerate legal    (veto unsafe    (pick one or   (gated,      (every
                  spreads, priced     trades)         skip; write    dry-run      decision
                  conservatively)                     a thesis)      default)     logged)
```

The brain only ever chooses from a menu the gates already approved. It cannot
invent a strike, size past the cap, or go naked — `_sanitize_entry` clamps
whatever the model returns, and a model outage degrades to inaction (entries
skip, open positions hold) rather than a crash or a blind trade.

## Risk gates — `src/risk/gates.py`

Functions that return `False`, not instructions in a prompt.

| Gate | Limit |
|---|---|
| Position size | max loss ≤ 5% of equity per trade |
| Daily loss kill-switch | halt new entries after −3% on the day |
| Spreads per underlying | ≤ 2 on one name |
| Open spreads | ≤ 5 total |
| Order size | ≤ 10 contracts |
| Defined-risk only | every short leg needs a protective long leg |

Closing orders bypass every gate and the halt — a halted agent must still be able
to flatten. Mechanical stops (`src/risk/stops.py`) run before the model each
cycle and fire without it.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows;  source .venv/bin/activate elsewhere
pip install -r requirements.txt
cp .env.example .env              # add Alpaca paper keys + ANTHROPIC_API_KEY
```

## Running it

```bash
python src/main.py --once --brain rules --ignore-hours   # safe, no API key
python src/main.py --once                                # Claude brain, dry run
python src/main.py --interval 600                        # the loop, dry run
python src/main.py --live                                # transmits orders
streamlit run dashboard/app.py                           # the decision-trail dashboard
pytest tests/ -q                                         # 120 tests
```

## Demo

- Live dashboard: _TBD_
- Pitch video: _TBD_
- Pitch deck: [`docs/deck.html`](docs/deck.html) (open in a browser; File → Print → Save as PDF for the deliverable)

## License

MIT — see [`LICENSE`](LICENSE).
