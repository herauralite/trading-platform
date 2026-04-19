import test from 'node:test'
import assert from 'node:assert/strict'
import { connectionStatusMeta } from './components/accountStatusMeta.js'

test('account_verified renders premium connected tone', () => {
  const meta = connectionStatusMeta('account_verified')
  assert.equal(meta.toneClass, 'status-connected')
  assert.equal(meta.label, 'Account Verified')
})

test('paper/live connected statuses render connected tone', () => {
  assert.equal(connectionStatusMeta('paper_connected').toneClass, 'status-connected')
  assert.equal(connectionStatusMeta('live_connected').toneClass, 'status-connected')
})

test('validation failed renders error tone', () => {
  const meta = connectionStatusMeta('validation_failed')
  assert.equal(meta.toneClass, 'status-error')
})
