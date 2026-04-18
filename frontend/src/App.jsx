import { useEffect, useMemo, useRef, useState } from 'react'
import { NavLink, Navigate, Route, Routes } from 'react-router-dom'
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
import { buildConnectorConfigDraft } from './connectorConfig'
import { buildApiUrl, formatTelegramConfigDiagnostics, resolveApiBase } from './apiBase'
import AccountSwitcher from './components/AccountSwitcher'
import AccountsOverviewPage from './pages/AccountsOverviewPage'
import ConnectionsPage from './pages/ConnectionsPage'
import './App.css'

const DEFAULT_STATUS = 'Sign in with Telegram to load your connected trading sources.'
const CANONICAL_HOST = 'www.talitrade.com'
const AUTH_DEBUG_QUERY_KEY = 'debugAuth'
const AUTH_DEBUG_STORAGE_KEY = 'tali_debug_auth'

const normalizeHost = (value) => {
  const raw = String(value || '').trim().toLowerCase()
  if (!raw) return ''
  return raw.replace(/^[a-z][a-z0-9+.-]*:\/\//, '').split('/')[0].split('?')[0].split('#')[0].split(':')[0].replace(/\.+$/, '')
}

const normalizeSessionUser = (user) => {
  if (!user || typeof user !== 'object') return null
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
  const [widgetDiagnostics, setWidgetDiagnostics] = useState([])
  const [configLoadFailed, setConfigLoadFailed] = useState(false)
  const [isConfigLoading, setIsConfigLoading] = useState(false)
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
  const [selectedAccountKey, setSelectedAccountKey] = useState('')

  const signedIn = Boolean(sessionToken && sessionUser?.telegram_user_id)
  const authDebugEnabled = useMemo(() => {
    const params = new URLSearchParams(window.location.search)
    return params.get(AUTH_DEBUG_QUERY_KEY) === '1' || localStorage.getItem(AUTH_DEBUG_STORAGE_KEY) === '1'
  }, [])
  const authHeaders = buildAuthHeaders(sessionToken)
  const currentHost = normalizeHost(window.location.hostname)
  const canonicalHost = normalizeHost(telegramConfig?.canonicalLoginDomain || telegramConfig?.loginDomain || CANONICAL_HOST)
  const isCanonicalHost = Boolean(currentHost && canonicalHost && currentHost === canonicalHost)

  function sourceLabel(connectorType) {
    if (connectorType === 'fundingpips_extension') return 'FundingPips Connector'
    if (connectorType === 'csv_import') return 'CSV Import'
    if (connectorType === 'manual') return 'Manual Journal'
    return connectorType
  }

  const allAccounts = useMemo(
    () => connectors.flatMap((connector) => connector.accounts.map((account) => ({ ...account, connector_type: connector.connector_type }))),
    [connectors],
  )

  const accountWorkspaces = useMemo(
    () => allAccounts.map((account) => ({
      accountKey: `${account.connector_type}:${account.external_account_id || account.id}`,
      accountId: account.id,
      externalAccountId: account.external_account_id,
      displayLabel: account.display_label || account.external_account_id || `Account ${account.id}`,
      connectorType: account.connector_type,
      sourceLabel: sourceLabel(account.connector_type),
      brokerName: account.broker_name || null,
    })),
    [allAccounts],
  )

  const selectedAccount = useMemo(
    () => accountWorkspaces.find((account) => account.accountKey === selectedAccountKey) || null,
    [accountWorkspaces, selectedAccountKey],
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

  useEffect(() => {
    if (selectedAccountKey) {
      const exists = accountWorkspaces.some((account) => account.accountKey === selectedAccountKey)
      if (exists) return
    }
    setSelectedAccountKey(accountWorkspaces[0]?.accountKey || '')
  }, [accountWorkspaces, selectedAccountKey])

  useEffect(() => {
    if (!widgetScriptLoaded || signedIn) return
    const timer = window.setTimeout(() => {
      const rendered = Boolean(widgetWrapRef.current?.querySelector('iframe, .telegram-login, div[id^="telegram-login"]'))
      if (!rendered) {
        setWidgetStatus('Telegram sign-in did not finish loading. Confirm you are on www.talitrade.com and disable browser blockers, then retry.')
      }
    }, 3000)
    return () => window.clearTimeout(timer)
  }, [widgetScriptLoaded, signedIn])

  const formatDate = (dateText) => (dateText ? new Date(dateText).toLocaleString() : '—')

  const statusTone = (connectorStatus) => {
    if (connectorStatus === 'connected') return 'status-connected'
    if (connectorStatus === 'sync_running' || connectorStatus === 'sync_queued' || connectorStatus === 'sync_retrying') return 'status-degraded'
    if (connectorStatus === 'degraded') return 'status-degraded'
    if (connectorStatus === 'sync_error') return 'status-error'
    return 'status-disconnected'
  }

  const syncStateLabel = (state) => state || 'idle'

  async function loadTelegramAuthConfig() {
    setIsConfigLoading(true)
    setConfigLoadFailed(false)
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
      setWidgetDiagnostics(diagnostics)
      setTelegramConfig(res.data || null)
      setWidgetStatus('Telegram sign-in is ready.')
    } catch (error) {
      setTelegramConfig(null)
      setConfigLoadFailed(true)
      const responseStatus = error?.response?.status
      const reason = responseStatus === 404
        ? 'http_404'
        : responseStatus === 500
          ? 'http_500'
          : responseStatus
            ? `http_${responseStatus}`
            : error instanceof SyntaxError
              ? 'invalid_json'
              : (typeof navigator !== 'undefined' && navigator.onLine === false)
                ? 'transport_offline'
                : 'cors_rejected_or_transport'
      const diagnostics = formatTelegramConfigDiagnostics({
        resolvedApiBase: resolvedBase,
        configUrl: requestUrl,
        configFetchStatus: responseStatus || 'request_failed',
        configFetchContentType: error?.response?.headers?.['content-type'] || 'missing',
        configFetchErrorName: error?.name || 'unknown',
        configFetchErrorMessage: String(error?.message || '').slice(0, 180) || 'n/a',
      })
      setWidgetDiagnostics([`config_fetch_failed=${reason}`, ...diagnostics])
      setWidgetStatus('We could not prepare Telegram sign-in right now. Please retry.')
    } finally {
      setIsConfigLoading(false)
    }
  }

  async function bootstrapSession(token) {
    setIsBootstrapping(true)
    try {
      const meRes = await axios.get(buildApiUrl('/auth/me'), { headers: buildAuthHeaders(token) })
      const user = normalizeSessionUser(meRes.data?.user)
      if (!canHydrateSession(meRes.data)) throw new Error('No authenticated user found in session')
      commitSession(token, user)
      setStatus(`Signed in as @${user.telegram_username || user.telegram_user_id}`)
      await loadConnectorData({ token, silent: true })
    } catch (error) {
      clearSession()
      setStatus(`Session expired or invalid. Please sign in again. (${error.message})`)
    } finally {
      setIsBootstrapping(false)
    }
  }

  function commitSession(token, user) {
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
    } catch {
      setStatus('Telegram sign-in failed. Please retry.')
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
    } catch {
      setStatus('Telegram sign-in failed. Please retry.')
    } finally {
      clearOidcCorrelation(localStorage)
      setIsBootstrapping(false)
    }
  }

  async function loadConnectorData({ token = sessionToken, silent = false } = {}) {
    try {
      const [catalogRes, overviewRes] = await Promise.all([
        axios.get(buildApiUrl('/connectors/catalog')),
        axios.get(buildApiUrl('/connectors/overview'), { headers: buildAuthHeaders(token) }),
      ])
      setCatalog(catalogRes.data.connectors || [])
      const overviewConnectors = overviewRes.data.connectors || []
      setConnectors(overviewConnectors)
      const configEntries = await Promise.all(
        overviewConnectors.map(async (connector) => {
          try {
            const configRes = await axios.get(buildApiUrl(`/connectors/${connector.connector_type}/config`), { headers: buildAuthHeaders(token) })
            return [connector.connector_type, buildConnectorConfigDraft(configRes.data || {})]
          } catch {
            return [connector.connector_type, buildConnectorConfigDraft()]
          }
        }),
      )
      setConfigDrafts((prev) => ({ ...prev, ...Object.fromEntries(configEntries) }))
      if (overviewConnectors.length > 0) {
        const historyEntries = await Promise.all(
          overviewConnectors.map(async (connector) => {
            try {
              const historyRes = await axios.get(buildApiUrl(`/connectors/${connector.connector_type}/sync-runs?limit=5`), { headers: buildAuthHeaders(token) })
              return [connector.connector_type, historyRes.data?.runs || []]
            } catch {
              return [connector.connector_type, []]
            }
          }),
        )
        setSyncHistory(Object.fromEntries(historyEntries))
      } else {
        setSyncHistory({})
      }
      if (!silent) setStatus(`Loaded ${overviewRes.data.count || 0} connected source(s) for your authenticated session`)
    } catch (error) {
      if (error?.response?.status === 401) {
        clearSession()
        setStatus('Your session expired. Please sign in with Telegram again.')
        return
      }
      setStatus(`Failed to load connectors: ${error.message}`)
    }
  }

  async function createManualAccount() {
    try {
      await axios.post(buildApiUrl('/ingest/accounts'), buildManualAccountPayload(manualAccount), { headers: authHeaders })
      setStatus('Manual account created for authenticated user')
      setManualTrade((prev) => ({ ...prev, externalAccountId: manualAccount.externalAccountId }))
      await loadConnectorData()
    } catch (error) {
      setStatus(`Manual account failed: ${error.message}`)
    }
  }

  async function createManualTrade() {
    try {
      await axios.post(buildApiUrl('/ingest/trades'), buildManualTradePayload(manualTrade), { headers: authHeaders })
      setStatus('Manual trade recorded for authenticated user')
      await loadConnectorData()
    } catch (error) {
      setStatus(`Manual trade failed: ${error.message}`)
    }
  }

  async function importCsvTrades() {
    try {
      const rows = JSON.parse(csvInput)
      await axios.post(buildApiUrl('/ingest/csv/trades'), buildCsvImportPayload(csvAccount, rows), { headers: authHeaders })
      setStatus(`Imported ${rows.length} CSV trade row(s) into authenticated workspace`)
      await loadConnectorData()
    } catch (error) {
      setStatus(`CSV import failed: ${error.message}`)
    }
  }

  async function connectorAction(connectorType, action, payload = {}) {
    try {
      await axios.post(buildApiUrl(`/connectors/${connectorType}/${action}`), payload, { headers: authHeaders })
      setStatus(`${sourceLabel(connectorType)} ${action} action completed`)
      await loadConnectorData({ silent: true })
    } catch (error) {
      setStatus(`${sourceLabel(connectorType)} ${action} failed: ${error.message}`)
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
    } catch (error) {
      setStatus(`${sourceLabel(connectorType)} config save failed: ${error.message}`)
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
    } catch (error) {
      setStatus(`${sourceLabel(connectorType)} config clear failed: ${error.message}`)
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
      <header className="app-header panel">
        <div>
          <h1>TaliTrade Platform</h1>
          <p>Status: {status}</p>
        </div>
        <div className="row">
          <span>Signed in:</span>
          <strong>{signedIn ? `@${sessionUser?.telegram_username || sessionUser?.telegram_user_id}` : 'No'}</strong>
          {signedIn ? <button onClick={() => loadConnectorData()}>Refresh</button> : null}
          {signedIn ? <button onClick={clearSession}>Sign out</button> : null}
        </div>
      </header>

      {!signedIn ? (
        <section className="panel">
          <h2>Session</h2>
          {isBootstrapping ? <p>Restoring authenticated session…</p> : null}
          <p>Primary login path: Telegram authenticated session.</p>
          {telegramConfig?.oidcEnabled ? (
            <button onClick={startOidcFlow}>Sign in with Telegram</button>
          ) : (
            <div ref={widgetWrapRef}>
              {!isCanonicalHost ? (
                <p className="error-text">Open www.talitrade.com to continue with Telegram sign-in.</p>
              ) : (
                <>
                  {isConfigLoading ? <p className="hint">Preparing secure Telegram sign-in…</p> : null}
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
              {configLoadFailed ? (
                <button onClick={() => loadTelegramAuthConfig()} type="button">
                  Retry Telegram setup
                </button>
              ) : null}
              {authDebugEnabled && widgetDiagnostics.length ? (
                <pre className="hint">{widgetDiagnostics.join('\n')}</pre>
              ) : null}
            </div>
          )}
        </section>
      ) : (
        <>
          <section className="panel app-shell-top">
            <nav className="app-nav">
              <NavLink className={({ isActive }) => `app-nav-link${isActive ? ' active' : ''}`} to="/app/accounts">Accounts</NavLink>
              <NavLink className={({ isActive }) => `app-nav-link${isActive ? ' active' : ''}`} to="/app/connections">Connections</NavLink>
            </nav>
            <AccountSwitcher
              accounts={accountWorkspaces}
              selectedAccountKey={selectedAccountKey}
              onSelectAccount={setSelectedAccountKey}
            />
          </section>

          <Routes>
            <Route path="/" element={<Navigate to="/app/accounts" replace />} />
            <Route path="/app" element={<Navigate to="/app/accounts" replace />} />
            <Route
              path="/app/accounts"
              element={(
                <AccountsOverviewPage
                  accountWorkspaces={accountWorkspaces}
                  selectedAccount={selectedAccount}
                  onSelectAccount={setSelectedAccountKey}
                />
              )}
            />
            <Route
              path="/app/connections"
              element={(
                <ConnectionsPage
                  catalog={catalog}
                  managedConnectors={managedConnectors}
                  syncHistory={syncHistory}
                  configDrafts={configDrafts}
                  connectorDrafts={connectorDrafts}
                  signedIn={signedIn}
                  manualAccount={manualAccount}
                  manualTrade={manualTrade}
                  csvAccount={csvAccount}
                  csvInput={csvInput}
                  sourceLabel={sourceLabel}
                  statusTone={statusTone}
                  formatDate={formatDate}
                  syncStateLabel={syncStateLabel}
                  setConnectorDrafts={setConnectorDrafts}
                  setConfigDrafts={setConfigDrafts}
                  setManualAccount={setManualAccount}
                  setManualTrade={setManualTrade}
                  setCsvAccount={setCsvAccount}
                  setCsvInput={setCsvInput}
                  connectorAction={connectorAction}
                  saveConnectorConfig={saveConnectorConfig}
                  clearConnectorConfig={clearConnectorConfig}
                  createManualAccount={createManualAccount}
                  createManualTrade={createManualTrade}
                  importCsvTrades={importCsvTrades}
                />
              )}
            />
            <Route path="*" element={<Navigate to="/app/accounts" replace />} />
          </Routes>
        </>
      )}
    </div>
  )
}

export default App
