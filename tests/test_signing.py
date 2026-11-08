"""Ed25519 verification infrastructure (finding O): canonical bytes, trusted keys, and every way a signature can be wrong.
Keys are throw-away and in memory. No key is pinned or created by the code under test."""
import base64
from datetime import timedelta
from pathlib import Path

import pytest

from g_aegisbot.backtest import freeze as fz
from g_aegisbot.backtest import signing as sg
from g_aegisbot.backtest.protected import ProtectedPathError
from tests.signing_helpers import NOW, key_entry, make_repo, new_key, pin_keys, sign_freeze, trusted_document

REC = {"status": "approved", "version": "v1", "n": 3, "f": 0.983, "nested": {"b": [1, 2, None, True], "a": "é"}}


def trusted(*keys, **kw):
    return sg.parse_trusted_keys(trusted_document(*[key_entry(k, **kw) for k in keys]))


# ---------------------------------------------------------------- canonical bytes
def test_canonical_bytes_are_deterministic_and_escape_non_ascii():
    a = {"b": 1, "a": {"y": 2, "x": [3, 4]}}
    b = {"a": {"x": [3, 4], "y": 2}, "b": 1}
    assert sg.canonical_bytes(a) == sg.canonical_bytes(b) == b'{"a":{"x":[3,4],"y":2},"b":1}'
    assert sg.canonical_bytes({"k": "é"}) == b'{"k":"\\u00e9"}'
    assert sg.canonical_bytes({"f": 0.983, "i": 3, "t": True, "n": None}) == b'{"f":0.983,"i":3,"n":null,"t":true}'


@pytest.mark.parametrize("bad", [{"a": float("nan")}, {"a": float("inf")}, {"a": (1, 2)}, {"a": b"x"}, {1: "a"}, {"a": {1, 2}},
                                 {"a": object()}, [[[1]]] and {"a": {"b": {"c": {"d": {"e": float("-inf")}}}}}])
def test_values_with_no_unique_encoding_are_rejected(bad):
    with pytest.raises(sg.SigningError):
        sg.canonical_bytes(bad)


def test_excessive_nesting_is_rejected():
    deep = cur = {}
    for _ in range(40):
        cur["x"] = {}
        cur = cur["x"]
    with pytest.raises(sg.SigningError):
        sg.canonical_bytes(deep)


def test_the_signed_message_is_domain_separated_by_purpose():
    assert sg.signing_message("approval", REC) != sg.signing_message("freeze", REC)
    assert sg.signing_message("approval", REC).startswith(sg.DOMAIN + b"\napproval\n")
    with pytest.raises(sg.SigningError):
        sg.signing_message("other", REC)


# ---------------------------------------------------------------- sign and verify
def test_a_record_signed_with_a_trusted_key_verifies():
    k = new_key()
    signed = sg.sign_record(REC, k, sg.PURPOSE_APPROVAL)
    assert signed["signature"]["algorithm"] == "ed25519" and signed["signature"]["key_id"] == sg.key_id_of(sg.public_raw(k))
    assert sg.verify_record(signed, sg.PURPOSE_APPROVAL, trusted(k), at=NOW) == []
    assert sg.verify_record(sg.sign_record(REC, k, sg.PURPOSE_FREEZE), sg.PURPOSE_FREEZE, trusted(k), at=NOW) == []


def test_signing_twice_or_signing_a_signed_record_is_refused():
    k = new_key()
    with pytest.raises(sg.SigningError):
        sg.sign_record(sg.sign_record(REC, k, "approval"), k, "approval")


@pytest.mark.parametrize("field,value", [("status", "pending"), ("version", "v2"), ("n", 4), ("f", 0.98), ("nested", {"a": "é"}), ("extra", 1)])
def test_any_change_to_the_record_breaks_the_signature(field, value):
    k = new_key()
    signed = sg.sign_record(REC, k, sg.PURPOSE_APPROVAL)
    signed[field] = value
    assert any("does not match" in p for p in sg.verify_record(signed, sg.PURPOSE_APPROVAL, trusted(k), at=NOW))


def test_removing_a_field_breaks_the_signature():
    k = new_key()
    signed = sg.sign_record(REC, k, sg.PURPOSE_APPROVAL)
    del signed["n"]
    assert sg.verify_record(signed, sg.PURPOSE_APPROVAL, trusted(k), at=NOW)


def test_a_signature_for_one_purpose_is_not_valid_for_the_other():
    k = new_key()
    approval = sg.sign_record(REC, k, sg.PURPOSE_APPROVAL)
    assert any("does not match" in p for p in sg.verify_record(approval, sg.PURPOSE_FREEZE, trusted(k), at=NOW))
    freeze = sg.sign_record(REC, k, sg.PURPOSE_FREEZE)
    assert any("does not match" in p for p in sg.verify_record(freeze, sg.PURPOSE_APPROVAL, trusted(k), at=NOW))


