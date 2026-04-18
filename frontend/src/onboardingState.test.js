import test from 'node:test'
import assert from 'node:assert/strict'
import { deriveAppOnboardingState } from './onboardingState.js'

function connectedAccount(overrides = {}) {
  return {
    account_key: 'mt5:acct-1',
    connection_status: 'connected',
    sync_state: 'idle',
    ...overrides,
  }
}

test('authenticated zero-account user landing state is onboarding-first', () => {
  const state = deriveAppOnboardingState({
    signedIn: true,
    useWorkspaceApi: true,
    workspaceApiHydrated: true,
    workspaceApiAccounts: [],
    fallbackAccounts: [connectedAccount()],
  })

  assert.equal(state.source, 'workspace_api')
  assert.equal(state.hasZeroUsableAccounts, true)
  assert.equal(state.accountConnectionState.connectedUsableCount, 0)
})

test('stale historical connector metadata does not suppress onboarding when workspace API has zero usable accounts', () => {
  const state = deriveAppOnboardingState({
    signedIn: true,
    useWorkspaceApi: true,
    workspaceApiHydrated: true,
    workspaceApiAccounts: [
      { account_key: 'legacy:1', connection_status: 'disconnected', sync_state: 'idle' },
      { account_key: 'legacy:2', connection_status: 'metadata_saved', sync_state: 'idle' },
    ],
    fallbackAccounts: [connectedAccount({ connection_status: 'connected' })],
  })

  assert.equal(state.hasZeroUsableAccounts, true)
  assert.equal(state.accountConnectionState.pendingOnlyCount, 1)
  assert.equal(state.accountConnectionState.staleInactiveCount, 1)
})

test('user with at least one usable account is not forced into zero-account onboarding', () => {
  const state = deriveAppOnboardingState({
    signedIn: true,
    useWorkspaceApi: true,
    workspaceApiHydrated: true,
    workspaceApiAccounts: [connectedAccount()],
    fallbackAccounts: [],
  })

  assert.equal(state.hasZeroUsableAccounts, false)
  assert.equal(state.accountConnectionState.connectedUsableCount, 1)
})
