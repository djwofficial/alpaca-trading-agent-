# [Project Name]

Autonomous AI options trading agent built on Alpaca for the
Alpaca AI Trading Agents Hackathon.

## What it does
One or two sentences.

## Strategy
The options strategy in plain English.

## Architecture
Agent reasoning → risk gates → execution.

## Risk Gates
- Max 5% equity per position
- Daily loss limit halts trading
- No naked short options

## Setup
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # add your Alpaca paper keys
python src/main.py
```

## Demo
Live app: TBD
Video: TBD
