const HOST_MATCH = ['fundingpips.com', 'match-trader.com'];

function detect(url) {
  return HOST_MATCH.some((host) => (url || '').includes(host));
}

function pickText(selectors) {
  for (const selector of selectors) {
    const node = document.querySelector(selector);
    if (node && node.textContent) {
      const value = node.textContent.trim();
      if (value) return value;
    }
  }
  return null;
}

function toNumber(raw) {
  if (!raw) return undefined;
  const parsed = Number(String(raw).replace(/[^0-9.-]/g, ''));
  return Number.isFinite(parsed) ? parsed : undefined;
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type !== 'TALITRADE_COLLECT_STATE') return false;
  if (!detect(window.location.href)) {
    sendResponse(null);
    return false;
  }

  const tab = message.tab || { id: null, url: window.location.href, title: document.title };
  const accountRef =
    pickText(['[data-account-id]', '[data-testid="account-id"]', '[data-account-number]']) ||
    new URL(window.location.href).searchParams.get('account') ||
    `tab-${tab.id || 'unknown'}`;

  sendResponse({
    platformSession: {
      adapter_key: 'fundingpips_browser',
      platform_key: 'fundingpips',
      tab_id: String(tab.id || ''),
      tab_url: tab.url || window.location.href,
      platform_account_ref: accountRef,
      session_ref: `fundingpips:${accountRef}`,
      status: 'active',
      capabilities: {
        canReadState: true,
        canPlaceOrder: true,
        canClosePosition: true,
        canModifyPosition: true,
        canCancelOrder: true,
      },
      metadata: { title: tab.title || document.title || null },
    },
    accountState: {
      adapter_key: 'fundingpips_browser',
      platform_key: 'fundingpips',
      platform_name: 'FundingPips',
      platform_account_ref: accountRef,
      display_label: `FundingPips ${accountRef}`,
      tab_id: String(tab.id || ''),
      session_ref: `fundingpips:${accountRef}`,
      snapshot: {
        timestamp: new Date().toISOString(),
        balance: toNumber(pickText(['[data-balance]', '[data-testid="balance"]'])),
        equity: toNumber(pickText(['[data-equity]', '[data-testid="equity"]'])),
      },
      positions: [],
      orders: [],
    },
  });

  return false;
});
