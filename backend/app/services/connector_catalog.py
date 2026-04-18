from __future__ import annotations

from typing import Any, Callable

from app.services.connector_ingest import validate_fundingpips_connector_config

ConnectorConfigValidator = Callable[[dict[str, Any], dict[str, Any]], tuple[str, str | None]]

CONNECTOR_CATALOG: dict[str, dict[str, Any]] = {
    "fundingpips_extension": {
        "label": "FundingPips Extension",
        "category": "extension",
        "auth_mode": "browser_extension",
        "status": "live",
        "supports_live_sync": True,
        "requires_bridge": False,
        "beta": False,
        "integration_status": "live",
        "connection_layer": "broker_connector",
        "supported_capabilities": ["account_sync", "position_ingest", "trade_ingest"],
        "onboarding_copy": "Connect through the installed extension.",
        "connection_state_labels": {
            "connected": "Connected",
            "disconnected": "Disconnected",
        },
    },
    "mt5_bridge": {
        "label": "MetaTrader 5 (MT5 Bridge)",
        "category": "broker_platform",
        "auth_mode": "trusted_bridge_pairing",
        "status": "beta",
        "supports_live_sync": False,
        "requires_bridge": True,
        "beta": True,
        "integration_status": "beta_bridge_required",
        "connection_layer": "broker_connector",
        "notes": "Trusted bridge worker registration required.",
        "supported_capabilities": ["trusted_bridge_pairing", "account_shell_attach"],
        "onboarding_copy": "Pair your MT5 bridge, then attach an account.",
        "connection_state_labels": {
            "bridge_required": "Bridge required",
            "waiting_for_registration": "Waiting for bridge registration",
            "ready_for_account_attach": "Ready to attach account",
            "connected": "Connected",
        },
    },
    "tradingview_webhook": {
        "label": "TradingView Webhook",
        "category": "signal_provider",
        "auth_mode": "webhook_token",
        "status": "beta",
        "supports_live_sync": False,
        "requires_bridge": False,
        "beta": True,
        "integration_status": "beta_webhook",
        "connection_layer": "signal_ingest",
        "supported_capabilities": ["webhook_signal_ingestion"],
        "onboarding_copy": "Create a webhook endpoint and paste it into TradingView alerts.",
        "connection_state_labels": {
            "webhook_created": "Webhook created",
            "awaiting_alerts": "Awaiting TradingView alerts",
            "active": "Active (alerts received)",
        },
    },
    "alpaca_api": {
        "label": "Alpaca API",
        "category": "public_api",
        "auth_mode": "api_keys_beta_shell",
        "status": "coming_soon",
        "supports_live_sync": False,
        "requires_bridge": False,
        "beta": True,
        "integration_status": "beta_pending_secure_auth",
        "connection_layer": "public_api_connector",
        "supported_capabilities": ["beta_metadata_registration"],
        "onboarding_copy": "Register metadata now; secure credential support is coming soon.",
        "connection_state_labels": {
            "beta_pending": "Beta pending",
            "metadata_saved": "Metadata saved",
            "awaiting_secure_auth": "Awaiting secure auth",
        },
    },
    "oanda_api": {
        "label": "OANDA API",
        "category": "public_api",
        "auth_mode": "api_keys_beta_shell",
        "status": "coming_soon",
        "supports_live_sync": False,
        "requires_bridge": False,
        "beta": True,
        "integration_status": "beta_pending_secure_auth",
        "connection_layer": "public_api_connector",
        "supported_capabilities": ["beta_metadata_registration"],
        "onboarding_copy": "Register metadata now; secure credential support is coming soon.",
        "connection_state_labels": {
            "beta_pending": "Beta pending",
            "metadata_saved": "Metadata saved",
            "awaiting_secure_auth": "Awaiting secure auth",
        },
    },
    "binance_api": {
        "label": "Binance API",
        "category": "public_api",
        "auth_mode": "api_keys_beta_shell",
        "status": "coming_soon",
        "supports_live_sync": False,
        "requires_bridge": False,
        "beta": True,
        "integration_status": "beta_pending_secure_auth",
        "connection_layer": "public_api_connector",
        "supported_capabilities": ["beta_metadata_registration"],
        "onboarding_copy": "Register metadata now; secure credential support is coming soon.",
        "connection_state_labels": {
            "beta_pending": "Beta pending",
            "metadata_saved": "Metadata saved",
            "awaiting_secure_auth": "Awaiting secure auth",
        },
    },
    "csv_import": {
        "label": "CSV Import",
        "category": "file_import",
        "auth_mode": "manual_import",
        "status": "live",
        "supports_live_sync": False,
        "requires_bridge": False,
        "beta": False,
        "integration_status": "live",
        "connection_layer": "import_tool",
        "supported_capabilities": ["manual_import"],
        "onboarding_copy": "Import historical rows from CSV/JSON.",
        "connection_state_labels": {
            "ready": "Ready",
        },
    },
    "manual": {
        "label": "Manual Journal",
        "category": "manual",
        "auth_mode": "manual_entry",
        "status": "live",
        "supports_live_sync": False,
        "requires_bridge": False,
        "beta": False,
        "integration_status": "live",
        "connection_layer": "manual_entry",
        "supported_capabilities": ["manual_account_journal"],
        "onboarding_copy": "Create and journal trades manually.",
        "connection_state_labels": {
            "ready": "Ready",
        },
    },
}

