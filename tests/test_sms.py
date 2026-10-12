"""M4 接码绑定：service 单测（httpx mock：submit/live/zip）+ API 集成测试。"""
from __future__ import annotations

import io
import json
import zipfile

import aiosqlite
import pytest

from app.services import sms_service as ss


def _make_zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            z.writestr(name, content)
    return buf.getvalue()


GOOD_AT = "eyJ" + "n3wATboundAccountToken0123456789ABCDEFGHIJKLMNOPQRSTUV"
ZIP_BYTES = _make_zip({
    "a@x.com_codex.json": json.dumps({"email": "a@x.com", "accessToken": GOOD_AT}),
})


class _FakeResp:
    def __init__(self, payload=None, content: bytes = b"", status: int = 200):
        self._payload = payload
        self.content = content
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"http {self.status_code}")


def _fake_post_factory(live_status: str = "ok"):
    async def fake_post(self, url, **kwargs):
        if url.endswith("/api/jobs"):
            data = kwargs.get("data") or kwargs.get("content") or ""
            text = str(data)
            assert "----" in text, f"submit must use ---- separator: {text[:80]}"
            assert "cdk_code" in text
            return _FakeResp({"ids": ["job-1"], "count": 1, "message": "已加入 1 个任务"})
        if url.endswith("/api/jobs/zip"):
            return _FakeResp(None, content=ZIP_BYTES)
        if url.endswith("/api/jobs/retry"):
            return _FakeResp({"ok": True})
        raise AssertionError(f"unexpected POST {url}")
    return fake_post


def _fake_get_factory(live_status: str = "ok"):
    async def fake_get(self, url, **kwargs):
        assert "api/jobs/live" in url
        return _FakeResp({"total": 1, "page": 1,
                          "jobs": [{"id": "job-1", "email": "a@x.com", "status": live_status,
                                    "progress": {"pct": 100, "detail": "完成"},
                                    "result": "绑定成功" if live_status == "ok" else "登录失败"}]})
    return fake_get


@pytest.fixture()
def _mock_http(monkeypatch):
    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post_factory())
    monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get_factory())


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
        "CREATE TABLE sms_tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, credential_id INTEGER NOT NULL,"
        " phone_number TEXT, sms_code TEXT, new_access_token TEXT, status TEXT DEFAULT 'waiting',"
        " retry_count INTEGER DEFAULT 0, max_retries INTEGER DEFAULT 3, provider TEXT, order_id TEXT,"
        " error_msg TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
        " updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    )
    await db.execute('CREATE TABLE account_exports(id INTEGER PRIMARY KEY,credential_id INTEGER,sms_task_id INTEGER,format TEXT,paths_json TEXT,UNIQUE(sms_task_id,format))')
    await db.execute("INSERT INTO credentials(account, password, twofa_secret, access_token)"
                     " VALUES('a@x.com','pw','2FA','eyJold')")
    await db.commit()
    return db


async def test_bind_success_stores_new_at(_mock_http):
    db = await _memdb()
    svc = ss.SMSService(db=db, base_url="https://h", cdk="CDK",
                        poll_interval=0.01, timeout=5)
    task = await svc.bind_phone(1)
    assert task["status"] == "bound"
    assert task["new_access_token"] == GOOD_AT
    async with db.execute("SELECT access_token, status FROM credentials WHERE id=1") as cur:
        row = await cur.fetchone()
    assert row["access_token"] == GOOD_AT  # 凭证 AT 已换新
    await db.close()


async def test_bind_fail_marks_failed(monkeypatch):
    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post_factory())
    monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get_factory("fail"))
    db = await _memdb()
    svc = ss.SMSService(db=db, base_url="https://h", cdk="CDK",
                        poll_interval=0.01, timeout=5)
    task = await svc.bind_phone(1)
    assert task["status"] == "failed"
    assert "登录失败" in (task["error_msg"] or "")
    await db.close()


async def test_extract_prefers_access_token():
    z = _make_zip({"x.json": json.dumps({"access_token": "eyJ" + "a" * 60, "other": "eyJ" + "b" * 60})})
    assert ss.extract_new_at(z, old_at="") == "eyJ" + "a" * 60


async def test_extract_nested_cpajson_shape():
    new_at = "eyJ" + "c" * 80
    old_at = "eyJ" + "o" * 80
    cpajson = {"email": "a@x.com",
               "codex": {"auth": {"accessToken": old_at, "refreshToken": "eyJ" + "r" * 80}},
               "cpa": {"session": {"access_token": new_at}}}
    z = _make_zip({"a@x.com_cpa.json": json.dumps(cpajson)})
    got = ss.extract_new_at(z, old_at=old_at)
    assert got == new_at and got != old_at


