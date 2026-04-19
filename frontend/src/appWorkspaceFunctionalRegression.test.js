import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { deriveAccountConnectionState } from './accountConnectionState.js'

async function readFrontendFile(relPath) {
  return readFile(new URL(`./${relPath}`, import.meta.url), 'utf8')
}

test('dashboard, accounts, and connections keep functional workspace summaries and refresh entry points', async () => {
  const landingSource = await readFrontendFile('pages/AppLandingPage.jsx')
  const accountsSource = await readFrontendFile('pages/AccountsOverviewPage.jsx')
  const connectionsSource = await readFrontendFile('pages/ConnectionsPage.jsx')
  const cardSource = await readFrontendFile('components/AccountWorkspaceCard.jsx')
  const detailSource = await readFrontendFile('components/AccountDetailPanel.jsx')

  assert.equal(landingSource.includes('Current workspace account'), true)
  assert.equal(landingSource.includes('What to do next'), true)
  assert.equal(landingSource.includes('No active usable account selected.'), true)
  assert.equal(landingSource.includes('Retry workspace load'), true)

  assert.equal(accountsSource.includes('Current active account'), true)
  assert.equal(accountsSource.includes('accounts-workspace-layout'), true)
  assert.equal(accountsSource.includes('No active usable account is selected right now.'), true)
  assert.equal(accountsSource.includes('only usable connected accounts can become active workspace context'), true)
  assert.equal(accountsSource.includes('Current state:</strong> pending-only workspace'), true)
  assert.equal(accountsSource.includes('Current state:</strong> stale/inactive only.'), true)
  assert.equal(accountsSource.includes('button type="button" className="secondary-button" onClick={onRefreshWorkspace}'), true)

  assert.equal(cardSource.includes('Pending setup (not active yet)'), true)
  assert.equal(cardSource.includes('Inactive record (not active)'), true)
  assert.equal(cardSource.includes('View details'), true)
  assert.equal(cardSource.includes('disabled={!canSetActive}'), true)

  assert.equal(detailSource.includes('Connection class:'), true)
  assert.equal(detailSource.includes('Open connections'), true)
  assert.equal(detailSource.includes('Refresh workspace'), true)

  assert.equal(connectionsSource.includes('Connected connectors'), true)
  assert.equal(connectionsSource.includes('Selected account provider'), true)
  assert.equal(connectionsSource.includes('Selected account context'), true)
  assert.equal(connectionsSource.includes('Recommended next action:'), true)
  assert.equal(connectionsSource.includes('provider-priority-card'), true)
  assert.equal(connectionsSource.includes('Needs attention'), true)
  assert.equal(connectionsSource.includes('button type="button" className="secondary-button" onClick={onRefreshWorkspace}'), true)
})

test('stale-only and pending-only account states remain non-usable', () => {
  const staleOnly = deriveAccountConnectionState([
    { account_key: 'a', connection_status: 'disconnected', sync_state: 'idle' },
    { account_key: 'b', connection_status: 'validation_failed', sync_state: 'failed' },
  ])
  assert.equal(staleOnly.connectedUsableCount, 0)
  assert.equal(staleOnly.staleInactiveCount, 2)

  const pendingOnly = deriveAccountConnectionState([
    { account_key: 'c', connection_status: 'waiting_for_registration', sync_state: 'idle' },
    { account_key: 'd', connection_status: 'disconnected', sync_state: 'queued' },
  ])
  assert.equal(pendingOnly.connectedUsableCount, 0)
  assert.equal(pendingOnly.pendingOnlyCount, 2)
  assert.equal(pendingOnly.hasZeroConnectedAccounts, true)
})

test('post-add-account flow routes users to accounts workspace and triggers route hydration', async () => {
  const appSource = await readFrontendFile('App.jsx')

  assert.equal(appSource.includes("setAddAccountReturnPath('/app/accounts')"), true)
  assert.equal(appSource.includes("navigate(addAccountReturnPath || '/app/accounts')"), true)
  assert.equal(appSource.includes('routeRefreshGuardRef'), true)
  assert.equal(appSource.includes("void loadConnectorData({ silent: true })"), true)
  assert.equal(appSource.includes('if (existingSelected && isCurrentlyConnectedAccount(existingSelected)) return'), true)
  assert.equal(appSource.includes('if (isCurrentlyConnectedAccount(matched)) setSelectedAccountKey(matched.account_key)'), true)
})
