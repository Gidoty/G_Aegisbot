"""Protocol approval, freeze record and holdout-exposure rules for acceptance runs (docs/STRATEGY_ACCEPTANCE.md).

  approval record   docs/STRATEGY_ACCEPTANCE.approval.json: approval of ONE exact version of the protocol document (by its
                    SHA-256), SIGNED with an Ed25519 key that is listed in docs/STRATEGY_ACCEPTANCE.trusted_keys.json. Without
                    a record that decodes to an object, has every field valid, matches the document and carries a verified
                    signature, the protocol is a draft and no run may be an acceptance run. No trusted-keys file exists yet,
                    so today nothing can be approved (see docs/APPROVAL_SIGNING.md).
  freeze record     a JSON file fixed before any confirmation result is seen: protocol hash, source-code hash, candidates
                    with their parameter hashes, the universe, the periods, the statistical method and the cost profiles,
                    the liquidation-bracket requirement, and the user's declaration about the 2019-2021 period. It carries
                    its own hash. An acceptance run must match it exactly.
  exposure          whether a run's MEASURED period overlaps the confirmation period, decided from timestamps, never from
                    the name of a segment.

Nothing here approves, signs or freezes anything by itself, and no key is created or pinned here. `build_approval_payload` and
`build_freeze_record` build UNSIGNED records for a person to sign offline. Tests sign with throw-away in-memory keys inside
temporary directories only.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from g_aegisbot.backtest import safeio, signing
from g_aegisbot.backtest.manifest import PROTOCOL_DOC, REPO_ROOT, digest, sha256_file
from g_aegisbot.backtest.protected import ProtectedPathError, assert_not_protected

FREEZE_VERSION = 1
APPROVAL_DOC = "docs/STRATEGY_ACCEPTANCE.approval.json"
TRUSTED_KEYS_DOC = "docs/STRATEGY_ACCEPTANCE.trusted_keys.json"
MAX_RECORD_BYTES = 64 * 1024
APPROVAL_FIELDS = frozenset({"status", "version", "approved_by", "approved_at", "protocol_sha256", "signature"})

# Periods in the protocol (UTC, start inclusive, end exclusive). The confirmation period is the candidate independent
# historical confirmation; the other two are development and validation evidence with known contamination.
PERIODS = {
    "confirmation": ("2019-09-08", "2021-11-06"),
    "development": ("2021-11-06", "2024-05-07"),
    "validation": ("2024-05-07", "2026-11-06"),
}
CONFIRMATION = ("confirmation",)

BASE_COSTS = {"taker_fee": 0.0005, "slippage_scale": 1.0, "funding_mode": "historical", "breaker_on": True}
STRESS_COSTS = {"taker_fee": 0.0006, "slippage_scale": 2.0, "funding_mode": "historical", "breaker_on": True}

METHODOLOGY = {
    "confidence": 0.983, "block_length_reference": 10, "block_lengths_checked": [5, 10, 20, 30], "bootstrap_resamples": 10000,
    "bootstrap_seed": 1, "window_days": 90, "min_profitable_window_share": 0.60, "min_completed_trades": 300,
    "min_profit_factor": 1.30, "max_drawdown": 0.25, "max_drawdown_p95_bootstrap": 0.35, "funding_ratio_limit": 0.25,
    "max_pair_share": 0.40, "top_trade_fraction": 0.05, "min_regime_trades": 30,
}


def _day(text: str) -> pd.Timestamp:
    return pd.Timestamp(text, tz="UTC")


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


# ------------------------------------------------------------------ approval of the protocol document
_HEX64 = re.compile(r"[0-9a-f]{64}")


def _read_record(path: Path) -> bytes:
    """Bytes of a small regular file that is not a symlink and not a protected path. Raises ProtectedPathError or
    signing.SigningError; reads nothing before those checks pass."""
    assert_not_protected(path)
    if path.is_symlink() or not path.is_file():
        raise signing.SigningError("the record is not a regular file")
    if path.stat().st_size > MAX_RECORD_BYTES:
        raise signing.SigningError("the record is too large")
    return path.read_bytes()


def _text_field(data: dict, key: str, limit: int) -> str | None:
    v = data.get(key)
    if isinstance(v, str) and v.strip() and len(v) <= limit and all(c.isprintable() for c in v):
        return v
    return None


def validate_protocol_approval(repo_root: str | Path = REPO_ROOT, trusted_keys_path: str | Path | None = None,
                               now: datetime | None = None) -> dict:
    """{approved, problems, protocol_sha256, signed_by}. FAILS CLOSED: `approved` is True only when every step below
    succeeded explicitly; the absence of problems is not enough, and any unexpected error is a refusal.

      1. the protocol document exists, is a regular file and hashes;
      2. the approval record is a regular, small, non-protected file that decodes as UTF-8 and as strict JSON (no duplicate
         keys, no NaN) into an OBJECT (null, booleans, numbers, strings and arrays are refused);
      3. it has exactly the fields status, version, approved_by, approved_at, protocol_sha256, signature, each valid
         (status 'approved'; printable non-empty text; an ISO-8601 time with a zone; a lowercase SHA-256 equal to the
         document's current hash; a well-formed signature block);
      4. the Ed25519 signature verifies against a key pinned in the trusted-keys file (none is pinned today: refusal)."""
    root = Path(repo_root)
    problems: list[str] = []
    sha: str | None = None
    signed_by: str | None = None
    verified = False
    try:
        doc, rec = root / PROTOCOL_DOC, root / APPROVAL_DOC
        if doc.is_file() and not doc.is_symlink():
            sha = sha256_file(doc)
        else:
            problems.append("the protocol document is missing")
        if not rec.exists() and not rec.is_symlink():
            problems.append("there is no approval record: the protocol is still a draft")
        else:
            try:
                data = signing.strict_json(_read_record(rec))
            except (signing.SigningError, ProtectedPathError, OSError) as exc:
                data = None
                problems.append(f"the approval record cannot be used: {exc}")
            else:
                if not isinstance(data, dict):
                    problems.append("the approval record is not an object")
                else:
                    if set(data) != APPROVAL_FIELDS:
                        problems.append("the approval record has missing or unexpected fields")
                    if data.get("status") != "approved":
                        problems.append("the approval record does not say 'approved'")
                    if _text_field(data, "version", 64) is None:
                        problems.append("the approval record has no valid version")
                    if _text_field(data, "approved_by", 128) is None:
                        problems.append("the approval record has no valid approved_by")
                    at = data.get("approved_at")
                    try:
                        if not isinstance(at, str) or datetime.fromisoformat(at).tzinfo is None:
                            raise ValueError
                    except ValueError:
                        problems.append("the approval record has no valid approved_at (ISO-8601 with a time zone)")
                    claimed = data.get("protocol_sha256")
                    if not isinstance(claimed, str) or not _HEX64.fullmatch(claimed):
                        problems.append("the approval record has no valid protocol_sha256")
                    elif sha is not None and claimed != sha:
                        problems.append("the protocol document has changed since it was approved")
                    if not problems:
                        keys_path = Path(trusted_keys_path) if trusted_keys_path else root / TRUSTED_KEYS_DOC
                        try:
                            trusted = signing.load_trusted_keys(keys_path)
                        except (signing.SigningError, ProtectedPathError, OSError) as exc:
                            problems.append(f"the trusted-keys file cannot be used: {exc}")
                        else:
                            sig_problems = signing.verify_record(data, signing.PURPOSE_APPROVAL, trusted, at=now)
                            problems += sig_problems
                            if not sig_problems:
                                verified = True
                                signed_by = data["signature"]["key_id"]
    except Exception as exc:                                              # any surprise is a refusal, never an approval
        problems.append(f"the approval check failed safely ({type(exc).__name__})")
        verified = False
    approved = verified is True and not problems
    return {"approved": approved, "problems": problems, "protocol_sha256": sha, "signed_by": signed_by if approved else None}


def build_approval_payload(protocol_sha256: str, approved_by: str, version: str, approved_at: datetime) -> dict:
    """The UNSIGNED approval record, for an approver to sign offline with signing.sign_record. Creates no key, signs
    nothing and writes nothing."""
    if approved_at.tzinfo is None:
        raise ValueError("approved_at needs a time zone")
    return {"status": "approved", "version": version, "approved_by": approved_by, "approved_at": approved_at.isoformat(),
            "protocol_sha256": protocol_sha256}


# ------------------------------------------------------------------ freeze record
def build_freeze_record(*, protocol_sha256: str, source_sha256: str, candidates: dict, universe: list[str],
                        liquidation_file_sha256: str | None, declaration: dict, diversification_metric: str,
                        periods: dict | None = None, methodology: dict | None = None, costs: dict | None = None,
                        now: datetime | None = None) -> dict:
    """The record of what is frozen. `candidates` maps each candidate name to {"strategies": [...], "params_sha256": "..."}
    (breakout, trend, breakout+trend). `declaration` is the person's statement about the confirmation period (who, when,
    what), kept verbatim: it is a declaration, not independently verified proof."""
    rec = {
        "freeze_version": FREEZE_VERSION,
        "created_at": (now or datetime.now(timezone.utc)).isoformat(),
        "protocol_sha256": protocol_sha256,
        "source_sha256": source_sha256,
        "candidates": candidates,
        "universe": sorted(universe),
        "periods": periods or {k: list(v) for k, v in PERIODS.items()},
        "confirmation_period_names": list(CONFIRMATION),
        "methodology": methodology or dict(METHODOLOGY),
        "diversification_metric": diversification_metric,
        "costs": costs or {"base": dict(BASE_COSTS), "stress": dict(STRESS_COSTS)},
        "liquidation": {"required": "exact binance brackets", "file_sha256": liquidation_file_sha256},
        "declaration": declaration,
    }
    rec["freeze_sha256"] = digest({k: v for k, v in rec.items() if k != "freeze_sha256"})
    return rec


def write_freeze_record(path: str | Path, record: dict, *, overwrite: bool = False) -> Path:
    """Write a freeze record atomically. Refuses protected paths, links and non-regular targets, and by default never
    replaces an existing file: a freeze record that already exists is the frozen one. The record must be signable
    (finite numbers, text keys, no exotic types), which is checked before anything is written."""
    signing.canonical_bytes(record)
    text = json.dumps(record, indent=2, sort_keys=True, allow_nan=False) + "\n"
    return safeio.write_atomic(path, text, overwrite=overwrite)


def load_freeze_record(path: str | Path) -> dict:
    """Read a freeze record through the same guards as the approval record: not protected, not a link, a regular file
    of at most MAX_RECORD_BYTES, UTF-8, strict JSON (no duplicate keys, no NaN or Infinity), and a JSON object. Raises
    safeio.UnsafePathError, signing.SigningError or ProtectedPathError (all refusals); never returns a non-dict."""
    data = safeio.read_json(path, MAX_RECORD_BYTES)
    if not isinstance(data, dict):
        raise signing.SigningError("the freeze record is not a JSON object")
    return data


def _hex64(v) -> bool:
    return isinstance(v, str) and _HEX64.fullmatch(v) is not None


def _shape_problems(record: dict) -> list[str]:
    """Types of every field, checked before any field is used. Wrong types are refusals, never crashes or passes."""
    p: list[str] = []
    if record["freeze_version"] != FREEZE_VERSION or isinstance(record["freeze_version"], bool):
        p.append("unsupported freeze_version")
    if not isinstance(record["created_at"], str):
        p.append("created_at is not text")
    for k in ("protocol_sha256", "source_sha256", "freeze_sha256"):
        if not _hex64(record[k]):
            p.append(f"{k} is not a lowercase SHA-256")
    cands = record["candidates"]
    if not isinstance(cands, dict) or not cands:
        p.append("candidates must be a non-empty object")
    else:
        for name, c in cands.items():
            if (not isinstance(c, dict) or not isinstance(c.get("strategies"), list) or not c["strategies"]
                    or not all(isinstance(x, str) for x in c["strategies"]) or not _hex64(c.get("params_sha256"))):
                p.append(f"candidate {name!r} is malformed")
    uni = record["universe"]
    if not isinstance(uni, list) or not uni or not all(isinstance(x, str) and x for x in uni) or len(set(uni)) != len(uni):
        p.append("universe must be a non-empty list of unique symbols")
    periods = record["periods"]
    if not isinstance(periods, dict) or not periods:
        p.append("periods must be a non-empty object")
    else:
        for name, pair in periods.items():
            try:
                a, b = pair
                if not (isinstance(a, str) and isinstance(b, str) and _day(a) < _day(b)):
                    raise ValueError
            except (TypeError, ValueError):
                p.append(f"period {name!r} must be [start, end] dates with start before end")
    names = record["confirmation_period_names"]
    if not isinstance(names, list) or not names or not all(isinstance(x, str) for x in names) \
            or (isinstance(periods, dict) and not all(x in periods for x in names)):
        p.append("confirmation_period_names must name existing periods")
    for k in ("methodology", "liquidation", "declaration"):
        if not isinstance(record[k], dict):
            p.append(f"{k} must be an object")
    costs = record["costs"]
    if not isinstance(costs, dict) or not isinstance(costs.get("base"), dict) or not isinstance(costs.get("stress"), dict):
        p.append("costs must have base and stress profiles")
    if not isinstance(record["diversification_metric"], str):
        p.append("diversification_metric is not text")
    return p


def trusted_keys_for_repo(repo_root: str | Path = REPO_ROOT):
    root = Path(repo_root)
    return signing.load_trusted_keys(root / TRUSTED_KEYS_DOC)


def freeze_signature_problems(record, repo_root: str | Path = REPO_ROOT, now: datetime | None = None) -> list[str]:
    """Why a freeze record does not count as signed by a pinned trusted key (empty = verified). Separate from
    validate_freeze_record, which checks structure, hash and match; a freeze record is only valid when BOTH are empty."""
    try:
        return signing.verify_record(record, signing.PURPOSE_FREEZE, trusted_keys_for_repo(repo_root), at=now)
    except (signing.SigningError, ProtectedPathError, OSError) as exc:
        return [f"the trusted-keys file cannot be used: {exc}"]
    except Exception as exc:
        return [f"the signature check failed safely ({type(exc).__name__})"]


_REQUIRED = ("freeze_version", "created_at", "protocol_sha256", "source_sha256", "candidates", "universe", "periods",
             "confirmation_period_names", "methodology", "diversification_metric", "costs", "liquidation", "declaration",
             "freeze_sha256")


def freeze_problems(record, run: dict | None = None, repo_root: str | Path = REPO_ROOT, now: datetime | None = None) -> list[str]:
    """THE check for "this freeze record counts": structure, hash and (with `run`) match, AND the signature against the
    pinned trusted key. Empty list = valid. Use this, not validate_freeze_record alone, wherever a freeze record decides
    anything: validate_freeze_record does not look at the signature."""
    return [*validate_freeze_record(record, run), *freeze_signature_problems(record, repo_root, now)]


def validate_freeze_record(record, run: dict | None = None) -> list[str]:
    """Problems with a freeze record (empty list = valid). Always checks its structure and its own hash. If `run` is given,
    also that the run matches the record: run = {protocol_sha256, source_sha256, candidate, strategies, params_sha256,
    symbols, cost_profile_values {taker_fee, slippage_scale, funding_mode, breaker_on}, liquidation_file_sha256, measured
    {start, end}}."""
    if not isinstance(record, dict):
        return ["the freeze record is not an object"]
    problems = [f"the freeze record has no {k}" for k in _REQUIRED if k not in record]
    if problems:
        return problems
    shape = _shape_problems(record)
    if shape:
        return shape                                  # nothing below may index into a field of the wrong type
    if digest({k: v for k, v in record.items() if k not in ("freeze_sha256", "signature")}) != record["freeze_sha256"]:
        problems.append("the freeze record's own hash does not match its content (it was edited after freezing)")
    decl = record["declaration"]
    if not isinstance(decl, dict) or not all(isinstance(decl.get(k), str) and decl[k].strip() for k in ("by", "date", "statement")):
        problems.append("the declaration about the confirmation period needs 'by', 'date' and 'statement'")
    if record["diversification_metric"] not in ("sharpe", "calmar", "drawdown_reduction"):
        problems.append("the diversification metric must be one of sharpe, calmar, drawdown_reduction")
    if not record["liquidation"].get("file_sha256"):
        problems.append("the record names no liquidation-bracket file hash (exact Binance brackets are required)")
    if run is None:
        return problems

    if record["protocol_sha256"] != run.get("protocol_sha256"):
        problems.append("the protocol document differs from the one frozen")
    if record["source_sha256"] != run.get("source_sha256"):
        problems.append("the result-affecting source code differs from the code frozen")
    cand = record["candidates"].get(run.get("candidate"))
    if cand is None:
        problems.append(f"candidate {run.get('candidate')!r} is not in the freeze record")
    else:
        if list(cand.get("strategies", [])) != list(run.get("strategies", [])):
            problems.append("the strategy set differs from the frozen candidate")
        if cand.get("params_sha256") != run.get("params_sha256"):
            problems.append("the strategy parameters differ from the frozen ones")
    if sorted(run.get("symbols", [])) != sorted(record["universe"]):
        problems.append("the symbol universe differs from the frozen universe")
    values = run.get("cost_profile_values")
    if values not in (record["costs"].get("base"), record["costs"].get("stress")):
        problems.append("the fee, slippage, funding or breaker settings match neither the frozen base nor the frozen stress profile")
    if run.get("liquidation_file_sha256") != record["liquidation"].get("file_sha256"):
        problems.append("the liquidation brackets differ from the frozen file")
    m = run.get("measured") or {}
    try:
        s, e = _ts(m["start"]), _ts(m["end"])
        inside = any(_day(a) <= s and e <= _day(b) for a, b in record["periods"].values())
        if not inside:
            problems.append("the measured period is not inside any frozen period")
    except (KeyError, ValueError, TypeError):
        problems.append("the run has no readable measured period")
    return problems


# ------------------------------------------------------------------ holdout exposure
def confirmation_periods(record: dict | None = None) -> list[tuple[str, pd.Timestamp, pd.Timestamp]]:
    """The frozen confirmation periods: from the freeze record if one is given, else the protocol's built-in period."""
    periods = (record or {}).get("periods") or {k: list(v) for k, v in PERIODS.items()}
    names = (record or {}).get("confirmation_period_names") or list(CONFIRMATION)
    return [(n, _day(periods[n][0]), _day(periods[n][1])) for n in names if n in periods]


def holdout_exposure(measured_start, measured_end, record: dict | None = None, data_first=None, data_last=None) -> dict:
    """Does the MEASURED period [start, end) overlap a confirmation period? Decided from the actual timestamps the run
    evaluated, not from any segment name or command-line option. `overlaps` lists each overlap with its start, end and
    number of days. `data_overlap` (informational) says whether any candle that was read, warm-up included, lies inside a
    confirmation period; warm-up candles never produce trades but are part of what the run saw."""
    s, e = _ts(measured_start), _ts(measured_end)
    overlaps = []
    for name, a, b in confirmation_periods(record):
        lo, hi = max(s, a), min(e, b)
        if lo < hi:
            overlaps.append({"period": name, "start": str(lo), "end": str(hi), "days": (hi - lo).days})
    data_overlap = False
    if data_first is not None and data_last is not None:
        df_, dl = _ts(data_first), _ts(data_last)
        data_overlap = any(max(df_, a) <= min(dl, b) for _, a, b in confirmation_periods(record))
    return {"measured": {"start": str(s), "end": str(e)}, "exposed": bool(overlaps), "overlaps": overlaps,
            "data_overlap": data_overlap}
