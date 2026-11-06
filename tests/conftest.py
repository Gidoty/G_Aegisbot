import pytest
from sqlalchemy import create_engine

from g_aegisbot.db.base import Base
from g_aegisbot.db.session import get_session_factory
from g_aegisbot.security.crypto import SecretBox, generate_key


@pytest.fixture
def box():
    return SecretBox([generate_key()])


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", future=True)
    from g_aegisbot.db import models  # noqa: F401
    Base.metadata.create_all(engine)
    with get_session_factory(engine)() as s:
        yield s
