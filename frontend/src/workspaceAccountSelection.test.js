import test from 'node:test'
import assert from 'node:assert/strict'
import { classifyWorkspaceAccountState, resolvePreferredDetailAccountKey } from './workspaceAccountSelection.js'

const usable = {
  account_key: 'usable:1',
  connection_status: 'connected',
  sync_state: 'idle',
  is_primary: false,
}

const pending = {
  account_key: 'pending:1',
  connection_status: 'bridge_required',
  sync_state: 'queued',
}

const stale = {
  account_key: 'stale:1',
  connection_status: 'disconnected',
  sync_state: 'idle',
}

test('classifyWorkspaceAccountState keeps usable/pending/stale semantics consistent', () => {
  assert.equal(classifyWorkspaceAccountState(usable), 'usable')
  assert.equal(classifyWorkspaceAccountState(pending), 'pending')
  assert.equal(classifyWorkspaceAccountState(stale), 'stale')
})

test('resolvePreferredDetailAccountKey recovers to usable then pending then stale', () => {
  assert.equal(resolvePreferredDetailAccountKey([stale, pending]), 'pending:1')
  assert.equal(resolvePreferredDetailAccountKey([stale]), 'stale:1')
  assert.equal(resolvePreferredDetailAccountKey([pending, usable]), 'usable:1')
})

test('resolvePreferredDetailAccountKey keeps existing detail selection when still available', () => {
  assert.equal(
    resolvePreferredDetailAccountKey([usable, pending], { currentDetailAccountKey: 'pending:1', selectedActiveAccountKey: 'usable:1' }),
    'pending:1',
  )
})
