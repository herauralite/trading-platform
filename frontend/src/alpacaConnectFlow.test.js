import test from 'node:test'
import assert from 'node:assert/strict'
import { buildAlpacaConnectPayload, clearSensitiveAddAccountDraft } from './alpacaConnectFlow.js'

test('alpaca payload normalizes environment and trims credentials', () => {
  const payload = buildAlpacaConnectPayload({
    label: '  My Alpaca  ',
    environment: 'LIVE',
    apiKey: ' key ',
    apiSecret: ' secret ',
  })

  assert.deepEqual(payload, {
    label: 'My Alpaca',
    environment: 'live',
    api_key: 'key',
    api_secret: 'secret',
  })
})

test('sensitive add account fields are cleared after submit', () => {
  const cleared = clearSensitiveAddAccountDraft({
    display_label: 'My Alpaca',
    api_key: 'raw-key',
    api_secret: 'raw-secret',
    environment: 'paper',
  })

  assert.equal(cleared.display_label, 'My Alpaca')
  assert.equal(cleared.api_key, '')
  assert.equal(cleared.api_secret, '')
  assert.equal(cleared.environment, 'paper')
})
