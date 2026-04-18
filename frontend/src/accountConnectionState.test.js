import test from 'node:test'
import assert from 'node:assert/strict'
import { deriveAccountConnectionState } from './accountConnectionState.js'

test('zero-account workspace reports onboarding state', () => {
  const summary = deriveAccountConnectionState([])
  assert.equal(summary.hasZeroConnectedAccounts, true)
  assert.equal(summary.hasConnectedAccounts, false)
  assert.equal(summary.connectedUsableCount, 0)
})

test('historical/disconnected workspace rows do not count as active accounts', () => {
  const summary = deriveAccountConnectionState([
    { account_key: 'mt5:old-1', connection_status: 'disconnected', sync_state: 'idle' },
    { account_key: 'mt5:old-2', connection_status: 'archived', sync_state: 'idle' },
    { account_key: 'tv:stale', connection_status: 'validation_failed', sync_state: 'failed' },
  ])

  assert.equal(summary.hasZeroConnectedAccounts, true)
  assert.equal(summary.connectedUsableCount, 0)
  assert.equal(summary.staleInactiveCount, 3)
})

test('at least one currently usable account keeps normal app flow', () => {
  const summary = deriveAccountConnectionState([
    { account_key: 'mt5:active', connection_status: 'connected', sync_state: 'idle' },
    { account_key: 'tv:pending', connection_status: 'awaiting_alerts', sync_state: 'queued' },
  ])

  assert.equal(summary.hasConnectedAccounts, true)
  assert.equal(summary.hasZeroConnectedAccounts, false)
  assert.equal(summary.connectedUsableCount, 1)
})
