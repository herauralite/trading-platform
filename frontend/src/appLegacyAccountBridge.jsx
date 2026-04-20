import React, { useMemo, useState } from 'react'
import { createRoot } from 'react-dom/client'
import AddAccountFlowModal from './components/AddAccountFlowModal'
import { buildAddAccountProviders, PUBLIC_API_BETA_CONNECTORS, PUBLIC_API_CONNECTORS } from './addAccountFlow'
import { checkMt5PairingState, createMt5PairingToken, fetchMt5BridgeRegistrationStatus } from './mt5PairingService'

const LEGACY_SESSION_TOKEN_KEY = 'tali_session_token_v1'

const DEFAULT_DRAFT = {
  external_account_id: '',
  display_label: '',
  bridge_url: '',
  mt5_server: '',
  provider_state: '',
  environment: 'paper',
  account_alias: '',
  api_key: '',
  api_secret: '',
  base_url: '',
  account_id: '',
  email: '',
  password: '',
  server: '',
  tradingview_webhook_url: '',
  tradingview_secret_hint: '',
}

function sourceLabel(connectorType) {
  if (connectorType === 'fundingpips_extension') return 'FundingPips Connector'
  if (connectorType === 'mt5_bridge') return 'MetaTrader 5 (MT5)'
  if (connectorType === 'tradingview_webhook') return 'TradingView Webhook'
  if (connectorType === 'alpaca_api') return 'Alpaca API (Beta)'
  if (connectorType === 'tradelocker_api') return 'TradeLocker API'
  if (connectorType === 'oanda_api') return 'OANDA API (Beta)'
  if (connectorType === 'binance_api') return 'Binance API (Beta)'
  if (connectorType === 'csv_import') return 'CSV Import'
  if (connectorType === 'manual') return 'Manual Journal'
  return connectorType
}

function buildApiUrl(path) {
  if (window.TaliApiBase?.buildApiUrl) return window.TaliApiBase.buildApiUrl(path)
  const base = window.TaliApiBase?.resolveApiBase?.() || ''
  return `${base}${path}`
}

async function authedFetch(path, init = {}) {
  const token = localStorage.getItem(LEGACY_SESSION_TOKEN_KEY) || ''
  const headers = new Headers(init.headers || {})
  headers.set('Content-Type', 'application/json')
  if (token) headers.set('Authorization', `Bearer ${token}`)
  const res = await fetch(buildApiUrl(path), { ...init, headers, credentials: 'include' })
  const raw = await res.text()
  let body = {}
  try { body = raw ? JSON.parse(raw) : {} } catch { body = {} }
  if (!res.ok) {
    throw new Error(body?.detail || body?.message || `Request failed (${res.status})`)
  }
  return body
}

