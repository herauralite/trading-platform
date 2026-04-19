export function buildAlpacaConnectPayload({ label, environment, apiKey, apiSecret }) {
  return {
    label: String(label || '').trim(),
    environment: String(environment || 'paper').trim().toLowerCase() === 'live' ? 'live' : 'paper',
    api_key: String(apiKey || '').trim(),
    api_secret: String(apiSecret || '').trim(),
  }
}

export function validateAlpacaDraft(draft = {}) {
  const errors = {}
  const label = String(draft.display_label || '').trim()
  const apiKey = String(draft.api_key || '').trim()
  const apiSecret = String(draft.api_secret || '').trim()
  if (!label) errors.display_label = 'Account label is required.'
  if (!apiKey) errors.api_key = 'API key is required.'
  if (!apiSecret) errors.api_secret = 'API secret is required.'
  return errors
}

export function resolveAlpacaConnectResult(responseData = {}) {
  const providerStatus = String(responseData?.status || '').toLowerCase()
  if (!['paper_connected', 'live_connected'].includes(providerStatus)) {
    throw new Error(responseData?.validation_error || 'Alpaca credentials could not be verified.')
  }
  return {
    providerStatus,
    accountId: responseData?.account?.id ?? null,
    displayLabel: responseData?.account?.display_label || '',
  }
}

export function clearSensitiveAddAccountDraft(draft = {}) {
  return {
    ...draft,
    api_key: '',
    api_secret: '',
  }
}
