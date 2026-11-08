"""Finding C: the manifest process must never read, list or hash protected files. Git-related tests use isolated temporary
repositories only. An audit hook records every file open, directory listing and subprocess during the manifest run."""
import json
import os
import shutil
import subprocess
import sys

import pandas as pd
import pytest

from g_aegisbot.backtest import freeze as fz
from g_aegisbot.backtest import manifest as mf
from g_aegisbot.backtest import source_closure as sc
from g_aegisbot.backtest.protected import ProtectedPathError, assert_not_protected, is_protected
from g_aegisbot.backtest.simulator import BacktestConfig
from tests import conftest as guard
from tests.signing_helpers import approved_repo

# ---------------------------------------------------------------- a recorder that stays inert unless a test switches it on
_RECORD = {"on": False, "root": None, "events": []}


def _recorder(event, args):
    if not _RECORD["on"]:
        return
    root = _RECORD["root"]
    if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
        p = os.path.realpath(os.fsdecode(args[0]))
        if p.startswith(root):
            _RECORD["events"].append(("open", p))
    elif event in ("os.listdir", "os.scandir") and args and isinstance(args[0], (str, bytes, os.PathLike)):
        p = os.path.realpath(os.fsdecode(args[0]))
        if p.startswith(root):
            _RECORD["events"].append(("list", p))
    elif event == "subprocess.Popen":
        _RECORD["events"].append(("run", [os.fsdecode(a) for a in args[1]], args[2]))


sys.addaudithook(_recorder)


class Recording:
    def __init__(self, root):
        self.root = os.path.realpath(root)

    def __enter__(self):
        _RECORD.update(on=True, root=self.root, events=[])
        return _RECORD["events"]

    def __exit__(self, *exc):
        _RECORD["on"] = False


PROTECTED_NAMES = [".env", ".env.example", ".env.local", ".ENV", ".Env.Production", "server.pem", "id_rsa", "id_ed25519.pub", "wallet.key",
                   "data.db", "data.sqlite", "data.sqlite3", "data.db-wal", "x.db-shm", "cert.p12", "secrets/token.txt", "sub/.ssh/config",
                   "a/.aws/credentials", "x/.gnupg/pubring.kbx"]
OPEN_NAMES = ["manifest.py", "env.py", "environment.md", ".environment_notes", "envelope.json", "development.json", "keyring.md",
              "docs/STRATEGY_ACCEPTANCE.md", "dotenv_helpers.py", "secretary.py", "database.md"]


@pytest.mark.parametrize("name", PROTECTED_NAMES)
def test_protected_names_are_recognised(name):
    assert is_protected("/work/repo/" + name) and is_protected(name)
    with pytest.raises(ProtectedPathError):
        assert_not_protected(name)


@pytest.mark.parametrize("name", OPEN_NAMES)
def test_ordinary_names_are_not_protected(name):
    assert not is_protected("/work/repo/" + name)
    assert_not_protected(name)


def test_a_symlink_to_a_protected_file_is_protected_whatever_it_is_called(tmp_path):
    (tmp_path / ".env.example").write_text("A=1")
    os.symlink(tmp_path / ".env.example", tmp_path / "innocent.json")
    assert is_protected(tmp_path / "innocent.json")
    os.symlink(tmp_path, tmp_path / "plain_dir_link")
    assert not is_protected(tmp_path / "plain_dir_link" / "notes.md")


def test_hashing_refuses_a_protected_file_before_reading_it(tmp_path):
    f = tmp_path / ".env"
    f.write_text("TOKEN=1")
    with Recording(tmp_path) as events:
        with pytest.raises(ProtectedPathError):
            mf.sha256_file(f)
    assert events == []                                                      # not even opened


def test_the_source_closure_refuses_to_follow_into_a_protected_file(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "main.py").write_text("import pkg.id_rsa_helper\n")
    (pkg / "id_rsa_helper.py").write_text("X = 1\n")
    with Recording(tmp_path) as events:
        with pytest.raises(ProtectedPathError):
            sc.closure(pkg, ("main.py",))
    assert not any(e[0] == "open" and e[1].endswith("id_rsa_helper.py") for e in events)


# ---------------------------------------------------------------- the manifest process in an isolated repository
def scratch_repo(tmp_path):
    root, key = approved_repo(tmp_path, name="scratch")
    for rel in (".env", ".env.example", ".env.local", "secrets/token.txt", "server.pem", "prod.db", "wallet.key", "sub/.ssh/config"):
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("PROTECTED-CONTENT-" + rel)
    (root / "notes.md").write_text("ordinary")
    run = lambda *a: subprocess.run(["git", *a], cwd=root, check=True, capture_output=True)
    run("init", "-q")
    run("add", "notes.md", "docs", ".env.example")                           # a tracked protected file, as in the real repository
    run("-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-q", "-m", "x")
    (root / ".env.example").write_text("MODIFIED-PROTECTED-CONTENT")         # an uncommitted change to it
    return root


