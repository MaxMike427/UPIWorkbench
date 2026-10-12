"""API 集成测试：auth → credentials CRUD → configs → dashboard → batches。"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch, tmp_path):
    db_file = tmp_path / "test.sqlite"
    monkeypatch.setenv("UPI_DB_PATH", str(db_file))
    monkeypatch.setenv("UPI_JWT_SECRET", "test-secret-123")
    monkeypatch.setenv("UPI_ADMIN_USER", "admin")
    monkeypatch.setenv("UPI_ADMIN_PASS", "admin123")
    import app.config as cfg

    cfg.get_settings.cache_clear()
    from app.main import create_app

    app = create_app()
    with TestClient(app) as c:
        yield c
    cfg.get_settings.cache_clear()


def _login(client) -> str:
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def test_login_and_me(client):
    tok = _login(client)
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert r.json()["username"] == "admin"


def test_login_wrong_password_rejected(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "bad"})
    assert r.status_code == 401


def test_unauthorized_blocked(client):
    assert client.get("/api/credentials").status_code in (401, 403)


def test_import_list_export_delete_flow(client):
    tok = _login(client)
    h = {"Authorization": f"Bearer {tok}"}
    content = (
        "卡密导出\n"
        "francis@dr.com---vxv4sztD!Q4Oge!a---BVSI7F3IX3B4OMHBMHC5KPKFLY7G2HM4---eyJhbGciOiJ9.x.y\n"
        "keira@dr.com---zeUVzKMmtjm2_5B4---TSXC6PJJU3NKTKYPVH72K5HGVA3EV6AK---eyJhbGciOiJ9.a.b\n"
    )
    r = client.post("/api/credentials/import", files={"file": ("k.txt", content, "text/plain")}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["imported"] == 2

    r = client.get("/api/credentials", params={"page": 1, "page_size": 10}, headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    cid = body["items"][0]["id"]

    r = client.get("/api/credentials/export", headers=h)
    assert r.status_code == 200
    assert "---" in r.text

    r = client.delete(f"/api/credentials/{cid}", headers=h)
    assert r.status_code == 200

    r = client.post("/api/credentials/batch-delete",
                    json={"ids": [x["id"] for x in body["items"][1:]]}, headers=h)
    assert r.status_code == 200


def test_configs_masked_and_update(client):
    tok = _login(client)
    h = {"Authorization": f"Bearer {tok}"}
    r = client.get("/api/configs", headers=h)
    assert r.status_code == 200
    keys = {c["key"] for c in r.json()}
    assert "sms_provider.cdk" in keys
    r = client.put("/api/configs/qualification.api_url",
                   json={"value": "https://example.com"}, headers=h)
    assert r.status_code == 200


def test_dashboard_and_batches(client, monkeypatch):
    import app.services.batch_service as bsm

    class FakeQ:
        def __init__(self, db, **kw):
            self.db = db

        async def check_batch(self, ids):
            from app.services.qualification_service import CheckResult
            return CheckResult(checked=0, qualified=0, failed=0, details=[])

        async def check_single(self, at):
            from app.services.qualification_service import CheckOutcome
            return CheckOutcome(False,"not-eligible",{})

    class FakeS:
        def __init__(self, db, **kw):
            self.db = db

        async def bind_batch(self, ids, auto_retry=False):
            from app.services.sms_service import SMSBatchResult
            return SMSBatchResult(total=0, bound=0, failed=0, details=[])

    class FakeU:
        def __init__(self, db, **kw):
            self.db = db

        async def create_batch(self, ids, amount=0, note="", callback_url=""):
            from app.services.upi_service import UPIBatchResult
            return UPIBatchResult(total=0, success=0, failed=0, details=[])

    monkeypatch.setattr(bsm, "QualificationService", FakeQ)
    monkeypatch.setattr(bsm, "SMSService", FakeS)
    monkeypatch.setattr(bsm, "UPIService", FakeU)
    tok = _login(client)
    h = {"Authorization": f"Bearer {tok}"}
    assert client.get("/api/dashboard/stats", headers=h).status_code == 200
    client.put("/api/configs/sms_provider.cdk", json={"value": "CDK"}, headers=h)
    client.put("/api/configs/upi_provider.cdk", json={"value": "ohk_test"}, headers=h)
    client.post("/api/credentials/import",files={"file":("test.txt","a@x.com---pw---2FA---eyJold\nb@x.com---pw---2FA---eyJold")},headers=h)
    r = client.post("/api/batches", json={"name": "t1", "credential_ids": [1, 2],
                                          "amount": 100}, headers=h)
    assert r.status_code == 200
    bid = r.json()["id"]
    assert client.get(f"/api/batches/{bid}/progress", headers=h).status_code == 200
    r = client.post(f"/api/batches/{bid}/start", headers=h)
    assert r.status_code == 200, r.text
    assert client.post(f"/api/batches/{bid}/pause", headers=h).status_code == 200
