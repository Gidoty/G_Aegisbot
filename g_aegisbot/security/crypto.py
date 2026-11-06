"""Fernet encryption for exchange API credentials.

Supports key rotation: the first key encrypts, every key can decrypt.
Run `python -m g_aegisbot.security.crypto` to print a fresh key.
"""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from g_aegisbot.config import get_settings


class CryptoError(Exception):
    """Raised when encryption is misconfigured or a token cannot be decrypted."""


class SecretBox:
    def __init__(self, keys: tuple[str, ...] | list[str]):
        if not keys:
            raise CryptoError("No encryption key configured. Set AEGIS_MASTER_KEYS in .env.")
        try:
            self._fernet = MultiFernet([Fernet(k.encode()) for k in keys])
        except (ValueError, TypeError) as exc:
            raise CryptoError("AEGIS_MASTER_KEYS contains an invalid Fernet key.") from exc

    @classmethod
    def from_env(cls) -> "SecretBox":
        return cls(get_settings().master_keys)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise CryptoError("Could not decrypt value (wrong key or corrupted data).") from exc

    def rotate(self, token: str) -> str:
        """Re-encrypt a token with the newest key."""
        return self._fernet.rotate(token.encode()).decode()


def generate_key() -> str:
    return Fernet.generate_key().decode()


if __name__ == "__main__":
    print(generate_key())
