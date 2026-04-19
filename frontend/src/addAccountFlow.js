const REQUIRED_CONNECTORS = [
  'mt5_bridge',
  'tradingview_webhook',
  'alpaca_api',
  'tradelocker_api',
  'oanda_api',
  'binance_api',
  'fundingpips_extension',
  'csv_import',
  'manual',
]

export const PUBLIC_API_CONNECTORS = ['alpaca_api', 'tradelocker_api']
export const PUBLIC_API_BETA_CONNECTORS = ['oanda_api', 'binance_api']
export const GUIDED_ADD_ACCOUNT_CONNECTORS = ['tradingview_webhook', ...PUBLIC_API_CONNECTORS, ...PUBLIC_API_BETA_CONNECTORS]

export function isGuidedAddAccountConnector(connectorType) {
  return GUIDED_ADD_ACCOUNT_CONNECTORS.includes(connectorType)
}

const PROVIDER_DEFAULTS = {
  mt5_bridge: {
    title: 'MetaTrader 5 (MT5)',
    shortLabel: 'MT5 Bridge',
    flowType: 'pairing',
    badge: 'Bridge Flow',
    description: 'Connect your MT5 account through a trusted bridge pairing flow.',
    ctaLabel: 'Connect MT5',
  },
  tradingview_webhook: {
    title: 'TradingView Webhook',
    shortLabel: 'TradingView',
    flowType: 'tradingview_webhook',
    badge: 'Signals · Beta',
    description: 'Create a secure webhook endpoint for TradingView alerts.',
    ctaLabel: 'Create TradingView webhook',
  },
  alpaca_api: {
    title: 'Alpaca API',
    shortLabel: 'Alpaca',
    flowType: 'alpaca_connect',
    badge: 'Beta',
    description: 'Connect read-only Alpaca API credentials to validate account access.',
    ctaLabel: 'Connect Alpaca',
  },
  tradelocker_api: {
    title: 'TradeLocker API',
    shortLabel: 'TradeLocker',
    flowType: 'tradelocker_connect',
    badge: 'Public API',
    description: 'Connect real TradeLocker credentials and validate access server-side.',
    ctaLabel: 'Connect TradeLocker',
  },
  oanda_api: {
    title: 'OANDA API',
    shortLabel: 'OANDA',
    flowType: 'public_api_beta',
    badge: 'Beta',
    description: 'Register for beta API onboarding with metadata only.',
    ctaLabel: 'Join beta access',
  },
  binance_api: {
    title: 'Binance API',
    shortLabel: 'Binance',
    flowType: 'public_api_beta',
    badge: 'Beta',
    description: 'Register for beta API onboarding with metadata only.',
    ctaLabel: 'Join beta access',
  },
  fundingpips_extension: {
    title: 'FundingPips Extension',
    shortLabel: 'FundingPips',
    flowType: 'connector_connect',
    badge: 'Browser Extension',
    description: 'Connect using the existing FundingPips connector flow.',
    ctaLabel: 'Connect FundingPips',
  },
  csv_import: {
    title: 'CSV Import',
    shortLabel: 'CSV',
    flowType: 'route_to_connections_csv',
    badge: 'File Import',
    description: 'Import historical trades from CSV/JSON rows through the import tools.',
    ctaLabel: 'Go to CSV import',
  },
  manual: {
    title: 'Manual Journal',
    shortLabel: 'Manual',
    flowType: 'route_to_connections_manual',
    badge: 'Manual Entry',
    description: 'Create and journal trades manually in the authenticated workspace.',
    ctaLabel: 'Go to manual journal',
  },
}

export function buildAddAccountProviders(catalog = [], sourceLabel) {
  const catalogByType = new Map((catalog || []).map((entry) => [entry.connector_type, entry]))

  return REQUIRED_CONNECTORS.map((connectorType) => {
    const catalogEntry = catalogByType.get(connectorType) || {}
    const defaults = PROVIDER_DEFAULTS[connectorType]

    return {
      connectorType,
      title: defaults.title,
      shortLabel: defaults.shortLabel,
      flowType: defaults.flowType,
      badge: catalogEntry.beta ? `${defaults.badge}` : defaults.badge,
      description: catalogEntry.onboarding_copy || defaults.description,
      ctaLabel: defaults.ctaLabel,
      sourceLabel: sourceLabel(connectorType),
      integrationStatus: catalogEntry.integration_status || 'unknown',
      notes: catalogEntry.notes || '',
      supportsLiveSync: Boolean(catalogEntry.supports_live_sync),
      connectionLayer: catalogEntry.connection_layer || null,
      isFromCatalog: Boolean(catalogByType.get(connectorType)),
      stateLabels: catalogEntry.connection_state_labels || {},
      status: catalogEntry.status || 'unknown',
      beta: Boolean(catalogEntry.beta),
      authMode: catalogEntry.auth_mode || null,
      requiresBridge: Boolean(catalogEntry.requires_bridge),
    }
  })
}
