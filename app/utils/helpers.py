"""通用小工具。"""
from __future__ import annotations

from datetime import datetime, timezone


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def mask_secret(value: str | None, show_last: int = 0) -> str:
    if not value:
        return ""
    if show_last > 0 and len(value) > show_last + 4:
        return "****" + value[-show_last:]
    return "****"


def build_page(items: list, total: int, page: int, page_size: int) -> dict:
    return {"items": items, "total": total, "page": page, "page_size": page_size}
