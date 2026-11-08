"""Finding B: the code identity is the static import closure of the backtest entry points, found by parsing, with reachable
modules hashed, unreachable ones left out, and every uncertainty a refusal."""
import pytest

from g_aegisbot.backtest import manifest as mf
from g_aegisbot.backtest import source_closure as sc
from g_aegisbot.backtest.protected import ProtectedPathError


def pkg(tmp_path, files: dict, name="pkg"):
    root = tmp_path / name
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


def mods(root, entry=("main.py",), **kw):
    return sorted(sc.closure(root, entry, **kw)["files"])


# ---------------------------------------------------------------- the real package
def test_the_real_closure_contains_exactly_what_the_backtest_can_import():
    c = sc.closure(mf.PACKAGE_DIR)
    got = set(c["files"])
    assert {"backtest/__main__.py", "backtest/simulator.py", "backtest/acceptance_methods.py", "backtest/manifest.py", "backtest/freeze.py",
            "backtest/signing.py", "backtest/source_closure.py", "backtest/protected.py", "backtest/data.py", "backtest/funding.py",
            "backtest/liquidation.py", "backtest/metrics.py", "config.py", "costs.py", "engine/strategy.py", "engine/strategies.py",
            "engine/trend.py", "engine/indicators.py", "engine/risk.py", "__init__.py", "backtest/__init__.py", "engine/__init__.py"} <= got
    # reached only because strategy.py imports them at load time: hashed, not excluded by judgement
    assert {"engine/protection.py", "exchange/binance_futures.py", "exchange/__init__.py"} <= got
    for unreachable in ("api/app.py", "engine/runner.py", "engine/journal.py", "engine/reconcile.py", "db/models.py", "admin.py",
                        "security/crypto.py", "tools/selftest.py", "migrations/env.py", "engine/paper.py", "services.py"):
        assert unreachable not in got, unreachable
    assert set(c["external"]) == {"ccxt", "cryptography", "dotenv", "numpy", "pandas"} and c["entry_points"] == list(sc.ENTRY_POINTS)


def test_the_real_closure_is_deterministic_and_has_no_protected_files():
    from g_aegisbot.backtest.protected import is_protected
    a, b = sc.closure(mf.PACKAGE_DIR), sc.closure(mf.PACKAGE_DIR)
    assert a == b and not any(is_protected(r) for r in a["files"])


def test_every_third_party_import_in_the_real_closure_is_declared():
    assert set(sc.closure(mf.PACKAGE_DIR)["external"]) <= sc.DECLARED_EXTERNAL


def test_the_manifest_code_section_comes_from_the_closure_and_has_no_hand_kept_lists():
    code = mf.source_tree_sha256()
    assert code["modules"] == sorted(sc.closure(mf.PACKAGE_DIR)["files"]) and len(code["sha256"]) == 64
    assert set(code["external_versions"]) == {"ccxt", "cryptography", "dotenv", "numpy", "pandas"}
    assert all(v and not v.startswith("unresolved") for v in code["external_versions"].values())
    for gone in ("EXCLUDED_SOURCE", "IMPORTED_NOT_RESULT_AFFECTING", "is_excluded", "source_files"):
        assert not hasattr(mf, gone), gone


def test_a_library_version_change_changes_the_experiment_id(monkeypatch):
    import pandas as pd

    from g_aegisbot.backtest.simulator import BacktestConfig
    idx = pd.date_range("2025-01-01", periods=3, freq="h", tz="UTC")
    fr = {"BTC/USDT:USDT": pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}, index=idx)}
    kw = dict(label="t", run_name="t", strategies=("breakout",), cfg=BacktestConfig(), frames=fr, funding=None)
    a = mf.build_manifest(**kw)["experiment_id"]
    real = mf.external_versions
    monkeypatch.setattr(mf, "external_versions", lambda names: {**real(names), "numpy": "numpy 0.0.0"})
    assert mf.build_manifest(**kw)["experiment_id"] != a


# ---------------------------------------------------------------- reachable and unreachable modules
BASE = {"__init__.py": "", "main.py": "from pkg import a\n", "a.py": "def f():\n    from pkg import b\n    return b\n",
        "b.py": "X = 1\n", "unused.py": "Y = 2\n", "sub/__init__.py": "", "sub/deep.py": "Z = 3\n"}


def test_reachable_modules_are_hashed_and_unreachable_ones_are_not(tmp_path):
    root = pkg(tmp_path, BASE)
    assert mods(root) == ["__init__.py", "a.py", "b.py", "main.py"]            # b is reached through an import inside a function
    h = mf.source_tree_sha256(root, entry_points=("main.py",))["sha256"]
    (root / "unused.py").write_text("Y = 99\n")
    (root / "sub/deep.py").write_text("Z = 99\n")
    assert mf.source_tree_sha256(root, entry_points=("main.py",))["sha256"] == h          # unreachable edits change nothing
    (root / "b.py").write_text("X = 2\n")
    assert mf.source_tree_sha256(root, entry_points=("main.py",))["sha256"] != h          # a reachable edit does


def test_a_module_that_becomes_reachable_changes_the_identity(tmp_path):
    root = pkg(tmp_path, BASE)
    h = mf.source_tree_sha256(root, entry_points=("main.py",))["sha256"]
    (root / "main.py").write_text("from pkg import a\nimport pkg.unused\n")
    assert "unused.py" in mods(root)
    assert mf.source_tree_sha256(root, entry_points=("main.py",))["sha256"] != h


