# Deploying the dashboard

The submission needs a **public app URL**. Deploy the Streamlit dashboard to
**Streamlit Community Cloud** — free, connects straight to the GitHub repo, and
this repo is already set up for it (`.streamlit/config.toml`,
`.streamlit/secrets.toml.example`, and the app bridges `st.secrets` into env
vars so the same client code works locally and hosted).

## Steps (~10 minutes)

1. **Make the repo public.** GitHub → repo → Settings → General → Danger Zone →
   Change visibility → Public. (Hackathon requires it.)

2. Go to **https://share.streamlit.io** and sign in with GitHub.

3. **New app → Deploy a public app from GitHub:**
   - Repository: `djwofficial/alpaca-trading-agent-`
   - Branch: `main`
   - Main file path: `dashboard/app.py`
   - (Advanced settings) Python version: **3.13** if offered, otherwise 3.12.

4. **Advanced settings → Secrets** — paste the **competition account** keys
   (TOML format, same file as `.streamlit/secrets.toml.example`):
   ```toml
   ALPACA_API_KEY = "PK..."
   ALPACA_SECRET_KEY = "..."
   ALPACA_PAPER_TRADE = "true"
   ```
   These are the *paper* keys for account `PA3JNFSU9BWL` — the ones the agent
   already trades with, so the dashboard shows the real book. **Never** put the
   secret key anywhere in the repo.

5. **Deploy.** First build takes 2–4 minutes. You get a URL like
   `https://theta-warden.streamlit.app` — that's the **Application URL** for the
   submission. You can rename it under Settings → General.

## What's live vs. what's cached on the hosted version

| Section | Hosted behaviour |
|---|---|
| Equity, Today, Open positions, Performance vs SPY, Risk gates | **Live** — fetched from Alpaca on every load using the secrets |
| Decision record + Decision trail | As fresh as the last commit of `logs/decisions.jsonl` (it's the only log file tracked in git) |
| Liveness pill | Falls back to the newest journal line — shows "last decision Nm ago", never a red STOPPED |

**Keep it fresh:** the agent writes logs on your local machine. Before you record
the video and before you submit, commit the current journal so the hosted trail
is up to date:
```bash
git add logs/decisions.jsonl && git commit -m "Refresh decision trail" && git push
```
Streamlit Cloud redeploys automatically on push.

## Submission form fields

| Field | Value |
|---|---|
| Public GitHub repository | `https://github.com/djwofficial/alpaca-trading-agent-` |
| Demo application platform | Streamlit |
| Application URL | your `*.streamlit.app` URL from step 5 |
| Alpaca paper trading account ID | **`PA3JNFSU9BWL`** (account number) — internal UUID is `9c6f9e9f-45dd-4ce4-8525-832b320949d2` if they want that form instead |

## If Streamlit Cloud won't build

- `ModuleNotFoundError` → check `requirements.txt` is at the repo root (it is).
- Timezone errors → add a line `tzdata` to `requirements.txt`, commit, push.
- Still stuck → Replit or Railway also work; point them at `dashboard/app.py`
  with the same three secrets and `pip install -r requirements.txt`.
