"""Ed25519 signature verification for approval and freeze records. Verification infrastructure ONLY.

This module never creates, stores or pins a key. It verifies that a record was signed by a key listed in a trusted-keys file
that somebody with authority over the repository chose to pin. Until such a file exists, nothing can be verified and every
approval or freeze record is rejected. Who holds the signing key and who may change the pinned list are decisions made outside
this code (see docs/APPROVAL_SIGNING.md).

Canonical form. A signature is over bytes, so the bytes must be unique for one record. `canonical_bytes` defines them: JSON
with object keys sorted, no whitespace, every non-ASCII character escaped, finite numbers only (Python's shortest `repr` for
floats), and only strings as keys. Anything else (a tuple, bytes, NaN, a non-string key) raises SigningError. This is a
deterministic rule of this module, not RFC 8785.

Domain separation. The signed message is DOMAIN + purpose + canonical bytes, so a signature made for an approval record cannot
be replayed as a signature on a freeze record, or the other way round.

Signature block (inside the record, excluded from the signed bytes):
  {"algorithm": "ed25519", "key_id": "<sha256 hex of the raw 32-byte public key>", "value": "<base64 of the 64-byte signature>"}

Trusted-keys file:
  {"version": 1, "keys": [{"key_id": ..., "public_key": "<base64 of 32 raw bytes>", "role": "approver",
                           "valid_from": "<ISO-8601 with zone>", "valid_until": "<ISO-8601 with zone>" | null,
                           "revoked": false, "label": "<free text>"}]}
`key_id` must equal the SHA-256 of `public_key`, so an id cannot be claimed for a different key.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from g_aegisbot.backtest.protected import assert_not_protected

ALGORITHM = "ed25519"
DOMAIN = b"G-AEGISBOT-SIGNED-RECORD-V1"
PURPOSE_APPROVAL = "approval"
PURPOSE_FREEZE = "freeze"
PURPOSES = (PURPOSE_APPROVAL, PURPOSE_FREEZE)
ROLE_APPROVER = "approver"
ROLES = (ROLE_APPROVER,)
MAX_KEYS_FILE_BYTES = 64 * 1024
TRUSTED_KEYS_VERSION = 1


class SigningError(ValueError):
    """Malformed signing data. Always a refusal, never a pass."""


# ------------------------------------------------------------------ canonical bytes
def _check(obj, depth: int = 0) -> None:
    if depth > 32:
        raise SigningError("record is nested too deeply")
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise SigningError("record has a non-string key")
            _check(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _check(v, depth + 1)
    elif isinstance(obj, float):
        if not math.isfinite(obj):
            raise SigningError("record contains a non-finite number")
    elif not (obj is None or isinstance(obj, (str, bool, int))):
        raise SigningError(f"record contains an unsupported value of type {type(obj).__name__}")


def canonical_bytes(obj) -> bytes:
    _check(obj)
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def signing_message(purpose: str, payload) -> bytes:
    if purpose not in PURPOSES:
        raise SigningError(f"unknown signing purpose {purpose!r}")
    return DOMAIN + b"\n" + purpose.encode("ascii") + b"\n" + canonical_bytes(payload)


def public_raw(private_key: Ed25519PrivateKey) -> bytes:
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    return private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def key_id_of(public_key_raw: bytes) -> str:
    return hashlib.sha256(public_key_raw).hexdigest()


def sign_record(record: dict, private_key: Ed25519PrivateKey, purpose: str) -> dict:
    """Return the record with a signature block. For the offline signer (and for tests, with throw-away in-memory keys):
    this library never holds or creates a long-lived key."""
    if "signature" in record:
        raise SigningError("the record already carries a signature")
    sig = private_key.sign(signing_message(purpose, record))
    return {**record, "signature": {"algorithm": ALGORITHM, "key_id": key_id_of(public_raw(private_key)),
                                    "value": base64.b64encode(sig).decode("ascii")}}


# ------------------------------------------------------------------ trusted keys
@dataclass(frozen=True)
class TrustedKey:
    key_id: str
    public_key: bytes
    role: str
    valid_from: datetime
    valid_until: datetime | None
    revoked: bool
    label: str = ""


@dataclass
class TrustedKeys:
    keys: dict[str, TrustedKey] = field(default_factory=dict)
    present: bool = False          # False when no trusted-keys file exists: nothing can be verified


def _when(value, what: str) -> datetime:
    if not isinstance(value, str):
        raise SigningError(f"{what} is not text")
    try:
        t = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SigningError(f"{what} is not an ISO-8601 time") from exc
    if t.tzinfo is None:
        raise SigningError(f"{what} has no time zone")
    return t


def _b64(value, what: str, length: int) -> bytes:
    if not isinstance(value, str):
        raise SigningError(f"{what} is not text")
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SigningError(f"{what} is not valid base64") from exc
    if len(raw) != length:
        raise SigningError(f"{what} must decode to {length} bytes")
    return raw


def parse_trusted_keys(data) -> TrustedKeys:
    """Strict parse of the trusted-keys document (already decoded JSON). Raises SigningError on any irregularity."""
    if not isinstance(data, dict) or set(data) != {"version", "keys"}:
        raise SigningError("the trusted-keys file must be an object with exactly 'version' and 'keys'")
    if data["version"] != TRUSTED_KEYS_VERSION or isinstance(data["version"], bool):
        raise SigningError("unsupported trusted-keys version")
    if not isinstance(data["keys"], list):
        raise SigningError("'keys' must be a list")
    out: dict[str, TrustedKey] = {}
    for i, k in enumerate(data["keys"]):
        if not isinstance(k, dict) or set(k) != {"key_id", "public_key", "role", "valid_from", "valid_until", "revoked", "label"}:
            raise SigningError(f"key #{i} has missing or unexpected fields")
        raw = _b64(k["public_key"], f"key #{i} public_key", 32)
        if k["key_id"] != key_id_of(raw):
            raise SigningError(f"key #{i}: key_id is not the SHA-256 of the public key")
        if k["role"] not in ROLES:
            raise SigningError(f"key #{i}: unknown role")
        if not isinstance(k["revoked"], bool):
            raise SigningError(f"key #{i}: 'revoked' must be true or false")
        if not isinstance(k["label"], str):
            raise SigningError(f"key #{i}: 'label' must be text")
        if k["key_id"] in out:
            raise SigningError(f"key #{i}: duplicate key_id")
        until = None if k["valid_until"] is None else _when(k["valid_until"], f"key #{i} valid_until")
        frm = _when(k["valid_from"], f"key #{i} valid_from")
        if until is not None and until <= frm:
            raise SigningError(f"key #{i}: valid_until is not after valid_from")
        out[k["key_id"]] = TrustedKey(k["key_id"], raw, k["role"], frm, until, k["revoked"], k["label"])
    return TrustedKeys(out, present=True)


def _no_duplicates(pairs):
    seen = {}
    for k, v in pairs:
        if k in seen:
            raise SigningError(f"duplicate key {k!r} in a JSON object")
        seen[k] = v
    return seen


def _reject_constant(name):
    raise SigningError(f"non-finite number {name} in JSON")


def strict_json(data: bytes):
    """Decode UTF-8 and parse JSON strictly: no BOM, no duplicate keys, no NaN or Infinity."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SigningError("the file is not valid UTF-8") from exc
    try:
        return json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, SigningError):
            raise
        raise SigningError("the file is not valid JSON") from exc


