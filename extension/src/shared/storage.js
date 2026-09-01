const KEY = 'talitrade.bridge.state';

export async function loadState() {
  const item = await chrome.storage.local.get(KEY);
  return item[KEY] || { apiBaseUrl: 'http://localhost:8000' };
}

export async function saveState(update) {
  const current = await loadState();
  const next = { ...current, ...update };
  await chrome.storage.local.set({ [KEY]: next });
  return next;
}
