import { loadState } from './storage.js';

async function request(path, init = {}) {
  const state = await loadState();
  const base = String(state.apiBaseUrl || '').replace(/\/$/, '');
  const res = await fetch(`${base}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(init.headers || {}),
    },
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`Request failed ${res.status}: ${body}`);
  }
  return res.json();
}

function bearer(token) {
  return { Authorization: `Bearer ${token}` };
}

export const extensionApi = {
  completePairing(payload) {
    return request('/extension/pair/complete', { method: 'POST', body: JSON.stringify(payload) });
  },
  heartbeat(extensionAccessToken, payload = {}) {
    return request('/extension/heartbeat', {
      method: 'POST',
      headers: bearer(extensionAccessToken),
      body: JSON.stringify(payload),
    });
  },
  upsertPlatformSessions(extensionAccessToken, sessions) {
    return request('/extension/platform-sessions/upsert', {
      method: 'POST',
      headers: bearer(extensionAccessToken),
      body: JSON.stringify({ sessions }),
    });
  },
  syncState(extensionAccessToken, accounts) {
    return request('/extension/state-sync', {
      method: 'POST',
      headers: bearer(extensionAccessToken),
      body: JSON.stringify({ accounts }),
    });
  },
  pollCommands(extensionAccessToken, adapterKeys = []) {
    const query = adapterKeys.length ? `?${new URLSearchParams(adapterKeys.map((k) => ['adapter_keys', k]))}` : '';
    return request(`/execution/commands/poll${query}`, { headers: bearer(extensionAccessToken) });
  },
  ackCommand(extensionAccessToken, commandId, status = 'acked', metadata = {}) {
    return request(`/execution/commands/${commandId}/ack`, {
      method: 'POST',
      headers: bearer(extensionAccessToken),
      body: JSON.stringify({ status, metadata }),
    });
  },
  reportCommandResult(extensionAccessToken, commandId, payload) {
    return request(`/execution/commands/${commandId}/result`, {
      method: 'POST',
      headers: bearer(extensionAccessToken),
      body: JSON.stringify(payload),
    });
  },
};
