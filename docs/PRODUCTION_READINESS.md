# Production readiness (as of the commit that contains this file)

**Verdict: not ready for real money, and nothing here claims otherwise.** The software is engineered and tested to run in paper
mode and to refuse live trading by default. Live trading is blocked by two things the code cannot supply: no strategy has passed
the registered acceptance criteria, and nothing has yet been exercised against a real exchange environment.

Evidence: backend `pytest` 1755 passed, 0 failed, 0 skipped (one third-party deprecation warning from `starlette.testclient`);
dashboard `tsc --noEmit` clean and `vitest` 6 passed. All of it runs offline: no test touches Binance.

## 1. Component checklist
Legend: **Done** = implemented and tested offline. **Unverified** = done, but never run against a real Binance environment.
**Open** = work or an owner action remains. **Fail** = tested and failed.

| Area | Component | Status | Notes |
|---|---|---|---|
| Exchange | ccxt adapter (markets, candles, orders, stops, positions, balances), ccxt pinned 4.5.92, lock file | Done / Unverified | Mocked-HTTP tests with the real ccxt classes. Test environment is now Binance Demo Trading by default (`AEGIS_TESTNET_ENDPOINT`), also unverified. |
| Exchange | API key permission check (withdrawals off, 7-day freshness) | Done / Unverified | Real Binance answer format assumed from documentation. |
| Execution | Client order ids, write-ahead journal, uncertain-order handling, evidence-based release, operator resolve | Done / Unverified | Crash and timeout cases tested with fakes. |
| Execution | Stop placement and visibility confirmation, emergency close if no stop | Done / Unverified | |
| Execution | Entry price guard (price already through the stop, or chased beyond 0.5 ATR) | Done | New; same rule in the backtester. |
| Execution | Closed-candle handling and stale-data refusal | Done | New; needs an accurate clock (NTP, confirmed synchronised on the VPS). |
| Position mgmt | Trailing stops, strategy exits, restart reconciliation, safe close, adopt unmanaged positions | Done / Unverified | |
| Risk | Per-trade 1.5%, 4 positions, 3 per side, 4% aggregate, 2x gross, 4% daily breaker, kill switch, lapsed-subscription manage-only | Done | Limits unchanged. |
| Safety gate | Live-trading gate G1 to G8 (`docs/LIVE_GATE.md`), enforced in runner, CLI and API | Done | New; closed everywhere today. |
| Security | Encrypted keys at rest, JWT auth, identity binding, rate limiter, CORS, billing signature check | Done | No independent penetration test; dependency vulnerability scan not run (needs network). |
| Security | Signing infrastructure and offline signer; no key exists | Done / Open | The owner must create and pin the key. |
| Security | Guarded file access, protected paths, source-closure code identity, committed-code check | Done | |
| Monitoring | Runner heartbeat, `/api/health`, dashboard runner status | Done | |
| Monitoring | Alerts for CRITICAL events to webhook or Telegram (`AEGIS_ALERT_*`) | Done / Open | New; the owner must configure a destination and test it. An external uptime monitor on `/api/health` is not set up. |
| Logging | Journal logging with levels; secrets never logged | Done | Plain text via journald; no central log store. |
| Recovery | Operator runbook, restart recovery, single-runner lock | Done / Unverified | Not practised on a real environment. |
| Data | Nightly verified backup tool and systemd timer | Done / Open | New; restore drill not done; Postgres path (`pg_dump`) not run. |
| Data | Migrations; SQLite path | Done | |
| Data | PostgreSQL / Supabase end to end | Open | Offline DDL checks only. A staging database is needed. |
| Build | CI workflow (pytest, typecheck, dashboard tests) | Done / Open | `.github/workflows/ci.yml` added; first run on GitHub not seen. |
| Build | Reproducible install (`requirements.lock`, Docker uses it) | Done | |
| Deploy | systemd units (use `/opt/g-aegisbot/venv`), Docker, Caddy | Done / Unverified | The VPS holds the code at the pre-registration commit; services inactive and disabled. |
| Dashboard | Pages, API client, formatting tests | Done | Only 6 unit tests; no browser end-to-end test. |
| Billing | NOWPayments invoices and IPN signature | Done / Open | Never tested against the NOWPayments sandbox. |
| Strategy | Breakout (1h, 4h, 1d) and trend (1h, 4h, 1d) | **Fail** | Rejected by the registered criteria (`docs/RESEARCH_LOG.md`). |
| Strategy | A strategy that passes acceptance | **Open** | None exists. Proposal: `docs/NEXT_STRATEGY_PROPOSAL.md` (not pre-registered). |
| Process | Paper soak of at least 28 days with an accepted strategy | Open | Cannot start until a strategy passes. |

