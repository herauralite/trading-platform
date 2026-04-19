import { buildConnectorConfigDraft, connectorConfigStateLabel } from '../connectorConfig'
import { formatSyncRunDiagnostics } from '../syncRunDiagnostics'
import { isGuidedAddAccountConnector } from '../addAccountFlow'

function displaySourceLabel(sourceLabel, connectorType) {
  if (connectorType === 'tradelocker_api') return 'TradeLocker API'
  return sourceLabel(connectorType)
}

function nextStepHint(connector) {
  if (connector.config_validation_error) {
    return 'Next: review credentials/config, then save connector config again.'
  }
  if (connector.last_error && (connector.current_sync_state === 'retrying' || (connector.current_sync_retry_count || 0) > 0)) {
    return 'Next: wait for retry or run Sync manually after reviewing the latest error.'
  }
  if (connector.account_count === 0 && isGuidedAddAccountConnector(connector.connector_type)) {
    return 'Next: use Add Account to continue this provider’s guided onboarding.'
  }
  return ''
}

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
}) {
  return (
    <>
      <section className="panel">
        <div className="row">
          <h2>Connections</h2>
          <button type="button" onClick={onAddAccount}>Add Account</button>
        </div>
        <p className="hint">
          <strong>Connections</strong> is for operational integration setup and connector controls. For adding and managing trading accounts, start in <strong>Accounts</strong>.
        </p>
        <p>Available connectors: {catalog.map((entry) => entry.label).join(', ') || '—'}</p>
        {managedConnectors.map((connector) => {
          const nextHint = nextStepHint(connector)
          return (
            <div key={connector.connector_type} className="card">
              <div className="row">
                <strong>{displaySourceLabel(sourceLabel, connector.connector_type)}</strong>
                <span className={`badge ${statusTone(connector.status)}`}>{connector.status}</span>
                {connector.integration_status ? <span className="pill">{connector.integration_status}</span> : null}
                {connector.provider_state ? <span className="pill">{connector.provider_state}</span> : null}
              </div>
              {connector.notes ? <p className="hint">{connector.notes}</p> : null}
              {connector.onboarding_copy ? <p className="hint">{connector.onboarding_copy}</p> : null}
              <div className="meta">
                State: {connector.is_connected ? 'connected' : 'disconnected'} · Accounts: {connector.account_count} · Last activity: {formatDate(connector.last_activity_at)} · Last sync: {formatDate(connector.last_sync_at)}
              </div>
              {connector.connector_type === 'tradingview_webhook' ? (
                <div className="meta">
                  {connector.status === 'active'
                    ? `Webhook active · Last alert received ${formatDate(connector.last_activity_at)}`
                    : 'Awaiting first TradingView alert'}
                </div>
              ) : null}
              <div className="meta">
                Sync state: {syncStateLabel(connector.current_sync_state)} · Retries: {connector.current_sync_retry_count || 0} · Next retry: {formatDate(connector.next_retry_at)}
              </div>
              <div className="meta">
                Config: {connectorConfigStateLabel(connector)} {connector.configured_secret_fields?.length ? `· secret fields: ${connector.configured_secret_fields.join(', ')}` : ''}
              </div>
              {connector.config_validation_error ? <p className="error-text">Config issue: {connector.config_validation_error}</p> : null}
              {connector.last_error ? <p className="error-text">Last error: {connector.last_error} ({formatDate(connector.last_error_at)})</p> : null}
              {nextHint ? <p className="hint"><strong>{nextHint}</strong></p> : null}
              <ul>
                {connector.accounts.map((account) => (
                  <li key={`${connector.connector_type}-${account.id}`}>
                    <span>{account.display_label || account.external_account_id}</span>
                    <span className="pill">{displaySourceLabel(sourceLabel, connector.connector_type)}</span>
                    <span className="pill">{account.broker_name || 'Unknown broker'}</span>
                    {connector.connector_type === 'tradingview_webhook' ? (
                      <span className="pill">
                        {account.activation_state === 'active' ? 'Webhook active' : 'Awaiting first alert'}
                      </span>
                    ) : null}
                  </li>
                ))}
              </ul>
              {connector.connector_type === 'tradingview_webhook' && (connector.recent_events || []).length > 0 ? (
                <details>
                  <summary>Recent TradingView alerts ({connector.recent_events.length})</summary>
                  <ul>
                    {connector.recent_events.map((event, index) => (
                      <li key={`tv-event-${index}`}>
                        <strong>{event.symbol || event.event_type || 'alert'}</strong>
                        {event.timeframe ? ` · ${event.timeframe}` : ''}
                        {event.title ? ` · ${event.title}` : ''}
                        {event.message ? ` · ${event.message}` : ''}
                        {' · '}
                        {formatDate(event.received_at)}
                      </li>
                    ))}
                  </ul>
                </details>
              ) : null}
              <div className="row">
                {!connector.supports_live_sync ? <span className="hint">Sync not supported</span> : null}
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
                    display_label: connectorDrafts[connector.connector_type]?.display_label || displaySourceLabel(sourceLabel, connector.connector_type),
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
                <p className="hint">Use <strong>Add Account</strong> for this provider’s guided onboarding flow.</p>
              ) : null}
              {connector.account_count === 0 && !isGuidedAddAccountConnector(connector.connector_type) ? (
                <div className="row">
                  <input
                    placeholder="Account id for connect"
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
                        placeholder="Bridge URL (optional for placeholder connect)"
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
                        placeholder="MT5 server name"
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
                  {(syncHistory[connector.connector_type] || []).map((run) => (
                    <li key={`run-${run.id}`}>
                      {(() => {
                        const diagnostics = formatSyncRunDiagnostics(run)
                        return (
                          <>
                            #{run.id} · {run.status} · retries {run.retry_count}/{run.max_retries} · created {formatDate(run.created_at)}
                            {diagnostics.resultCategory ? ` · category: ${diagnostics.resultCategory}` : ''}
                            {diagnostics.summary ? ` · ${diagnostics.summary}` : ''}
                            {run.error_detail ? ` · error: ${run.error_detail}` : ''}
                            {diagnostics.errorCode ? ` · code: ${diagnostics.errorCode}` : ''}
                            {diagnostics.errorCategory ? ` · failure: ${diagnostics.errorCategory}` : ''}
                            {diagnostics.isTransient === true ? ' · transient' : ''}
                            {diagnostics.isTransient === false ? ' · structural' : ''}
                          </>
                        )
                      })()}
                    </li>
                  ))}
                </ul>
              </details>
              {connector.supports_live_sync ? (
                <details>
                  <summary>Connector credentials/config</summary>
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
                      placeholder="External account id"
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
                  <p className="hint">Secrets are write-only in API responses. Saved tokens are never returned to the client.</p>
                </details>
              ) : null}
            </div>
          )
        })}
      </section>

      <section className={`panel${addFlowIntent === 'manual' ? ' dev-panel' : ''}`}>
        <h2>Manual Journal (authenticated)</h2>
        {addFlowIntent === 'manual' ? <p className="hint">Add Account routed you here for manual account setup.</p> : null}
        <div className="row">
          <input placeholder="External account id" value={manualAccount.externalAccountId} onChange={(event) => setManualAccount({ ...manualAccount, externalAccountId: event.target.value })} />
          <input placeholder="Display label" value={manualAccount.displayLabel} onChange={(event) => setManualAccount({ ...manualAccount, displayLabel: event.target.value })} />
          <button disabled={!signedIn} onClick={createManualAccount}>Create manual account</button>
        </div>
        <div className="row">
          <input placeholder="Manual account id" value={manualTrade.externalAccountId} onChange={(event) => setManualTrade({ ...manualTrade, externalAccountId: event.target.value })} />
          <input placeholder="Symbol" value={manualTrade.symbol} onChange={(event) => setManualTrade({ ...manualTrade, symbol: event.target.value })} />
          <select value={manualTrade.side} onChange={(event) => setManualTrade({ ...manualTrade, side: event.target.value })}>
            <option value="buy">buy</option>
            <option value="sell">sell</option>
          </select>
          <input type="number" placeholder="PnL" value={manualTrade.pnl} onChange={(event) => setManualTrade({ ...manualTrade, pnl: event.target.value })} />
          <button disabled={!signedIn} onClick={createManualTrade}>Record trade</button>
        </div>
      </section>

      <section className={`panel${addFlowIntent === 'csv' ? ' dev-panel' : ''}`}>
        <h2>CSV Import (authenticated)</h2>
        {addFlowIntent === 'csv' ? <p className="hint">Add Account routed you here for CSV import setup.</p> : null}
        <div className="row">
          <input value={csvAccount} onChange={(event) => setCsvAccount(event.target.value)} placeholder="CSV account id" />
          <button disabled={!signedIn} onClick={importCsvTrades}>Import JSON rows as CSV trades</button>
        </div>
        <textarea rows={5} value={csvInput} onChange={(event) => setCsvInput(event.target.value)} />
      </section>
    </>
  )
}

export default ConnectionsPage