def load_trusted_keys(path: str | Path) -> TrustedKeys:
    """A missing file gives an EMPTY set with present=False (nothing verifies). A present but malformed file raises."""
    p = Path(path)
    assert_not_protected(p)
    if not p.exists():
        return TrustedKeys({}, present=False)
    if p.is_symlink() or not p.is_file():
        raise SigningError("the trusted-keys path is not a regular file")
    if p.stat().st_size > MAX_KEYS_FILE_BYTES:
        raise SigningError("the trusted-keys file is too large")
    return parse_trusted_keys(strict_json(p.read_bytes()))


# ------------------------------------------------------------------ verification
def verify_record(record, purpose: str, trusted: TrustedKeys, *, at: datetime | None = None, role: str = ROLE_APPROVER) -> list[str]:
    """Problems that stop `record` from counting as signed by a trusted key for `purpose` (empty list = verified).
    Fails closed on every irregularity: unknown key, unknown algorithm, malformed base64, wrong lengths, a revoked or
    out-of-window key, altered content, or a signature made for a different purpose."""
    try:
        if not isinstance(record, dict):
            return ["the record is not an object"]
        sig = record.get("signature")
        if not isinstance(sig, dict) or set(sig) != {"algorithm", "key_id", "value"}:
            return ["the record has no valid signature block"]
        if sig["algorithm"] != ALGORITHM:
            return ["the signature algorithm is not ed25519"]
        if not trusted.present or not trusted.keys:
            return ["no trusted approver key is pinned, so nothing can be verified"]
        key = trusted.keys.get(sig["key_id"]) if isinstance(sig["key_id"], str) else None
        if key is None:
            return ["the record is signed with a key that is not trusted"]
        if key.role != role:
            return ["the signing key does not have the required role"]
        if key.revoked:
            return ["the signing key has been revoked"]
        now = at or datetime.now(timezone.utc)
        if now < key.valid_from or (key.valid_until is not None and now > key.valid_until):
            return ["the signing key is outside its validity window"]
        raw_sig = _b64(sig["value"], "the signature", 64)
        payload = {k: v for k, v in record.items() if k != "signature"}
        try:
            Ed25519PublicKey.from_public_bytes(key.public_key).verify(raw_sig, signing_message(purpose, payload))
        except InvalidSignature:
            return ["the signature does not match the record"]
        return []
    except SigningError as exc:
        return [f"the signature cannot be checked: {exc}"]
    except Exception as exc:                                             # never let a surprise become a pass
        return [f"the signature check failed safely ({type(exc).__name__})"]
