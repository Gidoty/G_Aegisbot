"""Check a Binance API key before it is stored: it must read and trade futures and must NOT be able to withdraw."""
from __future__ import annotations

from dataclasses import dataclass, field

import ccxt.async_support as ccxt


@dataclass
class KeyCheck:
    ok: bool
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _explicit(value):
    """True / False only for a real boolean or the strings 'true' / 'false'; None for anything else (never guessed)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    return None


def interpret_restrictions(r: dict) -> KeyCheck:
    """Turn Binance's /sapi/v1/account/apiRestrictions response into pass or fail. Pure function, fails closed: withdrawals
    must be EXPLICITLY false, reading and futures EXPLICITLY true. A missing, null, malformed or unexpected value for any of
    them fails the check (it is never read as 'disabled')."""
    if not isinstance(r, dict):
        return KeyCheck(ok=False, problems=["Binance's permission answer was not understood, so the key cannot be trusted."])
    problems, warnings = [], []
    withdrawals = _explicit(r.get("enableWithdrawals"))
    if withdrawals is True:
        problems.append("WITHDRAWALS ARE ENABLED on this key. Create a new key with 'Enable Withdrawals' switched "
                        "off. The bot never needs it, and a leaked key could empty the account.")
    elif withdrawals is None:
        problems.append("Binance did not state clearly whether withdrawals are enabled, so the key is refused.")
    reading = _explicit(r.get("enableReading"))
    if reading is None:
        problems.append("Binance did not state clearly whether reading is enabled, so the key is refused.")
    elif not reading:
        problems.append("Reading is not enabled on this key.")
    futures = _explicit(r.get("enableFutures"))
    if futures is None:
        problems.append("Binance did not state clearly whether futures is enabled, so the key is refused.")
    elif not futures:
        problems.append("'Enable Futures' is not switched on for this key.")
    if _explicit(r.get("ipRestrict")) is not True:
        warnings.append("The key is not restricted to trusted IP addresses. Restrict it to the server's IP in Binance.")
    if _explicit(r.get("enableInternalTransfer")) is True or _explicit(r.get("permitsUniversalTransfer")) is True:
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