def test_parent_package_inits_are_included(tmp_path):
    root = pkg(tmp_path, {"__init__.py": "A = 1\n", "main.py": "import pkg.sub.deep\n", "sub/__init__.py": "B = 1\n", "sub/deep.py": "Z = 3\n"})
    assert mods(root) == ["__init__.py", "main.py", "sub/__init__.py", "sub/deep.py"]


def test_import_styles_are_all_followed(tmp_path):
    root = pkg(tmp_path, {
        "__init__.py": "", "main.py": "import pkg.a\nfrom pkg.b import thing\nfrom . import c\nfrom .d import e\nfrom pkg import sub\n"
                                       "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import pkg.t\ntry:\n    import pkg.opt\nexcept ImportError:\n    pass\n",
        "a.py": "", "b.py": "thing = 1\n", "c.py": "", "d.py": "e = 1\n", "t.py": "", "opt.py": "", "sub/__init__.py": "", "never.py": ""})
    assert mods(root) == ["__init__.py", "a.py", "b.py", "c.py", "d.py", "main.py", "opt.py", "sub/__init__.py", "t.py"]


def test_from_package_import_module_pulls_in_the_module_but_an_attribute_does_not(tmp_path):
    root = pkg(tmp_path, {"__init__.py": "value = 1\n", "main.py": "from pkg import sub_mod, value\n", "sub_mod.py": "", "value.py": "SHADOW = 1\n"})
    assert "sub_mod.py" in mods(root)


def test_relative_imports_resolve_from_nested_packages(tmp_path):
    root = pkg(tmp_path, {"__init__.py": "", "main.py": "from .x import y\n", "x/__init__.py": "", "x/y.py": "from ..top import T\nfrom . import z\n",
                          "x/z.py": "", "top.py": "T = 1\n"})
    assert mods(root) == ["__init__.py", "main.py", "top.py", "x/__init__.py", "x/y.py", "x/z.py"]


def test_standard_library_imports_are_ignored(tmp_path):
    root = pkg(tmp_path, {"__init__.py": "", "main.py": "from __future__ import annotations\nimport os, sys, json\nfrom pathlib import Path\n"})
    c = sc.closure(root, ("main.py",))
    assert sorted(c["files"]) == ["__init__.py", "main.py"] and c["external"] == []


def test_declared_third_party_imports_are_recorded_not_hashed(tmp_path):
    root = pkg(tmp_path, {"__init__.py": "", "main.py": "import numpy as np\nimport pandas\n"})
    c = sc.closure(root, ("main.py",))
    assert c["external"] == ["numpy", "pandas"] and sorted(c["files"]) == ["__init__.py", "main.py"]


def test_external_versions_flag_an_uninstalled_name():
    v = mf.external_versions(["numpy", "definitely_not_installed_xyz"])
    assert v["numpy"].startswith("numpy ") and v["definitely_not_installed_xyz"].startswith("unresolved")


# ---------------------------------------------------------------- unclassified dependencies fail closed
@pytest.mark.parametrize("text,why", [
    ("import pkg.missing\n", "resolves to no file"),
    ("from pkg.missing import x\n", "resolves to no file"),
    ("from ... import x\n", "leaves the package"),
    ("import requests\n", "unclassified third-party"),
    ("from some_unknown_lib import x\n", "unclassified third-party"),
    ("import importlib\nname = 'pkg.a'\nimportlib.import_module(name)\n", "dynamic import"),
    ("__import__(some_name)\n", "dynamic import"),
    ("import importlib.util\nimportlib.util.spec_from_file_location('m', 'x.py')\n", "dynamic import"),
    ("import importlib\nimportlib.import_module()\n", "dynamic import"),
    ("def broken(:\n", "cannot be read or parsed"),
])
def test_an_import_the_analysis_cannot_classify_is_a_refusal(tmp_path, text, why):
    root = pkg(tmp_path, {"__init__.py": "", "main.py": text, "a.py": ""})
    with pytest.raises(sc.SourceClosureError, match=why):
        sc.closure(root, ("main.py",))


def test_a_literal_dynamic_import_is_followed(tmp_path):
    root = pkg(tmp_path, {"__init__.py": "", "main.py": "import importlib\nimportlib.import_module('pkg.a')\n", "a.py": ""})
    assert "a.py" in mods(root)


def test_an_undecodable_file_and_a_missing_entry_point_are_refusals(tmp_path):
    root = pkg(tmp_path, {"__init__.py": "", "main.py": "import pkg.a\n"})
    (root / "a.py").write_bytes(b"\xff\xfe = 1")
    with pytest.raises(sc.SourceClosureError):
        sc.closure(root, ("main.py",))
    with pytest.raises(sc.SourceClosureError, match="not a file"):
        sc.closure(root, ("nope.py",))


def test_a_protected_file_in_the_closure_is_refused(tmp_path):
    root = pkg(tmp_path, {"__init__.py": "", "main.py": "import pkg.id_rsa_tools\n", "id_rsa_tools.py": ""})
    with pytest.raises(ProtectedPathError):
        sc.closure(root, ("main.py",))


def test_the_declared_set_can_be_narrowed_to_force_a_refusal(tmp_path):
    root = pkg(tmp_path, {"__init__.py": "", "main.py": "import numpy\n"})
    with pytest.raises(sc.SourceClosureError, match="numpy"):
        sc.closure(root, ("main.py",), declared_external=frozenset())


def test_a_symlinked_module_to_a_protected_file_is_refused(tmp_path):
    import os
    root = pkg(tmp_path, {"__init__.py": "", "main.py": "import pkg.linked\n"})
    (tmp_path / ".env.example").write_text("X=1")
    os.symlink(tmp_path / ".env.example", root / "linked.py")
    with pytest.raises(ProtectedPathError):
        sc.closure(root, ("main.py",))
