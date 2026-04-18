import axios from 'axios'
import { buildApiUrl } from './apiBase.js'
import { buildAuthHeaders } from './sessionAuth.js'

const DEFAULT_CONNECTION_STATUS = 'disconnected'
const DEFAULT_SYNC_STATE = 'idle'

function normalizeConnectorType(value) {
  return String(value || 'manual').trim().toLowerCase().replace(/-/g, '_')
}

function sourceLabel(connectorType) {
  if (connectorType === 'fundingpips_extension') return 'FundingPips Connector'
  if (connectorType === 'mt5_bridge') return 'MetaTrader 5 (MT5)'
  if (connectorType === 'tradingview_webhook') return 'TradingView Webhook'
  if (connectorType === 'alpaca_api') return 'Alpaca API (Beta)'
  if (connectorType === 'oanda_api') return 'OANDA API (Beta)'
  if (connectorType === 'binance_api') return 'Binance API (Beta)'
  if (connectorType === 'csv_import') return 'CSV Import'
  if (connectorType === 'manual') return 'Manual Journal'
  return connectorType
}

function normalizeWorkspace(workspace) {
  const connectorType = normalizeConnectorType(workspace?.connector_type)
  const accountKey = String(workspace?.account_key || '').trim()
  return {
    account_key: accountKey,
    trading_account_id: workspace?.trading_account_id ?? null,
    user_id: workspace?.user_id ?? null,
    external_account_id: workspace?.external_account_id ?? null,
    display_label: workspace?.display_label || workspace?.external_account_id || accountKey || 'Unnamed account',
    broker_name: workspace?.broker_name ?? null,
    broker_family: workspace?.broker_family || connectorType || 'unknown',
    connector_type: connectorType,
    connection_status: String(workspace?.connection_status || DEFAULT_CONNECTION_STATUS).toLowerCase(),
    sync_state: String(workspace?.sync_state || DEFAULT_SYNC_STATE).toLowerCase(),
    account_type: workspace?.account_type ?? null,
    account_size: workspace?.account_size ?? null,
    last_activity_at: workspace?.last_activity_at ?? null,
    last_sync_at: workspace?.last_sync_at ?? null,
    is_primary: Boolean(workspace?.is_primary),
    source_label: sourceLabel(connectorType),
    provider_state: workspace?.provider_state || null,
    tradingview_activation_state: workspace?.tradingview_activation_state || null,
    tradingview_last_event_at: workspace?.tradingview_last_event_at || null,
    recent_events: Array.isArray(workspace?.recent_events) ? workspace.recent_events : [],
  }
}

export async function fetchAccountWorkspaces(token) {
  const res = await axios.get(buildApiUrl('/accounts/workspaces'), { headers: buildAuthHeaders(token) })
  const workspaces = Array.isArray(res?.data?.workspaces) ? res.data.workspaces : []
  return workspaces
    .map(normalizeWorkspace)
    .sort((a, b) => {
      if (a.is_primary && !b.is_primary) return -1
      if (!a.is_primary && b.is_primary) return 1
      return String(a.display_label || '').localeCompare(String(b.display_label || ''))
    })
}
