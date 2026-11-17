"""Feature flag for account setup. Enabled with Match-Trader unless explicitly disabled."""
from __future__ import annotations

import os

ENV_FLAG = "TALITRADE_ACCOUNT_SETUP_ENABLED"


def enabled() -> bool:
    """Account setup routes and schema. Needs the Match-Trader API integration as well, because
    setup is attached to linked Match-Trader accounts. Off => routes answer 404, the dashboard
    keeps its existing flow, and any saved profiles stay in the database untouched."""
    from app.services.matchtrader_api import config as mt_config

    return mt_config.api_enabled() and str(os.getenv(ENV_FLAG, "true")).strip().lower() in {"1", "true", "yes", "on"}
