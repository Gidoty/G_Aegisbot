# G-Aegisbot

Automated Binance USDT-M futures trading for one operator and up to ten friends and family, with a
paper-trading mode, a kill switch, and a web dashboard. It is built for private use.

> **Status: not proven with real money.** All logic is covered by offline tests, but the strategy has not
> been shown to be profitable on real market data, and the live order paths have not yet been exercised
> against Binance. Follow the go-live sequence below, in order, and start small.

## How it works

```
Binance  <──────────────►  runner  ◄───────────►  database  ◄───────────►  API  ◄──────►  dashboard
 (orders, candles)       (trades, stops,        (SQLite for dev,        (FastAPI,        (Next.js on
                          breaker, safety)       Postgres/Supabase       Supabase login,   Vercel)
                                                 in production)          billing)
```

* **runner** (`python -m g_aegisbot.engine.runner`): the only process that trades. After each closed 1h
  candle it scans 15 pairs, then for each account checks the daily loss limit, repairs unprotected
  positions, trails stops, and opens new trades. Every minute it re-checks the loss limit, the kill switch
  and stop-loss orders.
* **api** (`uvicorn --factory g_aegisbot.api.app:create_app_from_env`): serves the dashboard. It never
  trades and never holds exchange connections.
* **admin CLI** (`python -m g_aegisbot.admin`): create users, store API keys, set limits, kill switch.
* **backtester** (`python -m g_aegisbot.backtest`): replays the same rules over real history.
* **dashboard** (`dashboard/`): a Next.js and Tailwind site, deployed separately on Vercel.

## The strategy and its limits

Long when a candle closes above the prior 20-candle high, short below the prior 20-candle low, but only
if all of these hold: close above EMA50 above EMA200 (reverse for shorts); RSI between 50 and 70 (30 and
50 for shorts); volume at least 1.5 times the average of the prior 20 candles. The stop starts 2 ATR away
and trails, moving only in the trade's favour. The stop lives on the exchange, so it survives a crash.

| Rule | Value |
|---|---|
| Margin per trade | 5-7% of allocated capital, or 12-15% for high-conviction setups |
| Leverage | 1x to 5x per account, default 3x |
| Max open positions | 4 per account |
| Max loss at the stop | 1.5% of allocated capital (the size is cut to fit) |
| Daily loss limit | 4% of equity in a UTC day stops new entries until 00:00 UTC |
| Allocated capital | the smaller of the account's wallet cap and its equity |

These numbers are starting points. Nothing here has been optimised or validated on real data.

## Setup (development)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q                                  # all tests are offline
cp .env.example .env                                 # then edit it
python -m g_aegisbot.admin gen-key                   # paste the result into AEGIS_MASTER_KEYS
python -m g_aegisbot.admin create-user you@example.com --admin
```

`.env` holds secrets. Never commit it. Keep `AEGIS_MASTER_KEYS` somewhere safe: without it the stored
Binance keys cannot be decrypted.

## Binance account requirements

Use a **dedicated** Binance account or sub-account for the bot. The bot manages (and, under the kill
switch, closes) positions in the pairs it trades, so manual trades there would be adopted.

1. Futures account in **One-way** position mode (the runner refuses Hedge mode).
2. Create an API key with **Enable Reading** and **Enable Futures** only. **Never enable withdrawals.**
   The admin CLI (`set-keys --verify`) and the API refuse a key that can withdraw.
3. Restrict the key to your server's IP address.
4. Fund the account. The wallet cap you set (`set-cap`) limits how much of it the bot may use.

## Production on a VPS

```bash
# as root: a service user and the code
useradd --system --create-home --shell /usr/sbin/nologin aegis
git clone <your repo> /opt/g-aegisbot && cd /opt/g-aegisbot && git checkout claude/awesome-volta-efmbt4
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env && chown aegis:aegis .env   # then fill it in
mkdir -p data && chown -R aegis:aegis data

# schema: Postgres/Supabase uses migrations (SQLite creates tables itself)
sudo -u aegis .venv/bin/python -m g_aegisbot.admin migrate

