# Approval and freeze signing: what exists, and the decisions still open

Status: verification infrastructure only. No key has been generated, no public key is pinned, and no approval or freeze record
has been signed. Until the decisions below are made, every approval and every freeze record is rejected, so no acceptance run
can be eligible.

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
* Nothing in the repository creates a private key, stores one, or signs a record. The library can only verify. The helper that
  signs (`signing.sign_record`) exists for an offline signer and for tests that use throw-away in-memory keys.

## What a signature does and does not prove
It proves that someone holding the private key signed exactly these bytes. It does not prove who that person is, that they
reviewed the protocol, or that the key was not copied. Identity rests on custody of the key and on who may change the pinned
list. A signature also does not stop a signer from approving a bad protocol.

## Decisions required from you before anything is signed or pinned
1. **Who is the approver?** One named person, or several. The role in the trusted-keys file is `approver`. Decide whether one
   signature is enough or two independent ones are required (the code currently accepts one).
2. **Where does the private key live?** Suggested: on a device or hardware token you control, never on the VPS, never in this
   repository, never in a cloud session. Decide how it is backed up and who may use it.
3. **Who may change the pinned public-key list?** The list is the root of trust. If the implementer can edit it, the
   implementer can approve their own work. Suggested: only the approver commits changes to it, on a signed commit, and the
   commit that pins the key is reviewed by someone other than the implementer.
4. **Key lifetime and rotation.** `valid_from` and `valid_until` are checked at the moment a record is verified, so a record
   signed by a key that has since expired stops verifying. Decide whether that is acceptable or whether verification should use
   the signing time (which a record can claim falsely unless it is anchored externally).
5. **Revocation.** Decide how a compromised key is marked (`revoked: true`) and what happens to records it signed.
6. **External anchor.** A signature does not stop an approved record from being deleted or replaced by an earlier one. Suggested:
   commit the approval and freeze records on a signed commit, tag it, and write the tag and commit id into the report. That relies
   on repository access control, not on this code.
7. **Signing procedure.** Decide the offline steps (build the unsigned payload with `freeze.build_approval_payload`, sign on the
   trusted device, add the record, review the diff, commit). A command-line tool for this is deliberately not provided until the
   custody decisions are made.

## Not covered by this phase
Signing of the evaluator's verdicts, the run register and consumption tokens, and per-candidate confirmation limits belong to
later phases.
