"""V1.3.0 来源分组、供应商向导和动态代理；所有第三方请求使用替身。"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch, tmp_path):
    db_file = tmp_path / "v130.sqlite"
    monkeypatch.setenv("UPI_DB_PATH", str(db_file))
    monkeypatch.setenv("UPI_JWT_SECRET", "test-secret-12345678901234567890")
    monkeypatch.setenv("UPI_ADMIN_USER", "admin")
    monkeypatch.setenv("UPI_ADMIN_PASS", "admin123")
    import app.config as cfg
    cfg.get_settings.cache_clear()
    from app.main import create_app
    with TestClient(create_app()) as test_client:
        test_client.db_path = str(db_file)
        yield test_client
    cfg.get_settings.cache_clear()


def login(client):
    response = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def import_accounts(client, headers, filename="accounts.txt", accounts=("a",)):
    text = "\n".join(f"{name}@example.com---pw---2fa---eyJ{name}" for name in accounts)
    response = client.post("/api/credentials/import", files={"file": (filename, text, "text/plain")}, headers=headers)
    assert response.status_code == 200, response.text


def test_source_groups_and_exact_filter_combine_with_search_and_pagination(client):
    headers = login(client)
    import_accounts(client, headers, "a.txt", ("alice", "alex"))
    import_accounts(client, headers, "a.txt.bak", ("bob",))
    import_accounts(client, headers, "a.txt", ("ann",))
    groups = client.get("/api/credentials/sources", headers=headers)
    assert groups.status_code == 200, groups.text
    assert groups.json()["items"] == [{"source_file": "a.txt", "total": 3}, {"source_file": "a.txt.bak", "total": 1}]
    result = client.get("/api/credentials", params={"source_file": "a.txt", "keyword": "al", "page_size": 1}, headers=headers)
    assert result.status_code == 200
    assert result.json()["total"] == 2
    assert len(result.json()["items"]) == 1
    assert result.json()["items"][0]["account"] == "alex@example.com"
    empty = client.get("/api/credentials", params={"source_file": "missing.txt"}, headers=headers)
    assert empty.json()["total"] == 0


@pytest.mark.asyncio
async def test_wangyou_provider_uses_cdk_two_proxy_pools_and_job_secret(monkeypatch):
    from app.services.upi_providers import WangyouUPIProvider
    import httpx

    calls = []
    class Response:
        status_code = 202
        def json(self): return {"ok": True, "job_id": "job-wangyou", "job_secret": "secret-wangyou"}
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def post(self, url, **kwargs):
            calls.append(("post", url, kwargs)); return Response()
        async def get(self, url, **kwargs):
            calls.append(("get", url, kwargs))
            return type("Progress", (), {"status_code": 200, "json": lambda self: {"id":"job-wangyou","status":"done","result":{"provider_redirect_url":"https://pay.example/link"}}})()
    monkeypatch.setattr("app.services.upi_providers.httpx.AsyncClient", lambda **kwargs: Client())
    provider = WangyouUPIProvider("https://wangyou.example", "wangyou-cdk-test", ["http://entry:80"], ["http://exit:80"], poll_interval=0)
    link, raw = await provider.fetch_link("eyJtest", concurrency=2)
    assert link == "https://pay.example/link"
    assert raw["job_id"] == "job-wangyou" and raw["job_secret"] == "secret-wangyou"
    assert calls[0][0] == "post" and calls[0][1].endswith("/api/checkout")
    assert calls[0][2]["json"]["cdk"] == "wangyou-cdk-test"
    assert calls[0][2]["json"]["entry_proxies"] == ["http://entry:80"]
    assert calls[0][2]["json"]["exit_proxies"] == ["http://exit:80"]
    assert calls[1][2]["headers"]["X-Pay153-Job-Secret"] == "secret-wangyou"


@pytest.mark.asyncio
async def test_xxsyun_payment_provider_uses_bearer_ytc_key_and_orderhub_order_api(monkeypatch):
    from app.services.upi_providers import XxsyunPaymentProvider

    calls = []
    class Response:
        def __init__(self, status_code, data): self.status_code, self.data = status_code, data
        def json(self): return self.data
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def post(self, url, **kwargs):
            calls.append(("post", url, kwargs))
            return Response(200, {"ok": True, "created": [{"order": {"id": "order-1"}}], "failed": [], "duplicated": []})
        async def get(self, url, **kwargs):
            calls.append(("get", url, kwargs))
            return Response(200, {"ok": True, "order": {"id": "order-1", "status": "completed"}})
    monkeypatch.setattr("app.services.upi_providers.httpx.AsyncClient", lambda **kwargs: Client())
    provider = XxsyunPaymentProvider("https://upi.xxsyun.xyz", "ohk_payment-test", poll_interval=0)
    result = await provider.create_or_wait("https://payments.example/link", "eyJat", "a@example.com")
    assert result["status"] == "success"
    assert result["order_id"] == "order-1"
    assert calls[0][1].endswith("/api/v1/orders/batch")
    assert calls[0][2]["headers"]["Authorization"] == "Bearer ohk_payment-test"
    assert calls[0][2]["json"]["items"] == [{"at": "eyJat", "link": "https://payments.example/link"}]
    assert calls[1][1].endswith("/api/v1/orders/order-1")


def test_wangyou_is_an_integrated_extract_payment_provider_but_requires_system_proxy(client):
    headers = login(client)
    profile = {"flow_type": "extract_pay", "extract_provider": "wangyou", "proxy_source": "system",
               "sms_enabled": False, "credentials": {"wangyou": "wangyou-cdk-test"}}
    result = client.put("/api/configs/supplier", json=profile, headers=headers)
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["payment_provider"] == "wangyou"
    assert body["executable"] is True
    assert body["summary"] == "春晴提链 系统代理 · 提链+支付 · 不接码"
    assert body["credential_items"][0]["provider"] == "wangyou"
    provider_proxy = {**profile, "proxy_source": "provider"}
    rejected = client.put("/api/configs/supplier", json=provider_proxy, headers=headers)
    assert rejected.status_code == 400
    assert "内置代理" in rejected.json()["detail"]


def test_upi_service_constructs_wangyou_as_integrated_provider_with_cdk_and_proxy_pool():
    from app.services.upi_service import UPIService
    from app.services.upi_providers import WangyouUPIProvider
    service = UPIService(db=None, provider="wangyou", extract_provider="wangyou", payment_provider="wangyou",
                         ytc_base_url="https://wangyou.example", payment_cdk="wangyou-cdk-test",
                         ytc_proxies=["http://proxy.example:80"], wait_for_payment=True)
    assert isinstance(service.provider, WangyouUPIProvider)
    assert service.provider.cdk == "wangyou-cdk-test"
    assert service.provider.entry_proxies == ["http://proxy.example:80"]
    assert service.provider.exit_proxies == ["http://proxy.example:80"]


def test_upi_service_constructs_xxsyun_payment_with_ytc_api_key():
    from app.services.upi_service import UPIService
    from app.services.upi_providers import XxsyunPaymentProvider
    service = UPIService(db=None, provider="ytc", extract_provider="ytc", payment_provider="xxsyun_upi",
                         ytc_base_url="https://ytc.example", ytc_api_key="ohk_extract-test",
                         payment_base_url="https://upi.xxsyun.xyz", payment_cdk="ohk_payment-test",
                         wait_for_payment=True)
    assert isinstance(service.payment_adapter, XxsyunPaymentProvider)
    assert service.payment_adapter.api_key == "ohk_payment-test"


def test_xxsyun_payment_selection_reuses_ytc_key_and_is_executable(client):
    headers = login(client)
    profile = {"flow_type": "extract", "extract_provider": "ytc", "payment_provider": "xxsyun_upi",
               "proxy_source": "provider", "sms_enabled": False, "credentials": {"ytc": "ohk_shared-test"}}
    response = client.put("/api/configs/supplier", json=profile, headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["executable"] is True
    assert body["payment_provider"] == "xxsyun_upi"
    assert any(item["provider"] == "ytc" and item["saved"] for item in body["credential_items"])


def test_xxsyun_payment_provider_is_not_rejected_by_task_start_validation(client, monkeypatch):
    headers = login(client)
    profile = {"flow_type": "extract", "extract_provider": "ytc", "payment_provider": "xxsyun_upi",
               "proxy_source": "provider", "sms_enabled": False, "credentials": {"ytc": "ohk_task-test"}}
    assert client.put("/api/configs/supplier", json=profile, headers=headers).status_code == 200
    import_accounts(client, headers)
    bid = client.post("/api/batches", json={"name":"xxsyun-task","credential_ids":[1]}, headers=headers).json()["id"]
    monkeypatch.setattr("app.services.batch_service.BatchService.start", lambda self, batch_id: None)
    response = client.post(f"/api/batches/{bid}/start", headers=headers)
    assert response.status_code == 200, response.text


def test_github_local_extract_can_pair_with_xxsyun_payment_api_key(client, monkeypatch):
    headers = login(client)
    profile = {"flow_type": "extract", "extract_provider": "local", "payment_provider": "xxsyun_upi",
               "proxy_source": "system", "sms_enabled": False, "credentials": {"xxsyun_upi": "ohk_local-payment"}}
    saved = client.put("/api/configs/supplier", json=profile, headers=headers)
    assert saved.status_code == 200, saved.text
    assert saved.json()["executable"] is True
    assert client.post("/api/proxies", json={"text":"http://proxy.example:8080","enabled":True}, headers=headers).status_code == 200
    import_accounts(client, headers)
    bid = client.post("/api/batches", json={"name":"local-xxsyun","credential_ids":[1]}, headers=headers).json()["id"]
    monkeypatch.setattr("app.services.batch_service.BatchService.start", lambda self, batch_id: None)
    started = client.post(f"/api/batches/{bid}/start", headers=headers)
    assert started.status_code == 200, started.text


def test_supplier_profile_saves_atomically_and_returns_only_credential_status(client):
    headers = login(client)
    body = {"flow_type": "extract_pay", "extract_provider": "ytc", "proxy_source": "provider",
            "sms_enabled": True, "sms_provider": "handy",
            "credentials": {"ytc": "ohk_v130-test", "handy": "SMS-test"}}
    result = client.put("/api/configs/supplier", json=body, headers=headers)
    assert result.status_code == 200, result.text
    profile = result.json()
    assert profile["payment_provider"] == "ytc"
    assert profile["executable"] is True
    assert profile["summary"] == "ytc 内置代理 · 提链+支付 · Handy 接码"
    assert {c["key"] for c in profile["credential_items"]} == {"provider_credentials.ytc.cdk", "provider_credentials.handy.cdk"}
    assert all(c["saved"] for c in profile["credential_items"])
    assert "ohk_v130-test" not in json.dumps(profile) and "SMS-test" not in json.dumps(profile)
    read = client.get("/api/configs/supplier", headers=headers)
    assert read.json() == profile
    with sqlite3.connect(client.db_path) as db:
        values = dict(db.execute("SELECT key,value FROM configs"))
    assert values["ytc.proxy_source"] == "provider"
    assert values["sms_provider.cdk"] == "SMS-test"
    assert values["provider_credentials.ytc.cdk"] == "ohk_v130-test"
    client.cookies.clear()
    assert client.get("/api/configs/supplier").status_code == 401


def test_supplier_reselection_keeps_own_secrets_and_invalid_save_does_not_partially_write(client):
    headers = login(client)
    first = {"flow_type": "extract_pay", "extract_provider": "ytc", "proxy_source": "provider",
             "sms_enabled": True, "sms_provider": "handy", "credentials": {"ytc": "ohk_own", "handy": "SMS-own"}}
    assert client.put("/api/configs/supplier", json=first, headers=headers).status_code == 200
    placeholder = {"flow_type": "extract", "extract_provider": "lumen", "payment_provider": "masi",
                   "proxy_source": "system", "sms_enabled": False, "credentials": {"lumen": "LUMEN-own", "masi": "MASI-own"}}
    result = client.put("/api/configs/supplier", json=placeholder, headers=headers)
    assert result.status_code == 200, result.text
    assert result.json()["executable"] is False
    old_profile = result.json()
    invalid = {**first, "credentials": {"ytc": "wrong-cdk", "handy": "SMS-replacement"}}
    assert client.put("/api/configs/supplier", json=invalid, headers=headers).status_code == 400
    assert client.get("/api/configs/supplier", headers=headers).json() == old_profile
    restored = client.put("/api/configs/supplier", json={**first, "credentials": {}}, headers=headers)
    assert restored.status_code == 200
    with sqlite3.connect(client.db_path) as db:
        values = dict(db.execute("SELECT key,value FROM configs"))
    assert values["extract_provider.cdk"] == "ohk_own"
    assert values["sms_provider.cdk"] == "SMS-own"


def test_first_supplier_save_migrates_legacy_credentials_before_disabling_sms(client):
    headers = login(client)
    client.put("/api/configs/upi_provider.api_key", json={"value": "ohk_legacy-own"}, headers=headers)
    client.put("/api/configs/sms_provider.cdk", json={"value": "SMS-legacy-own"}, headers=headers)
    profile = {"flow_type": "extract_pay", "extract_provider": "ytc", "proxy_source": "provider",
               "sms_enabled": False}
    response = client.put("/api/configs/supplier", json=profile, headers=headers)
    assert response.status_code == 200, response.text
    restored = client.put("/api/configs/supplier", json={**profile, "sms_enabled": True, "sms_provider": "handy"}, headers=headers)
    assert restored.status_code == 200, restored.text
    with sqlite3.connect(client.db_path) as db:
        assert db.execute("SELECT value FROM configs WHERE key='provider_credentials.handy.cdk'").fetchone()[0] == "SMS-legacy-own"


def test_legacy_supplier_summary_reports_actual_credential_storage_keys_before_migration(client):
    headers = login(client)
    client.put("/api/configs/upi_provider.api_key", json={"value": "ohk_legacy-location"}, headers=headers)
    client.put("/api/configs/sms_provider.cdk", json={"value": "SMS-legacy-location"}, headers=headers)
    profile = client.get("/api/configs/supplier", headers=headers).json()
    assert profile["configured"] is False
    assert {c["provider"]: c["key"] for c in profile["credential_items"]} == {
        "ytc": "upi_provider.api_key", "handy": "sms_provider.cdk"}


def test_other_supplier_placeholder_can_save_without_live_credentials_but_cannot_start(client, monkeypatch):
    headers = login(client)
    profile = {"flow_type": "extract_pay", "extract_provider": "layo", "proxy_source": "system",
               "sms_enabled": False}
    saved = client.put("/api/configs/supplier", json=profile, headers=headers)
    assert saved.status_code == 200, saved.text
    assert saved.json()["executable"] is False
    assert saved.json()["credential_items"][0]["saved"] is False
    import_accounts(client, headers)
    bid = client.post("/api/batches", json={"name": "placeholder", "credential_ids": [1]}, headers=headers).json()["id"]
    started = client.post(f"/api/batches/{bid}/start", headers=headers)
    assert started.status_code == 400
    assert "占位" in started.json()["detail"]


def test_source_filter_includes_unknown_legacy_source_without_mixing_all_accounts(client):
    headers = login(client)
    import_accounts(client, headers)
    with sqlite3.connect(client.db_path) as db:
        db.execute("INSERT INTO credentials(account,password,source_file) VALUES('unknown@example.com','pw',NULL)")
    result = client.get("/api/credentials", params={"source_file": ""}, headers=headers)
    assert result.json()["total"] == 1
    assert result.json()["items"][0]["account"] == "unknown@example.com"
    assert client.get("/api/credentials/sources", headers=headers).json()["items"][0] == {"source_file": "", "total": 1}


def test_legacy_task_keeps_original_provider_after_first_supplier_reconfiguration(client, monkeypatch):
    headers = login(client)
    client.put("/api/configs/upi_provider.api_key", json={"value": "ohk_old-task"}, headers=headers)
    client.put("/api/configs/sms.enabled", json={"value": "false"}, headers=headers)
    client.put("/api/configs/ytc.proxy_source", json={"value": "provider"}, headers=headers)
    import_accounts(client, headers)
    bid = client.post("/api/batches", json={"name": "legacy-original", "credential_ids": [1]}, headers=headers).json()["id"]
    saved = client.put("/api/configs/supplier", json={"flow_type": "extract", "extract_provider": "lumen", "payment_provider": "masi",
                        "proxy_source": "system", "sms_enabled": False}, headers=headers)
    assert saved.status_code == 200, saved.text
    seen = {}
    def start(self, batch_id): seen.update(self.upi)
    monkeypatch.setattr("app.services.batch_service.BatchService.start", start)
    result = client.post(f"/api/batches/{bid}/start", headers=headers)
    assert result.status_code == 200, result.text
    assert seen["provider"] == "ytc"
    assert seen["ytc_api_key"] == "ohk_old-task"
    assert seen["ytc_base_url"] == "https://ytc.xxsyun.xyz"


def test_supplier_task_keeps_provider_and_proxy_snapshot_after_reconfiguration(client, monkeypatch):
    headers = login(client)
    profile = {"flow_type": "extract_pay", "extract_provider": "ytc", "proxy_source": "provider",
               "sms_enabled": False, "credentials": {"ytc": "ohk_snapshot"}}
    assert client.put("/api/configs/supplier", json=profile, headers=headers).status_code == 200
    import_accounts(client, headers)
    bid = client.post("/api/batches", json={"name": "snapshot", "credential_ids": [1]}, headers=headers).json()["id"]
    placeholder = {"flow_type": "extract", "extract_provider": "lumen", "payment_provider": "masi",
                   "proxy_source": "system", "sms_enabled": False, "credentials": {"lumen": "L", "masi": "M"}}
    assert client.put("/api/configs/supplier", json=placeholder, headers=headers).status_code == 200
    seen = {}
    def start(self, batch_id):
        seen.update(self.upi)
    monkeypatch.setattr("app.services.batch_service.BatchService.start", start)
    result = client.post(f"/api/batches/{bid}/start", headers=headers)
    assert result.status_code == 200, result.text
    assert seen["provider"] == "ytc"
    assert seen["ytc_api_key"] == "ohk_snapshot"
    assert seen["ytc_proxy_source"] == "provider"


def test_dynamic_proxy_configuration_is_private_and_saving_does_not_fetch(client, monkeypatch):
    headers = login(client)
    async def forbidden(*args, **kwargs):
        raise AssertionError("保存配置不可请求付费 API")
    monkeypatch.setattr("httpx.AsyncClient.get", forbidden)
    url = "https://api.711proxy.com/gen?token=private-test&num=2"
    response = client.put("/api/proxies/dynamic", json={"api_url": url, "protocol": "socks5", "enabled": True}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["url_saved"] is True
    assert response.json()["host"] == "api.711proxy.com"
    assert "private-test" not in response.text
    assert "private-test" not in client.get("/api/configs", headers=headers).text
    assert client.get("/api/proxies/dynamic", headers=headers).json() == response.json()


@pytest.mark.parametrize("payload", ["203.0.113.10:10000\n203.0.113.11:10000\n203.0.113.10:10000",
                                     '{"code":200,"data":[{"ip":"203.0.113.10","port":10000},{"ip":"203.0.113.11","port":10000}]}'])
def test_dynamic_api_fetch_parses_txt_and_json_and_adds_masked_dynamic_proxies(client, monkeypatch, payload):
    import httpx
    from app.services import proxy_service
    headers = login(client)
    url = "https://api.711proxy.com/gen?token=private-test"
    assert client.put("/api/proxies/dynamic", json={"api_url": url, "protocol": "socks5", "enabled": True}, headers=headers).status_code == 200
    calls = []
    def respond(request):
        calls.append(str(request.url))
        return httpx.Response(200, text=payload)
    original = httpx.AsyncClient
    monkeypatch.setattr(proxy_service, "_dynamic_client", lambda: original(transport=httpx.MockTransport(respond)))
    async def public_host(host): return None
    monkeypatch.setattr(proxy_service, "_validate_dynamic_host", public_host)
    result = client.post("/api/proxies/dynamic/fetch", headers=headers)
    assert result.status_code == 200, result.text
    assert result.json()["fetched"] == 2
    assert all(p["protocol"] == "socks5" and p["is_dynamic"] for p in result.json()["items"])
    assert calls == [url]
    assert "private-test" not in result.text


def test_new_system_proxy_task_fetches_once_and_resume_keeps_original_proxy(client, monkeypatch):
    from app.services import batch_service, proxy_service
    headers = login(client)
    profile = {"flow_type": "extract_pay", "extract_provider": "ytc", "proxy_source": "system",
               "sms_enabled": False, "credentials": {"ytc": "ohk_dynamic"}}
    assert client.put("/api/configs/supplier", json=profile, headers=headers).status_code == 200
    assert client.put("/api/proxies/dynamic", json={"api_url": "https://api.711proxy.com/gen?token=locked", "enabled": True}, headers=headers).status_code == 200
    calls = []
    async def fetch(config):
        calls.append(config)
        return [proxy_service.parse_proxy("http://203.0.113.20:10000")]
    monkeypatch.setattr(proxy_service, "fetch_dynamic_proxies", fetch)
    monkeypatch.setattr(batch_service.BatchService, "start", lambda self, batch_id: None)
    import_accounts(client, headers)
    bid = client.post("/api/batches", json={"name": "dynamic", "credential_ids": [1]}, headers=headers).json()["id"]
    assert not calls
    # 启动应使用任务保存时的动态 API 配置，而不是后续全局更改。
    client.put("/api/proxies/dynamic", json={"api_url": "https://api.711proxy.com/gen?token=new", "enabled": True}, headers=headers)
    started = client.post(f"/api/batches/{bid}/start", headers=headers)
    assert started.status_code == 200, started.text
    assert len(calls) == 1 and calls[0]["api_url"].endswith("token=locked")
    with sqlite3.connect(client.db_path) as db:
        snapshot = json.loads(db.execute("SELECT snapshot_json FROM task_batches WHERE id=?", (bid,)).fetchone()[0])
        original = db.execute("SELECT proxy_url FROM batch_members WHERE batch_id=?", (bid,)).fetchone()[0]
        db.execute("UPDATE task_batches SET status='paused' WHERE id=?", (bid,))
    assert snapshot["dynamic_proxy_fetched"] is True
    assert original == "http://203.0.113.20:10000"
    assert client.post(f"/api/batches/{bid}/resume", headers=headers).status_code == 200
    assert len(calls) == 1
    assert "token=locked" not in client.get(f"/api/batches/{bid}", headers=headers).text


def test_builtin_proxy_task_ignores_disabled_incompatible_local_proxies(client, monkeypatch):
    headers = login(client)
    profile = {"flow_type": "extract_pay", "extract_provider": "ytc", "proxy_source": "provider",
               "sms_enabled": False, "credentials": {"ytc": "ohk_builtin"}}
    client.put("/api/configs/supplier", json=profile, headers=headers)
    client.post("/api/proxies", json={"text": "socks4://proxy.example:1080", "enabled": False}, headers=headers)
    import_accounts(client, headers)
    bid = client.post("/api/batches", json={"name": "builtin", "credential_ids": [1]}, headers=headers).json()["id"]
    monkeypatch.setattr("app.services.batch_service.BatchService.start", lambda self, batch_id: None)
    started = client.post(f"/api/batches/{bid}/start", headers=headers)
    assert started.status_code == 200, started.text


@pytest.mark.asyncio
async def test_ytc_provider_choice_explicitly_maps_to_internal_even_with_proxies():
    import httpx
    from app.services.upi_providers import YtcUPIProvider
    sent = []
    def respond(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "job"})
    provider = YtcUPIProvider("https://ytc.example", "ohk_test", proxies=["http://proxy.example:80"], proxy_source="provider")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as remote:
        await provider.create_job(remote, "eyJtoken", 1)
    assert sent[0]["proxy_source"] == "internal"
    assert "proxies" not in sent[0]


def test_concurrent_task_validation_fetches_dynamic_proxy_only_once(client, monkeypatch):
    import aiosqlite
    from app.services import batch_service, proxy_service
    headers = login(client)
    profile = {"flow_type": "extract_pay", "extract_provider": "ytc", "proxy_source": "system",
               "sms_enabled": False, "credentials": {"ytc": "ohk_race"}}
    client.put("/api/configs/supplier", json=profile, headers=headers)
    client.put("/api/proxies/dynamic", json={"api_url": "https://api.711proxy.com/gen?token=race", "enabled": True}, headers=headers)
    import_accounts(client, headers)
    bid = client.post("/api/batches", json={"name": "race", "credential_ids": [1]}, headers=headers).json()["id"]
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def fetch(config):
        calls.append(config)
        entered.set()
        await release.wait()
        return [proxy_service.parse_proxy("http://203.0.113.40:10000")]
    monkeypatch.setattr(proxy_service, "fetch_dynamic_proxies", fetch)
    async def drive():
        async def validate():
            async with aiosqlite.connect(client.db_path) as db:
                db.row_factory = aiosqlite.Row
                service = await batch_service.BatchService.from_db(db, client.db_path)
                await service.validate_start(db, bid)
        one = asyncio.create_task(validate())
        await asyncio.wait_for(entered.wait(), 5)
        two = asyncio.create_task(validate())
        await asyncio.sleep(0.1)
        release.set()
        await asyncio.gather(one, two)
    asyncio.run(drive())
    assert len(calls) == 1


def test_failed_auto_fetch_keeps_task_created_and_never_starts_executor(client, monkeypatch):
    from app.services import batch_service, proxy_service
    headers = login(client)
    client.put("/api/configs/supplier", json={"flow_type": "extract_pay", "extract_provider": "ytc", "proxy_source": "system",
               "sms_enabled": False, "credentials": {"ytc": "ohk_failed"}}, headers=headers)
    client.put("/api/proxies/dynamic", json={"api_url": "https://api.711proxy.com/gen?token=failure", "enabled": True}, headers=headers)
    import_accounts(client, headers)
    bid = client.post("/api/batches", json={"name": "failed-fetch", "credential_ids": [1]}, headers=headers).json()["id"]
    calls = []
    async def fetch(config): raise ValueError("711proxy API 请求失败")
    monkeypatch.setattr(proxy_service, "fetch_dynamic_proxies", fetch)
    monkeypatch.setattr(batch_service.BatchService, "start", lambda self, batch_id: calls.append(batch_id))
    result = client.post(f"/api/batches/{bid}/start", headers=headers)
    assert result.status_code == 400
    assert not calls
    assert client.get(f"/api/batches/{bid}", headers=headers).json()["status"] == "created"
    with sqlite3.connect(client.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM upi_tasks").fetchone()[0] == 0
        snapshot = json.loads(db.execute("SELECT snapshot_json FROM task_batches WHERE id=?", (bid,)).fetchone()[0])
    assert snapshot["dynamic_proxy_fetched"] is False


def test_invalid_task_status_does_not_trigger_dynamic_api(client, monkeypatch):
    from app.services import proxy_service
    headers = login(client)
    client.put("/api/configs/supplier", json={"flow_type": "extract_pay", "extract_provider": "ytc", "proxy_source": "system",
               "sms_enabled": False, "credentials": {"ytc": "ohk_status"}}, headers=headers)
    client.put("/api/proxies/dynamic", json={"api_url": "https://api.711proxy.com/gen?token=status", "enabled": True}, headers=headers)
    import_accounts(client, headers)
    bid = client.post("/api/batches", json={"name": "bad-status", "credential_ids": [1]}, headers=headers).json()["id"]
    with sqlite3.connect(client.db_path) as db: db.execute("UPDATE task_batches SET status='completed' WHERE id=?", (bid,))
    calls = []
    async def fetch(config):
        calls.append(config)
        return [proxy_service.parse_proxy("http://203.0.113.50:10000")]
    monkeypatch.setattr(proxy_service, "fetch_dynamic_proxies", fetch)
    result = client.post(f"/api/batches/{bid}/start", headers=headers)
    assert result.status_code == 409
    assert not calls


@pytest.mark.parametrize("url", ["http://127.0.0.1:8000", "https://evil.test", "https://711proxy.com.evil.test/gen",
                                "https://api.711proxy.com@127.0.0.1/gen", "https://api.711proxy.com:8000/gen",
                                "https://user:secret@api.711proxy.com/gen"])
def test_dynamic_url_rejects_non_official_or_private_targets(client, url):
    headers = login(client)
    response = client.put("/api/proxies/dynamic", json={"api_url": url, "enabled": True}, headers=headers)
    assert response.status_code == 400
    assert url not in response.text


def test_dynamic_url_accepts_711proxy_rotating_api_host_and_port(client):
    headers = login(client)
    url = r"http://as.rotgbapi.trustsource.live:8089/gen?zone=custom&ptype=1&region=IN&asn=ASN137669&count=20&proto=socks5&stype=text&split=\r\n&sessType=rotating"
    response = client.put("/api/proxies/dynamic", json={"api_url": url, "protocol": "socks5", "enabled": True}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["host"] == "as.rotgbapi.trustsource.live"
    assert response.json()["url_saved"] is True


def test_generic_config_routes_cannot_bypass_dynamic_url_validation(client):
    headers = login(client)
    for value in ("http://127.0.0.1:8000/private", "****"):
        result = client.put("/api/configs/proxy.dynamic.api_url", json={"value": value}, headers=headers)
        assert result.status_code == 400
    result = client.post("/api/configs/batch", json={"items": {"proxy.dynamic.api_url": "http://127.0.0.1"}}, headers=headers)
    assert result.status_code == 400
    assert client.put("/api/configs/proxy.dynamic.protocol", json={"value": "file"}, headers=headers).status_code == 400


@pytest.mark.asyncio
async def test_dynamic_api_requests_respect_documented_one_second_interval(monkeypatch):
    import httpx
    from app.services import proxy_service
    starts = []
    def respond(request):
        starts.append(asyncio.get_running_loop().time())
        return httpx.Response(200, text="203.0.113.70:10000")
    original = httpx.AsyncClient
    monkeypatch.setattr(proxy_service, "_dynamic_client", lambda: original(transport=httpx.MockTransport(respond)))
    async def public_host(host): return None
    monkeypatch.setattr(proxy_service, "_validate_dynamic_host", public_host)
    config = {"api_url": "https://api.711proxy.com/gen?token=rate-test", "protocol": "http"}
    await asyncio.gather(proxy_service.fetch_dynamic_proxies(config), proxy_service.fetch_dynamic_proxies(config))
    assert len(starts) == 2
    assert starts[1] - starts[0] >= 0.99


@pytest.mark.asyncio
async def test_dynamic_api_rejects_official_hostname_resolving_to_private_ip(monkeypatch):
    import socket
    from app.services import proxy_service
    loop = asyncio.get_running_loop()
    async def private_resolution(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))]
    monkeypatch.setattr(loop, "getaddrinfo", private_resolution)
    with pytest.raises(ValueError, match="公网"):
        await proxy_service.fetch_dynamic_proxies({"api_url": "https://api.711proxy.com/gen?token=dns-test", "protocol": "http"})


@pytest.mark.parametrize("payload", [
    {"flow_type": "extract", "extract_provider": "ytc", "payment_provider": "masi", "proxy_source": "system", "sms_enabled": False},
    {"flow_type": "extract", "extract_provider": "lumen", "payment_provider": "masi", "proxy_source": "provider", "sms_enabled": False},
    {"flow_type": "extract_pay", "extract_provider": "ytc", "payment_provider": "masi", "proxy_source": "provider", "sms_enabled": False},
    {"flow_type": "extract_pay", "extract_provider": "ytc", "proxy_source": "provider", "sms_enabled": True, "sms_provider": "lumen"},
    {"flow_type": "extract_pay", "extract_provider": "ytc", "proxy_source": "provider", "sms_enabled": False, "credentials": {"ytc": "****"}},
])
def test_invalid_supplier_selection_preserves_current_config(client, payload):
    headers = login(client)
    before = client.get("/api/configs/supplier", headers=headers).json()
    result = client.put("/api/configs/supplier", json=payload, headers=headers)
    assert result.status_code == 400
    assert client.get("/api/configs/supplier", headers=headers).json() == before


def test_sensitive_configuration_validation_errors_do_not_echo_submitted_credentials(client):
    headers = login(client)
    result = client.put("/api/configs/supplier", headers=headers, json={"flow_type": "extract_pay", "extract_provider": "ytc",
                       "proxy_source": "provider", "sms_enabled": False, "credentials": {"ytc": "ohk_private-marker" + "x" * 4096}})
    assert result.status_code == 422
    assert "private-marker" not in result.text
    result = client.put("/api/proxies/dynamic", headers=headers, json={"api_url": "https://api.711proxy.com/gen?token=private-marker" + "x" * 4096})
    assert result.status_code == 422
    assert "private-marker" not in result.text


@pytest.mark.parametrize("status,payload", [(302, "redirect-secret"), (403, "private-test"),
                                          (200, '{"code":403,"msg":"private-test"}'),
                                          (200, "bad-private-test"), (200, ""), (200, "x" * 200001)])
def test_failed_dynamic_fetch_preserves_pool_and_never_leaks_provider_response(client, monkeypatch, status, payload):
    import httpx
    from app.services import proxy_service
    headers = login(client)
    client.post("/api/proxies", json={"text": "http://203.0.113.30:80", "enabled": True}, headers=headers)
    original_pool = client.get("/api/proxies", headers=headers).json()
    client.put("/api/proxies/dynamic", json={"api_url": "https://api.711proxy.com/gen?token=private-test", "enabled": True}, headers=headers)
    original = httpx.AsyncClient
    monkeypatch.setattr(proxy_service, "_dynamic_client", lambda: original(transport=httpx.MockTransport(lambda r: httpx.Response(status, text=payload))))
    async def public_host(host): return None
    monkeypatch.setattr(proxy_service, "_validate_dynamic_host", public_host)
    response = client.post("/api/proxies/dynamic/fetch", headers=headers)
    assert response.status_code == 400
    assert "private-test" not in response.text and "redirect-secret" not in response.text
    assert client.get("/api/proxies", headers=headers).json() == original_pool