def test_an_unknown_public_key_is_rejected():
    trusted_key, stranger = new_key(), new_key()
    signed = sg.sign_record(REC, stranger, sg.PURPOSE_APPROVAL)
    assert any("not trusted" in p for p in sg.verify_record(signed, sg.PURPOSE_APPROVAL, trusted(trusted_key), at=NOW))


def test_claiming_a_trusted_key_id_with_a_signature_from_another_key_is_rejected():
    real, forger = new_key(), new_key()
    signed = sg.sign_record(REC, forger, sg.PURPOSE_APPROVAL)
    signed["signature"]["key_id"] = sg.key_id_of(sg.public_raw(real))
    assert any("does not match" in p for p in sg.verify_record(signed, sg.PURPOSE_APPROVAL, trusted(real), at=NOW))


@pytest.mark.parametrize("mutate,why", [
    (lambda s: s.update(algorithm="rsa"), "not ed25519"),
    (lambda s: s.update(value="!!!not base64!!!"), "not valid base64"),
    (lambda s: s.update(value=base64.b64encode(b"short").decode()), "64 bytes"),
    (lambda s: s.update(value=5), "not text"),
    (lambda s: s.update(key_id="0" * 64), "not trusted"),
    (lambda s: s.update(key_id=None), "not trusted"),
    (lambda s: s.update(extra="x"), "no valid signature block"),
    (lambda s: s.pop("value"), "no valid signature block"),
])
def test_malformed_signature_blocks_are_rejected(mutate, why):
    k = new_key()
    signed = sg.sign_record(REC, k, sg.PURPOSE_APPROVAL)
    mutate(signed["signature"])
    assert any(why in p for p in sg.verify_record(signed, sg.PURPOSE_APPROVAL, trusted(k), at=NOW))


def test_a_flipped_signature_bit_is_rejected():
    k = new_key()
    signed = sg.sign_record(REC, k, sg.PURPOSE_APPROVAL)
    raw = bytearray(base64.b64decode(signed["signature"]["value"]))
    raw[0] ^= 1
    signed["signature"]["value"] = base64.b64encode(bytes(raw)).decode()
    assert any("does not match" in p for p in sg.verify_record(signed, sg.PURPOSE_APPROVAL, trusted(k), at=NOW))


@pytest.mark.parametrize("record", [None, "x", 5, [], True])
def test_a_record_that_is_not_an_object_is_rejected(record):
    assert sg.verify_record(record, sg.PURPOSE_APPROVAL, trusted(new_key()), at=NOW) == ["the record is not an object"]


def test_no_trusted_keys_means_nothing_verifies():
    k = new_key()
    signed = sg.sign_record(REC, k, sg.PURPOSE_APPROVAL)
    assert any("no trusted approver key" in p for p in sg.verify_record(signed, sg.PURPOSE_APPROVAL, sg.TrustedKeys(), at=NOW))
    assert any("no trusted approver key" in p for p in sg.verify_record(signed, sg.PURPOSE_APPROVAL, sg.parse_trusted_keys(trusted_document()), at=NOW))


def test_revoked_expired_not_yet_valid_and_wrong_role_keys_are_rejected():
    k = new_key()
    signed = sg.sign_record(REC, k, sg.PURPOSE_APPROVAL)
    assert any("revoked" in p for p in sg.verify_record(signed, sg.PURPOSE_APPROVAL, trusted(k, revoked=True), at=NOW))
    assert any("validity window" in p for p in sg.verify_record(signed, sg.PURPOSE_APPROVAL, trusted(k, valid_until=NOW - timedelta(days=1)), at=NOW))
    assert any("validity window" in p for p in sg.verify_record(signed, sg.PURPOSE_APPROVAL, trusted(k, valid_from=NOW + timedelta(days=1)), at=NOW))
    assert sg.verify_record(signed, sg.PURPOSE_APPROVAL, trusted(k, valid_until=NOW + timedelta(days=1)), at=NOW) == []
    assert any("required role" in p for p in sg.verify_record(signed, sg.PURPOSE_APPROVAL, trusted(k), at=NOW, role="other"))


# ---------------------------------------------------------------- the trusted-keys document
def doc_with(**override):
    e = key_entry(new_key())
    e.update(override)
    return trusted_document(e)


@pytest.mark.parametrize("data,why", [
    (None, "exactly"), ([], "exactly"), ("x", "exactly"), ({"version": 1}, "exactly"), ({"version": 1, "keys": [], "extra": 1}, "exactly"),
    ({"version": 2, "keys": []}, "version"), ({"version": True, "keys": []}, "version"), ({"version": 1, "keys": None}, "list"),
    ({"version": 1, "keys": [None]}, "missing or unexpected"), ({"version": 1, "keys": [{}]}, "missing or unexpected"),
])
def test_malformed_trusted_key_documents_are_rejected(data, why):
    with pytest.raises(sg.SigningError, match=why):
        sg.parse_trusted_keys(data)


