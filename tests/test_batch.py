"""M6 批次编排：全流程（资格→接码→提链）+ 暂停/继续/重试失败/进度。子服务全部 mock，只测编排。"""
from __future__ import annotations

import pytest

import app.services.batch_service as bs
from app.services.qualification_service import CheckResult, CheckOutcome
from app.services.sms_service import SMSBatchResult
from app.services.upi_service import UPIBatchResult


class FakeQual:
    called_with: list = []

    def __init__(self, db, **kw):
        self.db = db

    async def check_batch(self, ids):
        FakeQual.called_with.append(list(ids))
        return CheckResult(checked=len(ids), qualified=len(ids), failed=0,
                           details=[{"credential_id": i, "qualified": True, "reason": "eligible"}
                                    for i in ids])

    async def check_single(self,at):
        FakeQual.called_with.append([at])
        return CheckOutcome(True,"eligible",{})


class FakeSMS:
    called = 0

    def __init__(self, db, **kw):
        self.db = db

    async def bind_batch(self, ids, auto_retry=False):
        FakeSMS.called += 1
        return SMSBatchResult(total=len(ids), bound=len(ids), failed=0,
                              details=[{"credential_id": i, "status": "bound"} for i in ids])

    async def bind_with_retries(self,cid):
        FakeSMS.called+=1
        return {"status":"bound"}


class FakeUPI:
    called = 0

    def __init__(self, db, **kw):
        self.db = db

    async def create_batch(self, ids, amount=0, note="", callback_url=""):
        FakeUPI.called += 1
        assert amount > 0, "提链必须带金额"
        return UPIBatchResult(total=len(ids), success=len(ids), failed=0,
                              details=[{"credential_id": i, "status": "success"} for i in ids])

    async def create_with_retries(self,cid,amount,note='',callback_url='',at_token=None):
        FakeUPI.called+=1
        assert amount>0
        return {"status":"success"}


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("UPI_DB_PATH", str(tmp_path / "b.sqlite"))
    monkeypatch.setenv("UPI_JWT_SECRET", "test-secret-12345678901234567890")
    monkeypatch.setenv("UPI_ADMIN_USER", "admin")
    monkeypatch.setenv("UPI_ADMIN_PASS", "admin123")
    monkeypatch.setenv("UPI_REPORT_DIR",str(tmp_path / 'reports'))
    monkeypatch.setattr(bs, "QualificationService", FakeQual)
    monkeypatch.setattr(bs, "SMSService", FakeSMS)
    monkeypatch.setattr(bs, "UPIService", FakeUPI)
    FakeSMS.called = 0
    FakeUPI.called = 0
    FakeQual.called_with = []
    import app.config as cfg

    cfg.get_settings.cache_clear()
    from app.main import create_app

    from fastapi.testclient import TestClient

    with TestClient(create_app()) as c:
        yield c
    cfg.get_settings.cache_clear()


