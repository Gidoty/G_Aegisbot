"""One runner per database, enforced.

Two runners on one database would each journal, send and reconcile entry orders, and could cancel or resolve each other's
in-flight orders. The lock is taken before anything else happens (before schema work, before any exchange adapter exists)
and held for the whole life of the process, so a second runner refuses to start instead of acting.

  SQLite      an exclusive, non-blocking flock on "<database file>.runner.lock". The kernel drops the lock the moment the
              process ends for ANY reason (normal exit, crash, kill -9), so a stale lock cannot survive its owner. The lock file
              is left in place on purpose (it is not the lock; only the held flock is). Every process that shares the database
              file shares the lock file next to it, whether it runs under systemd or in a container with the same directory
              mounted. A container with its OWN copy of the file is a different database and is not excluded.
  PostgreSQL  a session-level advisory lock (pg_try_advisory_lock) on a dedicated connection that is kept open for the whole
              run. The server releases it when that connection ends. Through a transaction pooler (port 6543) a session lock is
              meaningless, so the runner refuses to start there: use the direct or session-pooler address. `check()` is called
              regularly and raises RunnerLockLost if the lock connection has died, which stops the runner.

No distributed-lock service or extra dependency is involved. The PostgreSQL branch is exercised in tests with a fake connection
only; it needs confirming against a real server.
"""
from __future__ import annotations

import logging
import os
import time

from sqlalchemy import text

try:
    import fcntl
except ImportError:                                  # pragma: no cover - non-POSIX platforms are not supported
    fcntl = None

log = logging.getLogger("aegis.runlock")

PG_LOCK_KEY = 0x41454749_52554E31        # "AEGI" "RUN1": any constant shared by every G-Aegisbot runner on one database
EXIT_CODE_ALREADY_RUNNING = 3


class RunnerLockError(RuntimeError):
    """The lock cannot be taken or used on this database."""


class RunnerAlreadyRunning(RunnerLockError):
    """Another runner holds the lock for this database. Nothing has been done."""


class RunnerLockLost(RunnerLockError):
    """The lock connection has gone away, so exclusivity is no longer guaranteed."""


class RunnerLock:
    def __init__(self, engine):
        self.engine = engine
        self.held = False
        self.mode = ""
        self._fd: int | None = None
        self._conn = None
        self.path: str | None = None

    # ------------------------------------------------------------------ acquire / release
    def acquire(self) -> None:
        if self.held:
            return
        dialect = self.engine.dialect.name
        if dialect == "sqlite":
            self._acquire_sqlite()
        elif dialect == "postgresql":
            self._acquire_postgres()
        else:
            raise RunnerLockError(f"the single-runner lock does not support the {dialect} database")
        self.held = True

    def _acquire_sqlite(self) -> None:
        database = self.engine.url.database
        if not database or database == ":memory:":
            self.mode = "memory (nothing is shared between processes)"
            return
        if fcntl is None:
            raise RunnerLockError("the single-runner lock needs a POSIX system")
        self.path = os.path.abspath(database) + ".runner.lock"
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            holder = ""
            try:
                holder = os.pread(fd, 200, 0).decode(errors="replace").strip()
            except OSError:
                pass
            os.close(fd)
            raise RunnerAlreadyRunning(
                f"another G-Aegisbot runner already holds {self.path}" + (f" ({holder})" if holder else "")
                + ". Stop it first, or do not start a second runner on the same database.") from None
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"pid {os.getpid()} started {time.strftime('%Y-%m-%d %H:%M:%S')}\n".encode(), 0)
        self._fd, self.mode = fd, f"flock {self.path}"

    def _acquire_postgres(self) -> None:
        url = str(self.engine.url)
        if self.engine.url.port == 6543 or ":6543" in url:
            raise RunnerLockError("the runner cannot hold its single-instance lock through the transaction pooler (port 6543). "
                                  "Point the runner at the direct connection or the session pooler (port 5432).")
        conn = self.engine.connect()
        try:
            conn = conn.execution_options(isolation_level="AUTOCOMMIT")
            got = conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": PG_LOCK_KEY}).scalar()
        except Exception:
            conn.close()
            raise
        if not got:
            conn.close()
            raise RunnerAlreadyRunning("another G-Aegisbot runner already holds the advisory lock on this database. "
                                       "Stop it first, or do not start a second runner.")
        self._conn, self.mode = conn, "postgresql advisory lock"

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None
        if self._conn is not None:
            try:
                self._conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": PG_LOCK_KEY})
            except Exception:
                log.warning("could not release the advisory lock cleanly; closing its connection releases it")
            finally:
                try:
                    self._conn.close()
                finally:
                    self._conn = None
        self.held = False

    # ------------------------------------------------------------------ liveness
    def check(self) -> None:
        """Raise RunnerLockLost if exclusivity can no longer be guaranteed. Cheap; called about once a minute."""
        if not self.held:
            raise RunnerLockLost("the runner lock is not held")
        if self._conn is not None:
            try:
                alive = self._conn.execute(text("SELECT 1")).scalar()
            except Exception as exc:
                raise RunnerLockLost(f"the lock connection is gone ({type(exc).__name__})") from exc
            if alive != 1:
                raise RunnerLockLost("the lock connection answered unexpectedly")

    def __enter__(self) -> "RunnerLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()
