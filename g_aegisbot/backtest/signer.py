"""Offline signer for approval and freeze records. Run it on YOUR OWN trusted device, never on the VPS, never in a cloud
session, never inside the repository.

    python -m g_aegisbot.backtest.signer keygen --out ~/aegis-keys/approver.pem [--label NAME] [--years 1]
    python -m g_aegisbot.backtest.signer pubentry --key ~/aegis-keys/approver.pem
    python -m g_aegisbot.backtest.signer sign-approval --key KEY --approved-by NAME --version v1.0
    python -m g_aegisbot.backtest.signer sign-freeze --key KEY --in unsigned_freeze.json --out freeze.json
    python -m g_aegisbot.backtest.signer verify --purpose approval|freeze --in FILE

Custody rules enforced here:
  * the private key is stored only as a passphrase-encrypted PKCS#8 PEM; the passphrase is typed at a prompt, never taken
    from an argument or an environment variable;
  * a key path inside the repository is refused (to create or to read), as is an existing file or a symbolic link;
  * the key file is created with mode 0600 and never overwritten;
  * signing an approval shows the protocol hash and needs its first eight characters typed back, so a signature is always
    a deliberate act on a specific document;
  * the public half is printed for you to put into docs/STRATEGY_ACCEPTANCE.trusted_keys.json by a commit that you make.
This module is not part of the backtest import closure and nothing in the backtester imports it.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from g_aegisbot.backtest import freeze as fz
from g_aegisbot.backtest import manifest as mf
from g_aegisbot.backtest import safeio
from g_aegisbot.backtest import signing as sg

MIN_PASSPHRASE = 12


class SignerError(Exception):
    """A refusal with a message that is safe to print."""


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _check_key_location(path: Path, repo_root: Path) -> Path:
    path = Path(path).expanduser()
    if _inside(path.parent if not path.exists() else path, repo_root):
        raise SignerError("the key path is inside the repository; keep the private key on your own device, outside it")
    return path


def generate_key(path: str | Path, passphrase: str, *, repo_root: Path = mf.REPO_ROOT) -> Ed25519PrivateKey:
    """Create a new key and write it, encrypted, with mode 0600. Never overwrites."""
    if len(passphrase) < MIN_PASSPHRASE:
        raise SignerError(f"the passphrase must be at least {MIN_PASSPHRASE} characters")
    p = _check_key_location(Path(path), repo_root)
    if p.exists() or p.is_symlink():
        raise SignerError("that key file already exists; it is never overwritten")
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.BestAvailableEncryption(passphrase.encode("utf-8")))
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(pem)
        fh.flush()
        os.fsync(fh.fileno())
    return key


def load_key(path: str | Path, passphrase: str, *, repo_root: Path = mf.REPO_ROOT) -> Ed25519PrivateKey:
    p = _check_key_location(Path(path), repo_root)
    if p.is_symlink() or not p.is_file():
        raise SignerError("the key path is not a regular file")
    if p.stat().st_mode & 0o077:
        raise SignerError("the key file is readable by other users; run: chmod 600 on it")
    try:
        key = serialization.load_pem_private_key(p.read_bytes(), passphrase.encode("utf-8"))
    except (ValueError, TypeError):
        raise SignerError("the key could not be opened (wrong passphrase, or not an encrypted key)") from None
    if not isinstance(key, Ed25519PrivateKey):
        raise SignerError("the key is not an Ed25519 key")
    return key


def public_entry(key: Ed25519PrivateKey, *, label: str, years: int = 1, now: datetime | None = None) -> dict:
    """The entry for docs/STRATEGY_ACCEPTANCE.trusted_keys.json (public information only)."""
    raw = sg.public_raw(key)
    start = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    return {"key_id": sg.key_id_of(raw), "public_key": base64.b64encode(raw).decode("ascii"), "role": sg.ROLE_APPROVER,
            "valid_from": start.isoformat(), "valid_until": (start + timedelta(days=365 * years)).isoformat(),
            "revoked": False, "label": label}


def approval_confirmation_text(protocol_sha256: str) -> str:
    return protocol_sha256[:8]


def make_signed_approval(key: Ed25519PrivateKey, *, repo_root: Path, approved_by: str, version: str, confirm: str,
                         now: datetime | None = None) -> dict:
    sha = mf.sha256_file(Path(repo_root) / mf.PROTOCOL_DOC)
    if confirm.strip().lower() != approval_confirmation_text(sha):
        raise SignerError("confirmation does not match the protocol hash; nothing was signed")
    payload = fz.build_approval_payload(sha, approved_by, version, now or datetime.now(timezone.utc))
    return sg.sign_record(payload, key, sg.PURPOSE_APPROVAL)


def make_signed_freeze(key: Ed25519PrivateKey, unsigned_path: str | Path) -> dict:
    record = fz.load_freeze_record(unsigned_path)
    if "signature" in record:
        raise SignerError("the freeze record is already signed")
    problems = fz.validate_freeze_record(record)
    if problems:
        raise SignerError("the freeze record is not valid, nothing was signed: " + "; ".join(problems))
    return sg.sign_record(record, key, sg.PURPOSE_FREEZE)


def _prompt_new_passphrase() -> str:
    first = getpass.getpass("New key passphrase (at least 12 characters): ")
    if first != getpass.getpass("Repeat the passphrase: "):
        raise SignerError("the two passphrases differ")
    return first


def main(argv: list[str] | None = None, *, repo_root: Path = mf.REPO_ROOT) -> int:
    ap = argparse.ArgumentParser(description="Offline signer for G-AegisBot approval and freeze records")
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen"); k.add_argument("--out", required=True); k.add_argument("--label", default="approver")
    k.add_argument("--years", type=int, default=1)
    e = sub.add_parser("pubentry"); e.add_argument("--key", required=True); e.add_argument("--label", default="approver")
    e.add_argument("--years", type=int, default=1)
    a = sub.add_parser("sign-approval"); a.add_argument("--key", required=True); a.add_argument("--approved-by", required=True)
    a.add_argument("--version", required=True)
    f = sub.add_parser("sign-freeze"); f.add_argument("--key", required=True); f.add_argument("--in", dest="inp", required=True)
    f.add_argument("--out", required=True)
    v = sub.add_parser("verify"); v.add_argument("--purpose", required=True, choices=sg.PURPOSES); v.add_argument("--in", dest="inp", required=True)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "keygen":
            key = generate_key(args.out, _prompt_new_passphrase(), repo_root=repo_root)
            print(f"Key written to {args.out} (encrypted, mode 0600). Back it up somewhere offline.")
            print("Add this entry to the \"keys\" list of docs/STRATEGY_ACCEPTANCE.trusted_keys.json "
                  '({"version": 1, "keys": [...]}), commit it yourself, and protect that file with branch protection:')
            print(json.dumps(public_entry(key, label=args.label, years=args.years), indent=2))
        elif args.cmd == "pubentry":
            key = load_key(args.key, getpass.getpass("Key passphrase: "), repo_root=repo_root)
            print(json.dumps(public_entry(key, label=args.label, years=args.years), indent=2))
        elif args.cmd == "sign-approval":
            key = load_key(args.key, getpass.getpass("Key passphrase: "), repo_root=repo_root)
            sha = mf.sha256_file(Path(repo_root) / mf.PROTOCOL_DOC)
            print(f"You are approving {mf.PROTOCOL_DOC}\n  SHA-256: {sha}\n  approver: {args.approved_by}  version: {args.version}")
            confirm = input(f"Type the first 8 characters of the hash ({approval_confirmation_text(sha)}) to sign: ")
            record = make_signed_approval(key, repo_root=Path(repo_root), approved_by=args.approved_by, version=args.version,
                                          confirm=confirm)
            out = safeio.write_atomic(Path(repo_root) / fz.APPROVAL_DOC, json.dumps(record, indent=2, sort_keys=True) + "\n")
            print(f"Signed approval written to {out}. Review the diff and commit it.")
        elif args.cmd == "sign-freeze":
            key = load_key(args.key, getpass.getpass("Key passphrase: "), repo_root=repo_root)
            record = make_signed_freeze(key, args.inp)
            out = safeio.write_atomic(args.out, json.dumps(record, indent=2, sort_keys=True) + "\n")
            print(f"Signed freeze record written to {out}")
        elif args.cmd == "verify":
            record = safeio.read_json(args.inp, fz.MAX_RECORD_BYTES)
            problems = sg.verify_record(record, args.purpose, fz.trusted_keys_for_repo(repo_root))
            print("VERIFIED" if not problems else "NOT VERIFIED: " + "; ".join(problems))
            return 0 if not problems else 1
    except (SignerError, sg.SigningError, safeio.UnsafePathError, FileExistsError, OSError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
