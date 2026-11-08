"""Experiment manifest: everything needed to say exactly what a backtest run was, and to check it was reproduced.

Each saved run directory gets a `manifest.json`. It records:

  * code           SHA-256 of every Python file in the static import closure of the backtest entry points (see
                   source_closure.py), the third-party libraries that closure uses with their versions, and the git COMMIT id
                   for reference. No working-tree content is read or hashed (see GIT below);
  * environment    interpreter, platform, and numeric-library build information (the part that can change numbers);
  * strategy       enabled strategies and the full StrategyParams, with a hash;
  * config         every BacktestConfig field, with a hash; liquidation brackets expanded;
  * assumptions    execution, fee, slippage, funding and liquidation assumptions, in words and numbers;
  * data           per symbol: rows, first and last candle, SHA-256 of the exact candles replayed;
  * funding        per symbol: events, first and last timestamp, SHA-256 of the exact rates replayed (validated);
  * protocol       the protocol document hash, whether it is approved, the freeze record hash and whether it is valid;
  * holdout        the MEASURED start and end and whether they overlap the frozen confirmation period (from timestamps);
  * eligibility    synthetic_data, data_integrity_passed, protocol_approved, freeze_record_valid, acceptance_eligible;
  * outputs        SHA-256 of summary.json, trades.csv and equity.csv (all three are required).

SOURCE SCOPE. Found from the code, not from a list: the files reachable by import from `backtest/__main__.py`,
`backtest/simulator.py` and `backtest/acceptance_methods.py` (source_closure.ENTRY_POINTS), every one of them hashed, including
modules reached only to be imported. Files nobody imports from there (web API, database layer, live runner...) are not part of
the identity because nothing in a backtest can execute them. An import the analysis cannot resolve, a dynamic import, or an
unclassified third-party import makes manifest creation fail rather than guess.

GIT. Only `git rev-parse HEAD` is run, which reads no working-tree file. `git status`, `git diff` and file listings are never
used: they would reach protected files (see protected.py). Whether the working tree is dirty is therefore NOT recorded; the
closure hash identifies the code that ran, whatever git says. Every file the manifest hashes is refused first if it, or any
directory above it, or a symlink target, is a protected path.

`experiment_id` is a SHA-256 over the parts that decide the result: code, interpreter and numeric environment, strategy,
config, assumptions, data, funding, liquidation, measured period, data policy, protocol and freeze hashes, run kind. It does
not include the time, the command line, file locations or the outputs. Reproducibility is NOT just a matching id:
`verify_outputs` checks every required output hash, and `compare_manifests` checks every input hash.

A manifest describes a run. It never says the strategy is good; the acceptance decision belongs to the protocol.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from g_aegisbot.backtest import source_closure as sc
from g_aegisbot.backtest import safeio
from g_aegisbot.backtest.protected import assert_not_protected

MANIFEST_VERSION = 3
MANIFEST_NAME = "manifest.json"
REQUIRED_OUTPUTS = ("summary.json", "trades.csv", "equity.csv")
ALLOWED_FILES = REQUIRED_OUTPUTS + (MANIFEST_NAME,)
PROTOCOL_DOC = "docs/STRATEGY_ACCEPTANCE.md"
REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIR = Path(__file__).resolve().parents[1]


class ManifestError(Exception):
    pass


class ManifestIntegrityError(ManifestError):
    """Data fed to the manifest is malformed (funding arrays, outputs). Never silently repaired or truncated."""


class AcceptanceRefused(ManifestError):
    """An acceptance run was requested but a necessary condition is not met."""


# ------------------------------------------------------------------ source scope (static import closure)
def external_versions(names) -> dict[str, str]:
    """Installed version of each third-party library the closure imports. Read from package metadata, nothing imported."""
    from importlib import metadata
    dists = metadata.packages_distributions()
    out = {}
    for name in sorted(names):
        owners = dists.get(name) or []
        try:
            out[name] = f"{owners[0]} {metadata.version(owners[0])}" if owners else "unresolved: no installed distribution"
        except metadata.PackageNotFoundError:
            out[name] = "unresolved: not installed"
    return out


def source_tree_sha256(package_dir: str | Path = PACKAGE_DIR, entry_points=None) -> dict:
    """Identity of the code: SHA-256 over {relative path: file hash} for the static import closure of the entry points.
    Raises source_closure.SourceClosureError when the closure cannot be established with certainty."""
    c = sc.closure(package_dir, tuple(entry_points) if entry_points is not None else sc.ENTRY_POINTS)
    return {"files": len(c["files"]), "sha256": digest(c["files"]), "modules": sorted(c["files"]),
            "entry_points": c["entry_points"], "external_versions": external_versions(c["external"])}


# ------------------------------------------------------------------ hashing helpers
def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    path = safeio.guard_read(path)                 # protected path, link, special file or extra hard link: refused before any read
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _clean(obj):
    """JSON-safe, deterministic form: tuples to lists, inf and nan to strings, numpy scalars to Python, unknown to text."""
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        items = [_clean(v) for v in obj]
        return sorted(items, key=json.dumps) if isinstance(obj, set) else items
    if isinstance(obj, np.generic):
        return _clean(obj.item())
    if isinstance(obj, float):
        return repr(obj) if (math.isinf(obj) or math.isnan(obj)) else obj
    if isinstance(obj, (str, int, bool)) or obj is None:
        return obj
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return _clean(dataclasses.asdict(obj))
    return str(obj)


def canonical(obj) -> str:
    return json.dumps(_clean(obj), sort_keys=True, separators=(",", ":"))


def digest(obj) -> str:
    return sha256_bytes(canonical(obj).encode())


# ------------------------------------------------------------------ git (reference only)
def git_state(repo_root: str | Path = REPO_ROOT) -> dict:
    """The commit id and nothing else. `git rev-parse HEAD` reads the ref database, never a working-tree file, so it cannot
    touch a protected file. No status, no diff, no listing, no hashing of changed or untracked files. Never raises."""
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo_root), capture_output=True, text=True, timeout=20,
                                check=True).stdout.strip()
        return {"commit": commit, "working_tree": "not recorded; the code identity is the import-closure hash"}
    except Exception as exc:
        return {"commit": None, "working_tree": "not recorded", "error": f"{type(exc).__name__}"}


def _blob_sha1(data: bytes) -> str:
    """The id git gives a file's content (`git hash-object`), computed here so no git command has to touch the worktree."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def code_matches_commit(files, repo_root: str | Path = REPO_ROOT, package_dir: str | Path = PACKAGE_DIR) -> dict:
    """Is every file of the import closure byte-for-byte what is committed at HEAD? `files` are the closure's paths relative
    to the package. Only closure files are read (they can never be protected files) and git is asked only
    `rev-parse HEAD:<path>`, which reads the object database: no status, no diff, no listing of the working tree. A file
    that is edited, new or not committed makes the answer False. Never raises: any doubt is False.

    This is what makes "run acceptance from a committed checkout" checkable: the code hash says WHAT ran, this says it is
    exactly what the recorded commit contains, so the run can be reproduced from that commit."""
    try:
        root, pkg = Path(repo_root).resolve(), Path(package_dir).resolve()
        rels = {rel: (pkg / rel).resolve().relative_to(root).as_posix() for rel in files}
        if not rels:
            return {"matches": False, "checked": 0, "mismatched": [], "error": "no files"}
        local = {rel: _blob_sha1(safeio.read_bytes(pkg / rel, 8 << 20)) for rel in rels}
        done = subprocess.run(["git", "rev-parse", *[f"HEAD:{r}" for r in rels.values()]], cwd=str(root),
                              capture_output=True, text=True, timeout=60)
        if done.returncode != 0:
            return {"matches": False, "checked": len(rels), "mismatched": [],
                    "error": "a closure file is not committed at HEAD (or git could not be asked)"}
        committed = done.stdout.split()
        if len(committed) != len(rels):
            return {"matches": False, "checked": len(rels), "mismatched": [], "error": "unexpected git answer"}
        bad = sorted(rel for rel, c in zip(rels, committed) if local[rel] != c)
        return {"matches": not bad, "checked": len(rels), "mismatched": bad}
    except Exception as exc:
        return {"matches": False, "checked": 0, "mismatched": [], "error": type(exc).__name__}


