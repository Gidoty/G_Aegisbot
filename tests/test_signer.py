"""The offline signer: custody rules and the full approve -> verify path, with throw-away keys in temporary directories."""
import json
import os
import stat

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from g_aegisbot.backtest import freeze as fz
from g_aegisbot.backtest import manifest as mf
from g_aegisbot.backtest import signer
from g_aegisbot.backtest import signing as sg
from tests.signing_helpers import make_repo
from tests.test_acceptance_freeze import good_record, good_run

PASS = "correct horse battery staple"


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path)


@pytest.fixture
def keyfile(tmp_path, repo):
    path = tmp_path / "keys" / "approver.pem"
    signer.generate_key(path, PASS, repo_root=repo)
    return path


def pin(repo, key, **kw):
    entry = signer.public_entry(key, label="owner", **kw)
    (repo / fz.TRUSTED_KEYS_DOC).write_text(json.dumps({"version": 1, "keys": [entry]}))
    return entry


# ------------------------------------------------------------------------------------------------ custody
def test_the_key_is_stored_encrypted_with_owner_only_permissions(keyfile):
    pem = keyfile.read_bytes()
    assert b"ENCRYPTED PRIVATE KEY" in pem and stat.S_IMODE(os.stat(keyfile).st_mode) == 0o600


def test_a_key_is_never_overwritten(repo, keyfile):
    before = keyfile.read_bytes()
    with pytest.raises(signer.SignerError, match="already exists"):
        signer.generate_key(keyfile, PASS, repo_root=repo)
    assert keyfile.read_bytes() == before


def test_a_weak_passphrase_is_refused_and_creates_nothing(tmp_path, repo):
    with pytest.raises(signer.SignerError, match="passphrase"):
        signer.generate_key(tmp_path / "k.pem", "short", repo_root=repo)
    assert not (tmp_path / "k.pem").exists()


def test_a_key_inside_the_repository_is_refused_to_create_and_to_read(tmp_path, repo, keyfile):
    with pytest.raises(signer.SignerError, match="inside the repository"):
        signer.generate_key(repo / "docs" / "approver.pem", PASS, repo_root=repo)
    assert not (repo / "docs" / "approver.pem").exists()
    inside = repo / "approver.pem"
    inside.write_bytes(keyfile.read_bytes())
    os.chmod(inside, 0o600)
    with pytest.raises(signer.SignerError, match="inside the repository"):
        signer.load_key(inside, PASS, repo_root=repo)


def test_wrong_passphrase_and_loose_permissions_and_wrong_key_type_are_refused(tmp_path, repo, keyfile):
    with pytest.raises(signer.SignerError, match="could not be opened"):
        signer.load_key(keyfile, "not the passphrase at all", repo_root=repo)
    os.chmod(keyfile, 0o644)
    with pytest.raises(signer.SignerError, match="readable by other users"):
        signer.load_key(keyfile, PASS, repo_root=repo)
    os.chmod(keyfile, 0o600)
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    rsa_pem = tmp_path / "rsa.pem"
    rsa_pem.write_bytes(rsa.generate_private_key(65537, 2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.BestAvailableEncryption(PASS.encode())))
    os.chmod(rsa_pem, 0o600)
    with pytest.raises(signer.SignerError, match="not an Ed25519"):
        signer.load_key(rsa_pem, PASS, repo_root=repo)


def test_a_symlinked_key_path_is_refused(tmp_path, repo, keyfile):
    link = tmp_path / "link.pem"
    link.symlink_to(keyfile)
    with pytest.raises(signer.SignerError):
        signer.load_key(link, PASS, repo_root=repo)


def test_the_public_entry_is_valid_for_the_trusted_key_parser_and_holds_no_private_material(repo, keyfile):
    key = signer.load_key(keyfile, PASS, repo_root=repo)
    entry = signer.public_entry(key, label="owner", years=1)
    trusted = sg.parse_trusted_keys({"version": 1, "keys": [entry]})
    assert entry["key_id"] in trusted.keys and entry["role"] == "approver" and entry["revoked"] is False
    assert set(entry) == {"key_id", "public_key", "role", "valid_from", "valid_until", "revoked", "label"}
    assert PASS not in json.dumps(entry)


# ------------------------------------------------------------------------------------------------ approval end to end
def test_a_confirmed_approval_is_accepted_by_the_validator(repo, keyfile):
    key = signer.load_key(keyfile, PASS, repo_root=repo)
    pin(repo, key)
    sha = mf.sha256_file(repo / mf.PROTOCOL_DOC)
    record = signer.make_signed_approval(key, repo_root=repo, approved_by="owner", version="v1.0", confirm=sha[:8])
    (repo / fz.APPROVAL_DOC).write_text(json.dumps(record))
    result = fz.validate_protocol_approval(repo)
    assert result["approved"] is True and result["signed_by"] == record["signature"]["key_id"]


