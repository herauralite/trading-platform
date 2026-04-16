import test from 'node:test'
import assert from 'node:assert/strict'

import {
  buildAuthHeaders,
  buildCsvImportPayload,
  buildManualAccountPayload,
  buildManualTradePayload,
  canHydrateSession,
  clearOidcCorrelation,
  OIDC_NONCE_KEY,
  OIDC_STATE_KEY,
  parseOidcCallbackPayload,
  parseStoredUser,
  persistOidcCorrelation,
  shouldShowBridgeFallback,
} from './sessionAuth.js'

test('authenticated boot hydration requires a telegram user id', () => {
  assert.equal(canHydrateSession({ user: { telegram_user_id: '123' } }), true)
  assert.equal(canHydrateSession({ user: {} }), false)
  assert.equal(canHydrateSession(null), false)
})

test('signed-in current-user parsing is resilient to invalid local storage', () => {
  assert.deepEqual(parseStoredUser('{"telegram_user_id":"42"}'), { telegram_user_id: '42' })
  assert.equal(parseStoredUser('not-json'), null)
  assert.equal(parseStoredUser(''), null)
})

test('connector/account/manual/csv flows use bearer auth and authenticated payload builders', () => {
  assert.deepEqual(buildAuthHeaders('abc123'), { Authorization: 'Bearer abc123' })
  assert.deepEqual(buildAuthHeaders(''), {})

  assert.deepEqual(
    buildManualAccountPayload({
      brokerName: 'Manual',
      externalAccountId: 'acct-1',
      displayLabel: '',
      accountType: 'demo',
      accountSize: '10000',
    }),
    {
      connector_type: 'manual',
      broker_name: 'Manual',
      external_account_id: 'acct-1',
      display_label: 'Manual acct-1',
      account_type: 'demo',
      account_size: 10000,
    }
  )

  assert.deepEqual(
    buildManualTradePayload({
      externalAccountId: 'acct-1',
      symbol: 'NAS100',
      side: 'buy',
      size: '0.1',
      entryPrice: '100',
      exitPrice: '110',
      pnl: '10',
    }),
    {
      connector_type: 'manual',
      external_account_id: 'acct-1',
      symbol: 'NAS100',
      side: 'buy',
      size: 0.1,
      entry_price: 100,
      exit_price: 110,
      pnl: 10,
      import_provenance: { entry_mode: 'ui_manual' },
      source_metadata: { created_from: 'manual_journal_panel' },
    }
  )

  assert.deepEqual(buildCsvImportPayload('csv-1', [{ symbol: 'US30' }]), {
    connector_type: 'csv_import',
    broker_name: 'csv',
    external_account_id: 'csv-1',
    rows: [{ symbol: 'US30' }],
  })
})

test('bridge fallback visibility is dev-only', () => {
  assert.equal(shouldShowBridgeFallback({ isDevEnv: false, devModeValue: null }), false)
  assert.equal(shouldShowBridgeFallback({ isDevEnv: false, devModeValue: '1' }), true)
  assert.equal(shouldShowBridgeFallback({ isDevEnv: true, devModeValue: null }), true)
})

test('OIDC flow start stores nonce and state correlation values', () => {
  const storage = new Map()
  const fakeStorage = {
    setItem: (k, v) => storage.set(k, v),
    removeItem: (k) => storage.delete(k),
    getItem: (k) => storage.get(k) ?? null,
  }
  persistOidcCorrelation(fakeStorage, { nonce: 'nonce-1', state: 'state-1' })
  assert.equal(fakeStorage.getItem(OIDC_NONCE_KEY), 'nonce-1')
  assert.equal(fakeStorage.getItem(OIDC_STATE_KEY), 'state-1')
})

test('OIDC callback is rejected when returned state is missing', () => {
  const parsed = parseOidcCallbackPayload('#id_token=token123', { expectedState: 'state-1', storedNonce: 'nonce-1' })
  assert.equal(parsed.ok, false)
  assert.match(parsed.error, /Missing OIDC state correlation/i)
})

test('OIDC callback is rejected when returned state does not match expected state', () => {
  const parsed = parseOidcCallbackPayload('#id_token=token123&state=wrong', { expectedState: 'state-1', storedNonce: 'nonce-1' })
  assert.equal(parsed.ok, false)
  assert.match(parsed.error, /OIDC state mismatch/i)
})

test('OIDC callback is allowed when returned state matches expected state', () => {
  const parsed = parseOidcCallbackPayload('#id_token=token123&state=state-1&nonce=nonce-1', { expectedState: 'state-1', storedNonce: 'stored' })
  assert.deepEqual(parsed, {
    ok: true,
    idToken: 'token123',
    state: 'state-1',
    nonce: 'nonce-1',
  })
})

test('OIDC correlation values are cleared after callback completion or failure', () => {
  const storage = new Map([
    [OIDC_NONCE_KEY, 'nonce-1'],
    [OIDC_STATE_KEY, 'state-1'],
  ])
  const fakeStorage = {
    setItem: (k, v) => storage.set(k, v),
    removeItem: (k) => storage.delete(k),
    getItem: (k) => storage.get(k) ?? null,
  }

  clearOidcCorrelation(fakeStorage)
  assert.equal(fakeStorage.getItem(OIDC_NONCE_KEY), null)
  assert.equal(fakeStorage.getItem(OIDC_STATE_KEY), null)
})
