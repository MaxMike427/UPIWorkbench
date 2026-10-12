"""配置加载/保存：yaml + 环境变量覆盖。"""
from __future__ import annotations

import os
import re
from pathlib import Path
from functools import lru_cache

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent


def parse_concurrency(value: str) -> int:
    """并发必须是非零正整数，禁止浮点、符号与科学记数法。"""
    if not isinstance(value,str) or not re.fullmatch(r'[0-9]+',value.strip()):
        raise ValueError('并发数必须是非零正整数')
    try:
        number=int(value.strip())
    except ValueError:
        raise ValueError('并发数必须是非零正整数')
    if number<=0:
        raise ValueError('并发数必须是非零正整数')
    return number


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class Settings:
    def __init__(self, data: dict | None = None):
        data = data or {}
        app = data.get("app", {})
        qual = data.get("qualification", {})
        sms = data.get("sms_provider", {})
        upi = data.get("upi_provider", {})
        self.title: str = os.getenv("UPI_TITLE", app.get("title", "UPI Workbench"))
        self.host: str = os.getenv("UPI_HOST", app.get("host", "127.0.0.1"))
        self.port: int = int(os.getenv("UPI_PORT", app.get("port", 8000)))
        self.data_dir: Path = Path(os.getenv("UPI_DATA_DIR", app.get("data_dir", "./data")))
        if not self.data_dir.is_absolute():
            self.data_dir = (BASE_DIR / self.data_dir).resolve()
        db_path = os.getenv("UPI_DB_PATH", app.get("db_path", "./data/db.sqlite"))
        self.db_path: Path = Path(db_path)
        if not self.db_path.is_absolute():
            self.db_path = (BASE_DIR / self.db_path).resolve()
        self.jwt_secret: str = os.getenv("UPI_JWT_SECRET", app.get("jwt_secret", "CHANGE_ME"))
        self.access_expire_minutes: int = int(
            os.getenv("UPI_ACCESS_MINUTES", app.get("access_token_expire_minutes", 15))
        )
        self.refresh_expire_days: int = int(
            os.getenv("UPI_REFRESH_DAYS", app.get("refresh_token_expire_days", 7))
        )
        self.admin_username: str = os.getenv("UPI_ADMIN_USER", app.get("admin_username", "admin"))
        self.admin_password: str = os.getenv("UPI_ADMIN_PASS", app.get("admin_password", "admin123"))
        self.purchase_url: str = os.getenv("UPI_PURCHASE_URL", app.get("purchase_url", "{购买网址}"))
        self.report_dir: Path = Path(os.getenv("UPI_REPORT_DIR", app.get("report_dir", str(BASE_DIR / "doc" / "task_reports"))))
        self.qual_api_url: str = os.getenv("UPI_QUAL_URL", qual.get("api_url", ""))
        self.qual_api_key: str = os.getenv("UPI_QUAL_KEY", qual.get("api_key", ""))
        self.sms_cdk: str = os.getenv("UPI_SMS_CDK", sms.get("cdk", ""))
        self.upi_cdk: str = os.getenv("UPI_UPI_CDK", upi.get("cdk", ""))


@lru_cache(maxsize=1)
def get_settings(config_path: str | None = None) -> Settings:
    path = Path(config_path) if config_path else BASE_DIR / "config.yaml"
    if not path.exists():
        path = BASE_DIR / "config.example.yaml"
    return Settings(_load_yaml(path))
