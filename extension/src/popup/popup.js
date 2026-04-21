import { loadState } from '../shared/storage';

const el = document.getElementById('status');
loadState().then((state) => {
  el.textContent = state.extensionDeviceId
    ? `Paired device #${state.extensionDeviceId}`
    : 'Not paired';
});
