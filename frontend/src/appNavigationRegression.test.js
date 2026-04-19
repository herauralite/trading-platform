import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

test('app shell exposes visible routes to Accounts and Connections regardless of auth gate state', async () => {
  const appSource = await readFile(new URL('./App.jsx', import.meta.url), 'utf8')
  const landingSource = await readFile(new URL('./pages/AppLandingPage.jsx', import.meta.url), 'utf8')
  const accountsSource = await readFile(new URL('./pages/AccountsOverviewPage.jsx', import.meta.url), 'utf8')

  assert.equal(appSource.includes('to="/app/accounts"'), true)
  assert.equal(appSource.includes('to="/app/connections"'), true)
  assert.equal(appSource.includes('path="/app"'), true)
  assert.equal(appSource.includes('Authenticate to unlock the account workspace, live sync operations, and connector controls.'), true)

  assert.equal(landingSource.includes('Review Accounts surface'), true)
  assert.equal(landingSource.includes('Review Connections surface'), true)
  assert.equal(landingSource.includes('unlock your trading workspace'), true)
  assert.equal(accountsSource.includes('manage account cards'), true)
})
