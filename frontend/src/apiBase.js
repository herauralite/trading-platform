const BUILD_API_BASE = import.meta?.env?.VITE_API_BASE

// API base precedence must match static auth surfaces: runtime config -> build-time -> Railway fallback.
// Returning blank is only valid when same-origin proxy is intentionally configured in window.__TALI_CONFIG__.
export function resolveApiBase() {
  return window.TaliApiBase.resolveApiBase({ buildEnvApiBase: BUILD_API_BASE })
}

export function buildApiUrl(path) {
  return window.TaliApiBase.buildApiUrl(path, { buildEnvApiBase: BUILD_API_BASE })
}

export function formatTelegramConfigDiagnostics(values) {
  return window.TaliApiBase.formatTelegramConfigDiagnostics(values)
}

export const FALLBACK_API_BASE = window?.TaliApiBase?.FALLBACK_API_BASE || ''
