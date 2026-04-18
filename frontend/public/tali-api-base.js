(function initTaliApiBaseResolver(globalScope) {
  const FALLBACK_API_BASE = 'https://trading-platform-production-0614.up.railway.app'

  const normalizeBase = (value) => String(value || '').trim().replace(/\/$/, '')

  const hasSameOriginProxy = () => {
    const config = globalScope?.__TALI_CONFIG__ || {}
    return config.sameOriginApiProxy === true || config.useRelativeApi === true
  }

  const resolveApiBase = (options = {}) => {
    const runtimeConfigured = normalizeBase(
      globalScope?.__TALI_CONFIG__?.apiBase || globalScope?.__TALI_CONFIG__?.api_base || ''
    )
    if (runtimeConfigured) return runtimeConfigured

    const buildConfigured = normalizeBase(options?.buildEnvApiBase || '')
    if (buildConfigured) return buildConfigured

    if (hasSameOriginProxy()) return ''

    return FALLBACK_API_BASE
  }

  const buildApiUrl = (path, options = {}) => {
    const rawPath = String(path || '')
    const normalizedPath = rawPath.startsWith('/') ? rawPath : `/${rawPath}`
    const base = resolveApiBase(options)
    return base ? `${base}${normalizedPath}` : normalizedPath
  }

  globalScope.TaliApiBase = {
    resolveApiBase,
    buildApiUrl,
    FALLBACK_API_BASE,
  }
})(window)
