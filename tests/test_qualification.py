"""M3 资格检查：service 单测（httpx mock）+ API 集成测试。"""
from __future__ import annotations

import json

import aiosqlite
import pytest

from app.services import qualification_service as qs


class _FakeResp:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"http {self.status_code}")


def _fake_post_factory():
    async def fake_post(self, url, json=None, headers=None, timeout=None):
        token = (json or {}).get("token", "")
        if token == "GOOD":
            return _FakeResp({"token_ok": True, "eligible": True, "reason": "eligible"})
        if token == "NOELIG":
            return _FakeResp({"token_ok": True, "eligible": False, "reason": "not-eligible",
                              "coupon_state": "not_eligible"})
        return _FakeResp({"token_ok": False, "eligible": False, "reason": "token-401"})
    return fake_post


@pytest.fixture()
def _mock_http(monkeypatch):
    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post_factory())


async def _memdb():
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.execute(
        "CREATE TABLE credentials (id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT, password TEXT,"
        " twofa_secret TEXT, access_token TEXT, source_file TEXT, status TEXT DEFAULT 'pending',"
        " error_msg TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
        " updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    )
    await db.execute(
        "CREATE TABLE qualification_checks (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " credential_id INTEGER NOT NULL, at_token TEXT NOT NULL, has_qualification BOOLEAN NOT NULL,"
        " raw_response TEXT, checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    )
    await db.commit()
    return db


async def test_check_single_eligible(_mock_http):
    svc = qs.QualificationService(db=None, api_url="https://x", api_key="")
    out = await svc.check_single("GOOD")
    assert out.has_qualification is True
    assert out.reason == "eligible"


async def test_check_single_not_eligible(_mock_http):
    svc = qs.QualificationService(db=None, api_url="https://x", api_key="")
    out = await svc.check_single("NOELIG")
    assert out.has_qualification is False
    assert out.reason == "not-eligible"


async def test_check_single_bad_token(_mock_http):
    svc = qs.QualificationService(db=None, api_url="https://x", api_key="")
    out = await svc.check_single("BAD")
    assert out.has_qualification is False
    assert out.reason == "token-401"


async def test_check_single_empty_token_no_http(_mock_http):
    svc = qs.QualificationService(db=None, api_url="https://x", api_key="")
    out = await svc.check_single("")
    assert out.has_qualification is False
    assert out.reason == "empty-token"


async def test_invalid_at_cannot_pass_even_when_upstream_eligible_is_true(monkeypatch):
    import httpx
    async def post(self,url,**kwargs):
        return _FakeResp({'token_ok':False,'eligible':True,'reason':'token-401'})
    monkeypatch.setattr(httpx.AsyncClient,'post',post)
    svc=qs.QualificationService(db=None,api_url='https://checker.example')
    result=await svc.check_single('invalid-token')
    assert result.has_qualification is False


async def test_check_batch_updates_status_and_history(_mock_http):
    db = await _memdb()
    for acc, at in [("a@x.com", "GOOD"), ("b@x.com", "BAD"), ("c@x.com", "")]:
        await db.execute(
            "INSERT INTO credentials(account, password, access_token) VALUES(?,?,?)", (acc, "p", at))
    await db.commit()
    svc = qs.QualificationService(db=db, api_url="https://x", api_key="", concurrency=5)
    res = await svc.check_batch([1, 2, 3])
    assert res.checked == 3 and res.qualified == 1 and res.failed == 2
    async with db.execute("SELECT id, status FROM credentials ORDER BY id") as cur:
        statuses = {r["id"]: r["status"] async for r in cur}
    assert statuses == {1: "qualified", 2: "failed", 3: "failed"}
    async with db.execute("SELECT COUNT(*) AS c FROM qualification_checks") as cur:
        assert (await cur.fetchone())["c"] == 3
    await db.close()


# ---- API 集成 ----
@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("UPI_DB_PATH", str(tmp_path / "q.sqlite"))
    monkeypatch.setenv("UPI_JWT_SECRET", "test-secret-12345678901234567890")
    monkeypatch.setenv("UPI_ADMIN_USER", "admin")
    monkeypatch.setenv("UPI_ADMIN_PASS", "admin123")
    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post_factory())
    import app.config as cfg

    cfg.get_settings.cache_clear()
    from app.main import create_app

    from fastapi.testclient import TestClient

    with TestClient(create_app()) as c:
        yield c
    cfg.get_settings.cache_clear()


def test_api_check_flow(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    client.post("/api/credentials/import",
                files={"file": ("k.txt", "a@x.com---p---2fa---GOOD\nb@x.com---p---2fa---BAD\n",
                                "text/plain")}, headers=h)
    r = client.post("/api/qualification/check", json={"status": "pending"}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["checked"] == 2 and body["qualified"] == 1 and body["failed"] == 1
    r = client.get("/api/dashboard/stats", headers=h)
    assert r.json()["by_status"].get("qualified") == 1
