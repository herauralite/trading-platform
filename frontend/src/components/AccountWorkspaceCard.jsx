import AccountStatusBadge from './AccountStatusBadge'
import { connectionStatusMeta } from './accountStatusMeta'

function formatTimestamp(value) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  return date.toLocaleString()
}

function displaySourceLabel(account) {
  if (account?.connector_type === 'tradelocker_api') return 'TradeLocker API'
  return account?.source_label || account?.connector_type || 'Unknown source'
}

function relativeHealth(account) {
  const now = Date.now()
  const lastSync = account?.last_sync_at ? new Date(account.last_sync_at).getTime() : null
  const lastActivity = account?.last_activity_at ? new Date(account.last_activity_at).getTime() : null

  if (String(account?.sync_state || '').toLowerCase() === 'failed' || String(account?.connection_status || '').toLowerCase() === 'sync_error') {
    return 'Needs attention: recent sync error.'
  }
  if (String(account?.sync_state || '').toLowerCase() === 'retrying') {
    return 'Retrying latest sync failure.'
  }
  if (String(account?.connection_status || '').toLowerCase() === 'validation_failed') {
    return 'Validation failed: re-check credentials/config.'
  }
  if (String(account?.provider_state || '').toLowerCase().includes('auth')) {
    return 'Auth state changed: verify connector session.'
  }
  if (lastSync && now - lastSync <= 24 * 60 * 60 * 1000) {
    return 'Recently synced.'
  }
  if (lastSync && now - lastSync > 7 * 24 * 60 * 60 * 1000) {
    return 'Sync looks stale.'
  }
  if (!lastActivity) {
    return 'Awaiting first activity.'
  }
  if (lastActivity && now - lastActivity > 14 * 24 * 60 * 60 * 1000) {
    return 'Inactive recently.'
  }
  return 'Operational and active.'
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
        <span className="pill">{displaySourceLabel(account)}</span>
        <span className="pill">{account.broker_name || 'Unknown broker'}</span>
        {account.account_type ? <span className="pill">{account.account_type}</span> : null}
        {account.provider_state ? <span className="pill">{account.provider_state}</span> : null}
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

      <p className="hint">{relativeHealth(account)}</p>

      <button type="button" onClick={() => onSelect(account.account_key)}>
        {isSelected ? 'Selected' : 'Open account context'}
      </button>
    </article>
  )
}

export default AccountWorkspaceCard
