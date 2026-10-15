"""供应商流程原子配置；凭证按服务商隔离，只向前端返回保存状态。"""
from __future__ import annotations

from ..config import get_settings
from .provider_catalog import get_provider, supports_provider_proxy
from .upi_providers import select_ytc_api_key


async def config_values(db) -> dict:
    async with db.execute("SELECT key,value FROM configs") as cur:
        return {r["key"]: r["value"] async for r in cur}


def credential_key(pid: str) -> str:
    return f"provider_credentials.{pid}.cdk"


def current_profile(values: dict) -> dict:
    extract = values.get("extract_provider.provider") or values.get("upi_provider.provider", "ytc")
    if extract == "local_zero_link": extract = "local"
    item = get_provider(extract) or {}
    payment = values.get("payment_provider.provider") or extract
    return {"flow_type": values.get("supplier.flow_type") or ("extract_pay" if item.get("combined") else "extract"),
            "extract_provider": extract, "payment_provider": payment,
            "proxy_source": values.get("supplier.proxy_source") or ("provider" if values.get("ytc.proxy_source") == "provider" else "system"),
            "sms_enabled": values.get("sms.enabled", "true") == "true",
            "sms_provider": values.get("sms_provider.provider", "handy")}


def provider_credential(values: dict, pid: str) -> str:
    key = credential_key(pid)
    if key in values: return values[key]
    if pid == "xxsyun_upi":
        return select_ytc_api_key(*(values.get(k, "") for k in
                                   ("provider_credentials.ytc.cdk", "payment_provider.cdk",
                                    "extract_provider.cdk", "upi_provider.cdk", "upi_provider.api_key")))
    profile = current_profile(values)
    if pid == "ytc" and pid in (profile["extract_provider"], profile["payment_provider"]):
        key = select_ytc_api_key(*(values.get(k, "") for k in
                                  ("extract_provider.cdk", "payment_provider.cdk", "upi_provider.cdk", "upi_provider.api_key")))
        return key if key.startswith("ohk_") else ""
    if pid == profile["sms_provider"]: return values.get("sms_provider.cdk", "")
    if pid == profile["extract_provider"]: return values.get("extract_provider.cdk", "")
    if pid == profile["payment_provider"]: return values.get("payment_provider.cdk", "")
    return ""


def provider_api_key(values: dict, pid: str) -> str:
    """ytc/OrderHub 使用账户 API Key；其它供应商仍使用各自 CDK。"""
    if pid in ("ytc", "xxsyun_upi"):
        return select_ytc_api_key(*(values.get(k, "") for k in
                                   ("provider_credentials.ytc.cdk", "provider_credentials.xxsyun_upi.cdk",
                                    "extract_provider.cdk", "payment_provider.cdk", "upi_provider.cdk", "upi_provider.api_key")))
    return provider_credential(values, pid)


def profile_provider_ids(profile: dict) -> list[str]:
    ids = [profile["extract_provider"], profile["payment_provider"]]
    if profile["sms_enabled"]: ids.append(profile["sms_provider"])
    ids=list(dict.fromkeys(pid for pid in ids if pid and pid != "local"))
    if 'ytc' in ids and 'xxsyun_upi' in ids: ids.remove('xxsyun_upi')
    return ids


def credential_storage_key(values: dict, pid: str) -> str:
    target=credential_key(pid)
    if target in values:return target
    value=provider_credential(values,pid)
    if not value:return target
    profile=current_profile(values)
    candidates=[]
    if pid=='ytc':candidates=['extract_provider.cdk','payment_provider.cdk','upi_provider.cdk','upi_provider.api_key']
    else:
        for prefix,selected in (('sms_provider',profile['sms_provider']),('extract_provider',profile['extract_provider']),('payment_provider',profile['payment_provider'])):
            if selected==pid:candidates.append(prefix+'.cdk')
    return next((key for key in candidates if values.get(key)==value),target)


async def get_supplier_config(db) -> dict:
    from .provider_catalog import PROVIDERS
    values = await config_values(db)
    profile = current_profile(values)
    extract, payment, sms = profile["extract_provider"], profile["payment_provider"], profile["sms_provider"]
    names = {p["id"]: p["name"] for p in PROVIDERS}
    short = lambda pid: {"ytc": "ytc", "handy": "Handy", "local": "本地提链"}.get(pid, names.get(pid, pid))
    summary = f"{short(extract)} {'内置代理' if profile['proxy_source']=='provider' else '系统代理'}"
    summary += " · 提链+支付" if profile["flow_type"] == "extract_pay" else f" · {short(payment)} 支付"
    summary += f" · {short(sms)} 接码" if profile["sms_enabled"] else " · 不接码"
    credential_items = []
    for pid in profile_provider_ids(profile):
        item = get_provider(pid) or {}
        credential_items.append({"provider": pid, "name": short(pid), "key": credential_storage_key(values,pid),
                                 "label": "ytc API Key（ohk_）" if pid == "ytc" or pid == "xxsyun_upi" else f"{short(pid)} CDK",
                                 "saved": bool(provider_api_key(values, pid) if pid in ("ytc", "xxsyun_upi") else provider_credential(values, pid)),
                                 "buy_url": item.get("buy_url", ""), "redeem_url": item.get("url", "")})
    executable = (((extract == payment and extract in ("ytc", "wangyou")) or
                   (extract == "ytc" and payment == "xxsyun_upi") or
                   (extract == "local" and payment in ("masi", "xxsyun_upi"))) and
                  (not profile["sms_enabled"] or sms == "handy"))
    return {**profile, "configured": "supplier.flow_type" in values, "summary": summary,
            "executable": executable, "warning": "" if executable else "此配置为占位，暂未开放自动任务",
            "credential_items": credential_items,
            "credential_status": {p["id"]: bool(provider_credential(values, p["id"])) for p in PROVIDERS
                                  if p["category"] != "account"},
            "storage": {"database": str(get_settings().db_path), "table": "configs"}}


