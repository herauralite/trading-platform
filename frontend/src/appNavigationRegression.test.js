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
  assert.equal(appSource.includes('Authenticate to connect accounts, run sync actions, and unlock account workflows.'), true)

  assert.equal(landingSource.includes('Go to Accounts'), true)
  assert.equal(landingSource.includes('Go to Connections'), true)
  assert.equal(landingSource.includes('premium app shell'), true)
  assert.equal(accountsSource.includes('This page is ready. Sign in with Telegram in the app shell'), true)
})
