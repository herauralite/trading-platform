import { useMemo } from 'react'
import { useLocation } from 'react-router-dom'
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
  selectedAccount,
  isWorkspaceLoading,
  onRefreshWorkspace,
}) {
  const location = useLocation()
  const inboundIntent = useMemo(() => {
    const params = new URLSearchParams(location.search)
    return {
      provider: String(params.get('provider') || '').trim().toLowerCase(),
      account: String(params.get('account') || '').trim(),
      intent: String(params.get('intent') || '').trim().toLowerCase(),
    }
  }, [location.search])

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
  const connectedCount = managedConnectors.filter((connector) => connector.account_count > 0 || connector.is_connected).length
  const syncingCount = managedConnectors.filter((connector) => ['sync_running', 'sync_retrying', 'sync_queued'].includes(connector.current_sync_state)).length
  const attentionCount = managedConnectors.filter((connector) => connector.last_error || connector.status === 'sync_error' || connector.config_validation_error).length
  const selectedConnectorType = selectedAccount?.connector_type || ''
  const prioritizedProviderKey = inboundIntent.provider || selectedConnectorType
  const selectedProviderLabel = selectedAccount?.source_label || selectedAccount?.connector_type || ''
  const selectedMethod = connectionMethods.find((method) => method.key === selectedConnectorType) || null
  const inboundMethod = connectionMethods.find((method) => method.key === inboundIntent.provider) || null

  const intentHeadline = inboundIntent.intent === 'setup'
    ? 'Continue provider setup'
    : inboundIntent.intent === 'reconnect'
      ? 'Reconnect provider and account'
      : inboundIntent.intent === 'manage'
        ? 'Manage provider connection'
        : ''

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

  function selectedProviderNextAction(connector, methodKey) {
    const status = String(selectedAccount?.connection_status || connector?.status || '').toLowerCase()
    const providerState = String(selectedAccount?.provider_state || connector?.provider_state || '').toLowerCase()
    const syncState = String(selectedAccount?.sync_state || connector?.current_sync_state || '').toLowerCase()
    if (status === 'waiting_for_registration' || status === 'bridge_required' || providerState === 'waiting_for_registration' || providerState === 'bridge_required') {
      return 'Recommended next action: continue MT5 setup to complete registration and bridge pairing.'
    }
    if (status === 'awaiting_alerts' || providerState === 'awaiting_alerts') {
      return 'Recommended next action: send your first TradingView alert to activate account ingestion.'
    }
    if (status === 'awaiting_secure_auth' || status === 'beta_pending' || providerState === 'awaiting_secure_auth' || providerState === 'beta_pending') {
      return 'Recommended next action: secure auth is not complete yet; finish provider auth in Add Account.'
    }
    if ((status === 'connected' || methodKey === selectedConnectorType) && (syncState === 'failed' || syncState === 'retrying' || status === 'sync_error')) {
      return 'Recommended next action: retry sync and review connector configuration for the selected account.'
    }
    if (status === 'connected' || status === 'active' || status === 'paper_connected' || status === 'live_connected' || status === 'account_verified') {
      return 'Recommended next action: view linked accounts or refresh connector health.'
    }
    return 'Recommended next action: open this provider to continue setup for the selected account.'
  }

  return (
    <>
      <section className="panel page-panel premium-workspace-panel connections-page">
        <div className="panel-header row">
          <div>
            <p className="kicker">Connections</p>
            <h2>Connector operations and sync controls</h2>
          </div>
          <div className="row">
            <button type="button" className="secondary-button" onClick={onRefreshWorkspace}>Refresh</button>
            <button type="button" className="primary-cta" onClick={() => onAddAccount('mt5_bridge')}>Add Account</button>
          </div>
        </div>
        <p className="hint">
          <strong>Accounts</strong> is your account-centric workspace. <strong>Connections</strong> handles provider configuration and sync operations for the current workspace context.
        </p>
        {inboundIntent.provider ? (
          <div className="card connections-intent-focus">
            <div className="row">
              <strong>{intentHeadline || 'Focused provider context'}</strong>
              <span className="pill primary-pill">{inboundMethod?.title || inboundIntent.provider}</span>
              {inboundIntent.account ? <span className="pill mono">{inboundIntent.account}</span> : null}
            </div>
            <p className="hint">This section is focused from Account details so you can continue the exact provider flow without reselecting context.</p>
          </div>
        ) : null}
        <div className="card selected-account-panel premium-focus-card connections-context-panel">
          <h3>Selected account context</h3>
          {selectedAccount ? (
            <>
              <p>
                <strong>{selectedAccount.display_label || selectedAccount.external_account_id || selectedAccount.account_key}</strong>
                {' · '}
                <span className="mono">{selectedAccount.account_key}</span>
              </p>
              <div className="row">
                <span className="pill">{selectedAccount.source_label || selectedAccount.connector_type}</span>
                <span className="pill">{selectedAccount.broker_name || 'Broker metadata pending'}</span>
                <span className="pill">Connection {selectedAccount.connection_status || 'unavailable'}</span>
                <span className="pill">Sync {selectedAccount.sync_state || 'unavailable'}</span>
                {selectedAccount.is_primary ? <span className="pill primary-pill">Primary</span> : null}
              </div>
              <p className="hint">Connections actions stay scoped around this selected account/provider context.</p>
              {selectedConnectorType ? (
                <>
                  <p className="hint"><strong>Selected account provider:</strong> {selectedProviderLabel}</p>
                  <p className="hint"><strong>Manage this connection method next:</strong> {selectedMethod?.title || selectedConnectorType}</p>
                  <p className="hint">{selectedProviderNextAction(findConnector(selectedConnectorType), selectedConnectorType)}</p>
                  {inboundIntent.provider && inboundIntent.provider !== selectedConnectorType ? <p className="hint"><strong>Route focus provider:</strong> {inboundMethod?.title || inboundIntent.provider}</p> : null}
                  {inboundIntent.account && inboundIntent.account !== selectedAccount.account_key ? <p className="hint"><strong>Route focus account key:</strong> <span className="mono">{inboundIntent.account}</span></p> : null}
                </>
              ) : null}
            </>
          ) : (
            <p className="hint">No active usable account selected yet. Go to Accounts to set active account focus before running provider operations.</p>
          )}
        </div>
        {!signedIn ? (
          <div className="card premium-auth-helper">
            <strong>Signed out: setup actions are disabled.</strong>
            <p className="hint">Sign in with Telegram in the shell gate to connect providers, run sync jobs, import CSV rows, or write manual journal entries.</p>
          </div>
        ) : null}
        <div className="meta-grid premium-summary-grid connections-method-grid">
          <div className="meta-card summary-card">
            <span className="hint">Connected connectors</span>
            <strong>{connectedCount}</strong>
          </div>
          <div className="meta-card summary-card">
            <span className="hint">Sync in progress</span>
            <strong>{syncingCount}</strong>
          </div>
          <div className="meta-card summary-card">
            <span className="hint">Needs attention</span>
            <strong>{attentionCount}</strong>
          </div>
        </div>
        {Object.entries(groupedMethods).map(([groupName, methods]) => (
          <div key={groupName} className="connections-group-section">
            <h3>{groupName}</h3>
            <div className="meta-grid premium-summary-grid connections-method-grid">
          {[...methods].sort((a, b) => {
            if (!prioritizedProviderKey) return 0
            if (a.key === prioritizedProviderKey) return -1
            if (b.key === prioritizedProviderKey) return 1
            return 0
          }).map((method) => {
            const connector = findConnector(method.key)
            const action = primaryAction(connector, method.key)
            const isSelectedProvider = prioritizedProviderKey && prioritizedProviderKey === method.key
            return (
              <div className={`meta-card summary-card ${isSelectedProvider ? 'provider-priority-card' : 'provider-secondary-card'}`} key={method.key}>
                <div className="row">
                  <strong>{method.title}</strong>
                  {isSelectedProvider ? <span className="pill primary-pill">Focused provider</span> : <span className="pill">Other provider</span>}
                  <span className={`badge ${statusTone(connector?.status || 'disconnected')}`}>
                    {connectorActionLabel(connector)}
                  </span>
                </div>
                <p className="hint">{method.description}</p>
                <p className="hint">
                  {isSelectedProvider ? selectedProviderNextAction(connector, method.key) : `Next action: ${signedIn ? action.label : 'Sign in to begin setup.'}`}
                </p>
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
          <div key={connector.connector_type} className={`card connector-card connections-connector-card${prioritizedProviderKey && prioritizedProviderKey === connector.connector_type ? ' provider-priority-card' : ''}`}>
            {(() => {
              const lifecycle = deriveConnectorLifecycleState(connector)
              return (
                <>
            <div className="row">
              <strong>{sourceLabel(connector.connector_type)}</strong>
              {prioritizedProviderKey && prioritizedProviderKey === connector.connector_type ? <span className="pill primary-pill">Focused provider</span> : null}
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
