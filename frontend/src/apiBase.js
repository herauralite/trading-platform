const BUILD_API_BASE = import.meta?.env?.VITE_API_BASE

export function resolveApiBase() {
  return window.TaliApiBase.resolveApiBase({ buildEnvApiBase: BUILD_API_BASE })
}

export function buildApiUrl(path) {
  return window.TaliApiBase.buildApiUrl(path, { buildEnvApiBase: BUILD_API_BASE })
}

export const FALLBACK_API_BASE = window.TaliApiBase.FALLBACK_API_BASE
