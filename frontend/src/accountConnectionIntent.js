export function resolveAccountProviderKey(account) {
  return String(account?.connector_type || '').trim().toLowerCase()
}

export function buildConnectionsIntentPath(account, intent = 'manage') {
  const params = new URLSearchParams()
  const provider = resolveAccountProviderKey(account)
  if (provider) params.set('provider', provider)
  const accountKey = String(account?.account_key || '').trim()
  if (accountKey) params.set('account', accountKey)
  if (intent) params.set('intent', intent)
  const query = params.toString()
  return query ? `/app/connections?${query}` : '/app/connections'
}

export function deriveConnectionsIntentFromState(accountState) {
  if (accountState === 'pending') return 'setup'
  if (accountState === 'stale') return 'reconnect'
  return 'manage'
}
