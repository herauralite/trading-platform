import test from 'node:test'
import assert from 'node:assert/strict'

import {
  buildAuthHeaders,
  buildCsvImportPayload,
  buildManualAccountPayload,
  buildManualTradePayload,
  canHydrateSession,
  parseOidcCallbackPayload,
  parseStoredUser,
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

test('oidc callback parser extracts id token and nonce for authenticated login continuation', () => {
  assert.deepEqual(
    parseOidcCallbackPayload('#id_token=token123&nonce=nonce123', 'fallback'),
    { idToken: 'token123', nonce: 'nonce123' }
  )
  assert.deepEqual(
    parseOidcCallbackPayload('#id_token=token123', 'stored-nonce'),
    { idToken: 'token123', nonce: 'stored-nonce' }
  )
  assert.equal(parseOidcCallbackPayload('#state=abc', null), null)
})