# ------------------------------------------------------------------ environment
def _numeric_build() -> dict:
    """BLAS and LAPACK name and version from numpy's build configuration, where numpy reports it."""
    try:
        cfg = np.show_config(mode="dicts")
        deps = cfg.get("Build Dependencies", {})
        return {k: {"name": deps[k].get("name"), "version": deps[k].get("version")} for k in ("blas", "lapack") if k in deps}
    except Exception as exc:
        return {"unavailable": type(exc).__name__}


def environment() -> dict:
    import ccxt
    return {
        "python_implementation": platform.python_implementation(), "python_version": platform.python_version(),
        "python_compiler": platform.python_compiler(), "system": platform.system(), "machine": platform.machine(),
        "byteorder": sys.byteorder, "platform": platform.platform(),
        "numpy": np.__version__, "numpy_build": _numeric_build(), "pandas": pd.__version__, "ccxt": ccxt.__version__,
    }


# What of the environment can change a result. `platform` (kernel release text) is recorded but not part of the identity.
IDENTITY_ENVIRONMENT_KEYS = ("python_implementation", "python_version", "python_compiler", "system", "machine", "byteorder",
                             "numpy", "numpy_build", "pandas", "ccxt")


def identity_environment(env: dict) -> dict:
    return {k: env.get(k) for k in IDENTITY_ENVIRONMENT_KEYS}


