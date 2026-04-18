function AccountsOverviewPage({
  accountWorkspaces,
  selectedAccount,
  onSelectAccount,
}) {
  return (
    <section className="panel">
      <h2>Accounts Workspace</h2>
      <p className="hint">
        Accounts are now the primary /app workspace surface. This Phase 1 view is read-only and uses existing connector-derived data.
      </p>

      <div className="meta-grid">
        <div className="meta-card">
          <span className="hint">Total accounts</span>
          <strong>{accountWorkspaces.length}</strong>
        </div>
        <div className="meta-card">
          <span className="hint">Selected account</span>
          <strong>{selectedAccount ? selectedAccount.displayLabel : 'None selected'}</strong>
        </div>
      </div>

      {selectedAccount ? (
        <div className="card">
          <h3>Selected account placeholder</h3>
          <p>
            <strong>{selectedAccount.displayLabel}</strong>
            {' · '}
            <span className="pill">{selectedAccount.sourceLabel}</span>
            <span className="pill">{selectedAccount.brokerName || 'Unknown broker'}</span>
          </p>
          <p className="hint">
            Future phases will mount account-level analytics, risk, and activity keyed by canonical account identity.
          </p>
        </div>
      ) : (
        <p className="hint">Connect a source in Connections to populate account workspaces.</p>
      )}

      <h3>Current accounts</h3>
      {accountWorkspaces.length === 0 ? (
        <p className="hint">No accounts yet. Use Connections to add or sync accounts.</p>
      ) : (
        <ul>
          {accountWorkspaces.map((account) => (
            <li key={account.accountKey}>
              <button type="button" onClick={() => onSelectAccount(account.accountKey)}>
                {account.displayLabel}
              </button>
              <span className="pill">{account.sourceLabel}</span>
              <span className="pill">{account.brokerName || 'Unknown broker'}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

export default AccountsOverviewPage