def _rmtree(path):
    for dirpath, dirs, files in os.walk(path):            # git object files are read-only: make everything removable first
        os.chmod(dirpath, 0o700)
        for f in files:
            os.chmod(os.path.join(dirpath, f), 0o600)
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def scratch(tmp_path):
    root = scratch_repo(tmp_path)
    yield root
    _rmtree(root)


def frames():
    idx = pd.date_range("2025-01-01", periods=3, freq="h", tz="UTC")
    return {"BTC/USDT:USDT": pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}, index=idx)}


def test_the_manifest_process_never_reads_lists_or_mentions_protected_files(scratch):
    root = scratch
    with Recording(root) as events:
        m = mf.build_manifest(label="t", run_name="t", strategies=("breakout",), cfg=BacktestConfig(), frames=frames(), funding=None,
                              repo_root=root)
    opened = [e[1] for e in events if e[0] == "open"]
    listed = [e[1] for e in events if e[0] == "list"]
    assert not any(is_protected(p) for p in opened), opened
    assert not any(is_protected(p) or p.rstrip("/") == os.path.realpath(root) for p in listed), listed     # the repo root is not even listed
    git_runs = [e[1] for e in events if e[0] == "run" and e[1][:1] == ["git"]]
    assert git_runs == [["git", "rev-parse", "HEAD"]], events                       # the only git command, and it reads no worktree file
    text = json.dumps(m, default=str)
    for leak in (".env", "secrets", "server.pem", "prod.db", "wallet.key", ".ssh", "PROTECTED-CONTENT", "MODIFIED"):
        assert leak not in text, leak
    for gone in ("changed_paths", "working_tree_sha256", "dirty"):
        assert gone not in text, gone


def test_git_state_runs_rev_parse_only_and_ignores_a_modified_protected_file(scratch):
    root = scratch
    with Recording(root) as events:
        state = mf.git_state(root)
    assert set(state) == {"commit", "working_tree"} and len(state["commit"]) == 40
    assert [e[1] for e in events if e[0] == "run"] == [["git", "rev-parse", "HEAD"]]
    assert not [e for e in events if e[0] in ("open", "list")]
    (root / ".env.example").write_text("ANOTHER-CHANGE")
    assert mf.git_state(root) == state                                       # protected-file content cannot influence it


def test_git_state_never_raises_outside_a_repository(tmp_path):
    assert mf.git_state(tmp_path)["commit"] is None and "error" in mf.git_state(tmp_path)


def test_the_manifest_does_not_depend_on_the_working_tree_state(scratch):
    root = scratch
    kw = dict(label="t", run_name="t", strategies=("breakout",), cfg=BacktestConfig(), frames=frames(), funding=None, repo_root=root)
    a = mf.build_manifest(**kw)["experiment_id"]
    (root / "notes.md").write_text("changed")
    (root / ".env.example").write_text("also changed")
    (root / "untracked_secret.pem").write_text("k")
    assert mf.build_manifest(**kw)["experiment_id"] == a


def test_approval_and_protocol_files_are_read_but_protected_neighbours_are_not(scratch):
    root = scratch
    with Recording(root) as events:
        r = fz.validate_protocol_approval(root)
    assert r["approved"] is True
    assert not any(is_protected(e[1]) for e in events if e[0] == "open")


# ---------------------------------------------------------------- the test-session guard itself (calls the hook function only)
def test_the_session_guard_blocks_protected_opens_in_the_real_repository_before_they_happen():
    probe = guard._REPO / ".env.guard_probe"                                 # a name that matches the rule and does not exist
    before = len(guard.ATTEMPTS)
    with pytest.raises(PermissionError):
        guard._guard("open", (str(probe), "r", 0))
    with pytest.raises(PermissionError):
        guard._guard("open", (guard._REPO / "keys" / "x.pem", "r", 0))
    assert len(guard.ATTEMPTS) == before + 2
    guard._guard("open", (str(guard._REPO / "README.md"), "r", 0))           # ordinary files are fine
    guard._guard("open", ("/tmp/some/.env", "r", 0))                          # outside the repository is not this guard's business
    guard._guard("open", (3, "r", 0))                                         # file descriptors are ignored


def test_the_session_guard_blocks_broad_git_inspection_in_the_real_repository():
    for sub in ("status", "diff", "ls-files", "show", "log", "grep", "add", "stash"):
        with pytest.raises(PermissionError):
            guard._guard("subprocess.Popen", ("git", ["git", sub], str(guard._REPO), None))
    with pytest.raises(PermissionError):
        guard._guard("subprocess.Popen", ("git", ["git", "-C", str(guard._REPO), "diff", "HEAD"], str(guard._REPO), None))
    guard._guard("subprocess.Popen", ("git", ["git", "rev-parse", "HEAD"], str(guard._REPO), None))     # rev-parse is allowed
    guard._guard("subprocess.Popen", ("git", ["git", "status"], "/tmp/scratch", None))                   # scratch repositories are allowed


def test_the_session_guard_is_really_installed_in_this_process():
    probe = guard._REPO / ".env.guard_probe"                                 # matches the rule, does not exist, is never created
    with pytest.raises(PermissionError):                                     # a missing file would give FileNotFoundError instead
        open(probe)
