"""FastAPI 入口。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
import asyncio
from contextlib import suppress

import aiosqlite
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .api import auth_router, batch_router, config_router, credential_router, dashboard_router, qualification_router, sms_router, upi_router, success_router, provider_router
from .api import proxy_router, integration_router
from .services.integration_service import push_worker,recover_pushes
from .config import get_settings
from .database import init_db
from .services.batch_service import reset_stuck_batches, synchronize_account_statuses
from .utils import crypto
from .version import VERSION

BASE_DIR = Path(__file__).resolve().parent.parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    await init_db()
    await synchronize_account_statuses(str(s.db_path))
    recovered = await reset_stuck_batches(str(s.db_path))
    if recovered:
        print(f"[lifespan] recovered {recovered} stuck batch(es) to paused")
    # 首次启动创建管理员
    async with aiosqlite.connect(str(s.db_path)) as db:
        async with db.execute("SELECT COUNT(*) AS c FROM admins") as cur:
            n = (await cur.fetchone())[0]
        if n == 0 and s.admin_password:
            await db.execute(
                "INSERT INTO admins(username, password_hash) VALUES(?,?)",
                (s.admin_username, crypto.hash_password(s.admin_password)),
            )
            await db.commit()
    async with aiosqlite.connect(str(s.db_path)) as db:
        await recover_pushes(db)
    worker=asyncio.create_task(push_worker(str(s.db_path)))
    try:yield
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):await worker


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(title=s.title, version=VERSION, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(auth_router.router)
    app.include_router(credential_router.router)
    app.include_router(batch_router.router)
    app.include_router(config_router.router)
    app.include_router(dashboard_router.router)
    app.include_router(qualification_router.router)
    app.include_router(sms_router.router)
    app.include_router(upi_router.router)
    app.include_router(success_router.router)
    app.include_router(proxy_router.router)
    app.include_router(provider_router.router)
    app.include_router(integration_router.router)

    @app.get("/api/health")
    async def health():
        return {"ok": True,'version':VERSION}

    static_dir = BASE_DIR / "static"
    static_dir.mkdir(exist_ok=True)
    app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")
    return app


app = create_app()
