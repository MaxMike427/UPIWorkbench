"""接码产物持久化与成功库打包下载。"""
import asyncio
import io
import json
import zipfile

import aiosqlite
import pytest

from tests.test_product_requirements import client, _login, _seed_success


def archive(name, content):
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED) as z:z.writestr(name,json.dumps(content))
    return buf.getvalue()


def test_persists_both_formats_under_data_and_supports_download_choices(client,monkeypatch,tmp_path):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(),'data_dir',tmp_path/'data')
    h=_login(client)
    _seed_success(client,h)
    cpa={'email':'alice@example.com','access_token':'eyJ'+'a'*80}
    sub2={'accounts':[{'name':'alice','credentials':{'access_token':'eyJ'+'a'*80}}]}
    async def save():
        from app.services.account_export_service import save_account_exports
        async with aiosqlite.connect(client._product_db_path) as db:
            db.row_factory=aiosqlite.Row
            await db.execute("INSERT INTO sms_tasks(credential_id,status) VALUES(1,'bound')")
            await db.commit()
            await save_account_exports(db,1,1,{'cpa':archive('../../escape.json',cpa),'sub2':archive('sub2.json',sub2)})
    asyncio.run(save())
    files=list((tmp_path/'data').rglob('*.json'))
    assert len(files)==2 and all(f.stat().st_mode & 0o777==0o600 for f in files)
    assert not (tmp_path/'escape.json').exists()
    for fmt,expected in (('cpa',1),('sub2',1),('both',2)):
        r=client.get(f'/api/successes/download?ids=1&format={fmt}',headers=h)
        assert r.status_code==200,r.text
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            names=z.namelist()
            assert len(names)==expected
            assert all(name.endswith('.json') and '..' not in name for name in names)
    assert client.get('/api/successes/download?ids=1&format=other',headers=h).status_code==422


def test_missing_files_do_not_silently_download_empty_zip(client):
    h=_login(client)
    _seed_success(client,h)
    r=client.get('/api/successes/download?ids=1&format=both',headers=h)
    assert r.status_code==409
