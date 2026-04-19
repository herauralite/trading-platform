import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

async function readFrontendFile(relPath) {
  return readFile(new URL(`./${relPath}`, import.meta.url), 'utf8')
}

test('shell-level Add Account action is wired from all /app route surfaces', async () => {
  const appSource = await readFrontendFile('App.jsx')

  assert.equal(appSource.includes('className="app-nav-link add-account-nav-link"'), true)
  assert.equal(appSource.includes('path="/app"'), true)
  assert.equal(appSource.includes('path="/app/accounts"'), true)
  assert.equal(appSource.includes('path="/app/connections"'), true)
  assert.equal(appSource.includes('onAddAccount={openAddAccountFlow}'), true)
})

test('signed-out users keep route-specific page experiences instead of a single generic fallback', async () => {
  const landingSource = await readFrontendFile('pages/AppLandingPage.jsx')
  const accountsSource = await readFrontendFile('pages/AccountsOverviewPage.jsx')
  const connectionsSource = await readFrontendFile('pages/ConnectionsPage.jsx')

  assert.equal(landingSource.includes('unlock it with Telegram'), true)
  assert.equal(accountsSource.includes('Sign in with Telegram from the app shell to manage account cards'), true)
  assert.equal(connectionsSource.includes('Signed out: setup actions are disabled.'), true)
})

test('workspace selectors keep stale metadata out of active account presence', async () => {
  const onboardingSource = await readFrontendFile('onboardingState.js')
  const connectionStateSource = await readFrontendFile('accountConnectionState.js')

  assert.equal(onboardingSource.includes('hasZeroUsableAccounts: connectionState.hasZeroConnectedAccounts'), true)
  assert.equal(connectionStateSource.includes('INACTIVE_CONNECTION_STATUSES'), true)
  assert.equal(connectionStateSource.includes('summary.staleInactiveCount += 1'), true)
})

test('active account selection persists in shell storage and is cleared on sign-out', async () => {
  const appSource = await readFrontendFile('App.jsx')

  assert.equal(appSource.includes("ACTIVE_ACCOUNT_STORAGE_KEY = 'tali_active_account_key'"), true)
  assert.equal(appSource.includes("DETAIL_ACCOUNT_STORAGE_KEY = 'tali_detail_account_key'"), true)
  assert.equal(appSource.includes('localStorage.setItem(ACTIVE_ACCOUNT_STORAGE_KEY, selectedAccountKey)'), true)
  assert.equal(appSource.includes('localStorage.setItem(DETAIL_ACCOUNT_STORAGE_KEY, detailAccountKey)'), true)
  assert.equal(appSource.includes('localStorage.removeItem(ACTIVE_ACCOUNT_STORAGE_KEY)'), true)
  assert.equal(appSource.includes('localStorage.removeItem(DETAIL_ACCOUNT_STORAGE_KEY)'), true)
  assert.equal(appSource.includes('No active usable account selected'), true)
  assert.equal(appSource.includes('Provider: {selectedAccount.source_label || selectedAccount.connector_type ||'), true)
})
