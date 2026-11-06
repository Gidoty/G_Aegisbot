"""Trading cost model shared by the paper ledger and the backtester.

Fees are Binance USDT-M futures standard (regular, no BNB discount) rates. VIP tiers or
BNB fee payment lower them. All of this bot's orders are market or stop-market, so they
pay the taker rate. The maker rate is kept for limit-order features later.

Slippage is a flat assumption per liquidity tier, not a measurement. Check it against
your own fills on testnet or live and adjust.
"""
from __future__ import annotations

MAKER_FEE = 0.0002   # 0.02%
TAKER_FEE = 0.0005   # 0.05%
FUNDING_RATE_8H = 0.0001  # 0.01% per 8h, the Binance default when the market is neutral

_TIER1 = {"BTC/USDT:USDT", "ETH/USDT:USDT"}
_TIER3 = {"FET/USDT:USDT", "1000PEPE/USDT:USDT", "RENDER/USDT:USDT"}
STOP_SLIPPAGE_MULT = 1.5  # stop-market orders fill worse than the trigger in fast moves


def slippage_rate(symbol: str, *, is_stop: bool = False, scale: float = 1.0) -> float:
    """Adverse price move per fill, as a fraction (3 bps for majors, 6 bps mid caps, 12 bps small caps)."""
    bps = 3.0 if symbol in _TIER1 else 12.0 if symbol in _TIER3 else 6.0
    if is_stop:
        bps *= STOP_SLIPPAGE_MULT
    return bps / 10_000 * scale
