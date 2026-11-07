"""One runner per database: refusal, release on shutdown, and no stale lock after the owner dies."""
import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from g_aegisbot.engine.runlock import (
    EXIT_CODE_ALREADY_RUNNING, PG_LOCK_KEY, RunnerAlreadyRunning, RunnerLock, RunnerLockError, RunnerLockLost,
)
from g_aegisbot.engine.runner import TradingRunner
from g_aegisbot.security.crypto import generate_key
from tests.fakes import FakeMarket

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def engine_for(tmp_path, name="lock.db"):
    return create_engine(f"sqlite:///{tmp_path}/{name}")


def holder(tmp_path, name="lock.db"):
    """A separate process that takes the lock and holds it until it is killed."""
    code = textwrap.dedent(f"""
        import sys, time
        from sqlalchemy import create_engine
        from g_aegisbot.engine.runlock import RunnerLock
        lock = RunnerLock(create_engine("sqlite:///{tmp_path}/{name}"))
        lock.acquire()
        print("locked", flush=True)
        time.sleep(600)
    """)
    p = subprocess.Popen([sys.executable, "-B", "-c", code], cwd=ROOT, stdout=subprocess.PIPE, text=True,
                         env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert p.stdout.readline().strip() == "locked"
    return p


# ---- in-process
def test_a_second_lock_on_the_same_database_is_refused_and_names_the_holder(tmp_path):
    first, second = RunnerLock(engine_for(tmp_path)), RunnerLock(engine_for(tmp_path))
    first.acquire()
    with pytest.raises(RunnerAlreadyRunning) as exc:
        second.acquire()
    assert f"pid {os.getpid()}" in str(exc.value) and "lock.db.runner.lock" in str(exc.value)
    assert first.held and not second.held
    first.release()


def test_the_lock_is_released_explicitly_and_can_then_be_taken_again(tmp_path):
    a, b = RunnerLock(engine_for(tmp_path)), RunnerLock(engine_for(tmp_path))
    a.acquire()
    a.release()
    b.acquire()
    assert b.held
    b.release()


def test_acquire_is_idempotent_and_the_context_manager_releases(tmp_path):
    with RunnerLock(engine_for(tmp_path)) as a:
        a.acquire()
        with pytest.raises(RunnerAlreadyRunning):
            RunnerLock(engine_for(tmp_path)).acquire()
    RunnerLock(engine_for(tmp_path)).acquire()


def test_different_databases_do_not_exclude_each_other(tmp_path):
    a, b = RunnerLock(engine_for(tmp_path, "one.db")), RunnerLock(engine_for(tmp_path, "two.db"))
    a.acquire()
    b.acquire()
    a.release(); b.release()


def test_an_in_memory_database_has_nothing_to_share_so_it_does_not_block():
    a, b = RunnerLock(create_engine("sqlite://")), RunnerLock(create_engine("sqlite://"))
    a.acquire(); b.acquire()
    assert "memory" in a.mode


# ---- the runner itself
async def test_a_second_runner_refuses_in_start_before_any_exchange_adapter_exists(tmp_path):
    engine = engine_for(tmp_path)
    first = TradingRunner(None, None, market=FakeMarket(), lock=RunnerLock(engine))
    second = TradingRunner(None, None, lock=RunnerLock(engine))
    await first.start()
    with pytest.raises(RunnerAlreadyRunning):
        await second.start()
    assert second.market is None and second.scanner is None, "the refused runner must not have created anything"
    await first.shutdown()


async def test_shutdown_releases_the_lock_so_the_next_runner_can_start(tmp_path):
    engine = engine_for(tmp_path)
    first = TradingRunner(None, None, market=FakeMarket(), lock=RunnerLock(engine))
    await first.start()
    await first.shutdown()
    third = TradingRunner(None, None, market=FakeMarket(), lock=RunnerLock(engine))
    await third.start()                                                  # no stale lock
    await third.shutdown()


async def test_the_lock_is_still_held_while_the_runner_is_running_not_only_during_start(tmp_path):
    engine = engine_for(tmp_path)
    r = TradingRunner(None, None, market=FakeMarket(), lock=RunnerLock(engine))
    await r.start()
    with pytest.raises(RunnerAlreadyRunning):
        RunnerLock(engine).acquire()                                     # long after start() has returned
    await r.shutdown()


# ---- process termination
def test_killing_the_owner_with_sigkill_leaves_no_stale_lock(tmp_path):
    p = holder(tmp_path)
    try:
        with pytest.raises(RunnerAlreadyRunning) as exc:
            RunnerLock(engine_for(tmp_path)).acquire()
        assert f"pid {p.pid}" in str(exc.value)
        p.send_signal(signal.SIGKILL)
        p.wait(timeout=10)
        again = RunnerLock(engine_for(tmp_path))
        again.acquire()                                                  # the kernel dropped the dead process's lock
        assert again.held
        again.release()
    finally:
        if p.poll() is None:
            p.kill()


def test_terminating_the_owner_normally_also_releases_it(tmp_path):
    p = holder(tmp_path)
    try:
        p.terminate()
        p.wait(timeout=10)
        RunnerLock(engine_for(tmp_path)).acquire()
    finally:
        if p.poll() is None:
            p.kill()


# ---- the real entry point
def run_main(tmp_path, *args):
    env = {**os.environ, "AEGIS_DATABASE_URL": f"sqlite:///{tmp_path}/lock.db", "PYTHONDONTWRITEBYTECODE": "1",
           "AEGIS_MASTER_KEYS": generate_key()}
    return subprocess.run([sys.executable, "-B", "-m", "g_aegisbot.engine.runner", *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=60)


def test_main_exits_with_a_clear_message_and_does_nothing_when_another_runner_holds_the_lock(tmp_path):
    p = holder(tmp_path)
    try:
        done = run_main(tmp_path, "--dry-run", "--once")
        out = done.stdout + done.stderr
        assert done.returncode == EXIT_CODE_ALREADY_RUNNING == 3
        assert "NOT STARTING" in out and "already holds" in out
        assert not (tmp_path / "lock.db").exists(), "a refused runner must not even create or migrate the database"
    finally:
        p.kill()


def test_main_runs_to_completion_and_releases_the_lock_on_exit(tmp_path):
    done = run_main(tmp_path, "--dry-run", "--once")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "No eligible profiles" in done.stdout + done.stderr
    again = RunnerLock(engine_for(tmp_path))
    again.acquire()                                                      # released when the process ended
    again.release()


# ---- PostgreSQL: advisory lock on a dedicated connection (fake connection; a real server is still to be tested)
class FakeResult:
    def __init__(self, value):
        self.value = value

    def scalar(self):
        return self.value


class FakeConn:
    def __init__(self, grant=True, alive=True):
        self.grant, self.alive, self.sql, self.closed, self.isolation = grant, alive, [], False, None

    def execution_options(self, **kw):
        self.isolation = kw.get("isolation_level")
        return self

    def execute(self, statement, params=None):
        text = str(statement)
        self.sql.append((text, params))
        if not self.alive:
            raise ConnectionError("server closed the connection")
        if "pg_try_advisory_lock" in text:
            return FakeResult(self.grant)
        return FakeResult(1)

    def close(self):
        self.closed = True


class FakePgEngine:
    class dialect:
        name = "postgresql"

    def __init__(self, url="postgresql+psycopg://u:p@db.example.com:5432/postgres", conn=None):
        self.url = make_url(url)
        self.conn = conn or FakeConn()

    def connect(self):
        return self.conn


def test_postgres_takes_a_session_advisory_lock_on_a_dedicated_autocommit_connection():
    eng = FakePgEngine()
    lock = RunnerLock(eng)
    lock.acquire()
    assert lock.held and eng.conn.isolation == "AUTOCOMMIT"
    text, params = eng.conn.sql[0]
    assert "pg_try_advisory_lock" in text and params == {"k": PG_LOCK_KEY}
    assert not eng.conn.closed                                           # kept open for the whole run
    lock.release()
    assert any("pg_advisory_unlock" in t for t, _ in eng.conn.sql) and eng.conn.closed and not lock.held


def test_postgres_refuses_when_another_session_holds_the_lock():
    eng = FakePgEngine(conn=FakeConn(grant=False))
    with pytest.raises(RunnerAlreadyRunning):
        RunnerLock(eng).acquire()
    assert eng.conn.closed


@pytest.mark.parametrize("url", ["postgresql+psycopg://u:p@pooler.example.com:6543/postgres",
                                 "postgresql://u:p@aws-0-eu.pooler.supabase.com:6543/postgres"])
def test_postgres_refuses_the_transaction_pooler_before_connecting(url):
    eng = FakePgEngine(url=url)
    with pytest.raises(RunnerLockError, match="transaction pooler"):
        RunnerLock(eng).acquire()
    assert eng.conn.sql == []


def test_postgres_check_detects_a_lost_lock_connection():
    eng = FakePgEngine()
    lock = RunnerLock(eng)
    lock.acquire()
    lock.check()                                                         # alive
    eng.conn.alive = False
    with pytest.raises(RunnerLockLost):
        lock.check()


async def test_the_runner_stops_itself_when_it_notices_the_lock_is_gone(tmp_path):
    eng = FakePgEngine()
    lock = RunnerLock(eng)
    r = TradingRunner(None, None, market=FakeMarket(), lock=lock)
    await r.start()
    eng.conn.alive = False
    await r.risk_pass()
    assert r._stop.is_set()
    await r.shutdown()


def test_an_unsupported_database_is_refused_clearly():
    class Weird:
        class dialect:
            name = "mysql"
        url = make_url("mysql://u:p@h/db")
    with pytest.raises(RunnerLockError, match="does not support"):
        RunnerLock(Weird()).acquire()
