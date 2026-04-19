import { buildConnectorConfigDraft, connectorConfigStateLabel } from '../connectorConfig'
import { formatSyncRunDiagnostics } from '../syncRunDiagnostics'
import { isGuidedAddAccountConnector } from '../addAccountFlow'
import { connectorEnvironmentLabel, deriveConnectorLifecycleState } from '../connectorLifecycleState'

function ConnectionsPage({
  catalog,
  managedConnectors,
  syncHistory,
  configDrafts,
  connectorDrafts,
  signedIn,
  manualAccount,
  manualTrade,
  csvAccount,
  csvInput,
  sourceLabel,
  statusTone,
  formatDate,
  syncStateLabel,
  setConnectorDrafts,
  setConfigDrafts,
  setManualAccount,
  setManualTrade,
  setCsvAccount,
  setCsvInput,
  connectorAction,
  saveConnectorConfig,
  clearConnectorConfig,
  createManualAccount,
  createManualTrade,
  importCsvTrades,
  onAddAccount,
  addFlowIntent,
  isWorkspaceLoading,
}) {
  const connectionMethods = [
    { key: 'mt5_bridge', title: 'MT5', description: 'Pair bridge worker + account metadata, then run sync.', group: 'Core connectors' },
    { key: 'fundingpips_extension', title: 'FundingPips Extension', description: 'Attach extension-backed account and hydrate workspace records.', group: 'Core connectors' },
    { key: 'tradingview_webhook', title: 'TradingView Webhook', description: 'Create webhook endpoint and ingest alert-driven trade events.', group: 'Core connectors' },
    { key: 'csv_import', title: 'CSV Import', description: 'Upload historical trade rows for backfilled account context.', group: 'Utility connectors' },
    { key: 'manual', title: 'Manual Journal', description: 'Create manual accounts and add trades directly from app UI.', group: 'Utility connectors' },
    ...catalog
      .filter((entry) => !['mt5_bridge', 'fundingpips_extension', 'tradingview_webhook', 'csv_import', 'manual'].includes(entry.connector_type))
      .map((entry) => ({
        key: entry.connector_type,
        title: entry.label || sourceLabel(entry.connector_type),
        description: entry.onboarding_copy || entry.notes || 'Public API beta/provider path available from catalog.',
        group: 'Public API beta',
      })),
  ]
  const groupedMethods = connectionMethods.reduce((acc, method) => {
    const group = method.group || 'Other'
    if (!acc[group]) acc[group] = []
    acc[group].push(method)
    return acc
  }, {})

  function findConnector(methodKey) {
    return managedConnectors.find((connector) => connector.connector_type === methodKey) || null
  }

  function connectorActionLabel(connector) {
    if (!connector) return 'not connected'
    if (connector.account_count > 0 && (connector.status === 'connected' || connector.is_connected)) return 'connected'
    if (connector.status === 'bridge_required' || connector.status === 'waiting_for_registration' || connector.status === 'awaiting_secure_auth' || connector.status === 'awaiting_alerts') return 'awaiting setup'
    if (connector.status === 'beta_pending' || connector.beta) return 'beta / bridge required'
    if (connector.status === 'disconnected' || !connector.is_connected) return 'not connected'
    if (connector.status === 'validation_failed' || connector.status === 'sync_error') return 'stale / disconnected'
    return connector.status
  }

  function primaryAction(connector, methodKey) {
    const state = connectorActionLabel(connector)
    if (!signedIn) return { label: 'Sign in to continue', disabled: true, onClick: () => {} }
    if (state === 'connected') return { label: 'View linked accounts', disabled: false, onClick: () => onAddAccount(methodKey) }
    if (state === 'awaiting setup') return { label: 'Continue setup', disabled: false, onClick: () => onAddAccount(methodKey) }
    if (state === 'beta / bridge required') return { label: 'Re-open Add Account flow', disabled: false, onClick: () => onAddAccount(methodKey) }
    return { label: 'Connect', disabled: false, onClick: () => onAddAccount(methodKey) }
  }

  return (
    <>
      <section className="panel page-panel premium-workspace-panel connections-page">
        <div className="panel-header row">
          <div>
            <p className="kicker">Connections</p>
            <h2>Connector operations and sync controls</h2>
          </div>
          <button type="button" className="primary-cta" onClick={() => onAddAccount('mt5_bridge')}>Add Account</button>
        </div>
        <p className="hint">
          Configure providers, run sync actions, and manage connector credentials here. Use <strong>Accounts</strong> for account-centric management and Add Account onboarding.
        </p>
        {!signedIn ? (
          <div className="card premium-auth-helper">
            <strong>Signed out: setup actions are disabled.</strong>
            <p className="hint">Sign in with Telegram in the shell gate to connect providers, run sync jobs, import CSV rows, or write manual journal entries.</p>
          </div>
        ) : null}
        {Object.entries(groupedMethods).map(([groupName, methods]) => (
          <div key={groupName} className="connections-group-section">
            <h3>{groupName}</h3>
            <div className="meta-grid premium-summary-grid connections-method-grid">
          {methods.map((method) => {
            const connector = findConnector(method.key)
            const action = primaryAction(connector, method.key)
            return (
              <div className="meta-card summary-card" key={method.key}>
                <div className="row">
                  <strong>{method.title}</strong>
                  <span className={`badge ${statusTone(connector?.status || 'disconnected')}`}>
                    {connectorActionLabel(connector)}
                  </span>
                </div>
                <p className="hint">{method.description}</p>
                <p className="hint">Next action: {signedIn ? action.label : 'Sign in to begin setup.'}</p>
                <button
                  type="button"
                  className="secondary-button"
                  disabled={action.disabled}
                  onClick={action.onClick}
                >
                  {action.label}
                </button>
              </div>
            )
          })}
            </div>
          </div>
        ))}
        <p className="hint">Connector catalog: {catalog.map((entry) => entry.label).join(', ') || 'No connectors available yet.'}</p>

        {isWorkspaceLoading ? (
          <div className="skeleton-grid">
            <div className="skeleton-card" />
            <div className="skeleton-card" />
          </div>
        ) : null}

        {managedConnectors.map((connector) => (
          <div key={connector.connector_type} className="card connector-card connections-connector-card">
            {(() => {
              const lifecycle = deriveConnectorLifecycleState(connector)
              return (
                <>
            <div className="row">
              <strong>{sourceLabel(connector.connector_type)}</strong>
              <span className={`badge ${statusTone(connector.status)}`}>{connector.status}</span>
              <span className={`badge ${lifecycle.toneClass}`}>{lifecycle.label}</span>
              {connector.integration_status ? <span className="pill">{connector.integration_status}</span> : null}
              {connector.beta ? <span className="pill">beta</span> : null}
            </div>
            {connector.notes ? <p className="hint">{connector.notes}</p> : null}
            {connector.onboarding_copy ? <p className="hint">{connector.onboarding_copy}</p> : null}
            <p className="hint">{lifecycle.helper}</p>
            <div className="meta">
              State: {connector.is_connected ? 'connected' : 'disconnected'} · Accounts: {connector.account_count} · Last activity: {formatDate(connector.last_activity_at)} · Last sync: {formatDate(connector.last_sync_at)}
            </div>
            <div className="meta">
              Provider state: {connector.provider_state || connector.status || 'unknown'} · Last validated: {formatDate(connector.last_validated_at)}
            </div>
            <div className="meta">
              Environment: {connectorEnvironmentLabel(connector)}
            </div>
            <div className="meta">
              Sync: {syncStateLabel(connector.current_sync_state)} · Retries: {connector.current_sync_retry_count || 0} · Next retry: {formatDate(connector.next_retry_at)}
            </div>
            <div className="meta">
              Config: {connectorConfigStateLabel(connector)} {connector.configured_secret_fields?.length ? `· secret fields: ${connector.configured_secret_fields.join(', ')}` : ''}
            </div>
            {connector.config_validation_error ? <p className="error-text">Config issue: {connector.config_validation_error}</p> : null}
            {connector.last_error ? <p className="error-text">Last error: {connector.last_error} ({formatDate(connector.last_error_at)})</p> : null}

            <ul className="connector-account-list">
              {connector.accounts.map((account) => (
                <li key={`${connector.connector_type}-${account.id}`}>
                  <span>{account.display_label || account.external_account_id}</span>
                  <span className="pill">{account.broker_name || 'Broker N/A'}</span>
                  {account.environment ? <span className="pill">{String(account.environment).toUpperCase()}</span> : null}
                  <span className={`badge ${statusTone(account.connection_status)}`}>{account.connection_status || 'disconnected'}</span>
                  {account.last_validated_at ? <span className="hint">Validated {formatDate(account.last_validated_at)}</span> : null}
                  {account.last_sync_at ? <span className="hint">Last sync {formatDate(account.last_sync_at)}</span> : null}
                </li>
              ))}
            </ul>
                </>
              )
            })()}

            <div className="row">
              {!connector.supports_live_sync ? <span className="hint">Sync unavailable for this connector.</span> : null}
              <button
                disabled={!signedIn || !connector.supports_live_sync}
                onClick={() => connectorAction(connector.connector_type, 'sync')}
              >
                Sync
              </button>
              <button
                disabled={!signedIn || connector.account_count > 0 || isGuidedAddAccountConnector(connector.connector_type)}
                onClick={() => connectorAction(connector.connector_type, 'connect', {
                  external_account_id: connectorDrafts[connector.connector_type]?.external_account_id || `${connector.connector_type}-account`,
                  display_label: connectorDrafts[connector.connector_type]?.display_label || sourceLabel(connector.connector_type),
                  broker_name: connector.connector_type,
                  connection_metadata: connector.connector_type === 'mt5_bridge'
                    ? {
                      bridge_url: connectorDrafts[connector.connector_type]?.bridge_url || '',
                      mt5_server: connectorDrafts[connector.connector_type]?.mt5_server || '',
                    }
                    : {},
                })}
              >
                Connect
              </button>
              <button disabled={!signedIn} onClick={() => connectorAction(connector.connector_type, 'disconnect')}>Disconnect</button>
            </div>

            {isGuidedAddAccountConnector(connector.connector_type) ? (
              <div className="row">
                <p className="hint">Use <strong>Add Account</strong> for this provider’s guided flow.</p>
                <button type="button" className="secondary-button" disabled={!signedIn} onClick={() => onAddAccount(connector.connector_type)}>
                  Open guided flow
                </button>
              </div>
            ) : null}

            {connector.account_count === 0 && !isGuidedAddAccountConnector(connector.connector_type) ? (
              <div className="row">
                <input
                  placeholder="Account ID"
                  value={connectorDrafts[connector.connector_type]?.external_account_id || ''}
                  onChange={(event) => setConnectorDrafts((prev) => ({
                    ...prev,
                    [connector.connector_type]: {
                      ...prev[connector.connector_type],
                      external_account_id: event.target.value,
                    },
                  }))}
                />
                <input
                  placeholder="Display label"
                  value={connectorDrafts[connector.connector_type]?.display_label || ''}
                  onChange={(event) => setConnectorDrafts((prev) => ({
                    ...prev,
                    [connector.connector_type]: {
                      ...prev[connector.connector_type],
                      display_label: event.target.value,
                    },
                  }))}
                />
                {connector.connector_type === 'mt5_bridge' ? (
                  <>
                    <input
                      placeholder="Bridge URL"
                      value={connectorDrafts[connector.connector_type]?.bridge_url || ''}
                      onChange={(event) => setConnectorDrafts((prev) => ({
                        ...prev,
                        [connector.connector_type]: {
                          ...prev[connector.connector_type],
                          bridge_url: event.target.value,
                        },
                      }))}
                    />
                    <input
                      placeholder="MT5 server"
                      value={connectorDrafts[connector.connector_type]?.mt5_server || ''}
                      onChange={(event) => setConnectorDrafts((prev) => ({
                        ...prev,
                        [connector.connector_type]: {
                          ...prev[connector.connector_type],
                          mt5_server: event.target.value,
                        },
                      }))}
                    />
                  </>
                ) : null}
              </div>
            ) : null}

            <details>
              <summary>Recent sync runs ({(syncHistory[connector.connector_type] || []).length})</summary>
              <ul>
                {(syncHistory[connector.connector_type] || []).map((run) => {
                  const diagnostics = formatSyncRunDiagnostics(run)
                  return (
                    <li key={`run-${run.id}`}>
                      #{run.id} · {run.status} · retries {run.retry_count}/{run.max_retries} · created {formatDate(run.created_at)}
                      {diagnostics.summary ? ` · ${diagnostics.summary}` : ''}
                      {run.error_detail ? ` · error: ${run.error_detail}` : ''}
                    </li>
                  )
                })}
              </ul>
            </details>

            {connector.supports_live_sync ? (
              <details>
                <summary>Connector credentials and config</summary>
                <div className="row">
                  <input
                    placeholder="Healthcheck URL"
                    value={(configDrafts[connector.connector_type] || {}).healthcheck_url || ''}
                    onChange={(event) => setConfigDrafts((prev) => ({
                      ...prev,
                      [connector.connector_type]: {
                        ...(prev[connector.connector_type] || buildConnectorConfigDraft()),
                        healthcheck_url: event.target.value,
                      },
                    }))}
                  />
                  <input
                    placeholder="External account ID"
                    value={(configDrafts[connector.connector_type] || {}).external_account_id || ''}
                    onChange={(event) => setConfigDrafts((prev) => ({
                      ...prev,
                      [connector.connector_type]: {
                        ...(prev[connector.connector_type] || buildConnectorConfigDraft()),
                        external_account_id: event.target.value,
                      },
                    }))}
                  />
                  <input
                    type="number"
                    placeholder="Timeout seconds"
                    value={(configDrafts[connector.connector_type] || {}).timeout_seconds || 8}
                    onChange={(event) => setConfigDrafts((prev) => ({
                      ...prev,
                      [connector.connector_type]: {
                        ...(prev[connector.connector_type] || buildConnectorConfigDraft()),
                        timeout_seconds: event.target.value,
                      },
                    }))}
                  />
                </div>
                <div className="row">
                  <input
                    type="password"
                    placeholder={(configDrafts[connector.connector_type] || {}).hasSecret ? 'API token saved (enter to rotate)' : 'API token'}
                    value={(configDrafts[connector.connector_type] || {}).api_token || ''}
                    onChange={(event) => setConfigDrafts((prev) => ({
                      ...prev,
                      [connector.connector_type]: {
                        ...(prev[connector.connector_type] || buildConnectorConfigDraft()),
                        api_token: event.target.value,
                      },
                    }))}
                  />
                  <button disabled={!signedIn} onClick={() => saveConnectorConfig(connector.connector_type)}>Save config</button>
                  <button disabled={!signedIn} onClick={() => clearConnectorConfig(connector.connector_type)}>Clear config</button>
                </div>
                <p className="hint">Secrets are write-only and never returned by API responses.</p>
              </details>
            ) : null}
          </div>
        ))}
      </section>

      <section className={`panel page-panel connections-utility-panel${addFlowIntent === 'manual' ? ' dev-panel' : ''}`}>
        <h2>Manual Journal</h2>
        {addFlowIntent === 'manual' ? <p className="hint">Add Account directed you here for manual setup.</p> : null}
        <div className="row">
          <input placeholder="External account ID" value={manualAccount.externalAccountId} onChange={(event) => setManualAccount({ ...manualAccount, externalAccountId: event.target.value })} />
          <input placeholder="Display label" value={manualAccount.displayLabel} onChange={(event) => setManualAccount({ ...manualAccount, displayLabel: event.target.value })} />
          <button disabled={!signedIn} onClick={createManualAccount}>Create manual account</button>
        </div>
        <div className="row">
          <input placeholder="Account ID" value={manualTrade.externalAccountId} onChange={(event) => setManualTrade({ ...manualTrade, externalAccountId: event.target.value })} />
          <input placeholder="Symbol" value={manualTrade.symbol} onChange={(event) => setManualTrade({ ...manualTrade, symbol: event.target.value })} />
          <select value={manualTrade.side} onChange={(event) => setManualTrade({ ...manualTrade, side: event.target.value })}>
            <option value="buy">buy</option>
            <option value="sell">sell</option>
          </select>
          <input type="number" placeholder="PnL" value={manualTrade.pnl} onChange={(event) => setManualTrade({ ...manualTrade, pnl: event.target.value })} />
          <button disabled={!signedIn} onClick={createManualTrade}>Record trade</button>
        </div>
      </section>

      <section className={`panel page-panel connections-utility-panel${addFlowIntent === 'csv' ? ' dev-panel' : ''}`}>
        <h2>CSV Import</h2>
        {addFlowIntent === 'csv' ? <p className="hint">Add Account directed you here for CSV import.</p> : null}
        <div className="row">
          <input value={csvAccount} onChange={(event) => setCsvAccount(event.target.value)} placeholder="CSV account ID" />
          <button disabled={!signedIn} onClick={importCsvTrades}>Import rows</button>
        </div>
        <textarea rows={5} value={csvInput} onChange={(event) => setCsvInput(event.target.value)} />
      </section>
    </>
  )
}

export default ConnectionsPage
