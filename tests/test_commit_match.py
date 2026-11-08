"""code_matches_commit: the closure files must be exactly what HEAD contains. Real git, in throw-away repositories."""
import subprocess

import pytest

from g_aegisbot.backtest import manifest as mf


def git(root, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=root, check=True,
                          capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    pkg = root / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "a.py").write_text("x = 1\n")
    (pkg / "b.py").write_text("y = 2\n")
    git(root, "init", "-q")
    git(root, "add", "pkg")
    git(root, "commit", "-q", "-m", "init")
    return root


def check(root, files=("a.py", "b.py")):
    return mf.code_matches_commit(list(files), root, root / "pkg")


def test_a_committed_unmodified_package_matches(repo):
    r = check(repo)
    assert r["matches"] is True and r["checked"] == 2 and r["mismatched"] == []


def test_an_edited_file_is_reported_by_name(repo):
    (repo / "pkg" / "a.py").write_text("x = 99\n")
    r = check(repo)
    assert r["matches"] is False and r["mismatched"] == ["a.py"]


def test_a_staged_but_uncommitted_edit_still_does_not_match(repo):
    (repo / "pkg" / "b.py").write_text("y = 3\n")
    git(repo, "add", "pkg/b.py")
    assert check(repo)["mismatched"] == ["b.py"]


def test_a_new_uncommitted_file_in_the_closure_does_not_match(repo):
    (repo / "pkg" / "c.py").write_text("z = 3\n")
    r = check(repo, ("a.py", "b.py", "c.py"))
    assert r["matches"] is False and "not committed" in r["error"]


def test_a_committed_change_matches_again(repo):
    (repo / "pkg" / "a.py").write_text("x = 5\n")
    git(repo, "commit", "-q", "-am", "edit")
    assert check(repo)["matches"] is True


def test_outside_a_repository_or_with_nothing_to_check_the_answer_is_false_and_nothing_raises(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("x = 1\n")
    assert mf.code_matches_commit(["a.py"], tmp_path, tmp_path / "pkg")["matches"] is False
    assert mf.code_matches_commit([], tmp_path, tmp_path / "pkg")["matches"] is False
    assert mf.code_matches_commit(["missing.py"], tmp_path, tmp_path / "pkg")["matches"] is False


def test_a_package_outside_the_repository_does_not_match(repo, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "a.py").write_text("x = 1\n")
    assert mf.code_matches_commit(["a.py"], repo, other)["matches"] is False


def test_only_rev_parse_is_ever_asked_of_git(repo, monkeypatch):
    calls = []
    real = subprocess.run
    monkeypatch.setattr(subprocess, "run", lambda argv, *a, **k: (calls.append(list(argv)), real(argv, *a, **k))[1])
    check(repo)
    assert calls and all(c[:2] == ["git", "rev-parse"] for c in calls)


def test_the_answer_is_part_of_eligibility_not_of_the_experiment_identity(repo, monkeypatch):
    import pandas as pd
    from g_aegisbot.backtest.simulator import BacktestConfig
    from tests.test_protected_paths import frames
    kw = dict(label="t", run_name="t", strategies=("breakout",), cfg=BacktestConfig(), frames=frames(), funding=None, repo_root=repo)
    monkeypatch.setattr(mf, "code_matches_commit", lambda *a, **k: {"matches": True, "checked": 1, "mismatched": []})
    yes = mf.build_manifest(**kw)
    monkeypatch.setattr(mf, "code_matches_commit", lambda *a, **k: {"matches": False, "checked": 1, "mismatched": ["x.py"]})
    no = mf.build_manifest(**kw)
    assert yes["eligibility"]["conditions"]["code_matches_commit"] is True and no["eligibility"]["conditions"]["code_matches_commit"] is False
    assert no["eligibility"]["acceptance_eligible"] is False and yes["experiment_id"] == no["experiment_id"]
    assert no["code"]["committed"]["mismatched"] == ["x.py"]
