# The live-trading gate

`g_aegisbot/engine/livegate.py` decides one thing: **may a new real-money entry open now?** It is checked by the runner before
every live entry (twice: for the cycle and again at the entry itself), by `admin set-mode live`, and by the API when an
admin switches a profile to live. Paper (testnet-mode) profiles are not affected. Open positions are always managed (stops,
trailing, exits, kill switch) whatever the gate says. Any error inside the gate is a refusal.

## What must hold (all of it, every time)
| # | Condition |
|---|---|
| G1 | `AEGIS_LIVE_ENABLED=1` in the runner's environment. Default off. Never set by a unit file or by code. |
| G2 | The strategy-acceptance protocol is approved: signed approval record, valid signature from a pinned key, document hash unchanged. |
| G3 | `docs/LIVE_AUTHORIZATION.json` exists, is strict JSON with exactly the expected fields, is signed (purpose `live_authorization`) by a pinned trusted key, is inside its validity window and was issued for at most 90 days. |
| G4 | It names the exact strategy parameters this process runs (SHA-256 of `StrategyParams`) and the approved protocol hash; it states acceptance outcome `PASS` with the freeze-record and report hashes and at least 28 days of paper soak. |
| G5 | The code this process runs (import closure of the runner and the simulator) equals the code hash in the record, and is byte-for-byte a committed version. |
| G6 | Every risk limit is at least as strict as the authorised one (a limit set to 0, meaning "off", counts as looser); the daily drawdown limit is no looser. |
| G7 | Per account: wallet cap and leverage within the authorised maximum, and API keys verified (withdrawals off) within the last 7 days. |
| G8 | The single-runner lock is held. |

There is no override and no environment variable that skips a condition. **Changing any strategy parameter, any line of code in the
runner's closure, or any risk limit invalidates the authorisation** until the owner re-authorises and re-signs.

## Who creates the record
Only the owner, on their own device, with the offline signer (`docs/APPROVAL_SIGNING.md`):
```bash
python -m g_aegisbot.backtest.signer live-template --candidate ID --version v1 --authorised-by "Name" --valid-days 60 \
  --freeze-sha256 H --report-sha256 H --paper-soak-days 28 --max-cap 200 --max-leverage 3 > live_unsigned.json
python -m g_aegisbot.backtest.signer sign-live --key ~/aegis-keys/approver.pem --in live_unsigned.json
```
`live-template` refuses to run unless the code is committed and the protocol is approved. `sign-live` shows the full record and needs
the candidate id typed back. Nothing in the repository creates or signs a record, and no key exists today.

## Today
No key is pinned, no protocol approval exists, and no live authorisation exists, so the gate is closed everywhere. Tests:
`tests/test_livegate.py` (the real gate, every condition on its own, the runner, the CLI and the API) and the live cases in
`tests/test_signer.py`.
