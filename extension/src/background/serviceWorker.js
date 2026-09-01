import { adapterRegistry } from '../adapters/registry.js';
import { extensionApi } from '../shared/apiClient.js';
import { loadState } from '../shared/storage.js';

const HEARTBEAT_ALARM = 'talitrade-heartbeat';
const COMMAND_POLL_ALARM = 'talitrade-command-poll';
const PLATFORM_SCAN_ALARM = 'talitrade-platform-scan';

async function withAuthedState(fn) {
  const state = await loadState();
  if (!state.extensionAccessToken) return;
  await fn(state);
}

async function collectFundingPipsStateForTab(tab) {
  return chrome.tabs.sendMessage(tab.id, { type: 'TALITRADE_COLLECT_STATE', tab });
}

async function scanSupportedTabs(state) {
  const tabs = await chrome.tabs.query({});
  const platformSessions = [];
  const accounts = [];

  for (const tab of tabs) {
    const adapter = await adapterRegistry.detectForUrl(tab.url || '');
    if (!adapter) continue;
    try {
      const payload = await collectFundingPipsStateForTab(tab);
      if (!payload || !payload.platformSession || !payload.accountState) continue;
      platformSessions.push(payload.platformSession);
      accounts.push(payload.accountState);
    } catch (error) {
      console.warn('state collect failed', tab.id, error);
    }
  }

  if (platformSessions.length) {
    await extensionApi.upsertPlatformSessions(state.extensionAccessToken, platformSessions);
  }
  if (accounts.length) {
    await extensionApi.syncState(state.extensionAccessToken, accounts);
  }
}

chrome.runtime.onInstalled.addListener(async () => {
  chrome.alarms.create(HEARTBEAT_ALARM, { periodInMinutes: 0.5 });
  chrome.alarms.create(COMMAND_POLL_ALARM, { periodInMinutes: 0.25 });
  chrome.alarms.create(PLATFORM_SCAN_ALARM, { periodInMinutes: 0.5 });
});

chrome.alarms.onAlarm.addListener(async (alarm) => {
  await withAuthedState(async (state) => {
    if (alarm.name === HEARTBEAT_ALARM) {
      await extensionApi.heartbeat(state.extensionAccessToken, {
        metadata: { source: 'alarm' },
      });
    }

    if (alarm.name === COMMAND_POLL_ALARM) {
      const adapterKeys = adapterRegistry.list().map((x) => x.adapterKey);
      const result = await extensionApi.pollCommands(state.extensionAccessToken, adapterKeys);
      console.debug('pending commands', result.commands?.length || 0);
    }

    if (alarm.name === PLATFORM_SCAN_ALARM) {
      await scanSupportedTabs(state);
    }
  });
});

chrome.tabs.onUpdated.addListener(async (_tabId, changeInfo, tab) => {
  if (!changeInfo.url && !tab.url) return;
  await withAuthedState(async (state) => {
    const adapter = await adapterRegistry.detectForUrl(changeInfo.url || tab.url || '');
    if (!adapter) return;
    await scanSupportedTabs(state);
  });
});
