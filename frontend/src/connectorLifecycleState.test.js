import test from 'node:test'
import assert from 'node:assert/strict'
import { connectorEnvironmentLabel, deriveConnectorLifecycleState } from './connectorLifecycleState.js'

test('connector lifecycle marks verified alpaca connector as connected', () => {
  const state = deriveConnectorLifecycleState({
    status: 'paper_connected',
    provider_state: 'account_verified',
    accounts: [{ connection_status: 'account_verified' }],
  })
  assert.equal(state.key, 'verified_connected')
})

test('connector lifecycle marks validation failures clearly', () => {
  const state = deriveConnectorLifecycleState({
    status: 'disconnected',
    provider_state: 'validation_failed',
    accounts: [],
  })
  assert.equal(state.key, 'validation_failed')
  assert.equal(state.toneClass, 'status-error')
})

test('connector lifecycle marks beta metadata rows as pending', () => {
  const state = deriveConnectorLifecycleState({ status: 'awaiting_secure_auth' })
  assert.equal(state.key, 'pending_beta_metadata')
})

test('connector environment rolls up account environments', () => {
  const label = connectorEnvironmentLabel({
    accounts: [{ environment: 'paper' }, { environment: 'live' }, { environment: 'paper' }],
  })
  assert.equal(label, 'Paper, Live')
})