# ------------------------------------------------------------------ data and funding
def frame_inventory(frames: dict[str, pd.DataFrame]) -> dict:
    out = {}
    for sym in sorted(frames):
        df = frames[sym]
        out[sym] = {"rows": int(len(df)), "first": str(df.index[0]) if len(df) else None,
                    "last": str(df.index[-1]) if len(df) else None, "sha256": sha256_bytes(df.to_csv().encode())}
    return out


def funding_inventory(funding: dict | None) -> dict | None:
    """Validated inventory of the funding data actually replayed. Any malformed schedule raises ManifestIntegrityError:
    unequal lengths, empty, not one-dimensional, non-numeric, non-finite, or timestamps not strictly increasing. Nothing is
    truncated, skipped or repaired."""
    if funding is None:
        return None
    out = {}
    for sym in sorted(funding):
        sch = funding[sym]
        try:
            ts = np.asarray(sch.ts)
            rate = np.asarray(sch.rate)
        except Exception as exc:
            raise ManifestIntegrityError(f"funding for {sym} is unreadable ({type(exc).__name__})") from exc
        if ts.ndim != 1 or rate.ndim != 1:
            raise ManifestIntegrityError(f"funding for {sym} is not one-dimensional")
        if len(ts) != len(rate):
            raise ManifestIntegrityError(f"funding for {sym} has {len(ts)} timestamps but {len(rate)} rates")
        if len(ts) == 0:
            raise ManifestIntegrityError(f"funding for {sym} has no events")
        if ts.dtype == bool or rate.dtype == bool or ts.dtype.kind not in "iuf" or rate.dtype.kind not in "iuf":
            raise ManifestIntegrityError(f"funding for {sym} contains non-numeric values")
        if not np.all(np.isfinite(ts)) or not np.all(np.isfinite(rate)):
            raise ManifestIntegrityError(f"funding for {sym} contains a non-finite timestamp or rate")
        if np.any(ts != np.floor(ts)):
            raise ManifestIntegrityError(f"funding for {sym} has a fractional millisecond timestamp")
        ts = ts.astype(np.int64)
        if np.any(np.diff(ts) <= 0):
            raise ManifestIntegrityError(f"funding timestamps for {sym} are not strictly increasing")
        text = "\n".join(f"{int(t)},{float(r)!r}" for t, r in zip(ts, rate))
        out[sym] = {"events": int(len(ts)), "first_ms": int(ts[0]), "last_ms": int(ts[-1]), "sha256": sha256_bytes(text.encode())}
    return out


# ------------------------------------------------------------------ configuration and assumptions
def _liq(model) -> dict:
    return {"source": model.source,
            "brackets": [{"notional_cap": b.notional_cap, "mmr": b.mmr, "cum": b.cum} for b in model.brackets]}


