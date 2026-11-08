# Approval and freeze signing: how it works, and what only you can do

Status: the verification code and an offline signer exist (`g_aegisbot/backtest/signer.py`). **No key has been generated, no
public key is pinned, and nothing has been signed.** Until you do the steps in "What only you can do", every approval and
every freeze record is rejected, so no acceptance run can be eligible. That is the intended safe state.

## Decisions taken (the builder's recommendation, in force until you change them)
1. **One approver: you.** One signature is required. A second approver can be added later (more `approver` keys and a rule
   that two must sign); it is not needed while there is a single owner.
2. **The private key lives only on your own trusted device**, encrypted with a passphrase of at least 12 characters, backed up
   offline. Never on the VPS, never in this repository, never in a cloud session or chat. The signer refuses any key path
   inside the repository and any key file other users can read.
3. **Only you change the pinned public-key list**, by your own commit, with GitHub branch protection (a required review by you,
   no direct pushes) on `docs/STRATEGY_ACCEPTANCE.trusted_keys.json`, the approval record and the freeze records. The
   implementer, human or AI, must not be able to edit that file unreviewed.
4. **Key lifetime: one year** (`--years`), with rotation by adding the new key before the old one expires. Verification uses the
   time of checking, so an expired key stops verifying its old records. That is the safe default (it fails closed); re-sign
   the records with the new key after rotation.
5. **Revocation:** set `"revoked": true` for the key in the pinned file; records it signed stop verifying.
6. **External anchor:** commit the signed approval and freeze records on a signed commit and tag it; write the tag and commit id
   in every report. The records' integrity then rests on repository access control, not on this code.
7. **Procedure:** the signer commands below. Each signature is a deliberate act: signing an approval shows the protocol's
   SHA-256 and asks you to type its first eight characters.

## What only you can do (commands run on your own device, in a checkout of this repository)
```bash
python -m g_aegisbot.backtest.signer keygen --out ~/aegis-keys/approver.pem --label "owner 2026" --years 1
```
Copy the printed public entry into `docs/STRATEGY_ACCEPTANCE.trusted_keys.json` as `{"version": 1, "keys": [ ...entry... ]}`
and commit it yourself. Back up `approver.pem` offline. Then, once you have read `docs/STRATEGY_ACCEPTANCE.md` and accept it:
```bash
python -m g_aegisbot.backtest.signer sign-approval --key ~/aegis-keys/approver.pem --approved-by "Your Name" --version v1.0
python -m g_aegisbot.backtest.signer verify --purpose approval --in docs/STRATEGY_ACCEPTANCE.approval.json
```
After the freeze record is built (unsigned) for an acceptance run:
```bash
python -m g_aegisbot.backtest.signer sign-freeze --key ~/aegis-keys/approver.pem --in freeze_unsigned.json --out freeze.json
```
Changing the protocol document after approval withdraws the approval automatically (the hash no longer matches).

## What the code does (g_aegisbot/backtest/signing.py, freeze.py)
* A record is approved or frozen only if it carries an **Ed25519 signature** that verifies against a key listed in
  `docs/STRATEGY_ACCEPTANCE.trusted_keys.json` (the file does not exist yet).
* The signed bytes are a defined canonical JSON form of the record without its signature block, prefixed with a domain tag and
  the purpose (`approval` or `freeze`). A signature made for one purpose is not valid for the other.
* A key is trusted only if its entry matches its own SHA-256 id, has the role `approver`, is not revoked, and is inside its
  validity window when the record is checked.
* The approval record must be a regular file (not a link), small, valid UTF-8, strict JSON without duplicate keys or NaN, and
  decode to an object with exactly the expected fields. `null`, booleans, numbers, strings and arrays are refused. `approved` is
  true only after every step succeeds explicitly.
* The verifying code never creates, stores or signs with a key. Signing is done only by the separate offline signer
  (`signer.py`), which the backtester does not import, and by tests that use throw-away in-memory keys.

## What a signature does and does not prove
It proves that someone holding the private key signed exactly these bytes. It does not prove who that person is, that they
reviewed the protocol, or that the key was not copied. Identity rests on custody of the key and on who may change the pinned
list. A signature also does not stop a signer from approving a bad protocol.

## Not covered by this phase
Signing of the evaluator's verdicts, the run register and consumption tokens, and per-candidate confirmation limits belong to
later phases.

## File access and code identity (hardening added after the Phase 1 review)

**Guarded file access.** Every file the backtest tooling reads or writes by a path it is given (freeze records, liquidation
brackets, caches, run outputs, the manifest) goes through `backtest/safeio.py`. A protected path (environment files, keys,
credentials, databases), a symbolic link, a special file and a file with more than one hard link are refused before any
byte is read, so an innocent name hard-linked to a secret is caught. Reads are size-bounded and JSON is strict (UTF-8, no
duplicate keys, no NaN or Infinity). Writes are atomic; a freeze record is never overwritten unless asked, and writing
through a link is refused. Errors never repeat file content. A race between the check and the open remains possible if
another process can change the directory at that moment; keep the working directory writable by the operator only.

**One freeze check.** `freeze.freeze_problems()` is the check that counts: structure, hash, match to the run, AND the
signature against the pinned key. `validate_freeze_record()` alone does not look at the signature.

**What the code hash proves.** `source_closure` reads the source and refuses what it cannot follow: `exec`, `eval`, bare
`compile`, `runpy`, `builtins`, aliases of dynamic loaders (assigned, imported by name or looked up by string), symbolic
links and files that resolve outside the package. It cannot see a loader name assembled at run time from pieces, data
files read at run time, or the third-party libraries themselves (only their versions are recorded). The manifest therefore
also records `code.committed`: whether every closure file is byte-for-byte what `HEAD` contains (checked with
`git rev-parse HEAD:<path>` only, never `git status` or `git diff`). `code_matches_commit` is an acceptance condition, so an
acceptance run needs a committed, unmodified copy of the code, and anyone can reproduce it from the recorded commit.
