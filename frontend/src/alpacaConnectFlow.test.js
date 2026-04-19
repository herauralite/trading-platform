import test from 'node:test'
import assert from 'node:assert/strict'
import {
  buildAlpacaConnectPayload,
  clearSensitiveAddAccountDraft,
  resolveAlpacaConnectResult,
  validateAlpacaDraft,
} from './alpacaConnectFlow.js'

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

test('alpaca modal validation requires label, api key, and api secret', () => {
  const errors = validateAlpacaDraft({ display_label: ' ', api_key: '', api_secret: '  ' })
  assert.deepEqual(errors, {
    display_label: 'Account label is required.',
    api_key: 'API key is required.',
    api_secret: 'API secret is required.',
  })
})

test('alpaca connect result accepts verified statuses and returns focus payload', () => {
  const result = resolveAlpacaConnectResult({
    status: 'paper_connected',
    account: { id: 42, display_label: 'Paper Acc' },
  })
  assert.equal(result.providerStatus, 'paper_connected')
  assert.equal(result.accountId, 42)
  assert.equal(result.displayLabel, 'Paper Acc')
})

test('alpaca connect result throws backend validation detail for invalid credentials', () => {
  assert.throws(
    () => resolveAlpacaConnectResult({ status: 'validation_failed', validation_error: 'invalid_credentials' }),
    /invalid_credentials/,
  )
})
