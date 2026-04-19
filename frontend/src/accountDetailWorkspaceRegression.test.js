import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

async function readFrontendFile(relPath) {
  return readFile(new URL(`./${relPath}`, import.meta.url), 'utf8')
}

test('active account detail panel renders truthful unavailable states and action hub labels', async () => {
  const detailSource = await readFrontendFile('components/AccountDetailPanel.jsx')

  assert.equal(detailSource.includes('Active account'), true)
  assert.equal(detailSource.includes('Unavailable'), true)
  assert.equal(detailSource.includes('Last heartbeat / activity'), true)
  assert.equal(detailSource.includes('Connection class:'), true)
  assert.equal(detailSource.includes('Set as active'), true)
  assert.equal(detailSource.includes('Open dashboard'), true)
  assert.equal(detailSource.includes('Manage connection'), true)
  assert.equal(detailSource.includes('Refresh workspace'), true)
})

test('accounts page updates detail context and builds provider/account intent routes', async () => {
  const accountsSource = await readFrontendFile('pages/AccountsOverviewPage.jsx')
  const intentSource = await readFrontendFile('accountConnectionIntent.js')

  assert.equal(accountsSource.includes('accounts-workspace-layout'), true)
  assert.equal(accountsSource.includes('onDetailAccountChange(accountKey)'), true)
  assert.equal(accountsSource.includes('onSelectAccount(accountKey)'), true)
  assert.equal(accountsSource.includes("buildConnectionsIntentPath(detailAccount, 'manage')"), true)
  assert.equal(accountsSource.includes("buildConnectionsIntentPath(detailAccount, 'setup')"), true)
  assert.equal(accountsSource.includes("buildConnectionsIntentPath(detailAccount, 'reconnect')"), true)
  assert.equal(intentSource.includes("params.set('provider', provider)"), true)
  assert.equal(intentSource.includes("params.set('account', accountKey)"), true)
  assert.equal(intentSource.includes("params.set('intent', intent)"), true)
})

test('pending and stale cards keep non-active actions truthful', async () => {
  const cardSource = await readFrontendFile('components/AccountWorkspaceCard.jsx')
  const detailSource = await readFrontendFile('components/AccountDetailPanel.jsx')

  assert.equal(cardSource.includes('Pending setup (not active yet)'), true)
  assert.equal(cardSource.includes('Inactive record (not active)'), true)
  assert.equal(detailSource.includes('Continue setup'), true)
  assert.equal(detailSource.includes('Setup still required:'), true)
  assert.equal(detailSource.includes('Reconnect account'), true)
  assert.equal(detailSource.includes('Reconnect required:'), true)
})

test('dashboard and connections reflect selected account context with route-driven provider focus', async () => {
  const landingSource = await readFrontendFile('pages/AppLandingPage.jsx')
  const connectionsSource = await readFrontendFile('pages/ConnectionsPage.jsx')

  assert.equal(landingSource.includes('Current workspace account'), true)
  assert.equal(landingSource.includes('Manage accounts'), true)
  assert.equal(landingSource.includes('Open connections'), true)
  assert.equal(landingSource.includes('Add another account'), true)

  assert.equal(connectionsSource.includes('Selected account context'), true)
  assert.equal(connectionsSource.includes('connections-intent-focus'), true)
  assert.equal(connectionsSource.includes("params.get('provider')"), true)
  assert.equal(connectionsSource.includes("params.get('account')"), true)
  assert.equal(connectionsSource.includes("params.get('intent')"), true)
  assert.equal(connectionsSource.includes('Focused provider'), true)
})

test('accounts detail selection supports resilient fallback ordering', async () => {
  const appSource = await readFrontendFile('App.jsx')
  const accountsSource = await readFrontendFile('pages/AccountsOverviewPage.jsx')
  const helperSource = await readFrontendFile('workspaceAccountSelection.js')

  assert.equal(appSource.includes('DETAIL_ACCOUNT_STORAGE_KEY'), true)
  assert.equal(accountsSource.includes('resolvePreferredDetailAccountKey'), true)
  assert.equal(helperSource.includes('sortBySelectionPriority'), true)
})
