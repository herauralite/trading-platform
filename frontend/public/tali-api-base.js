(function initTaliApiBaseResolver(globalScope) {
  const FALLBACK_API_BASE = 'https://trading-platform-production-0614.up.railway.app'

  const normalizeBase = (value) => String(value || '').trim().replace(/\/$/, '')

  const hasSameOriginProxy = () => {
    const config = globalScope?.__TALI_CONFIG__ || {}
    return config.sameOriginApiProxy === true || config.useRelativeApi === true
  }

  const resolveApiBase = (options = {}) => {
    // Required production precedence for auth/config requests:
    // 1) runtime config (window.__TALI_CONFIG__.apiBase/api_base)
    // 2) explicit build-time API base
    // 3) explicit Railway production fallback
    // Never return blank unless a same-origin API proxy is explicitly enabled.
    const runtimeConfigured = normalizeBase(
      globalScope?.__TALI_CONFIG__?.apiBase || globalScope?.__TALI_CONFIG__?.api_base || ''
    )
    if (runtimeConfigured) return runtimeConfigured

    const buildConfigured = normalizeBase(options?.buildEnvApiBase || '')
    if (buildConfigured) return buildConfigured

    if (hasSameOriginProxy()) return ''

    return FALLBACK_API_BASE
  }


  const formatTelegramConfigDiagnostics = ({
    resolvedApiBase = '',
    configUrl = '',
    configFetchStatus = 'request_failed',
    configFetchContentType = 'missing',
    configFetchErrorName = 'n/a',
    configFetchErrorMessage = 'n/a',
  } = {}) => [
    `resolved_api_base=${resolvedApiBase || '(empty)'}`,
    `config_url=${configUrl || '(missing)'}`,
    `config_fetch_status=${configFetchStatus}`,
    `config_fetch_content_type=${configFetchContentType || 'missing'}`,
    `config_fetch_error_name=${configFetchErrorName || 'unknown'}`,
    `config_fetch_error_message=${configFetchErrorMessage || 'n/a'}`,
  ]

  const buildApiUrl = (path, options = {}) => {
    const rawPath = String(path || '')
    const normalizedPath = rawPath.startsWith('/') ? rawPath : `/${rawPath}`
    const base = resolveApiBase(options)
    return base ? `${base}${normalizedPath}` : normalizedPath
  }

  globalScope.TaliApiBase = {
    resolveApiBase,
    buildApiUrl,
    formatTelegramConfigDiagnostics,
    FALLBACK_API_BASE,
  }
})(window)
