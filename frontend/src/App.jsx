import { useEffect, useMemo, useRef, useState } from 'react'
import { NavLink, Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
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
  normalizeTelegramAuthUser,
  resolveTelegramAuthUser,
  SESSION_STORAGE_KEY,
  USER_STORAGE_KEY,
} from './sessionAuth'
import { buildConnectorConfigDraft } from './connectorConfig'
import { buildApiUrl, formatTelegramConfigDiagnostics, resolveApiBase } from './apiBase'
import { fetchAccountWorkspaces } from './accountWorkspaceService'
import { deriveAccountConnectionState, isCurrentlyConnectedAccount, isPendingOnlyAccount } from './accountConnectionState'
import { deriveAppOnboardingState } from './onboardingState'
import AccountSwitcher from './components/AccountSwitcher'
import AccountsOverviewPage from './pages/AccountsOverviewPage'
import ConnectionsPage from './pages/ConnectionsPage'
import AppLandingPage from './pages/AppLandingPage'
import AddAccountFlowModal from './components/AddAccountFlowModal'
import { buildAddAccountProviders, PUBLIC_API_BETA_CONNECTORS } from './addAccountFlow'
import {
  buildAlpacaConnectPayload,
  clearSensitiveAddAccountDraft,
  resolveAlpacaConnectResult,
} from './alpacaConnectFlow'
import { checkMt5PairingState, createMt5PairingToken, fetchMt5BridgeRegistrationStatus } from './mt5PairingService'
import './App.css'

const DEFAULT_STATUS = 'Sign in with Telegram to load your connected trading sources.'
const CANONICAL_HOST = 'www.talitrade.com'
const AUTH_DEBUG_QUERY_KEY = 'debugAuth'
const AUTH_DEBUG_STORAGE_KEY = 'tali_debug_auth'
const USE_ACCOUNT_WORKSPACES_API = import.meta.env.VITE_APP_USE_ACCOUNT_WORKSPACES !== '0'
const FIRST_RUN_ADD_ACCOUNT_PROMPT_KEY = 'tali_first_run_add_account_prompt_seen'