@pytest.mark.parametrize("confirm", ["", "deadbeef", "yes", "  "])
def test_a_wrong_confirmation_signs_nothing(repo, keyfile, confirm):
    key = signer.load_key(keyfile, PASS, repo_root=repo)
    with pytest.raises(signer.SignerError, match="confirmation"):
        signer.make_signed_approval(key, repo_root=repo, approved_by="owner", version="v1", confirm=confirm)


def test_changing_the_protocol_after_signing_withdraws_the_approval(repo, keyfile):
    key = signer.load_key(keyfile, PASS, repo_root=repo)
    pin(repo, key)
    sha = mf.sha256_file(repo / mf.PROTOCOL_DOC)
    (repo / fz.APPROVAL_DOC).write_text(json.dumps(
        signer.make_signed_approval(key, repo_root=repo, approved_by="owner", version="v1", confirm=sha[:8])))
    assert fz.validate_protocol_approval(repo)["approved"] is True
    (repo / mf.PROTOCOL_DOC).write_text("protocol text changed after approval")
    assert fz.validate_protocol_approval(repo)["approved"] is False


def test_a_key_that_is_not_pinned_does_not_approve(repo, keyfile):
    key = signer.load_key(keyfile, PASS, repo_root=repo)
    sha = mf.sha256_file(repo / mf.PROTOCOL_DOC)
    (repo / fz.APPROVAL_DOC).write_text(json.dumps(
        signer.make_signed_approval(key, repo_root=repo, approved_by="owner", version="v1", confirm=sha[:8])))
    assert fz.validate_protocol_approval(repo)["approved"] is False                  # no trusted-keys file at all


# ------------------------------------------------------------------------------------------------ freeze
def test_a_signed_freeze_record_passes_the_combined_check(tmp_path, repo, keyfile):
    key = signer.load_key(keyfile, PASS, repo_root=repo)
    pin(repo, key)
    unsigned = fz.write_freeze_record(tmp_path / "unsigned.json", good_record())
    signed = signer.make_signed_freeze(key, unsigned)
    assert fz.freeze_problems(signed, good_run(), repo) == []


def test_an_invalid_or_already_signed_freeze_record_is_not_signed(tmp_path, repo, keyfile):
    key = signer.load_key(keyfile, PASS, repo_root=repo)
    bad = dict(good_record(), freeze_sha256="0" * 64)
    with pytest.raises(signer.SignerError, match="not valid"):
        signer.make_signed_freeze(key, fz.write_freeze_record(tmp_path / "bad.json", bad))
    done = sg.sign_record(good_record(), key, sg.PURPOSE_FREEZE)
    (tmp_path / "done.json").write_text(json.dumps(done))
    with pytest.raises(signer.SignerError, match="already signed"):
        signer.make_signed_freeze(key, tmp_path / "done.json")


def test_an_approval_signature_cannot_be_replayed_as_a_freeze_signature(repo, keyfile):
    key = signer.load_key(keyfile, PASS, repo_root=repo)
    pin(repo, key)
    record = sg.sign_record(good_record(), key, sg.PURPOSE_APPROVAL)
    assert fz.freeze_problems(record, None, repo)


# ------------------------------------------------------------------------------------------------ command line
def test_the_command_line_end_to_end(tmp_path, repo, monkeypatch, capsys):
    answers = iter([PASS, PASS])
    monkeypatch.setattr(signer.getpass, "getpass", lambda prompt="": next(answers))
    key_path = tmp_path / "cli" / "approver.pem"
    assert signer.main(["keygen", "--out", str(key_path), "--label", "owner"], repo_root=repo) == 0
    entry = json.loads(capsys.readouterr().out.split("commit it yourself", 1)[1].split(":\n", 1)[1])
    (repo / fz.TRUSTED_KEYS_DOC).write_text(json.dumps({"version": 1, "keys": [entry]}))

    monkeypatch.setattr(signer.getpass, "getpass", lambda prompt="": PASS)
    sha = mf.sha256_file(repo / mf.PROTOCOL_DOC)
    monkeypatch.setattr("builtins.input", lambda prompt="": sha[:8])
    assert signer.main(["sign-approval", "--key", str(key_path), "--approved-by", "owner", "--version", "v1"], repo_root=repo) == 0
    assert fz.validate_protocol_approval(repo)["approved"] is True
    assert signer.main(["verify", "--purpose", "approval", "--in", str(repo / fz.APPROVAL_DOC)], repo_root=repo) == 0
    assert "VERIFIED" in capsys.readouterr().out

    assert signer.main(["sign-approval", "--key", str(key_path), "--approved-by", "owner", "--version", "v2"], repo_root=repo) == 2
    assert "already exists" in capsys.readouterr().err                                     # the approval file is not overwritten


