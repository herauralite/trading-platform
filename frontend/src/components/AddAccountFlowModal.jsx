import { useEffect, useMemo, useState } from 'react'
import { PUBLIC_API_BETA_CONNECTORS, PUBLIC_API_CONNECTORS, PROP_FIRM_CONNECTORS } from '../addAccountFlow'

const MT5_TOTAL_STEPS = 6

function stepLabel(step) {
  return `Step ${step} of ${MT5_TOTAL_STEPS}`
}

function canAttemptMt5Check(draft) {
  return Boolean(String(draft.external_account_id || '').trim())
}

function mt5StateTone(pairing) {
  if (!pairing) return 'hint'
  if (pairing.bridge_status === 'bridge_required') return 'error-text'
  if (pairing.bridge_status === 'waiting_for_bridge_worker' || pairing.discovery_status === 'account_id_provided') return 'hint success-text'
  return 'hint'
}

function renderMt5DiscoverySummary(pairing) {
  if (!pairing) return 'Create a pairing token to begin trusted MT5 bridge registration.'
  if (pairing.pairing_state === 'no_registered_bridge') return 'No trusted bridge is registered yet. Start by creating a pairing token.'
  if (pairing.pairing_state === 'pairing_token_created' || pairing.pairing_state === 'waiting_for_bridge_registration') return 'Pairing token created. Waiting for the MT5 bridge worker to register with that token.'
  if (pairing.pairing_state === 'bridge_registered') return 'Trusted bridge is registered. This account is ready for future MT5 discovery integration.'
  return pairing.message || 'Discovery status is available.'
}

function mt5ProviderState(pairing) {
  if (!pairing) return 'bridge_required'
  if (pairing.bridge_status === 'bridge_registered') return 'ready_for_account_attach'
  if (pairing.bridge_status === 'waiting_for_bridge_registration') return 'waiting_for_registration'
  return 'bridge_required'
}

