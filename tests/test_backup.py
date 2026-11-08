import os
import sqlite3
import stat
from datetime import datetime, timedelta, timezone

import pytest

from g_aegisbot.tools import backup


def make_db(path):
    c = sqlite3.connect(path)
    c.execute("create table t (a integer)")
    c.executemany("insert into t values (?)", [(i,) for i in range(50)])
    c.commit()
    c.close()


T0 = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)


def test_a_sqlite_backup_is_consistent_private_and_verified(tmp_path):
    db = tmp_path / "live.db"
    make_db(db)
    out = backup.run(f"sqlite:///{db}", tmp_path / "bk", now=T0)
    assert out.name == "aegisbot-20261108T120000Z.sqlite" and stat.S_IMODE(os.stat(out).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(out.parent).st_mode) == 0o700
    assert sqlite3.connect(out).execute("select count(*) from t").fetchone()[0] == 50


def test_the_backup_is_taken_while_the_database_is_open_for_writing(tmp_path):
    db = tmp_path / "live.db"
    make_db(db)
    writer = sqlite3.connect(db)
    writer.execute("insert into t values (999)")             # an open, uncommitted write on the live database
    out = backup.run(f"sqlite:///{db}", tmp_path / "bk", now=T0)
    assert sqlite3.connect(out).execute("select count(*) from t").fetchone()[0] == 50
    writer.rollback()


def test_only_the_newest_backups_are_kept(tmp_path):
    db = tmp_path / "live.db"
    make_db(db)
    for i in range(5):
        backup.run(f"sqlite:///{db}", tmp_path / "bk", keep=3, now=T0 + timedelta(hours=i))
    assert [p.name for p in sorted((tmp_path / "bk").iterdir())] == [
        f"aegisbot-20261108T{h}0000Z.sqlite" for h in (14, 15, 16)]


def test_a_missing_or_empty_or_memory_database_is_refused_and_leaves_no_file(tmp_path):
    with pytest.raises(backup.BackupError):
        backup.run(f"sqlite:///{tmp_path / 'missing.db'}", tmp_path / "bk", now=T0)
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    with pytest.raises(backup.BackupError):
        backup.run(f"sqlite:///{empty}", tmp_path / "bk", now=T0)
    with pytest.raises(backup.BackupError):
        backup.run("sqlite://", tmp_path / "bk", now=T0)
    assert list((tmp_path / "bk").glob("aegisbot-*")) == []


def test_other_database_types_are_refused(tmp_path):
    with pytest.raises(backup.BackupError):
        backup.run("mysql://u:p@h/db", tmp_path / "bk", now=T0)


def test_the_command_line_never_prints_the_database_url(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AEGIS_DATABASE_URL", "postgresql://user:SECRETPASSWORD@nowhere.invalid:5432/db")
    monkeypatch.setattr(backup.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    assert backup.main(["--out", str(tmp_path / "bk")]) == 2
    err = capsys.readouterr().err
    assert "SECRETPASSWORD" not in err and "nowhere.invalid" not in err and "pg_dump is not installed" in err


def test_postgres_dump_arguments_carry_no_password_on_the_command_line(tmp_path, monkeypatch):
    seen = {}

    def fake(cmd, **kw):
        if cmd[0] == "pg_dump":
            seen["cmd"], seen["env"] = cmd, kw.get("env", {})
            (tmp_path / "bk" / "aegisbot-20261108T120000Z.dump").write_bytes(b"PGDMP")
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(backup.subprocess, "run", fake)
    (tmp_path / "bk").mkdir()
    backup.backup_postgres("postgresql://user:SECRETPASSWORD@db.example.com:5432/app", tmp_path / "bk", "20261108T120000Z")
    assert "SECRETPASSWORD" not in " ".join(seen["cmd"]) and seen["env"]["PGPASSWORD"] == "SECRETPASSWORD"
