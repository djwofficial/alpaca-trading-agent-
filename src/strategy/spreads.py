"""Build defined-risk put credit spreads from a live option chain.

This layer is deliberately dumb. It enumerates what is *possible* and prices
it honestly; it holds no opinion about what is *wise*. The agent decides
which candidate to take, and the risk gates decide whether it may.

Pricing is conservative throughout: we sell at the bid and buy at the ask,
so a candidate never looks better on paper than it would fill in practice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from risk.gates import OptionLeg, TradeProposal

CONTRACT_MULTIPLIER = 100
_OCC_SUFFIX_LENGTH = 15


@dataclass(frozen=True)
class Contract:
    """One parsed option contract with its current market."""

    symbol: str
    underlying: str
    expiration: date
    option_type: str
    strike: float
    bid: float
    ask: float


@dataclass(frozen=True)
class SpreadCandidate:
    """A priced, defined-risk vertical spread."""

    underlying: str
    expiration: date
    short: Contract
    long: Contract

    @property
    def width(self) -> float:
        return abs(self.short.strike - self.long.strike)

    @property
    def credit(self) -> float:
        """Net premium per share, selling the bid and buying the ask."""
        return self.short.bid - self.long.ask

    @property
    def credit_dollars(self) -> float:
        return self.credit * CONTRACT_MULTIPLIER

    @property
    def max_loss_per_contract(self) -> float:
        return (self.width - self.credit) * CONTRACT_MULTIPLIER

    @property
    def breakeven(self) -> float:
        return self.short.strike - self.credit

    def cushion_pct(self, spot: float) -> float:
        """How far the underlying can fall before the short strike is touched."""
        return (spot - self.short.strike) / spot

    def required_win_rate(self) -> float:
        """Win rate needed just to break even. The market prices this near
        the true probability, which is why strike selection alone is not edge."""
        return self.max_loss_per_contract / (self.width * CONTRACT_MULTIPLIER)

    def max_contracts(self, equity: float, max_position_pct: float) -> int:
        """Largest size that still fits under the per-position risk cap."""
        if self.max_loss_per_contract <= 0:
            return 0
        return int((equity * max_position_pct) // self.max_loss_per_contract)

    def to_proposal(
        self, contracts: int, thesis: str = "", invalidation: str = ""
    ) -> TradeProposal:
        expiration = self.expiration.isoformat()
        return TradeProposal(
            underlying=self.underlying,
            legs=(
                OptionLeg(
                    self.short.symbol, "sell", "put", self.short.strike, expiration
                ),
                OptionLeg(
                    self.long.symbol, "buy", "put", self.long.strike, expiration
                ),
            ),
            contracts=contracts,
            max_loss=self.max_loss_per_contract * contracts,
            thesis=thesis,
            invalidation=invalidation,
        )


def parse_occ(symbol: str) -> tuple[str, date, str, float]:
    """SPY260904P00765000 -> ("SPY", date(2026, 9, 4), "put", 765.0)."""
    if len(symbol) <= _OCC_SUFFIX_LENGTH:
        raise ValueError(f"Not an OCC option symbol: {symbol!r}")

    underlying = symbol[:-_OCC_SUFFIX_LENGTH].strip()
    body = symbol[-_OCC_SUFFIX_LENGTH:]
    expiration = datetime.strptime(body[:6], "%y%m%d").date()
    option_type = "call" if body[6].upper() == "C" else "put"
    strike = int(body[7:]) / 1000
    return underlying, expiration, option_type, strike


def contracts_from_chain(chain: dict) -> list[Contract]:
    """Turn Alpaca option snapshots into priced Contract records.

    Contracts without a two-sided market are dropped: we cannot honestly
    price a spread against a quote that does not exist.
    """
    contracts = []
    for symbol, snapshot in chain.items():
        quote = getattr(snapshot, "latest_quote", None)
        if quote is None or not quote.bid_price or not quote.ask_price:
            continue
        try:
            underlying, expiration, option_type, strike = parse_occ(symbol)
        except ValueError:
            continue
        contracts.append(
            Contract(
                symbol=symbol,
                underlying=underlying,
                expiration=expiration,
                option_type=option_type,
                strike=strike,
                bid=float(quote.bid_price),
                ask=float(quote.ask_price),
            )
        )
    return contracts


def find_put_credit_spreads(
    chain: dict,
    spot: float,
    *,
    width: float = 5.0,
    min_credit: float = 0.05,
    min_cushion_pct: float = 0.005,
    max_cushion_pct: float = 0.05,
) -> list[SpreadCandidate]:
    """Enumerate every viable put credit spread in the chain.

    Only strikes below spot are considered — a put credit spread is a bet
    that the underlying stays above the short strike, so selling above spot
    would be a directional bet the strategy does not make.
    """
    puts = [c for c in contracts_from_chain(chain) if c.option_type == "put"]
    by_expiry: dict[date, dict[float, Contract]] = {}
    for contract in puts:
        by_expiry.setdefault(contract.expiration, {})[contract.strike] = contract

    candidates = []
    for expiration, strikes in by_expiry.items():
        for strike, short in strikes.items():
            long = strikes.get(strike - width)
            if long is None:
                continue

            candidate = SpreadCandidate(
                underlying=short.underlying,
                expiration=expiration,
                short=short,
                long=long,
            )

            if candidate.credit < min_credit:
                continue
            if candidate.credit >= width:
                continue  # mispriced or stale quote, not free money

            cushion = candidate.cushion_pct(spot)
            if not min_cushion_pct <= cushion <= max_cushion_pct:
                continue

            candidates.append(candidate)

    candidates.sort(key=lambda c: (c.expiration, -c.short.strike))
    return candidates
