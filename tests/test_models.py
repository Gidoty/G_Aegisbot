from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from g_aegisbot.config import DEFAULT_COINS
from g_aegisbot.db.models import UserProfile
from g_aegisbot.db.repository import ProfileLimitError, ProfileRepository


def test_defaults(session, box):
    p = ProfileRepository(session, box).create("alice")
    assert p.market_type == "futures"
    assert p.trading_mode == "testnet"
    assert p.virtual_balance == Decimal("10000")
    assert p.leverage == 3
    assert p.active_coins == list(DEFAULT_COINS)
    assert p.subscription_status == "expired"
    assert p.daily_loss_tracker["breaker_tripped"] is False


def test_testnet_cap_defaults_to_reference_balance(session, box):
    repo = ProfileRepository(session, box)
    assert repo.create("t").max_wallet_cap_usdt == Decimal("10000")


def test_credentials_encrypted_at_rest(session, box):
    repo = ProfileRepository(session, box)
    p = repo.create("alice")
    repo.set_credentials(p, "KEY123", "SECRET456", verified=False)
    assert "KEY123" not in p.encrypted_api_key and "SECRET456" not in p.encrypted_api_secret
    assert repo.get_credentials(p) == ("KEY123", "SECRET456")
    assert "KEY123" not in repr(p)


def test_profile_limit_is_11(session, box):
    repo = ProfileRepository(session, box)
    repo.create("admin", is_admin=True)
    for i in range(10):
        repo.create(f"user{i}")
    with pytest.raises(ProfileLimitError):
        repo.create("one_too_many")


def test_leverage_bounds(session, box):
    repo = ProfileRepository(session, box)
    p = repo.create("a")
    p.leverage = 5
    p.leverage = 1
    for bad in (0, 6, 10):
        with pytest.raises(ValueError):
            p.leverage = bad


def test_coin_validation(session, box):
    p = ProfileRepository(session, box).create("a")
    with pytest.raises(ValueError):
        p.active_coins = [f"C{i}/USDT:USDT" for i in range(16)]
    with pytest.raises(ValueError):
        p.active_coins = ["BTC/USDT"]  # spot format
    with pytest.raises(ValueError):
        p.active_coins = ["BTC/USDT:USDT", "BTC/USDT:USDT"]


def test_mode_and_status_validation(session, box):
    p = ProfileRepository(session, box).create("a")
    for attr, bad in (("trading_mode", "paper"), ("market_type", "spot"), ("subscription_status", "trial")):
        with pytest.raises(ValueError):
            setattr(p, attr, bad)


def test_db_constraints_back_up_validators(session):
    session.add(UserProfile(user_id="x", leverage=3))
    session.commit()
    with pytest.raises(IntegrityError):
        session.execute(UserProfile.__table__.update().values(leverage=9))


def test_subscription_logic(session, box):
    repo = ProfileRepository(session, box)
    p = repo.create("a")
    assert not p.subscription_is_active()
    repo.set_subscription(p, "active", datetime.now(timezone.utc) + timedelta(days=30))
    assert p.subscription_is_active()
    repo.set_subscription(p, "active", datetime.now(timezone.utc) - timedelta(seconds=1))
    assert not p.subscription_is_active()
    assert repo.create("boss", is_admin=True).subscription_is_active()


def test_loss_tracker_mutation_persists(session, box):
    p = ProfileRepository(session, box).create("a")
    p.daily_loss_tracker["realized_pnl"] = -120.5
    session.commit()
    session.expire_all()
    assert session.get(UserProfile, p.id).daily_loss_tracker["realized_pnl"] == -120.5
