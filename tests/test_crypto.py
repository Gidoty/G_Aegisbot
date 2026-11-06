import pytest

from g_aegisbot.security.crypto import CryptoError, SecretBox, generate_key


def test_roundtrip_and_not_plaintext(box):
    token = box.encrypt("my-secret")
    assert "my-secret" not in token
    assert box.decrypt(token) == "my-secret"


def test_wrong_key_fails():
    token = SecretBox([generate_key()]).encrypt("x")
    with pytest.raises(CryptoError):
        SecretBox([generate_key()]).decrypt(token)


def test_rotation():
    old, new = generate_key(), generate_key()
    token = SecretBox([old]).encrypt("x")
    rotated = SecretBox([new, old]).rotate(token)
    assert SecretBox([new]).decrypt(rotated) == "x"


def test_missing_or_bad_key():
    with pytest.raises(CryptoError):
        SecretBox([])
    with pytest.raises(CryptoError):
        SecretBox(["not-a-key"])
