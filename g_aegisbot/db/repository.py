"""All reads and writes of tenant data go through here so encryption and limits are enforced once."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from g_aegisbot.config import MAX_PROFILES
from g_aegisbot.db.models import UserProfile
from g_aegisbot.security.crypto import SecretBox


class ProfileLimitError(Exception):
    pass


class ProfileRepository:
    def __init__(self, session: Session, box: SecretBox):
        self.session = session
        self.box = box

    def count(self) -> int:
        return self.session.scalar(select(func.count()).select_from(UserProfile)) or 0

    def get(self, user_id: str) -> UserProfile | None:
        return self.session.scalar(select(UserProfile).where(UserProfile.user_id == user_id))

    def list_all(self) -> list[UserProfile]:
        return list(self.session.scalars(select(UserProfile).order_by(UserProfile.id)))

    def create(self, user_id: str, *, is_admin: bool = False, **fields) -> UserProfile:
        if self.count() >= MAX_PROFILES:
            raise ProfileLimitError(f"Profile limit reached ({MAX_PROFILES}).")
        profile = UserProfile(user_id=user_id, is_admin=is_admin, **fields)
        self.session.add(profile)
        self.session.commit()
        return profile

    def set_credentials(self, profile: UserProfile, api_key: str, api_secret: str) -> None:
        profile.encrypted_api_key = self.box.encrypt(api_key)
        profile.encrypted_api_secret = self.box.encrypt(api_secret)
        self.session.commit()

    def get_credentials(self, profile: UserProfile) -> tuple[str, str]:
        if not profile.has_credentials:
            raise ValueError(f"Profile {profile.user_id!r} has no API credentials.")
        return (
            self.box.decrypt(profile.encrypted_api_key),
            self.box.decrypt(profile.encrypted_api_secret),
        )

    def set_subscription(
        self, profile: UserProfile, status: str, expires_at: datetime | None
    ) -> None:
        profile.subscription_status = status
        profile.subscription_expires_at = expires_at
        self.session.commit()

    def set_wallet_cap(self, profile: UserProfile, cap_usdt: Decimal) -> None:
        profile.max_wallet_cap_usdt = cap_usdt
        self.session.commit()