function BridgeModal({ isOpen, onClose, onSuccess }) {
  const [catalog, setCatalog] = useState([])
  const [selectedProviderType, setSelectedProviderType] = useState('mt5_bridge')
  const [draft, setDraft] = useState(DEFAULT_DRAFT)
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [error, setError] = useState('')

  const providers = useMemo(() => buildAddAccountProviders(catalog, sourceLabel), [catalog])

  React.useEffect(() => {
    if (!isOpen) return
    setError('')
    void (async () => {
      try {
        const payload = await authedFetch('/connectors/catalog', { method: 'GET' })
        setCatalog(payload?.connectors || [])
      } catch {
        setCatalog([])
      }
    })()
  }, [isOpen])

  async function submitAddAccount(provider) {
    const externalAccountId = String(draft.external_account_id || '').trim()
    const displayLabel = String(draft.display_label || '').trim()

    if ((provider.connectorType === 'mt5_bridge' || provider.connectorType === 'fundingpips_extension') && !externalAccountId) {
      setError('Account ID is required for this connector flow.')
      return
    }

    setIsSubmitting(true)
    setError('')
    try {
      if (provider.connectorType === 'csv_import' || provider.connectorType === 'manual') {
        onClose()
        return
      }
      if (provider.connectorType === 'tradingview_webhook') {
        if (!draft.tradingview_webhook_url) {
          const tvRes = await authedFetch('/providers/tradingview-webhook/connections', {
            method: 'POST',
            body: JSON.stringify({
              display_label: displayLabel || 'TradingView Signals',
              account_alias: String(draft.account_alias || '').trim() || null,
            }),
          })
          const created = tvRes?.connection || {}
          setDraft((prev) => ({
            ...prev,
            provider_state: 'webhook_created',
            tradingview_webhook_url: created.webhook_url || '',
            tradingview_secret_hint: created.webhook_secret_hint || '',
          }))
          return
        }
        onClose()
        onSuccess()
        return
      }

      if (PUBLIC_API_CONNECTORS.includes(provider.connectorType)) {
        if (provider.connectorType === 'alpaca_api') {
          await authedFetch('/providers/public-api/alpaca_api/connect', {
            method: 'POST',
            body: JSON.stringify({
              label: displayLabel || provider.title,
              environment: draft.environment || 'paper',
              api_key: String(draft.api_key || '').trim(),
              api_secret: String(draft.api_secret || '').trim(),
            }),
          })
        } else if (provider.connectorType === 'tradelocker_api') {
          await authedFetch('/providers/public-api/tradelocker_api/connect', {
            method: 'POST',
            body: JSON.stringify({
              label: displayLabel || provider.title,
              base_url: String(draft.base_url || '').trim(),
              account_id: String(draft.account_id || '').trim(),
              email: String(draft.email || '').trim(),
              password: draft.password || '',
              server: String(draft.server || '').trim() || null,
              environment: draft.environment || 'paper',
            }),
          })
        }
        onClose()
        onSuccess()
        return
      }

      if (PUBLIC_API_BETA_CONNECTORS.includes(provider.connectorType)) {
        await authedFetch(`/providers/public-api/${provider.connectorType}/beta`, {
          method: 'POST',
          body: JSON.stringify({
            display_label: displayLabel || provider.title,
            environment: draft.environment || 'paper',
            account_alias: String(draft.account_alias || '').trim() || null,
          }),
        })
        onClose()
        onSuccess()
        return
      }

      await authedFetch(`/connectors/${provider.connectorType}/connect`, {
        method: 'POST',
        body: JSON.stringify({
          external_account_id: externalAccountId,
          display_label: displayLabel || provider.title,
          broker_name: provider.connectorType,
          connection_metadata: provider.connectorType === 'mt5_bridge'
            ? {
              bridge_url: String(draft.bridge_url || '').trim(),
              mt5_server: String(draft.mt5_server || '').trim(),
              provider_state: String(draft.provider_state || '').trim() || 'bridge_required',
            }
            : {},
        }),
      })

      onClose()
      onSuccess()
    } catch (submitError) {
      setError(submitError?.message || 'Could not complete this add account flow.')
    } finally {
      setDraft((prev) => ({ ...prev, api_key: '', api_secret: '', password: '' }))
      setIsSubmitting(false)
    }
  }

  return (
    <AddAccountFlowModal
      isOpen={isOpen}
      providers={providers}
      selectedProviderType={selectedProviderType}
      setSelectedProviderType={setSelectedProviderType}
      draft={draft}
      setDraft={setDraft}
      onClose={onClose}
      onSubmit={submitAddAccount}
      onCheckMt5Pairing={(payload) => checkMt5PairingState(payload, { fetchImpl: fetch })}
      onCreateMt5PairingToken={(payload) => createMt5PairingToken(payload, { fetchImpl: fetch })}
      onLoadMt5RegistrationStatus={() => fetchMt5BridgeRegistrationStatus({ fetchImpl: fetch })}
      isSubmitting={isSubmitting}
      error={error}
    />
  )
}

function ensureModalStyles() {
  if (document.getElementById('legacy-add-account-modal-styles')) return
  const style = document.createElement('style')
  style.id = 'legacy-add-account-modal-styles'
  style.textContent = `
.modal-backdrop{position:fixed;inset:0;background:rgba(0,0,0,.62);z-index:1201;padding:20px;overflow:auto}
.modal-panel{max-width:980px;margin:20px auto;background:#1e2c3a;border:1px solid rgba(255,255,255,.08);border-radius:14px;padding:16px;color:#e8f4fd}
.row{display:flex;gap:10px;flex-wrap:wrap}.row>*{flex:1}
.provider-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px;margin:10px 0}
.provider-card,.secondary-button,button,input,select{border-radius:10px;border:1px solid rgba(255,255,255,.12);background:#242f3d;color:#e8f4fd;padding:10px}
.provider-card.selected{border-color:#2aabee;background:rgba(42,171,238,.12)}
.add-account-form{margin-top:10px;padding:12px;background:#17212b;border-radius:12px}
.hint{font-size:12px;color:#8bafc7}.error-text{color:#e05c5c}.success-text{color:#4dcd8a}.mono{font-family:monospace}
.meta-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:8px;margin:10px 0}.meta-card{background:#242f3d;padding:10px;border-radius:8px}
`
  document.head.appendChild(style)
}

export function initLegacyAccountBridge() {
  const cta = document.getElementById('onboardAddAccountCta')
  const host = document.getElementById('legacyAddAccountModalRoot')
  if (!cta || !host) return

  ensureModalStyles()
  const root = createRoot(host)
  let open = false

  const render = () => {
    root.render(
      <BridgeModal
        isOpen={open}
        onClose={() => {
          open = false
          render()
        }}
        onSuccess={() => {
          window.dispatchEvent(new CustomEvent('tali:legacy-add-account-success'))
        }}
      />,
    )
  }

  cta.addEventListener('click', () => {
    open = true
    render()
  })

  window.addEventListener('tali:legacy-open-add-account', () => {
    open = true
    render()
  })

  render()
}
