"""M5 提链生成：service 单测（httpx mock）+ API 集成测试。"""
from __future__ import annotations

import aiosqlite
import pytest

from app.services import upi_service as us


class _FakeResp:
    def __init__(self, payload=None, status: int = 200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"http {self.status_code}")


def _fake_post_factory(mode: str = "ok"):
    async def fake_post(self, url, **kwargs):
        body = kwargs.get("json") or {}
        assert "token" in body and body["token"], "must send AT"
        assert url.endswith("/api/v1/upi")
        if mode == "ok":
            return _FakeResp({
                "ok": True, "reason": "ok", "retryable": False,
                "next_action_type": "redirect_to_url",
                "upi_redirect_url": "https://pay.stripe.com/upi/abc",
                "upi_qr_data": "upi://pay?pa=x@upi",
                "upi_qr_image_url": "https://files.stripe.com/u.png",
                "upi_hosted_url": "https://pay.openai.com/c/abc",
            })
        if mode == "busy":
            return _FakeResp({"ok": False, "reason": "concurrency-busy",
                              "retryable": True, "message": "忙"})
        return _FakeResp({"ok": False, "reason": "approve-blocked",
                          "retryable": False, "message": "拿不到连接"})
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
        "CREATE TABLE upi_tasks (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " credential_id INTEGER NOT NULL, sms_task_id INTEGER, amount INTEGER NOT NULL, note TEXT,"
        " callback_url TEXT, upi_link TEXT, qr_code_data TEXT, status TEXT DEFAULT 'pending',"
        " raw_response TEXT, error_msg TEXT, external_job_id TEXT, external_job_secret TEXT,"
        " payment_order_id TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
        " updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    )
    await db.execute("INSERT INTO credentials(account, password, access_token)"
                     " VALUES('a@x.com','p','eyJoldAT')")
    await db.commit()
    return db


async def test_create_link_success(_mock_http):
    db = await _memdb()
    svc = us.UPIService(db=db, base_url="https://n", api_key="K", timeout=30)
    t = await svc.create_upi_link(1, amount=10000, note="test")
    assert t["status"] == "success"
    assert t["upi_link"] == "https://pay.stripe.com/upi/abc"
    assert t["qr_code_data"] == "upi://pay?pa=x@upi"
    assert t["amount"] == 10000
    await db.close()


async def test_create_link_retryable_fail(monkeypatch):
    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post_factory("busy"))
    db = await _memdb()
    svc = us.UPIService(db=db, base_url="https://n", api_key="K", timeout=30)
    t = await svc.create_upi_link(1, amount=100)
    assert t["status"] == "failed"
    assert t["error_msg"] == "concurrency-busy"
    await db.close()


async def test_create_link_terminal_fail(monkeypatch):
    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post_factory("blocked"))
    db = await _memdb()
    svc = us.UPIService(db=db, base_url="https://n", api_key="K", timeout=30)
    t = await svc.create_upi_link(1, amount=100)
    assert t["status"] == "failed"
    await db.close()


async def test_empty_at_rejected_without_http(_mock_http):
    db = await _memdb()
    await db.execute("UPDATE credentials SET access_token='' WHERE id=1")
    await db.commit()
    svc = us.UPIService(db=db, base_url="https://n", api_key="K", timeout=30)
    t = await svc.create_upi_link(1, amount=100)
    assert t["status"] == "failed" and t["error_msg"] == "empty-token"
    await db.close()


def _ytc_post_factory():
    async def fake_post(self, url, **kwargs):
        if url.endswith("/api/auth/login"):
            assert (kwargs.get("json") or {}).get("api_key", "").startswith("ohk_")
            return _FakeResp({"ok": True})
        if url.endswith("/api/jobs"):
            body = kwargs.get("json") or {}
            assert "tokens" in body and body.get("auto_submit") is True
            return _FakeResp({"id": "job-9"})
        raise AssertionError(url)
    return fake_post


async def _ytc_get(self, url, **kwargs):
    if "/results" in url:
        return _FakeResp({"job_id": "job-9",
                          "items": [{"account": "a@x.com", "task_id": "t0001",
                                     "url": "https://pay.openai.com/ytc-link"}]})
    if "/api/tasks" in url:
        return _FakeResp({"items": [{"task": {"stage": "needs_review",
                                               "error": {"code": "no_link"}}}]})
    return _FakeResp({"id": "job-9", "status": "succeeded"})


