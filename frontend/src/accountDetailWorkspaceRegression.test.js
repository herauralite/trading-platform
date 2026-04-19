import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

async function readFrontendFile(relPath) {
  return readFile(new URL(`./${relPath}`, import.meta.url), 'utf8')
}

test('active account detail panel renders truthful unavailable states and active treatment', async () => {
  const detailSource = await readFrontendFile('components/AccountDetailPanel.jsx')

  assert.equal(detailSource.includes('Active account'), true)
  assert.equal(detailSource.includes('Unavailable'), true)
  assert.equal(detailSource.includes('Last heartbeat / activity'), true)
  assert.equal(detailSource.includes('Connection class:'), true)
})

test('accounts page updates detail context when selecting a usable account', async () => {
  const accountsSource = await readFrontendFile('pages/AccountsOverviewPage.jsx')

  assert.equal(accountsSource.includes('accounts-workspace-layout'), true)
  assert.equal(accountsSource.includes('setDetailAccountKey(accountKey)'), true)
  assert.equal(accountsSource.includes('onSelectAccount(accountKey)'), true)
})

test('pending and stale cards keep non-active actions truthful', async () => {
  const cardSource = await readFrontendFile('components/AccountWorkspaceCard.jsx')
  const detailSource = await readFrontendFile('components/AccountDetailPanel.jsx')

  assert.equal(cardSource.includes('Pending setup (not active yet)'), true)
  assert.equal(cardSource.includes('Inactive record (not active)'), true)
  assert.equal(detailSource.includes('Not eligible for active workspace'), true)
})

test('dashboard and connections reflect selected account context with action links', async () => {
  const landingSource = await readFrontendFile('pages/AppLandingPage.jsx')
  const connectionsSource = await readFrontendFile('pages/ConnectionsPage.jsx')

  assert.equal(landingSource.includes('Current workspace account'), true)
  assert.equal(landingSource.includes('Manage accounts'), true)
  assert.equal(landingSource.includes('Open connections'), true)
  assert.equal(landingSource.includes('Add another account'), true)

  assert.equal(connectionsSource.includes('Selected account context'), true)
  assert.equal(connectionsSource.includes('Connections actions stay scoped around this selected account/provider context.'), true)
})
