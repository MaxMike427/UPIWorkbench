"""V1.2.0 服务商分离、本地提炼和任务级自动推送。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch, tmp_path):
    db_file = tmp_path / "v120.sqlite"
    monkeypatch.setenv("UPI_DB_PATH", str(db_file))
    monkeypatch.setenv("UPI_JWT_SECRET", "test-secret-12345678901234567890")
    monkeypatch.setenv("UPI_ADMIN_USER", "admin")
    monkeypatch.setenv("UPI_ADMIN_PASS", "admin123")
    monkeypatch.setenv("UPI_REPORT_DIR", str(tmp_path / "reports"))
    import app.config as cfg

    cfg.get_settings.cache_clear()
    from app.main import create_app

    with TestClient(create_app()) as test_client:
        test_client._v120_db_path = str(db_file)
        yield test_client
    cfg.get_settings.cache_clear()


def login(client):
    response = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_provider_catalog_endpoint_returns_detail_without_recursion(client):
    headers = login(client)
    response = client.get("/api/providers/ytc", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["id"] == "ytc"
    assert response.json()["combined"] is True


def test_workspace_exposes_independent_extract_and_payment_settings(client):
    headers = login(client)
    response = client.get("/api/configs/workspace", headers=headers)
    assert response.status_code == 200, response.text
    keys = {item["key"] for item in response.json()}
    assert {"extract_provider.provider", "extract_provider.cdk",
            "payment_provider.provider", "payment_provider.cdk"} <= keys


def test_batch_snapshot_keeps_provider_choices_and_auto_push_flag(client):
    headers = login(client)
    content = "a@example.com---pw---2fa---eyJv120token\n"
    imported = client.post(
        "/api/credentials/import",
        files={"file": ("accounts.txt", content, "text/plain")},
        headers=headers,
    )
    assert imported.status_code == 200, imported.text
    response = client.post(
        "/api/batches",
        json={"name": "provider-snapshot", "credential_ids": [1], "auto_push": False},
        headers=headers,
    )
    assert response.status_code == 200, response.text

    import sqlite3

    connection = sqlite3.connect(client._v120_db_path)
    snapshot = connection.execute(
        "SELECT snapshot_json FROM task_batches WHERE id=?", (response.json()["id"],)
    ).fetchone()[0]
    connection.close()
    data = json.loads(snapshot)
    assert data["auto_push"] is False
    assert data["extract_provider"] == "ytc"
    assert data["payment_provider"] == "ytc"


def test_provider_selection_rejects_unintegrated_automatic_pair(client):
    headers = login(client)
    assert client.put("/api/configs/extract_provider.provider",
                      json={"value": "lumen"}, headers=headers).status_code == 200
    assert client.put("/api/configs/payment_provider.provider",
                      json={"value": "masi"}, headers=headers).status_code == 200
    content = "a@example.com---pw---2fa---eyJv120pair\n"
    assert client.post("/api/credentials/import",
                       files={"file": ("accounts.txt", content, "text/plain")},
                       headers=headers).status_code == 200
    batch = client.post("/api/batches", json={"name": "unsupported-pair", "credential_ids": [1]}, headers=headers)
    assert batch.status_code == 200
    start = client.post(f"/api/batches/{batch.json()['id']}/start", headers=headers)
    assert start.status_code == 400
    assert "组合未接入" in start.json()["detail"]


@pytest.mark.asyncio
async def test_local_provider_passes_account_email_and_proxy_to_protocol():
    from app.services.upi_providers import LocalUPIProvider

    seen = {}

    def fake_extract(account, proxy):
        seen.update(account=account, proxy=proxy)
        return {"ok": True, "url": "https://upi.example/link", "due": 0}

    provider = LocalUPIProvider(proxies=["socks5://proxy.example:1080"], extract_func=fake_extract)
    link, raw = await provider.fetch_link("eyJtoken", account_email="a@example.com")
    assert link == "https://upi.example/link"
    assert raw["provider"] == "local"
    assert seen == {
        "account": {"email": "a@example.com", "access_token": "eyJtoken", "session_token": ""},
        "proxy": "socks5://proxy.example:1080",
    }


@pytest.mark.asyncio
async def test_masi_payment_provider_submits_and_waits_for_completed_order(monkeypatch):
    from app.services.upi_providers import MasiPaymentProvider

    calls = []

    class Response:
        def __init__(self, status_code, body):
            self.status_code = status_code
            self._body = body

        def json(self):
            return self._body

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, path, **kwargs):
            calls.append(("post", path, kwargs))
            return Response(201, {"ok": True, "order": {"order_id": "ord-1"}, "order_secret": "secret-1"})

        async def get(self, path, **kwargs):
            calls.append(("get", path, kwargs))
            return Response(200, {"ok": True, "order": {"status": "completed"}})

    monkeypatch.setattr("app.services.upi_providers.httpx.AsyncClient", lambda **kwargs: Client())
    provider = MasiPaymentProvider("https://masi.example", "KSCAN-test", poll_interval=0)
    result = await provider.create_or_wait(
        "https://payments.stripe.com/upi/instructions/link",
        "eyJtoken",
        "a@example.com",
    )
    assert result["status"] == "success"
    assert result["order_id"] == "ord-1"
    assert calls[0][0] == "post" and calls[1][0] == "get"
