import { NavLink } from 'react-router-dom'

function AppLandingPage({
  signedIn,
  hasZeroConnectedAccounts,
  accountConnectionState,
  selectedAccount,
  accountWorkspaces,
  managedConnectors,
  syncHistory,
  onAddAccount,
  onRefreshWorkspace,
  formatDate,
  isWorkspaceLoading,
  workspaceLoadError,
}) {
  const usableAccounts = accountWorkspaces.filter((account) => ['connected', 'active', 'paper_connected', 'live_connected', 'account_verified', 'degraded', 'sync_error'].includes(String(account.connection_status || '').toLowerCase()))
  const pendingAccounts = accountWorkspaces.filter((account) => !usableAccounts.some((item) => item.account_key === account.account_key) && ['awaiting_alerts', 'bridge_required', 'waiting_for_registration', 'ready_for_account_attach', 'beta_pending', 'metadata_saved', 'awaiting_secure_auth', 'waiting_for_secure_auth_support'].includes(String(account.connection_status || '').toLowerCase()))
  const staleAccounts = accountWorkspaces.filter((account) => !usableAccounts.some((item) => item.account_key === account.account_key) && !pendingAccounts.some((item) => item.account_key === account.account_key))
  const connectorsWithErrors = managedConnectors.filter((connector) => connector.last_error)
  const connectorsSyncing = managedConnectors.filter((connector) => ['sync_running', 'sync_retrying', 'sync_queued'].includes(connector.current_sync_state))
  const connectorsConnected = managedConnectors.filter((connector) => connector.account_count > 0 || connector.is_connected)
  const latestConnectorActivity = managedConnectors
    .map((connector) => ({
      connectorType: connector.connector_type,
      sourceName: connector.source_label || connector.connector_type,
      status: connector.status || 'disconnected',
      lastActivityAt: connector.last_activity_at || connector.last_sync_at || null,
      accountCount: Number(connector.account_count || 0),
    }))
    .sort((a, b) => new Date(b.lastActivityAt || 0).getTime() - new Date(a.lastActivityAt || 0).getTime())
    .slice(0, 3)
  const latestRuns = Object.values(syncHistory)
    .flatMap((runs) => runs || [])
    .sort((a, b) => new Date(b.created_at || 0).getTime() - new Date(a.created_at || 0).getTime())
    .slice(0, 4)

  if (!signedIn) {
    return (
      <section className="panel page-panel app-dashboard-hub premium-workspace-panel dashboard-page">
        <div className="panel-header">
          <p className="kicker">Workspace</p>
          <h2>Preview the TaliTrade workspace, then unlock it with Telegram</h2>
        </div>
        <p className="hint">
          You can browse Dashboard, Accounts, and Connections now. Sign in to attach broker accounts, sync data, and run account-specific workflows.
        </p>
        <div className="meta-grid accounts-summary-grid">
          <div className="meta-card summary-card">
            <span className="hint">Navigation</span>
            <strong>Dashboard · Accounts · Connections</strong>
          </div>
          <div className="meta-card summary-card">
            <span className="hint">What unlocks after sign-in</span>
            <strong>Add Account, sync control, account focus</strong>
          </div>
        </div>
        <div className="row app-onboarding-links">
          <NavLink className="app-nav-link" to="/app/accounts">Review Accounts surface</NavLink>
          <NavLink className="app-nav-link" to="/app/connections">Review Connections surface</NavLink>
        </div>
      </section>
    )
  }

  if (isWorkspaceLoading) {
    return (
      <section className="panel page-panel app-dashboard-hub premium-workspace-panel dashboard-page">
        <div className="panel-header">
          <p className="kicker">Dashboard</p>
          <h2>Loading account workspace</h2>
        </div>
        <div className="skeleton-grid">
          <div className="skeleton-card" />
          <div className="skeleton-card" />
          <div className="skeleton-card" />
        </div>
      </section>
    )
  }

  if (workspaceLoadError) {
    return (
      <section className="panel page-panel app-dashboard-hub premium-workspace-panel dashboard-page">
        <div className="panel-header">
          <p className="kicker">Dashboard</p>
          <h2>Workspace data could not be loaded</h2>
        </div>
        <p className="error-text">{workspaceLoadError}</p>
        <div className="row">
          <p className="hint">Use refresh to re-hydrate account and connector data.</p>
          <button type="button" className="secondary-button" onClick={onRefreshWorkspace}>Retry workspace load</button>
        </div>
      </section>
    )
  }

  if (hasZeroConnectedAccounts) {
    const hasPendingOnly = pendingAccounts.length > 0 && staleAccounts.length === 0
    const hasStaleOnly = staleAccounts.length > 0 && pendingAccounts.length === 0
    return (
      <section className="panel page-panel app-onboarding-hub premium-workspace-panel dashboard-page">
        <div className="panel-header row">
          <div>
            <p className="kicker">Onboarding</p>
            <h2>Add your first live workspace account</h2>
          </div>
          <button type="button" className="primary-cta" onClick={() => onAddAccount('mt5_bridge')}>Add Account</button>
        </div>
        <p className="hint">
          This is your workspace launch point: connect a provider in Accounts, then run connector operations in Connections.
        </p>
        <ul className="onboarding-path-list">
          <li><strong>MT5</strong> bridge with registration and pairing support</li>
          <li><strong>FundingPips</strong> extension-linked account attach</li>
          <li><strong>TradingView</strong> webhook signal routing</li>
          <li><strong>CSV Import</strong> for historical journals</li>
          <li><strong>Manual Journal</strong> for custom account/trade entry</li>
          <li><strong>Public API connectors</strong> (Alpaca, OANDA, Binance beta paths)</li>
        </ul>
        <div className="row app-onboarding-links">
          <button type="button" className="primary-cta" onClick={() => onAddAccount('mt5_bridge')}>Add Account</button>
          <NavLink className="app-nav-link" to="/app/accounts">Go to Accounts (primary)</NavLink>
          <NavLink className="app-nav-link" to="/app/connections">Go to Connections (operations)</NavLink>
        </div>
        <p className="hint">
          Current workspace inventory: {accountConnectionState.totalCount} total · {accountConnectionState.pendingOnlyCount} pending-only · {accountConnectionState.staleInactiveCount} inactive/stale.
        </p>
        {hasPendingOnly ? <p className="hint"><strong>Current mode:</strong> pending-only workspace context. Complete provider setup before an account becomes usable.</p> : null}
        {hasStaleOnly ? <p className="hint"><strong>Current mode:</strong> stale/inactive workspace context. Reconnect a provider to restore an active account.</p> : null}
        {!hasPendingOnly && !hasStaleOnly && accountConnectionState.totalCount > 0 ? (
          <p className="hint"><strong>Current mode:</strong> mixed non-usable records detected (pending + stale). Use Accounts to pick which provider to recover first.</p>
        ) : null}
      </section>
    )
  }

  return (
    <section className="panel page-panel app-dashboard-hub premium-workspace-panel dashboard-page">
      <div className="panel-header row">
        <div>
          <p className="kicker">Dashboard</p>
          <h2>Account workspace overview</h2>
        </div>
        <button type="button" className="primary-cta" onClick={() => onAddAccount('mt5_bridge')}>Add Account</button>
      </div>

        <div className="meta-grid accounts-summary-grid premium-summary-grid dashboard-summary-grid">
        <div className="meta-card summary-card">
          <span className="hint">Total account rows</span>
          <strong>{accountConnectionState.totalCount}</strong>
        </div>
        <div className="meta-card summary-card">
          <span className="hint">Connected accounts</span>
          <strong>{accountConnectionState.connectedUsableCount}</strong>
        </div>
        <div className="meta-card summary-card">
          <span className="hint">Stale / inactive accounts</span>
          <strong>{accountConnectionState.staleInactiveCount}</strong>
        </div>
        <div className="meta-card summary-card">
          <span className="hint">Connectors syncing now</span>
          <strong>{connectorsSyncing.length}</strong>
        </div>
        <div className="meta-card summary-card">
          <span className="hint">Connectors needing review</span>
          <strong>{connectorsWithErrors.length}</strong>
        </div>
      </div>
      <div className="row app-onboarding-links">
        <button type="button" className="primary-cta" onClick={() => onAddAccount('mt5_bridge')}>Add Account</button>
        <NavLink className="app-nav-link" to="/app/accounts">Go to Accounts</NavLink>
        <NavLink className="app-nav-link" to="/app/connections">Go to Connections</NavLink>
      </div>

      <div className="card selected-account-panel premium-focus-card dashboard-focus-card">
        <h3>Active account focus</h3>
        {selectedAccount ? (
          <>
            <p>
              <strong>{selectedAccount.display_label || selectedAccount.external_account_id}</strong>
              {' · '}
              <span className="pill">{selectedAccount.source_label}</span>
              {selectedAccount.broker_name ? <span className="pill">{selectedAccount.broker_name}</span> : null}
              {selectedAccount.is_primary ? <span className="pill primary-pill">Primary</span> : null}
            </p>
            <div className="meta-grid">
              <div className="meta-card">
                <span className="hint">Account label</span>
                <strong>{selectedAccount.display_label || '—'}</strong>
              </div>
              <div className="meta-card">
                <span className="hint">Account ID</span>
                <strong className="mono">{selectedAccount.external_account_id || selectedAccount.trading_account_id || selectedAccount.account_key}</strong>
              </div>
              <div className="meta-card">
                <span className="hint">Provider/source</span>
                <strong>{selectedAccount.source_label || selectedAccount.connector_type || 'Unknown provider'}</strong>
              </div>
              <div className="meta-card">
                <span className="hint">Broker</span>
                <strong>{selectedAccount.broker_name || 'Broker metadata pending'}</strong>
              </div>
              <div className="meta-card">
                <span className="hint">Connection status</span>
                <strong>{selectedAccount.connection_status || 'disconnected'}</strong>
              </div>
              <div className="meta-card">
                <span className="hint">Sync state</span>
                <strong>{selectedAccount.sync_state || 'idle'}</strong>
              </div>
              <div className="meta-card">
                <span className="hint">Last sync</span>
                <strong>{formatDate(selectedAccount.last_sync_at)}</strong>
              </div>
              <div className="meta-card">
                <span className="hint">Last activity</span>
                <strong>{formatDate(selectedAccount.last_activity_at)}</strong>
              </div>
            </div>
          </>
        ) : (
          <p className="hint">No active usable account is selected. Use Accounts to select a workspace account.</p>
        )}
      </div>

      <div className="card premium-activity-card dashboard-activity-card">
        <h3>Connector health snapshot</h3>
        <p className="hint">
          Connected connectors: <strong>{connectorsConnected.length}</strong> · Syncing: <strong>{connectorsSyncing.length}</strong> · Needs review: <strong>{connectorsWithErrors.length}</strong>
        </p>
        {latestConnectorActivity.length > 0 ? (
          <ul className="sync-activity-list">
            {latestConnectorActivity.map((entry) => (
              <li key={`connector-activity-${entry.connectorType}`}>
                <span className="pill">{entry.connectorType}</span>
                <span className="pill">{entry.status}</span>
                <span>{entry.accountCount} linked account(s)</span>
                <span className="hint">Last activity {formatDate(entry.lastActivityAt)}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="hint">No connector activity yet. Add and connect an account to start workspace operations.</p>
        )}
      </div>

      <div className="card premium-activity-card dashboard-activity-card">
        <h3>Account readiness snapshot</h3>
        <p className="hint">
          Usable accounts: <strong>{usableAccounts.length}</strong> · Pending setup: <strong>{pendingAccounts.length}</strong> · Historical/inactive: <strong>{accountConnectionState.staleInactiveCount}</strong>
        </p>
        {usableAccounts.slice(0, 3).length > 0 ? (
          <ul className="sync-activity-list">
            {usableAccounts.slice(0, 3).map((account) => (
              <li key={`usable-${account.account_key}`}>
                <span className="pill">{account.display_label || account.external_account_id || account.account_key}</span>
                <span className="pill">{account.source_label || account.connector_type}</span>
                <span>{account.broker_name || 'Broker pending metadata'}</span>
                <span className="hint">Last sync {formatDate(account.last_sync_at)}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="hint">No usable accounts yet. Use Add Account to connect MT5, FundingPips, TradingView, CSV, Manual, or supported beta API providers.</p>
        )}
      </div>

      <div className="card premium-activity-card dashboard-activity-card">
        <h3>Recent sync activity</h3>
        {latestRuns.length > 0 ? (
          <ul className="sync-activity-list">
            {latestRuns.map((run) => (
              <li key={`dashboard-run-${run.id}`}>
                <span className="pill">{run.status || 'unknown'}</span>
                <span>Run #{run.id}</span>
                <span className="hint">{formatDate(run.created_at)}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="hint">Sync history will appear after your first connector sync run.</p>
        )}
      </div>
    </section>
  )
}

export default AppLandingPage