def config_snapshot(cfg) -> dict:
    snap = {}
    for f in dataclasses.fields(cfg):
        v = getattr(cfg, f.name)
        if f.name == "funding_history":
            snap[f.name] = "see funding" if v else None
        elif f.name == "liquidation":
            snap[f.name] = _liq(v)
        elif f.name == "liquidation_overrides":
            snap[f.name] = {s: _liq(m) for s, m in sorted(v.items())}
        else:
            snap[f.name] = v
    return _clean(snap)


def liquidation_kind(cfg, symbols) -> str:
    """'exact' only when every symbol is covered by bracket data loaded from a file; 'exploratory' if any symbol falls back
    to the flat default (whose source text starts with 'flat')."""
    def exact(model) -> bool:
        return not model.source.startswith("flat")
    return "exact" if all(exact(cfg.liquidation_overrides.get(s, cfg.liquidation)) for s in symbols) else "exploratory"


def assumptions_snapshot(cfg, symbols) -> dict:
    from g_aegisbot.backtest.simulator import build_assumptions
    from g_aegisbot.costs import STOP_SLIPPAGE_MULT, slippage_rate
    return {
        "text": build_assumptions(cfg),
        "numbers": {
            "taker_fee": cfg.taker_fee, "maker_fee_unused": cfg.maker_fee, "funding_mode": cfg.funding_mode,
            "funding_rate_8h_if_approx": cfg.funding_rate_8h, "slippage_scale": cfg.slippage_scale,
            "stop_slippage_multiplier": STOP_SLIPPAGE_MULT,
            "slippage_per_fill_by_symbol": {s: slippage_rate(s, scale=cfg.slippage_scale) for s in sorted(symbols)},
            "stop_slippage_by_symbol": {s: slippage_rate(s, is_stop=True, scale=cfg.slippage_scale) for s in sorted(symbols)},
            "multiplier": cfg.leverage, "daily_drawdown_limit": cfg.drawdown_limit, "breaker_on": cfg.use_breaker,
            "warmup_candles": cfg.warmup_candles,
        },
    }


def cost_profile_values(cfg) -> dict:
    return {"taker_fee": round(float(cfg.taker_fee), 10), "slippage_scale": float(cfg.slippage_scale),
            "funding_mode": cfg.funding_mode, "breaker_on": bool(cfg.use_breaker)}


def production_risk_limits(cfg) -> bool:
    """True when the run uses the production risk limits: default portfolio and per-trade limits, breaker on at the
    production threshold, production multiplier."""
    from g_aegisbot.config import DAILY_DRAWDOWN_LIMIT, DEFAULT_LEVERAGE
    from g_aegisbot.engine.strategy import StrategyParams
    d, p = StrategyParams(), cfg.params
    same = all(getattr(p, k) == getattr(d, k) for k in ("max_aggregate_risk_pct", "max_same_direction", "max_gross_notional_x",
                                                         "max_risk_pct", "max_open_positions", "leverage"))
    return bool(same and cfg.use_breaker and cfg.drawdown_limit == DAILY_DRAWDOWN_LIMIT and cfg.leverage == DEFAULT_LEVERAGE)


