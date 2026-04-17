export function buildConnectorConfigDraft(existing = {}) {
  const nonSecret = existing.non_secret_config || {}
  return {
    healthcheck_url: nonSecret.healthcheck_url || '',
    external_account_id: nonSecret.external_account_id || '',
    timeout_seconds: nonSecret.timeout_seconds || 8,
    api_token: '',
    hasSecret: Boolean(existing.has_secret_config),
  }
}

export function connectorConfigStateLabel(connector) {
  if (!connector?.supports_live_sync) return 'not_required'
  if (!connector?.has_config) return 'missing'
  if (connector?.config_status === 'configured') return 'ready'
  if (connector?.config_status === 'incomplete' || connector?.config_status === 'invalid') return 'incomplete'
  return 'unknown'
}
