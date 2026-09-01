"""Alpaca connection — account, positions, and options chain retrieval.

Read-only plumbing. No order placement lives here: that belongs in
src/execution/, and only behind the risk gates.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv

from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import (
    OptionChainRequest,
    StockBarsRequest,
    StockLatestTradeRequest,
)
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.requests import GetOptionContractsRequest, GetOrdersRequest

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


def fetch_open_orders(client: TradingClient) -> list:
    """Orders that are still working.

    A submitted order is not a position yet. Without this, an order sitting
    unfilled is invisible to the next cycle, which re-decides from a book
    that does not include it and submits the same trade again.
    """
    return list(
        client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN))
    )


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


def stock_data_client(creds: Credentials | None = None) -> StockHistoricalDataClient:
    creds = creds or load_credentials()
    return StockHistoricalDataClient(
        api_key=creds.api_key, secret_key=creds.secret_key
    )


def fetch_spot(client: StockHistoricalDataClient, symbol: str) -> float:
    """Last traded price of the underlying."""
    request = StockLatestTradeRequest(symbol_or_symbols=symbol)
    return float(client.get_stock_latest_trade(request)[symbol].price)


def fetch_daily_bars(
    client: StockHistoricalDataClient, symbol: str, *, days: int = 10
) -> list:
    """Recent daily bars, oldest first — the agent's sense of where we are."""
    request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Day,
        start=datetime.now(timezone.utc) - timedelta(days=days * 2),
    )
    bars = client.get_stock_bars(request)
    return list(bars[symbol])[-days:] if symbol in bars.data else []


def fetch_clock(client: TradingClient):
    """Market open/closed and the next transition."""
    return client.get_clock()
