import { loadState } from './storage';

async function request(path: string, init: RequestInit = {}) {
  const state = await loadState();
  const base = state.apiBaseUrl.replace(/\/$/, '');
  const res = await fetch(`${base}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(init.headers || {}),
    },
  });
  if (!res.ok) throw new Error(`Request failed ${res.status}`);
  return res.json();
}

export const extensionApi = {
  startPairing(sessionToken: string) {
    return request('/extension/pair/start', { method: 'POST', headers: { Authorization: `Bearer ${sessionToken}` }, body: JSON.stringify({}) });
  },
  completePairing(payload: Record<string, unknown>) {
    return request('/extension/pair/complete', { method: 'POST', body: JSON.stringify(payload) });
  },
  heartbeat(sessionToken: string, extensionDeviceId: number) {
    return request('/extension/heartbeat', {
      method: 'POST',
      headers: { Authorization: `Bearer ${sessionToken}` },
      body: JSON.stringify({ extension_device_id: extensionDeviceId }),
    });
  },
  pollCommands(sessionToken: string, extensionDeviceId: number) {
    return request(`/execution/commands/poll?extension_device_id=${extensionDeviceId}`, { headers: { Authorization: `Bearer ${sessionToken}` } });
  },
};
