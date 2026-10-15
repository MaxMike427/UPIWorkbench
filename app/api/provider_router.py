from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth import get_current_admin
from ..services.provider_catalog import PROVIDERS, get_provider as find_provider, providers_for

router = APIRouter(prefix="/api/providers", tags=["providers"])


@router.get("")
async def list_providers(capability: str | None = Query(default=None), admin: dict = Depends(get_current_admin)):
    items = providers_for(capability) if capability else PROVIDERS
    return {"items": items, "total": len(items)}


@router.get("/{provider_id}")
async def get_provider(provider_id: str, admin: dict = Depends(get_current_admin)):
    item = find_provider(provider_id)
    if not item:
        raise HTTPException(status_code=404, detail="服务商不存在")
    return item
