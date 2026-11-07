# Testnet readiness checklist

Nothing below has been run. It needs your Binance USDT-M futures **testnet** keys and your approval for each step.
Do not use mainnet keys, and do not enable live mode, until every item passes and you have reviewed the results.

1. Confirm `AEGIS_DATABASE_URL` points at a **test** database, not production. Take and check a backup of that database.
   Then, only with your explicit approval, apply the schema with `python -m g_aegisbot.admin migrate` (the module
   `g_aegisbot.migrate` has no command line of its own). This changes the database schema. Do not run it against
   the production database until you have a verified backup and have approved that specific run. SQLite databases are
   also migrated automatically the first time the runner or the admin CLI starts, so take the backup before that too.
2. Create a testnet-mode profile with testnet keys (`python -m g_aegisbot.admin create-user` then `set-keys`). Before the
   dashboard can be used, link each login. Ordinary users link themselves on first sign-in only if the API can ask
   Supabase Auth to confirm their email (`SUPABASE_URL` and the public `SUPABASE_ANON_KEY` set, and Supabase
   "Confirm email" ON); check this with a real invited account and a second, unconfirmed one that must be refused.
   Admin profiles, and everyone when those settings are absent, are linked by you with
   `python -m g_aegisbot.admin bind-login <user> <Supabase User UID>`.
3. `python -m g_aegisbot.tools.selftest --symbol BTC/USDT:USDT` (testnet is the default; `--mainnet` needs
   `--yes-real-money`, never use it here). **This places a small real order on the testnet exchange** (about 120 USDT
   notional) and closes it, so it needs your explicit go-ahead. Read every line of its output.
4. `python -m g_aegisbot.engine.runner --dry-run` for at least 3 candle closes. Expect "DRY RUN, would open ..." lines and no orders.
5. Start with `AEGIS_STRATEGIES=breakout`, one profile, small cap. Verify on the testnet UI for each entry:
   * the entry quantity and the client order id (`aeg-bre-...`);
   * a reduce-only STOP_MARKET exists for the filled quantity;
   * the dashboard shows the position, strategy and stop.
6. Restart the runner while a position is open. Expect the position to be recognised, its stop verified, and trailing
   to continue from the stored state. Delete the stop order on the testnet UI and confirm the bot recreates it.
7. Cut the network for 60 seconds around an entry (or block the host). Expect `OrderUncertain` handling: no second
   entry and entries blocked. Recovery is NOT automatic for an order the exchange never showed: expect the block to stay
   until `python -m g_aegisbot.admin pending-entries` and `resolve-entry` (docs/OPERATOR_RECOVERY.md) are used, or until
   the exchange shows the order terminal. Practise that procedure here, and confirm on the testnet that the order
   history, trades and conditional-order queries return real data for a known order.
8. Trigger the kill switch (pause, then flatten) from the admin page. Confirm positions close and nothing reopens.
9. Let the daily breaker trip (lower the threshold in a test build only). Confirm entries stop until 00:00 UTC.
10. Only then enable `trend` alongside `breakout`, repeat steps 5 to 8, and check the conflict and duplicate decisions
    in the log.
11. NOWPayments sandbox: create an invoice, pay it in the sandbox, confirm the IPN signature validates against the
    single canonical form in `api/billing.py` and the subscription extends exactly once. If signatures fail, stop and
    compare one real body byte for byte before changing anything.
12. Record results with dates. Production deployment needs a separate review.

## Added in Phase 1.3 (confirm on the testnet before any live use)
13. The ccxt futures sandbox is refused by ccxt 4.5.92 for USD-M futures ("use demo trading"). Work out how the testnet or demo
    account is reached before running the self-test; the adapter tests use mocked HTTP only.
14. Place a conditional stop and confirm it appears in `openAlgoOrders`, that the cancel goes to the algo endpoint, and that the
    listing is empty afterwards. Compare the real payload fields with `tests/ccxt_real.py`.
15. Start two runners on one database and confirm the second exits with code 3. Kill the first with `kill -9` and confirm a new
    runner starts. For PostgreSQL, confirm the advisory lock on the direct or session-pooler address.
16. Run `--dry-run --once` with an open position and an unresolved entry. Confirm the exchange shows no new or cancelled orders.
