import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

test('authenticated app shell exposes visible routes to Accounts and Connections', async () => {
  const appSource = await readFile(new URL('./App.jsx', import.meta.url), 'utf8')
  const landingSource = await readFile(new URL('./pages/AppLandingPage.jsx', import.meta.url), 'utf8')

  assert.equal(appSource.includes('to="/app/accounts"'), true)
  assert.equal(appSource.includes('to="/app/connections"'), true)
  assert.equal(appSource.includes('path="/app"'), true)

  assert.equal(landingSource.includes('Go to Accounts'), true)
  assert.equal(landingSource.includes('Go to Connections'), true)
  assert.equal(landingSource.includes('Add Account'), true)
})
