function toTitle(text) {
  return String(text || '')
    .replace(/_/g, ' ')
    .trim()
    .replace(/\b\w/g, (char) => char.toUpperCase())
}

export function connectionStatusMeta(status) {
  const normalized = String(status || 'disconnected').toLowerCase()
  if (normalized === 'connected') {
    return {
      toneClass: 'status-connected',
      label: 'Connected',
      helper: 'Connectivity health is derived from connector-level status.',
    }
  }
  if (normalized === 'degraded' || normalized === 'sync_queued' || normalized === 'sync_running' || normalized === 'sync_retrying') {
    return {
      toneClass: 'status-degraded',
      label: normalized.startsWith('sync_') ? toTitle(normalized) : 'Degraded',
      helper: 'This reflects connector rollup health, not per-account transport checks.',
    }
  }
  if (normalized === 'sync_error') {
    return {
      toneClass: 'status-error',
      label: 'Sync Error',
      helper: 'A recent connector sync failed; impact is shown at connector rollup level.',
    }
  }
  if (normalized === 'awaiting_alerts') {
    return {
      toneClass: 'status-degraded',
      label: 'Awaiting First Alert',
      helper: 'TradingView webhook is created and waiting for the first valid alert event.',
    }
  }
  if (normalized === 'active') {
    return {
      toneClass: 'status-connected',
      label: 'Webhook Active',
      helper: 'TradingView webhook has received a valid alert event.',
    }
  }
  if (normalized === 'bridge_required' || normalized === 'waiting_for_registration' || normalized === 'ready_for_account_attach') {
    return {
      toneClass: normalized === 'ready_for_account_attach' ? 'status-connected' : 'status-degraded',
      label: toTitle(normalized),
      helper: 'MT5 bridge onboarding state is shown honestly before full connectivity.',
    }
  }
  if (normalized === 'beta_pending' || normalized === 'metadata_saved' || normalized === 'awaiting_secure_auth' || normalized === 'waiting_for_secure_auth_support') {
    return {
      toneClass: 'status-degraded',
      label: toTitle(normalized),
      helper: 'Public API connector is in beta metadata mode only.',
    }
  }
  if (normalized === 'paper_connected' || normalized === 'live_connected') {
    return {
      toneClass: 'status-connected',
      label: toTitle(normalized),
      helper: 'Alpaca credentials were validated server-side and account access is read-only.',
    }
  }
  if (normalized === 'validation_failed') {
    return {
      toneClass: 'status-error',
      label: 'Validation Failed',
      helper: 'Credential validation failed; no live connection is claimed.',
    }
  }
  return {
    toneClass: 'status-disconnected',
    label: 'Disconnected',
    helper: 'No active connector session is available for this source.',
  }
}

export function syncStateMeta(state) {
  const normalized = String(state || 'idle').toLowerCase()
  if (normalized === 'succeeded') return { toneClass: 'status-connected', label: 'Succeeded' }
  if (normalized === 'failed') return { toneClass: 'status-error', label: 'Failed' }
  if (normalized === 'queued' || normalized === 'running' || normalized === 'retrying') {
    return { toneClass: 'status-degraded', label: toTitle(normalized) }
  }
  return { toneClass: 'status-disconnected', label: toTitle(normalized) || 'Idle' }
}
