const DEFAULT_API_BASE = 'https://trading-platform-production-70e0.up.railway.app'
const TALITRADE_HOSTS = new Set(['www.talitrade.com', 'talitrade.com'])

function normalizeHost(value) {
  const raw = String(value || '').trim().toLowerCase()
  if (!raw) return ''
  return raw
    .replace(/^[a-z][a-z0-9+.-]*:\/\//, '')
    .split('/')[0]
    .split('?')[0]
    .split('#')[0]
    .split(':')[0]
    .replace(/\.+$/, '')
}

export function resolveApiBase() {
  const configured = String(import.meta?.env?.VITE_API_BASE || '').trim()
  if (configured) return configured.replace(/\/$/, '')

  const host = normalizeHost(window.location.hostname)
  if (TALITRADE_HOSTS.has(host)) {
    return window.location.origin.replace(/\/$/, '')
  }

  return DEFAULT_API_BASE
}

export function buildApiUrl(path) {
  const normalizedPath = String(path || '').startsWith('/') ? path : `/${String(path || '')}`
  return `${resolveApiBase()}${normalizedPath}`
}
