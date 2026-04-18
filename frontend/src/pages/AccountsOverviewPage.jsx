import { useMemo } from 'react'
import AccountStatusBadge from '../components/AccountStatusBadge'
import AccountWorkspaceCard from '../components/AccountWorkspaceCard'
import { deriveAccountConnectionState } from '../accountConnectionState'

function countBy(items, predicate) {
  return items.reduce((count, item) => (predicate(item) ? count + 1 : count), 0)
}

function AccountsOverviewPage({
  accountWorkspaces,
  selectedAccount,
  onSelectAccount,
  onAddAccount,
  recentlyAddedAccountLabel,
  formatDate,
}) {
  const summary = useMemo(() => {
    const connectionState = deriveAccountConnectionState(accountWorkspaces)
    const attention = countBy(accountWorkspaces, (account) => account.connection_status === 'sync_error' || account.sync_state === 'failed')
    const syncing = countBy(accountWorkspaces, (account) => ['queued', 'running', 'retrying'].includes(account.sync_state))
    const primary = accountWorkspaces.find((account) => account.is_primary) || null
    return {
      total: connectionState.totalCount,
      connected: connectionState.connectedUsableCount,
      pendingOnly: connectionState.pendingOnlyCount,
      staleInactive: connectionState.staleInactiveCount,
      attention,
      syncing,
      primary,
      hasZeroConnectedAccounts: connectionState.hasZeroConnectedAccounts,
    }
  }, [accountWorkspaces])

  if (summary.hasZeroConnectedAccounts) {
    return (
      <section className="panel">
        <div className="row">
          <h2>Accounts</h2>
          <button type="button" onClick={onAddAccount}>Add Account</button>
        </div>
        <p className="hint">Accounts is your primary onboarding space after sign-in.</p>
        <div className="empty-state account-onboarding-empty-state">
          <h3>Connect your first trading account</h3>
          <p className="empty-state-copy">
            Start here to activate your account workspace. Supported setup paths:
          </p>
          <ul className="onboarding-path-list">
            <li><strong>MT5</strong> for bridge-based MetaTrader 5 account onboarding.</li>
            <li><strong>FundingPips Extension</strong> to connect through the browser extension flow.</li>
            <li><strong>TradingView Webhook</strong> for signal-driven account automation.</li>
            <li><strong>CSV Import</strong> to import historical trades.</li>
            <li><strong>Manual Journal</strong> to add accounts and trades manually.</li>
          </ul>
          <button type="button" className="primary-cta" onClick={onAddAccount}>Add your first account</button>
          {summary.total > 0 ? (
            <p className="hint">
              Existing workspace rows: {summary.total} (pending-only: {summary.pendingOnly}, inactive/stale: {summary.staleInactive}).
              Onboarding stays visible until at least one account is currently connected.
            </p>
          ) : null}
          <p className="hint">Need operational setup and connector controls later? Use <strong>Connections</strong>.</p>
        </div>
      </section>
    )
  }

  return (
    <section className="panel">
      <div className="row">
        <h2>Accounts</h2>
        <button type="button" onClick={onAddAccount}>Add Account</button>
      </div>
      <p className="hint">
        This is the main workspace for all connected trading accounts. Connector health is displayed using workspace rollup semantics.
      </p>


      {recentlyAddedAccountLabel ? (
        <div className="card add-success-banner">
          <strong>Account added successfully.</strong>
          <p className="hint">Focused account: <strong>{recentlyAddedAccountLabel}</strong>. You can manage connector/bridge details in Connections.</p>
        </div>
      ) : null}
      <div className="meta-grid accounts-summary-grid">
        <div className="meta-card summary-card">
          <span className="hint">All workspace accounts</span>
          <strong>{summary.total}</strong>
        </div>
        <div className="meta-card summary-card">
          <span className="hint">Healthy connections</span>
          <strong>{summary.connected}</strong>
        </div>
        <div className="meta-card summary-card">
          <span className="hint">Currently syncing</span>
          <strong>{summary.syncing}</strong>
        </div>
        <div className="meta-card summary-card">
          <span className="hint">Needs attention</span>
          <strong>{summary.attention}</strong>
        </div>
      </div>

      <div className="card selected-account-panel">
        <div className="row">
          <h3>Active account context</h3>
          {selectedAccount?.is_primary ? <span className="pill primary-pill">Primary</span> : null}
        </div>
        {selectedAccount ? (
          <>
            <p>
              <strong>{selectedAccount.display_label || selectedAccount.external_account_id || selectedAccount.account_key}</strong>
              {' · '}
              <span className="mono">{selectedAccount.account_key}</span>
            </p>
            <div className="row">
              <span className="pill">{selectedAccount.source_label}</span>
              <span className="pill">{selectedAccount.broker_name || 'Unknown broker'}</span>
              <span className="hint">Connection</span>
              <AccountStatusBadge value={selectedAccount.connection_status} />
              {selectedAccount.provider_state ? <span className="pill">{selectedAccount.provider_state}</span> : null}
              <span className="hint">Sync</span>
              <AccountStatusBadge variant="sync" value={selectedAccount.sync_state} />
            </div>
            {selectedAccount.connector_type === 'tradingview_webhook' ? (
              <div className="meta tradingview-activity-preview">
                <p className="hint">
                  {selectedAccount.connection_status === 'active'
                    ? `Webhook active · Last alert received ${formatDate(selectedAccount.tradingview_last_event_at || selectedAccount.last_activity_at)}`
                    : 'Awaiting first TradingView alert'}
                </p>
                {(selectedAccount.recent_events || []).length > 0 ? (
                  <ul>
                    {selectedAccount.recent_events.slice(0, 3).map((event, index) => (
                      <li key={`${selectedAccount.account_key}-event-${index}`}>
                        <strong>{event.symbol || event.event_type || 'alert'}</strong>
                        {event.timeframe ? ` · ${event.timeframe}` : ''}
                        {' · '}
                        {formatDate(event.received_at)}
                      </li>
                    ))}
                  </ul>
                ) : null}
              </div>
            ) : null}
          </>
        ) : (
          <p className="hint">Select an account to establish workspace context for future account-specific views.</p>
        )}
        {summary.primary ? (
          <p className="hint">
            Preferred account: <strong>{summary.primary.display_label || summary.primary.external_account_id || summary.primary.account_key}</strong>
          </p>
        ) : (
          <p className="hint">No primary account is marked yet.</p>
        )}
      </div>

      <div className="accounts-grid">
        {accountWorkspaces.map((account) => (
          <AccountWorkspaceCard
            key={account.account_key}
            account={account}
            isSelected={selectedAccount?.account_key === account.account_key}
            onSelect={onSelectAccount}
          />
        ))}
      </div>
    </section>
  )
}

export default AccountsOverviewPage
