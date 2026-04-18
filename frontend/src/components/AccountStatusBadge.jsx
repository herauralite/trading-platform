import { connectionStatusMeta, syncStateMeta } from './accountStatusMeta'

function AccountStatusBadge({ variant, value }) {
  const meta = variant === 'sync' ? syncStateMeta(value) : connectionStatusMeta(value)
  return <span className={`badge ${meta.toneClass}`}>{meta.label}</span>
}

export default AccountStatusBadge
