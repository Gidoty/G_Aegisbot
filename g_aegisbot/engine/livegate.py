"""The live-trading gate. Real money can be traded only while EVERY condition below holds; anything missing, unreadable,
expired or different is a refusal. The gate never starts, stops or closes anything: it only answers "may a NEW live entry open
now?". Open positions keep being managed (stops, trailing, exits) whatever the answer.

Conditions
  G1 switch      AEGIS_LIVE_ENABLED=1 in the environment of the process (an operator decision, separate from the code).
  G2 protocol    the strategy-acceptance protocol is approved: signed approval record, valid signature, hash equals the document.
  G3 record      docs/LIVE_AUTHORIZATION.json exists, is strict JSON with exactly the expected fields, is signed by a pinned
                 trusted key for the purpose "live_authorization", has not expired and is valid for at most 90 days.
  G4 strategy    the record names the exact strategy parameters this process runs (SHA-256 of StrategyParams) and the same
                 protocol document the approval covers; it states an acceptance outcome PASS with the freeze-record and
                 report hashes, and at least 28 days of paper soak.
  G5 code        the code this process runs (import closure of the runner) is exactly the code the record names, and it is
                 byte-for-byte a committed version (git HEAD).
  G6 risk        every risk limit of this process is at least as strict as the limit the record authorises (a limit set to 0,
                 meaning "off", is never stricter), and the daily drawdown limit is no looser.
  G7 account     (per profile) the wallet cap and leverage do not exceed what the record authorises, and the API keys passed
                 the Binance permission check recently (services.keys_verification_problem).
  G8 runner      the single-runner lock is held.
There is no override, no "force" and no environment variable that skips a condition. Changing anything the record covers
(strategy parameters, code, limits) invalidates it until it is re-authorised and re-signed by the owner's key.
Creating the record is a human decision made on the owner's device with `python -m g_aegisbot.backtest.signer` (see
docs/APPROVAL_SIGNING.md); nothing in this repository creates or signs one.
"""
from __future__ import annotations

import functools
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from g_aegisbot.backtest import freeze as fz
from g_aegisbot.backtest import manifest as mf
from g_aegisbot.backtest import safeio, signing
from g_aegisbot.backtest import source_closure as sc
from g_aegisbot.config import DAILY_DRAWDOWN_LIMIT, MAX_LEVERAGE, MIN_LEVERAGE
from g_aegisbot.engine import strategies as strat
from g_aegisbot.engine.strategy import StrategyParams
from g_aegisbot.services import keys_verification_problem

LIVE_AUTH_DOC = "docs/LIVE_AUTHORIZATION.json"
LIVE_ENV = "AEGIS_LIVE_ENABLED"
MAX_VALIDITY = timedelta(days=90)
MIN_PAPER_SOAK_DAYS = 28
LIVE_ENTRY_POINTS = ("engine/runner.py", "backtest/simulator.py")
LIVE_EXTERNAL = sc.DECLARED_EXTERNAL | frozenset({"sqlalchemy", "alembic"})
RECORD_FIELDS = frozenset({"status", "version", "authorised_by", "authorised_at", "valid_until", "candidate", "strategies",
                           "params_sha256", "protocol_sha256", "freeze_sha256", "acceptance_outcome", "acceptance_report_sha256",
                           "paper_soak_days", "code_sha256", "max_wallet_cap_usdt", "max_leverage", "risk_limits", "signature"})
RISK_FIELDS = ("max_risk_pct", "max_open_positions", "max_same_direction", "max_aggregate_risk_pct", "max_gross_notional_x",
               "daily_drawdown_limit")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_NO_RECORD = object()


@dataclass
class GateResult:
    allowed: bool
    problems: list[str] = field(default_factory=list)
    checks: dict[str, bool] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.allowed


# ------------------------------------------------------------------------------------------------ identities of this process
def configured_params(env=None) -> StrategyParams:
    """The strategy parameters this deployment runs, from AEGIS_STRATEGIES (default breakout). The runner, the admin CLI and
    the API must see the same environment, or the gate (correctly) refuses."""
    env = os.environ if env is None else env
    wanted = tuple(x.strip() for x in (env.get("AEGIS_STRATEGIES") or "breakout").split(",") if x.strip())
    return StrategyParams(strategies=strat.validate_strategies(wanted))


def risk_snapshot(params: StrategyParams, drawdown_limit: float = DAILY_DRAWDOWN_LIMIT) -> dict:
    return {"max_risk_pct": params.max_risk_pct, "max_open_positions": params.max_open_positions,
            "max_same_direction": params.max_same_direction, "max_aggregate_risk_pct": params.max_aggregate_risk_pct,
            "max_gross_notional_x": params.max_gross_notional_x, "daily_drawdown_limit": drawdown_limit}


