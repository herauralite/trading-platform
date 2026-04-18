import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

test('/app shell keeps canonical route tree mounted while using an in-shell Telegram auth gate', async () => {
  const appSource = await readFile(new URL('./App.jsx', import.meta.url), 'utf8')
  const landingSource = await readFile(new URL('./pages/AppLandingPage.jsx', import.meta.url), 'utf8')
  const accountsSource = await readFile(new URL('./pages/AccountsOverviewPage.jsx', import.meta.url), 'utf8')
  const connectionsSource = await readFile(new URL('./pages/ConnectionsPage.jsx', import.meta.url), 'utf8')

  assert.equal(appSource.includes('<Routes>'), true)
  assert.equal(appSource.includes('path="/app/accounts"'), true)
  assert.equal(appSource.includes('path="/app/connections"'), true)
  assert.equal(appSource.includes('Sign in with Telegram'), true)

  assert.equal(appSource.includes('TaliTrade Premium Workspace'), false)
  assert.equal(appSource.includes('Secure Telegram Sign-In'), false)
  assert.equal(appSource.includes('Preparing secure Telegram sign-in…'), false)

  assert.equal(landingSource.includes('Workspace Dashboard'), true)
  assert.equal(accountsSource.includes('<h2>Accounts</h2>'), true)
  assert.equal(connectionsSource.includes('<h2>Connections</h2>'), true)
})
