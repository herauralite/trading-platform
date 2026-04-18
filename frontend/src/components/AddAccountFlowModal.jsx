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
  if (pairing.bridge_status === 'bridge_not_reachable') return 'error-text'
  if (pairing.discovery_status === 'discovered_account_ready' || pairing.discovery_status === 'discovered_accounts_available') return 'hint success-text'
  return 'hint'
}

function renderMt5DiscoverySummary(pairing) {
  if (!pairing) return 'Run a bridge/discovery check to continue.'
  if (pairing.bridge_status === 'bridge_required') return 'Bridge URL required before discovery can run.'
  if (pairing.bridge_status === 'bridge_not_reachable') return 'Bridge is not reachable from the API right now.'
  if (pairing.discovery_status === 'waiting_for_bridge') return 'Bridge is reachable but discovery endpoint is not ready yet.'
  if (pairing.discovery_status === 'account_not_discovered_yet') return 'Bridge responded but the account is not discovered yet.'
  if (pairing.discovery_status === 'discovered_account_ready') return 'The requested MT5 account is discovered and ready to add.'
  if (pairing.discovery_status === 'discovered_accounts_available') return 'Bridge returned discovered accounts. Pick one to continue.'
  return pairing.message || 'Discovery status is available.'
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
  isSubmitting,
  error,
}) {
  const [mt5Step, setMt5Step] = useState(1)
  const [mt5Pairing, setMt5Pairing] = useState(null)
  const [mt5PairingError, setMt5PairingError] = useState('')
  const [mt5IsChecking, setMt5IsChecking] = useState(false)

  const selectedProvider = providers.find((provider) => provider.connectorType === selectedProviderType) || null
  const isMt5 = selectedProvider?.connectorType === 'mt5_bridge'

  useEffect(() => {
    if (!isOpen) return
    setMt5Step(1)
    setMt5Pairing(null)
    setMt5PairingError('')
    setMt5IsChecking(false)
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
      setMt5Step(4)
    } catch (checkError) {
      setMt5Pairing(null)
      setMt5PairingError(checkError?.message || 'Could not run MT5 bridge discovery check.')
      setMt5Step(4)
    } finally {
      setMt5IsChecking(false)
    }
  }

  async function submitCurrentProvider() {
    await onSubmit(selectedProvider)
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
                      <li>Enter account + bridge connection hints.</li>
                      <li>Run bridge/discovery check.</li>
                      <li>Confirm add account with truthful bridge state.</li>
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
                        placeholder="Bridge URL"
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
                    <p className="hint">Run bridge connectivity + account discovery check before adding this MT5 account.</p>
                    <div className="row">
                      <button type="button" className="secondary-button" onClick={() => setMt5Step(2)}>Back</button>
                      <button type="button" onClick={() => void runMt5PairingCheck()} disabled={mt5IsChecking || !canAttemptMt5Check(draft)}>
                        {mt5IsChecking ? 'Checking bridge…' : 'Check bridge and discovery'}
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
                        <div className="meta-card"><span className="hint">Discovery state</span><strong>{mt5Pairing.discovery_status}</strong></div>
                        <div className="meta-card"><span className="hint">Implementation mode</span><strong>{mt5Pairing.implementation_mode || 'unknown'}</strong></div>
                        <div className="meta-card"><span className="hint">Add allowed</span><strong>{mt5Pairing.can_add_account ? 'yes' : 'no'}</strong></div>
                      </div>
                    ) : null}
                    {mt5Pairing?.discovered_accounts?.length ? (
                      <div>
                        <p className="hint">Discovered accounts:</p>
                        <div className="discovered-list">
                          {mt5Pairing.discovered_accounts.map((account) => (
                            <button
                              key={account.external_account_id}
                              type="button"
                              className="discovered-item"
                              onClick={() => setDraft((prev) => ({
                                ...prev,
                                external_account_id: account.external_account_id,
                                display_label: prev.display_label || account.display_label || prev.display_label,
                              }))}
                            >
                              <strong>{account.display_label || account.external_account_id}</strong>
                              <span className="mono">{account.external_account_id}</span>
                            </button>
                          ))}
                        </div>
                      </div>
                    ) : null}
                    {mt5PairingError ? <p className="error-text">{mt5PairingError}</p> : null}
                    <div className="row">
                      <button type="button" className="secondary-button" onClick={() => setMt5Step(2)}>Edit info</button>
                      <button type="button" className="secondary-button" onClick={() => void runMt5PairingCheck()} disabled={mt5IsChecking}>
                        {mt5IsChecking ? 'Retrying…' : 'Retry discovery'}
                      </button>
                      <button type="button" onClick={() => setMt5Step(5)} disabled={!canGoToMt5Confirm}>Continue</button>
                    </div>
                  </div>
                ) : null}

                {mt5Step === 5 ? (
                  <div className="mt5-wizard-block">
                    <p className="hint">Confirm add MT5 account. If bridge is not live yet, this account is added in bridge-required/pending mode.</p>
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

            {selectedProvider.connectorType === 'csv_import' ? (
              <p className="hint">This sends you to the CSV import tools in Connections where you can paste/import rows.</p>
            ) : null}

            {selectedProvider.connectorType === 'manual' ? (
              <p className="hint">This sends you to Manual Journal tools in Connections to create and journal an account.</p>
            ) : null}

            {error ? <p className="error-text">{error}</p> : null}
            {!isMt5 ? (
              <div className="row">
                <button type="submit" disabled={isSubmitting}>{isSubmitting ? 'Working…' : selectedProvider.ctaLabel}</button>
              </div>
            ) : null}
          </form>
        ) : null}
      </section>
    </div>
  )
}

export default AddAccountFlowModal
