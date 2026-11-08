"""Approval validation fails closed (findings P and X1): null, malformed JSON, wrong types, bad UTF-8, oversize files, links,
every field, and the signature. `approved` is only ever True after a complete explicit success."""
import json
import os

import pytest

from g_aegisbot.backtest import freeze as fz
from g_aegisbot.backtest import manifest as mf
from g_aegisbot.backtest import signing as sg
from tests.signing_helpers import NOW, approved_repo, make_repo, new_key, write_signed_approval


def rec_path(root):
    return root / fz.APPROVAL_DOC


def check(root, **kw):
    return fz.validate_protocol_approval(root, now=NOW, **kw)


def refused(root, why=None):
    r = check(root)
    assert r["approved"] is False and r["problems"] and r["signed_by"] is None, r
    if why:
        assert any(why in p for p in r["problems"]), r["problems"]
    return r


# ---------------------------------------------------------------- the success path is explicit
def test_a_correctly_signed_approval_of_the_current_document_is_approved(tmp_path):
    root, key = approved_repo(tmp_path)
    r = check(root)
    assert r["approved"] is True and r["problems"] == [] and r["signed_by"] == sg.key_id_of(sg.public_raw(key))
    assert r["protocol_sha256"] == mf.sha256_file(root / mf.PROTOCOL_DOC)


def test_the_unsigned_record_of_the_old_design_is_not_an_approval(tmp_path):
    root, key = approved_repo(tmp_path)
    data = json.loads(rec_path(root).read_text())
    del data["signature"]
    rec_path(root).write_text(json.dumps(data))
    refused(root, "missing or unexpected fields")


def test_no_trusted_keys_file_means_nothing_can_be_approved(tmp_path):
    root, key = approved_repo(tmp_path)
    (root / fz.TRUSTED_KEYS_DOC).unlink()
    refused(root, "no trusted approver key is pinned")


def test_the_repository_draft_has_no_approval_record_and_is_not_approved():
    r = fz.validate_protocol_approval()
    assert r["approved"] is False and any("still a draft" in p for p in r["problems"])


# ---------------------------------------------------------------- JSON that is not an approval object
@pytest.mark.parametrize("content", ["null", "true", "false", "0", "1", "-1", "1.5", '""', '"approved"', "[]", "[{}]", '[null]',
                                     "[1, 2, 3]", "  null  ", "null\n"])
def test_a_json_value_that_is_not_an_object_is_refused(tmp_path, content):
    root, _ = approved_repo(tmp_path)
    rec_path(root).write_text(content)
    refused(root, "is not an object")


def test_json_null_in_particular_is_not_an_approval(tmp_path):
    root = make_repo(tmp_path)
    rec_path(root).write_text("null")
    r = refused(root, "is not an object")
    assert r["approved"] is False                                  # the defect: this used to be True with no problems


@pytest.mark.parametrize("content", ["", " ", "{", "}", "{'status': 'approved'}", '{"status": "approved",}', "{status: approved}",
                                     "﻿{}", '{"a": 1} trailing', "nul", "True", "None",
                                     '{"status": "approved", "status": "approved"}',            # duplicate keys
                                     '{"status": NaN}', '{"x": Infinity}', '{"x": -Infinity}'])
def test_malformed_json_is_refused(tmp_path, content):
    root, _ = approved_repo(tmp_path)
    rec_path(root).write_text(content)
    refused(root, "cannot be used")


@pytest.mark.parametrize("raw", [b"\xff\xfe{}", b'{"status": "\xc3("}', b"\x80", b"\xef\xbb\xbf{}", b"{\x00}"])
def test_invalid_utf8_or_binary_content_is_refused(tmp_path, raw):
    root, _ = approved_repo(tmp_path)
    rec_path(root).write_bytes(raw)
    refused(root)


def test_an_oversized_record_is_refused_without_being_parsed(tmp_path):
    root, _ = approved_repo(tmp_path)
    rec_path(root).write_text('{"status": "' + "a" * (fz.MAX_RECORD_BYTES + 10) + '"}')
    refused(root, "too large")


