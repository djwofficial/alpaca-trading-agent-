"""Alpaca connection — account, positions, and options chain retrieval.

Read-only plumbing. No order placement lives here: that belongs in
src/execution/, and only behind the risk gates.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, timedelta

from dotenv import load_dotenv

from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionChainRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOptionContractsRequest

load_dotenv()


class MissingCredentials(RuntimeError):
    """Raised when .env has not been filled in yet."""


@dataclass(frozen=True)
class Credentials:
    api_key: str
    secret_key: str
    paper: bool


def load_credentials() -> Credentials:
    """Read Alpaca keys from the environment.

    Defaults to paper trading. Only an explicit ALPACA_PAPER_TRADE=false
    turns that off, so a typo or a missing value can never point the agent
    at a live account.
    """
    api_key = os.getenv("ALPACA_API_KEY", "").strip()
    secret_key = os.getenv("ALPACA_SECRET_KEY", "").strip()
    paper = os.getenv("ALPACA_PAPER_TRADE", "true").strip().lower() != "false"

    missing = [
        name
        for name, value in (
            ("ALPACA_API_KEY", api_key),
            ("ALPACA_SECRET_KEY", secret_key),
        )
        if not value
    ]
    if missing:
        raise MissingCredentials(
            f"Missing {', '.join(missing)}. Copy .env.example to .env and add "
            "your dev paper keys — not the competition account."
        )

    return Credentials(api_key=api_key, secret_key=secret_key, paper=paper)


def trading_client(creds: Credentials | None = None) -> TradingClient:
    creds = creds or load_credentials()
    return TradingClient(
        api_key=creds.api_key, secret_key=creds.secret_key, paper=creds.paper
    )


def option_data_client(creds: Credentials | None = None) -> OptionHistoricalDataClient:
    creds = creds or load_credentials()
    return OptionHistoricalDataClient(
        api_key=creds.api_key, secret_key=creds.secret_key
    )


def fetch_account(client: TradingClient):
    """Equity, buying power, and the options approval level."""
    return client.get_account()


def fetch_positions(client: TradingClient) -> list:
    """Open positions. An empty list is a valid, healthy result."""
    return client.get_all_positions()


def fetch_option_contracts(
    client: TradingClient,
    underlying: str,
    *,
    days_out: int = 10,
    limit: int = 40,
) -> list:
    """Tradable contract metadata for one underlying.

    Contract metadata is served outside market hours, which makes this the
    reliable way to prove chain plumbing works on a closed market.
    """
    today = date.today()
    request = GetOptionContractsRequest(
        underlying_symbols=[underlying],
        expiration_date_gte=today,
        expiration_date_lte=today + timedelta(days=days_out),
        limit=limit,
    )
    return client.get_option_contracts(request).option_contracts


def fetch_option_chain(
    client: OptionHistoricalDataClient,
    underlying: str,
    *,
    days_out: int = 10,
) -> dict:
    """Live snapshots — quotes, greeks, implied vol — keyed by contract symbol.

    Returns few or no quotes when the market is closed. That is expected,
    not a failure.
    """
    today = date.today()
    request = OptionChainRequest(
        underlying_symbol=underlying,
        expiration_date_gte=today,
        expiration_date_lte=today + timedelta(days=days_out),
    )
    return client.get_option_chain(request)
