import { useEffect, useMemo, useState } from 'react'
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

const API = 'https://trading-platform-production-70e0.up.railway.app'
const DEFAULT_STATUS = 'Sign in with Telegram to load your connected trading sources.'

function App() {
  const [sessionToken, setSessionToken] = useState(localStorage.getItem(SESSION_STORAGE_KEY) || '')
  const [sessionUser, setSessionUser] = useState(parseStoredUser(localStorage.getItem(USER_STORAGE_KEY)))
  const [telegramConfig, setTelegramConfig] = useState(null)
  const [catalog, setCatalog] = useState([])
  const [connectors, setConnectors] = useState([])
  const [status, setStatus] = useState(DEFAULT_STATUS)
  const [isBootstrapping, setIsBootstrapping] = useState(true)
  const [manualAccount, setManualAccount] = useState({ externalAccountId: '', brokerName: 'Manual', displayLabel: '', accountType: 'demo', accountSize: 10000 })
  const [manualTrade, setManualTrade] = useState({ externalAccountId: '', symbol: 'NAS100', side: 'buy', size: 0.1, entryPrice: 15000, exitPrice: 15025, pnl: 25 })
  const [csvInput, setCsvInput] = useState('[{"symbol":"US30","side":"buy","open_time":"2026-04-16T10:00:00Z","close_time":"2026-04-16T10:10:00Z","pnl":18}]')
  const [csvAccount, setCsvAccount] = useState('csv-account-1')

  const signedIn = Boolean(sessionToken && sessionUser?.telegram_user_id)
  const authHeaders = buildAuthHeaders(sessionToken)

  const allAccounts = useMemo(
    () => connectors.flatMap((connector) => connector.accounts.map((account) => ({ ...account, connector_type: connector.connector_type }))),
    [connectors]
  )

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

  async function loadTelegramAuthConfig() {
    try {
      const res = await axios.get(`${API}/auth/telegram/config`)
      setTelegramConfig(res.data || null)
    } catch {
      setTelegramConfig(null)
    }
  }

  async function bootstrapSession(token) {
    setIsBootstrapping(true)
    try {
      const meRes = await axios.get(`${API}/auth/me`, { headers: buildAuthHeaders(token) })
      const user = meRes.data?.user || null
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
    setSessionToken(token)
    setSessionUser(user)
    localStorage.setItem(SESSION_STORAGE_KEY, token)
    localStorage.setItem(USER_STORAGE_KEY, JSON.stringify(user))
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
      const res = await axios.post(`${API}/auth/telegram`, authData)
      const user = res.data?.user || null
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
      const res = await axios.post(`${API}/auth/telegram/oidc`, { id_token: idToken, nonce })
      const user = res.data?.user || null
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
        axios.get(`${API}/connectors/catalog`),
        axios.get(`${API}/connectors/overview`, { headers: buildAuthHeaders(token) })
      ])
      setCatalog(catalogRes.data.connectors || [])
      setConnectors(overviewRes.data.connectors || [])
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
      await axios.post(`${API}/ingest/accounts`, buildManualAccountPayload(manualAccount), { headers: authHeaders })
      setStatus('Manual account created for authenticated user')
      setManualTrade((prev) => ({ ...prev, externalAccountId: manualAccount.externalAccountId }))
      await loadConnectorData()
    } catch (e) {
      setStatus(`Manual account failed: ${e.message}`)
    }
  }

  async function createManualTrade() {
    try {
      await axios.post(`${API}/ingest/trades`, buildManualTradePayload(manualTrade), { headers: authHeaders })
      setStatus('Manual trade recorded for authenticated user')
      await loadConnectorData()
    } catch (e) {
      setStatus(`Manual trade failed: ${e.message}`)
    }
  }

  async function importCsvTrades() {
    try {
      const rows = JSON.parse(csvInput)
      await axios.post(`${API}/ingest/csv/trades`, buildCsvImportPayload(csvAccount, rows), { headers: authHeaders })
      setStatus(`Imported ${rows.length} CSV trade row(s) into authenticated workspace`)
      await loadConnectorData()
    } catch (e) {
      setStatus(`CSV import failed: ${e.message}`)
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
              <div>
                <script
                  async
                  src="https://telegram.org/js/telegram-widget.js?22"
                  data-telegram-login={telegramConfig?.botUsername || 'TaliTradeBot'}
                  data-size="large"
                  data-userpic="false"
                  data-request-access="write"
                  data-onauth="onTelegramAuth(user)"
                />
                <p className="hint">Telegram widget mode enabled.</p>
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
        {connectors.map((connector) => (
          <div key={connector.connector_type} className="card">
            <div className="row">
              <strong>{sourceLabel(connector.connector_type)}</strong>
              <span className="badge">{connector.status}</span>
            </div>
            <div className="meta">
              Accounts: {connector.account_count} · Last activity: {formatDate(connector.last_activity_at)} · Last sync: {formatDate(connector.last_sync_at)}
            </div>
            <ul>
              {connector.accounts.map((account) => (
                <li key={`${connector.connector_type}-${account.id}`}>
                  <span>{account.display_label || account.external_account_id}</span>
                  <span className="pill">{sourceLabel(connector.connector_type)}</span>
                  <span className="pill">{account.broker_name || 'Unknown broker'}</span>
                </li>
              ))}
            </ul>
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
