from __future__ import annotations

from typing import Any, Callable

from app.services.connector_ingest import validate_fundingpips_connector_config

ConnectorConfigValidator = Callable[[dict[str, Any], dict[str, Any]], tuple[str, str | None]]

CONNECTOR_CATALOG: dict[str, dict[str, Any]] = {
    "fundingpips_extension": {
        "label": "FundingPips Extension",
        "category": "extension",
        "supports_live_sync": True,
        "integration_status": "live",
        "connection_layer": "broker_connector",
    },
    "mt5_bridge": {
        "label": "MetaTrader 5 (MT5)",
        "category": "broker_platform",
        "supports_live_sync": False,
        "integration_status": "beta_bridge_required",
        "connection_layer": "broker_connector",
        "notes": "Bridge/worker process required for live MT5 account sync.",
    },
    "csv_import": {
        "label": "CSV Import",
        "category": "file_import",
        "supports_live_sync": False,
        "integration_status": "live",
        "connection_layer": "import_tool",
    },
    "manual": {
        "label": "Manual Journal",
        "category": "manual",
        "supports_live_sync": False,
        "integration_status": "live",
        "connection_layer": "manual_entry",
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