const normalizeHost = (value) => {
  const raw = String(value || '').trim().toLowerCase()
  if (!raw) return ''
  return raw.replace(/^[a-z][a-z0-9+.-]*:\/\//, '').split('/')[0].split('?')[0].split('#')[0].split(':')[0].replace(/\.+$/, '')
}

const normalizeSessionUser = (user) => normalizeTelegramAuthUser(user)

function hasWorkspaceIdentity(account) {
  return Boolean(
    account
    && (
      account.account_key
      || account.trading_account_id
      || account.id
      || account.external_account_id
    ),
  )
}

function App() {
  const navigate = useNavigate()
  const location = useLocation()
  const [sessionToken, setSessionToken] = useState(localStorage.getItem(SESSION_STORAGE_KEY) || '')
  const [sessionUser, setSessionUser] = useState(normalizeSessionUser(parseStoredUser(localStorage.getItem(USER_STORAGE_KEY))))
  const [telegramConfig, setTelegramConfig] = useState(null)
  const [catalog, setCatalog] = useState([])
  const [connectors, setConnectors] = useState([])
  const [isWorkspaceLoading, setIsWorkspaceLoading] = useState(false)
  const [workspaceLoadError, setWorkspaceLoadError] = useState('')
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
  const [workspaceApiAccounts, setWorkspaceApiAccounts] = useState([])
  const [workspaceApiHydrated, setWorkspaceApiHydrated] = useState(false)
  const [isAddAccountOpen, setIsAddAccountOpen] = useState(false)
  const [selectedProviderType, setSelectedProviderType] = useState('mt5_bridge')
  const [addAccountDraft, setAddAccountDraft] = useState({
    external_account_id: '',
    display_label: '',
    bridge_url: '',
    mt5_server: '',
    provider_state: '',
    environment: 'paper',
    account_alias: '',
    api_key: '',
    api_secret: '',
    tradingview_webhook_url: '',
    tradingview_secret_hint: '',
  })
  const [isAddAccountSubmitting, setIsAddAccountSubmitting] = useState(false)
  const [addAccountError, setAddAccountError] = useState('')
  const [addAccountSuccessMessage, setAddAccountSuccessMessage] = useState('')
  const [pendingAccountFocus, setPendingAccountFocus] = useState(null)
  const [recentlyAddedAccountLabel, setRecentlyAddedAccountLabel] = useState('')
  const [authActionPrompt, setAuthActionPrompt] = useState('')

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
    if (connectorType === 'mt5_bridge') return 'MetaTrader 5 (MT5)'
    if (connectorType === 'tradingview_webhook') return 'TradingView Webhook'
    if (connectorType === 'alpaca_api') return 'Alpaca API'
    if (connectorType === 'oanda_api') return 'OANDA API (Beta)'
    if (connectorType === 'binance_api') return 'Binance API (Beta)'
    if (connectorType === 'csv_import') return 'CSV Import'
    if (connectorType === 'manual') return 'Manual Journal'
    return connectorType
  }

  const allAccounts = useMemo(() => (
    connectors.flatMap((connector) => {
      const connectorAccounts = Array.isArray(connector?.accounts) ? connector.accounts : []
      return connectorAccounts
        .filter((account) => hasWorkspaceIdentity(account))
        .map((account) => ({ ...account, connector_type: connector.connector_type, connector_status: connector.status }))
    })
  ), [connectors])

  const accountWorkspaces = useMemo(
    () => allAccounts.map((account) => ({
      account_key: `${account.connector_type}:${account.external_account_id || account.id}`,
      trading_account_id: account.id,
      external_account_id: account.external_account_id,
      display_label: account.display_label || account.external_account_id || `Account ${account.id}`,
      connector_type: account.connector_type,
      source_label: sourceLabel(account.connector_type),
      broker_name: account.broker_name || null,
      connection_status: String(account.connection_status || 'disconnected').toLowerCase(),
      sync_state: String(account.sync_state || 'idle').toLowerCase(),
      account_type: account.account_type || null,
      last_activity_at: account.last_activity_at || null,
      last_sync_at: account.last_sync_at || null,
      is_primary: Boolean(account.is_primary),
    })),
    [allAccounts],
  )

  const unifiedAccountWorkspaces = useMemo(() => {
    if (USE_ACCOUNT_WORKSPACES_API && workspaceApiHydrated) return workspaceApiAccounts
    return accountWorkspaces
  }, [workspaceApiAccounts, accountWorkspaces, workspaceApiHydrated])

  const selectedAccount = useMemo(
    () => unifiedAccountWorkspaces.find((account) => account.account_key === selectedAccountKey) || null,
    [unifiedAccountWorkspaces, selectedAccountKey],
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
          integration_status: entry.integration_status || 'unknown',
          connection_layer: entry.connection_layer || null,
          notes: entry.notes || '',
          category: entry.category || null,
          connection_state_labels: entry.connection_state_labels || {},
          onboarding_copy: entry.onboarding_copy || '',
          beta: Boolean(entry.beta),
        })
      } else {
        map.set(entry.connector_type, {
          ...map.get(entry.connector_type),
          supports_live_sync: Boolean(entry.supports_live_sync),
          integration_status: entry.integration_status || map.get(entry.connector_type)?.integration_status || 'unknown',
          connection_layer: entry.connection_layer || map.get(entry.connector_type)?.connection_layer || null,
          notes: entry.notes || map.get(entry.connector_type)?.notes || '',
          category: entry.category || map.get(entry.connector_type)?.category || null,
          connection_state_labels: entry.connection_state_labels || map.get(entry.connector_type)?.connection_state_labels || {},
          onboarding_copy: entry.onboarding_copy || map.get(entry.connector_type)?.onboarding_copy || '',
          beta: Boolean(entry.beta),
        })
      }
    }
    return Array.from(map.values())
  }, [catalog, connectors])

  const addAccountProviders = useMemo(() => buildAddAccountProviders(catalog, sourceLabel), [catalog])

  const addFlowIntent = useMemo(() => new URLSearchParams(location.search).get('addFlow') || '', [location.search])
  const accountConnectionState = useMemo(() => deriveAccountConnectionState(unifiedAccountWorkspaces), [unifiedAccountWorkspaces])
  const onboardingState = useMemo(() => deriveAppOnboardingState({
    signedIn,
    useWorkspaceApi: USE_ACCOUNT_WORKSPACES_API,
    workspaceApiHydrated,
    workspaceApiAccounts,
    fallbackAccounts: accountWorkspaces,
  }), [signedIn, workspaceApiHydrated, workspaceApiAccounts, accountWorkspaces])
  const hasZeroConnectedAccounts = onboardingState.hasZeroUsableAccounts
  const switcherAccounts = useMemo(
    () => unifiedAccountWorkspaces.filter((account) => (
      isCurrentlyConnectedAccount(account) || isPendingOnlyAccount(account)
    )),
    [unifiedAccountWorkspaces],
  )
  const shellSyncingCount = useMemo(
    () => managedConnectors.filter((connector) => ['sync_running', 'sync_retrying', 'sync_queued'].includes(connector.current_sync_state)).length,
    [managedConnectors],
  )
  const shellNeedsAttentionCount = useMemo(
    () => managedConnectors.filter((connector) => connector.last_error || connector.status === 'sync_error').length,
    [managedConnectors],
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

  useEffect(() => {
    if (selectedAccountKey) {
      const exists = unifiedAccountWorkspaces.some((account) => account.account_key === selectedAccountKey)
      if (exists) return
    }
    const primary = unifiedAccountWorkspaces.find((account) => account.is_primary && isCurrentlyConnectedAccount(account))
    const firstConnected = unifiedAccountWorkspaces.find((account) => isCurrentlyConnectedAccount(account))
    const firstPending = unifiedAccountWorkspaces.find((account) => !isCurrentlyConnectedAccount(account) && ['queued', 'running', 'retrying'].includes(String(account.sync_state || '').toLowerCase()))
    setSelectedAccountKey(primary?.account_key || firstConnected?.account_key || firstPending?.account_key || '')
  }, [unifiedAccountWorkspaces, selectedAccountKey])


  useEffect(() => {
    if (!pendingAccountFocus) return
    const matched = unifiedAccountWorkspaces.find((account) => (
      account.connector_type === pendingAccountFocus.connectorType
      && (
        (pendingAccountFocus.tradingAccountId != null && Number(account.trading_account_id) === Number(pendingAccountFocus.tradingAccountId))
        || (pendingAccountFocus.externalAccountId && String(account.external_account_id || '') === String(pendingAccountFocus.externalAccountId))
      )
    ))
    if (!matched) return
    setSelectedAccountKey(matched.account_key)
    setRecentlyAddedAccountLabel(matched.display_label || matched.external_account_id || matched.account_key)
    setPendingAccountFocus(null)
  }, [pendingAccountFocus, unifiedAccountWorkspaces])

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

  useEffect(() => {
    if (signedIn) return
    if (telegramConfig?.oidcEnabled) return
    if (!isCanonicalHost) return
    const mountNode = widgetWrapRef.current
    if (!mountNode) return

    const botUsername = telegramConfig?.botUsername || 'TaliTradeBot'
    mountNode.innerHTML = ''
    setWidgetScriptLoaded(false)
    setWidgetStatus('')

    const script = document.createElement('script')
    script.async = true
    script.src = 'https://telegram.org/js/telegram-widget.js?22'
    script.setAttribute('data-telegram-login', botUsername)
    script.setAttribute('data-size', 'large')
    script.setAttribute('data-userpic', 'false')
    script.setAttribute('data-request-access', 'write')
    script.setAttribute('data-onauth', 'onTelegramAuth(user)')
    script.onload = () => {
      setWidgetScriptLoaded(true)
      setWidgetStatus('')
    }
    script.onerror = () => {
      setWidgetStatus('Could not load Telegram widget script. Disable blockers and retry.')
    }
    mountNode.appendChild(script)

    return () => {
      script.onload = null
      script.onerror = null
      if (mountNode.contains(script)) {
        mountNode.removeChild(script)
      }
    }
  }, [signedIn, telegramConfig?.oidcEnabled, telegramConfig?.botUsername, isCanonicalHost])

  useEffect(() => {
    if (!signedIn) return
    if (!hasZeroConnectedAccounts) return
    if (location.pathname !== '/app' && location.pathname !== '/app/accounts') return
    if (isAddAccountOpen) return
    if (sessionStorage.getItem(FIRST_RUN_ADD_ACCOUNT_PROMPT_KEY) === '1') return
    sessionStorage.setItem(FIRST_RUN_ADD_ACCOUNT_PROMPT_KEY, '1')
    openAddAccountFlow('mt5_bridge')
  }, [signedIn, hasZeroConnectedAccounts, location.pathname, isAddAccountOpen])

  const formatDate = (dateText) => (dateText ? new Date(dateText).toLocaleString() : '—')

  const statusTone = (connectorStatus) => {
    if (connectorStatus === 'connected') return 'status-connected'
    if (connectorStatus === 'active') return 'status-connected'
    if (connectorStatus === 'ready_for_account_attach') return 'status-connected'
    if (connectorStatus === 'paper_connected' || connectorStatus === 'live_connected') return 'status-connected'
    if (connectorStatus === 'sync_running' || connectorStatus === 'sync_queued' || connectorStatus === 'sync_retrying') return 'status-degraded'
    if (connectorStatus === 'awaiting_alerts' || connectorStatus === 'bridge_required' || connectorStatus === 'waiting_for_registration' || connectorStatus === 'beta_pending' || connectorStatus === 'metadata_saved' || connectorStatus === 'awaiting_secure_auth' || connectorStatus === 'waiting_for_secure_auth_support') return 'status-degraded'
    if (connectorStatus === 'validation_failed') return 'status-error'
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
      setWidgetStatus('')
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
    setWorkspaceApiAccounts([])
    setWorkspaceApiHydrated(false)
    setIsWorkspaceLoading(false)
    setRecentlyAddedAccountLabel('')
    localStorage.removeItem(SESSION_STORAGE_KEY)
    localStorage.removeItem(USER_STORAGE_KEY)
    sessionStorage.removeItem(FIRST_RUN_ADD_ACCOUNT_PROMPT_KEY)
  }

  async function signInWithTelegramWidget(authData) {
    try {
      const res = await axios.post(buildApiUrl('/auth/telegram'), authData)
      const token = res.data?.access_token || ''
      const user = await resolveTelegramAuthUser({
        accessToken: token,
        responseUser: res.data?.user,
        widgetUser: authData,
        fetchMeUser: async () => {
          const meRes = await axios.get(buildApiUrl('/auth/me'), { headers: buildAuthHeaders(token) })
          return meRes.data?.user || null
        },
      })
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
    setIsWorkspaceLoading(true)
    setWorkspaceLoadError('')
    try {
      const [catalogRes, overviewRes] = await Promise.all([
        axios.get(buildApiUrl('/connectors/catalog')),
        axios.get(buildApiUrl('/connectors/overview'), { headers: buildAuthHeaders(token) }),
      ])
      setCatalog(catalogRes.data.connectors || [])
      const overviewConnectors = overviewRes.data.connectors || []
      setConnectors(overviewConnectors)
      if (USE_ACCOUNT_WORKSPACES_API) {
        try {
          const workspaceRows = await fetchAccountWorkspaces(token)
          setWorkspaceApiAccounts(workspaceRows)
          setWorkspaceApiHydrated(true)
        } catch {
          setWorkspaceApiAccounts([])
          setWorkspaceApiHydrated(false)
        }
      } else {
        setWorkspaceApiAccounts([])
        setWorkspaceApiHydrated(false)
      }
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
      setWorkspaceLoadError(`Could not hydrate workspace data: ${error?.message || 'unknown error'}`)
      setStatus(`Failed to load connectors: ${error.message}`)
    } finally {
      setIsWorkspaceLoading(false)
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
      return { ok: true }
    } catch (error) {
      setStatus(`${sourceLabel(connectorType)} ${action} failed: ${error.message}`)
      throw error
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



  function openAddAccountFlow(defaultProviderType = 'mt5_bridge') {
    if (!signedIn) {
      setAuthActionPrompt('Sign in with Telegram to open Add Account and run account actions.')
      setStatus('Sign in with Telegram to add or connect trading accounts.')
      return
    }
    setAuthActionPrompt('')
    setSelectedProviderType(defaultProviderType)
    setAddAccountError('')
    setAddAccountSuccessMessage('')
    setIsAddAccountOpen(true)
  }

  function closeAddAccountFlow() {
    setIsAddAccountOpen(false)
    setAddAccountError('')
    setAddAccountSuccessMessage('')
  }

  async function submitAddAccount(provider) {
    const externalAccountId = (addAccountDraft.external_account_id || '').trim()
    const displayLabel = (addAccountDraft.display_label || '').trim()

    if ((provider.connectorType === 'mt5_bridge' || provider.connectorType === 'fundingpips_extension') && !externalAccountId) {
      setAddAccountError('Account ID is required for this connector flow.')
      return
    }

    setIsAddAccountSubmitting(true)
    setAddAccountError('')
    setAddAccountSuccessMessage('')

    try {
      if (provider.connectorType === 'csv_import') {
        closeAddAccountFlow()
        navigate('/app/connections?addFlow=csv')
        return
      }

      if (provider.connectorType === 'manual') {
        closeAddAccountFlow()
        navigate('/app/connections?addFlow=manual')
        return
      }
      if (provider.connectorType === 'tradingview_webhook') {
        if (!addAccountDraft.tradingview_webhook_url) {
          const tvRes = await axios.post(
            buildApiUrl('/providers/tradingview-webhook/connections'),
            {
              display_label: displayLabel || 'TradingView Signals',
              account_alias: (addAccountDraft.account_alias || '').trim() || null,
            },
            { headers: authHeaders },
          )
          const created = tvRes?.data?.connection || {}
          setAddAccountDraft((prev) => ({
            ...prev,
            provider_state: 'webhook_created',
            tradingview_webhook_url: created.webhook_url || '',
            tradingview_secret_hint: created.webhook_secret_hint || '',
          }))
          await loadConnectorData({ silent: true })
          setStatus('TradingView webhook created. Copy the URL and complete the flow.')
          return
        }
        closeAddAccountFlow()
        navigate('/app/accounts')
        return
      }
      if (PUBLIC_API_BETA_CONNECTORS.includes(provider.connectorType)) {
        if (provider.connectorType === 'alpaca_api') {
          const alpacaPayload = buildAlpacaConnectPayload({
            label: displayLabel || provider.title,
            environment: addAccountDraft.environment || 'paper',
            apiKey: addAccountDraft.api_key,
            apiSecret: addAccountDraft.api_secret,
          })
          const connectResponse = await axios.post(
            buildApiUrl('/providers/public-api/alpaca_api/connect'),
            alpacaPayload,
            { headers: authHeaders },
          )
          const { providerStatus, accountId, displayLabel: connectedLabel } = resolveAlpacaConnectResult(connectResponse?.data || {})
          setPendingAccountFocus({
            connectorType: 'alpaca_api',
            externalAccountId: '',
            tradingAccountId: accountId,
            displayLabel: connectedLabel || displayLabel || provider.title,
          })
          setAddAccountSuccessMessage(`Alpaca ${providerStatus.replace('_', ' ')}. Refreshing workspace…`)
          setStatus(`Alpaca ${providerStatus.replace('_', ' ')}.`)
        } else {
          await axios.post(
            buildApiUrl(`/providers/public-api/${provider.connectorType}/beta`),
            {
              display_label: displayLabel || provider.title,
              environment: addAccountDraft.environment || 'paper',
              account_alias: (addAccountDraft.account_alias || '').trim() || null,
            },
            { headers: authHeaders },
          )
        }
        await loadConnectorData({ silent: true })
        if (provider.connectorType === 'alpaca_api') {
          await new Promise((resolve) => window.setTimeout(resolve, 900))
        }
        closeAddAccountFlow()
        navigate('/app/accounts')
        return
      }

      const payload = {
        external_account_id: externalAccountId,
        display_label: displayLabel || provider.title,
        broker_name: provider.connectorType,
        connection_metadata: provider.connectorType === 'mt5_bridge'
          ? {
            bridge_url: (addAccountDraft.bridge_url || '').trim(),
            mt5_server: (addAccountDraft.mt5_server || '').trim(),
            provider_state: (addAccountDraft.provider_state || '').trim() || 'bridge_required',
          }
          : {},
      }

      await connectorAction(provider.connectorType, 'connect', payload)
      setPendingAccountFocus({ connectorType: provider.connectorType, externalAccountId })
      closeAddAccountFlow()
      navigate('/app/accounts')
    } catch (error) {
      const apiDetail = error?.response?.data?.detail
      setAddAccountError(apiDetail || error?.message || 'Could not complete this add account flow.')
    } finally {
      setAddAccountDraft((prev) => clearSensitiveAddAccountDraft(prev))
      setIsAddAccountSubmitting(false)
    }
  }

  async function checkMt5Pairing(draft) {
    return checkMt5PairingState(sessionToken, draft)
  }

  async function createPairingToken(draft) {
    return createMt5PairingToken(sessionToken, draft)
  }

  async function loadMt5RegistrationStatus() {
    return fetchMt5BridgeRegistrationStatus(sessionToken)
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
    <div className={`app ${signedIn ? 'app-authenticated' : 'app-unauthenticated'}`}>
      <header className="app-header-shell">
        <div className="app-brand-row">
          <div className="brand-avatar">{signedIn ? (sessionUser?.first_name?.[0] || sessionUser?.telegram_username?.[0] || 'T') : 'T'}</div>
          <div>
            <h1>TaliTrade</h1>
            <p className="hint">{signedIn ? 'Connected workspace' : 'Waiting for Telegram sign-in'}</p>
          </div>
        </div>
        <div className="app-header-actions">
          <span className="header-clock">{new Date().toLocaleTimeString()}</span>
          <span className="header-bell" aria-hidden>🔔</span>
          <span className="header-profile">{signedIn ? `@${sessionUser?.telegram_username || sessionUser?.telegram_user_id}` : 'Guest'}</span>
          {signedIn ? <button onClick={() => loadConnectorData()} type="button">Refresh</button> : null}
          {signedIn ? <button onClick={clearSession} type="button">Sign out</button> : null}
        </div>
      </header>
      <section className="panel app-shell-health-strip">
        <div className="row">
          <span className={`badge ${signedIn ? 'status-connected' : 'status-disconnected'}`}>{signedIn ? 'Authenticated' : 'Signed out'}</span>
          <span className="pill">Usable accounts: {accountConnectionState.connectedUsableCount}</span>
          <span className="pill">Pending setup: {accountConnectionState.pendingOnlyCount}</span>
          <span className="pill">Syncing: {shellSyncingCount}</span>
          <span className="pill">Needs attention: {shellNeedsAttentionCount}</span>
          {selectedAccount ? <span className="pill">Active: {selectedAccount.display_label || selectedAccount.external_account_id}</span> : null}
        </div>
        <p className="hint">{status}</p>
        {workspaceLoadError ? <p className="error-text">{workspaceLoadError}</p> : null}
      </section>

      <section className="panel app-shell-top">
        <div className="app-shell-nav-block">
          <p className="kicker">Workspace</p>
          <nav className="app-nav" aria-label="Primary app navigation">
            <NavLink className={({ isActive }) => `app-nav-link${isActive ? ' active' : ''}`} to="/app">Dashboard</NavLink>
            <NavLink className={({ isActive }) => `app-nav-link${isActive ? ' active' : ''}`} to="/app/accounts">Accounts</NavLink>
            <NavLink className={({ isActive }) => `app-nav-link${isActive ? ' active' : ''}`} to="/app/connections">Connections</NavLink>
          </nav>
        </div>
        <div className="row app-shell-actions">
          <button
            type="button"
            className="app-nav-link add-account-nav-link"
            onClick={() => openAddAccountFlow('mt5_bridge')}
            disabled={!signedIn}
            aria-disabled={!signedIn}
            title={signedIn ? 'Add and connect an account' : 'Sign in with Telegram to add an account'}
          >
            + Add Account
          </button>
          <AccountSwitcher
            accounts={switcherAccounts}
            selectedAccountKey={selectedAccountKey}
            onSelectAccount={setSelectedAccountKey}
          />
        </div>
      </section>
      {!signedIn && authActionPrompt ? (
        <section className="panel app-shell-auth-prompt" aria-live="polite">
          <div className="row">
            <strong>Telegram sign-in required</strong>
            <button type="button" className="secondary-button" onClick={() => setAuthActionPrompt('')}>Dismiss</button>
          </div>
          <p className="hint">{authActionPrompt}</p>
        </section>
      ) : null}
      {!signedIn ? (
        <section className="panel auth-shell-gate" aria-live="polite">
          <h2>Secure Telegram sign-in</h2>
          {isBootstrapping ? <p>Restoring authenticated session…</p> : null}
          <p>Authenticate to unlock the account workspace, live sync operations, and connector controls.</p>
          {telegramConfig?.oidcEnabled ? (
            <button onClick={startOidcFlow}>Continue with Telegram</button>
          ) : (
            <div ref={widgetWrapRef}>
              {!isCanonicalHost ? (
                <p className="error-text">Open www.talitrade.com to continue with Telegram sign-in.</p>
              ) : (
                isConfigLoading ? <p className="hint">Loading Telegram sign-in…</p> : null
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
      ) : null}

      {signedIn && hasZeroConnectedAccounts ? (
        <section className="panel first-run-shell-cta" aria-live="polite">
          <div className="row">
            <h2>Start your account workspace</h2>
            <button type="button" className="primary-cta" onClick={() => openAddAccountFlow('mt5_bridge')}>Add your first account</button>
          </div>
          <p className="hint">
            Choose MT5, FundingPips Extension, TradingView Webhook, CSV Import, or Manual Journal. You can return to <strong>Connections</strong> later for operational setup details.
          </p>
        </section>
      ) : null}

      <Routes>
            <Route path="/" element={<Navigate to="/app" replace />} />
            <Route
              path="/app"
              element={(
                <AppLandingPage
                  signedIn={signedIn}
                  hasZeroConnectedAccounts={hasZeroConnectedAccounts}
                  accountConnectionState={accountConnectionState}
                  onAddAccount={openAddAccountFlow}
                  selectedAccount={selectedAccount}
                  managedConnectors={managedConnectors}
                  syncHistory={syncHistory}
                  formatDate={formatDate}
                  isWorkspaceLoading={isWorkspaceLoading}
                  workspaceLoadError={workspaceLoadError}
                />
              )}
            />
            <Route
              path="/app/accounts"
              element={(
                <AccountsOverviewPage
                  signedIn={signedIn}
                  accountWorkspaces={unifiedAccountWorkspaces}
                  selectedAccount={selectedAccount}
                  onSelectAccount={setSelectedAccountKey}
                  onAddAccount={openAddAccountFlow}
                  recentlyAddedAccountLabel={recentlyAddedAccountLabel}
                  formatDate={formatDate}
                  isWorkspaceLoading={isWorkspaceLoading}
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
                  onAddAccount={openAddAccountFlow}
                  addFlowIntent={addFlowIntent}
                  isWorkspaceLoading={isWorkspaceLoading}
                />
              )}
            />
            <Route path="*" element={<Navigate to="/app/accounts" replace />} />
      </Routes>
      <AddAccountFlowModal
        isOpen={isAddAccountOpen}
        providers={addAccountProviders}
        selectedProviderType={selectedProviderType}
        setSelectedProviderType={setSelectedProviderType}
        draft={addAccountDraft}
        setDraft={setAddAccountDraft}
        onClose={closeAddAccountFlow}
        onSubmit={submitAddAccount}
        onCheckMt5Pairing={checkMt5Pairing}
        onCreateMt5PairingToken={createPairingToken}
        onLoadMt5RegistrationStatus={loadMt5RegistrationStatus}
        isSubmitting={isAddAccountSubmitting}
        error={addAccountError}
        successMessage={addAccountSuccessMessage}
      />
    </div>
  )
}

export default App