@functools.lru_cache(maxsize=4)
def _live_code(package_dir: str, repo_root: str) -> tuple[str, bool]:
    """(hash of the runner's import closure, True if every file of it equals HEAD). Computed once per process: the code
    that is loaded in memory is the code that was on disk when this was first asked."""
    c = sc.closure(package_dir, LIVE_ENTRY_POINTS, declared_external=LIVE_EXTERNAL)
    committed = mf.code_matches_commit(sorted(c["files"]), repo_root, package_dir)
    return mf.digest(c["files"]), bool(committed["matches"])


def live_code_identity(repo_root: Path = mf.REPO_ROOT, package_dir: Path = mf.PACKAGE_DIR) -> tuple[str, bool]:
    return _live_code(str(package_dir), str(repo_root))


# ------------------------------------------------------------------------------------------------ the record
def record_shape_problems(rec) -> list[str]:
    """Structure of an UNSIGNED or signed record (the signature block itself is checked by signing.verify_record)."""
    if not isinstance(rec, dict):
        return ["the record is not an object"]
    if set(rec) != RECORD_FIELDS:
        return ["the record has missing or unexpected fields"]
    p: list[str] = []
    if rec["status"] != "authorised":
        p.append("status is not 'authorised'")
    for k in ("version", "authorised_by", "candidate"):
        if not (isinstance(rec[k], str) and rec[k].strip() and len(rec[k]) <= 128 and all(c.isprintable() for c in rec[k])):
            p.append(f"{k} is not valid text")
    for k in ("params_sha256", "protocol_sha256", "freeze_sha256", "acceptance_report_sha256", "code_sha256"):
        if not (isinstance(rec[k], str) and _HEX64.fullmatch(rec[k])):
            p.append(f"{k} is not a lowercase SHA-256")
    if not (isinstance(rec["strategies"], list) and rec["strategies"] and all(x in strat.ALL_STRATEGIES for x in rec["strategies"])):
        p.append("strategies must be a non-empty list of known strategies")
    if rec["acceptance_outcome"] != "PASS":
        p.append("acceptance_outcome is not PASS")
    soak = rec["paper_soak_days"]
    if isinstance(soak, bool) or not isinstance(soak, int) or soak < MIN_PAPER_SOAK_DAYS:
        p.append(f"paper_soak_days must be an integer of at least {MIN_PAPER_SOAK_DAYS}")
    cap = rec["max_wallet_cap_usdt"]
    if isinstance(cap, bool) or not isinstance(cap, (int, float)) or not math.isfinite(cap) or cap <= 0:
        p.append("max_wallet_cap_usdt must be a positive number")
    lev = rec["max_leverage"]
    if isinstance(lev, bool) or not isinstance(lev, int) or not MIN_LEVERAGE <= lev <= MAX_LEVERAGE:
        p.append(f"max_leverage must be an integer from {MIN_LEVERAGE} to {MAX_LEVERAGE}")
    rl = rec["risk_limits"]
    if not (isinstance(rl, dict) and set(rl) == set(RISK_FIELDS)
            and all(not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v) and v >= 0 for v in rl.values())):
        p.append("risk_limits must hold exactly " + ", ".join(RISK_FIELDS) + " as non-negative numbers")
    for k in ("authorised_at", "valid_until"):
        try:
            if datetime.fromisoformat(rec[k]).tzinfo is None:
                raise ValueError
        except (TypeError, ValueError):
            p.append(f"{k} is not an ISO-8601 time with a time zone")
    return p


def build_payload(*, params: StrategyParams, drawdown_limit: float, candidate: str, version: str, authorised_by: str,
                  authorised_at: datetime, valid_days: int, freeze_sha256: str, acceptance_report_sha256: str,
                  paper_soak_days: int, max_wallet_cap_usdt: float, max_leverage: int, protocol_sha256: str,
                  code_sha256: str) -> dict:
    """The UNSIGNED record, for the owner to review and sign offline. Creates no key and signs nothing."""
    if valid_days > MAX_VALIDITY.days:
        raise ValueError(f"a live authorisation is valid for at most {MAX_VALIDITY.days} days")
    return {"status": "authorised", "version": version, "authorised_by": authorised_by,
            "authorised_at": authorised_at.isoformat(), "valid_until": (authorised_at + timedelta(days=valid_days)).isoformat(),
            "candidate": candidate, "strategies": list(params.strategies), "params_sha256": mf.digest(params),
            "protocol_sha256": protocol_sha256, "freeze_sha256": freeze_sha256, "acceptance_outcome": "PASS",
            "acceptance_report_sha256": acceptance_report_sha256, "paper_soak_days": paper_soak_days, "code_sha256": code_sha256,
            "max_wallet_cap_usdt": max_wallet_cap_usdt, "max_leverage": max_leverage,
            "risk_limits": risk_snapshot(params, drawdown_limit)}


