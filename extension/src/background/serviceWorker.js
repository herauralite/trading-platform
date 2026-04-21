import { adapterRegistry } from '../adapters/registry';
import { extensionApi } from '../shared/apiClient';
import { loadState } from '../shared/storage';

const HEARTBEAT_ALARM = 'talitrade-heartbeat';
const COMMAND_POLL_ALARM = 'talitrade-command-poll';

chrome.runtime.onInstalled.addListener(async () => {
  chrome.alarms.create(HEARTBEAT_ALARM, { periodInMinutes: 0.5 });
  chrome.alarms.create(COMMAND_POLL_ALARM, { periodInMinutes: 0.25 });
});

chrome.alarms.onAlarm.addListener(async (alarm) => {
  const state = await loadState();
  if (!state.extensionDeviceId) return;
  if (!state.pairSecret || !state.pairCode) return;

  if (alarm.name === HEARTBEAT_ALARM) {
    try {
      await extensionApi.heartbeat(state.pairSecret, state.extensionDeviceId);
    } catch (error) {
      console.warn('heartbeat failed', error);
    }
  }

  if (alarm.name === COMMAND_POLL_ALARM) {
    try {
      const pending = await extensionApi.pollCommands(state.pairSecret, state.extensionDeviceId);
      console.debug('pending commands', pending.commands?.length || 0);
    } catch (error) {
      console.warn('poll failed', error);
    }
  }
});

chrome.tabs.onUpdated.addListener(async (tabId, changeInfo, tab) => {
  if (!changeInfo.url && !tab.url) return;
  const adapter = await adapterRegistry.detectForUrl(changeInfo.url || tab.url || '');
  if (!adapter) return;
  console.debug('adapter detected', adapter.adapterKey, tabId);
  // TODO: report platform session + state sync from content bridge.
});
