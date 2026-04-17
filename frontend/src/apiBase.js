const DEFAULT_API_BASE = 'https://trading-platform-production-70e0.up.railway.app'

export function resolveApiBase() {
  const runtimeConfigured = String(window?.__TALI_CONFIG__?.apiBase || window?.__TALI_CONFIG__?.api_base || '').trim()
  if (runtimeConfigured) return runtimeConfigured.replace(/\/$/, '')
  const viteConfigured = String(import.meta?.env?.VITE_API_BASE || '').trim()
  if (viteConfigured) return viteConfigured.replace(/\/$/, '')
  return DEFAULT_API_BASE.replace(/\/$/, '')
}

export function buildApiUrl(path) {
  const normalizedPath = String(path || '').startsWith('/') ? path : `/${String(path || '')}`
  return `${resolveApiBase()}${normalizedPath}`
}
