"""Throw-away signing material for tests. Keys are generated in memory for one test and never written to disk; only the PUBLIC
half is written, into a temporary repository. No production key exists, is created or is pinned by this code."""
import base64
import json
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from g_aegisbot.backtest import freeze as fz
from g_aegisbot.backtest import manifest as mf
from g_aegisbot.backtest import signing as sg

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)


def new_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def key_entry(key, *, role="approver", valid_from=None, valid_until=None, revoked=False, label="test key") -> dict:
    raw = sg.public_raw(key)
    return {"key_id": sg.key_id_of(raw), "public_key": base64.b64encode(raw).decode(), "role": role,
            "valid_from": (valid_from or datetime(2000, 1, 1, tzinfo=timezone.utc)).isoformat(),
            "valid_until": None if valid_until is None else valid_until.isoformat(), "revoked": revoked, "label": label}


def trusted_document(*entries) -> dict:
    return {"version": 1, "keys": list(entries)}


def make_repo(tmp_path: Path, protocol_text="protocol text under review", name="repo") -> Path:
    root = tmp_path / name
    (root / "docs").mkdir(parents=True)
    (root / mf.PROTOCOL_DOC).write_text(protocol_text)
    return root


def pin_keys(root: Path, *keys, **kw) -> Path:
    path = root / fz.TRUSTED_KEYS_DOC
    path.write_text(json.dumps(trusted_document(*[key_entry(k, **kw) for k in keys])))
    return path


def signed_approval_record(root: Path, key, *, approved_by="reviewer", version="v1", at=None, **override) -> dict:
    payload = fz.build_approval_payload(mf.sha256_file(root / mf.PROTOCOL_DOC), approved_by, version, at or NOW)
    payload.update(override)
    return sg.sign_record(payload, key, sg.PURPOSE_APPROVAL)


def write_signed_approval(root: Path, key, **kw) -> Path:
    path = root / fz.APPROVAL_DOC
    path.write_text(json.dumps(signed_approval_record(root, key, **kw)))
    return path


def approved_repo(tmp_path: Path, key=None, name="repo"):
    """A temporary repo with a protocol, a pinned throw-away key and a validly signed approval. Returns (root, key)."""
    key = key or new_key()
    root = make_repo(tmp_path, name=name)
    pin_keys(root, key)
    write_signed_approval(root, key)
    return root, key


def sign_freeze(record: dict, key) -> dict:
    return sg.sign_record(record, key, sg.PURPOSE_FREEZE)