CONNECTOR_CONFIG_SPEC: dict[str, dict[str, Any]] = {
    "fundingpips_extension": {
        "non_secret_fields": ["healthcheck_url", "external_account_id", "timeout_seconds"],
        "secret_fields": ["api_token"],
        "supports_external_sync": True,
    },
    "mt5_bridge": {
        "non_secret_fields": ["bridge_url", "external_account_id", "mt5_server", "bridge_timeout_seconds"],
        "secret_fields": ["bridge_api_key"],
        "supports_external_sync": True,
    },
}


def validate_mt5_bridge_connector_config(non_secret_config: dict[str, Any], secret_config: dict[str, Any]) -> tuple[str, str | None]:
    bridge_url = str(non_secret_config.get("bridge_url") or "").strip()
    account_id = str(non_secret_config.get("external_account_id") or "").strip()
    server = str(non_secret_config.get("mt5_server") or "").strip()
    bridge_api_key = str(secret_config.get("bridge_api_key") or "").strip()
    if not bridge_url:
        return ("incomplete", "bridge_url is required for MT5 bridge connectivity")
    if not account_id:
        return ("incomplete", "external_account_id is required for MT5 bridge connectivity")
    if not server:
        return ("incomplete", "mt5_server is required for MT5 bridge connectivity")
    if not bridge_api_key:
        return ("incomplete", "bridge_api_key is required for MT5 bridge connectivity")
    return ("configured", None)


CONNECTOR_CONFIG_VALIDATORS: dict[str, ConnectorConfigValidator] = {
    "fundingpips_extension": validate_fundingpips_connector_config,
    "mt5_bridge": validate_mt5_bridge_connector_config,
}


def normalize_connector_type(value: str | None) -> str:
    return (value or "manual").strip().lower().replace("-", "_")


def connector_supports_live_sync(connector_type: str) -> bool:
    normalized = normalize_connector_type(connector_type)
    return bool(CONNECTOR_CATALOG.get(normalized, {}).get("supports_live_sync", False))


def connector_config_spec(connector_type: str) -> dict[str, Any]:
    normalized = normalize_connector_type(connector_type)
    return CONNECTOR_CONFIG_SPEC.get(normalized, {"non_secret_fields": [], "secret_fields": [], "supports_external_sync": False})


def connector_validation_for(connector_type: str) -> ConnectorConfigValidator | None:
    normalized = normalize_connector_type(connector_type)
    return CONNECTOR_CONFIG_VALIDATORS.get(normalized)