async def test_ytc_terminal_error_reports_code(monkeypatch):
    import httpx

    async def _get_fail(self, url, **kwargs):
        if "/api/tasks" in url:
            return _FakeResp({"items": [{"task": {"stage": "needs_review", "failure_stage": "instructions",
                                                         "error": {"code": "no_link"}}}]})
        return _FakeResp({"id": "job-9", "status": "needs_review",
                          "counts": {"needs_review": 1}})

    monkeypatch.setattr(httpx.AsyncClient, "post", _ytc_post_factory())
    monkeypatch.setattr(httpx.AsyncClient, "get", _get_fail)
    db = await _memdb()
    svc = us.UPIService(db=db, provider="ytc", ytc_base_url="https://y",
                        ytc_api_key="ohk_test", ytc_poll_interval=0.01, timeout=30)
    t = await svc.create_upi_link(1, amount=100)
    assert t["status"] == "failed"
    assert "no_link" in t["error_msg"]
    await db.close()


async def test_ytc_flow(monkeypatch):
    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "post", _ytc_post_factory())
    monkeypatch.setattr(httpx.AsyncClient, "get", _ytc_get)
    db = await _memdb()
    svc = us.UPIService(db=db, provider="ytc", ytc_base_url="https://y",
                        ytc_api_key="ohk_test", ytc_poll_interval=0.01, timeout=30)
    t = await svc.create_upi_link(1, amount=100)
    assert t["status"] == "success"
    assert t["upi_link"] == "https://pay.openai.com/ytc-link"
    await db.close()


def _ytc_flaky_factories(fail_times: int):
    state = {"n": 0}

    async def fake_post(self, url, **kwargs):
        if url.endswith("/api/auth/login"):
            return _FakeResp({"ok": True})
        if url.endswith("/api/jobs"):
            state["n"] += 1
            return _FakeResp({"id": f"job-{state['n']}"})
        raise AssertionError(url)

    async def fake_get(self, url, **kwargs):
        if "/results" in url:
            return _FakeResp({"job_id": "x", "items": [
                {"url": "https://pay.openai.com/ok"}]})
        m = __import__("re").search(r"/api/jobs/(job-\d+)", url)
        idx = int(m.group(1).split("-")[1]) if m else 99
        if idx <= fail_times:
            return _FakeResp({"id": m.group(1), "status": "needs_review"})
        return _FakeResp({"id": m.group(1) if m else "x", "status": "succeeded"})

    async def fake_tasks(self, url, **kwargs):
        return _FakeResp({"items": []})

    async def fake_get_all(self, url, **kwargs):
        if "/api/tasks" in url:
            return await fake_tasks(self, url, **kwargs)
        return await fake_get(self, url, **kwargs)
    return fake_post, fake_get_all


async def test_ytc_retries_then_succeeds(monkeypatch):
    import httpx

    fp, fg = _ytc_flaky_factories(fail_times=2)
    monkeypatch.setattr(httpx.AsyncClient, "post", fp)
    monkeypatch.setattr(httpx.AsyncClient, "get", fg)
    db = await _memdb()
    svc = us.UPIService(db=db, provider="ytc", ytc_base_url="https://y",
                        ytc_api_key="ohk_test", ytc_poll_interval=0.01, timeout=30,
                        max_retries=3, retry_interval=0.01)
    t = await svc.create_with_retries(1, amount=100)
    assert t["status"] == "success"
    async with db.execute("SELECT COUNT(*) AS c FROM upi_tasks") as cur:
        assert (await cur.fetchone())["c"] == 3
    await db.close()


# ---- API 集成 ----
@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("UPI_DB_PATH", str(tmp_path / "u.sqlite"))
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


def test_api_upi_flow(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    client.post("/api/credentials/import",
                files={"file": ("k.txt", "a@x.com---p---2fa---eyJoldAT\n", "text/plain")}, headers=h)
    client.put("/api/configs/upi_provider.provider", json={"value": "nerver"}, headers=h)
    r = client.post("/api/upi/create",
                    json={"status": "pending", "amount": 10000, "note": "t"}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] == 1 and body["total"] == 1
    assert client.get("/api/upi/tasks", headers=h).status_code == 200