# services (templates in deploy/): review them, then
cp deploy/systemd/*.service /etc/systemd/system/ && systemctl daemon-reload
systemctl enable --now g-aegisbot-runner g-aegisbot-api
```

Put HTTPS in front of the API (`deploy/Caddyfile`) and set `AEGIS_CORS_ORIGINS` to the dashboard URL.
Docker is an alternative: `deploy/Dockerfile` and `deploy/docker-compose.yml`.

Supabase: use the project's Postgres connection string as `AEGIS_DATABASE_URL`, and `SUPABASE_URL` so the
API can verify logins. Turn off public sign-ups in Supabase Auth and invite people instead. A login can
use the app only if a profile exists whose `user_id` is that login's email.

## Go-live sequence (do not skip steps)

1. **Check Binance is reachable** from the server: `curl -s -o /dev/null -w '%{http_code}\n' https://fapi.binance.com/fapi/v1/ping` should print `200`.
2. **Self-test on the Binance testnet.** Create testnet API keys, then:
   ```bash
   export AEGIS_SELFTEST_API_KEY=... AEGIS_SELFTEST_API_SECRET=...
   python -m g_aegisbot.tools.selftest
   ```
   Every step must pass. It proves entries, exchange-side stops, stop discovery and moving them work.
3. **Backtest on real history:** `python -m g_aegisbot.backtest --years 2`, then again with
   `--slippage-scale 2`. Read the assumptions block. If the result does not survive doubled slippage, stop.
4. **Paper trade for 2 to 4 weeks:** accounts in `testnet` mode use the built-in paper ledger on live
   prices. Watch the logs, the daily loss limit and the trailing stops.
5. **Tiny real trade:** `python -m g_aegisbot.tools.selftest --mainnet --yes-real-money --notional 120`.
6. **Live, your own account only,** with a small cap:
   ```bash
   python -m g_aegisbot.admin set-keys you@example.com --verify
   python -m g_aegisbot.admin set-cap you@example.com 200
   python -m g_aegisbot.admin set-mode you@example.com live --yes-real-money
   python -m g_aegisbot.engine.runner --dry-run --once      # logs what it would do, places nothing
   ```
7. Add other accounts only after weeks of clean live behaviour.

## Operating it

| Task | Command |
|---|---|
| Overview | `python -m g_aegisbot.admin status` |
| **Pause all new trades** | `python -m g_aegisbot.admin kill --reason "why"` |
| **Close everything now** | `python -m g_aegisbot.admin kill --flatten --reason "why"` |
| Pause one account | `python -m g_aegisbot.admin kill --user you@example.com` |
| Lift a pause | `python -m g_aegisbot.admin resume` |
| Add subscription days | `python -m g_aegisbot.admin extend you@example.com --days 30` |
| Who changed what | `python -m g_aegisbot.admin audit` |
| Rotate the encryption key | put the new key first in `AEGIS_MASTER_KEYS`, then `python -m g_aegisbot.admin rotate-keys` |

The kill switch is read by the runner every minute, so it takes effect within about a minute, and only
while the runner is running. `flatten` stays in force until you `resume`. Pausing never removes stops.
Logs go to the journal: `journalctl -u g-aegisbot-runner -f`. The dashboard's admin page shows whether the
runner has reported in recently.

Back up the database. For Supabase use its backups; for SQLite copy `data/aegisbot.db` while the runner is
stopped.

## What protects the money

* Stops are exchange-side orders; the bot re-places a missing stop within a minute and puts a stop on any
  position it finds without one (for example after a crash between entry and stop).
* A key that can withdraw is refused. Keys are encrypted at rest, never logged, never returned by the API.
* The daily loss limit, the 4-position cap and the 1.5% per-trade cap are enforced in code.
* A lapsed subscription stops new trades but keeps managing open positions.

## Known limits

* The strategy is unproven on real data. Costs are modelled as assumptions (flat slippage tiers, a constant
  funding rate, a simplified liquidation model). The backtest is not an exact Binance simulation.
* The 4% limit stops new entries; it does not close open positions.
* A deposit or withdrawal during the day shifts equity and can trip or mask the daily limit.
* Binance changes its API: re-run the self-test after any ccxt upgrade.
* Billing signatures follow NOWPayments' documented method but have only been tested against locally
  generated signatures. Test with the NOWPayments sandbox before taking real payments.
* Not financial advice. Futures trading can lose more than you expect, quickly.

## Environment reference

See `.env.example`. Key variables: `AEGIS_MASTER_KEYS`, `AEGIS_DATABASE_URL`, `SUPABASE_URL` or
`SUPABASE_JWT_SECRET`, `AEGIS_CORS_ORIGINS`, and the optional `NOWPAYMENTS_*` / `SUBSCRIPTION_*` group.
