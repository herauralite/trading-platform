import { loadState, saveState } from '../shared/storage.js';

const el = document.getElementById('status');
const tokenLabel = document.getElementById('token-status');
const apiInput = document.getElementById('api-base-url');
const saveBtn = document.getElementById('save-settings');

loadState().then((state) => {
  apiInput.value = state.apiBaseUrl || 'http://localhost:8000';
  el.textContent = state.extensionDeviceId ? `Paired device #${state.extensionDeviceId}` : 'Not paired';
  tokenLabel.textContent = state.extensionAccessToken ? 'Extension auth ready' : 'Missing extension auth token';
});

saveBtn.addEventListener('click', async () => {
  await saveState({ apiBaseUrl: apiInput.value.trim() });
  tokenLabel.textContent = 'Saved';
});
