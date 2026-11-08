"""Guarded file access for the backtest tooling. Every read and write of a file the tooling chooses goes through here.

Reads: the path is checked against the protected list (names, parent directories and the real target of a link) BEFORE any
byte is read; a symbolic link, a non-regular file and a file with more than one hard link are refused (a hard link under an
innocent name can point at a protected file, and a link count above 1 is the only trace it leaves); the file is opened with
O_NOFOLLOW and judged by `fstat` on the open descriptor, so the size limit and the file-type check apply to the very file
that is read. Size is bounded.

Writes: a protected path, a link or a non-regular target is refused; the data goes to a temporary file in the same
directory, is flushed to disk, and is then moved into place atomically. Unless `overwrite=True` an existing file is never
replaced (the move is a hard-link creation, which fails if the name exists), so a record that must not change cannot be
silently overwritten and a crash cannot leave a half-written file.

The check and the open are separate steps (a race remains if another process can change the directory between them); the
descriptor checks above close the link and size cases, not that one. Nothing here echoes file content in an error.
"""
from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

from g_aegisbot.backtest import signing
from g_aegisbot.backtest.protected import assert_not_protected

MAX_JSON_BYTES = 1 << 20          # 1 MiB: far above any real configuration file, far below anything worth parsing blindly


class UnsafePathError(ValueError):
    """The path is not an acceptable regular file for this tool. Always a refusal; carries no file content."""


def guard_read(path: str | os.PathLike) -> Path:
    """The path as a Path, after the protected-path and file-type checks. Reads nothing."""
    p = Path(path)
    assert_not_protected(p)
    try:
        st = os.lstat(p)
    except OSError as exc:
        raise UnsafePathError(f"cannot inspect {p.name!r} ({type(exc).__name__})") from None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise UnsafePathError(f"{p.name!r} is not a regular file (links and special files are refused)")
    if st.st_nlink > 1:
        raise UnsafePathError(f"{p.name!r} has more than one hard link, so it may be another file under a different name")
    return p


def read_bytes(path: str | os.PathLike, max_bytes: int = MAX_JSON_BYTES) -> bytes:
    p = guard_read(path)
    try:
        fd = os.open(p, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        raise UnsafePathError(f"cannot open {p.name!r} ({type(exc).__name__})") from None
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
            raise UnsafePathError(f"{p.name!r} is not an acceptable regular file")
        if st.st_size > max_bytes:
            raise UnsafePathError(f"{p.name!r} is larger than {max_bytes} bytes")
        data = fh.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise UnsafePathError(f"{p.name!r} is larger than {max_bytes} bytes")
    return data


def read_json(path: str | os.PathLike, max_bytes: int = MAX_JSON_BYTES):
    """Strict JSON: UTF-8, no duplicate keys, no NaN or Infinity. Raises UnsafePathError or signing.SigningError (both
    ValueError) with fixed messages."""
    return signing.strict_json(read_bytes(path, max_bytes))


def guard_write(path: str | os.PathLike, *, overwrite: bool = False) -> Path:
    p = Path(path)
    assert_not_protected(p)
    if os.path.islink(p):
        raise UnsafePathError(f"{p.name!r} is a symbolic link; writing through links is refused")
    if p.exists():
        if not p.is_file():
            raise UnsafePathError(f"{p.name!r} exists and is not a regular file")
        if not overwrite:
            raise FileExistsError(f"{p.name!r} already exists and will not be overwritten")
        if os.stat(p).st_nlink > 1:
            raise UnsafePathError(f"{p.name!r} has more than one hard link; replacing it is refused")
    return p


def write_atomic(path: str | os.PathLike, data: bytes | str, *, overwrite: bool = False) -> Path:
    """Write `data` to `path` atomically. Without `overwrite` an existing file is never touched."""
    p = guard_write(path, overwrite=overwrite)
    raw = data.encode("utf-8") if isinstance(data, str) else data
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)
        if overwrite:
            os.replace(tmp, p)
        else:
            os.link(tmp, p)                      # atomic, and fails if the name appeared since the check
            os.unlink(tmp)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return p