async def test_extract_real_codex_json_shape():
    # handy cpajson 包实测结构（2026-10-12）：顶层扁平键
    new_at = "eyJhbGciOiJSUzI1NiIs" + "x" * 60
    codex = {"id_token": "eyJhbGciOiJSUzI1NiIs" + "i" * 60,
             "access_token": new_at,
             "refresh_token": "rt.1.AAC" + "y" * 60,
             "account_id": "61ca577a", "email": "a@x.com", "type": "codex"}
    z = _make_zip({"a.codex.json": json.dumps(codex), "README.txt": "x"})
    assert ss.extract_new_at(z, old_at="eyJold") == new_at


async def test_id_token_is_never_used_as_access_token():
    z=_make_zip({'auth.json':json.dumps({'id_token':'eyJ'+'i'*100,'refresh_token':'eyJ'+'r'*100})})
    assert ss.extract_new_at(z)==''


def _flaky_factories(fail_times: int):
    """前 fail_times 次 live 返回 fail，之后 ok；每次 submit 返回不同 order_id。"""
    state = {"n": 0}

    async def fake_post(self, url, **kwargs):
        if url.endswith("/api/jobs"):
            state["n"] += 1
            return _FakeResp({"ids": [f"job-{state['n']}"], "count": 1})
        if url.endswith("/api/jobs/zip"):
            return _FakeResp(None, content=ZIP_BYTES)
        raise AssertionError(url)

    async def fake_get(self, url, **kwargs):
        oid = (kwargs.get("params") or {}).get("ids", "")
        idx = int(oid.split("-")[1])
        st = "fail" if idx <= fail_times else "ok"
        return _FakeResp({"jobs": [{"id": oid, "email": "a@x.com", "status": st,
                                    "progress": {"pct": 100}, "result": "绑定成功" if st == "ok" else "风控"}]})
    return fake_post, fake_get


async def test_bind_with_retries_succeeds_on_third(monkeypatch):
    import httpx

    fp, fg = _flaky_factories(fail_times=2)
    monkeypatch.setattr(httpx.AsyncClient, "post", fp)
    monkeypatch.setattr(httpx.AsyncClient, "get", fg)
    db = await _memdb()
    svc = ss.SMSService(db=db, base_url="https://h", cdk="CDK",
                        poll_interval=0.01, timeout=5, max_retries=5, retry_interval=0.01)
    t = await svc.bind_with_retries(1)
    assert t["status"] == "bound"
    assert t["new_access_token"] == GOOD_AT
    async with db.execute("SELECT retry_count FROM sms_tasks ORDER BY id") as cur:
        counts = [r["retry_count"] async for r in cur]
    assert counts == [0, 1, 2]
    await db.close()


async def test_bind_with_retries_gives_up(monkeypatch):
    import httpx

    fp, fg = _flaky_factories(fail_times=99)
    monkeypatch.setattr(httpx.AsyncClient, "post", fp)
    monkeypatch.setattr(httpx.AsyncClient, "get", fg)
    db = await _memdb()
    svc = ss.SMSService(db=db, base_url="https://h", cdk="CDK",
                        poll_interval=0.01, timeout=5, max_retries=3, retry_interval=0.01)
    t = await svc.bind_with_retries(1)
    assert t["status"] == "failed"
    async with db.execute("SELECT COUNT(*) AS c FROM sms_tasks") as cur:
        assert (await cur.fetchone())["c"] == 3
    await db.close()


# ---- API 集成 ----
@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("UPI_DB_PATH", str(tmp_path / "s.sqlite"))
    monkeypatch.setenv("UPI_JWT_SECRET", "test-secret-12345678901234567890")
    monkeypatch.setenv("UPI_ADMIN_USER", "admin")
    monkeypatch.setenv("UPI_ADMIN_PASS", "admin123")
    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post_factory())
    monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get_factory())
    import app.config as cfg

    cfg.get_settings.cache_clear()
    from app.main import create_app

    from fastapi.testclient import TestClient

    with TestClient(create_app()) as c:
        yield c
    cfg.get_settings.cache_clear()


def test_api_sms_flow(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    client.post("/api/credentials/import",
                files={"file": ("k.txt", "a@x.com---pw---2FA---eyJold\n", "text/plain")}, headers=h)
    client.put("/api/configs/sms_provider.cdk", json={"value": "CDK"}, headers=h)
    r = client.post("/api/sms/bind", json={"status": "pending"}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bound"] == 1 and body["total"] == 1
    r = client.get("/api/sms/tasks", headers=h)
    assert r.status_code == 200 and r.json()["total"] == 1
