"""配置管理：GET 脱敏 / PUT 单项 / POST 批量。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from ..auth import get_current_admin
from ..config import parse_concurrency
from ..database import get_db, log_action
from ..models import BatchConfigUpdate, ConfigItem, ConfigUpdate
from ..utils.helpers import mask_secret

router = APIRouter(prefix="/api/configs", tags=["configs"])


SENSITIVE_SUFFIX = (".cdk", ".key", ".api_key", ".secret", ".password", ".token")


def _masked(key: str, value: str) -> tuple[str, bool]:
    if key.endswith(SENSITIVE_SUFFIX) and value:
        return mask_secret(value), True
    return value, False


@router.get("/workspace", response_model=list[ConfigItem])
async def workspace_configs(admin: dict = Depends(get_current_admin), db=Depends(get_db)):
    async with db.execute("SELECT key,value FROM configs WHERE key IN ('sms_provider.cdk','upi_provider.cdk','upi_provider.api_key','app.concurrency')") as cur:
        values = {row["key"]: row["value"] async for row in cur}
    out = []
    for key, description in (("sms_provider.cdk", "接码 CDK"), ("upi_provider.cdk", "提链支付 CDK")):
        raw = values.get(key, "")
        if key=="upi_provider.cdk" and not raw:
            raw=values.get("upi_provider.api_key", "")
        value, masked = _masked(key, raw)
        out.append({"key":key,"description":description,"value":value,"masked":masked})
    out.append({'key':'app.concurrency','description':'并发控制','value':values.get('app.concurrency','3'),'masked':False})
    return out


@router.get("", response_model=list[ConfigItem])
async def list_configs(admin: dict = Depends(get_current_admin), db=Depends(get_db)):
    async with db.execute("SELECT key, value, COALESCE(description,'') AS description FROM configs ORDER BY key") as cur:
        rows = await cur.fetchall()
    out = []
    for r in rows:
        v, m = _masked(r["key"], r["value"])
        out.append({"key": r["key"], "value": v, "description": r["description"], "masked": m})
    return out


@router.put("/{key}")
async def update_config(key: str, body: ConfigUpdate, request: Request,
                        admin: dict = Depends(get_current_admin), db=Depends(get_db)):
    if body.value == "****" and key.endswith(SENSITIVE_SUFFIX):
        raise HTTPException(status_code=400, detail="请填写新值，不可保存脱敏占位符")
    value=body.value
    if key=='app.concurrency':
        try:value=str(parse_concurrency(value))
        except ValueError as e:raise HTTPException(status_code=400,detail=str(e))
    async with db.execute("SELECT key FROM configs WHERE key=?", (key,)) as cur:
        if not await cur.fetchone():
            raise HTTPException(status_code=404, detail="配置项不存在")
    await db.execute("UPDATE configs SET value=?, updated_at=CURRENT_TIMESTAMP WHERE key=?",
                      (value, key))
    await db.commit()
    await log_action(db, admin["id"], "config_change", "config", None, key,
                     request.client.host if request.client else "")
    return {"ok": True}


@router.post("/batch")
async def batch_update(body: BatchConfigUpdate, request: Request,
                       admin: dict = Depends(get_current_admin), db=Depends(get_db)):
    if any(v == "****" and k.endswith(SENSITIVE_SUFFIX) for k,v in body.items.items()):
        raise HTTPException(status_code=400, detail="请填写新值，不可保存脱敏占位符")
    values=dict(body.items)
    if 'app.concurrency' in values:
        try:values['app.concurrency']=str(parse_concurrency(values['app.concurrency']))
        except ValueError as e:raise HTTPException(status_code=400,detail=str(e))
    for k, v in values.items():
        await db.execute(
            "INSERT INTO configs(key, value) VALUES(?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
            (k, v),
        )
    await db.commit()
    await log_action(db, admin["id"], "config_change", "config", None, f"batch n={len(body.items)}",
                     request.client.host if request.client else "")
    return {"ok": True, "updated": len(body.items)}
