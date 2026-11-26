"""Trading fee model.

All simulated fills cross the spread (buys at best_ask, sells at best_bid),
so only the taker rate is applied. Quoted in basis points (bps): 1 bps = 0.01%.

The default of 60 bps matches Coinbase Advanced Trade's base-tier taker rate.
The rate is configured via config.json (`trading.fees.taker_bps`), read
independently by the tools-node entrypoint (which charges the fee) and the
price-feed connector (which advertises it to agents).

Perpetual futures carry their own rate (`trading.fees.perp_taker_bps`, default
5 bps = Binance USDⓈ-M VIP 0 taker) because a contract is not a spot fill: the
venue charges commission on the contract notional on both the open and the
close. Callers pick the rate per product with `perp=True`.

Rates are integers: every documented venue preset (Coinbase 60, Binance spot
VIP 0 = 10, Binance USDⓈ-M futures VIP 0 = 5, Kraken Pro = 40) is a whole
number of basis points. Fractional-bps rates (e.g. Binance's 7.5 bps BNB
discount) are intentionally out of scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

# Single canonical default, referenced by FeeModel and config.FeeConfig so the
# "60 bps" default lives in exactly one place.
DEFAULT_TAKER_BPS = 60
# Binance USDⓈ-M perpetual futures, VIP 0 taker = 0.05%.
DEFAULT_PERP_TAKER_BPS = 5
# Upper guard against fat-fingered config (1000 bps = 10%); a higher rate is
# almost certainly a misconfiguration rather than a real venue fee.
MAX_TAKER_BPS = 1000


class Fill(NamedTuple):
    """Outcome of applying fees to one fill.

    `cash` is the cash leaving the account on a buy (cash_out) or entering it on
    a sell (cash_in); `fee` is the taker fee charged on the fill.
    """

    cash: float
    fee: float


@dataclass(frozen=True)
class FeeModel:
    taker_bps: int = DEFAULT_TAKER_BPS
    perp_taker_bps: int = DEFAULT_PERP_TAKER_BPS

    def __post_init__(self) -> None:
        for name in ("taker_bps", "perp_taker_bps"):
            value = getattr(self, name)
            if not 0 <= value <= MAX_TAKER_BPS:
                raise ValueError(
                    f"{name} must be between 0 and {MAX_TAKER_BPS}, got {value}"
                )

    @property
    def taker_rate(self) -> float:
        return self.taker_bps / 10_000.0

    @property
    def perp_taker_rate(self) -> float:
        return self.perp_taker_bps / 10_000.0

    def taker_bps_for(self, *, perp: bool = False) -> int:
        """The rate in effect for one product: perps are priced separately."""
        return self.perp_taker_bps if perp else self.taker_bps

    def rate_for(self, *, perp: bool = False) -> float:
        return self.taker_bps_for(perp=perp) / 10_000.0

    def fee(self, notional: float, *, perp: bool = False) -> float:
        """Commission charged on `notional` (price * qty) for one fill."""
        return notional * self.rate_for(perp=perp)

    def buy_cost(self, notional: float, *, perp: bool = False) -> Fill:
        """For a buy of size `notional` (price * qty), return the cash out and fee.

        The fee is capitalized into cost basis: cash_out = notional + fee.
        """
        fee = notional * self.rate_for(perp=perp)
        return Fill(notional + fee, fee)

    def sell_proceeds(self, notional: float, *, perp: bool = False) -> Fill:
        """For a sell of size `notional` (price * qty), return the cash in and fee.

        The fee is deducted from proceeds: cash_in = notional - fee.
        """
        fee = notional * self.rate_for(perp=perp)
        return Fill(notional - fee, fee)

    def disclosure_prompt(self, *, perp: bool = False) -> str:
        """Agent-facing description of the fee, injected into the ticker prompt."""
        bps = self.taker_bps_for(perp=perp)
        if bps == 0:
            return "Trading is fee-free in this deployment."
        if perp:
            return (
                f"A taker fee of {bps} bps ({bps / 100:.2f}%) is charged on the "
                "notional of every perpetual-futures fill — on the open and again "
                "on the close. Factor this into your sizing and P&L targets — "
                f"a round-trip costs {2 * bps} bps."
            )
        return (
            f"A taker fee of {bps} bps "
            f"({bps / 100:.2f}%) is charged on every fill "
            "(both buys and sells). Factor this into your sizing and P&L targets — "
            f"a round-trip costs {2 * bps} bps."
        )
