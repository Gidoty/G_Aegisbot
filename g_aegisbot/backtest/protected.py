"""Protected paths: files whose CONTENT or presence in a listing the backtest tooling must never touch.

Environment files and anything that looks like a credential, key or database are protected. The rule is applied to the
file's own name, to every directory name above it, and to the target of a symbolic link, BEFORE any content is read.
Tooling that reads files (hashing, source analysis, approval and key files) calls `assert_not_protected` first, and tooling
that lists files filters with `is_protected`. Nothing here reads a file.
"""
from __future__ import annotations

import fnmatch
import os
from pathlib import Path

PROTECTED_NAME_PATTERNS = (".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*", "*.db", "*.sqlite",
                           "*.sqlite3", "*.db-wal", "*.db-shm", "*.db-journal")
PROTECTED_DIR_NAMES = frozenset({"secrets", ".ssh", ".aws", ".gnupg"})


class ProtectedPathError(PermissionError):
    """A protected file was about to be read or listed."""


def _name_protected(name: str) -> bool:
    low = name.lower()
    return any(fnmatch.fnmatchcase(low, pat) for pat in PROTECTED_NAME_PATTERNS)


def is_protected(path: str | os.PathLike) -> bool:
    """True if the path, any directory above it, or (for a symbolic link) its target is protected. Uses only the path text
    and `realpath`; no file content is read."""
    p = Path(path)
    for candidate in {p, Path(os.path.realpath(p))}:
        parts = candidate.parts
        if any(part.lower() in PROTECTED_DIR_NAMES for part in parts[:-1]):
            return True
        if parts and (_name_protected(parts[-1]) or parts[-1].lower() in PROTECTED_DIR_NAMES):
            return True
    return False


def assert_not_protected(path: str | os.PathLike) -> None:
    if is_protected(path):
        raise ProtectedPathError(f"refusing to access a protected path: {Path(path).name!r}")
