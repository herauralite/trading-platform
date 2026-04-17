import test from 'node:test'
import assert from 'node:assert/strict'
import { buildConnectorConfigDraft, connectorConfigStateLabel } from './connectorConfig.js'

test('builds config draft with safe defaults and no secret echo', () => {
  const draft = buildConnectorConfigDraft({
    non_secret_config: {
      healthcheck_url: 'https://api.example.com/health',
      external_account_id: 'acct-1',
      timeout_seconds: 12,
    },
    has_secret_config: true,
  })

  assert.equal(draft.healthcheck_url, 'https://api.example.com/health')
  assert.equal(draft.external_account_id, 'acct-1')
  assert.equal(draft.timeout_seconds, 12)
  assert.equal(draft.api_token, '')
  assert.equal(draft.hasSecret, true)
})

test('maps connector readiness states for config UX', () => {
  assert.equal(connectorConfigStateLabel({ supports_live_sync: false }), 'not_required')
  assert.equal(connectorConfigStateLabel({ supports_live_sync: true, has_config: false }), 'missing')
  assert.equal(connectorConfigStateLabel({ supports_live_sync: true, has_config: true, config_status: 'configured' }), 'ready')
  assert.equal(connectorConfigStateLabel({ supports_live_sync: true, has_config: true, config_status: 'incomplete' }), 'incomplete')
})
