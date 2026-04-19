import { NavLink } from 'react-router-dom'
import AccountStatusBadge from './AccountStatusBadge'
import { connectionStatusMeta } from './accountStatusMeta'

function formatTimestamp(value) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  return date.toLocaleString()
}

function accountIdentityRows(account) {
  const rows = [
    ['Account key', account.account_key],
    ['External account ID', account.external_account_id],
    ['Trading account ID', account.trading_account_id],
    ['Account type', account.account_type],
    ['Environment', account.environment ? String(account.environment).toUpperCase() : null],
    ['Provider state', account.provider_state ? String(account.provider_state).replace(/_/g, ' ') : null],
  ]
  return rows.filter(([, value]) => value != null && value !== '')
}

function AccountDetailPanel({
  account,
  accountState,
  isSelected,
  onSetActive,
  onRefreshWorkspace,
  onClose,
}) {
  if (!account) return null

  const connectionMeta = connectionStatusMeta(account.connection_status)
  const stateClassLabel = accountState === 'usable' ? 'Usable' : accountState === 'pending' ? 'Pending' : 'Stale'
  const canSetActive = accountState === 'usable' && !isSelected

  return (
    <div className="account-detail-overlay" role="presentation" onClick={onClose}>
      <aside className="card account-detail-drawer" role="dialog" aria-modal="true" onClick={(event) => event.stopPropagation()}>
        <div className="row">
          <h3>Account details</h3>
          <button type="button" className="secondary-button" onClick={onClose}>Close</button>
        </div>
        <p>
          <strong>{account.display_label || account.external_account_id || account.account_key}</strong>
          {isSelected ? <span className="pill primary-pill">Active workspace account</span> : null}
        </p>
        <div className="row">
          <span className="pill">{account.source_label || account.connector_type || 'Unknown provider'}</span>
          <span className="pill">Connector {account.connector_type || 'unknown'}</span>
          <span className="pill">State class: {stateClassLabel}</span>
          {account.is_primary ? <span className="pill">Primary account</span> : null}
        </div>

        <div className="meta-grid">
          <div className="meta-card">
            <span className="hint">Connection status</span>
            <AccountStatusBadge value={account.connection_status} />
            <small className="hint">{connectionMeta.helper}</small>
          </div>
          <div className="meta-card">
            <span className="hint">Sync state</span>
            <AccountStatusBadge variant="sync" value={account.sync_state} />
            <small className="hint">Workspace-reported sync state.</small>
          </div>
          <div className="meta-card">
            <span className="hint">Last sync</span>
            <strong>{formatTimestamp(account.last_sync_at)}</strong>
          </div>
          <div className="meta-card">
            <span className="hint">Last heartbeat/activity</span>
            <strong>{formatTimestamp(account.last_activity_at)}</strong>
          </div>
          <div className="meta-card">
            <span className="hint">Last validated</span>
            <strong>{formatTimestamp(account.last_validated_at)}</strong>
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
            {isSelected ? 'Current Active Account' : 'Set Active Account'}
          </button>
          <NavLink className="app-nav-link" to="/app/connections">Go to Connections</NavLink>
          <button type="button" className="secondary-button" onClick={onRefreshWorkspace}>Refresh Workspace</button>
        </div>
      </aside>
    </div>
  )
}

export default AccountDetailPanel