async def save_supplier_config(db, body) -> dict:
    profile = body.model_dump(exclude={"credentials"})
    extract = get_provider(body.extract_provider)
    if not extract or extract["provider_type"] not in ("extract", "extract_pay"):
        raise ValueError("请选择有效的提链供应商")
    if (body.flow_type == "extract_pay") != bool(extract["combined"]) and not (
            body.flow_type == "extract" and body.extract_provider == "ytc" and body.payment_provider == "xxsyun_upi"):
        raise ValueError("供应商与提链类型不匹配")
    if body.proxy_source == "provider" and not supports_provider_proxy(body.extract_provider):
        raise ValueError("该供应商未确认支持内置代理，请选择系统代理")
    if body.flow_type == "extract_pay":
        if body.payment_provider and body.payment_provider != body.extract_provider:
            if not (body.extract_provider == "ytc" and body.payment_provider == "xxsyun_upi"):
                raise ValueError("提链支付一体供应商不可另选支付供应商")
        profile["payment_provider"] = body.extract_provider
    else:
        payment = get_provider(body.payment_provider)
        if not payment or (payment["provider_type"] != "pay" and not (body.extract_provider == 'ytc' and body.payment_provider == 'xxsyun_upi')):
            raise ValueError("请选择独立支付供应商")
    if body.sms_enabled:
        sms = get_provider(body.sms_provider)
        if not sms or sms["category"] != "sms": raise ValueError("请选择接码供应商")
    else:
        profile["sms_provider"] = ""
    values = await config_values(db)
    selected = profile_provider_ids(profile)
    executable=(((profile['extract_provider']==profile['payment_provider'] and profile['extract_provider'] in ('ytc','wangyou')) or
                 (profile['extract_provider']=='ytc' and profile['payment_provider']=='xxsyun_upi') or
                 (profile['extract_provider']=='local' and profile['payment_provider'] in ('masi','xxsyun_upi'))) and
                (not profile['sms_enabled'] or profile['sms_provider']=='handy'))
    if any(pid not in selected for pid in body.credentials): raise ValueError("不能保存未选中供应商的凭证")
    secrets = {}
    for pid in selected:
        value = body.credentials.get(pid, "").strip()
        if "****" in value: raise ValueError("不可保存脱敏占位符，请输入新凭证或留空保留")
        secrets[pid] = value or provider_credential(values, pid)
        if executable and not secrets[pid]: raise ValueError(f"请填写 {pid} 的 CDK/API Key")
        if pid == "ytc" and secrets[pid] and not secrets[pid].startswith("ohk_"):
            raise ValueError("ytc 需要 ohk_ 开头的账户 API Key，请先在供应商网站兑换 CDK")
    changes = {"supplier.flow_type": profile["flow_type"], "supplier.proxy_source": profile["proxy_source"],
               "extract_provider.provider": profile["extract_provider"], "payment_provider.provider": profile["payment_provider"],
               "sms.enabled": "true" if profile["sms_enabled"] else "false",
               "sms_provider.provider": profile["sms_provider"], "ytc.proxy_source": "provider" if body.proxy_source == "provider" else "provided",
               "upi_provider.provider": profile["extract_provider"],
               "extract_provider.cdk": secrets.get(profile["extract_provider"], ""),
               "payment_provider.cdk": secrets.get(profile["payment_provider"], ""),
               "sms_provider.cdk": secrets.get(profile["sms_provider"], "")}
    # 首次保存新流程之前归档旧凭证；关闭接码或换供应商不能丢失原来的 CDK。
    previous=current_profile(values)
    for pid in dict.fromkeys((previous['extract_provider'],previous['payment_provider'],previous['sms_provider'])):
        if get_provider(pid) and pid!='local' and credential_key(pid) not in values:
            value=provider_credential(values,pid)
            if value:changes[credential_key(pid)]=value
    for pid, value in secrets.items(): changes[credential_key(pid)] = value
    for prefix, pid in (("extract_provider", profile["extract_provider"]), ("payment_provider", profile["payment_provider"]),
                        ("sms_provider", profile["sms_provider"])):
        item = get_provider(pid)
        if item: changes[prefix + ".base_url"] = item["url"].rstrip("/")
    try:
        for key, value in changes.items():
            await db.execute("INSERT INTO configs(key,value) VALUES(?,?) ON CONFLICT(key)"
                             " DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP", (key, value))
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return await get_supplier_config(db)
