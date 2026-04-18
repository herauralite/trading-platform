(function initTaliApiBaseResolver(globalScope) {
  const normalizeBase = (value) => String(value || '').trim().replace(/\/$/, '')

  const resolveApiBase = (options = {}) => {
    const runtimeConfigured = normalizeBase(
      globalScope?.__TALI_CONFIG__?.apiBase || globalScope?.__TALI_CONFIG__?.api_base || ''
    )
    if (runtimeConfigured) return runtimeConfigured

    const buildConfigured = normalizeBase(options?.buildEnvApiBase || '')
    if (buildConfigured) return buildConfigured

    return ''
  }

  const buildApiUrl = (path, options = {}) => {
    const normalizedPath = String(path || '').startsWith('/') ? String(path || '') : `/${String(path || '')}`
    const base = resolveApiBase(options)
    return base ? `${base}${normalizedPath}` : normalizedPath
  }

  globalScope.TaliApiBase = {
    resolveApiBase,
    buildApiUrl,
  }
})(window)
