const REQUIRED_CONNECTORS = [
  'mt5_bridge',
  'fundingpips_extension',
  'csv_import',
  'manual',
]

const PROVIDER_DEFAULTS = {
  mt5_bridge: {
    title: 'MetaTrader 5 (MT5)',
    shortLabel: 'MT5 Bridge',
    flowType: 'pairing',
    badge: 'Bridge Required · Beta',
    description: 'Pair your MT5 account through a bridge connection. This is a pairing flow, not OAuth.',
    ctaLabel: 'Pair MT5 account',
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
      badge: defaults.badge,
      description: defaults.description,
      ctaLabel: defaults.ctaLabel,
      sourceLabel: sourceLabel(connectorType),
      integrationStatus: catalogEntry.integration_status || 'unknown',
      notes: catalogEntry.notes || '',
      supportsLiveSync: Boolean(catalogEntry.supports_live_sync),
      connectionLayer: catalogEntry.connection_layer || null,
      isFromCatalog: Boolean(catalogByType.get(connectorType)),
    }
  })
}
