import { NavLink } from 'react-router-dom'

function AppLandingPage({ signedIn, hasZeroConnectedAccounts, accountConnectionState, onAddAccount }) {
  if (!signedIn) {
    return (
      <section className="panel app-dashboard-hub">
        <div className="row">
          <h2>Workspace Dashboard</h2>
        </div>
        <p className="hint">
          You are viewing the premium app shell. Sign in with Telegram above to add accounts and unlock account-linked workflows.
        </p>
        <div className="row app-onboarding-links">
          <NavLink className="app-nav-link" to="/app/accounts">Open Accounts</NavLink>
          <NavLink className="app-nav-link" to="/app/connections">Open Connections</NavLink>
        </div>
      </section>
    )
  }

  if (hasZeroConnectedAccounts) {
    return (
      <section className="panel app-onboarding-hub">
        <div className="row">
          <h2>Add your first account</h2>
          <button type="button" className="primary-cta" onClick={onAddAccount}>Add Account</button>
        </div>
        <p className="hint">
          TaliTrade is account-centric and multi-broker. Start by adding a usable account, then manage provider setup in <strong>Connections</strong>.
        </p>
        <ul className="onboarding-path-list">
          <li><strong>MT5</strong> bridge onboarding</li>
          <li><strong>FundingPips Extension</strong> account attach flow</li>
          <li><strong>TradingView Webhook</strong> signal account routing</li>
          <li><strong>CSV Import</strong> historical trades</li>
          <li><strong>Manual Journal</strong> account/trade entry</li>
          <li><strong>Future broker/API connectors</strong> as providers are added</li>
        </ul>
        <div className="row app-onboarding-links">
          <NavLink className="app-nav-link" to="/app/accounts">Go to Accounts</NavLink>
          <NavLink className="app-nav-link" to="/app/connections">Go to Connections</NavLink>
        </div>
        <p className="hint">
          Current usable accounts: {accountConnectionState.connectedUsableCount}. Pending-only rows: {accountConnectionState.pendingOnlyCount}. Inactive/stale rows: {accountConnectionState.staleInactiveCount}.
        </p>
      </section>
    )
  }

  return (
    <section className="panel app-dashboard-hub">
      <div className="row">
        <h2>Workspace Dashboard</h2>
        <button type="button" className="primary-cta" onClick={onAddAccount}>Add Account</button>
      </div>
      <p className="hint">
        You already have usable accounts connected. Continue in <strong>Accounts</strong> for workspace context or <strong>Connections</strong> for integration operations.
      </p>
      <div className="row app-onboarding-links">
        <NavLink className="app-nav-link" to="/app/accounts">Open Accounts</NavLink>
        <NavLink className="app-nav-link" to="/app/connections">Open Connections</NavLink>
      </div>
    </section>
  )
}

export default AppLandingPage
