function AddAccountFlowModal({
  isOpen,
  providers,
  selectedProviderType,
  setSelectedProviderType,
  draft,
  setDraft,
  onClose,
  onSubmit,
  isSubmitting,
  error,
}) {
  if (!isOpen) return null

  const selectedProvider = providers.find((provider) => provider.connectorType === selectedProviderType) || null

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
              onSubmit(selectedProvider)
            }}
          >
            <h3>{selectedProvider.title}</h3>

            {selectedProvider.connectorType === 'mt5_bridge' ? (
              <>
                <p className="hint">Pair your MT5 account by entering a label and bridge details used by your MT5 bridge setup.</p>
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
                    placeholder="MT5 server (optional)"
                    value={draft.mt5_server}
                    onChange={(event) => setDraft((prev) => ({ ...prev, mt5_server: event.target.value }))}
                  />
                  <input
                    placeholder="Bridge URL (optional)"
                    value={draft.bridge_url}
                    onChange={(event) => setDraft((prev) => ({ ...prev, bridge_url: event.target.value }))}
                  />
                </div>
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
            <div className="row">
              <button type="submit" disabled={isSubmitting}>{isSubmitting ? 'Working…' : selectedProvider.ctaLabel}</button>
            </div>
          </form>
        ) : null}
      </section>
    </div>
  )
}

export default AddAccountFlowModal
