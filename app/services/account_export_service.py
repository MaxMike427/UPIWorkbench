"""保存接码平台原始 JSON 产物并为成功库打包。绝不提取 ZIP 中的路径。"""
from __future__ import annotations

import asyncio
import io
import json
import zipfile
from pathlib import Path

from ..config import get_settings
from .success_service import SUCCESS_FILTER


def _json_files(blob: bytes) -> list[bytes]:
    if len(blob)>20*1024*1024:
        raise ValueError('平台导出压缩包超过大小限制')
    files=[]
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        members=[info for info in z.infolist() if info.filename.lower().endswith('.json') and not info.is_dir()]
        if not members or len(members)>100 or sum(info.file_size for info in members)>40*1024*1024:
            raise ValueError('平台导出文件缺失或超过大小限制')
        for member in members:
            content=z.read(member)
            parsed=json.loads(content.decode('utf-8-sig'))
            if not isinstance(parsed,(dict,list)):
                raise ValueError('平台导出 JSON 格式无效')
            files.append(content)
    return files


async def save_account_exports(db,credential_id:int,sms_task_id:int,archives:dict[str,bytes]) -> dict:
    if set(archives)!={'cpa','sub2'}:
        raise ValueError('必须同时下载 CPA 和 sub2api 产物')
    parsed={fmt:_json_files(blob) for fmt,blob in archives.items()}
    root=get_settings().data_dir.resolve()/'account_exports'/str(credential_id)/str(sms_task_id)
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    root.chmod(0o700)
    paths={}
    def write_files():
        for fmt,contents in parsed.items():
            names=[]
            for i,content in enumerate(contents,1):
                target=root/f'{fmt}-{i}.json'
                tmp=target.with_suffix('.json.tmp')
                tmp.write_bytes(content)
                tmp.chmod(0o600)
                tmp.replace(target)
                names.append(str(target))
            paths[fmt]=names
    await asyncio.to_thread(write_files)
    for fmt,names in paths.items():
        await db.execute('INSERT INTO account_exports(credential_id,sms_task_id,format,paths_json) VALUES(?,?,?,?)'
                         ' ON CONFLICT(sms_task_id,format) DO UPDATE SET paths_json=excluded.paths_json',
                         (credential_id,sms_task_id,fmt,json.dumps(names)))
    await db.commit()
    return paths


async def package_exports(db,ids:list[int],fmt:str) -> bytes:
    async with db.execute(f"SELECT c.id FROM credentials c WHERE {SUCCESS_FILTER}"
                          f" AND c.id IN ({','.join('?' for _ in ids)})",ids) as cur:
        eligible={r['id'] async for r in cur}
    if eligible!=set(ids):raise ValueError('部分账号不存在或尚未完成全流程')
    async with db.execute(f"SELECT c.id FROM credentials c WHERE c.id IN ({','.join('?' for _ in ids)})"
                          " AND NOT EXISTS(SELECT 1 FROM batch_members m WHERE m.credential_id=c.id AND m.stage IN ('done','upi_ok'))",ids) as cur:
        without_sms=await cur.fetchone()
    if without_sms:raise FileNotFoundError('选中账号尚未接码，暂无 CPA/sub2api JSON，可导出 TXT')
    formats=['cpa','sub2'] if fmt=='both' else [fmt]
    root=(get_settings().data_dir.resolve()/'account_exports')
    files=[]
    for cid in ids:
        for format_name in formats:
            async with db.execute('SELECT paths_json FROM account_exports WHERE credential_id=? AND format=?'
                                  ' ORDER BY id DESC LIMIT 1',(cid,format_name)) as cur:
                row=await cur.fetchone()
            if not row:raise FileNotFoundError(f'账号 #{cid} 缺少 {format_name} 文件，请等待下载完成或联系管理员补取')
            for i,stored in enumerate(json.loads(row['paths_json']),1):
                path=Path(stored).resolve()
                if not path.is_relative_to(root) or not path.is_file():
                    raise FileNotFoundError(f'账号 #{cid} 的导出文件不可用')
                files.append((path,f'{format_name}/account-{cid}-{i}.json'))
    if sum(path.stat().st_size for path,_ in files)>100*1024*1024:
        raise ValueError('打包文件过大，请减少选中账号数量')
    def package():
        buf=io.BytesIO()
        with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED) as z:
            for path,name in files:z.writestr(name,path.read_bytes())
        return buf.getvalue()
    return await asyncio.to_thread(package)
