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

  assert.equal(landingSource.includes('Account readiness snapshot'), true)
  assert.equal(landingSource.includes('Retry workspace load'), true)

  assert.equal(accountsSource.includes('Current state:</strong> pending-only workspace'), true)
  assert.equal(accountsSource.includes('Current state:</strong> stale/inactive only.'), true)
  assert.equal(accountsSource.includes('button type="button" className="secondary-button" onClick={onRefreshWorkspace}'), true)

  assert.equal(connectionsSource.includes('Connected connectors'), true)
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

  assert.equal(appSource.includes("navigate('/app/accounts')"), true)
  assert.equal(appSource.includes('routeRefreshGuardRef'), true)
  assert.equal(appSource.includes("void loadConnectorData({ silent: true })"), true)
})
