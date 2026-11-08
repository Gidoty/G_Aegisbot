"""Database backup with verification and retention.

    python -m g_aegisbot.tools.backup --out /var/backups/aegisbot [--keep 14]

SQLite: an online, consistent copy through SQLite's backup API (safe while the runner is running), then PRAGMA integrity_check on
the copy. PostgreSQL / Supabase: `pg_dump` (custom format) must be installed; the connection string is passed through the
environment, never on a command line and never printed. The backup file is created with mode 0600, a backup of the wrong kind
or one that fails verification is deleted and the command fails, and only the newest --keep backups are kept.
The database URL is read from AEGIS_DATABASE_URL (the same variable the runner uses).
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.engine import make_url


class BackupError(Exception):
    """A refusal or failure with a message that is safe to print (no URL, no password)."""


def _prune(out: Path, prefix: str, suffix: str, keep: int) -> None:
    files = sorted(out.glob(f"{prefix}-*{suffix}"))
    for old in files[:-keep] if keep > 0 else []:
        old.unlink()


def backup_sqlite(db_path: Path, out: Path, stamp: str) -> Path:
    if not db_path.is_file():
        raise BackupError("the SQLite database file does not exist")
    target = out / f"aegisbot-{stamp}.sqlite"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    try:
        src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        check = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
        try:
            ok = check.execute("PRAGMA integrity_check").fetchone()[0]
            tables = check.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        finally:
            check.close()
        if ok != "ok" or tables == 0:
            raise BackupError("the backup failed verification (integrity check or empty schema)")
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


def backup_postgres(url: str, out: Path, stamp: str) -> Path:
    target = out / f"aegisbot-{stamp}.dump"
    parsed = make_url(url)
    env = {**os.environ, "PGPASSWORD": parsed.password or ""}
    cmd = ["pg_dump", "--format=custom", "--no-owner", f"--host={parsed.host}", f"--port={parsed.port or 5432}",
           f"--username={parsed.username}", f"--dbname={parsed.database}", f"--file={target}"]
    try:
        done = subprocess.run(cmd, env=env, capture_output=True, timeout=1800)
    except FileNotFoundError:
        raise BackupError("pg_dump is not installed") from None
    except subprocess.TimeoutExpired:
        target.unlink(missing_ok=True)
        raise BackupError("pg_dump timed out") from None
    if done.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
        target.unlink(missing_ok=True)
        raise BackupError(f"pg_dump failed (exit {done.returncode})")
    os.chmod(target, 0o600)
    listing = subprocess.run(["pg_restore", "--list", str(target)], capture_output=True, timeout=300)
    if listing.returncode != 0:
        target.unlink(missing_ok=True)
        raise BackupError("the dump could not be read back by pg_restore")
    return target


def run(url: str, out: str | Path, keep: int = 14, now: datetime | None = None) -> Path:
    outdir = Path(out)
    outdir.mkdir(parents=True, exist_ok=True)
    os.chmod(outdir, 0o700)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite":
        if not parsed.database or parsed.database == ":memory:":
            raise BackupError("an in-memory SQLite database cannot be backed up")
        path = backup_sqlite(Path(parsed.database), outdir, stamp)
        _prune(outdir, "aegisbot", ".sqlite", keep)
    elif parsed.get_backend_name() in ("postgresql", "postgres"):
        path = backup_postgres(url, outdir, stamp)
        _prune(outdir, "aegisbot", ".dump", keep)
    else:
        raise BackupError(f"unsupported database type {parsed.get_backend_name()!r}")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Back up the G-Aegisbot database")
    ap.add_argument("--out", required=True)
    ap.add_argument("--keep", type=int, default=14)
    a = ap.parse_args(argv)
    url = os.environ.get("AEGIS_DATABASE_URL", "sqlite:///./data/aegisbot.db")
    try:
        print(f"Backup written: {run(url, a.out, a.keep)}")
    except BackupError as exc:
        print(f"BACKUP FAILED: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"BACKUP FAILED: {type(exc).__name__}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