// Broker metadata for the picker UI
const BROKER_META = {
  fundingpips_prop: {
    name: 'FundingPips',
    tagline: 'Prop Firm',
    badge: 'PROP FIRM',
    badgeColor: '#1a6b3a',
    badgeBg: 'rgba(34,197,94,0.12)',
    accent: '#22c55e',
    portalBg: '#0d1a11',
    portalBorder: 'rgba(34,197,94,0.2)',
    logo: (
      <svg width="36" height="36" viewBox="0 0 36 36" fill="none">
        <rect width="36" height="36" rx="8" fill="#0d1a11"/>
        <text x="18" y="24" textAnchor="middle" fontSize="16" fontWeight="800" fill="#22c55e" fontFamily="Inter, sans-serif">FP</text>
      </svg>
    ),
  },
  tradelocker_api: {
    name: 'TradeLocker',
    tagline: 'API Connection',
    badge: 'LIVE',
    badgeColor: '#2AABEE',
    badgeBg: 'rgba(42,171,238,0.12)',
    accent: '#2AABEE',
    portalBg: '#0d1520',
    portalBorder: 'rgba(42,171,238,0.2)',
    logo: (
      <svg width="36" height="36" viewBox="0 0 36 36" fill="none">
        <rect width="36" height="36" rx="8" fill="#0d1520"/>
        <text x="18" y="24" textAnchor="middle" fontSize="14" fontWeight="800" fill="#2AABEE" fontFamily="Inter, sans-serif">TL</text>
      </svg>
    ),
  },
  mt5_bridge: {
    name: 'MetaTrader 5',
    tagline: 'MT5 Bridge',
    badge: 'BRIDGE',
    badgeColor: '#a78bfa',
    badgeBg: 'rgba(167,139,250,0.12)',
    accent: '#a78bfa',
    portalBg: '#110d1a',
    portalBorder: 'rgba(167,139,250,0.2)',
    logo: (
      <svg width="36" height="36" viewBox="0 0 36 36" fill="none">
        <rect width="36" height="36" rx="8" fill="#110d1a"/>
        <text x="18" y="24" textAnchor="middle" fontSize="13" fontWeight="800" fill="#a78bfa" fontFamily="Inter, sans-serif">MT5</text>
      </svg>
    ),
  },
  alpaca_api: {
    name: 'Alpaca',
    tagline: 'API Connection',
    badge: 'BETA',
    badgeColor: '#f59e0b',
    badgeBg: 'rgba(245,158,11,0.12)',
    accent: '#f59e0b',
    portalBg: '#1a1500',
    portalBorder: 'rgba(245,158,11,0.2)',
    logo: (
      <svg width="36" height="36" viewBox="0 0 36 36" fill="none">
        <rect width="36" height="36" rx="8" fill="#1a1500"/>
        <text x="18" y="24" textAnchor="middle" fontSize="13" fontWeight="800" fill="#f59e0b" fontFamily="Inter, sans-serif">ALP</text>
      </svg>
    ),
  },
  oanda_api: {
    name: 'OANDA',
    tagline: 'API Connection',
    badge: 'BETA',
    badgeColor: '#f59e0b',
    badgeBg: 'rgba(245,158,11,0.12)',
    accent: '#f59e0b',
    portalBg: '#1a1500',
    portalBorder: 'rgba(245,158,11,0.2)',
    logo: (
      <svg width="36" height="36" viewBox="0 0 36 36" fill="none">
        <rect width="36" height="36" rx="8" fill="#1a1500"/>
        <text x="18" y="24" textAnchor="middle" fontSize="11" fontWeight="800" fill="#f59e0b" fontFamily="Inter, sans-serif">OAN</text>
      </svg>
    ),
  },
  binance_api: {
    name: 'Binance',
    tagline: 'API Connection',
    badge: 'BETA',
    badgeColor: '#f59e0b',
    badgeBg: 'rgba(245,158,11,0.12)',
    accent: '#f59e0b',
    portalBg: '#1a1500',
    portalBorder: 'rgba(245,158,11,0.2)',
    logo: (
      <svg width="36" height="36" viewBox="0 0 36 36" fill="none">
        <rect width="36" height="36" rx="8" fill="#1a1500"/>
        <text x="18" y="24" textAnchor="middle" fontSize="11" fontWeight="800" fill="#f59e0b" fontFamily="Inter, sans-serif">BNB</text>
      </svg>
    ),
  },
  tradingview_webhook: {
    name: 'TradingView',
    tagline: 'Webhook Signals',
    badge: 'SIGNALS',
    badgeColor: '#2AABEE',
    badgeBg: 'rgba(42,171,238,0.12)',
    accent: '#2AABEE',
    portalBg: '#0d1520',
    portalBorder: 'rgba(42,171,238,0.2)',
    logo: (
      <svg width="36" height="36" viewBox="0 0 36 36" fill="none">
        <rect width="36" height="36" rx="8" fill="#0d1520"/>
        <text x="18" y="24" textAnchor="middle" fontSize="11" fontWeight="800" fill="#2AABEE" fontFamily="Inter, sans-serif">TV</text>
      </svg>
    ),
  },
}

// Broker card picker — step 1
function BrokerPicker({ providers, selectedProviderType, setSelectedProviderType }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      {providers.map((provider) => {
        const meta = BROKER_META[provider.connectorType] || {}
        const isSelected = provider.connectorType === selectedProviderType
        return (
          <button
            key={provider.connectorType}
            type="button"
            onClick={() => setSelectedProviderType(provider.connectorType)}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 14,
              padding: '14px 16px',
              background: isSelected ? 'rgba(42,171,238,0.07)' : 'rgba(255,255,255,0.03)',
              border: isSelected ? `1px solid ${meta.accent || '#2AABEE'}` : '1px solid rgba(255,255,255,0.07)',
              borderRadius: 12,
              cursor: 'pointer',
              textAlign: 'left',
              transition: 'all 0.15s',
              width: '100%',
            }}
          >
            {meta.logo || null}
            <div style={{ flex: 1 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 2 }}>
                <span style={{ fontWeight: 700, fontSize: 15, color: '#e8f0ff' }}>{meta.name || provider.title}</span>
                <span style={{
                  fontSize: 10,
                  fontWeight: 700,
                  letterSpacing: '0.8px',
                  padding: '2px 7px',
                  borderRadius: 99,
                  color: meta.badgeColor || '#2AABEE',
                  background: meta.badgeBg || 'rgba(42,171,238,0.12)',
                }}>
                  {meta.badge || provider.badge}
                </span>
              </div>
              <span style={{ fontSize: 13, color: 'rgba(148,163,184,0.75)' }}>{meta.tagline || provider.description}</span>
            </div>
            {isSelected && (
              <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
                <circle cx="9" cy="9" r="8" stroke="#2AABEE" strokeWidth="1.5"/>
                <path d="M5.5 9l2.5 2.5L12.5 6" stroke="#2AABEE" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"/>
              </svg>
            )}
          </button>
        )
      })}
    </div>
  )
}

