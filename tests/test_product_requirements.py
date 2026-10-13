"""20261012 产品需求：成功库、设置三项、购买链接与运行日志。"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime

import aiosqlite
import pytest

from app.utils.parser import to_success_txt_row


@pytest.fixture()
def client(monkeypatch, tmp_path):
    db_path = tmp_path / "product.sqlite"
    monkeypatch.setenv("UPI_DB_PATH", str(db_path))
    monkeypatch.setenv("UPI_JWT_SECRET", "test-secret-12345678901234567890")
    monkeypatch.setenv("UPI_ADMIN_USER", "admin")
    monkeypatch.setenv("UPI_ADMIN_PASS", "admin123")
    monkeypatch.setenv("UPI_PURCHASE_URL", "https://buy.example.test")
    monkeypatch.setenv("UPI_REPORT_DIR",str(tmp_path / "reports"))
    import app.config as cfg

    cfg.get_settings.cache_clear()
    from app.main import create_app
    from fastapi.testclient import TestClient

    with TestClient(create_app()) as c:
        c._product_db_path = str(db_path)
        yield c
    cfg.get_settings.cache_clear()


def _login(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _seed_success(client, headers):
    content = "alice@example.com---P@ss---TOTP---eyJold\n"
    r = client.post("/api/credentials/import",
                    files={"file": ("accounts.txt", content, "text/plain")}, headers=headers)
    assert r.status_code == 200
    db_path = client._product_db_path

    async def seed():
        db = await aiosqlite.connect(db_path)
        await db.execute("INSERT INTO task_batches(name,total_credentials,status,current_step,amount)"
                         " VALUES('全流程',1,'completed','done',100)")
        await db.execute("INSERT INTO batch_members(batch_id,credential_id,stage) VALUES(1,1,'upi_ok')")
        await db.execute("INSERT INTO upi_tasks(credential_id, amount, status, upi_link)"
                         " VALUES(1,100,'success','https://pay.example/1')")
        await db.execute("UPDATE credentials SET status='done' WHERE id=1")
        await db.commit()
        await db.close()

    asyncio.run(seed())


def test_success_export_uses_required_four_dash_format(client):
    h = _login(client)
    _seed_success(client, h)
    r = client.get("/api/successes", headers=h)
    assert r.status_code == 200
    assert r.json()["total"] == 1
    assert r.json()["items"][0]["account"] == "alice@example.com"

    r = client.get("/api/successes/export?ids=1", headers=h)
    assert r.status_code == 200
    assert r.text.strip() == "alice@example.com----P@ss----TOTP"
    assert "attachment" in r.headers.get("content-disposition", "")


def test_change_password_and_purchase_meta(client):
    h = _login(client)
    r = client.get("/api/dashboard/meta", headers=h)
    assert r.status_code == 200
    assert r.json()["purchase_url"] == "https://buy.example.test"

    r = client.post("/api/auth/password", headers=h, json={
        "current_password": "admin123",
        "new_password": "new-admin-123",
        "confirm_password": "new-admin-123",
    })
    assert r.status_code == 200
    assert client.post("/api/auth/login", json={
        "username": "admin", "password": "new-admin-123"
    }).status_code == 200


def test_dashboard_reports_success_count_and_runtime_logs(client):
    h = _login(client)
    _seed_success(client, h)
    r = client.get("/api/dashboard/stats", headers=h)
    assert r.json()["success_total"] == 1
    r = client.get("/api/dashboard/runtime-logs?after_id=0", headers=h)
    assert r.status_code == 200
    assert "items" in r.json()


def test_success_row_formatter():
    assert to_success_txt_row("a", "b", "c") == "a----b----c"


def test_retry_failed_creates_a_new_task_and_preserves_original(client, monkeypatch):
    h = _login(client)
    content = "retry@example.com---P@ss---TOTP---eyJold\n"
    client.post("/api/credentials/import",
                files={"file": ("accounts.txt", content, "text/plain")}, headers=h)
    ids = [client.get("/api/credentials", headers=h).json()["items"][0]["id"]]
    client.put('/api/proxies',headers=h,json={'text':'http://proxy.example:8080','enabled':True})
    created = client.post("/api/batches", json={"name": "任务", "credential_ids": ids,
                                                  "amount": 100}, headers=h).json()
    source_id = created["id"]

    async def mark_failed():
        db = await aiosqlite.connect(client._product_db_path)
        await db.execute("UPDATE task_batches SET status='failed' WHERE id=?", (source_id,))
        await db.execute("UPDATE batch_members SET stage='upi_failed', error='no_link' WHERE batch_id=?",
                         (source_id,))
        await db.commit()
        await db.close()

    asyncio.run(mark_failed())
    client.put("/api/configs/sms_provider.cdk", json={"value": "CDK"}, headers=h)
    client.put("/api/configs/upi_provider.cdk", json={"value": "ohk_test"}, headers=h)
    monkeypatch.setattr("app.services.batch_service.BatchService.start", lambda self, batch_id: None)

    r = client.post(f"/api/batches/{source_id}/retry-failed", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["batch_id"] != source_id
    assert body["name"].startswith("任务") and "-重试" in body["name"]
    assert client.get(f"/api/batches/{source_id}/progress", headers=h).json()["status"] == "failed"
    detail = client.get(f"/api/batches/{body['batch_id']}", headers=h)
    assert detail.status_code == 200
    assert detail.json()["members"][0]["stage"] == "pending"


def test_success_count_is_unique_and_excludes_link_only_accounts(client):
    h = _login(client)
    _seed_success(client, h)

    async def seed():
        async with aiosqlite.connect(client._product_db_path) as db:
            await db.execute("INSERT INTO upi_tasks(credential_id,amount,status,upi_link)"
                             " VALUES(1,100,'success','https://pay.example/duplicate')")
            await db.execute("INSERT INTO credentials(account,password) VALUES('link-only@example.com','pw')")
            await db.execute("INSERT INTO upi_tasks(credential_id,amount,status,upi_link)"
                             " VALUES(2,100,'success','https://pay.example/link-only')")
            await db.commit()

    asyncio.run(seed())
    assert client.get("/api/dashboard/stats", headers=h).json()["success_total"] == 1
    result = client.get("/api/successes", headers=h).json()
    assert result["total"] == 1
    assert "password" not in result["items"][0]
    assert "twofa_secret" not in result["items"][0]


def test_invalid_selection_never_exports_all_successes(client):
    h = _login(client)
    _seed_success(client, h)
    for ids in ("", "wrong", "1,wrong", "0", "-1"):
        r = client.get("/api/successes/export", params={"ids": ids}, headers=h)
        assert r.status_code == 400, (ids, r.text)


def test_task_details_include_email_error_and_scoped_success_export(client):
    h = _login(client)
    _seed_success(client, h)
    detail = client.get("/api/batches/1", headers=h).json()
    assert detail["members"][0]["account"] == "alice@example.com"
    assert "error" in detail["members"][0]
    r = client.get("/api/batches/1/export-success", headers=h)
    assert r.status_code == 200
    assert r.text.strip() == "alice@example.com----P@ss----TOTP"
    assert client.get("/api/batches/999/export-success", headers=h).status_code == 404


def test_retry_names_use_one_date_and_keep_source_lineage(client, monkeypatch):
    h = _login(client)
    _seed_success(client, h)
    client.put('/api/proxies',headers=h,json={'text':'http://proxy.example:8080','enabled':True})
    today = datetime.now().strftime("%Y%m%d")

    async def seed():
        async with aiosqlite.connect(client._product_db_path) as db:
            await db.execute("UPDATE task_batches SET name=?,status='failed' WHERE id=1", (f"任务{today}",))
            await db.execute("UPDATE batch_members SET stage='upi_failed',error='no_link' WHERE batch_id=1")
            await db.commit()

    asyncio.run(seed())
    client.put("/api/configs/sms_provider.cdk", json={"value": "CDK"}, headers=h)
    client.put("/api/configs/upi_provider.cdk", json={"value": "ohk_test"}, headers=h)
    monkeypatch.setattr("app.services.batch_service.BatchService.start", lambda self, batch_id: None)
    first = client.post("/api/batches/1/retry-failed", headers=h).json()
    second = client.post("/api/batches/1/retry-failed", headers=h).json()
    assert first["name"] == f"任务{today}-重试"
    assert second["name"] == f"任务{today}-重试-2"
    assert client.get(f"/api/batches/{first['batch_id']}", headers=h).json()["source_batch_id"] == 1


def test_create_task_deduplicates_and_rejects_missing_accounts(client):
    h = _login(client)
    _seed_success(client, h)
    r = client.post("/api/batches", json={"name": "任务", "credential_ids": [1, 1], "amount": 100}, headers=h)
    assert r.status_code == 200
    assert r.json()["total_credentials"] == 1
    r = client.post("/api/batches", json={"name": "不存在", "credential_ids": [999], "amount": 100}, headers=h)
    assert r.status_code == 400


def test_settings_do_not_expose_api_key_and_reject_masked_overwrite(client):
    h = _login(client)
    client.put("/api/configs/upi_provider.api_key", json={"value": "ohk_secret-test"}, headers=h)
    client.put("/api/configs/sms_provider.cdk", json={"value": "TEST-CDK"}, headers=h)
    all_configs = client.get("/api/configs", headers=h).json()
    assert "ohk_secret-test" not in json.dumps(all_configs)
    visible = client.get("/api/configs/workspace", headers=h)
    assert visible.status_code == 200
    assert {c["key"] for c in visible.json()} == {"sms_provider.cdk", "upi_provider.cdk", "app.concurrency",'sms.enabled','sub2api.enabled','sub2api.base_url','sub2api.admin_key'}
    assert client.put("/api/configs/sms_provider.cdk", json={"value": "****"}, headers=h).status_code == 400


def test_saved_payment_setting_is_used_by_batch_executor(client):
    h = _login(client)
    client.put("/api/configs/upi_provider.api_key", json={"value": "ohk_old-key"}, headers=h)
    client.put("/api/configs/upi_provider.cdk", json={"value": "ohk_new-key"}, headers=h)

    async def load():
        from app.services.batch_service import BatchService
        async with aiosqlite.connect(client._product_db_path) as db:
            db.row_factory = aiosqlite.Row
            svc = await BatchService.from_db(db, client._product_db_path)
            assert svc.upi["ytc_api_key"] == "ohk_new-key"
            assert svc.sms["max_retries"] is None

    asyncio.run(load())


def test_runtime_logs_initial_tail_and_incremental_cursor(client):
    h=_login(client)
    async def seed():
        async with aiosqlite.connect(client._product_db_path) as db:
            await db.executemany('INSERT INTO runtime_logs(message) VALUES(?)',[(f'日志 {i}',) for i in range(1,121)])
            await db.commit()
    asyncio.run(seed())
    body=client.get('/api/dashboard/runtime-logs?limit=10',headers=h).json()
    assert [r['id'] for r in body['items']]==list(range(111,121))
    assert body['last_id']==120
    body=client.get('/api/dashboard/runtime-logs?after_id=118',headers=h).json()
    assert [r['id'] for r in body['items']]==[119,120]


def test_password_validation_and_logout(client):
    h=_login(client)
    r=client.post('/api/auth/password',headers=h,json={'current_password':'admin123','new_password':'长'*30,'confirm_password':'长'*30})
    assert r.status_code==400
    assert client.post('/api/auth/logout',headers=h).status_code==200
    assert client.get('/api/auth/me').status_code==401


def test_deleting_task_preserves_success_account_and_report_history(client):
    h=_login(client)
    _seed_success(client,h)
    assert client.delete('/api/batches/1',headers=h).status_code==200
    assert client.get('/api/batches/1',headers=h).status_code==404
    assert client.get('/api/batches',headers=h).json()['total']==0
    assert client.get('/api/successes',headers=h).json()['total']==1


def test_deleting_selected_accounts_is_confirmed_by_api_and_blocks_in_use(client):
    h=_login(client)
    content='one@example.com---pw---2FA---eyJold\ntwo@example.com---pw---2FA---eyJold'
    client.post('/api/credentials/import',files={'file':('a.txt',content)},headers=h)
    created=client.post('/api/batches',json={'name':'任务','credential_ids':[1]},headers=h).json()
    r=client.post('/api/credentials/batch-delete',json={'ids':[1,2]},headers=h)
    assert r.status_code==409
    assert client.get('/api/credentials',headers=h).json()['total']==2
    client.delete('/api/batches/'+str(created['id']),headers=h)
    r=client.post('/api/credentials/batch-delete',json={'ids':[1,2]},headers=h)
    assert r.status_code==200
    assert r.json()['deleted']==2
    assert client.get('/api/credentials',headers=h).json()['total']==0


def test_concurrency_is_persisted_as_a_positive_integer_and_used_by_executor(client):
    h=_login(client)
    for value in ('0','-1','1.5','','abc','1e3','+2'):
        r=client.put('/api/configs/app.concurrency',json={'value':value},headers=h)
        assert r.status_code==400,(value,r.text)
    r=client.put('/api/configs/app.concurrency',json={'value':'5'},headers=h)
    assert r.status_code==200,r.text
    settings=client.get('/api/configs/workspace',headers=h).json()
    assert next(item for item in settings if item['key']=='app.concurrency')['value']=='5'
    async def read():
        from app.services.batch_service import BatchService
        async with aiosqlite.connect(client._product_db_path) as db:
            db.row_factory=aiosqlite.Row
            svc=await BatchService.from_db(db,client._product_db_path)
            assert svc.concurrency==5
            assert svc.sms['concurrency']==5 and svc.upi['concurrency']==5
    asyncio.run(read())
    r=client.post('/api/configs/batch',json={'items':{'app.concurrency':'0','sms_provider.cdk':'must-not-save'}},headers=h)
    assert r.status_code==400
    assert next(item for item in client.get('/api/configs',headers=h).json() if item['key']=='sms_provider.cdk')['value']==''


def test_account_pagination_accepts_all_requested_page_sizes(client):
    h=_login(client)
    text='\n'.join(f'account{i}@example.com---pw---2FA---eyJold' for i in range(125))
    client.post('/api/credentials/import',files={'file':('paging.txt',text)},headers=h)
    for size in (10,20,50,100):
        body=client.get('/api/credentials',params={'page':1,'page_size':size},headers=h).json()
        assert len(body['items'])==size
        assert body['total']==125
        assert body['page_size']==size
