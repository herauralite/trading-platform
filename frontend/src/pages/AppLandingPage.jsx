import { NavLink } from 'react-router-dom'

function AppLandingPage({
  signedIn,
  hasZeroConnectedAccounts,
  accountConnectionState,
  selectedAccount,
  managedConnectors,
  syncHistory,
  onAddAccount,
  formatDate,
  isWorkspaceLoading,
}) {
  const connectorsWithErrors = managedConnectors.filter((connector) => connector.last_error)
  const connectorsSyncing = managedConnectors.filter((connector) => ['sync_running', 'sync_retrying', 'sync_queued'].includes(connector.current_sync_state))
  const latestRuns = Object.values(syncHistory)
    .flatMap((runs) => runs || [])
    .sort((a, b) => new Date(b.created_at || 0).getTime() - new Date(a.created_at || 0).getTime())
    .slice(0, 4)

  if (!signedIn) {
    return (
      <section className="panel page-panel app-dashboard-hub">
        <div className="panel-header">
          <p className="kicker">Workspace</p>
          <h2>Connect Telegram to unlock your trading workspace</h2>
        </div>
        <p className="hint">
          You can browse Dashboard, Accounts, and Connections now. Sign in to attach broker accounts, sync data, and run account-specific workflows.
        </p>
        <div className="row app-onboarding-links">
          <NavLink className="app-nav-link" to="/app/accounts">Review Accounts surface</NavLink>
          <NavLink className="app-nav-link" to="/app/connections">Review Connections surface</NavLink>
        </div>
      </section>
    )
  }

  if (isWorkspaceLoading) {
    return (
      <section className="panel page-panel app-dashboard-hub">
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

  if (hasZeroConnectedAccounts) {
    return (
      <section className="panel page-panel app-onboarding-hub">
        <div className="panel-header row">
          <div>
            <p className="kicker">Onboarding</p>
            <h2>Add your first live workspace account</h2>
          </div>
          <button type="button" className="primary-cta" onClick={onAddAccount}>Add Account</button>
        </div>
        <p className="hint">
          Choose a provider, link an account, then use Connections for sync operations and connector-level controls.
        </p>
        <ul className="onboarding-path-list">
          <li><strong>MT5</strong> bridge with registration and pairing support</li>
          <li><strong>FundingPips</strong> extension-linked account attach</li>
          <li><strong>TradingView</strong> webhook signal routing</li>
          <li><strong>CSV Import</strong> for historical journals</li>
          <li><strong>Manual Journal</strong> for custom account/trade entry</li>
          <li><strong>Public API connectors</strong> (Alpaca, OANDA, Binance beta paths)</li>
        </ul>
        <p className="hint">
          Current workspace inventory: {accountConnectionState.totalCount} total · {accountConnectionState.pendingOnlyCount} pending-only · {accountConnectionState.staleInactiveCount} inactive/stale.
        </p>
      </section>
    )
  }

  return (
    <section className="panel page-panel app-dashboard-hub">
      <div className="panel-header row">
        <div>
          <p className="kicker">Dashboard</p>
          <h2>Account workspace overview</h2>
        </div>
        <button type="button" className="primary-cta" onClick={onAddAccount}>Add Account</button>
      </div>

      <div className="meta-grid accounts-summary-grid">
        <div className="meta-card summary-card">
          <span className="hint">Connected accounts</span>
          <strong>{accountConnectionState.connectedUsableCount}</strong>
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

      <div className="card selected-account-panel">
        <h3>Active account focus</h3>
        {selectedAccount ? (
          <p>
            <strong>{selectedAccount.display_label || selectedAccount.external_account_id}</strong>
            {' · '}
            <span className="pill">{selectedAccount.source_label}</span>
            {' · Last sync '}
            {formatDate(selectedAccount.last_sync_at)}
          </p>
        ) : (
          <p className="hint">Select an account in the shell switcher to focus account context across pages.</p>
        )}
      </div>

      <div className="card">
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
