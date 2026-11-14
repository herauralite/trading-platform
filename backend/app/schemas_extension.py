from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class PairStartRequest(BaseModel):
    device_label: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PairCompleteRequest(BaseModel):
    pair_code: str
    pair_secret: str
    device_fingerprint: str
    label: str | None = None
    platform: str | None = None
    browser: str | None = None
    extension_version: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class HeartbeatRequest(BaseModel):
    metadata: dict[str, Any] = Field(default_factory=dict)
    session_metadata: dict[str, Any] = Field(default_factory=dict)


class PlatformSessionItem(BaseModel):
    adapter_key: str
    platform_key: str
    tab_id: str | int
    tab_url: str | None = None
    platform_account_ref: str | None = None
    session_ref: str | None = None
    status: str = "active"
    capabilities: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PlatformSessionsUpsertRequest(BaseModel):
    sessions: list[PlatformSessionItem] = Field(default_factory=list)


class StateSyncAccount(BaseModel):
    adapter_key: str
    platform_key: str
    platform_name: str | None = None
    platform_account_ref: str
    display_label: str | None = None
    account_type: str | None = None
    account_size: int | None = None
    platform_session_id: int | None = None
    tab_id: str | int | None = None
    session_ref: str | None = None
    snapshot: dict[str, Any] = Field(default_factory=dict)
    positions: list[dict[str, Any]] = Field(default_factory=list)
    orders: list[dict[str, Any]] = Field(default_factory=list)


class StateSyncRequest(BaseModel):
    accounts: list[StateSyncAccount] = Field(default_factory=list)


class ExecutionCommandInput(BaseModel):
    trading_account_id: int
    extension_device_id: int
    platform_session_id: int | None = None
    adapter_key: str
    command_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    expires_at: datetime | None = None


class ExecutionBatchRequest(BaseModel):
    request_id: str | None = None
    requested_by: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    commands: list[ExecutionCommandInput] = Field(default_factory=list)


class CommandAckRequest(BaseModel):
    status: Literal["acked", "running"] = "acked"
    metadata: dict[str, Any] = Field(default_factory=dict)


class CommandResultRequest(BaseModel):
    status: Literal["succeeded", "failed", "expired"]
    result_payload: dict[str, Any] = Field(default_factory=dict)
    adapter_error_code: str | None = None
    adapter_error_message: str | None = None
