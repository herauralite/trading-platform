const KEY = 'talitrade.bridge.state';

export type ExtensionLocalState = {
  apiBaseUrl: string;
  pairCode?: string;
  pairSecret?: string;
  extensionDeviceId?: number;
};

export async function loadState(): Promise<ExtensionLocalState> {
  const item = await chrome.storage.local.get(KEY);
  return item[KEY] || { apiBaseUrl: 'http://localhost:8000' };
}

export async function saveState(update: Partial<ExtensionLocalState>): Promise<ExtensionLocalState> {
  const current = await loadState();
  const next = { ...current, ...update };
  await chrome.storage.local.set({ [KEY]: next });
  return next;
}
