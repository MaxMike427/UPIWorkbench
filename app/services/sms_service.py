"""M4 接码绑定：对接 apihub.space/handy。

流程：提交 `账号----密码----2FA` → 轮询 live → 成功后下载 zip 导出 → 提取新 AT。
契约见 INTEGRATION.md §2（页面 JS 逆向，无公开文档；zip 内 JSON 结构以下载实测为准，
extract_new_at 采用"已知键优先 + eyJ 兜底"策略并存全量 raw 备查）。
"""
from __future__ import annotations

import asyncio
import io
import json
import time
import zipfile
from dataclasses import dataclass, field

import httpx

from ..utils.helpers import utcnow_iso
from .account_export_service import save_account_exports

# token 候选键优先级（codexjson/sub2json/cpajson 常见键）
_TOKEN_KEYS = ("accessToken", "access_token")


def _walk(obj, path=""):
    """yield (key_path, value) 遍历嵌套 JSON。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            yield p, v
            yield from _walk(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk(v, f"{path}[{i}]")


def _is_token(s: str) -> bool:
    return isinstance(s, str) and s.startswith("eyJ") and len(s) > 50


def extract_new_at(zip_bytes: bytes, old_at: str = "") -> str:
    """从 handy 导出 zip 中提取新 AT。找不到返回空串（调用方记 failed）。"""
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except Exception:
        return ""
    keyed: dict[str, str] = {}
    for name in zf.namelist():
        if not name.lower().endswith(".json"):
            continue
        try:
            data = json.loads(zf.read(name).decode("utf-8", errors="ignore"))
        except Exception:
            continue
        for p, v in _walk(data):
            if _is_token(v) and v != old_at:
                leaf = p.split(".")[-1].rstrip("]")
                keyed.setdefault(leaf, v)
    for k in _TOKEN_KEYS:
        if k in keyed:
            return keyed[k]
    return ""


@dataclass
class SMSTaskOut:
    id: int
    credential_id: int
    status: str
    new_access_token: str = ""
    error_msg: str = ""
    retry_count: int = 0


@dataclass
class SMSBatchResult:
    total: int = 0
    bound: int = 0
    failed: int = 0
    details: list[dict] = field(default_factory=list)


class SMSService:
    def __init__(self, db, base_url: str, cdk: str, fmt: str = "cpa", provider: str = "handy",
                 concurrency: int = 2, poll_interval: float = 5.0,
                 timeout: float = 600.0, max_retries: int | None = 5,
                 retry_interval: float = 120.0):
        self.db = db
        self.base_url = base_url.rstrip("/")
        self.cdk = cdk or ""
        self.provider = provider
        self.fmt = fmt
        self.concurrency = max(1, concurrency)
        self.poll_interval = poll_interval
        self.timeout = timeout
        self.max_retries = max(1, max_retries) if max_retries is not None else None
        self.retry_interval = max(0.0, retry_interval)
        self.should_continue = None
        self.on_progress = None

    async def _submit(self, account: str, password: str, twofa: str) -> str:
        """提交单条，返回 handy job id。"""
        line = "----".join([account, password, twofa or ""])
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/api/jobs",
                data={"accounts": line, "fmt": self.fmt, "cdk_code": self.cdk},
                timeout=30.0,
            )
            try:
                data = resp.json()
            except Exception:
                raise RuntimeError(
                    f"submit rejected (非 JSON 返回，可能是 CDK 余额不足): HTTP {resp.status_code}")
        ids = (data or {}).get("ids") or []
        if not ids:
            raise RuntimeError(f"submit rejected: {json.dumps(data, ensure_ascii=False)[:300]}")
        return str(ids[0])

    async def _live(self, order_id: str) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self.base_url}/api/jobs/live",
                params={"ids": order_id},
                timeout=30.0,
            )
            data = resp.json()
        if isinstance(data, list):
            jobs = data
        elif isinstance(data, dict):
            jobs = data.get("jobs") or data.get("items") or data.get("tasks") or []
        else:
            jobs = []
        for j in jobs:
            if str(j.get("id")) == order_id:
                return j
        return jobs[0] if jobs else {}

    async def _export_zip(self, order_id: str, fmt: str | None = None) -> bytes:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/api/jobs/zip",
                json={"mode": "selected", "ids": [order_id], "fmt": fmt or self.fmt},
                timeout=60.0,
            )
            return resp.content

    async def _set(self, task_id: int, **kw):
        sets = ", ".join(f"{k}=?" for k in kw)
        await self.db.execute(
            f"UPDATE sms_tasks SET {sets}, updated_at=? WHERE id=?",
            (*kw.values(), utcnow_iso(), task_id),
        )
        await self.db.commit()

    async def _get_task(self, task_id: int) -> dict | None:
        async with self.db.execute("SELECT * FROM sms_tasks WHERE id=?", (task_id,)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def bind_phone(self, credential_id: int) -> dict:
        """单次尝试（retry_count=0）。"""
        return await self._attempt(credential_id, 0)

    async def bind_with_retries(self, credential_id: int) -> dict:
        """None 表示不限尝试次数；任务暂停时停止下一次提交。"""
        last, attempt = {}, 0
        while self.max_retries is None or attempt < self.max_retries:
            if self.should_continue and not await self.should_continue():
                return {**last,"status":"paused"}
            if attempt > 0 and not await self._wait_retry():
                return {**last,"status":"paused"}
            if self.on_progress:
                await self.on_progress(f"开始第 {attempt + 1} 次自动登录接码尝试")
            last = await self._attempt(credential_id, attempt)
            if last.get("status") == "bound":
                return last
            if last.get("status") == "paused":
                return last
            if self.on_progress:
                await self.on_progress("本次接码未完成，将在间隔后重试")
            attempt += 1
        return last

    async def _wait_retry(self) -> bool:
        deadline = time.monotonic() + self.retry_interval
        while time.monotonic() < deadline:
            if self.should_continue and not await self.should_continue():
                return False
            await asyncio.sleep(min(1.0, max(0,deadline-time.monotonic())))
        return True

    async def _attempt(self, credential_id: int, retry_no: int) -> dict:
        async with self.db.execute(
                "SELECT id, account, COALESCE(password,'') AS password,"
                " COALESCE(twofa_secret,'') AS twofa, COALESCE(access_token,'') AS old_at"
                " FROM credentials WHERE id=?", (credential_id,)) as cur:
            cred = await cur.fetchone()
        if not cred:
            raise ValueError(f"credential {credential_id} 不存在")
        if not self.cdk:
            raise RuntimeError("sms_provider.cdk 未配置")
        # 暂停后继续复用已提交但尚未结束的订单，避免重复绑定、重复扣费。
        async with self.db.execute(
            "SELECT id,order_id,status FROM sms_tasks WHERE credential_id=? AND status IN ('getting_phone','getting_code','exporting')"
            " AND order_id IS NOT NULL ORDER BY id DESC LIMIT 1", (credential_id,)) as cur:
            active = await cur.fetchone()
        if active:
            task_id, order_id = active["id"], active["order_id"]
        else:
            return await self._submit_attempt(cred, credential_id, retry_no)
        if active['status']=='exporting':
            return await self._export_bound(task_id,order_id,cred,credential_id)
        return await self._poll_and_export(task_id,order_id,cred,credential_id)

    async def _submit_attempt(self, cred, credential_id: int, retry_no: int) -> dict:
        async with self.db.execute(
            "INSERT INTO sms_tasks(credential_id, status, provider, max_retries, retry_count)"
            " VALUES(?, 'waiting', 'handy', ?, ?)",
            (credential_id, self.max_retries, retry_no)) as cur:
            task_id = cur.lastrowid
        await self.db.commit()
        try:
            order_id = await self._submit(cred["account"], cred["password"], cred["twofa"])
        except Exception as e:
            await self._set(task_id, status="failed", error_msg=f"submit: {e}"[:500])
            return await self._get_task(task_id)
        await self._set(task_id, status="getting_phone", order_id=order_id)
        return await self._poll_and_export(task_id,order_id,cred,credential_id)

    async def _poll_and_export(self,task_id: int,order_id: str,cred,credential_id: int) -> dict:
        # 轮询等短信/绑定完成
        deadline = time.monotonic() + self.timeout
        job: dict = {}
        while self.max_retries is None or time.monotonic() < deadline:
            if self.should_continue and not await self.should_continue():
                return {**(await self._get_task(task_id)),"status":"paused"}
            await asyncio.sleep(self.poll_interval)
            try:
                job = await self._live(order_id)
            except Exception:
                continue
            st = (job.get("status") or "").lower()
            if st in ("ok", "success", "done", "bound"):
                break
            if st in ("fail", "failed", "error"):
                prog = job.get("progress") or {}
                detail = (job.get("result") or prog.get("detail") or "")[:500]
                await self._set(task_id, status="failed", error_msg=detail or "handy 绑定失败")
                return await self._get_task(task_id)
            await self._set(task_id, status="getting_code")
        else:
            await self._set(task_id, status="failed", error_msg="poll timeout")
            return await self._get_task(task_id)
        await self._set(task_id,status='exporting')
        return await self._export_bound(task_id,order_id,cred,credential_id)

    async def _export_bound(self,task_id,order_id,cred,credential_id):
        # 平台已绑定成功后只重试下载，不能再次提交账号购买短信。
        try:
            # handy 网页下拉选项的真实值是 cpa/sub2，不是界面显示的 cpajson/sub2json。
            cpa_blob = await self._export_zip(order_id,'cpa')
            sub2_blob = await self._export_zip(order_id,'sub2')
            new_at = extract_new_at(cpa_blob, old_at=cred["old_at"])
            if new_at:
                await save_account_exports(self.db,credential_id,task_id,{'cpa':cpa_blob,'sub2':sub2_blob})
                if self.on_progress:
                    await self.on_progress('CPA JSON 和 sub2api JSON 已保存到 data')
        except Exception as e:
            await self._set(task_id, status="exporting", error_msg="绑定完成，导出文件暂不可用，将继续下载")
            return await self._get_task(task_id)
        if not new_at:
            await self._set(task_id, status="exporting", error_msg="绑定完成，导出文件暂未包含新访问令牌")
            return await self._get_task(task_id)
        await self._set(task_id, status="bound", new_access_token=new_at,error_msg='')
        await self.db.execute(
            "UPDATE credentials SET access_token=?, updated_at=? WHERE id=?",
            (new_at, utcnow_iso(), credential_id),
        )
        await self.db.commit()
        return await self._get_task(task_id)

    async def bind_batch(self, credential_ids: list[int], auto_retry: bool = False) -> SMSBatchResult:
        sem = asyncio.Semaphore(self.concurrency)

        async def _one(cid: int):
            async with sem:
                try:
                    t = await (self.bind_with_retries(cid) if auto_retry else self.bind_phone(cid))
                    return {"credential_id": cid, "status": t["status"],
                            "new_at": bool(t.get("new_access_token")),
                            "error": t.get("error_msg") or ""}
                except Exception as e:
                    return {"credential_id": cid, "status": "failed", "error": str(e)[:300]}

        details = await asyncio.gather(*(_one(cid) for cid in credential_ids))
        res = SMSBatchResult(total=len(details), details=list(details))
        for d in details:
            if d["status"] == "bound":
                res.bound += 1
            else:
                res.failed += 1
        return res

    async def retry_failed(self, task_id: int) -> dict:
        task = await self._get_task(task_id)
        if not task:
            raise ValueError("sms task 不存在")
        if task["status"] != "failed":
            raise ValueError("仅 failed 任务可重试")
        limit = task["max_retries"] if task["max_retries"] is not None else self.max_retries
        if limit is not None and (task["retry_count"] or 0) >= limit:
            raise ValueError("已达最大重试次数")
        await self._set(task_id, retry_count=(task["retry_count"] or 0) + 1)
        return await self._attempt(task["credential_id"], (task["retry_count"] or 0) + 1)