def test_a_deeply_nested_record_is_refused_safely(tmp_path):
    root, _ = approved_repo(tmp_path)
    rec_path(root).write_text("[" * 200000 + "]" * 200000)
    refused(root)


def test_a_directory_a_symlink_and_a_broken_link_at_the_record_path_are_refused(tmp_path):
    root, _ = approved_repo(tmp_path)
    good = rec_path(root).read_text()
    rec_path(root).unlink()
    rec_path(root).mkdir()
    refused(root, "not a regular file")
    rec_path(root).rmdir()
    target = tmp_path / "elsewhere.json"
    target.write_text(good)
    os.symlink(target, rec_path(root))
    refused(root, "not a regular file")
    rec_path(root).unlink()
    os.symlink(tmp_path / "missing.json", rec_path(root))
    refused(root, "not a regular file")


def test_a_symlink_to_a_protected_file_is_refused_before_it_is_read(tmp_path):
    root, _ = approved_repo(tmp_path)
    protected = tmp_path / ".env.example"
    protected.write_text("SECRET=1")
    rec_path(root).unlink()
    os.symlink(protected, rec_path(root))
    refused(root, "protected path")


# ---------------------------------------------------------------- every field
def edit(root, fn):
    data = json.loads(rec_path(root).read_text())
    fn(data)
    rec_path(root).write_text(json.dumps(data))


@pytest.mark.parametrize("key", ["status", "version", "approved_by", "approved_at", "protocol_sha256", "signature"])
def test_a_missing_field_is_refused(tmp_path, key):
    root, _ = approved_repo(tmp_path)
    edit(root, lambda d: d.pop(key))
    refused(root)


@pytest.mark.parametrize("status", ["Approved", "APPROVED", "pending", "draft", "", " approved", True, 1, None, ["approved"], {"a": 1}])
def test_the_status_must_be_exactly_approved(tmp_path, status):
    root, _ = approved_repo(tmp_path)
    edit(root, lambda d: d.update(status=status))
    refused(root, "does not say 'approved'")


@pytest.mark.parametrize("field", ["version", "approved_by"])
@pytest.mark.parametrize("bad", ["", "   ", None, 5, True, [], {}, "bad\x00name", "line\nbreak", "tab\there", "x" * 300])
def test_text_fields_must_be_valid(tmp_path, field, bad):
    root, _ = approved_repo(tmp_path)
    edit(root, lambda d: d.update({field: bad}))
    refused(root, f"no valid {field}")


@pytest.mark.parametrize("bad", ["", "yesterday", "2026-13-45", "2026-11-07T12:00:00", "2026-11-07", None, 5, True, [], {}])
def test_approved_at_must_be_a_time_with_a_zone(tmp_path, bad):
    root, _ = approved_repo(tmp_path)
    edit(root, lambda d: d.update(approved_at=bad))
    refused(root, "no valid approved_at")


@pytest.mark.parametrize("bad", ["", "abc", "A" * 64, "g" * 64, "a" * 63, "a" * 65, None, 5, True, [], {}, ["a" * 64]])
def test_the_hash_must_be_a_lowercase_sha256(tmp_path, bad):
    root, _ = approved_repo(tmp_path)
    edit(root, lambda d: d.update(protocol_sha256=bad))
    refused(root, "no valid protocol_sha256")


def test_a_document_changed_after_approval_is_refused(tmp_path):
    root, _ = approved_repo(tmp_path)
    (root / mf.PROTOCOL_DOC).write_text("edited after approval")
    refused(root, "has changed since it was approved")


def test_a_missing_or_linked_protocol_document_is_refused(tmp_path):
    root, _ = approved_repo(tmp_path)
    (root / mf.PROTOCOL_DOC).unlink()
    refused(root, "document is missing")


