import { useEffect, useMemo, useRef, useState } from 'react'
import axios from 'axios'
import {
  buildAuthHeaders,
  buildCsvImportPayload,
  buildManualAccountPayload,
  buildManualTradePayload,
  canHydrateSession,
  clearOidcCorrelation,
  OIDC_NONCE_KEY,
  OIDC_STATE_KEY,
  parseOidcCallbackPayload,
  parseStoredUser,
  persistOidcCorrelation,
  SESSION_STORAGE_KEY,
  USER_STORAGE_KEY,
} from './sessionAuth'
import { formatSyncRunDiagnostics } from './syncRunDiagnostics'
import { buildConnectorConfigDraft, connectorConfigStateLabel } from './connectorConfig'
import { buildApiUrl, formatTelegramConfigDiagnostics, resolveApiBase } from './apiBase'

const DEFAULT_STATUS = 'Sign in with Telegram to load your connected trading sources.'
const CANONICAL_HOST = 'www.talitrade.com'

const normalizeHost = (value) => {
  const raw = String(value || '').trim().toLowerCase()
  if (!raw) return ''
  return raw.replace(/^[a-z][a-z0-9+.-]*:\/\//, '').split('/')[0].split('?')[0].split('#')[0].split(':')[0].replace(/\.+$/, '')
}

const normalizeSessionUser = (user) => {
  if (!user || typeof user !== 'object') return null
  // Keep both field shapes for compatibility across widget/OIDC/login callbacks and stored sessions.
  // Future auth changes must preserve BOTH keys: telegramUserId and telegram_user_id.
  const telegramUserId = String(user.telegram_user_id || user.telegramUserId || '').trim()
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

function App() {
  const [sessionToken, setSessionToken] = useState(localStorage.getItem(SESSION_STORAGE_KEY) || '')
  const [sessionUser, setSessionUser] = useState(normalizeSessionUser(parseStoredUser(localStorage.getItem(USER_STORAGE_KEY))))
  const [telegramConfig, setTelegramConfig] = useState(null)
  const [catalog, setCatalog] = useState([])
  const [connectors, setConnectors] = useState([])
  const [status, setStatus] = useState(DEFAULT_STATUS)
  const [widgetStatus, setWidgetStatus] = useState('')
  const [widgetScriptLoaded, setWidgetScriptLoaded] = useState(false)
  const [isBootstrapping, setIsBootstrapping] = useState(true)
  const widgetWrapRef = useRef(null)
  const [manualAccount, setManualAccount] = useState({ externalAccountId: '', brokerName: 'Manual', displayLabel: '', accountType: 'demo', accountSize: 10000 })
  const [manualTrade, setManualTrade] = useState({ externalAccountId: '', symbol: 'NAS100', side: 'buy', size: 0.1, entryPrice: 15000, exitPrice: 15025, pnl: 25 })
  const [csvInput, setCsvInput] = useState('[{"symbol":"US30","side":"buy","open_time":"2026-04-16T10:00:00Z","close_time":"2026-04-16T10:10:00Z","pnl":18}]')
  const [csvAccount, setCsvAccount] = useState('csv-account-1')
  const [connectorDrafts, setConnectorDrafts] = useState({})
  const [configDrafts, setConfigDrafts] = useState({})
  const [syncHistory, setSyncHistory] = useState({})

  const signedIn = Boolean(sessionToken && sessionUser?.telegram_user_id)
  const authHeaders = buildAuthHeaders(sessionToken)
  const currentHost = normalizeHost(window.location.hostname)
  const canonicalHost = normalizeHost(telegramConfig?.canonicalLoginDomain || telegramConfig?.loginDomain || CANONICAL_HOST)
  const isCanonicalHost = Boolean(currentHost && canonicalHost && currentHost === canonicalHost)

  const allAccounts = useMemo(
    () => connectors.flatMap((connector) => connector.accounts.map((account) => ({ ...account, connector_type: connector.connector_type }))),
    [connectors]
  )
  const managedConnectors = useMemo(() => {
    const map = new Map(connectors.map((connector) => [connector.connector_type, connector]))
    for (const entry of catalog) {
      if (!map.has(entry.connector_type)) {
        map.set(entry.connector_type, {
          connector_type: entry.connector_type,
          status: 'disconnected',
          is_connected: false,
          account_count: 0,
          accounts: [],
          last_activity_at: null,
          last_sync_at: null,
          last_error: null,
          last_error_at: null,
          current_sync_state: null,
          current_sync_run_id: null,
          current_sync_retry_count: 0,
          next_retry_at: null,
          supports_live_sync: Boolean(entry.supports_live_sync),
        })
      } else {
        map.set(entry.connector_type, {
          ...map.get(entry.connector_type),
          supports_live_sync: Boolean(entry.supports_live_sync),
        })
      }
    }
    return Array.from(map.values())
  }, [catalog, connectors])

  useEffect(() => {
    void loadTelegramAuthConfig()
    const oidc = parseOidcCallbackPayload(window.location.hash, {
      expectedState: localStorage.getItem(OIDC_STATE_KEY),
      storedNonce: localStorage.getItem(OIDC_NONCE_KEY),
    })
    if (oidc) {
      window.history.replaceState({}, document.title, window.location.pathname)
      if (!oidc.ok) {
        clearOidcCorrelation(localStorage)
        clearSession()
        setStatus(`Telegram OIDC callback rejected: ${oidc.error}`)
        setIsBootstrapping(false)
        return
      }
      void signInWithOidcToken(oidc.idToken, oidc.nonce)
      return
    }
    if (sessionToken) {
      void bootstrapSession(sessionToken)
      return
    }
    setIsBootstrapping(false)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    window.onTelegramAuth = (user) => {
      void signInWithTelegramWidget(user)
    }
    return () => {
      delete window.onTelegramAuth
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const sourceLabel = (connectorType) => {
    if (connectorType === 'fundingpips_extension') return 'FundingPips Connector'
    if (connectorType === 'csv_import') return 'CSV Import'
    if (connectorType === 'manual') return 'Manual Journal'
    return connectorType
  }

  const formatDate = (dateText) => (dateText ? new Date(dateText).toLocaleString() : '—')

  const statusTone = (status) => {
    if (status === 'connected') return 'status-connected'
    if (status === 'sync_running' || status === 'sync_queued' || status === 'sync_retrying') return 'status-degraded'
    if (status === 'degraded') return 'status-degraded'
    if (status === 'sync_error') return 'status-error'
    return 'status-disconnected'
  }

  const syncStateLabel = (state) => state || 'idle'

  async function loadTelegramAuthConfig() {
    const resolvedBase = resolveApiBase()
    const requestUrl = buildApiUrl('/auth/telegram/config')
    try {
      const res = await axios.get(requestUrl, { withCredentials: true })
      if (!res?.data || typeof res.data !== 'object') {
        throw new SyntaxError(`invalid_json url=${requestUrl}`)
      }
      const diagnostics = formatTelegramConfigDiagnostics({
        resolvedApiBase: resolvedBase,
        configUrl: requestUrl,
        configFetchStatus: res.status,
        configFetchContentType: res?.headers?.['content-type'] || 'missing',
      })
      setTelegramConfig(res.data || null)
      setWidgetStatus(['Telegram config loaded.', ...diagnostics].join(' '))
    } catch (error) {
      setTelegramConfig(null)
      const status = error?.response?.status
      const reason = status === 404
        ? 'http_404'
        : status === 500
          ? 'http_500'
        : status
          ? `http_${status}`
          : error instanceof SyntaxError
            ? 'invalid_json'
            : (typeof navigator !== 'undefined' && navigator.onLine === false)
              ? 'transport_offline'
              : 'cors_rejected_or_transport'
      const diagnostics = formatTelegramConfigDiagnostics({
        resolvedApiBase: resolvedBase,
        configUrl: requestUrl,
        configFetchStatus: status || 'request_failed',
        configFetchContentType: error?.response?.headers?.['content-type'] || 'missing',
        configFetchErrorName: error?.name || 'unknown',
        configFetchErrorMessage: String(error?.message || '').slice(0, 180) || 'n/a',
      })
      setWidgetStatus([
        'Could not load Telegram config. Login may fail.',
        `config_fetch_failed=${reason}`,
        ...diagnostics,
      ].join(' '))
    }
  }


  useEffect(() => {
    if (!widgetScriptLoaded || signedIn) return
    const timer = window.setTimeout(() => {
      const rendered = Boolean(widgetWrapRef.current?.querySelector('iframe, .telegram-login, div[id^="telegram-login"]'))
      if (!rendered) {
        setWidgetStatus('Telegram script loaded but widget did not render. Open www.talitrade.com to continue with Telegram sign-in, then disable blockers if needed.')
      }
    }, 3000)
    return () => window.clearTimeout(timer)
  }, [widgetScriptLoaded, signedIn])

  async function bootstrapSession(token) {
    setIsBootstrapping(true)
    try {
      const meRes = await axios.get(buildApiUrl('/auth/me'), { headers: buildAuthHeaders(token) })
      const user = normalizeSessionUser(meRes.data?.user)
      if (!canHydrateSession(meRes.data)) throw new Error('No authenticated user found in session')
      commitSession(token, user)
      setStatus(`Signed in as @${user.telegram_username || user.telegram_user_id}`)
      await loadConnectorData({ token, silent: true })
    } catch (e) {
      clearSession()
      setStatus(`Session expired or invalid. Please sign in again. (${e.message})`)
    } finally {
      setIsBootstrapping(false)
    }
  }

  function commitSession(token, user) {
    // Guardrail: auth success must normalize Telegram user ids into both camelCase and snake_case fields
    // so gate checks and persisted session hydration remain compatible.
    const normalizedUser = normalizeSessionUser(user)
    if (!normalizedUser) throw new Error('Invalid user payload for session commit')
    setSessionToken(token)
    setSessionUser(normalizedUser)
    localStorage.setItem(SESSION_STORAGE_KEY, token)
    localStorage.setItem(USER_STORAGE_KEY, JSON.stringify(normalizedUser))
  }

  function clearSession() {
    setSessionToken('')
    setSessionUser(null)
    setConnectors([])
    setCatalog([])
    localStorage.removeItem(SESSION_STORAGE_KEY)
    localStorage.removeItem(USER_STORAGE_KEY)
  }

  async function signInWithTelegramWidget(authData) {
    try {
      const res = await axios.post(buildApiUrl('/auth/telegram'), authData)
      const user = normalizeSessionUser(res.data?.user)
      const token = res.data?.access_token || ''
      if (!token || !user?.telegram_user_id) throw new Error('Telegram auth returned no session token')
      commitSession(token, user)
      setStatus(`Signed in as @${user.telegram_username || user.telegram_user_id}`)
      await loadConnectorData({ token, silent: true })
    } catch (e) {
      setStatus(`Telegram sign-in failed: ${e.message}`)
    }
  }

  async function signInWithOidcToken(idToken, nonce = null) {
    setIsBootstrapping(true)
    try {
      const res = await axios.post(buildApiUrl('/auth/telegram/oidc'), { id_token: idToken, nonce })
      const user = normalizeSessionUser(res.data?.user)
      const token = res.data?.access_token || ''
      if (!token || !user?.telegram_user_id) throw new Error('Telegram OIDC returned no session token')
      commitSession(token, user)
      setStatus(`Signed in as @${user.telegram_username || user.telegram_user_id}`)
      await loadConnectorData({ token, silent: true })
    } catch (e) {
      setStatus(`Telegram OIDC sign-in failed: ${e.message}`)
    } finally {
      clearOidcCorrelation(localStorage)
      setIsBootstrapping(false)
    }
  }

  async function loadConnectorData({ token = sessionToken, silent = false } = {}) {
    try {
      const [catalogRes, overviewRes] = await Promise.all([
        axios.get(buildApiUrl('/connectors/catalog')),
        axios.get(buildApiUrl('/connectors/overview'), { headers: buildAuthHeaders(token) })
      ])
      setCatalog(catalogRes.data.connectors || [])
      const overviewConnectors = overviewRes.data.connectors || []
      setConnectors(overviewConnectors)
      const configEntries = await Promise.all(
        overviewConnectors.map(async (connector) => {
          try {
            const configRes = await axios.get(
              buildApiUrl(`/connectors/${connector.connector_type}/config`),
              { headers: buildAuthHeaders(token) }
            )
            return [connector.connector_type, buildConnectorConfigDraft(configRes.data || {})]
          } catch {
            return [connector.connector_type, buildConnectorConfigDraft()]
          }
        })
      )
      setConfigDrafts((prev) => ({ ...prev, ...Object.fromEntries(configEntries) }))
      if (overviewConnectors.length > 0) {
        const historyEntries = await Promise.all(
          overviewConnectors.map(async (connector) => {
            try {
              const historyRes = await axios.get(
                buildApiUrl(`/connectors/${connector.connector_type}/sync-runs?limit=5`),
                { headers: buildAuthHeaders(token) }
              )
              return [connector.connector_type, historyRes.data?.runs || []]
            } catch {
              return [connector.connector_type, []]
            }
          })
        )
        setSyncHistory(Object.fromEntries(historyEntries))
      } else {
        setSyncHistory({})
      }
      if (!silent) setStatus(`Loaded ${overviewRes.data.count || 0} connected source(s) for your authenticated session`)
    } catch (e) {
      if (e?.response?.status === 401) {
        clearSession()
        setStatus('Your session expired. Please sign in with Telegram again.')
        return
      }
      setStatus(`Failed to load connectors: ${e.message}`)
    }
  }

  async function createManualAccount() {
    try {
      await axios.post(buildApiUrl('/ingest/accounts'), buildManualAccountPayload(manualAccount), { headers: authHeaders })
      setStatus('Manual account created for authenticated user')
      setManualTrade((prev) => ({ ...prev, externalAccountId: manualAccount.externalAccountId }))
      await loadConnectorData()
    } catch (e) {
      setStatus(`Manual account failed: ${e.message}`)
    }
  }

  async function createManualTrade() {
    try {
      await axios.post(buildApiUrl('/ingest/trades'), buildManualTradePayload(manualTrade), { headers: authHeaders })
      setStatus('Manual trade recorded for authenticated user')
      await loadConnectorData()
    } catch (e) {
      setStatus(`Manual trade failed: ${e.message}`)
    }
  }

  async function importCsvTrades() {
    try {
      const rows = JSON.parse(csvInput)
      await axios.post(buildApiUrl('/ingest/csv/trades'), buildCsvImportPayload(csvAccount, rows), { headers: authHeaders })
      setStatus(`Imported ${rows.length} CSV trade row(s) into authenticated workspace`)
      await loadConnectorData()
    } catch (e) {
      setStatus(`CSV import failed: ${e.message}`)
    }
  }

  async function connectorAction(connectorType, action, payload = {}) {
    try {
      await axios.post(buildApiUrl(`/connectors/${connectorType}/${action}`), payload, { headers: authHeaders })
      setStatus(`${sourceLabel(connectorType)} ${action} action completed`)
      await loadConnectorData({ silent: true })
    } catch (e) {
      setStatus(`${sourceLabel(connectorType)} ${action} failed: ${e.message}`)
    }
  }

  async function saveConnectorConfig(connectorType) {
    const draft = configDrafts[connectorType] || buildConnectorConfigDraft()
    try {
      await axios.put(buildApiUrl(`/connectors/${connectorType}/config`), {
        non_secret_config: {
          healthcheck_url: draft.healthcheck_url,
          external_account_id: draft.external_account_id,
          timeout_seconds: Number(draft.timeout_seconds || 8),
        },
        secret_config: draft.api_token ? { api_token: draft.api_token } : {},
      }, { headers: authHeaders })
      setStatus(`${sourceLabel(connectorType)} config saved`)
      setConfigDrafts((prev) => ({
        ...prev,
        [connectorType]: { ...prev[connectorType], api_token: '', hasSecret: true },
      }))
      await loadConnectorData({ silent: true })
    } catch (e) {
      setStatus(`${sourceLabel(connectorType)} config save failed: ${e.message}`)
    }
  }

  async function clearConnectorConfig(connectorType) {
    try {
      await axios.delete(buildApiUrl(`/connectors/${connectorType}/config`), { headers: authHeaders })
      setStatus(`${sourceLabel(connectorType)} config cleared`)
      setConfigDrafts((prev) => ({
        ...prev,
        [connectorType]: buildConnectorConfigDraft(),
      }))
      await loadConnectorData({ silent: true })
    } catch (e) {
      setStatus(`${sourceLabel(connectorType)} config clear failed: ${e.message}`)
    }
  }


  const startOidcFlow = () => {
    const cfg = telegramConfig
    if (!cfg?.oidcAuthorizeUrl || !cfg?.oidcClientId) {
      setStatus('Telegram OIDC is not configured by backend. Use widget mode or contact support.')
      return
    }
    const nonce = crypto.randomUUID()
    const state = crypto.randomUUID()
    persistOidcCorrelation(localStorage, { nonce, state })
    const redirectUri = `${window.location.origin}${window.location.pathname}`
    const params = new URLSearchParams({
      client_id: cfg.oidcClientId,
      response_type: 'id_token',
      scope: (cfg.oidcScopes || ['openid', 'profile']).join(' '),
      redirect_uri: redirectUri,
      nonce,
      state,
    })
    window.location.assign(`${cfg.oidcAuthorizeUrl}?${params.toString()}`)
  }

  return (
    <div className="app">
      <h1>TaliTrade Platform Console</h1>
      <p>Status: {status}</p>

      <section className="panel">
        <h2>Session</h2>
        {isBootstrapping ? <p>Restoring authenticated session…</p> : null}
        <div className="row">
          <span>Signed in:</span>
          <strong>{signedIn ? `@${sessionUser?.telegram_username || sessionUser?.telegram_user_id}` : 'No'}</strong>
          {signedIn ? <button onClick={clearSession}>Sign out</button> : null}
        </div>

        {!signedIn ? (
          <>
            <p>Primary login path: Telegram authenticated session.</p>
            {telegramConfig?.oidcEnabled ? (
              <button onClick={startOidcFlow}>Sign in with Telegram</button>
            ) : (
              <div ref={widgetWrapRef}>
                {!isCanonicalHost ? (
                  <p className="error-text">Open www.talitrade.com to continue with Telegram sign-in.</p>
                ) : (
                  <>
                    <script
                      async
                      src="https://telegram.org/js/telegram-widget.js?22"
                      data-telegram-login={telegramConfig?.botUsername || 'TaliTradeBot'}
                      data-size="large"
                      data-userpic="false"
                      data-request-access="write"
                      data-onauth="onTelegramAuth(user)"
                      onLoad={() => setWidgetScriptLoaded(true)}
                      onError={() => setWidgetStatus('Could not load Telegram widget script. Disable blockers and retry.')}
                    />
                    <p className="hint">Telegram widget mode enabled.</p>
                  </>
                )}
                {widgetStatus ? <p className="error-text">{widgetStatus}</p> : null}
              </div>
            )}
          </>
        ) : (
          <button onClick={() => loadConnectorData()}>Refresh connectors</button>
        )}
      </section>


      <section className="panel">
        <h2>Connector Management</h2>
        <p>Available connectors: {catalog.map((c) => c.label).join(', ') || '—'}</p>
        {managedConnectors.map((connector) => (
          <div key={connector.connector_type} className="card">
            <div className="row">
              <strong>{sourceLabel(connector.connector_type)}</strong>
              <span className={`badge ${statusTone(connector.status)}`}>{connector.status}</span>
            </div>
            <div className="meta">
              State: {connector.is_connected ? 'connected' : 'disconnected'} · Accounts: {connector.account_count} · Last activity: {formatDate(connector.last_activity_at)} · Last sync: {formatDate(connector.last_sync_at)}
            </div>
            <div className="meta">
              Sync state: {syncStateLabel(connector.current_sync_state)} · Retries: {connector.current_sync_retry_count || 0} · Next retry: {formatDate(connector.next_retry_at)}
            </div>
            <div className="meta">
              Config: {connectorConfigStateLabel(connector)} {connector.configured_secret_fields?.length ? `· secret fields: ${connector.configured_secret_fields.join(', ')}` : ''}
            </div>
            {connector.config_validation_error ? <p className="error-text">Config issue: {connector.config_validation_error}</p> : null}
            {connector.last_error ? <p className="error-text">Last error: {connector.last_error} ({formatDate(connector.last_error_at)})</p> : null}
            <ul>
              {connector.accounts.map((account) => (
                <li key={`${connector.connector_type}-${account.id}`}>
                  <span>{account.display_label || account.external_account_id}</span>
                  <span className="pill">{sourceLabel(connector.connector_type)}</span>
                  <span className="pill">{account.broker_name || 'Unknown broker'}</span>
                </li>
              ))}
            </ul>
            <div className="row">
              {!connector.supports_live_sync ? <span className="hint">Sync not supported</span> : null}
              <button
                disabled={!signedIn || !connector.supports_live_sync}
                onClick={() => connectorAction(connector.connector_type, 'sync')}
              >
                Sync
              </button>
              <button disabled={!signedIn || connector.account_count > 0} onClick={() => connectorAction(connector.connector_type, 'connect', {
                external_account_id: connectorDrafts[connector.connector_type]?.external_account_id || `${connector.connector_type}-account`,
                display_label: connectorDrafts[connector.connector_type]?.display_label || sourceLabel(connector.connector_type),
                broker_name: connector.connector_type,
              })}>
                Connect
              </button>
              <button disabled={!signedIn} onClick={() => connectorAction(connector.connector_type, 'disconnect')}>Disconnect</button>
            </div>
            {connector.account_count === 0 ? (
              <div className="row">
                <input
                  placeholder="Account id for connect"
                  value={connectorDrafts[connector.connector_type]?.external_account_id || ''}
                  onChange={(e) => setConnectorDrafts((prev) => ({
                    ...prev,
                    [connector.connector_type]: {
                      ...prev[connector.connector_type],
                      external_account_id: e.target.value,
                    }
                  }))}
                />
                <input
                  placeholder="Display label"
                  value={connectorDrafts[connector.connector_type]?.display_label || ''}
                  onChange={(e) => setConnectorDrafts((prev) => ({
                    ...prev,
                    [connector.connector_type]: {
                      ...prev[connector.connector_type],
                      display_label: e.target.value,
                    }
                  }))}
                />
              </div>
            ) : null}
            <details>
              <summary>Recent sync runs ({(syncHistory[connector.connector_type] || []).length})</summary>
              <ul>
                {(syncHistory[connector.connector_type] || []).map((run) => (
                  <li key={`run-${run.id}`}>
                    {(() => {
                      const diag = formatSyncRunDiagnostics(run)
                      return (
                        <>
                          #{run.id} · {run.status} · retries {run.retry_count}/{run.max_retries} · created {formatDate(run.created_at)}
                          {diag.resultCategory ? ` · category: ${diag.resultCategory}` : ''}
                          {diag.summary ? ` · ${diag.summary}` : ''}
                          {run.error_detail ? ` · error: ${run.error_detail}` : ''}
                          {diag.errorCode ? ` · code: ${diag.errorCode}` : ''}
                          {diag.errorCategory ? ` · failure: ${diag.errorCategory}` : ''}
                          {diag.isTransient === true ? ' · transient' : ''}
                          {diag.isTransient === false ? ' · structural' : ''}
                        </>
                      )
                    })()}
                  </li>
                ))}
              </ul>
            </details>
            {connector.supports_live_sync ? (
              <details>
                <summary>Connector credentials/config</summary>
                <div className="row">
                  <input
                    placeholder="Healthcheck URL"
                    value={(configDrafts[connector.connector_type] || {}).healthcheck_url || ''}
                    onChange={(e) => setConfigDrafts((prev) => ({
                      ...prev,
                      [connector.connector_type]: {
                        ...(prev[connector.connector_type] || buildConnectorConfigDraft()),
                        healthcheck_url: e.target.value,
                      },
                    }))}
                  />
                  <input
                    placeholder="External account id"
                    value={(configDrafts[connector.connector_type] || {}).external_account_id || ''}
                    onChange={(e) => setConfigDrafts((prev) => ({
                      ...prev,
                      [connector.connector_type]: {
                        ...(prev[connector.connector_type] || buildConnectorConfigDraft()),
                        external_account_id: e.target.value,
                      },
                    }))}
                  />
                  <input
                    type="number"
                    placeholder="Timeout seconds"
                    value={(configDrafts[connector.connector_type] || {}).timeout_seconds || 8}
                    onChange={(e) => setConfigDrafts((prev) => ({
                      ...prev,
                      [connector.connector_type]: {
                        ...(prev[connector.connector_type] || buildConnectorConfigDraft()),
                        timeout_seconds: e.target.value,
                      },
                    }))}
                  />
                </div>
                <div className="row">
                  <input
                    type="password"
                    placeholder={(configDrafts[connector.connector_type] || {}).hasSecret ? 'API token saved (enter to rotate)' : 'API token'}
                    value={(configDrafts[connector.connector_type] || {}).api_token || ''}
                    onChange={(e) => setConfigDrafts((prev) => ({
                      ...prev,
                      [connector.connector_type]: {
                        ...(prev[connector.connector_type] || buildConnectorConfigDraft()),
                        api_token: e.target.value,
                      },
                    }))}
                  />
                  <button disabled={!signedIn} onClick={() => saveConnectorConfig(connector.connector_type)}>Save config</button>
                  <button disabled={!signedIn} onClick={() => clearConnectorConfig(connector.connector_type)}>Clear config</button>
                </div>
                <p className="hint">Secrets are write-only in API responses. Saved tokens are never returned to the client.</p>
              </details>
            ) : null}
          </div>
        ))}
      </section>

      <section className="panel">
        <h2>Manual Journal (authenticated)</h2>
        <div className="row">
          <input placeholder="External account id" value={manualAccount.externalAccountId} onChange={(e) => setManualAccount({ ...manualAccount, externalAccountId: e.target.value })} />
          <input placeholder="Display label" value={manualAccount.displayLabel} onChange={(e) => setManualAccount({ ...manualAccount, displayLabel: e.target.value })} />
          <button disabled={!signedIn} onClick={createManualAccount}>Create manual account</button>
        </div>
        <div className="row">
          <input placeholder="Manual account id" value={manualTrade.externalAccountId} onChange={(e) => setManualTrade({ ...manualTrade, externalAccountId: e.target.value })} />
          <input placeholder="Symbol" value={manualTrade.symbol} onChange={(e) => setManualTrade({ ...manualTrade, symbol: e.target.value })} />
          <select value={manualTrade.side} onChange={(e) => setManualTrade({ ...manualTrade, side: e.target.value })}>
            <option value="buy">buy</option>
            <option value="sell">sell</option>
          </select>
          <input type="number" placeholder="PnL" value={manualTrade.pnl} onChange={(e) => setManualTrade({ ...manualTrade, pnl: e.target.value })} />
          <button disabled={!signedIn} onClick={createManualTrade}>Record trade</button>
        </div>
      </section>

      <section className="panel">
        <h2>CSV Import (authenticated)</h2>
        <div className="row">
          <input value={csvAccount} onChange={(e) => setCsvAccount(e.target.value)} placeholder="CSV account id" />
          <button disabled={!signedIn} onClick={importCsvTrades}>Import JSON rows as CSV trades</button>
        </div>
        <textarea rows={5} value={csvInput} onChange={(e) => setCsvInput(e.target.value)} />
      </section>

      <section className="panel">
        <h2>All Accounts (source-aware)</h2>
        <ul>
          {allAccounts.map((account) => (
            <li key={`${account.connector_type}-${account.id}`}>
              {account.display_label || account.external_account_id}
              {' · '}
              <span className="pill">{sourceLabel(account.connector_type)}</span>
              <span className="pill">{account.broker_name || 'Unknown broker'}</span>
            </li>
          ))}
        </ul>
      </section>
    </div>
  )
}

export default App
