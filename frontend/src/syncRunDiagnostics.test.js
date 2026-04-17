import test from 'node:test'
import assert from 'node:assert/strict'
import { formatSyncRunDiagnostics } from './syncRunDiagnostics.js'

test('formats successful sync diagnostics summary', () => {
  const diag = formatSyncRunDiagnostics({
    result_detail: {
      result_category: 'connector_sync_summary',
      status_detail: 'FundingPips sync checked 2 accounts; 1 account(s) are fresh.',
      counts: {
        accounts_total: 2,
        accounts_fresh: 1,
        open_positions: 3,
        trades_24h: 5,
      },
    },
  })

  assert.equal(diag.resultCategory, 'connector_sync_summary')
  assert.match(diag.summary, /FundingPips sync checked 2 accounts/)
  assert.match(diag.summary, /accounts 1\/2 fresh/)
  assert.equal(diag.errorCode, null)
  assert.equal(diag.isTransient, null)
})

test('formats structured error diagnostics', () => {
  const diag = formatSyncRunDiagnostics({
    result_detail: {
      result_category: 'error',
      error_code: 'stale_source_data',
      error_category: 'source_staleness',
      is_transient: true,
      status_detail: 'No FundingPips accounts have recent snapshots.',
    },
  })

  assert.equal(diag.resultCategory, 'error')
  assert.equal(diag.errorCode, 'stale_source_data')
  assert.equal(diag.errorCategory, 'source_staleness')
  assert.equal(diag.isTransient, true)
  assert.match(diag.summary, /No FundingPips accounts have recent snapshots/)
})
