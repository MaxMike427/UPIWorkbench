"""M3 资格检查接口：POST /api/qualification/check + 历史查询。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from ..auth import get_current_admin
from ..database import get_db, log_action
from ..models import QualificationCheckRequest, QualificationCheckResult
from ..services.qualification_service import DEFAULT_PROMO_ID, QualificationService

router = APIRouter(prefix="/api/qualification", tags=["qualification"])


async def _cfg(db, key: str, default: str = "") -> str:
    async with db.execute("SELECT value FROM configs WHERE key=?", (key,)) as cur:
        row = await cur.fetchone()
    return row["value"] if row else default


@router.post("/check", response_model=QualificationCheckResult)
async def check(body: QualificationCheckRequest, request: Request,
                admin: dict = Depends(get_current_admin), db=Depends(get_db)):
    ids = list(body.credential_ids)
    if not ids and body.status:
        async with db.execute("SELECT id FROM credentials WHERE status=? ORDER BY id",
                              (body.status,)) as cur:
            ids = [r["id"] async for r in cur]
    if not ids:
        raise HTTPException(status_code=400, detail="请提供 credential_ids 或有效的 status 筛选")
    api_url = await _cfg(db, "qualification.api_url", "https://cha.nerver.cc")
    api_key = await _cfg(db, "qualification.api_key", "")
    promo_id = body.promo_id or await _cfg(db, "qualification.promo_id", DEFAULT_PROMO_ID)
    svc = QualificationService(db=db, api_url=api_url, api_key=api_key,
                               promo_id=promo_id, concurrency=body.concurrency)
    res = await svc.check_batch(ids)
    await log_action(db, admin["id"], "qualification_check", "credentials", None,
                     f"checked={res.checked} qualified={res.qualified} failed={res.failed}",
                     request.client.host if request.client else "")
    return {"checked": res.checked, "qualified": res.qualified,
            "failed": res.failed, "details": res.details}


@router.get("/history/{credential_id}")
async def history(credential_id: int, admin: dict = Depends(get_current_admin),
                  db=Depends(get_db)):
    async with db.execute(
        "SELECT id, credential_id, at_token, has_qualification, raw_response, checked_at"
        " FROM qualification_checks WHERE credential_id=? ORDER BY id DESC LIMIT 50",
        (credential_id,)) as cur:
        items = [dict(r) async for r in cur]
    for it in items:  # AT 脱敏：只保留前 12 字符
        at = it.get("at_token") or ""
        it["at_token"] = (at[:12] + "…") if len(at) > 12 else at
    return items
