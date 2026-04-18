import axios from 'axios'
import { buildApiUrl } from './apiBase'

export async function checkMt5PairingState(token, draft) {
  const payload = {
    external_account_id: String(draft.external_account_id || '').trim(),
    bridge_url: String(draft.bridge_url || '').trim(),
    mt5_server: String(draft.mt5_server || '').trim(),
    bridge_id: String(draft.bridge_id || '').trim(),
    pairing_token: String(draft.pairing_token || '').trim(),
  }

  const res = await axios.post(buildApiUrl('/connectors/mt5_bridge/pairing/check'), payload, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  })

  return res?.data?.pairing || null
}

export async function createMt5PairingToken(token, draft) {
  const payload = {
    external_account_id: String(draft.external_account_id || '').trim(),
    mt5_server: String(draft.mt5_server || '').trim(),
    bridge_url: String(draft.bridge_url || '').trim(),
    display_name: String(draft.display_label || '').trim(),
  }
  const res = await axios.post(buildApiUrl('/connectors/mt5_bridge/pairing/token'), payload, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  })
  return {
    pairing: res?.data?.pairing || null,
    registration: res?.data?.registration || null,
  }
}

export async function fetchMt5BridgeRegistrationStatus(token) {
  const res = await axios.get(buildApiUrl('/connectors/mt5_bridge/registration/status'), {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  })
  return {
    pairing: res?.data?.pairing || null,
    registration: res?.data?.registration || null,
  }
}
