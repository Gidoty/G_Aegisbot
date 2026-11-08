"""Guarded file access (backtest/safeio.py) and its use by the freeze-record and liquidation-bracket loaders.
All files live in pytest temporary directories; the secret-looking content is a fixed marker that must never come back."""
import json
import os

import pytest

from g_aegisbot.backtest import freeze as fz
from g_aegisbot.backtest import safeio
from g_aegisbot.backtest import signing as sg
from g_aegisbot.backtest.liquidation import LiquidationModel, load_liquidation_config
from g_aegisbot.backtest.protected import ProtectedPathError
from tests.signing_helpers import approved_repo, new_key, sign_freeze
from tests.test_acceptance_freeze import good_record, good_run

MARKER = "TOP-SECRET-MARKER-9f3a"
BRACKETS = [{"notional_cap": 100000, "mmr": 0.004, "cum": 0}, {"notional_cap": None, "mmr": 0.01, "cum": 300}]


def write(path, text):
    path.write_text(text)
    return path


# ------------------------------------------------------------------------------------------------ reads
@pytest.mark.parametrize("name", [".env", ".env.example", "api.key", "wallet.pem", "bot.db", "id_rsa"])
def test_protected_files_are_refused_before_any_byte_is_read(tmp_path, name):
    p = write(tmp_path / name, MARKER)
    with pytest.raises(ProtectedPathError) as err:
        safeio.read_bytes(p)
    assert MARKER not in str(err.value)


def test_a_file_under_a_protected_directory_is_refused(tmp_path):
    d = tmp_path / "secrets"
    d.mkdir()
    with pytest.raises(ProtectedPathError):
        safeio.read_json(write(d / "ok.json", "{}"))


def test_a_symlink_is_refused_even_to_an_innocent_file(tmp_path):
    real = write(tmp_path / "real.json", "{}")
    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(safeio.UnsafePathError):
        safeio.read_bytes(link)


def test_a_symlink_to_a_protected_file_is_refused(tmp_path):
    secret = write(tmp_path / ".env", MARKER)
    link = tmp_path / "innocent.json"
    link.symlink_to(secret)
    with pytest.raises(ProtectedPathError):
        safeio.read_bytes(link)


def test_a_hard_link_to_a_protected_file_is_refused(tmp_path):
    secret = write(tmp_path / ".env", MARKER)
    alias = tmp_path / "innocent.json"
    os.link(secret, alias)                                          # the name is harmless; the link count is not
    with pytest.raises(safeio.UnsafePathError) as err:
        safeio.read_bytes(alias)
    assert MARKER not in str(err.value)


def test_directories_and_missing_files_are_refused(tmp_path):
    with pytest.raises(safeio.UnsafePathError):
        safeio.read_bytes(tmp_path)
    with pytest.raises(safeio.UnsafePathError):
        safeio.read_bytes(tmp_path / "missing.json")


def test_the_size_limit_is_enforced(tmp_path):
    p = write(tmp_path / "big.json", "[" + "1," * 600 + "1]")
    assert safeio.read_bytes(p, 5000)
    with pytest.raises(safeio.UnsafePathError):
        safeio.read_bytes(p, 100)


def test_json_is_read_strictly(tmp_path):
    for text in ('{"a": 1, "a": 2}', '{"a": NaN}', '{"a": Infinity}', "not json", b"\xff\xfe".decode("latin-1")):
        with pytest.raises(sg.SigningError):
            safeio.read_json(write(tmp_path / "x.json", text))
    assert safeio.read_json(write(tmp_path / "ok.json", '{"a": [1, 2.5]}')) == {"a": [1, 2.5]}


# ------------------------------------------------------------------------------------------------ writes
def test_write_is_atomic_and_leaves_no_temporary_file(tmp_path):
    p = safeio.write_atomic(tmp_path / "out.json", '{"a": 1}')
    assert p.read_text() == '{"a": 1}' and [x.name for x in tmp_path.iterdir()] == ["out.json"]


def test_an_existing_file_is_never_overwritten_by_default(tmp_path):
    p = write(tmp_path / "frozen.json", "original")
    with pytest.raises(FileExistsError):
        safeio.write_atomic(p, "replacement")
    assert p.read_text() == "original" and [x.name for x in tmp_path.iterdir()] == ["frozen.json"]
    safeio.write_atomic(p, "replacement", overwrite=True)
    assert p.read_text() == "replacement"


def test_writing_to_a_protected_path_is_refused_and_nothing_is_created(tmp_path):
    for name in (".env", ".env.example", "x.key", "x.db"):
        with pytest.raises(ProtectedPathError):
            safeio.write_atomic(tmp_path / name, "x", overwrite=True)
    assert list(tmp_path.iterdir()) == []


def test_writing_through_a_symlink_is_refused_and_the_target_is_untouched(tmp_path):
    secret = write(tmp_path / ".env", MARKER)
    link = tmp_path / "freeze.json"
    link.symlink_to(secret)
    with pytest.raises((safeio.UnsafePathError, ProtectedPathError)):
        safeio.write_atomic(link, "overwritten", overwrite=True)
    assert secret.read_text() == MARKER


def test_a_failed_write_leaves_no_partial_file(tmp_path, monkeypatch):
    p = write(tmp_path / "keep.json", "original")
    monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        safeio.write_atomic(p, "new", overwrite=True)
    assert p.read_text() == "original" and [x.name for x in tmp_path.iterdir()] == ["keep.json"]