// Branded portal wrapper for credentials steps
function BrokerPortal({ connectorType, children }) {
  const meta = BROKER_META[connectorType] || {}
  return (
    <div style={{
      background: meta.portalBg || '#0d1520',
      border: `1px solid ${meta.portalBorder || 'rgba(42,171,238,0.2)'}`,
      borderRadius: 16,
      padding: '24px 20px',
      marginTop: 4,
    }}>
      {/* Portal header */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 20, paddingBottom: 16, borderBottom: `1px solid ${meta.portalBorder || 'rgba(42,171,238,0.1)'}` }}>
        {meta.logo}
        <div>
          <div style={{ fontWeight: 700, fontSize: 15, color: '#e8f0ff' }}>{meta.name}</div>
          <div style={{ fontSize: 12, color: 'rgba(148,163,184,0.7)', marginTop: 1 }}>Secure login portal</div>
        </div>
        <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 5 }}>
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
            <path d="M3 5V4a3 3 0 016 0v1" stroke={meta.accent || '#2AABEE'} strokeWidth="1.3" strokeLinecap="round"/>
            <rect x="1.5" y="5" width="9" height="6" rx="1.5" fill={meta.accent || '#2AABEE'} opacity="0.15" stroke={meta.accent || '#2AABEE'} strokeWidth="1.2"/>
          </svg>
          <span style={{ fontSize: 11, color: meta.accent || '#2AABEE', fontWeight: 600 }}>Encrypted</span>
        </div>
      </div>
      {children}
    </div>
  )
}

// FundingPips Prop Firm Flow
function FundingPipsPropFlow({ draft, setDraft, discoveredAccounts, isDiscovering, discoverError }) {
  return (
    <BrokerPortal connectorType="fundingpips_prop">
      <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
        <p className="hint" style={{ marginBottom: 4 }}>
          Enter your FundingPips portal credentials. TaliTrade will authenticate on your behalf, discover all your funded accounts, and extract connection details automatically.
        </p>

        <input
          type="email"
          placeholder="Email address"
          value={draft.email || ''}
          onChange={(e) => setDraft((prev) => ({ ...prev, email: e.target.value }))}
          autoComplete="email"
          required
          style={{ background: 'rgba(255,255,255,0.05)', border: '1px solid rgba(34,197,94,0.2)', borderRadius: 8, padding: '11px 14px', color: '#e8f0ff', fontSize: 14, outline: 'none', width: '100%' }}
        />
        <input
          type="password"
          placeholder="Password"
          value={draft.password || ''}
          onChange={(e) => setDraft((prev) => ({ ...prev, password: e.target.value }))}
          autoComplete="current-password"
          required
          style={{ background: 'rgba(255,255,255,0.05)', border: '1px solid rgba(34,197,94,0.2)', borderRadius: 8, padding: '11px 14px', color: '#e8f0ff', fontSize: 14, outline: 'none', width: '100%' }}
        />
        <input
          placeholder="Label (optional — e.g. My $100K Account)"
          value={draft.display_label || ''}
          onChange={(e) => setDraft((prev) => ({ ...prev, display_label: e.target.value }))}
          style={{ background: 'rgba(255,255,255,0.05)', border: '1px solid rgba(34,197,94,0.2)', borderRadius: 8, padding: '11px 14px', color: '#e8f0ff', fontSize: 14, outline: 'none', width: '100%' }}
        />

        <p className="hint" style={{ fontSize: 11, color: 'rgba(148,163,184,0.55)', marginTop: 2 }}>
          Your credentials are encrypted server-side and never stored in plain text.
        </p>

        {isDiscovering ? (
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '10px 0' }}>
            <span className="discovering-spinner" />
            <span className="hint">Authenticating and discovering accounts…</span>
          </div>
        ) : null}

        {discoverError ? <p className="error-text">{discoverError}</p> : null}

        {discoveredAccounts && discoveredAccounts.length > 0 ? (
          <div className="discovered-accounts">
            <p className="hint success-text">
              ✔ {discoveredAccounts.length} account{discoveredAccounts.length !== 1 ? 's' : ''} discovered
            </p>
            <div className="meta-grid">
              {discoveredAccounts.map((acct) => (
                <div key={acct.external_account_id} className="meta-card">
                  <span className="hint">{acct.display_label}</span>
                  <strong>{acct.external_account_id}</strong>
                  {acct.account_type ? <span className="hint">{acct.account_type}</span> : null}
                  {acct.account_size ? <span className="hint">${acct.account_size.toLocaleString()}</span> : null}
                </div>
              ))}
            </div>
          </div>
        ) : null}
      </div>
    </BrokerPortal>
  )
}