def _login(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _seed(client, h, n=2):
    lines = "".join(f"u{i}@x.com---p---2fa---eyJtok{i}\n" for i in range(n))
    client.post("/api/credentials/import", files={"file": ("k.txt", lines, "text/plain")}, headers=h)
    client.put("/api/configs/sms_provider.cdk", json={"value": "CDK"}, headers=h)
    client.put("/api/configs/upi_provider.cdk", json={"value": "ohk_test"}, headers=h)
    r = client.get("/api/credentials", params={"page": 1, "page_size": 50}, headers=h)
    return [c["id"] for c in r.json()["items"]]


def test_full_pipeline_completes(client):
    h = _login(client)
    ids = _seed(client, h)
    r = client.post("/api/batches", json={"name": "e2e", "credential_ids": ids,
                                          "amount": 10000, "note": "t"}, headers=h)
    assert r.status_code == 200
    bid = r.json()["id"]
    r = client.post(f"/api/batches/{bid}/start", headers=h)
    assert r.status_code in (200, 202), r.text
    import time

    deadline = time.time() + 20
    body = {}
    while time.time() < deadline:
        body = client.get(f"/api/batches/{bid}/progress", headers=h).json()
        if body["status"] in ("completed", "failed"):
            break
        time.sleep(0.3)
    assert body["status"] == "completed", body
    assert FakeSMS.called == 2 and FakeUPI.called == 2
    assert body["progress_json"]
    import json

    prog = json.loads(body["progress_json"])
    assert prog["upi_ok"] == 2 and prog["qualified"] == 2


def test_pause_stops_before_sms_and_resume_completes(client):
    h = _login(client)
    ids = _seed(client, h)

    orig_bind = FakeUPI.create_with_retries

    async def pausing_bind(self,cid,amount,note='',callback_url='',at_token=None):
        import aiosqlite

        from app.database import get_db_path

        db = await aiosqlite.connect(get_db_path())
        await db.execute("UPDATE task_batches SET status='paused' WHERE status='running'")
        await db.commit()
        await db.close()
        return await orig_bind(self,cid,amount,note,callback_url,at_token)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(FakeUPI, "create_with_retries", pausing_bind)
    try:
        r = client.post("/api/batches", json={"name": "p", "credential_ids": ids,
                                              "amount": 100}, headers=h)
        bid = r.json()["id"]
        client.post(f"/api/batches/{bid}/start", headers=h)
        import time

        time.sleep(2)
        body = client.get(f"/api/batches/{bid}/progress", headers=h).json()
        assert body["status"] == "paused", body
        assert FakeSMS.called == 0
        monkeypatch.setattr(FakeUPI,"create_with_retries",orig_bind)
        client.post(f"/api/batches/{bid}/resume", headers=h)
        deadline = time.time() + 20
        while time.time() < deadline:
            body = client.get(f"/api/batches/{bid}/progress", headers=h).json()
            if body["status"] in ("completed", "failed"):
                break
            time.sleep(0.3)
        assert body["status"] == "completed", body
        assert FakeSMS.called == 2
    finally:
        monkeypatch.undo()


def test_retry_failed_reruns_only_failed(client):
    h = _login(client)
    ids = _seed(client, h)
    calls = {"n": 0}

    async def flaky_qual(self, at):
        calls["n"] += 1
        if calls["n"] == 1:
            return CheckOutcome(False,'token-401',{})
        return await FakeQual.check_single(self,at)

    import app.services.batch_service as bsm

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(bsm, "QualificationService",
                        type("Q2", (), {"__init__": lambda self, db, **kw: setattr(self, "db", db),
                                        "check_single": flaky_qual}))
    try:
        r = client.post("/api/batches", json={"name": "r", "credential_ids": ids,
                                              "amount": 100}, headers=h)
        bid = r.json()["id"]
        client.post(f"/api/batches/{bid}/start", headers=h)
        import time

        deadline = time.time() + 20
        while time.time() < deadline:
            body = client.get(f"/api/batches/{bid}/progress", headers=h).json()
            if body["status"] in ("completed", "failed"):
                break
            time.sleep(0.3)
        import json

        prog = json.loads(body["progress_json"])
        assert prog["qualified"] == 1 and prog["failed_qual"] == 1, prog
        r = client.post(f"/api/batches/{bid}/retry-failed", headers=h)
        assert r.status_code == 200, r.text
        retry_bid = r.json()["batch_id"]
        deadline = time.time() + 20
        while time.time() < deadline:
            body = client.get(f"/api/batches/{retry_bid}/progress", headers=h).json()
            if body["status"] in ("completed", "failed"):
                break
            time.sleep(0.3)
        prog = json.loads(body["progress_json"])
        assert prog["upi_ok"] == 1, prog
    finally:
        monkeypatch.undo()


async def test_reset_stuck_batches_recovers_running(tmp_path):
    import aiosqlite

    from app.database import init_db
    from app.services.batch_service import reset_stuck_batches

    db_path = str(tmp_path / "stuck.sqlite")
    import app.config as cfg

    cfg.get_settings.cache_clear()
    import os

    os.environ["UPI_DB_PATH"] = db_path
    try:
        await init_db(db_path)
        db = await aiosqlite.connect(db_path)
        await db.execute("INSERT INTO task_batches(name, total_credentials, status) VALUES('s',1,'running')")
        await db.execute("INSERT INTO task_batches(name, total_credentials, status) VALUES('c',1,'completed')")
        await db.commit()
        await db.close()
        n = await reset_stuck_batches(db_path)
        assert n == 1
        db = await aiosqlite.connect(db_path)
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT name, status FROM task_batches ORDER BY id") as cur:
            rows = {r["name"]: r["status"] async for r in cur}
        await db.close()
        assert rows == {"s": "paused", "c": "completed"}
    finally:
        del os.environ["UPI_DB_PATH"]
        cfg.get_settings.cache_clear()


def test_sse_streams_progress_until_completed(client):
    h = _login(client)
    ids = _seed(client, h)
    r = client.post("/api/batches", json={"name": "sse", "credential_ids": ids,
                                          "amount": 100}, headers=h)
    bid = r.json()["id"]
    client.post(f"/api/batches/{bid}/start", headers=h)
    import time

    deadline = time.time() + 25
    got_completed = False
    with client.stream("GET", f"/api/batches/{bid}/events", headers=h) as s:
        assert s.status_code == 200
        for line in s.iter_lines():
            if line.startswith("data:"):
                import json

                d = json.loads(line[5:].strip())
                if d.get("status") == "completed":
                    got_completed = True
                    break
            if time.time() > deadline:
                break
    assert got_completed


def test_delete_batch(client):
    h = _login(client)
    ids = _seed(client, h)
    r = client.post("/api/batches", json={"name": "del", "credential_ids": ids,
                                          "amount": 100}, headers=h)
    bid = r.json()["id"]
    assert client.delete(f"/api/batches/{bid}", headers=h).status_code == 200
    assert client.get(f"/api/batches/{bid}/progress", headers=h).status_code == 404
    r = client.post("/api/batches", json={"name": "run", "credential_ids": ids,
                                          "amount": 100}, headers=h)
    bid2 = r.json()["id"]
    client.post(f"/api/batches/{bid2}/start", headers=h)
    import time

    time.sleep(0.2)
    status = client.get(f"/api/batches/{bid2}/progress", headers=h).json()["status"]
    delete_response = client.delete(f"/api/batches/{bid2}", headers=h)
    if status == "running":
        assert delete_response.status_code == 409
    else:
        assert delete_response.status_code == 200
