import { useEffect, useMemo, useState } from 'react'

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

  const selectedProvider = providers.find((provider) => provider.connectorType === selectedProviderType) || null
  const isMt5 = selectedProvider?.connectorType === 'mt5_bridge'

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
  }, [isOpen, selectedProviderType])

  const canGoToMt5Confirm = useMemo(() => {
    if (!mt5Pairing) return false
    return Boolean(String(draft.external_account_id || '').trim())
  }, [mt5Pairing, draft.external_account_id])

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

  return (
    <div className="modal-backdrop" role="presentation">
      <section className="modal-panel" role="dialog" aria-modal="true" aria-label="Add account">
        <div className="row modal-header-row">
          <div>
            <h2>Add Account</h2>
            <p className="hint">Choose a broker/platform, complete the matching flow, and continue in Accounts.</p>
          </div>
          <button type="button" className="secondary-button" onClick={onClose}>Close</button>
        </div>

        <div className="provider-grid">
          {providers.map((provider) => {
            const isSelected = provider.connectorType === selectedProviderType
            return (
              <button
                key={provider.connectorType}
                type="button"
                className={`provider-card${isSelected ? ' selected' : ''}`}
                onClick={() => setSelectedProviderType(provider.connectorType)}
              >
                <div className="row">
                  <strong>{provider.title}</strong>
                  <span className="pill">{provider.badge}</span>
                </div>
                <p className="hint">{provider.description}</p>
                {provider.notes ? <p className="hint">Catalog note: {provider.notes}</p> : null}
              </button>
            )
          })}
        </div>

        {selectedProvider ? (
          <form
            className="card add-account-form"
            onSubmit={(event) => {
              event.preventDefault()
              void submitCurrentProvider()
            }}
          >
            <h3>{selectedProvider.title}</h3>

            {isMt5 ? (
              <>
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
                      <input
                        placeholder="Account ID"
                        value={draft.external_account_id}
                        onChange={(event) => setDraft((prev) => ({ ...prev, external_account_id: event.target.value }))}
                        required
                      />
                      <input
                        placeholder="Display label"
                        value={draft.display_label}
                        onChange={(event) => setDraft((prev) => ({ ...prev, display_label: event.target.value }))}
                      />
                    </div>
                    <div className="row">
                      <input
                        placeholder="MT5 server (optional but recommended)"
                        value={draft.mt5_server}
                        onChange={(event) => setDraft((prev) => ({ ...prev, mt5_server: event.target.value }))}
                      />
                      <input
                        placeholder="Bridge URL (optional metadata only)"
                        value={draft.bridge_url}
                        onChange={(event) => setDraft((prev) => ({ ...prev, bridge_url: event.target.value }))}
                      />
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
                      <button type="button" className="secondary-button" onClick={() => void runMt5PairingCheck()} disabled={mt5IsChecking}>
                        {mt5IsChecking ? 'Refreshing…' : 'Refresh pairing state'}
                      </button>
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
              </>
            ) : null}

            {selectedProvider.connectorType === 'fundingpips_extension' ? (
              <>
                <p className="hint">Connect a FundingPips account with the existing extension connector behavior.</p>
                <div className="row">
                  <input
                    placeholder="Account ID"
                    value={draft.external_account_id}
                    onChange={(event) => setDraft((prev) => ({ ...prev, external_account_id: event.target.value }))}
                    required
                  />
                  <input
                    placeholder="Display label"
                    value={draft.display_label}
                    onChange={(event) => setDraft((prev) => ({ ...prev, display_label: event.target.value }))}
                  />
                </div>
              </>
            ) : null}

            {selectedProvider.connectorType === 'tradingview_webhook' ? (
              <>
                <p className="hint">Create a TradingView webhook connection. Status stays <strong>Awaiting TradingView alerts</strong> until real alerts arrive.</p>
                <div className="row">
                  <input
                    placeholder="Connection label"
                    value={draft.display_label}
                    onChange={(event) => setDraft((prev) => ({ ...prev, display_label: event.target.value }))}
                    required
                  />
                  <input
                    placeholder="Optional account alias"
                    value={draft.account_alias || ''}
                    onChange={(event) => setDraft((prev) => ({ ...prev, account_alias: event.target.value }))}
                  />
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
                {copiedField ? <p className="hint success-text">Copied.</p> : null}
              </>
            ) : null}

            {['alpaca_api', 'oanda_api', 'binance_api'].includes(selectedProvider.connectorType) ? (
              <>
                <p className="hint">Register this provider for beta onboarding. We only save safe metadata in this slice.</p>
                <div className="row">
                  <input
                    placeholder="Display name"
                    value={draft.display_label}
                    onChange={(event) => setDraft((prev) => ({ ...prev, display_label: event.target.value }))}
                    required
                  />
                  <select
                    value={draft.environment || 'paper'}
                    onChange={(event) => setDraft((prev) => ({ ...prev, environment: event.target.value }))}
                  >
                    <option value="paper">Paper</option>
                    <option value="live">Live (metadata only)</option>
                  </select>
                  <input
                    placeholder="Optional account alias"
                    value={draft.account_alias || ''}
                    onChange={(event) => setDraft((prev) => ({ ...prev, account_alias: event.target.value }))}
                  />
                </div>
                <p className="hint">End state: <strong>Waiting for secure auth support</strong>. No live broker connectivity is claimed yet.</p>
              </>
            ) : null}

            {selectedProvider.connectorType === 'csv_import' ? (
              <p className="hint">This sends you to the CSV import tools in Connections where you can paste/import rows.</p>
            ) : null}

            {selectedProvider.connectorType === 'manual' ? (
              <p className="hint">This sends you to Manual Journal tools in Connections to create and journal an account.</p>
            ) : null}

            {error ? <p className="error-text">{error}</p> : null}
            {!isMt5 ? (
              <div className="row">
                <button type="submit" disabled={isSubmitting}>
                  {isSubmitting ? 'Working…' : (selectedProvider.connectorType === 'tradingview_webhook' && draft.tradingview_webhook_url ? 'Finish and return to Accounts' : selectedProvider.ctaLabel)}
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