// Main Modal
function AddAccountFlowModal({
  isOpen,
  providers,
  selectedProviderType,
  setSelectedProviderType,
  draft,
  setDraft,
  onClose,
  onSubmit,
  onCheckMt5Pairing,
  onCreateMt5PairingToken,
  onLoadMt5RegistrationStatus,
  isSubmitting,
  error,
}) {
  const [mt5Step, setMt5Step] = useState(1)
  const [mt5Pairing, setMt5Pairing] = useState(null)
  const [mt5TokenInfo, setMt5TokenInfo] = useState(null)
  const [mt5RegistrationStatus, setMt5RegistrationStatus] = useState(null)
  const [mt5PairingError, setMt5PairingError] = useState('')
  const [mt5IsChecking, setMt5IsChecking] = useState(false)
  const [mt5IsCreatingToken, setMt5IsCreatingToken] = useState(false)
  const [copiedField, setCopiedField] = useState('')

  const [discoveredAccounts, setDiscoveredAccounts] = useState(null)
  const [isDiscovering, setIsDiscovering] = useState(false)
  const [discoverError, setDiscoverError] = useState('')

  // Filter out legacy/non-broker connectors
  const brokerProviders = providers.filter(
    (p) => !['fundingpips_extension', 'csv_import', 'manual'].includes(p.connectorType)
  )

  const selectedProvider = brokerProviders.find((p) => p.connectorType === selectedProviderType) || null
  const isMt5 = selectedProvider?.connectorType === 'mt5_bridge'
  const isFundingPipsProp = selectedProvider?.connectorType === 'fundingpips_prop'

  useEffect(() => {
    if (!isOpen) return
    setMt5Step(1)
    setMt5Pairing(null)
    setMt5TokenInfo(null)
    setMt5RegistrationStatus(null)
    setMt5PairingError('')
    setMt5IsChecking(false)
    setMt5IsCreatingToken(false)
    setCopiedField('')
    setDiscoveredAccounts(null)
    setIsDiscovering(false)
    setDiscoverError('')
  }, [isOpen, selectedProviderType])

  const canGoToMt5Confirm = useMemo(() => {
    if (!mt5Pairing) return false
    return Boolean(String(draft.external_account_id || '').trim())
  }, [mt5Pairing, draft.external_account_id])

  const canSubmitPropFirm = useMemo(() => {
    if (!isFundingPipsProp) return true
    return Boolean((draft.email || '').trim() && (draft.password || '').trim())
  }, [isFundingPipsProp, draft.email, draft.password])

  if (!isOpen) return null

  async function runMt5PairingCheck() {
    if (!canAttemptMt5Check(draft)) {
      setMt5PairingError('Account ID is required before bridge/discovery check.')
      return
    }
    setMt5IsChecking(true)
    setMt5PairingError('')
    try {
      const payload = await onCheckMt5Pairing(draft)
      setMt5Pairing(payload)
      setDraft((prev) => ({ ...prev, provider_state: mt5ProviderState(payload) }))
      setMt5RegistrationStatus(payload?.trusted_registration || null)
      setMt5Step(4)
    } catch (checkError) {
      setMt5Pairing(null)
      setMt5PairingError(checkError?.message || 'Could not run MT5 bridge discovery check.')
      setMt5Step(4)
    } finally {
      setMt5IsChecking(false)
    }
  }

  async function createPairingToken() {
    if (!canAttemptMt5Check(draft)) {
      setMt5PairingError('Account ID is required before creating a pairing token.')
      return
    }
    setMt5PairingError('')
    setMt5IsCreatingToken(true)
    try {
      const payload = await onCreateMt5PairingToken(draft)
      setMt5TokenInfo(payload?.pairing || null)
      setMt5RegistrationStatus(payload?.registration || null)
      const pairing = await onCheckMt5Pairing(draft)
      setMt5Pairing(pairing)
      setDraft((prev) => ({ ...prev, provider_state: mt5ProviderState(pairing) }))
      setMt5Step(4)
    } catch (createError) {
      setMt5PairingError(createError?.message || 'Could not create MT5 pairing token.')
    } finally {
      setMt5IsCreatingToken(false)
    }
  }

  async function refreshRegistrationStatus() {
    setMt5PairingError('')
    try {
      const payload = await onLoadMt5RegistrationStatus()
      setMt5RegistrationStatus(payload?.registration || null)
      setMt5Pairing(payload?.pairing || null)
      setDraft((prev) => ({ ...prev, provider_state: mt5ProviderState(payload?.pairing || null) }))
      setMt5Step(4)
    } catch (loadError) {
      setMt5PairingError(loadError?.message || 'Could not refresh MT5 bridge registration state.')
    }
  }

  async function submitCurrentProvider() {
    if (isFundingPipsProp && !discoveredAccounts) {
      setIsDiscovering(true)
      setDiscoverError('')
      try {
        const result = await onSubmit(selectedProvider, { preview: false })
        if (result?.accounts) {
          setDiscoveredAccounts(result.accounts)
        }
      } catch (err) {
        setDiscoverError(err?.message || 'Could not connect to FundingPips. Check your credentials.')
        setIsDiscovering(false)
        return
      }
      setIsDiscovering(false)
      return
    }
    await onSubmit(selectedProvider)
  }

  async function copyValue(value, key) {
    if (!value) return
    try {
      await navigator.clipboard.writeText(value)
      setCopiedField(key)
      window.setTimeout(() => setCopiedField(''), 1500)
    } catch {
      setCopiedField('')
    }
  }

  function submitLabel() {
    if (isSubmitting || isDiscovering) return 'Working…'
    if (isFundingPipsProp) {
      if (discoveredAccounts && discoveredAccounts.length > 0) return 'Confirm and add accounts'
      return 'Connect FundingPips'
    }
    if (selectedProvider?.connectorType === 'tradingview_webhook' && draft.tradingview_webhook_url) {
      return 'Finish and return to Accounts'
    }
    return selectedProvider?.ctaLabel || 'Connect'
  }

  return (
    <div className="modal-backdrop" role="presentation">
      <section className="modal-panel" role="dialog" aria-modal="true" aria-label="Add account">
        <div className="row modal-header-row">
          <div>
            <h2>Add Account</h2>
            <p className="hint">Select your broker or prop firm to get started.</p>
          </div>
          <button type="button" className="secondary-button" onClick={onClose}>Close</button>
        </div>

        {/* Step 1 — Broker picker */}
        <BrokerPicker
          providers={brokerProviders}
          selectedProviderType={selectedProviderType}
          setSelectedProviderType={setSelectedProviderType}
        />

        {/* Step 2 — Selected broker flow */}
        {selectedProvider ? (
          <form
            className="card add-account-form"
            style={{ marginTop: 16 }}
            onSubmit={(event) => {
              event.preventDefault()
              void submitCurrentProvider()
            }}
          >
            {/* FundingPips Prop Firm */}
            {isFundingPipsProp ? (
              <FundingPipsPropFlow
                draft={draft}
                setDraft={setDraft}
                discoveredAccounts={discoveredAccounts}
                isDiscovering={isDiscovering}
                discoverError={discoverError}
              />
            ) : null}

            {/* MT5 Bridge Flow — unchanged */}
            {isMt5 ? (
              <BrokerPortal connectorType="mt5_bridge">
                <p className="hint mt5-step-label">{stepLabel(mt5Step)}</p>

                {mt5Step === 1 ? (
                  <div className="mt5-wizard-block">
                    <p className="hint">MT5 pairing links this app account to your MT5 bridge worker. Live bridge execution remains intentionally gated until bridge workers are active.</p>
                    <ul>
                      <li>Enter account + MT5 server details.</li>
                      <li>Create a pairing token from backend.</li>
                      <li>Wait for trusted bridge worker registration, then confirm add account.</li>
                    </ul>
                    <div className="row">
                      <button type="button" onClick={() => setMt5Step(2)}>Start MT5 pairing</button>
                    </div>
                  </div>
                ) : null}

                {mt5Step === 2 ? (
                  <div className="mt5-wizard-block">
                    <p className="hint">Enter MT5 connection info used by your bridge setup.</p>
                    <div className="row">
                      <input placeholder="Account ID" value={draft.external_account_id} onChange={(event) => setDraft((prev) => ({ ...prev, external_account_id: event.target.value }))} required />
                      <input placeholder="Display label" value={draft.display_label} onChange={(event) => setDraft((prev) => ({ ...prev, display_label: event.target.value }))} />
                    </div>
                    <div className="row">
                      <input placeholder="MT5 server (optional but recommended)" value={draft.mt5_server} onChange={(event) => setDraft((prev) => ({ ...prev, mt5_server: event.target.value }))} />
                      <input placeholder="Bridge URL (optional metadata only)" value={draft.bridge_url} onChange={(event) => setDraft((prev) => ({ ...prev, bridge_url: event.target.value }))} />
                    </div>
                    <div className="row">
                      <button type="button" className="secondary-button" onClick={() => setMt5Step(1)}>Back</button>
                      <button type="button" onClick={() => setMt5Step(3)}>Continue</button>
                    </div>
                  </div>
                ) : null}

                {mt5Step === 3 ? (
                  <div className="mt5-wizard-block">
                    <p className="hint">Create a backend pairing token and keep this window open while your MT5 bridge worker registers itself.</p>
                    <div className="row">
                      <button type="button" className="secondary-button" onClick={() => setMt5Step(2)}>Back</button>
                      <button type="button" onClick={() => void createPairingToken()} disabled={mt5IsCreatingToken || !canAttemptMt5Check(draft)}>
                        {mt5IsCreatingToken ? 'Creating token…' : 'Create pairing token'}
                      </button>
                    </div>
                  </div>
                ) : null}

                {mt5Step === 4 ? (
                  <div className="mt5-wizard-block">
                    <p className={mt5StateTone(mt5Pairing)}>{renderMt5DiscoverySummary(mt5Pairing)}</p>
                    {mt5Pairing ? (
                      <div className="meta-grid">
                        <div className="meta-card"><span className="hint">Bridge state</span><strong>{mt5Pairing.bridge_status}</strong></div>
                        <div className="meta-card"><span className="hint">Pairing state</span><strong>{mt5Pairing.pairing_state || 'unknown'}</strong></div>
                        <div className="meta-card"><span className="hint">Discovery state</span><strong>{mt5Pairing.discovery_status}</strong></div>
                        <div className="meta-card"><span className="hint">Implementation mode</span><strong>{mt5Pairing.implementation_mode || 'unknown'}</strong></div>
                        <div className="meta-card"><span className="hint">Add allowed</span><strong>{mt5Pairing.can_add_account ? 'yes' : 'no'}</strong></div>
                      </div>
                    ) : null}
                    {mt5Pairing?.registration ? (
                      <div className="meta-grid">
                        <div className="meta-card"><span className="hint">Bridge URL provided</span><strong>{mt5Pairing.registration.bridge_url_provided ? 'yes' : 'no'}</strong></div>
                        <div className="meta-card"><span className="hint">MT5 server provided</span><strong>{mt5Pairing.registration.mt5_server_provided ? 'yes' : 'no'}</strong></div>
                        <div className="meta-card"><span className="hint">Bridge ID linked</span><strong>{mt5Pairing.registration.bridge_id_provided ? 'yes' : 'no'}</strong></div>
                        <div className="meta-card"><span className="hint">Pairing token linked</span><strong>{mt5Pairing.registration.pairing_token_provided ? 'yes' : 'no'}</strong></div>
                      </div>
                    ) : null}
                    {mt5TokenInfo ? (
                      <div className="meta-grid">
                        <div className="meta-card"><span className="hint">Pairing token</span><strong className="mono">{mt5TokenInfo.pairing_token || '—'}</strong></div>
                        <div className="meta-card"><span className="hint">Token expires</span><strong>{mt5TokenInfo.expires_at || '—'}</strong></div>
                      </div>
                    ) : null}
                    {mt5RegistrationStatus?.bridges?.length ? (
                      <div className="meta-grid">
                        <div className="meta-card"><span className="hint">Registered bridges</span><strong>{mt5RegistrationStatus.bridges.length}</strong></div>
                        <div className="meta-card"><span className="hint">Latest bridge</span><strong>{mt5RegistrationStatus.bridges[0]?.display_name || mt5RegistrationStatus.bridges[0]?.bridge_id || '—'}</strong></div>
                      </div>
                    ) : null}
                    {mt5PairingError ? <p className="error-text">{mt5PairingError}</p> : null}
                    <div className="row">
                      <button type="button" className="secondary-button" onClick={() => setMt5Step(2)}>Edit info</button>
                      <button type="button" className="secondary-button" onClick={() => void runMt5PairingCheck()} disabled={mt5IsChecking}>{mt5IsChecking ? 'Refreshing…' : 'Refresh pairing state'}</button>
                      <button type="button" className="secondary-button" onClick={() => void refreshRegistrationStatus()}>Load bridge status</button>
                      <button type="button" onClick={() => setMt5Step(5)} disabled={!canGoToMt5Confirm}>Continue</button>
                    </div>
                  </div>
                ) : null}

                {mt5Step === 5 ? (
                  <div className="mt5-wizard-block">
                    <p className="hint">Confirm add MT5 account. If registration is still pending, the account remains in waiting mode until trusted bridge worker heartbeat is seen.</p>
                    <div className="meta-grid">
                      <div className="meta-card"><span className="hint">Account ID</span><strong>{draft.external_account_id || '—'}</strong></div>
                      <div className="meta-card"><span className="hint">Display label</span><strong>{draft.display_label || selectedProvider.title}</strong></div>
                      <div className="meta-card"><span className="hint">Bridge URL</span><strong>{draft.bridge_url || 'Not provided'}</strong></div>
                      <div className="meta-card"><span className="hint">MT5 server</span><strong>{draft.mt5_server || 'Not provided'}</strong></div>
                    </div>
                    <div className="row">
                      <button type="button" className="secondary-button" onClick={() => setMt5Step(4)}>Back</button>
                      <button type="submit" disabled={isSubmitting}>{isSubmitting ? 'Adding MT5 account…' : 'Confirm and add MT5 account'}</button>
                    </div>
                  </div>
                ) : null}

                <p className="hint">Step 6 happens after confirm: you are returned to <span className="mono">/app/accounts</span> with success focus.</p>
              </BrokerPortal>
            ) : null}

            {/* TradingView Webhook */}
            {selectedProvider.connectorType === 'tradingview_webhook' ? (
              <BrokerPortal connectorType="tradingview_webhook">
                <p className="hint">Create a TradingView webhook connection. Paste this webhook URL into your TradingView alert. Status stays <strong>Awaiting first alert</strong> until a real event arrives.</p>
                <div className="row">
                  <input placeholder="Connection label" value={draft.display_label} onChange={(event) => setDraft((prev) => ({ ...prev, display_label: event.target.value }))} required />
                  <input placeholder="Optional account alias" value={draft.account_alias || ''} onChange={(event) => setDraft((prev) => ({ ...prev, account_alias: event.target.value }))} />
                </div>
                {draft.tradingview_webhook_url ? (
                  <div className="meta-grid">
                    <div className="meta-card">
                      <span className="hint">Webhook URL</span>
                      <strong className="mono">{draft.tradingview_webhook_url}</strong>
                      <button type="button" className="secondary-button" onClick={() => void copyValue(draft.tradingview_webhook_url, 'webhook')}>Copy URL</button>
                    </div>
                    <div className="meta-card">
                      <span className="hint">Secret hint</span>
                      <strong className="mono">{draft.tradingview_secret_hint || '—'}</strong>
                      <button type="button" className="secondary-button" onClick={() => void copyValue(draft.tradingview_secret_hint, 'secret')}>Copy hint</button>
                    </div>
                  </div>
                ) : null}
                {draft.tradingview_webhook_url ? <p className="hint">Connection becomes active after your first valid alert.</p> : null}
                {copiedField ? <p className="hint success-text">Copied.</p> : null}
              </BrokerPortal>
            ) : null}

            {/* Public API Connectors (Alpaca, TradeLocker) */}
            {PUBLIC_API_CONNECTORS.includes(selectedProvider.connectorType) ? (
              <BrokerPortal connectorType={selectedProvider.connectorType}>
                <p className="hint">
                  {selectedProvider.connectorType === 'alpaca_api'
                    ? 'Connect read-only Alpaca API credentials. Credentials are validated server-side and never echoed back.'
                    : 'Connect TradeLocker API credentials. Credentials are validated server-side and secrets are never echoed back.'}
                </p>
                <div className="row">
                  <input placeholder="Account label" value={draft.display_label} onChange={(event) => setDraft((prev) => ({ ...prev, display_label: event.target.value }))} required />
                  <select value={draft.environment || 'paper'} onChange={(event) => setDraft((prev) => ({ ...prev, environment: event.target.value }))}>
                    <option value="paper">Paper</option>
                    <option value="live">Live</option>
                  </select>
                </div>
                {selectedProvider.connectorType === 'alpaca_api' ? (
                  <div className="row">
                    <input placeholder="API key" value={draft.api_key || ''} onChange={(event) => setDraft((prev) => ({ ...prev, api_key: event.target.value }))} required />
                    <input type="password" placeholder="API secret" value={draft.api_secret || ''} onChange={(event) => setDraft((prev) => ({ ...prev, api_secret: event.target.value }))} required />
                  </div>
                ) : (
                  <>
                    <div className="row">
                      <input placeholder="Base URL" value={draft.base_url || ''} onChange={(event) => setDraft((prev) => ({ ...prev, base_url: event.target.value }))} required />
                      <input placeholder="Account ID" value={draft.account_id || ''} onChange={(event) => setDraft((prev) => ({ ...prev, account_id: event.target.value }))} required />
                    </div>
                    <div className="row">
                      <input placeholder="Email" value={draft.email || ''} onChange={(event) => setDraft((prev) => ({ ...prev, email: event.target.value }))} required />
                      <input type="password" placeholder="Password" value={draft.password || ''} onChange={(event) => setDraft((prev) => ({ ...prev, password: event.target.value }))} required />
                    </div>
                    <div className="row">
                      <input placeholder="Server (optional)" value={draft.server || ''} onChange={(event) => setDraft((prev) => ({ ...prev, server: event.target.value }))} />
                    </div>
                  </>
                )}
              </BrokerPortal>
            ) : null}

            {/* Beta Connectors */}
            {PUBLIC_API_BETA_CONNECTORS.includes(selectedProvider.connectorType) ? (
              <BrokerPortal connectorType={selectedProvider.connectorType}>
                <p className="hint">Register this provider for beta onboarding. We only save safe metadata in this slice.</p>
                <div className="row">
                  <input placeholder="Display name" value={draft.display_label} onChange={(event) => setDraft((prev) => ({ ...prev, display_label: event.target.value }))} required />
                  <select value={draft.environment || 'paper'} onChange={(event) => setDraft((prev) => ({ ...prev, environment: event.target.value }))}>
                    <option value="paper">Paper</option>
                    <option value="live">Live (metadata only)</option>
                  </select>
                  <input placeholder="Optional account alias" value={draft.account_alias || ''} onChange={(event) => setDraft((prev) => ({ ...prev, account_alias: event.target.value }))} />
                </div>
                <p className="hint">End state: <strong>Awaiting secure auth</strong>. No live broker connectivity is claimed yet.</p>
              </BrokerPortal>
            ) : null}

            {error ? <p className="error-text">{error}</p> : null}

            {!isMt5 ? (
              <div className="row" style={{ marginTop: 16 }}>
                <button
                  type="submit"
                  disabled={isSubmitting || isDiscovering || (isFundingPipsProp && !canSubmitPropFirm)}
                >
                  {submitLabel()}
                </button>
              </div>
            ) : null}
          </form>
        ) : null}
      </section>
    </div>
  )
}

export default AddAccountFlowModal
