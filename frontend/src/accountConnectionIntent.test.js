import test from 'node:test'
import assert from 'node:assert/strict'
import { buildConnectionsIntentPath, deriveConnectionsIntentFromState, resolveAccountProviderKey } from './accountConnectionIntent.js'

test('buildConnectionsIntentPath encodes provider/account/intent query params', () => {
  const path = buildConnectionsIntentPath({ connector_type: 'mt5_bridge', account_key: 'mt5_bridge:acct-1' }, 'reconnect')
  assert.equal(path, '/app/connections?provider=mt5_bridge&account=mt5_bridge%3Aacct-1&intent=reconnect')
})

test('resolveAccountProviderKey normalizes provider keys', () => {
  assert.equal(resolveAccountProviderKey({ connector_type: ' TradingView_Webhook ' }), 'tradingview_webhook')
})

test('deriveConnectionsIntentFromState maps account class to connector intent', () => {
  assert.equal(deriveConnectionsIntentFromState('usable'), 'manage')
  assert.equal(deriveConnectionsIntentFromState('pending'), 'setup')
  assert.equal(deriveConnectionsIntentFromState('stale'), 'reconnect')
})