# ------------------------------------------------------------------ command-line arguments
# Every argument of `python -m g_aegisbot.backtest` is classified. "identity": it changes results, and the way it does is
# captured in the experiment id (through the derived data, config, measured period or run kind). "excluded": it demonstrably
# cannot change a result, with the reason. A test fails if the parser gains an argument that is not classified here.
ARG_CLASSIFICATION = {
    "years": ("identity", "sets the measured period when --start is absent; captured by the measured period and data hashes"),
    "start": ("identity", "measured period start; captured by the measured period and data hashes"),
    "end": ("identity", "measured period end; captured by the measured period and data hashes"),
    "timeframe": ("identity", "part of StrategyParams and of the candle data"),
    "capital": ("identity", "BacktestConfig.initial_capital"),
    "leverage": ("identity", "BacktestConfig.leverage (the multiplier)"),
    "symbols": ("identity", "the universe; captured by the per-symbol data hashes"),
    "strategies": ("identity", "enabled strategy set"),
    "compare": ("excluded", "selects WHICH strategy sets are run; each run's identity holds its own strategy set"),
    "split": ("excluded", "selects which extra segments are run; each segment has its own measured period and identity"),
    "windows": ("excluded", "selects which extra windows are run; each window has its own measured period and identity"),
    "funding": ("identity", "BacktestConfig.funding_mode and the funding data hashes"),
    "liq_brackets": ("identity", "the expanded brackets are in the config; the file's hash is recorded"),
    "slippage_scale": ("identity", "BacktestConfig.slippage_scale"),
    "fee_scale": ("identity", "BacktestConfig.taker_fee"),
    "no_breaker": ("identity", "BacktestConfig.use_breaker"),
    "legacy_risk": ("identity", "removes the portfolio limits from StrategyParams"),
    "cache_dir": ("excluded", "where candle files are cached; the candles themselves are hashed"),
    "out": ("excluded", "where results are written"),
    "refresh": ("excluded", "re-download instead of cache; the candles themselves are hashed"),
    "retries": ("excluded", "network retry count; cannot change validated data"),
    "allow_late_start": ("identity", "changes which data is accepted; recorded as data policy"),
    "allow_partial": ("identity", "changes which symbols are used; recorded as data policy"),
    "from_listing": ("identity", "changes the measured period and universe; captured by them"),
    "individually": ("identity", "runs one symbol per portfolio; captured by the universe"),
    "synthetic": ("identity", "synthetic data flag, part of the identity"),
    "seed": ("identity", "seeds synthetic data; captured by the data hashes"),
    "acceptance": ("identity", "run kind; recorded with the freeze and protocol hashes"),
    "freeze_record": ("identity", "its hash is part of the identity"),
}


# ------------------------------------------------------------------ eligibility
def data_integrity_passed(data_integrity: dict | None, frames, excluded_symbols: dict | None, synthetic: bool,
                          funding_ok: bool) -> bool:
    """True only for real data where EVERY replayed symbol has an integrity report with no duplicate, out-of-order or
    missing candle, no symbol was excluded, and funding (if used) passed its checks."""
    if synthetic or not data_integrity or excluded_symbols:
        return False
    for s in frames:
        r = data_integrity.get(s)
        if not isinstance(r, dict) or r.get("duplicates") != 0 or r.get("out_of_order") != 0 or r.get("missing_candles") != 0:
            return False
    return bool(funding_ok)


