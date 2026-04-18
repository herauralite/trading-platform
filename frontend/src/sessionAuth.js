export const SESSION_STORAGE_KEY = 'talitrade.sessionToken'
export const USER_STORAGE_KEY = 'talitrade.sessionUser'
export const OIDC_NONCE_KEY = 'talitrade.oidc.nonce'
export const OIDC_STATE_KEY = 'talitrade.oidc.state'

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

export function persistOidcCorrelation(storage, { nonce, state }) {
  storage.setItem(OIDC_NONCE_KEY, nonce)
  storage.setItem(OIDC_STATE_KEY, state)
}

export function clearOidcCorrelation(storage) {
  storage.removeItem(OIDC_NONCE_KEY)
  storage.removeItem(OIDC_STATE_KEY)
}

export function parseOidcCallbackPayload(hash, { expectedState = null, storedNonce = null } = {}) {
  if (!hash || !hash.startsWith('#')) return null
  const params = new URLSearchParams(hash.slice(1))
  const idToken = params.get('id_token')
  if (!idToken) return null

  const state = params.get('state')
  if (!expectedState || !state) {
    return { ok: false, error: 'Missing OIDC state correlation.' }
  }
  if (state !== expectedState) {
    return { ok: false, error: 'OIDC state mismatch.' }
  }

  return {
    ok: true,
    idToken,
    state,
    nonce: params.get('nonce') || storedNonce || null,
  }
}



export function normalizeTelegramAuthUser(user) {
  if (!user || typeof user !== 'object') return null
  const telegramUserId = String(user.telegram_user_id || user.telegramUserId || user.id || '').trim()
  if (!telegramUserId) return null
  return {
    ...user,
    telegram_user_id: telegramUserId,
    telegramUserId,
    telegram_username: user.telegram_username || user.username || '',
    username: user.username || user.telegram_username || '',
    first_name: user.first_name || user.firstName || '',
    firstName: user.firstName || user.first_name || '',
    last_name: user.last_name || user.lastName || '',
    lastName: user.lastName || user.last_name || '',
    photo_url: user.photo_url || user.photoUrl || '',
    photoUrl: user.photoUrl || user.photo_url || '',
  }
}

export async function resolveTelegramAuthUser({ accessToken, responseUser = null, widgetUser = null, fetchMeUser = null } = {}) {
  if (!String(accessToken || '').trim()) {
    throw new Error('missing_session_token')
  }

  let user = normalizeTelegramAuthUser(responseUser) || normalizeTelegramAuthUser(widgetUser)
  if (user) return user

  if (typeof fetchMeUser === 'function') {
    const meUser = await fetchMeUser()
    user = normalizeTelegramAuthUser(meUser)
    if (user) return user
  }

  throw new Error('missing_or_invalid_user')
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
