"""SQLite + aiosqlite 连接与初始化。表结构以 ARCHITECTURE.md §2.1 为准。"""
from __future__ import annotations

import aiosqlite

from .config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS admins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS configs (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    description TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL,
    password TEXT NOT NULL,
    twofa_secret TEXT,
    access_token TEXT,
    source_file TEXT,
    status TEXT DEFAULT 'pending',
    error_msg TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS qualification_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    credential_id INTEGER NOT NULL REFERENCES credentials(id),
    at_token TEXT NOT NULL,
    has_qualification BOOLEAN NOT NULL,
    raw_response TEXT,
    checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS sms_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    credential_id INTEGER NOT NULL REFERENCES credentials(id),
    phone_number TEXT,
    sms_code TEXT,
    new_access_token TEXT,
    status TEXT DEFAULT 'waiting',
    retry_count INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 3,
    provider TEXT,
    order_id TEXT,
    error_msg TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS upi_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    credential_id INTEGER NOT NULL REFERENCES credentials(id),
    sms_task_id INTEGER REFERENCES sms_tasks(id),
    amount INTEGER NOT NULL,
    note TEXT,
    callback_url TEXT,
    upi_link TEXT,
    qr_code_data TEXT,
    status TEXT DEFAULT 'pending',
    raw_response TEXT,
    error_msg TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS task_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    total_credentials INTEGER DEFAULT 0,
    status TEXT DEFAULT 'running',
    current_step TEXT,
    progress_json TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id INTEGER REFERENCES admins(id),
    action TEXT NOT NULL,
    target_type TEXT,
    target_id INTEGER,
    detail TEXT,
    ip TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS runtime_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level TEXT DEFAULT 'info',
    message TEXT NOT NULL,
    batch_id INTEGER REFERENCES task_batches(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS account_exports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    credential_id INTEGER NOT NULL REFERENCES credentials(id),
    sms_task_id INTEGER NOT NULL REFERENCES sms_tasks(id),
    format TEXT NOT NULL,
    paths_json TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(sms_task_id, format)
);
CREATE INDEX IF NOT EXISTS idx_exports_credential ON account_exports(credential_id,format);
CREATE INDEX IF NOT EXISTS idx_credentials_status ON credentials(status);
CREATE INDEX IF NOT EXISTS idx_sms_tasks_credential ON sms_tasks(credential_id);
CREATE INDEX IF NOT EXISTS idx_upi_tasks_credential ON upi_tasks(credential_id);
CREATE INDEX IF NOT EXISTS idx_batches_status ON task_batches(status);
CREATE TABLE IF NOT EXISTS batch_members (
    batch_id INTEGER NOT NULL REFERENCES task_batches(id),
    credential_id INTEGER NOT NULL REFERENCES credentials(id),
    stage TEXT DEFAULT 'pending',
    proxy_url TEXT,
    error TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (batch_id, credential_id)
);
CREATE INDEX IF NOT EXISTS idx_batch_members_batch ON batch_members(batch_id);
CREATE TABLE IF NOT EXISTS proxies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    protocol TEXT NOT NULL,
    host TEXT NOT NULL,
    port INTEGER NOT NULL,
    is_dynamic INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS integration_pushes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    credential_id INTEGER NOT NULL REFERENCES credentials(id),
    batch_id INTEGER REFERENCES task_batches(id),
    instance_url TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    template_json TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    attempt_count INTEGER DEFAULT 0,
    remote_account_id INTEGER,
    error_msg TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(instance_url, credential_id, fingerprint)
);
"""

_MIGRATIONS = [
    ("task_batches", "amount", "ALTER TABLE task_batches ADD COLUMN amount INTEGER DEFAULT 0"),
    ("task_batches", "note", "ALTER TABLE task_batches ADD COLUMN note TEXT DEFAULT ''"),
    ("task_batches", "source_batch_id", "ALTER TABLE task_batches ADD COLUMN source_batch_id INTEGER REFERENCES task_batches(id)"),
    ("task_batches", "report_path", "ALTER TABLE task_batches ADD COLUMN report_path TEXT"),
    ("task_batches", "workflow_version", "ALTER TABLE task_batches ADD COLUMN workflow_version INTEGER DEFAULT 1"),
    ("credentials", "original_access_token", "ALTER TABLE credentials ADD COLUMN original_access_token TEXT"),
    ("batch_members", "at_snapshot", "ALTER TABLE batch_members ADD COLUMN at_snapshot TEXT"),
    ("batch_members", "upi_task_id", "ALTER TABLE batch_members ADD COLUMN upi_task_id INTEGER REFERENCES upi_tasks(id)"),
    ("batch_members", "sms_task_id", "ALTER TABLE batch_members ADD COLUMN sms_task_id INTEGER REFERENCES sms_tasks(id)"),
    ("upi_tasks", "external_job_id", "ALTER TABLE upi_tasks ADD COLUMN external_job_id TEXT"),
    ("task_batches", "deleted_at", "ALTER TABLE task_batches ADD COLUMN deleted_at TEXT"),
    ("task_batches", "snapshot_json", "ALTER TABLE task_batches ADD COLUMN snapshot_json TEXT"),
    ("integration_pushes", "next_attempt_at", "ALTER TABLE integration_pushes ADD COLUMN next_attempt_at REAL DEFAULT 0"),
    ("proxies", "is_dynamic", "ALTER TABLE proxies ADD COLUMN is_dynamic INTEGER NOT NULL DEFAULT 0"),
    ("batch_members", "proxy_url", "ALTER TABLE batch_members ADD COLUMN proxy_url TEXT"),
]

DEFAULT_CONFIGS = [
    ("proxy.enabled", "false", "启用代理库"),
    ("sms.enabled", "true", "启用接码"),
    ("sub2api.enabled", "false", "启用 sub2api 推送"),
    ("sub2api.base_url", "", "sub2api 实例 URL"),
    ("sub2api.admin_key", "", "sub2api 管理员 Admin Key"),
    ("sub2api.template", '{"group_ids":[],"concurrency":3,"rate_multiplier":1.0,"models":[],"update_existing":false}', "sub2api 推送模板"),
    ("extract_provider.provider", "ytc", "提炼服务商"),
    ("extract_provider.base_url", "https://ytc.xxsyun.xyz", "提炼平台接口地址"),
    ("extract_provider.cdk", "", "提炼平台兑换码"),
    ("payment_provider.provider", "ytc", "支付服务商"),
    ("payment_provider.base_url", "https://upi.xxsyun.xyz", "支付平台接口地址"),
    ("payment_provider.cdk", "", "支付平台兑换码"),
    ("app.concurrency", "3", "并发控制"),
    ("sms_provider.cdk", "", "接码平台兑换码"),
    ("upi_provider.cdk", "", "备用提链平台兑换码"),
    ("qualification.api_url", "https://cha.nerver.cc", "资格检查接口地址"),
    ("qualification.api_key", "", "资格检查访问密钥"),
    ("qualification.promo_id", "plus-1-month-free", "优惠活动编号"),
    ("sms_provider.provider", "handy", "接码服务商"),
    ("sms_provider.base_url", "https://apihub.space/handy", "接码平台接口地址"),
    ("upi_provider.provider", "ytc", "提炼/支付服务商"),
    ("upi_provider.base_url", "https://ytc.xxsyun.xyz", "提链平台接口地址"),
    ("upi_provider.api_key", "", "提链平台访问密钥（ohk_ 开头）"),
]


async def init_db(db_path: str | None = None) -> str:
    settings = get_settings()
    path = db_path or str(settings.db_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(path) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.executescript(SCHEMA)
        for key, value, desc in DEFAULT_CONFIGS:
            await db.execute(
                "INSERT OR IGNORE INTO configs(key, value, description) VALUES(?,?,?)",
                (key, value, desc),
            )
        for table, column, sql in _MIGRATIONS:
            async with db.execute(f"PRAGMA table_info({table})") as cur:
                cols = [r[1] async for r in cur]
            if column not in cols:
                await db.execute(sql)
        await db.execute('CREATE INDEX IF NOT EXISTS idx_integration_queue ON integration_pushes(status,instance_url,next_attempt_at,id)')
        await db.execute("UPDATE credentials SET original_access_token=access_token WHERE original_access_token IS NULL")
        await db.execute("UPDATE batch_members SET at_snapshot=(SELECT original_access_token FROM credentials WHERE id=credential_id) WHERE at_snapshot IS NULL")
        await db.commit()
    return path


def get_db_path(db_path: str | None = None) -> str:
    if db_path:
        return db_path
    return str(get_settings().db_path)


async def get_db(db_path: str | None = None):
    """FastAPI dependency：每次请求一个连接。"""
    path = get_db_path(db_path)
    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    try:
        yield db
    finally:
        await db.close()


async def log_action(db, admin_id: int | None, action: str, target_type: str | None = None,
                     target_id: int | None = None, detail: str = "", ip: str = "") -> None:
    await db.execute(
        "INSERT INTO audit_logs(admin_id, action, target_type, target_id, detail, ip)"
        " VALUES(?,?,?,?,?,?)",
        (admin_id, action, target_type, target_id, detail, ip),
    )
    await db.commit()


async def log_runtime(db, message: str, level: str = "info", batch_id: int | None = None) -> None:
    await db.execute(
        "INSERT INTO runtime_logs(level, message, batch_id) VALUES(?,?,?)",
        (level, message, batch_id),
    )
    await db.commit()
