import test from 'node:test'
import assert from 'node:assert/strict'

test('formatTelegramConfigDiagnostics returns required production debug keys', async () => {
  globalThis.window = {
    TaliApiBase: {
      FALLBACK_API_BASE: 'https://trading-platform-production-0614.up.railway.app',
      resolveApiBase: () => 'https://api.example.com',
      buildApiUrl: (path) => `https://api.example.com${path}`,
      formatTelegramConfigDiagnostics: (values) => [
        `resolved_api_base=${values.resolvedApiBase}`,
        `config_url=${values.configUrl}`,
        `config_fetch_status=${values.configFetchStatus}`,
        `config_fetch_content_type=${values.configFetchContentType}`,
        `config_fetch_error_name=${values.configFetchErrorName || 'n/a'}`,
        `config_fetch_error_message=${values.configFetchErrorMessage || 'n/a'}`,
      ],
    },
  }

  const { formatTelegramConfigDiagnostics } = await import('./apiBase.js')

  const diagnostics = formatTelegramConfigDiagnostics({
    resolvedApiBase: 'https://api.example.com',
    configUrl: 'https://api.example.com/auth/telegram/config',
    configFetchStatus: 200,
    configFetchContentType: 'application/json',
  })

  assert.deepEqual(diagnostics, [
    'resolved_api_base=https://api.example.com',
    'config_url=https://api.example.com/auth/telegram/config',
    'config_fetch_status=200',
    'config_fetch_content_type=application/json',
    'config_fetch_error_name=n/a',
    'config_fetch_error_message=n/a',
  ])
})
