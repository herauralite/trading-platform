from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class IngestTradingAccount(BaseModel):
    user_id: str | None = None
    connector_type: str = Field(default="manual")
    source_connector: str | None = None
    broker_name: str | None = None
    external_account_id: str
    display_label: str | None = None
    account_type: str | None = None
    account_size: int | None = None
    is_active: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class IngestAccountSnapshot(BaseModel):
    user_id: str | None = None
    connector_type: str = Field(default="manual")
    external_account_id: str
    timestamp: datetime | None = None
    balance: float | None = None
    equity: float | None = None
    drawdown: float | None = None
    risk_used: float | None = None
    source_metadata: dict[str, Any] = Field(default_factory=dict)


class IngestPosition(BaseModel):
    user_id: str | None = None
    connector_type: str = Field(default="manual")
    external_account_id: str
    symbol: str
    side: str | None = None
    size: float | None = None
    average_entry: float | None = None
    unrealized_pnl: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    opened_at: datetime | None = None
    source_metadata: dict[str, Any] = Field(default_factory=dict)


class IngestTrade(BaseModel):
    user_id: str | None = None
    connector_type: str = Field(default="manual")
    external_account_id: str
    account_type: str | None = None
    account_size: int | None = None
    symbol: str
    side: str
    size: float | None = None
    entry_price: float | None = None
    exit_price: float | None = None
    open_time: datetime | None = None
    close_time: datetime | None = None
    pnl: float | None = None
    fees: float | None = None
    tags: list[str] = Field(default_factory=list)
    source_metadata: dict[str, Any] = Field(default_factory=dict)
    import_provenance: dict[str, Any] = Field(default_factory=dict)


class IngestEvent(BaseModel):
    user_id: str | None = None
    connector_type: str = Field(default="manual")
    external_account_id: str | None = None
    event_type: str
    event_payload: dict[str, Any] = Field(default_factory=dict)
    event_time: datetime | None = None


class CsvTradeImportRequest(BaseModel):
    user_id: str | None = None
    connector_type: str = "csv_import"
    broker_name: str | None = "csv"
    external_account_id: str
    account_type: str | None = None
    account_size: int | None = None
    rows: list[dict[str, Any]]
