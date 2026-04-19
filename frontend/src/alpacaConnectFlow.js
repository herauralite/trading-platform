export function buildAlpacaConnectPayload({ label, environment, apiKey, apiSecret }) {
  return {
    label: String(label || '').trim(),
    environment: String(environment || 'paper').trim().toLowerCase() === 'live' ? 'live' : 'paper',
    api_key: String(apiKey || '').trim(),
    api_secret: String(apiSecret || '').trim(),
  }
}

export function clearSensitiveAddAccountDraft(draft = {}) {
  return {
    ...draft,
    api_key: '',
    api_secret: '',
  }
}
