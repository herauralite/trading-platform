import AccountStatusBadge from './AccountStatusBadge'
import { connectionStatusMeta } from './accountStatusMeta'

function formatTimestamp(value) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  return date.toLocaleString()
}

function AccountWorkspaceCard({ account, isSelected, onSelect }) {
  const connectionMeta = connectionStatusMeta(account.connection_status)
  const displayName = account.display_label || account.external_account_id || account.account_key

  return (
    <article className={`account-card${isSelected ? ' selected' : ''}`}>
      <div className="row account-card-top">
        <div>
          <h3>{displayName}</h3>
          <p className="hint mono">{account.account_key}</p>
        </div>
        <div className="row">
          {account.is_primary ? <span className="pill primary-pill">Primary</span> : null}
          {isSelected ? <span className="pill">Active Context</span> : null}
        </div>
      </div>

      <div className="row">
        <span className="pill">{account.source_label}</span>
        <span className="pill">{account.broker_name || 'Unknown broker'}</span>
        {account.account_type ? <span className="pill">{account.account_type}</span> : null}
      </div>

      <div className="account-card-grid">
        <div className="meta-card">
          <span className="hint">Connection status</span>
          <AccountStatusBadge value={account.connection_status} />
          <small className="hint">{connectionMeta.helper}</small>
        </div>
        <div className="meta-card">
          <span className="hint">Sync status</span>
          <AccountStatusBadge variant="sync" value={account.sync_state} />
          <small className="hint">Sync status is presented from workspace rollup semantics.</small>
        </div>
        <div className="meta-card">
          <span className="hint">Last sync</span>
          <strong>{formatTimestamp(account.last_sync_at)}</strong>
        </div>
        <div className="meta-card">
          <span className="hint">Last activity</span>
          <strong>{formatTimestamp(account.last_activity_at)}</strong>
        </div>
      </div>

      <button type="button" onClick={() => onSelect(account.account_key)}>
        {isSelected ? 'Selected' : 'Open account context'}
      </button>
    </article>
  )
}

export default AccountWorkspaceCard
