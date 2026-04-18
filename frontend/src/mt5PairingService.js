import axios from 'axios'
import { buildApiUrl } from './apiBase'

export async function checkMt5PairingState(token, draft) {
  const payload = {
    external_account_id: String(draft.external_account_id || '').trim(),
    bridge_url: String(draft.bridge_url || '').trim(),
    mt5_server: String(draft.mt5_server || '').trim(),
  }

  const res = await axios.post(buildApiUrl('/connectors/mt5_bridge/pairing/check'), payload, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  })

  return res?.data?.pairing || null
}