# ------------------------------------------------------------------------------------------------ freeze record loader and writer
def test_freeze_round_trip_and_the_second_write_refuses(tmp_path):
    r = good_record()
    p = fz.write_freeze_record(tmp_path / "freeze.json", r)
    assert fz.load_freeze_record(p) == r
    with pytest.raises(FileExistsError):
        fz.write_freeze_record(p, good_record(declaration={"by": "x", "date": "d", "statement": "other"}))
    assert fz.load_freeze_record(p) == r                            # the frozen record is intact


def test_freeze_write_refuses_a_record_that_cannot_be_signed(tmp_path):
    bad = dict(good_record(), extra=float("nan"))
    with pytest.raises(sg.SigningError):
        fz.write_freeze_record(tmp_path / "freeze.json", bad)
    assert list(tmp_path.iterdir()) == []


def test_freeze_write_refuses_protected_targets(tmp_path):
    with pytest.raises(ProtectedPathError):
        fz.write_freeze_record(tmp_path / ".env", good_record(), overwrite=True)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("name", [".env", ".env.example", "freeze.key", "freeze.db"])
def test_freeze_load_refuses_protected_files(tmp_path, name):
    p = write(tmp_path / name, MARKER)
    with pytest.raises(ProtectedPathError) as err:
        fz.load_freeze_record(p)
    assert MARKER not in str(err.value)


@pytest.mark.parametrize("text", ["[]", "null", "5", '"text"', '{"a": 1, "a": 2}', '{"a": NaN}', "{"])
def test_freeze_load_refuses_anything_that_is_not_one_strict_object(tmp_path, text):
    with pytest.raises(ValueError):
        fz.load_freeze_record(write(tmp_path / "f.json", text))


def test_freeze_load_refuses_symlinks_and_oversized_files(tmp_path):
    real = write(tmp_path / "real.json", json.dumps(good_record()))
    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(ValueError):
        fz.load_freeze_record(link)
    big = write(tmp_path / "big.json", json.dumps({"pad": "x" * (fz.MAX_RECORD_BYTES + 10)}))
    with pytest.raises(ValueError):
        fz.load_freeze_record(big)


# ------------------------------------------------------------------------------------------------ the combined freeze check
def test_freeze_problems_includes_the_signature_check(tmp_path):
    root, key = approved_repo(tmp_path)
    unsigned = good_record()
    assert fz.validate_freeze_record(unsigned, good_run()) == []     # structure alone says nothing about the signature
    problems = fz.freeze_problems(unsigned, good_run(), root)
    assert problems and any("signature" in p for p in problems)
    assert fz.freeze_problems(sign_freeze(unsigned, key), good_run(), root) == []


def test_freeze_problems_rejects_a_signature_by_an_untrusted_key_and_an_edited_record(tmp_path):
    root, key = approved_repo(tmp_path)
    assert fz.freeze_problems(sign_freeze(good_record(), new_key()), None, root)
    signed = sign_freeze(good_record(), key)
    signed["universe"] = signed["universe"] + ["SOL/USDT:USDT"]
    assert fz.freeze_problems(signed, None, root)


def test_freeze_problems_without_any_pinned_key_never_validates(tmp_path):
    from tests.signing_helpers import make_repo
    root = make_repo(tmp_path)
    assert fz.freeze_problems(sign_freeze(good_record(), new_key()), None, root)


# ------------------------------------------------------------------------------------------------ liquidation brackets
def test_liquidation_file_loads_through_the_guard(tmp_path):
    p = write(tmp_path / "brackets.json", json.dumps(BRACKETS))
    default, per_symbol = load_liquidation_config(p)
    assert default.brackets[1].mmr == 0.01 and per_symbol == {}
    assert LiquidationModel.from_file(p).brackets[0].notional_cap == 100000
    default, per_symbol = load_liquidation_config(write(tmp_path / "map.json", json.dumps({"default": BRACKETS, "BTC/USDT:USDT": BRACKETS})))
    assert set(per_symbol) == {"BTC/USDT:USDT"}


@pytest.mark.parametrize("name", [".env", ".env.example", "brackets.key", "brackets.db"])
def test_liquidation_loader_refuses_protected_files(tmp_path, name):
    p = write(tmp_path / name, MARKER)
    for loader in (load_liquidation_config, LiquidationModel.from_file):
        with pytest.raises(ProtectedPathError) as err:
            loader(p)
        assert MARKER not in str(err.value)


def test_liquidation_loader_refuses_links_oversized_and_non_strict_files(tmp_path):
    real = write(tmp_path / "real.json", json.dumps(BRACKETS))
    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(ValueError):
        load_liquidation_config(link)
    with pytest.raises(ValueError):
        load_liquidation_config(write(tmp_path / "big.json", "[" + "0," * (safeio.MAX_JSON_BYTES // 2) + "0]"))
    for text in ('[{"mmr": NaN}]', '[{"mmr": 0.1, "mmr": 0.2}]', "{", "5", "null"):
        with pytest.raises(ValueError):
            load_liquidation_config(write(tmp_path / "x.json", text))


def test_malformed_rows_give_a_fixed_message_that_does_not_echo_the_file(tmp_path):
    for rows in ([{"mmr": MARKER}], [{"notional_cap": MARKER, "mmr": 0.1}], [{"mmr": 0.1, "cum": MARKER}], [MARKER], [{}],
                 {"default": MARKER}):
        with pytest.raises(ValueError) as err:
            load_liquidation_config(write(tmp_path / "x.json", json.dumps(rows)))
        assert MARKER not in str(err.value)
