"""M3 资格检查：对接 cha.nerver.cc POST /api/v1/check。

契约见 INTEGRATION.md §1.1：请求 {token, promoId} → 主看 token_ok/eligible/reason。
qualified 当且仅当 eligible == True；其余一律 failed 并记录 reason。
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

import httpx

from ..utils.helpers import utcnow_iso

DEFAULT_PROMO_ID = "plus-1-month-free"


@dataclass
class CheckOutcome:
    has_qualification: bool
    reason: str
    raw: dict = field(default_factory=dict)


@dataclass
class CheckResult:
    checked: int = 0
    qualified: int = 0
    failed: int = 0
    details: list[dict] = field(default_factory=list)


class QualificationService:
    def __init__(self, db, api_url: str, api_key: str = "",
                 promo_id: str = DEFAULT_PROMO_ID,
                 timeout: float = 30.0, concurrency: int = 10):
        self.db = db
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key or ""
        self.promo_id = promo_id or DEFAULT_PROMO_ID
        self.timeout = timeout
        self.concurrency = max(1, concurrency)

    def _headers(self) -> dict:
        h = {"content-type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    async def check_single(self, at_token: str) -> CheckOutcome:
        token = (at_token or "").strip()
        if not token:
            return CheckOutcome(False, "empty-token", {})
        # 允许整段 session JSON 直传（上游自动提取 accessToken）
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{self.api_url}/api/v1/check",
                    json={"token": token, "promoId": self.promo_id},
                    headers=self._headers(),
                    timeout=self.timeout,
                )
                data = resp.json()
        except Exception as e:  # 网络/解析失败视为可重试的临时错误
            return CheckOutcome(False, "fetch-error", {"error": str(e)[:400]})
        if not isinstance(data, dict):
            return CheckOutcome(False, "invalid-response", {"raw": str(data)[:400]})
        eligible = resp.status_code < 400 and data.get("token_ok") is True and data.get("eligible") is True
        reason = str(data.get("reason") or ("eligible" if eligible else "not-eligible"))
        return CheckOutcome(eligible, reason, data)

    async def _check_one(self, sem: asyncio.Semaphore, cid: int, at: str) -> dict:
        async with sem:
            outcome = await self.check_single(at)
            status = "qualified" if outcome.has_qualification else "failed"
            await self.db.execute(
                "INSERT INTO qualification_checks(credential_id, at_token, has_qualification, raw_response)"
                " VALUES(?,?,?,?)",
                (cid, at or "", 1 if outcome.has_qualification else 0,
                 json.dumps(outcome.raw, ensure_ascii=False)[:8000]),
            )
            await self.db.execute(
                "UPDATE credentials SET status=?, error_msg=?, updated_at=? WHERE id=?",
                (status, outcome.reason, utcnow_iso(), cid),
            )
            await self.db.commit()
            return {"credential_id": cid, "qualified": outcome.has_qualification,
                    "reason": outcome.reason}

    async def check_batch(self, credential_ids: list[int]) -> CheckResult:
        sem = asyncio.Semaphore(self.concurrency)
        pairs: list[tuple[int, str]] = []
        for cid in credential_ids:
            async with self.db.execute(
                    "SELECT id, COALESCE(access_token,'') AS at FROM credentials WHERE id=?",
                    (cid,)) as cur:
                row = await cur.fetchone()
            if row:
                pairs.append((row["id"], row["at"]))
        details = await asyncio.gather(
            *(self._check_one(sem, cid, at) for cid, at in pairs))
        res = CheckResult(checked=len(details))
        for d in details:
            res.details.append(d)
            if d["qualified"]:
                res.qualified += 1
            else:
                res.failed += 1
        return res