# ------------------------------------------------------------------------------------------------ the gate
def _stricter_or_equal(actual: float, authorised: float) -> bool:
    """A limit of 0 means "off", which is the loosest possible; it is acceptable only if the record also says 0."""
    if authorised == 0:
        return True
    return actual != 0 and actual <= authorised


def evaluate(*, params: StrategyParams, drawdown_limit: float = DAILY_DRAWDOWN_LIMIT, profile=None, lock_held: bool = True,
             repo_root: Path = mf.REPO_ROOT, now: datetime | None = None, env=None, code_identity=None,
             check_switch: bool = True) -> GateResult:
    """May a NEW live entry open? Never raises: any error is a refusal. `check_switch=False` is for the admin CLI and the API,
    which decide whether a PROFILE may be switched to live; the runner's AEGIS_LIVE_ENABLED switch is enforced in the runner."""
    now = now or datetime.now(timezone.utc)
    env = os.environ if env is None else env
    checks: dict[str, bool] = {}
    problems: list[str] = []

    def check(name: str, ok: bool, why: str) -> bool:
        checks[name] = bool(ok)
        if not ok:
            problems.append(f"{name}: {why}")
        return bool(ok)

    try:
        if check_switch:
            check("G1 switch", env.get(LIVE_ENV) == "1", f"{LIVE_ENV} is not set to 1 in this process")
        check("G8 runner", lock_held, "the single-runner lock is not held")

        approval = fz.validate_protocol_approval(repo_root, now=now)
        check("G2 protocol", approval["approved"], "the acceptance protocol is not approved ("
              + ("; ".join(approval["problems"]) or "no reason given") + ")")

        rec = _NO_RECORD
        path = Path(repo_root) / LIVE_AUTH_DOC
        if check("G3 record", path.exists(), f"{LIVE_AUTH_DOC} does not exist"):
            try:
                rec = safeio.read_json(path, fz.MAX_RECORD_BYTES)
            except Exception as exc:
                check("G3 record", False, f"the record cannot be read ({type(exc).__name__})")
        if rec is not _NO_RECORD:
            shape = record_shape_problems(rec)
            if shape:
                check("G3 record", False, "; ".join(shape))
            else:
                try:
                    trusted = fz.trusted_keys_for_repo(repo_root)
                    sig = signing.verify_record(rec, signing.PURPOSE_LIVE, trusted, at=now)
                except Exception as exc:
                    sig = [f"the trusted keys cannot be used ({type(exc).__name__})"]
                if sig:
                    check("G3 record", False, "; ".join(sig))
                else:
                    start, end = datetime.fromisoformat(rec["authorised_at"]), datetime.fromisoformat(rec["valid_until"])
                    if end - start > MAX_VALIDITY:
                        check("G3 record", False, f"valid for more than {MAX_VALIDITY.days} days")
                    elif not start <= now < end:
                        check("G3 record", False, "not in its validity window (expired, or not yet valid)")
                    else:
                        checks["G3 record"] = True
            ok_record = checks.get("G3 record", False)
            if ok_record:
                check("G4 strategy", mf.digest(params) == rec["params_sha256"] and list(params.strategies) == rec["strategies"],
                      "the strategy parameters of this process are not the ones authorised")
                check("G4 protocol", approval["protocol_sha256"] == rec["protocol_sha256"],
                      "the authorisation names a different protocol document than the approved one")
                code_sha, committed = (code_identity or (lambda: live_code_identity(repo_root)))()
                check("G5 code", code_sha == rec["code_sha256"] and committed,
                      "the code this process runs is not the authorised, committed code")
                limits = rec["risk_limits"]
                actual = risk_snapshot(params, drawdown_limit)
                loose = [k for k in RISK_FIELDS if not _stricter_or_equal(actual[k], limits[k])]
                check("G6 risk", not loose, "limits looser than authorised: " + ", ".join(loose))
                if profile is not None:
                    cap = float(profile.max_wallet_cap_usdt or 0)
                    lev = int(getattr(profile, "leverage", None) or params.leverage)
                    check("G7 account", 0 < cap <= rec["max_wallet_cap_usdt"] and lev <= rec["max_leverage"],
                          f"wallet cap {cap:g} / leverage {lev}x exceed the authorised {rec['max_wallet_cap_usdt']:g} / {rec['max_leverage']}x")
                    keys = keys_verification_problem(profile, now)
                    check("G7 keys", keys is None, keys or "")
        if profile is None:
            checks.setdefault("G7 account", True)         # per-profile checks run when a profile is given
    except Exception as exc:                               # a surprise is a refusal, never a pass
        problems.append(f"gate: the check failed safely ({type(exc).__name__})")
        checks["gate"] = False
    allowed = not problems and bool(checks) and all(checks.values())
    return GateResult(allowed, problems, checks)