def test_an_extra_field_is_refused(tmp_path):
    root, _ = approved_repo(tmp_path)
    edit(root, lambda d: d.update(note="smuggled"))
    refused(root, "missing or unexpected fields")


@pytest.mark.parametrize("sig", [None, "x", 5, True, [], {}, {"algorithm": "ed25519"}, {"algorithm": "ed25519", "key_id": "a", "value": "b", "extra": 1},
                                 {"algorithm": "rsa", "key_id": "a", "value": "b"}, {"algorithm": "ed25519", "key_id": 5, "value": "b"},
                                 {"algorithm": "ed25519", "key_id": "a" * 64, "value": 5}])
def test_an_invalid_signature_structure_is_refused(tmp_path, sig):
    root, _ = approved_repo(tmp_path)
    edit(root, lambda d: d.update(signature=sig))
    refused(root)


# ---------------------------------------------------------------- the signature itself
def test_a_record_signed_by_a_key_that_is_not_pinned_is_refused(tmp_path):
    root, _ = approved_repo(tmp_path)
    stranger = new_key()
    write_signed_approval(root, stranger)
    refused(root, "not trusted")


def test_a_record_altered_after_signing_is_refused(tmp_path):
    root, _ = approved_repo(tmp_path)
    edit(root, lambda d: d.update(approved_by="somebody else"))
    refused(root, "does not match")
    root2, _ = approved_repo(tmp_path, name="repo2")
    edit(root2, lambda d: d.update(version="v2"))
    refused(root2, "does not match")


def test_a_valid_signature_cannot_be_moved_to_another_document_version(tmp_path):
    root, key = approved_repo(tmp_path)
    old = rec_path(root).read_text()
    (root / mf.PROTOCOL_DOC).write_text("a different protocol")
    rec = json.loads(old)
    rec["protocol_sha256"] = mf.sha256_file(root / mf.PROTOCOL_DOC)          # the hash now matches, the signature does not
    rec_path(root).write_text(json.dumps(rec))
    refused(root, "does not match")


@pytest.mark.parametrize("trusted", ["null", "[]", "5", '{"version": 1}', '{"keys": []}', '{"version": 2, "keys": []}',
                                     '{"version": 1, "keys": null}', '{"version": 1, "keys": [null]}', "{", "\xff"])
def test_a_malformed_trusted_keys_file_means_refusal(tmp_path, trusted):
    root, _ = approved_repo(tmp_path)
    (root / fz.TRUSTED_KEYS_DOC).write_text(trusted)
    refused(root, "trusted-keys file")


def test_an_unexpected_error_during_the_check_is_a_refusal(tmp_path, monkeypatch):
    root, _ = approved_repo(tmp_path)
    monkeypatch.setattr(fz, "sha256_file", lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
    r = check(root)
    assert r["approved"] is False and any("failed safely (RuntimeError)" in p for p in r["problems"])


def test_approved_is_never_true_when_any_problem_is_listed(tmp_path):
    root, _ = approved_repo(tmp_path)
    good = rec_path(root).read_text()
    cases = ["null", "[]", "{", "", '{"status": "approved"}', good.replace("approved", "pending", 1), good.replace('"v1"', '""')]
    for c in cases:
        rec_path(root).write_text(c)
        r = check(root)
        assert r["approved"] is False and r["problems"]
    rec_path(root).write_text(good)
    assert check(root)["approved"] is True


def test_the_function_that_builds_an_approval_payload_creates_no_signature_and_no_key(tmp_path):
    root = make_repo(tmp_path)
    payload = fz.build_approval_payload(mf.sha256_file(root / mf.PROTOCOL_DOC), "reviewer", "v1", NOW)
    assert set(payload) == {"status", "version", "approved_by", "approved_at", "protocol_sha256"} and "signature" not in payload
    assert not hasattr(fz, "write_approval_record")
    with pytest.raises(ValueError):
        fz.build_approval_payload("a" * 64, "r", "v1", NOW.replace(tzinfo=None))
