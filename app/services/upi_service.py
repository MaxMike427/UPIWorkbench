"""M5 提链编排：DB 落库 + provider 调度（ytc 主 / nerver 备）。"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field

import httpx

from ..utils.helpers import utcnow_iso
from .upi_providers import ExecutionPaused, NerverUPIProvider, YtcUPIProvider
from .report_service import safe_error


@dataclass
class UPIBatchResult:
    total: int = 0
    success: int = 0
    failed: int = 0
    details: list[dict] = field(default_factory=list)


class UPIService:
    def __init__(self, db, provider: str = "ytc",
                 ytc_base_url: str = "https://ytc.xxsyun.xyz", ytc_api_key: str = "",
                 nerver_base_url: str = "https://cha.nerver.cc", nerver_api_key: str = "",
                 promo_id: str = "plus-1-month-free",
                 pool_keep: int = 50, pool_max: int = 2000,
                 timeout: float = 600.0, concurrency: int = 3,
                 ytc_poll_interval: float = 10.0,
                 max_retries: int = 5, retry_interval: float = 120.0,
                 ytc_payment_cdk: str = "",
                 wait_for_payment: bool = False,
                 ytc_proxies: list[str] | None = None,
                 # 兼容旧构造（nerver 直参）
                 base_url: str | None = None, api_key: str | None = None):
        self.db = db
        if base_url is not None:  # 旧式：base_url/api_key 即 nerver
            provider = "nerver"
            nerver_base_url = base_url
            if api_key is not None:
                nerver_api_key = api_key
        self.provider_name = provider
        if provider == "ytc":
            if not ytc_api_key:
                raise RuntimeError("upi_provider.api_key（ohk_）未配置")
            self.provider = YtcUPIProvider(ytc_base_url, ytc_api_key, timeout=timeout,
                                           poll_interval=ytc_poll_interval, wait_for_payment=wait_for_payment,proxies=ytc_proxies)
        elif provider == "nerver":
            self.provider = NerverUPIProvider(nerver_base_url, nerver_api_key, promo_id,
                                              pool_keep, pool_max, timeout=timeout)
        else:
            raise ValueError(f"未知提链 provider: {provider}")
        self.concurrency = max(1, concurrency)
        self.max_retries = max(1, max_retries)
        self.retry_interval = max(0.0, retry_interval)
        self.wait_for_payment = wait_for_payment

    async def create_upi_link(self, credential_id: int, amount: int,
                              note: str = "", callback_url: str = "",
                              sms_task_id: int | None = None, at_token: str | None = None) -> dict:
        async with self.db.execute(
                "SELECT id, COALESCE(access_token,'') AS at FROM credentials WHERE id=?",
                (credential_id,)) as cur:
            cred = await cur.fetchone()
        if not cred:
            raise ValueError(f"credential {credential_id} 不存在")
        active = None
        if self.wait_for_payment:
            async with self.db.execute("SELECT id,external_job_id FROM upi_tasks WHERE credential_id=?"
                                       " AND status='processing' AND external_job_id IS NOT NULL ORDER BY id DESC LIMIT 1",(credential_id,)) as cur:
                active=await cur.fetchone()
        if active:
            task_id=active['id']
        else:
            async with self.db.execute(
                "INSERT INTO upi_tasks(credential_id, sms_task_id, amount, note, callback_url, status)"
                " VALUES(?,?,?,?,?, 'pending')",
                (credential_id, sms_task_id, amount, note, callback_url)) as cur:
                task_id = cur.lastrowid
            await self.db.commit()

        async def _fail(reason: str):
            await self.db.execute(
                "UPDATE upi_tasks SET status='failed', error_msg=?, updated_at=? WHERE id=?",
                (reason[:500], utcnow_iso(), task_id))
            await self.db.commit()
            return await self._get(task_id)

        token = cred["at"] if at_token is None else at_token
        if not (token or "").strip():
            return await _fail("empty-token")
        await self.db.execute("UPDATE upi_tasks SET status='processing' WHERE id=?", (task_id,))
        await self.db.commit()
        try:
            if self.wait_for_payment:
                async def submitted(jid):
                    await self.db.execute('UPDATE upi_tasks SET external_job_id=? WHERE id=?',(jid,task_id))
                    await self.db.commit()
                self.provider.on_submitted=submitted
                link,raw=await self.provider.fetch_link(token,self.concurrency,job_id=active['external_job_id'] if active else None)
            else:
                link, raw = await self.provider.fetch_link(token, self.concurrency)
        except ExecutionPaused as e:
            return {**(await self._get(task_id)), 'status':'paused', 'error_msg':str(e)}
        except Exception as e:
            if self.wait_for_payment and isinstance(e,httpx.RequestError):
                saved=await self._get(task_id)
                if saved.get('external_job_id'):
                    return {**saved,'status':'paused','error_msg':'等待原支付订单状态，请继续任务核对'}
            return await _fail(str(e)[:500])
        if not link:
            return await _fail("no-next-action")
        await self.db.execute(
            "UPDATE upi_tasks SET status='success', upi_link=?, qr_code_data=?,"
            " raw_response=?, updated_at=? WHERE id=?",
            (link, (raw.get("upi_qr_data") or "") if isinstance(raw, dict) else "",
             json.dumps(raw, ensure_ascii=False)[:16000], utcnow_iso(), task_id))
        await self.db.commit()
        # 提链支付成功不等于全流程完成，账号 status=done 由最后接码步骤写入。
        return await self._get(task_id)

    async def create_with_retries(self, credential_id: int, amount: int,
                                    note: str = "", callback_url: str = "", at_token: str | None = None) -> dict:
        """单号自动重试：失败后等待 retry_interval，最多 max_retries 次。"""
        last: dict = {}
        progress=getattr(self.provider,'on_progress',None)
        for attempt in range(self.max_retries):
            running=getattr(self.provider,'should_continue',None)
            if running and not await running():
                return {**last,'status':'paused'}
            if attempt > 0:
                deadline=time.monotonic()+self.retry_interval
                while time.monotonic()<deadline:
                    if running and not await running():return {**last,'status':'paused'}
                    await asyncio.sleep(min(1.0,max(0,deadline-time.monotonic())))
            if progress:
                await progress(f'开始第 {attempt+1}/{self.max_retries} 次提链支付尝试')
            last = await self.create_upi_link(credential_id, amount, note, callback_url, at_token=at_token)
            if last.get("status") in ("success","paused"):
                return last
            if (last.get("error_msg") or "") == "empty-token":
                return last  # 无 AT，重试无意义
            if progress:
                suffix=f'，等待 {int(self.retry_interval)} 秒后重试' if attempt+1<self.max_retries else '，已达到本次任务尝试上限'
                await progress(f"本次提链未完成：{safe_error(last.get('error_msg',''))}{suffix}")
        return last

    async def _get(self, task_id: int) -> dict:
        async with self.db.execute("SELECT * FROM upi_tasks WHERE id=?", (task_id,)) as cur:
            return dict(await cur.fetchone())

    async def create_batch(self, credential_ids: list[int], amount: int,
                           note: str = "", callback_url: str = "") -> UPIBatchResult:
        sem = asyncio.Semaphore(self.concurrency)

        async def _one(cid: int):
            async with sem:
                try:
                    t = await self.create_with_retries(cid, amount, note, callback_url)
                    return {"credential_id": cid, "status": t["status"],
                            "upi_link": bool(t.get("upi_link")), "error": t.get("error_msg") or ""}
                except Exception as e:
                    return {"credential_id": cid, "status": "failed", "error": str(e)[:300]}

        details = await asyncio.gather(*(_one(cid) for cid in credential_ids))
        res = UPIBatchResult(total=len(details), details=list(details))
        for d in details:
            if d["status"] == "success":
                res.success += 1
            else:
                res.failed += 1
        return res

    async def retry_task(self, task_id: int) -> dict:
        async with self.db.execute("SELECT * FROM upi_tasks WHERE id=?", (task_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            raise ValueError("upi task 不存在")
        if row["status"] != "failed":
            raise ValueError("仅 failed 任务可重试")
        return await self.create_upi_link(row["credential_id"], row["amount"],
                                          row["note"] or "", row["callback_url"] or "",
                                          row["sms_task_id"])
