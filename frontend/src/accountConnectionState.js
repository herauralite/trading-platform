const USABLE_CONNECTION_STATUSES = new Set([
  'connected',
  'active',
  'paper_connected',
  'live_connected',
  'account_verified',
  'degraded',
  'sync_error',
])

const PENDING_CONNECTION_STATUSES = new Set([
  'awaiting_alerts',
  'bridge_required',
  'waiting_for_registration',
  'ready_for_account_attach',
  'beta_pending',
  'metadata_saved',
  'awaiting_secure_auth',
  'waiting_for_secure_auth_support',
])

const PENDING_SYNC_STATES = new Set(['queued', 'running', 'retrying'])
const INACTIVE_CONNECTION_STATUSES = new Set(['disconnected', 'validation_failed', 'failed', 'archived', 'placeholder'])

function normalizeStatus(value, fallback = '') {
  return String(value || fallback).trim().toLowerCase()
}

export function isCurrentlyConnectedAccount(account) {
  const status = normalizeStatus(account?.connection_status, 'disconnected')
  if (USABLE_CONNECTION_STATUSES.has(status)) return true
  return false
}

export function isPendingOnlyAccount(account) {
  const connectionStatus = normalizeStatus(account?.connection_status, 'disconnected')
  const syncState = normalizeStatus(account?.sync_state, 'idle')
  if (isCurrentlyConnectedAccount(account)) return false
  return PENDING_CONNECTION_STATUSES.has(connectionStatus) || PENDING_SYNC_STATES.has(syncState)
}

export function deriveAccountConnectionState(accounts = []) {
  const summary = {
    totalCount: accounts.length,
    connectedUsableCount: 0,
    pendingOnlyCount: 0,
    staleInactiveCount: 0,
  }

  for (const account of accounts) {
    if (isCurrentlyConnectedAccount(account)) {
      summary.connectedUsableCount += 1
      continue
    }
    if (isPendingOnlyAccount(account)) {
      summary.pendingOnlyCount += 1
      continue
    }

    const status = normalizeStatus(account?.connection_status, 'disconnected')
    if (INACTIVE_CONNECTION_STATUSES.has(status) || !status) {
      summary.staleInactiveCount += 1
      continue
    }

    // Unknown non-usable status should not block onboarding.
    summary.staleInactiveCount += 1
  }

  return {
    ...summary,
    hasConnectedAccounts: summary.connectedUsableCount > 0,
    hasZeroConnectedAccounts: summary.connectedUsableCount === 0,
  }
}
