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
        {!hasAccounts ? <option value="">No accounts yet</option> : null}
        {accounts.map((account) => (
          <option key={account.accountKey} value={account.accountKey}>
            {account.displayLabel} ({account.sourceLabel})
          </option>
        ))}
      </select>
    </div>
  )
}

export default AccountSwitcher