def build_manifest(*, label: str, run_name: str, strategies, cfg, frames, funding, argv=None, synthetic: bool = False,
                   segment: dict | None = None, measured: dict | None = None, data_integrity: dict | None = None,
                   excluded_symbols: dict | None = None, acceptance: bool = False, freeze_record: dict | None = None,
                   liquidation_file_sha256: str | None = None, data_policy: dict | None = None, arguments: dict | None = None,
                   repo_root: str | Path = REPO_ROOT, package_dir: str | Path = PACKAGE_DIR, now: datetime | None = None,
                   env: dict | None = None) -> dict:
    """The manifest of one run (outputs are added by `attach_outputs`). Raises ManifestIntegrityError for malformed funding
    data. Never raises for a failed acceptance condition: it records it, and acceptance_eligible is then False."""
    from g_aegisbot.backtest import freeze as fz
    params = cfg.params
    strategy_section = {"enabled": list(strategies), "params": _clean(params), "params_sha256": digest(params),
                        "priority": list(params.strategy_priority)}
    config = config_snapshot(cfg)
    data = frame_inventory(frames)
    fund = funding_inventory(funding)                               # may raise: malformed funding is an integrity failure
    code = source_tree_sha256(package_dir)
    environment_ = env or environment()

    first = min((d["first"] for d in data.values() if d["first"]), default=None)
    last = max((d["last"] for d in data.values() if d["last"]), default=None)
    m = measured or {"start": str(cfg.measure_start) if cfg.measure_start is not None else first,
                     "end": str(cfg.measure_end) if cfg.measure_end is not None else last}
    seg = segment or {"name": "full", "start": m["start"], "end": m["end"]}

    committed = code_matches_commit(code["modules"], repo_root, package_dir)
    approval = fz.validate_protocol_approval(repo_root)
    exposure = fz.holdout_exposure(m["start"], m["end"], freeze_record, first, last) if m["start"] and m["end"] else \
        {"measured": m, "exposed": None, "overlaps": [], "data_overlap": None}

    candidate = "+".join(strategies)
    run = {"protocol_sha256": approval["protocol_sha256"], "source_sha256": code["sha256"], "candidate": candidate,
           "strategies": list(strategies), "params_sha256": strategy_section["params_sha256"], "symbols": list(frames),
           "cost_profile_values": cost_profile_values(cfg), "liquidation_file_sha256": liquidation_file_sha256, "measured": m}
    freeze_problems = (["no freeze record was supplied"] if freeze_record is None else
                       fz.freeze_problems(freeze_record, run, repo_root))
    freeze_valid = not freeze_problems
    funding_ok = bool(fund) and cfg.funding_mode == "historical" and not cfg.funding_allow_gaps
    integrity_ok = data_integrity_passed(data_integrity, frames, excluded_symbols, synthetic, funding_ok or cfg.funding_mode != "historical")
    conditions = {
        "not_synthetic": not synthetic,
        "data_integrity_passed": integrity_ok,
        "historical_funding": funding_ok,
        "production_risk_limits": production_risk_limits(cfg),
        "liquidation_brackets_exact": liquidation_kind(cfg, frames.keys()) == "exact",
        "protocol_approved": approval["approved"],
        "freeze_record_valid": freeze_valid,
        "code_matches_commit": committed["matches"],
        "acceptance_requested": bool(acceptance),
    }
    eligibility = {"synthetic_data": bool(synthetic), "data_integrity_passed": integrity_ok,
                   "protocol_approved": approval["approved"], "freeze_record_valid": freeze_valid,
                   "acceptance_eligible": all(conditions.values()), "conditions": conditions,
                   "problems": [*approval["problems"], *freeze_problems]}

    identity = {
        "code": code["sha256"], "code_external": code["external_versions"], "environment": identity_environment(environment_),
        "strategy": strategy_section, "config": config,
        "assumptions": assumptions_snapshot(cfg, frames.keys()), "data": {s: d["sha256"] for s, d in data.items()},
        "funding": {s: d["sha256"] for s, d in (fund or {}).items()}, "liquidation_kind": liquidation_kind(cfg, frames.keys()),
        "liquidation_file_sha256": liquidation_file_sha256, "measured": m, "segment": seg, "synthetic": bool(synthetic),
        "data_policy": data_policy or {}, "protocol_sha256": approval["protocol_sha256"],
        "freeze_sha256": (freeze_record or {}).get("freeze_sha256"), "acceptance_requested": bool(acceptance),
    }
    return {
        "manifest_version": MANIFEST_VERSION,
        "experiment_id": digest(identity),
        "label": label, "run": run_name,
        "created_at": (now or datetime.now(timezone.utc)).isoformat(),
        "synthetic": bool(synthetic),
        "eligibility": eligibility,
        "code": {**code, "git": git_state(repo_root), "committed": committed},
        "environment": environment_,
        "strategy": strategy_section,
        "config": config, "config_sha256": digest(config),
        "assumptions": identity["assumptions"],
        "data": data, "funding": fund, "data_integrity": data_integrity or {}, "excluded_symbols": excluded_symbols or {},
        "data_policy": data_policy or {},
        "liquidation": {"kind": identity["liquidation_kind"], "file_sha256": liquidation_file_sha256},
        "protocol": {"document": PROTOCOL_DOC, "sha256": approval["protocol_sha256"], "approved": approval["approved"],
                     "approval_problems": approval["problems"], "segment": seg,
                     "freeze_sha256": (freeze_record or {}).get("freeze_sha256"), "freeze_valid": freeze_valid,
                     "freeze_problems": freeze_problems},
        "holdout": exposure,
        "arguments": _clean(arguments) if arguments is not None else None,
        "command_line": list(argv) if argv is not None else None,
        "outputs": {},
    }


