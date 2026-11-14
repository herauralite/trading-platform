"""Validated models for Trader API responses.

Contract source: Match-Trader Trader API documentation as captured in
MATCHTRADER_API_INTEGRATION.md (reviewed 2026-11-14). Rules applied here:
* ids are strings; money/price/volume are Decimal (never float);
* times are UTC epoch milliseconds, exposed as timezone-aware datetimes with ms kept;
* enums are kept as the provider's strings; null or unknown values stay null/unknown and are
  never coerced to BUY or zero;
* unknown extra fields are ignored for typed access but preserved in ``raw``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


def ms_to_datetime(value: int | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)


def datetime_to_ms(value: datetime) -> int:
    return int(round(value.timestamp() * 1000))


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, coerce_numbers_to_str=False)
    raw: dict[str, Any] = {}

    @model_validator(mode="before")
    @classmethod
    def _keep_raw(cls, data: Any) -> Any:
        if isinstance(data, dict) and "raw" not in data:
            return {**data, "raw": dict(data)}
        return data

    @field_validator("*", mode="before")
    @classmethod
    def _no_float_money(cls, value: Any, info) -> Any:
        # JSON numbers arrive as float/int; go through str so Decimal keeps the printed value.
        annotation = str(cls.model_fields[info.field_name].annotation) if info.field_name in cls.model_fields else ""
        if "Decimal" in annotation and isinstance(value, (int, float)) and not isinstance(value, bool):
            return Decimal(str(value))
        return value


class _StringId(_Model):
    @field_validator("id", "uid", "closingOrderId", "login", mode="before", check_fields=False)
    @classmethod
    def _ids_are_strings(cls, value: Any) -> Any:
        if isinstance(value, bool) or value is None:
            return value
        return str(value) if isinstance(value, (int, float)) else value


class PlatformDetails(_Model):
    brokerName: str | None = None
    platformUrl: str | None = None


class AccountProfile(_StringId):
    login: str
    currency: str | None = None
    accountType: str | None = None
    currencyPrecision: int | None = None
    leverage: int | None = None
    createTime: int | None = None
    accessRight: str | None = None
    # email / firstName / lastName are intentionally not modelled: PII we do not need.


class AccountSummary(_Model):
    balance: Decimal
    credit: Decimal | None = None
    equity: Decimal
    margin: Decimal | None = None
    freeFunds: Decimal | None = None
    marginLevel: Decimal | None = None
    profit: Decimal | None = None


class OpenPosition(_StringId):
    id: str
    symbol: str
    alias: str | None = None
    comment: str | None = None
    side: str | None = None
    volume: Decimal
    openPrice: Decimal
    currentPrice: Decimal | None = None
    commission: Decimal | None = None
    swap: Decimal | None = None
    profit: Decimal | None = None
    netProfit: Decimal | None = None
    margin: Decimal | None = None
    openTime: int
    tpPrice: Decimal | None = None
    slPrice: Decimal | None = None
    trailingDistance: Decimal | None = None


class PendingOrder(_StringId):
    id: str
    symbol: str
    alias: str | None = None
    comment: str | None = None
    side: str | None = None
    type: str | None = None
    volume: Decimal
    activationPrice: Decimal | None = None
    tpPrice: Decimal | None = None
    slPrice: Decimal | None = None
    createTime: int | None = None
    lastUpdateTime: int | None = None


class ClosedPosition(_StringId):
    uid: str
    id: str
    closingOrderId: str | None = None
    symbol: str
    alias: str | None = None
    comment: str | None = None
    closeReason: str | None = None
    side: str | None = None
    volume: Decimal
    openPrice: Decimal | None = None
    closePrice: Decimal | None = None
    commission: Decimal | None = None
    swap: Decimal | None = None
    profit: Decimal | None = None
    netProfit: Decimal | None = None
    openTime: int | None = None
    closeTime: int
    slPrice: Decimal | None = None
    tpPrice: Decimal | None = None


class LedgerEntry(_StringId):
    id: str
    comment: str | None = None
    amount: Decimal
    timestamp: int
    type: str | None = None


class ContractViolation(Exception):
    """The response did not match the documented envelope/schema. Never treated as 'empty'."""


def parse_envelope(payload: Any, key: str, model: type[_Model]) -> list:
    if not isinstance(payload, dict) or not isinstance(payload.get(key), list):
        raise ContractViolation(f"missing '{key}' array")
    try:
        return [model.model_validate(item) for item in payload[key]]
    except Exception as exc:
        raise ContractViolation(f"invalid '{key}' entry ({type(exc).__name__})") from None


def parse_object(payload: Any, model: type[_Model]):
    if not isinstance(payload, dict):
        raise ContractViolation("expected an object")
    try:
        return model.model_validate(payload)
    except Exception as exc:
        raise ContractViolation(f"invalid object ({type(exc).__name__})") from None
