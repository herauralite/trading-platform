import { NavLink } from 'react-router-dom'
import AccountStatusBadge from './AccountStatusBadge'
import { connectionStatusMeta } from './accountStatusMeta'

function formatTimestamp(value) {
  if (!value) return 'Unavailable'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return 'Unavailable'
  return date.toLocaleString()
}

function valueOrUnavailable(value, fallback = 'Unavailable') {
  return value == null || value === '' ? fallback : value
}

function accountIdentityRows(account) {
  return [
    ['Account key', account.account_key],
    ['External account ID', valueOrUnavailable(account.external_account_id)],
    ['Trading account ID', valueOrUnavailable(account.trading_account_id)],
    ['Account type', valueOrUnavailable(account.account_type)],
    ['Environment', account.environment ? String(account.environment).toUpperCase() : 'Unavailable'],
    ['Provider state', account.provider_state ? String(account.provider_state).replace(/_/g, ' ') : 'Unavailable'],
  ]
}

function statusClassCopy(accountState) {
  if (accountState === 'usable') return { label: 'Usable', helper: 'This account can be active workspace context right now.' }
  if (accountState === 'pending') return { label: 'Pending', helper: 'Setup is in progress. Actions stay limited until the connection is usable.' }
  return { label: 'Stale', helper: 'Historical/disconnected record. Reconnect first to restore active workspace use.' }
}

function AccountDetailPanel({
  account,
  accountState,
  isSelected,
  onSetActive,
  onRefreshWorkspace,
}) {
  if (!account) {
    return (
      <aside className="card account-detail-panel account-detail-panel-empty">
        <h3>Account details</h3>
        <p className="hint">Select an account card to see provider, sync, and activity detail here.</p>
      </aside>
    )
  }

  const connectionMeta = connectionStatusMeta(account.connection_status)
  const stateClass = statusClassCopy(accountState)
  const canSetActive = accountState === 'usable' && !isSelected

  return (
    <aside className="card account-detail-panel premium-focus-card" aria-live="polite">
      <div className="row">
        <h3>Account details</h3>
        {isSelected ? <span className="pill primary-pill">Active account</span> : null}
      </div>
      <p>
        <strong>{account.display_label || account.external_account_id || account.account_key}</strong>
      </p>
      <div className="row">
        <span className="pill">Provider {valueOrUnavailable(account.source_label || account.connector_type)}</span>
        <span className="pill">Connection class: {stateClass.label}</span>
        {account.is_primary ? <span className="pill">Primary account</span> : null}
      </div>
      <p className="hint">{stateClass.helper}</p>

      <div className="meta-grid">
        <div className="meta-card">
          <span className="hint">Connection state</span>
          <AccountStatusBadge value={account.connection_status} />
          <small className="hint">{connectionMeta.helper}</small>
        </div>
        <div className="meta-card">
          <span className="hint">Sync state</span>
          <AccountStatusBadge variant="sync" value={account.sync_state} />
          <small className="hint">Workspace-reported sync state.</small>
        </div>
        <div className="meta-card">
          <span className="hint">Last synced</span>
          <strong>{formatTimestamp(account.last_sync_at)}</strong>
        </div>
        <div className="meta-card">
          <span className="hint">Last heartbeat / activity</span>
          <strong>{formatTimestamp(account.last_activity_at)}</strong>
        </div>
      </div>

      <div className="card account-detail-identity-card">
        <h4>Safe account identity</h4>
        <ul className="connector-account-list">
          {accountIdentityRows(account).map(([label, value]) => (
            <li key={label}>
              <span>{label}</span>
              <span className="mono">{value}</span>
            </li>
          ))}
        </ul>
      </div>

      <div className="row">
        <button type="button" className={canSetActive ? 'primary-cta' : 'secondary-button'} disabled={!canSetActive} onClick={() => onSetActive(account.account_key)}>
          {isSelected ? 'Current Active Account' : accountState === 'usable' ? 'Set Active Account' : 'Not eligible for active workspace'}
        </button>
        <NavLink className="app-nav-link" to="/app/connections">Open connections</NavLink>
        <button type="button" className="secondary-button" onClick={onRefreshWorkspace}>Refresh Workspace</button>
      </div>
    </aside>
  )
}

export default AccountDetailPanel
