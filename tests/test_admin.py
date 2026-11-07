import io
from datetime import datetime, timedelta, timezone

import ccxt.async_support as ccxt
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select, text

from g_aegisbot import audit
from g_aegisbot.admin import Admin, build_parser, run
from g_aegisbot.db import get_session_factory, init_db
from g_aegisbot.db.models import UserProfile
from g_aegisbot.db.trading_models import OpenTrade
from g_aegisbot.exchange.key_check import KeyCheck, check_api_key, interpret_restrictions
from g_aegisbot.security.crypto import SecretBox, generate_key

NOW = datetime(2026, 3, 5, 12, 0, tzinfo=timezone.utc)


class TTY(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture
def env(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/a.db")
    init_db(engine)
    sf = get_session_factory(engine)
    lines: list[str] = []
    box = SecretBox([generate_key()])
    admin = Admin(sf, box, out=lines.append, stdin=io.StringIO("KEY123456\nSECRET987654\n"),
                  now=lambda: NOW, actor="tester", verify=lambda k, s: KeyCheck(True))
    return dict(sf=sf, box=box, lines=lines, admin=admin, engine=engine)


def do(env, *argv):
    env["lines"].clear()
    code = run(list(argv), env["admin"])
    return code, "\n".join(env["lines"])


def profile(env, user):
    with env["sf"]() as s:
        return s.scalar(select(UserProfile).where(UserProfile.user_id == user))


# ------------------------------------------------------------------ users
def test_create_user_defaults_and_listing_hides_secrets(env):
    code, out = do(env, "create-user", "alice", "--days", "30")
    assert code == 0 and "Created 'alice'" in out
    p = profile(env, "alice")
    assert p.trading_mode == "testnet" and p.leverage == 3 and p.subscription_status == "active"
    assert float(p.max_wallet_cap_usdt) == 10000                      # testnet cap defaults to the paper balance
    _, out = do(env, "list")
    assert "alice" in out and "none" in out and "1 profile(s)" in out


def test_create_live_user_is_blocked_until_a_cap_is_set(env):
    _, out = do(env, "create-user", "bob", "--mode", "live")
    assert "wallet cap of 0" in out and float(profile(env, "bob").max_wallet_cap_usdt) == 0


def test_duplicate_limit_and_bad_input_give_clear_errors(env):
    do(env, "create-user", "alice")
    code, out = do(env, "create-user", "alice")
    assert code == 1 and "already exists" in out
    code, out = do(env, "create-user", "x", "--leverage", "9")
    assert code == 1 and "leverage" in out.lower()
    code, out = do(env, "create-user", "y", "--coins", "BTC/USDT")
    assert code == 1 and "USDT-margined perpetual" in out
    for i in range(10):
        assert do(env, "create-user", f"u{i}")[0] == 0
    code, out = do(env, "create-user", "overflow")
    assert code == 1 and "limit reached (11)" in out


def test_unknown_user_is_reported(env):
    code, out = do(env, "show", "ghost")
    assert code == 1 and "No profile named 'ghost'" in out


# ------------------------------------------------------------------ api keys
def test_set_keys_encrypts_never_prints_and_audits(env):
    do(env, "create-user", "alice")
    code, out = do(env, "set-keys", "alice")
    assert code == 0 and "Stored encrypted API keys" in out and "not verified" in out
    assert "KEY123456" not in out and "SECRET987654" not in out
    with env["engine"].connect() as c:                                 # raw database content is ciphertext
        raw = c.execute(text("select encrypted_api_key, encrypted_api_secret from user_profiles")).one()
    assert "KEY123456" not in raw[0] and "SECRET987654" not in raw[1]
    assert env["box"].decrypt(raw[0]) == "KEY123456" and env["box"].decrypt(raw[1]) == "SECRET987654"
    _, out = do(env, "show", "alice")
    assert "stored (encrypted)" in out and "KEY123456" not in out
    entries = audit.recent(env["sf"])
    assert any(e.action == "set-keys" and e.target == "alice" for e in entries)
    assert all("KEY123456" not in (e.detail or "") and "SECRET987654" not in (e.detail or "") for e in entries)


def test_set_keys_interactive_prompt_and_empty_input(env):
    do(env, "create-user", "alice")
    answers = iter(["K", "S"])
    env["admin"].stdin = TTY()
    env["admin"].ask_secret = lambda prompt: next(answers)
    assert do(env, "set-keys", "alice")[0] == 0
    env["admin"].ask_secret = lambda prompt: ""
    code, out = do(env, "set-keys", "alice")
    assert code == 1 and "required" in out


def test_verify_refuses_a_key_that_can_withdraw_and_stores_nothing(env):
    do(env, "create-user", "alice")
    env["admin"].verify = lambda k, s: KeyCheck(False, ["WITHDRAWALS ARE ENABLED on this key."])
    code, out = do(env, "set-keys", "alice", "--verify")
    assert code == 1 and "REFUSED: WITHDRAWALS ARE ENABLED" in out and "was not stored" in out
    assert not profile(env, "alice").has_credentials


def test_verify_passes_and_shows_warnings(env):
    do(env, "create-user", "alice")
    env["admin"].verify = lambda k, s: KeyCheck(True, warnings=["The key is not restricted to trusted IP addresses."])
    code, out = do(env, "set-keys", "alice", "--verify")
    assert code == 0 and "Warning: The key is not restricted" in out and "withdrawals disabled" in out
    assert profile(env, "alice").has_credentials


def test_secrets_cannot_be_passed_on_the_command_line():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["set-keys", "alice", "--key", "abc"])


def test_rotate_keys_reencrypts_with_the_newest_key(env):
    do(env, "create-user", "alice")
    do(env, "set-keys", "alice")
    old = env["box"]
    new_key = generate_key()
    old_key = Fernet.generate_key().decode()
    # data stored under the old key; the operator now runs with [new, old]
    with env["sf"]() as s:
        p = s.scalar(select(UserProfile))
        plain_k, plain_s = old.decrypt(p.encrypted_api_key), old.decrypt(p.encrypted_api_secret)
        legacy = SecretBox([old_key])
        p.encrypted_api_key, p.encrypted_api_secret = legacy.encrypt(plain_k), legacy.encrypt(plain_s)
        s.commit()
    env["admin"].box = SecretBox([new_key, old_key])
    code, out = do(env, "rotate-keys")
    assert code == 0 and "Re-encrypted 1 profile" in out
    p = profile(env, "alice")
    only_new = SecretBox([new_key])
    assert only_new.decrypt(p.encrypted_api_key) == "KEY123456" and only_new.decrypt(p.encrypted_api_secret) == "SECRET987654"


def test_commands_needing_the_master_key_explain_when_it_is_missing(env):
    do(env, "create-user", "alice")
    env["admin"].box = None
    code, out = do(env, "set-keys", "alice")
    assert code == 1 and "AEGIS_MASTER_KEYS is not set" in out


def test_gen_key_prints_a_valid_fernet_key(env):
    _, out = do(env, "gen-key")
    Fernet(out.splitlines()[0].encode())


# ------------------------------------------------------------------ settings
def test_cap_balance_leverage_coins_updates(env):
    do(env, "create-user", "alice")
    assert do(env, "set-cap", "alice", "2500.5")[0] == 0 and float(profile(env, "alice").max_wallet_cap_usdt) == 2500.5
    assert do(env, "set-balance", "alice", "7000")[0] == 0 and float(profile(env, "alice").virtual_balance) == 7000
    assert do(env, "set-leverage", "alice", "5")[0] == 0 and profile(env, "alice").leverage == 5
    code, out = do(env, "set-leverage", "alice", "6")
    assert code == 1 and "between 1 and 5" in out
    assert do(env, "set-coins", "alice", "BTC/USDT:USDT", "ETH/USDT:USDT")[0] == 0
    assert profile(env, "alice").active_coins == ["BTC/USDT:USDT", "ETH/USDT:USDT"]
    code, out = do(env, "set-coins", "alice", *[f"C{i}/USDT:USDT" for i in range(16)])
    assert code == 1 and "limited to 15" in out


def test_live_mode_needs_keys_a_cap_and_explicit_confirmation(env):
    do(env, "create-user", "alice", "--mode", "testnet")
    code, out = do(env, "set-mode", "alice", "live")
    assert code == 1 and "no API keys" in out and "wallet cap is 0" not in out      # testnet cap defaulted to 10000
    do(env, "set-keys", "alice")
    code, out = do(env, "set-mode", "alice", "live")
    assert code == 1 and "--yes-real-money" in out and profile(env, "alice").trading_mode == "testnet"
    code, out = do(env, "set-mode", "alice", "live", "--yes-real-money")
    assert code == 0 and "REAL funds" in out and "One-way" in out and profile(env, "alice").trading_mode == "live"
    assert do(env, "set-mode", "alice", "testnet")[0] == 0


def test_extend_and_expire(env):
    do(env, "create-user", "alice")
    do(env, "extend", "alice", "--days", "30")
    assert profile(env, "alice").subscription_expires_at.replace(tzinfo=timezone.utc) == NOW + timedelta(days=30)
    do(env, "extend", "alice", "--days", "10")                         # stacks on the remaining time
    assert profile(env, "alice").subscription_expires_at.replace(tzinfo=timezone.utc) == NOW + timedelta(days=40)
    do(env, "expire", "alice")
    assert profile(env, "alice").subscription_status == "expired"
    do(env, "extend", "alice", "--days", "5")                          # restarts from now
    assert profile(env, "alice").subscription_expires_at.replace(tzinfo=timezone.utc) == NOW + timedelta(days=5)
    assert do(env, "extend", "alice", "--days", "0")[0] == 1


# ------------------------------------------------------------------ kill switch
def test_kill_pause_flatten_and_resume(env):
    do(env, "create-user", "alice")
    code, out = do(env, "kill", "--reason", "news event")
    assert code == 0 and "PAUSE set for ALL accounts" in out
    code, out = do(env, "kill", "--flatten", "--reason", "drill")
    assert "FLATTEN set for ALL accounts" in out and "runner must be running" in out
    _, out = do(env, "status")
    assert "KILL SWITCH: global=FLATTEN (drill)" in out
    _, out = do(env, "resume")
    assert "Cleared" in out
    _, out = do(env, "status")
    assert "KILL SWITCH: off" in out


def test_kill_one_user_and_unknown_user(env):
    do(env, "create-user", "alice")
    assert do(env, "kill", "--user", "alice", "--reason", "x")[0] == 0
    _, out = do(env, "show", "alice")
    assert "kill switch:   PAUSED" in out
    assert do(env, "kill", "--user", "ghost")[0] == 1
    do(env, "resume", "--user", "alice")
    assert "kill switch" not in do(env, "show", "alice")[1]


def test_status_counts_open_trades(env):
    do(env, "create-user", "alice")
    with env["sf"]() as s:
        s.add(OpenTrade(user_id="alice", symbol="BTC/USDT:USDT", side="buy", entry_price=1, stop_price=1,
                        extreme_price=1, leverage=3, mode="testnet"))
        s.commit()
    assert "Open managed trades: 1" in do(env, "status")[1]


def test_every_change_is_audited_and_the_log_is_readable(env):
    do(env, "create-user", "alice")
    do(env, "set-cap", "alice", "100")
    do(env, "kill", "--reason", "test")
    _, out = do(env, "audit", "--limit", "5")
    for word in ("create-user", "set-cap", "kill-pause", "tester"):
        assert word in out


def test_init_db_is_repeatable(env):
    assert do(env, "init-db")[0] == 0 and do(env, "init-db")[0] == 0


# ------------------------------------------------------------------ key restrictions
@pytest.mark.parametrize("r,ok,fragment", [
    ({"enableReading": True, "enableFutures": True, "enableWithdrawals": False, "ipRestrict": True}, True, None),
    ({"enableReading": True, "enableFutures": True, "enableWithdrawals": True, "ipRestrict": True}, False, "WITHDRAWALS"),
    ({"enableReading": True, "enableFutures": False, "enableWithdrawals": False, "ipRestrict": True}, False, "Futures"),
    ({"enableReading": False, "enableFutures": True, "enableWithdrawals": False, "ipRestrict": True}, False, "Reading"),
    ({"enableReading": "true", "enableFutures": "true", "enableWithdrawals": "false", "ipRestrict": "true"}, True, None),
])
def test_interpret_restrictions(r, ok, fragment):
    c = interpret_restrictions(r)
    assert c.ok is ok and (fragment is None or any(fragment in p for p in c.problems))


def test_missing_ip_restriction_and_transfers_are_warnings_not_failures():
    c = interpret_restrictions({"enableReading": True, "enableFutures": True, "enableWithdrawals": False,
                                "ipRestrict": False, "enableInternalTransfer": True})
    assert c.ok and len(c.warnings) == 2


class FakeKeyExchange:
    def __init__(self, restrictions=None, error=None):
        self.restrictions, self.error, self.closed = restrictions, error, False

    async def sapiGetAccountApiRestrictions(self):
        if self.error:
            raise self.error
        return self.restrictions

    async def fapiPrivateV2GetAccount(self):
        return {}

    async def close(self):
        self.closed = True


async def test_check_api_key_paths():
    good = {"enableReading": True, "enableFutures": True, "enableWithdrawals": False, "ipRestrict": True}
    assert (await check_api_key("k", "s", exchange=FakeKeyExchange(good))).ok
    bad = await check_api_key("k", "s", exchange=FakeKeyExchange(error=ccxt.AuthenticationError("Invalid API-key")))
    assert not bad.ok and "rejected the key" in bad.problems[0]
    with pytest.raises(ccxt.RequestTimeout):                           # a network problem is not a verdict on the key
        await check_api_key("k", "s", exchange=FakeKeyExchange(error=ccxt.RequestTimeout("t")))


# ------------------------------------------------------------------ bind-login (operator-side identity binding)
U1 = "5e1b2f0c-1111-4222-8333-444455556666"
U2 = "9a8b7c6d-5555-4666-8777-888899990000"


def test_bind_login_attaches_the_supabase_id_and_is_audited(env):
    do(env, "create-user", "root@example.com", "--admin")
    code, out = do(env, "bind-login", "root@example.com", U1)
    assert code == 0 and "now bound" in out
    assert profile(env, "root@example.com").auth_user_id == U1
    rows = [r for r in audit.recent(env["sf"], 10) if r.action == "bind-login"]
    assert len(rows) == 1 and rows[0].target == "root@example.com" and rows[0].actor == "tester"
    assert rows[0].detail == f"auth_user_id={U1}"                      # no key, secret, token or password in the record


def test_bind_login_normalises_the_uid_to_the_form_used_in_tokens(env):
    do(env, "create-user", "root@example.com", "--admin")
    code, _ = do(env, "bind-login", "root@example.com", f"  {U1.upper()}  ")
    assert code == 0 and profile(env, "root@example.com").auth_user_id == U1


def test_bind_login_never_overwrites_a_bound_profile(env):
    do(env, "create-user", "root@example.com", "--admin")
    do(env, "bind-login", "root@example.com", U1)
    code, out = do(env, "bind-login", "root@example.com", U2)
    assert code == 1 and "already bound" in out
    assert profile(env, "root@example.com").auth_user_id == U1
    assert len([r for r in audit.recent(env["sf"], 10) if r.action == "bind-login"]) == 1      # the refusal is not a bind


def test_bind_login_refuses_an_id_that_belongs_to_another_profile(env):
    do(env, "create-user", "a@example.com")
    do(env, "create-user", "b@example.com")
    do(env, "bind-login", "a@example.com", U1)
    code, out = do(env, "bind-login", "b@example.com", U1)
    assert code == 1 and "already bound to 'a@example.com'" in out
    assert profile(env, "b@example.com").auth_user_id is None


def test_the_database_itself_refuses_one_uid_on_two_profiles(env):
    """Even if the CLI check were bypassed, the unique index and the conditional update hold the line."""
    from g_aegisbot.api.identity import claim_profile
    do(env, "create-user", "a@example.com")
    do(env, "create-user", "b@example.com")
    pa, pb = profile(env, "a@example.com").id, profile(env, "b@example.com").id
    assert claim_profile(env["sf"], pa, U1) is True
    assert claim_profile(env["sf"], pb, U1) is False                    # unique index
    assert claim_profile(env["sf"], pa, U2) is False                    # already bound: the conditional update matches nothing
    assert profile(env, "a@example.com").auth_user_id == U1 and profile(env, "b@example.com").auth_user_id is None


@pytest.mark.parametrize("bad", ["", "has space", "x" * 65, "sb-one", "5e1b2f0c-1111-2222-3333", "not-a-uuid-at-all",
                                 "5e1b2f0c11114222833344445555666677", "'; DROP TABLE user_profiles; --"])
def test_bind_login_rejects_anything_that_is_not_a_uuid(env, bad):
    do(env, "create-user", "a@example.com")
    code, out = do(env, "bind-login", "a@example.com", bad)
    assert code == 1 and "UUID" in out and profile(env, "a@example.com").auth_user_id is None


def test_bind_login_unknown_profile(env):
    code, out = do(env, "bind-login", "ghost@example.com", U1)
    assert code == 1 and "No profile named" in out


def test_bind_login_output_and_audit_contain_no_secrets(env):
    do(env, "create-user", "root@example.com", "--admin")
    do(env, "set-keys", "root@example.com")                              # stores a (fake) API key and secret
    code, out = do(env, "bind-login", "root@example.com", U1)
    everything = out + " ".join(f"{r.actor} {r.action} {r.target} {r.detail}" for r in audit.recent(env["sf"], 20))
    assert code == 0 and "KEY123456" not in everything and "SECRET987654" not in everything


def test_bind_login_tells_the_operator_it_cannot_verify_that_the_uid_exists(env):
    do(env, "create-user", "root@example.com", "--admin")
    code, out = do(env, "bind-login", "root@example.com", U1)
    assert code == 0 and "cannot check that the UID exists in Supabase" in out and "service-role" in out
