function toLabel(value) {
  return String(value || '')
    .replace(/_/g, ' ')
    .trim()
    .replace(/\b\w/g, (char) => char.toUpperCase())
}

export function deriveConnectorLifecycleState(connector = {}) {
  const status = String(connector?.status || 'disconnected').toLowerCase()
  const providerState = String(connector?.provider_state || '').toLowerCase()
  const accountStates = (Array.isArray(connector?.accounts) ? connector.accounts : [])
    .map((account) => String(account?.connection_status || '').toLowerCase())

  if (status === 'validation_failed' || providerState === 'validation_failed' || accountStates.includes('validation_failed')) {
    return {
      key: 'validation_failed',
      toneClass: 'status-error',
      label: 'Validation Failed',
      helper: 'Credential validation failed; no live connection is implied.',
    }
  }

  if (
    status === 'connected'
    || status === 'active'
    || status === 'account_verified'
    || status === 'paper_connected'
    || status === 'live_connected'
    || providerState === 'account_verified'
    || providerState === 'paper_connected'
    || providerState === 'live_connected'
    || accountStates.includes('account_verified')
    || accountStates.includes('paper_connected')
    || accountStates.includes('live_connected')
  ) {
    return {
      key: 'verified_connected',
      toneClass: 'status-connected',
      label: 'Verified / Connected',
      helper: 'Credentials are verified and workspace account data is connected.',
    }
  }

  if (
    status === 'beta_pending'
    || status === 'metadata_saved'
    || status === 'awaiting_secure_auth'
    || status === 'waiting_for_secure_auth_support'
    || providerState === 'beta_pending'
    || providerState === 'metadata_saved'
    || providerState === 'awaiting_secure_auth'
    || providerState === 'waiting_for_secure_auth_support'
  ) {
    return {
      key: 'pending_beta_metadata',
      toneClass: 'status-degraded',
      label: 'Pending / Metadata only',
      helper: 'Connector is present in beta metadata mode and not yet fully connected.',
    }
  }

  return {
    key: 'disconnected',
    toneClass: 'status-disconnected',
    label: 'Disconnected',
    helper: 'No active validated connection is available for this connector.',
  }
}

export function connectorEnvironmentLabel(connector = {}) {
  const envs = new Set(
    (Array.isArray(connector?.accounts) ? connector.accounts : [])
      .map((account) => String(account?.environment || '').toLowerCase().trim())
      .filter(Boolean),
  )
  if (envs.size === 0) return '—'
  return Array.from(envs).map((env) => toLabel(env)).join(', ')
}
