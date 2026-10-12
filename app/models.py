"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")

CredentialStatus = Literal["pending", "qualified", "failed", "processing", "done"]


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = 900


class RefreshRequest(BaseModel):
    refresh_token: str = ""


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)
    confirm_password: str


class AdminOut(BaseModel):
    id: int
    username: str


class CredentialOut(BaseModel):
    id: int
    account: str
    password: str
    twofa_secret: str = ""
    access_token: str = ""
    source_file: str = ""
    status: str = "pending"
    error_msg: str = ""


class PageOut(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int


class ImportResult(BaseModel):
    imported: int
    skipped: int
    filename: str


class BatchDeleteRequest(BaseModel):
    ids: list[int] = Field(min_length=1)


class ConfigItem(BaseModel):
    key: str
    value: str
    description: str = ""
    masked: bool = False


class ConfigUpdate(BaseModel):
    value: str


class BatchConfigUpdate(BaseModel):
    items: dict[str, str]


class BatchCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    credential_ids: list[int] = Field(min_length=1)
    amount: int = Field(default=0, ge=0, description="提链金额(paise)")
    note: str = ""


class BatchOut(BaseModel):
    id: int
    name: str
    total_credentials: int
    status: str
    current_step: str | None = None
    progress_json: str | None = None
    amount: int = 0
    note: str = ""
    members: list[dict] = Field(default_factory=list)
    source_batch_id: int | None = None
    report_available: bool = False


class DashboardStats(BaseModel):
    credentials_total: int
    by_status: dict[str, int]
    batches_total: int
    sms_total: int
    upi_total: int


class QualificationCheckRequest(BaseModel):
    credential_ids: list[int] = Field(default_factory=list)
    status: str | None = None  # 为空且 ids 为空时不查任何记录
    promo_id: str | None = None
    concurrency: int = Field(default=10, ge=1, le=50)


class QualificationCheckResult(BaseModel):
    checked: int
    qualified: int
    failed: int
    details: list[dict]


class SMSBindRequest(BaseModel):
    credential_ids: list[int] = Field(default_factory=list)
    status: str | None = None
    concurrency: int = Field(default=2, ge=1, le=5)
    max_retries: int = Field(default=5, ge=1, le=10)
    retry_interval_s: float = Field(default=120.0, ge=0, le=3600)


class SMSBindResult(BaseModel):
    total: int
    bound: int
    failed: int
    details: list[dict]


class UPICreateRequest(BaseModel):
    credential_ids: list[int] = Field(default_factory=list)
    status: str | None = None
    amount: int = Field(gt=0, description="金额(paise)")
    note: str = ""
    callback_url: str = ""
    concurrency: int = Field(default=3, ge=1, le=10)
    max_retries: int = Field(default=5, ge=1, le=10)
    retry_interval_s: float = Field(default=120.0, ge=0, le=3600)


class UPICreateResult(BaseModel):
    total: int
    success: int
    failed: int
    details: list[dict]
