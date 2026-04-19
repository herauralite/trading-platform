import { isCurrentlyConnectedAccount, isPendingOnlyAccount } from './accountConnectionState.js'

function accountStateRank(account) {
  if (isCurrentlyConnectedAccount(account)) return 0
  if (isPendingOnlyAccount(account)) return 1
  return 2
}

function sortBySelectionPriority(accounts = []) {
  return [...accounts].sort((a, b) => {
    const rankDiff = accountStateRank(a) - accountStateRank(b)
    if (rankDiff !== 0) return rankDiff
    if (a.is_primary && !b.is_primary) return -1
    if (!a.is_primary && b.is_primary) return 1
    const aActivityTs = new Date(a.last_activity_at || a.last_sync_at || 0).getTime()
    const bActivityTs = new Date(b.last_activity_at || b.last_sync_at || 0).getTime()
    if (aActivityTs !== bActivityTs) return bActivityTs - aActivityTs
    return String(a.display_label || a.external_account_id || a.account_key || '').localeCompare(
      String(b.display_label || b.external_account_id || b.account_key || ''),
    )
  })
}

export function classifyWorkspaceAccountState(account) {
  if (isCurrentlyConnectedAccount(account)) return 'usable'
  if (isPendingOnlyAccount(account)) return 'pending'
  return 'stale'
}

export function resolvePreferredDetailAccountKey(accounts = [], { currentDetailAccountKey = '', selectedActiveAccountKey = '' } = {}) {
  if (!accounts.length) return ''
  const existingByKey = new Map(accounts.map((account) => [account.account_key, account]))
  if (currentDetailAccountKey && existingByKey.has(currentDetailAccountKey)) return currentDetailAccountKey
  if (selectedActiveAccountKey && existingByKey.has(selectedActiveAccountKey)) return selectedActiveAccountKey
  const sorted = sortBySelectionPriority(accounts)
  return sorted[0]?.account_key || ''
}
