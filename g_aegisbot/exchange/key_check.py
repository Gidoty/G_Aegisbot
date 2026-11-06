"""Check a Binance API key before it is stored: it must read and trade futures and must NOT be able to withdraw."""
from __future__ import annotations

from dataclasses import dataclass, field

import ccxt.async_support as ccxt


@dataclass
class KeyCheck:
    ok: bool
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def interpret_restrictions(r: dict) -> KeyCheck:
    """Turn Binance's /sapi/v1/account/apiRestrictions response into pass or fail. Pure function."""
    flag = lambda k: str(r.get(k)).lower() == "true"
    problems, warnings = [], []
    if flag("enableWithdrawals"):
        problems.append("WITHDRAWALS ARE ENABLED on this key. Create a new key with 'Enable Withdrawals' switched "
                        "off. The bot never needs it, and a leaked key could empty the account.")
    if not flag("enableReading"):
        problems.append("Reading is not enabled on this key.")
    if not flag("enableFutures"):
        problems.append("'Enable Futures' is not switched on for this key.")
    if not flag("ipRestrict"):
        warnings.append("The key is not restricted to trusted IP addresses. Restrict it to the server's IP in Binance.")
    if flag("enableInternalTransfer") or flag("permitsUniversalTransfer"):
        warnings.append("The key can make internal or universal transfers. The bot does not need that permission.")
    return KeyCheck(ok=not problems, problems=problems, warnings=warnings)


async def check_api_key(api_key: str, api_secret: str, *, exchange=None) -> KeyCheck:
    """Ask Binance what the key can do. Network errors propagate so the caller can say 'try again'."""
    own = exchange is None
    ex = exchange or ccxt.binanceusdm({"apiKey": api_key, "secret": api_secret, "enableRateLimit": True,
                                       "options": {"adjustForTimeDifference": True}})
    try:
        try:
            restrictions = await ex.sapiGetAccountApiRestrictions()
            await ex.fapiPrivateV2GetAccount()          # proves the key really reaches the futures account
        except ccxt.AuthenticationError as exc:
            return KeyCheck(False, [f"Binance rejected the key: {str(exc)[:200]}"])
        return interpret_restrictions(restrictions)
    finally:
        if own:
            await ex.close()
