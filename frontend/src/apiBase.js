export function resolveApiBase() {
  return window.TaliApiBase.resolveApiBase({ buildEnvApiBase: import.meta?.env?.VITE_API_BASE })
}

export function buildApiUrl(path) {
  return window.TaliApiBase.buildApiUrl(path, { buildEnvApiBase: import.meta?.env?.VITE_API_BASE })
}
