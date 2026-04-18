function accountOptionLabel(account) {
  const name = account.display_label || account.external_account_id || account.account_key
  const broker = account.broker_name || account.source_label || 'Unknown broker'
  const primarySuffix = account.is_primary ? ' · Primary' : ''
  return `${name} (${broker})${primarySuffix}`
}

function AccountSwitcher({
  accounts,
  selectedAccountKey,
  onSelectAccount,
}) {
  const hasAccounts = accounts.length > 0

  return (
    <div className="account-switcher">
      <label htmlFor="account-switcher-select">Active account</label>
      <select
        id="account-switcher-select"
        value={selectedAccountKey}
        onChange={(event) => onSelectAccount(event.target.value)}
        disabled={!hasAccounts}
      >
        {!hasAccounts ? <option value="">No usable or pending accounts yet</option> : null}
        {accounts.map((account) => (
          <option key={account.account_key} value={account.account_key}>
            {accountOptionLabel(account)}
          </option>
        ))}
      </select>
    </div>
  )
}

export default AccountSwitcher
