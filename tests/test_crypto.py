"""crypto 单测：bcrypt + JWT。"""
from datetime import timedelta

from app.utils import crypto


def test_password_roundtrip():
    h = crypto.hash_password("admin123")
    assert crypto.verify_password("admin123", h) is True
    assert crypto.verify_password("wrong", h) is False


def test_jwt_roundtrip():
    t = crypto.create_token("admin", "secret123", timedelta(minutes=15), "access")
    payload = crypto.decode_token(t, "secret123")
    assert payload["sub"] == "admin"
    assert payload["type"] == "access"