# ------------------------------------------------------------------ outputs
def attach_outputs(manifest: dict, out_dir: str | Path) -> dict:
    """Hash the three required outputs. Raises ManifestIntegrityError if any is missing, or if the directory holds a file
    that is not an expected output or the manifest itself (sub-directories of segment runs are allowed)."""
    out = Path(out_dir)
    missing = [n for n in REQUIRED_OUTPUTS if not (out / n).is_file()]
    if missing:
        raise ManifestIntegrityError(f"required output file(s) missing: {', '.join(missing)}")
    extra = sorted(p.name for p in out.iterdir() if p.is_file() and p.name not in ALLOWED_FILES)
    if extra:
        raise ManifestIntegrityError(f"unexpected file(s) in the run directory: {', '.join(extra)}")
    manifest["outputs"] = {n: sha256_file(out / n) for n in REQUIRED_OUTPUTS}
    return manifest


def write_manifest(out_dir: str | Path, manifest: dict) -> Path:
    if sorted(manifest.get("outputs", {})) != sorted(REQUIRED_OUTPUTS):
        raise ManifestIntegrityError("the manifest must list exactly summary.json, trades.csv and equity.csv")
    return safeio.write_atomic(Path(out_dir) / MANIFEST_NAME, json.dumps(_clean(manifest), indent=2, sort_keys=True), overwrite=True)


def load_manifest(out_dir: str | Path) -> dict:
    return json.loads(safeio.read_bytes(Path(out_dir) / MANIFEST_NAME, 8 << 20).decode("utf-8"))


def verify_outputs(out_dir: str | Path) -> list[str]:
    """Problems with a run directory (empty list = every required file is present, listed and unchanged). Fails if a
    required file is missing from the manifest or from the directory, if the manifest lists an unexpected name, or if a
    file in the directory is not an expected output."""
    d = Path(out_dir)
    try:
        m = load_manifest(d)
    except (OSError, ValueError) as exc:
        return [f"the manifest cannot be read ({type(exc).__name__})"]
    listed = m.get("outputs") or {}
    problems = []
    for name in REQUIRED_OUTPUTS:
        if name not in listed:
            problems.append(f"{name} is not listed in the manifest")
        if not (d / name).is_file():
            problems.append(f"{name} is missing from the directory")
        elif name in listed and sha256_file(d / name) != listed[name]:
            problems.append(f"{name} has changed since the run")
    problems += [f"the manifest lists an unexpected output {n}" for n in sorted(set(listed) - set(REQUIRED_OUTPUTS))]
    problems += [f"unexpected file {p.name} in the run directory" for p in sorted(d.iterdir())
                 if p.is_file() and p.name not in ALLOWED_FILES]
    return problems


# The hashes that must all agree for a run to count as reproduced. A matching experiment_id alone is not enough.
REPRODUCTION_FIELDS = ("experiment_id", "code.sha256", "config_sha256", "strategy.params_sha256", "protocol.sha256",
                       "protocol.freeze_sha256", "liquidation.file_sha256")


def _dig(m: dict, dotted: str):
    for part in dotted.split("."):
        m = m.get(part) if isinstance(m, dict) else None
    return m


def compare_manifests(a: dict, b: dict) -> list[str]:
    """Differences between two manifests that prevent calling the second a reproduction of the first: the experiment id,
    code, configuration, parameter, protocol, freeze and liquidation hashes, every per-symbol candle hash, every funding
    hash, and every required output hash."""
    diffs = [f"{f} differs" for f in REPRODUCTION_FIELDS if _dig(a, f) != _dig(b, f)]
    for sect in ("data", "funding"):
        ha = {s: d["sha256"] for s, d in (a.get(sect) or {}).items()}
        hb = {s: d["sha256"] for s, d in (b.get(sect) or {}).items()}
        if ha != hb:
            diffs.append(f"{sect} hashes differ")
    for name in REQUIRED_OUTPUTS:
        if (a.get("outputs") or {}).get(name) != (b.get("outputs") or {}).get(name):
            diffs.append(f"output {name} differs")
    return diffs