@pytest.mark.parametrize("override,why", [
    ({"key_id": "f" * 64}, "not the SHA-256"), ({"public_key": "***"}, "base64"), ({"public_key": base64.b64encode(b"x" * 31).decode()}, "32 bytes"),
    ({"public_key": 5}, "not text"), ({"role": "admin"}, "role"), ({"revoked": "no"}, "revoked"), ({"label": 5}, "label"),
    ({"valid_from": "2026-01-01T00:00:00"}, "time zone"), ({"valid_from": "yesterday"}, "ISO-8601"), ({"valid_from": 5}, "not text"),
    ({"valid_until": "2000-01-01T00:00:00+00:00"}, "not after"), ({"valid_until": "soon"}, "ISO-8601"),
])
def test_malformed_trusted_keys_are_rejected(override, why):
    with pytest.raises(sg.SigningError, match=why):
        sg.parse_trusted_keys(doc_with(**override))


def test_duplicate_key_ids_are_rejected():
    e = key_entry(new_key())
    with pytest.raises(sg.SigningError, match="duplicate"):
        sg.parse_trusted_keys(trusted_document(e, dict(e)))


def test_a_missing_trusted_keys_file_is_empty_and_not_present(tmp_path):
    tk = sg.load_trusted_keys(tmp_path / "absent.json")
    assert tk.present is False and tk.keys == {}


@pytest.mark.parametrize("raw", [b"\xff\xfe", b"{", b'{"version": 1, "keys": [], "keys": []}', b'{"version": NaN, "keys": []}', b"null"])
def test_a_malformed_trusted_keys_file_raises(tmp_path, raw):
    p = tmp_path / "keys.json"
    p.write_bytes(raw)
    with pytest.raises(sg.SigningError):
        sg.load_trusted_keys(p)


def test_trusted_keys_path_rules(tmp_path):
    import os
    big = tmp_path / "big.json"
    big.write_text(" " * (sg.MAX_KEYS_FILE_BYTES + 1))
    with pytest.raises(sg.SigningError, match="too large"):
        sg.load_trusted_keys(big)
    link = tmp_path / "link.json"
    os.symlink(big, link)
    with pytest.raises(sg.SigningError, match="regular file"):
        sg.load_trusted_keys(link)
    (tmp_path / "d.json").mkdir()
    with pytest.raises(sg.SigningError, match="regular file"):
        sg.load_trusted_keys(tmp_path / "d.json")
    with pytest.raises(ProtectedPathError):
        sg.load_trusted_keys(tmp_path / ".env.keys")


# ---------------------------------------------------------------- freeze records through the repository helper
def test_a_freeze_record_needs_a_pinned_key_and_a_valid_signature(tmp_path):
    root, k = make_repo(tmp_path), new_key()
    record = {"freeze_version": 1, "x": "y"}
    assert any("no trusted approver key" in p for p in fz.freeze_signature_problems(sg.sign_record(record, k, "freeze"), root, NOW))
    pin_keys(root, k)
    assert fz.freeze_signature_problems(sign_freeze(record, k), root, NOW) == []
    tampered = sign_freeze(record, k)
    tampered["x"] = "z"
    assert any("does not match" in p for p in fz.freeze_signature_problems(tampered, root, NOW))
    assert any("no valid signature block" in p for p in fz.freeze_signature_problems(record, root, NOW))
    (root / fz.TRUSTED_KEYS_DOC).write_text("null")
    assert any("trusted-keys file cannot be used" in p for p in fz.freeze_signature_problems(sign_freeze(record, k), root, NOW))


def test_the_signature_block_is_excluded_from_the_freeze_hash_but_covered_by_the_signature(tmp_path):
    k = new_key()
    rec = fz.build_freeze_record(protocol_sha256="a" * 64, source_sha256="b" * 64,
                                 candidates={"breakout": {"strategies": ["breakout"], "params_sha256": "c" * 64}}, universe=["BTC/USDT:USDT"],
                                 liquidation_file_sha256="e" * 64, declaration={"by": "u", "date": "d", "statement": "s"},
                                 diversification_metric="sharpe")
    signed = sign_freeze(rec, k)
    assert fz.validate_freeze_record(signed) == [] and fz.validate_freeze_record(rec) == []
    signed["universe"] = ["ETH/USDT:USDT"]
    assert any("own hash" in p for p in fz.validate_freeze_record(signed))


# ---------------------------------------------------------------- what this code must NOT do
def test_the_signing_module_never_creates_a_key_or_writes_a_file():
    text = (Path(sg.__file__)).read_text()
    for forbidden in (".generate(", "generate_private", "write_text", "write_bytes", "open(", "os.environ", "private_bytes"):
        assert forbidden not in text, forbidden
    assert "docs/STRATEGY_ACCEPTANCE.trusted_keys.json" not in text            # no key location or key is built into the module


def test_the_repository_pins_no_key():
    assert not (Path(fz.REPO_ROOT) / fz.TRUSTED_KEYS_DOC).exists()
