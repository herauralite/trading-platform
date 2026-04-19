import { useMemo } from 'react'
import AccountStatusBadge from '../components/AccountStatusBadge'
import AccountWorkspaceCard from '../components/AccountWorkspaceCard'
import { deriveAccountConnectionState, isCurrentlyConnectedAccount, isPendingOnlyAccount } from '../accountConnectionState'

function countBy(items, predicate) {
  return items.reduce((count, item) => (predicate(item) ? count + 1 : count), 0)
}

function AccountsOverviewPage({
  signedIn,
  accountWorkspaces,
  selectedAccount,
  onSelectAccount,
  onAddAccount,
  recentlyAddedAccountLabel,
  formatDate,
  isWorkspaceLoading,
  onRefreshWorkspace,
}) {
  const summary = useMemo(() => {
    const connectionState = deriveAccountConnectionState(accountWorkspaces)
    const usableAccounts = accountWorkspaces.filter((account) => isCurrentlyConnectedAccount(account))
    const pendingAccounts = accountWorkspaces.filter((account) => !isCurrentlyConnectedAccount(account) && isPendingOnlyAccount(account))
    const staleAccounts = accountWorkspaces.filter((account) => !isCurrentlyConnectedAccount(account) && !isPendingOnlyAccount(account))
    const attention = countBy(accountWorkspaces, (account) => account.connection_status === 'sync_error' || account.sync_state === 'failed')
    const syncing = countBy(accountWorkspaces, (account) => ['queued', 'running', 'retrying'].includes(account.sync_state))
    const primary = usableAccounts.find((account) => account.is_primary) || null
    return {
      total: connectionState.totalCount,
      connected: connectionState.connectedUsableCount,
      pendingOnly: connectionState.pendingOnlyCount,
      staleInactive: connectionState.staleInactiveCount,
      attention,
      syncing,
      primary,
      hasZeroConnectedAccounts: connectionState.hasZeroConnectedAccounts,
      usableAccounts,
      pendingAccounts,
      staleAccounts,
    }
  }, [accountWorkspaces])

  if (!signedIn) {
    return (
      <section className="panel page-panel premium-workspace-panel accounts-page">
        <div className="panel-header row">
          <div>
            <p className="kicker">Accounts</p>
            <h2>Accounts</h2>
          </div>
          <button type="button" disabled onClick={() => onAddAccount('mt5_bridge')}>Add Account</button>
        </div>
        <p className="hint">
          Sign in with Telegram from the app shell to manage account cards, active account selection, and provider-linked workspace data.
        </p>
      </section>
    )
  }

  if (isWorkspaceLoading) {
    return (
      <section className="panel page-panel premium-workspace-panel accounts-page">
        <div className="panel-header row">
          <h2>Accounts</h2>
          <button type="button" disabled>Add Account</button>
        </div>
        <div className="skeleton-grid">
          <div className="skeleton-card" />
          <div className="skeleton-card" />
          <div className="skeleton-card" />
        </div>
      </section>
    )
  }

  if (summary.hasZeroConnectedAccounts) {
    const hasPendingOnly = summary.pendingAccounts.length > 0 && summary.staleAccounts.length === 0
    const hasStaleOnly = summary.pendingAccounts.length === 0 && summary.staleAccounts.length > 0
    return (
      <section className="panel page-panel premium-workspace-panel accounts-page">
        <div className="panel-header row">
          <h2>Accounts</h2>
          <div className="row">
            <button type="button" className="secondary-button" onClick={onRefreshWorkspace}>Refresh</button>
            <button type="button" className="primary-cta" onClick={() => onAddAccount('mt5_bridge')}>Add Account</button>
          </div>
        </div>
        <div className="empty-state account-onboarding-empty-state">
          <h3>Connect your first trading account</h3>
          <p className="empty-state-copy">
            Accounts is the primary place to attach providers and shape your real workspace identity.
          </p>
          <ul className="onboarding-path-list">
            <li><strong>MT5 Bridge</strong> onboarding</li>
            <li><strong>FundingPips Extension</strong> attach flow</li>
            <li><strong>TradingView Webhook</strong> signal intake</li>
            <li><strong>CSV Import</strong> account history import</li>
            <li><strong>Manual Journal</strong> account + trade recording</li>
          </ul>
          <button type="button" className="primary-cta" onClick={() => onAddAccount('mt5_bridge')}>Add your first account</button>
          <p className="hint">Inventory detected: {summary.total} rows · pending-only: {summary.pendingOnly} · stale/inactive: {summary.staleInactive}.</p>
          {hasPendingOnly ? <p className="hint"><strong>Current state:</strong> pending-only workspace. Finish connector setup to make an account usable.</p> : null}
          {hasStaleOnly ? <p className="hint"><strong>Current state:</strong> stale/inactive only. Reconnect a provider to restore an actively usable account.</p> : null}
        </div>
        {summary.pendingAccounts.length > 0 ? (
          <div className="card">
            <h3>Pending setup accounts</h3>
            <p className="hint">These records are tracked but are not yet usable connected accounts.</p>
            <div className="accounts-grid">
              {summary.pendingAccounts.map((account) => (
                <AccountWorkspaceCard
                  key={account.account_key}
                  account={account}
                  isSelected={selectedAccount?.account_key === account.account_key}
                  onSelect={onSelectAccount}
                />
              ))}
            </div>
          </div>
        ) : null}
        {summary.staleAccounts.length > 0 ? (
          <div className="card">
            <h3>Historical / disconnected records</h3>
            <p className="hint">Shown for history and audit context only. These are intentionally not counted as active connected workspace accounts.</p>
            <ul className="connector-account-list">
              {summary.staleAccounts.map((account) => (
                <li key={`stale-onboarding-${account.account_key}`}>
                  <span>{account.display_label || account.external_account_id || account.account_key}</span>
                  <span className="pill">{account.source_label || account.connector_type || 'Unknown source'}</span>
                  <span className="pill">{account.connection_status || 'disconnected'}</span>
                  <span className="hint">Last sync {formatDate(account.last_sync_at)} · Updated {formatDate(account.last_activity_at)}</span>
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </section>
    )
  }

  return (
    <section className="panel page-panel premium-workspace-panel accounts-page">
      <div className="panel-header row">
        <div>
          <p className="kicker">Accounts</p>
          <h2>Manage connected accounts</h2>
        </div>
        <div className="row">
          <button type="button" className="secondary-button" onClick={onRefreshWorkspace}>Refresh</button>
          <button type="button" className="primary-cta" onClick={() => onAddAccount('mt5_bridge')}>Add Account</button>
        </div>
      </div>
      <p className="hint">This workspace shows account health, broker source, sync freshness, and active account context.</p>

      {recentlyAddedAccountLabel ? (
        <div className="card add-success-banner premium-success-banner">
          <strong>Account added.</strong>
          <p className="hint">Focused account: <strong>{recentlyAddedAccountLabel}</strong>.</p>
        </div>
      ) : null}
      <div className="meta-grid accounts-summary-grid premium-summary-grid accounts-summary-premium-grid">
        <div className="meta-card summary-card">
          <span className="hint">All accounts</span>
          <strong>{summary.total}</strong>
        </div>
        <div className="meta-card summary-card">
          <span className="hint">Healthy</span>
          <strong>{summary.connected}</strong>
        </div>
        <div className="meta-card summary-card">
          <span className="hint">Syncing</span>
          <strong>{summary.syncing}</strong>
        </div>
        <div className="meta-card summary-card">
          <span className="hint">Needs attention</span>
          <strong>{summary.attention}</strong>
        </div>
      </div>

      <div className="card selected-account-panel premium-focus-card accounts-focus-panel">
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
              <span className="pill">{selectedAccount.broker_name || 'Broker not yet available'}</span>
              {selectedAccount.environment ? <span className="pill">{String(selectedAccount.environment).toUpperCase()}</span> : null}
              {selectedAccount.provider_state ? <span className="pill">{String(selectedAccount.provider_state).replace(/_/g, ' ')}</span> : null}
              <span className="hint">Connection</span>
              <AccountStatusBadge value={selectedAccount.connection_status} />
              <span className="hint">Sync</span>
              <AccountStatusBadge variant="sync" value={selectedAccount.sync_state} />
            </div>
            <p className="hint">Last activity: {formatDate(selectedAccount.last_activity_at)} · Last sync: {formatDate(selectedAccount.last_sync_at)}</p>
            <div className="meta-grid">
              <div className="meta-card">
                <span className="hint">Connection health</span>
                <strong>{selectedAccount.connection_status || 'disconnected'}</strong>
              </div>
              <div className="meta-card">
                <span className="hint">Validation</span>
                <strong>{selectedAccount.last_validated_at ? 'Verified' : 'Pending'}</strong>
              </div>
              <div className="meta-card">
                <span className="hint">Last validated</span>
                <strong>{formatDate(selectedAccount.last_validated_at)}</strong>
              </div>
              <div className="meta-card">
                <span className="hint">Environment</span>
                <strong>{selectedAccount.environment ? String(selectedAccount.environment).toUpperCase() : '—'}</strong>
              </div>
              {selectedAccount.account_summary?.equity != null ? (
                <div className="meta-card">
                  <span className="hint">Equity</span>
                  <strong>{selectedAccount.account_summary.equity}</strong>
                </div>
              ) : null}
              {selectedAccount.account_summary?.buying_power != null ? (
                <div className="meta-card">
                  <span className="hint">Buying power</span>
                  <strong>{selectedAccount.account_summary.buying_power}</strong>
                </div>
              ) : null}
              {selectedAccount.account_summary?.cash != null ? (
                <div className="meta-card">
                  <span className="hint">Cash</span>
                  <strong>{selectedAccount.account_summary.cash}</strong>
                </div>
              ) : null}
              {selectedAccount.account_summary?.portfolio_value != null ? (
                <div className="meta-card">
                  <span className="hint">Portfolio value</span>
                  <strong>{selectedAccount.account_summary.portfolio_value}</strong>
                </div>
              ) : null}
            </div>
          </>
        ) : (
          <p className="hint">Select an account to establish workspace focus.</p>
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
        {summary.usableAccounts.map((account) => (
          <AccountWorkspaceCard
            key={account.account_key}
            account={account}
            isSelected={selectedAccount?.account_key === account.account_key}
            onSelect={onSelectAccount}
          />
        ))}
      </div>
      {summary.pendingAccounts.length > 0 ? (
        <div className="card">
          <h3>Pending setup accounts</h3>
          <p className="hint">These accounts exist but still require connector setup or sync completion before becoming fully usable.</p>
          <div className="accounts-grid">
            {summary.pendingAccounts.map((account) => (
              <AccountWorkspaceCard
                key={account.account_key}
                account={account}
                isSelected={selectedAccount?.account_key === account.account_key}
                onSelect={onSelectAccount}
              />
            ))}
          </div>
        </div>
      ) : null}
      {summary.staleAccounts.length > 0 ? (
        <div className="card">
          <h3>Historical / disconnected records</h3>
          <p className="hint">These rows are shown for audit context and are intentionally not treated as active account presence.</p>
          <ul className="connector-account-list">
            {summary.staleAccounts.map((account) => (
              <li key={`stale-${account.account_key}`}>
                <span>{account.display_label || account.external_account_id || account.account_key}</span>
                <span className="pill">{account.connection_status || 'disconnected'}</span>
                <span className="hint">Last sync {formatDate(account.last_sync_at)}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  )
}

export default AccountsOverviewPage