def test_verify_reports_a_tampered_record(tmp_path, repo, keyfile, monkeypatch, capsys):
    key = signer.load_key(keyfile, PASS, repo_root=repo)
    pin(repo, key)
    sha = mf.sha256_file(repo / mf.PROTOCOL_DOC)
    record = signer.make_signed_approval(key, repo_root=repo, approved_by="owner", version="v1", confirm=sha[:8])
    record["approved_by"] = "someone else"
    (tmp_path / "r.json").write_text(json.dumps(record))
    assert signer.main(["verify", "--purpose", "approval", "--in", str(tmp_path / "r.json")], repo_root=repo) == 1
    assert "NOT VERIFIED" in capsys.readouterr().out


def test_the_signer_is_not_part_of_the_backtest_code_identity():
    assert not any("signer" in m for m in mf.source_tree_sha256()["modules"])


def test_no_private_key_exists_in_the_repository_tree():
    # tests create keys only in temporary directories; the repository must never hold a private key
    root = mf.REPO_ROOT
    assert [p for d in ("g_aegisbot", "docs", "tests") for p in (root / d).rglob("*.pem")] == []


# ------------------------------------------------------------------------------------------------ live authorisation
def live_unsigned(tmp_path, repo, key):
    from dataclasses import replace  # noqa: F401
    from datetime import datetime, timezone
    from g_aegisbot.engine import livegate as lg
    from g_aegisbot.engine.strategy import StrategyParams
    pin(repo, key)
    sha = mf.sha256_file(repo / mf.PROTOCOL_DOC)
    (repo / fz.APPROVAL_DOC).write_text(json.dumps(signer.make_signed_approval(key, repo_root=repo, approved_by="o", version="v", confirm=sha[:8])))
    rec = lg.build_payload(params=StrategyParams(), drawdown_limit=0.04, candidate="CAND-1", version="v1", authorised_by="owner",
                           authorised_at=datetime.now(timezone.utc).replace(microsecond=0), valid_days=30, freeze_sha256="a" * 64,
                           acceptance_report_sha256="b" * 64, paper_soak_days=30, max_wallet_cap_usdt=200.0, max_leverage=3,
                           protocol_sha256=sha, code_sha256="c" * 64)
    path = tmp_path / "live_unsigned.json"
    path.write_text(json.dumps(rec))
    return path


def test_a_confirmed_live_authorisation_verifies_for_its_own_purpose_only(tmp_path, repo, keyfile):
    key = signer.load_key(keyfile, PASS, repo_root=repo)
    path = live_unsigned(tmp_path, repo, key)
    signed = signer.make_signed_live_authorization(key, path, confirm="CAND-1")
    trusted = fz.trusted_keys_for_repo(repo)
    assert sg.verify_record(signed, sg.PURPOSE_LIVE, trusted) == []
    assert sg.verify_record(signed, sg.PURPOSE_APPROVAL, trusted) and sg.verify_record(signed, sg.PURPOSE_FREEZE, trusted)


@pytest.mark.parametrize("confirm", ["", "cand-1", "yes", "CAND-2"])
def test_a_wrong_candidate_confirmation_signs_no_live_authorisation(tmp_path, repo, keyfile, confirm):
    key = signer.load_key(keyfile, PASS, repo_root=repo)
    with pytest.raises(signer.SignerError, match="confirmation"):
        signer.make_signed_live_authorization(key, live_unsigned(tmp_path, repo, key), confirm=confirm)


def test_an_invalid_or_already_signed_live_authorisation_is_not_signed(tmp_path, repo, keyfile):
    key = signer.load_key(keyfile, PASS, repo_root=repo)
    path = live_unsigned(tmp_path, repo, key)
    rec = json.loads(path.read_text())
    rec["paper_soak_days"] = 3
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(rec))
    with pytest.raises(signer.SignerError, match="not valid"):
        signer.make_signed_live_authorization(key, bad, confirm="CAND-1")
    signed = tmp_path / "signed.json"
    signed.write_text(json.dumps(signer.make_signed_live_authorization(key, path, confirm="CAND-1")))
    with pytest.raises(signer.SignerError, match="already signed"):
        signer.make_signed_live_authorization(key, signed, confirm="CAND-1")
    (tmp_path / "list.json").write_text("[]")
    with pytest.raises(signer.SignerError):
        signer.make_signed_live_authorization(key, tmp_path / "list.json", confirm="x")


@pytest.mark.real_gate
def test_a_signed_live_authorisation_passes_the_gate_with_matching_code_and_params(tmp_path, repo, keyfile):
    from g_aegisbot.engine import livegate as lg
    from g_aegisbot.engine.strategy import StrategyParams
    key = signer.load_key(keyfile, PASS, repo_root=repo)
    signed = signer.make_signed_live_authorization(key, live_unsigned(tmp_path, repo, key), confirm="CAND-1")
    (repo / lg.LIVE_AUTH_DOC).write_text(json.dumps(signed))
    res = lg.evaluate(params=StrategyParams(), repo_root=repo, env={lg.LIVE_ENV: "1"}, code_identity=lambda: ("c" * 64, True),
                      check_switch=True)
    assert res.allowed, res.problems
    assert "G3 record" in res.checks and "stub" not in res.checks      # the real gate ran
