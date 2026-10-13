"""配置管理：GET 脱敏 / PUT 单项 / POST 批量。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request

from ..auth import get_current_admin
from ..config import parse_concurrency
from ..database import get_db, log_action
from ..models import BatchConfigUpdate, ConfigItem, ConfigUpdate, IntegrationTemplate
from ..utils.helpers import mask_secret

router = APIRouter(prefix="/api/configs", tags=["configs"])


SENSITIVE_SUFFIX = (".cdk", ".key", ".api_key", ".admin_key", ".secret", ".password", ".token")


def validate_config(key,value):
    if key in ('sms.enabled','sub2api.enabled','proxy.enabled') and value not in ('true','false'):
        raise ValueError('开关必须为 true 或 false')
    if key=='sub2api.base_url' and value:
        from ..services.integration_service import normalize_url
        return normalize_url(value)
    if key=='sub2api.template':
        try:return json.dumps(IntegrationTemplate.model_validate_json(value).model_dump())
        except ValueError:raise ValueError('sub2api 推送模板无效，请检查分组、并发、倍率与模型')
    return value


async def validate_integration_settings(db,values):
    if not any(key.startswith('sub2api.') for key in values):return
    async with db.execute("SELECT key,value FROM configs WHERE key IN ('sub2api.enabled','sub2api.base_url','sub2api.admin_key')") as cur:
        merged={row['key']:row['value'] async for row in cur}
    merged.update(values)
    if merged.get('sub2api.enabled')=='true' and (not merged.get('sub2api.base_url') or not merged.get('sub2api.admin_key')):
        raise ValueError('启用集成前请保存 sub2api URL 和管理员 Admin Key')


def _masked(key: str, value: str) -> tuple[str, bool]:
    if key.endswith(SENSITIVE_SUFFIX) and value:
        return mask_secret(value), True
    return value, False


@router.get("/workspace", response_model=list[ConfigItem])
async def workspace_configs(admin: dict = Depends(get_current_admin), db=Depends(get_db)):
    async with db.execute("SELECT key,value FROM configs WHERE key IN ('sms_provider.cdk','sms_provider.provider','upi_provider.cdk','upi_provider.api_key','upi_provider.provider','app.concurrency','sms.enabled','sub2api.enabled','sub2api.base_url','sub2api.admin_key','extract_provider.cdk','extract_provider.provider','payment_provider.cdk','payment_provider.provider')") as cur:
        values = {row["key"]: row["value"] async for row in cur}
    out = []
    for key, description in (("sms_provider.cdk", "接码 CDK"), ("upi_provider.cdk", "提链支付 CDK"), ("extract_provider.cdk", "提炼 CDK"), ("payment_provider.cdk", "支付 CDK"), ("sms_provider.provider", "接码服务商"), ("upi_provider.provider", "提炼/支付服务商"), ("extract_provider.provider", "提炼服务商"), ("payment_provider.provider", "支付服务商")):
        raw = values.get(key, "")
        if key=="upi_provider.cdk" and not raw:
            raw=values.get("upi_provider.api_key", "")
        value, masked = _masked(key, raw)
        out.append({"key":key,"description":description,"value":value,"masked":masked})
    out.append({'key':'app.concurrency','description':'并发控制','value':values.get('app.concurrency','3'),'masked':False})
    for key in ('sms.enabled','sub2api.enabled','sub2api.base_url','sub2api.admin_key'):
        value,masked=_masked(key,values.get(key,''))
        out.append({'key':key,'description':key,'value':value,'masked':masked})
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
    try:value=validate_config(key,value)
    except ValueError as e:raise HTTPException(status_code=400,detail=str(e))
    if key=='app.concurrency':
        try:value=str(parse_concurrency(value))
        except ValueError as e:raise HTTPException(status_code=400,detail=str(e))
    try:await validate_integration_settings(db,{key:value})
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
    try:values={k:validate_config(k,v) for k,v in values.items()}
    except ValueError as e:raise HTTPException(status_code=400,detail=str(e))
    if 'app.concurrency' in values:
        try:values['app.concurrency']=str(parse_concurrency(values['app.concurrency']))
        except ValueError as e:raise HTTPException(status_code=400,detail=str(e))
    try:await validate_integration_settings(db,values)
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
