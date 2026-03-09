const TALI_STORAGE_KEY = 'tali_telegram_uid';

function normalizeTelegramUserId(value) {
  if (value == null) return null;
  const str = String(value).trim();
  if (!str) return null;
  return str;
}

chrome.runtime.onInstalled.addListener(() => {
  console.log('TaliTrade Monitor installed');
});

chrome.runtime.onMessageExternal.addListener((message, sender, sendResponse) => {
  const origin = sender?.origin || sender?.url || 'unknown-origin';
  const type = message?.type;

  if (!type) {
    sendResponse?.({ ok: false, error: 'missing_type' });
    return false;
  }

  if (type === 'TALI_SET_UID') {
    const telegramUserId = normalizeTelegramUserId(message?.telegramUserId);
    if (!telegramUserId) {
      sendResponse?.({ ok: false, error: 'invalid_telegram_user_id' });
      return false;
    }

    chrome.storage.local.set({ [TALI_STORAGE_KEY]: telegramUserId }, () => {
      if (chrome.runtime.lastError) {
        console.error('TaliTrade: failed to set telegram uid', chrome.runtime.lastError);
        sendResponse?.({ ok: false, error: chrome.runtime.lastError.message || 'storage_set_failed' });
        return;
      }
      console.log(`TaliTrade: stored telegram uid from ${origin}`);
      sendResponse?.({ ok: true, telegramUserId });
    });

    return true;
  }

  if (type === 'TALI_CLEAR_UID') {
    chrome.storage.local.remove(TALI_STORAGE_KEY, () => {
      if (chrome.runtime.lastError) {
        console.error('TaliTrade: failed to clear telegram uid', chrome.runtime.lastError);
        sendResponse?.({ ok: false, error: chrome.runtime.lastError.message || 'storage_remove_failed' });
        return;
      }
      console.log(`TaliTrade: cleared telegram uid from ${origin}`);
      sendResponse?.({ ok: true, cleared: true });
    });

    return true;
  }

  if (type === 'TALI_GET_UID') {
    chrome.storage.local.get(TALI_STORAGE_KEY, (result) => {
      if (chrome.runtime.lastError) {
        sendResponse?.({ ok: false, error: chrome.runtime.lastError.message || 'storage_get_failed' });
        return;
      }
      sendResponse?.({ ok: true, telegramUserId: result?.[TALI_STORAGE_KEY] || null });
    });

    return true;
  }

  sendResponse?.({ ok: false, error: 'unsupported_message_type' });
  return false;
});
