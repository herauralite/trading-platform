export const SESSION_STORAGE_KEY = 'talitrade.sessionToken'
export const USER_STORAGE_KEY = 'talitrade.sessionUser'
export const DEV_MODE_KEY = 'talitrade.devMode'

export function buildAuthHeaders(sessionToken) {
  if (!sessionToken) return {}
  return { Authorization: `Bearer ${sessionToken}` }
}

export function parseStoredUser(rawUser) {
  if (!rawUser) return null
  try {
    return JSON.parse(rawUser)
  } catch {
    return null
  }
}

export function parseOidcCallbackPayload(hash, storedNonce = null) {
  if (!hash || !hash.startsWith('#')) return null
  const params = new URLSearchParams(hash.slice(1))
  const idToken = params.get('id_token')
  if (!idToken) return null
  return {
    idToken,
    nonce: params.get('nonce') || storedNonce || null,
  }
}

export function shouldShowBridgeFallback({ isDevEnv = false, devModeValue = null }) {
  return Boolean(isDevEnv || devModeValue === '1')
}

export function buildManualAccountPayload(form) {
  return {
    connector_type: 'manual',
    broker_name: form.brokerName,
    external_account_id: form.externalAccountId,
    display_label: form.displayLabel || `Manual ${form.externalAccountId}`,
    account_type: form.accountType,
    account_size: Number(form.accountSize) || null,
  }
}

export function buildManualTradePayload(form) {
  return {
    connector_type: 'manual',
    external_account_id: form.externalAccountId,
    symbol: form.symbol,
    side: form.side,
    size: Number(form.size),
    entry_price: Number(form.entryPrice),
    exit_price: Number(form.exitPrice),
    pnl: Number(form.pnl),
    import_provenance: { entry_mode: 'ui_manual' },
    source_metadata: { created_from: 'manual_journal_panel' },
  }
}

export function buildCsvImportPayload(csvAccount, rows) {
  return {
    connector_type: 'csv_import',
    broker_name: 'csv',
    external_account_id: csvAccount,
    rows,
  }
}

export function canHydrateSession(meResponse) {
  return Boolean(meResponse?.user?.telegram_user_id)
}
