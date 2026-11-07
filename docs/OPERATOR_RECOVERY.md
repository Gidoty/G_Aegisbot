# Operator recovery: a stuck entry order

An entry order whose outcome is unknown (a timeout, a crash, an exchange that did not answer) **blocks new trades for that
user** until it is resolved. The bot never releases such an order because time passed, or because the exchange said "no
such order", or because a listing came back empty. It releases it by itself only when the exchange shows a terminal
outcome for that exact order (filled and protected, or canceled/expired/rejected with nothing filled, a flat position and
a complete trade history). Everything else waits for you.

Run these on the server, in the project directory, with the virtual environment active. They change nothing on the
exchange; they only read it, and (with `--confirm`) append one line to the audit log.

## 1. See what is stuck
```bash
python -m g_aegisbot.admin pending-entries
```
Each line gives the user, the **client order id**, the symbol and when it was sent. `DAMAGED RECORD` means the journal row
is incomplete (see section 4).

## 2. Look at the evidence for ONE order
```bash
python -m g_aegisbot.admin resolve-entry <user> <client order id> --symbol <exact symbol>
```
The id and the symbol must both match the journal. The command reads the exchange now and prints: the order looked up by
client id, the order history and the account trades for the window since it was sent (at most 7 days), the open orders
and conditional orders, the position (and its position mode), and the protective stop. Then it prints a verdict:

| Verdict | Meaning | What you can do |
|---|---|---|
| `FILLED` | The order is terminal with a fill. | Resolvable if the position is flat or has a valid full-size stop. If the position has no valid stop, **protect or close it first** (the bot's next check does this). |
| `UNFILLED` | Terminal, zero fill, flat, and the trade history is complete and empty. | Resolvable (the bot normally does this by itself). |
| `NOT_SEEN` | Every source answered and none shows the order. **This does not prove it was never accepted.** | Resolvable only by you, after you have looked yourself (for example in the Binance web or app order and trade history for that symbol and time). |
| `BLOCKED` | A source failed, is incomplete, contradicts another, shows the order still working, shows a position, or hedge mode. | **Refused.** Nothing can clear it. Fix the cause (network, API permissions, close the position) and run the command again. |

A plain run changes nothing.

## 3. Resolve it
Only when the verdict allows it and you accept it:
```bash
python -m g_aegisbot.admin resolve-entry <user> <client order id> --symbol <symbol> --confirm --reason "<why, 12+ characters, no secrets>"
```
A reason is required and recorded, but it **never** replaces evidence: with a `BLOCKED` verdict the command refuses even
with `--confirm` and a good reason. A refusal is also written to the audit log.

On success one row (`entry_resolved`) is appended with your operator name, the time, the reason, the outcome and a digest
of the evidence. The original `entry_intent` row is never changed or removed. A running bot notices the resolution on its
next check (about a minute); a restarted bot reads it from the journal.

Review it with `python -m g_aegisbot.admin audit`.

## 4. A damaged journal record
* **No symbol** (the row has a client id): `pending-entries` shows it as damaged. Run `resolve-entry` with the `--symbol`
  you determine from the exchange; it is accepted only with the same clean evidence as above.
* **No client order id**: it cannot be addressed by id, so the tool cannot resolve it and entries for that user stay
  blocked. Find the audit row number in the log line (`damaged journal record #N`), check the exchange yourself for any
  position or order that user may have on that symbol around the row's time, and only then, with direct database access,
  append an `entry_resolved` row for it yourself (action `entry_resolved`, target any unique text, detail stating what you
  checked). There is deliberately no command for this: it should be rare and fully manual.

## 5. Paper (testnet-mode) accounts
A paper account has no separate exchange; its ledger is in our database. The same commands work and read that ledger. An
intent can only be left open there if the process died inside a single database write.

## What this process cannot do
* It cannot prove an order was never accepted. `NOT_SEEN` is your judgment, recorded with its evidence.
* It cannot see another exchange account or sub-account. Use a dedicated Binance account for the bot.
* The order and trade history endpoints serve at most 7 days per query. An intent older than that cannot be reconciled
  automatically (the verdict is `BLOCKED`); check the exchange by hand and use section 4's manual route.
* Whether Binance returns every order and trade in these queries immediately is not something this code assumes. That is why
  an absent order never clears itself.

## Phase 1.3 notes
* `resolve-entry` also reads the account's position mode. Hedge mode, or a mode that cannot be read, is refused.
* A resolution is tied to the user, the client id, the original intent row and the symbol. A second command, or a second operator
  racing the first, writes nothing and says so.
* The order the exchange returns must carry the client id and symbol you asked about. Anything else is treated as an error.
