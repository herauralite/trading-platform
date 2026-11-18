"""The setup profile document and its validation.

Everything here is trader-entered. Broker-reported facts are never stored in the profile; they
are read from the Match-Trader tables when the profile is served (see ``store.detected``).
Money stays Decimal and travels as a string. Unknown keys are rejected so the document cannot
grow into an unbounded blob.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_DRAFT_BYTES = 4096
MAX_PROFILE_BYTES = 16384

Base = Literal["starting", "sod_balance", "sod_equity", "high_water", "high_water_equity"]


class _Doc(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Money(_Doc):
    """An amount or a percent, exactly as the trader entered it. ``base`` names what a percent is
    measured from; it is kept even when that reference is not known yet."""
    value: Decimal = Field(gt=0, le=Decimal("1000000000000"))
    unit: Literal["amount", "percent"]
    base: Base | None = None

    @model_validator(mode="after")
    def _percent_range(self):
        if self.unit == "percent" and self.value > 1000:
            raise ValueError("percent out of range")
        return self


class Size(_Doc):
    amount: Decimal = Field(gt=0, le=Decimal("10000000000"))
    currency: str

    @field_validator("currency")
    @classmethod
    def _iso(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Z]{3}", value or ""):
            raise ValueError("currency must be a 3-letter code")
        return value


class Goal(_Doc):
    id: Literal["payout", "grow", "limits", "other"]
    text: str = Field(default="", max_length=80)

    @model_validator(mode="after")
    def _other_needs_words(self):
        self.text = self.text.strip() if self.id == "other" else ""
        if self.id == "other" and not self.text:
            raise ValueError("an Other goal needs the trader's words")
        return self


class PhaseStart(_Doc):
    """The balance the trader confirmed a phase started from. Tied to that phase: it stops
    applying when the current phase changes."""
    phase: int = Field(ge=1, le=3)
    amount: Decimal = Field(gt=0, le=Decimal("10000000000"))


class CycleStart(_Doc):
    """Where the current payout cycle started, confirmed after the withdrawal at ``after``. A later
    withdrawal makes it outdated."""
    amount: Decimal = Field(gt=0, le=Decimal("10000000000"))
    after: datetime


class Evaluation(_Doc):
    phaseCount: int | None = Field(default=None, ge=1, le=3)
    currentPhase: int | None = Field(default=None, ge=1, le=3)
    phaseTarget: Money | None = None
    phaseStart: PhaseStart | None = None

    @model_validator(mode="after")
    def _phase_in_range(self):
        if self.phaseCount == 1:
            self.currentPhase = None          # a one-phase evaluation has no "which phase"
        if self.currentPhase and self.phaseCount and self.currentPhase > self.phaseCount:
            raise ValueError("current phase is after the last phase")
        return self


class Master(_Doc):
    goal: Goal | None = None
    payoutTarget: Money | None = None          # the trader's own target
    firmMin: Money | None = None               # the firm's minimum profit for a withdrawal, kept separately
    growthTarget: Money | None = None
    cycleStart: CycleStart | None = None
    profitSplit: Decimal | None = Field(default=None, gt=0, le=100)   # the trader's share of profit, in percent; targets stay gross


class FirmRules(_Doc):
    dailyLoss: Money | None = None
    dailyReset: str | None = None
    dailyResetZone: str | None = Field(default=None, max_length=64)    # IANA zone the reset time is in
    dailyFrom: Literal["sod_balance", "sod_equity"] | None = None     # what a starting-size limit counts each day from
    overall: Money | None = None
    overallMode: Literal["static", "trailing"] | None = None
    overallRef: Literal["balance", "equity"] | None = None

    @field_validator("dailyReset")
    @classmethod
    def _hhmm(cls, value):
        if value is not None and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("reset time must be HH:MM")
        return value

    @field_validator("dailyResetZone")
    @classmethod
    def _zone(cls, value):
        if value is None:
            return value
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("unknown time zone") from None
        return value


class Alerts(_Doc):
    daily: bool = False
    budget: bool = False


class Personal(_Doc):
    sessionBudget: Money | None = None
    riskPerTrade: Money | None = None
    style: Literal["Scalping", "Day trading", "Swing"] | None = None
    reminder: str | None = Field(default=None, max_length=120)
    alerts: Alerts | None = None


FLOWS = ("basics", "phase", "payout", "growth", "firm", "personal", "phaseStart", "cycle")


class Resume(_Doc):
    flow: Literal["basics", "phase", "payout", "growth", "firm", "personal", "phaseStart", "cycle"]
    step: str = Field(max_length=32, pattern=r"^[A-Za-z]+$")


class Profile(_Doc):
    schema_version: Literal[1] = 1
    status: Literal["new", "in_progress", "saved"] = "new"
    type: Literal["evaluation", "master"] | None = None
    size: Size | None = None
    generalGoal: Goal | None = None              # used only when the account type was skipped
    evaluation: Evaluation | None = None         # kept when the type changes, so switching back restores it
    master: Master | None = None
    firmRules: FirmRules | None = None
    personal: Personal | None = None
    done: dict[Literal["basics", "phase", "payout", "growth", "firm", "personal", "phaseStart", "cycle"], bool] = Field(default_factory=dict)
    resume: Resume | None = None
    draft: dict[str, Any] = Field(default_factory=dict)   # typed but not yet confirmed answers

    @field_validator("draft")
    @classmethod
    def _small_flat_draft(cls, value: dict) -> dict:
        def ok(v, depth=0):
            if v is None or isinstance(v, (bool, int, float)):
                return True
            if isinstance(v, str):
                return len(v) <= 200
            if isinstance(v, dict) and depth < 2:
                return len(v) <= 12 and all(isinstance(k, str) and len(k) <= 32 and ok(x, depth + 1) for k, x in v.items())
            return False
        if len(value) > 24 or not all(isinstance(k, str) and len(k) <= 32 and ok(v) for k, v in value.items()):
            raise ValueError("draft has an unexpected shape")
        if len(json.dumps(value, default=str)) > MAX_DRAFT_BYTES:
            raise ValueError("draft is too large")
        return value


def dump(profile: Profile) -> dict:
    return profile.model_dump(mode="json", exclude_none=True)


class SaveBody(_Doc):
    base_version: int = Field(ge=0)
    save_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    profile: dict[str, Any]
