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
