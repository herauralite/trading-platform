import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { deriveAppOnboardingState } from './onboardingState.js'

async function readFrontendFile(relPath) {
  return readFile(new URL(`./${relPath}`, import.meta.url), 'utf8')
}

test('/app routes keep canonical shell navigation and route-specific page surfaces', async () => {
  const appSource = await readFrontendFile('App.jsx')
  const landingSource = await readFrontendFile('pages/AppLandingPage.jsx')
  const accountsSource = await readFrontendFile('pages/AccountsOverviewPage.jsx')
  const connectionsSource = await readFrontendFile('pages/ConnectionsPage.jsx')

  assert.equal(appSource.includes('to="/app">Dashboard</NavLink>'), true)
  assert.equal(appSource.includes('to="/app/accounts">Accounts</NavLink>'), true)
  assert.equal(appSource.includes('to="/app/connections">Connections</NavLink>'), true)

  assert.equal(landingSource.includes('Connect Telegram to unlock your trading workspace'), true)
  assert.equal(accountsSource.includes('Sign in with Telegram from the app shell to manage account cards'), true)
  assert.equal(connectionsSource.includes('Signed out: setup actions are disabled.'), true)
})

test('signed-in zero-account onboarding and add-account entry points remain visible across shell + pages', async () => {
  const appSource = await readFrontendFile('App.jsx')
  const landingSource = await readFrontendFile('pages/AppLandingPage.jsx')
  const accountsSource = await readFrontendFile('pages/AccountsOverviewPage.jsx')
  const connectionsSource = await readFrontendFile('pages/ConnectionsPage.jsx')

  assert.equal(appSource.includes('className="app-nav-link add-account-nav-link"'), true)
  assert.equal(appSource.includes('openAddAccountFlow'), true)

  assert.equal(landingSource.includes('Add your first live workspace account'), true)
  assert.equal(landingSource.includes('Go to Accounts (primary)'), true)
  assert.equal(landingSource.includes('Go to Connections (operations)'), true)

  assert.equal(accountsSource.includes('Connect your first trading account'), true)
  assert.equal(accountsSource.includes('Add your first account'), true)

  assert.equal(connectionsSource.includes('Start ${method.title}'), true)
  assert.equal(connectionsSource.includes('Open guided flow'), true)
})

test('workspace API remains onboarding source of truth even with stale connector metadata', () => {
  const state = deriveAppOnboardingState({
    signedIn: true,
    useWorkspaceApi: true,
    workspaceApiHydrated: true,
    workspaceApiAccounts: [
      { account_key: 'legacy:1', connection_status: 'disconnected', sync_state: 'idle' },
      { account_key: 'legacy:2', connection_status: 'validation_failed', sync_state: 'failed' },
    ],
    fallbackAccounts: [
      { account_key: 'fallback:1', connection_status: 'connected', sync_state: 'idle' },
    ],
  })

  assert.equal(state.source, 'workspace_api')
  assert.equal(state.hasZeroUsableAccounts, true)
  assert.equal(state.accountConnectionState.connectedUsableCount, 0)
})

test('homepage hero markers and placeholder shell copy stay out of /app shell source', async () => {
  const appSource = await readFrontendFile('App.jsx')

  assert.equal(appSource.includes('The platform built for <span class="accent">prop firm</span> traders.'), false)
  assert.equal(appSource.includes('TaliTrade Command Center'), false)
  assert.equal(appSource.includes('Fallback contract page'), false)
})