## 2. What I could not do from here (blockers outside the sandbox)
* Binance is unreachable from the build sandbox (HTTP 451), so nothing was run against Demo Trading, the self-test, or live data.
* I cannot SSH to the VPS; VPS facts come from the owner's pasted output.
* Creating and pinning the signing key, branch protection, alert destinations, uptime monitoring, backups restore drill and the
  NOWPayments sandbox test are owner actions.

## 3. Exact requirements to authorise the first live trade
All must be true, in this order. Skipping any is a refusal by the gate or a violation of the owner's own rules.
1. **A strategy passes the registered acceptance protocol**: development screen, validation, signed approval, signed freeze record,
   one confirmation run judged by section 7 unchanged (including 300 completed trades and the bootstrap bounds). Today: none.
2. **Signing key created on the owner's device**, public key committed by the owner, branch protection on the trusted-keys,
   approval, freeze and live-authorisation files (`docs/APPROVAL_SIGNING.md`).
3. **Protocol approved** (`sign-approval`) and the freeze record signed (`sign-freeze`).
4. **Exchange verification on Binance Demo Trading** with the owner's demo keys, from the VPS: the self-test, then every item of
   `docs/TESTNET_CHECKLIST.md` (stop visibility and cancel route, restart with an open position, network cut during an entry,
   kill switch, daily breaker, two runners on one database). Results recorded with dates.
5. **PostgreSQL / Supabase staging check** if Supabase is the production database: migrations, advisory lock on the direct or
   session-pooler address, restore from a backup.
6. **Paper soak of at least 28 days** with the accepted strategy and the exact production configuration; no CRITICAL events left
   unexplained; paper results consistent with the backtest within the tolerance fixed in advance.
7. **Operations ready**: alert destination tested, uptime monitor on `/api/health`, nightly backup running and one restore tested,
   NTP synchronised, the runner unit and `.env` reviewed by the owner.
8. **Keys**: a dedicated live API key with withdrawals off, IP-restricted to the VPS, verified with `set-keys --verify` within 7 days.
9. **Live authorisation signed** (`live-template`, `sign-live`) for the exact strategy parameters, committed code, risk limits, wallet cap
   (start small) and leverage, valid for at most 90 days.
10. **Separate explicit written authorisation from the owner** to switch on `AEGIS_LIVE_ENABLED=1` and `set-mode live`. Then
    `selftest --mainnet --yes-real-money --notional 120`, then `runner --dry-run --once`, then the runner on one account with a small cap.

## 4. Known limits that remain whatever the tests say
Exchange behaviour (status names, order lookups, algo-order endpoints) is based on ccxt's mapping and documentation until it is
observed on Demo Trading. The backtester is not an exact Binance simulation (flat slippage tiers, flat maintenance margin unless
bracket files are supplied, last-price stops). A deposit or withdrawal during the day can shift the daily-loss tracker. Futures
trading can lose more than the amount deposited in adverse conditions; the gate and the acceptance protocol reduce the chance of
trading an unproven strategy, they do not make a strategy profitable.
