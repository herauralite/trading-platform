import { AdapterError } from '../../types/adapter.js';
import { FUNDINGPIPS_HOST_MATCH, FUNDINGPIPS_SELECTORS } from './selectors.js';

function pickText(selectors, doc = document) {
  for (const selector of selectors) {
    const node = doc.querySelector(selector);
    if (node && node.textContent) {
      const value = node.textContent.trim();
      if (value) return value;
    }
  }
  return null;
}

function toNumber(raw) {
  if (!raw) return undefined;
  const normalized = String(raw).replace(/[^0-9.-]/g, '');
  const parsed = Number(normalized);
  return Number.isFinite(parsed) ? parsed : undefined;
}

export class FundingPipsBrowserAdapter {
  constructor() {
    this.adapterKey = 'fundingpips_browser';
    this.platformKey = 'fundingpips';
    this.capabilities = {
      canReadState: true,
      canPlaceOrder: true,
      canClosePosition: true,
      canModifyPosition: true,
      canCancelOrder: true,
    };
  }

  async detect(url) {
    return FUNDINGPIPS_HOST_MATCH.some((host) => (url || '').includes(host));
  }

  buildPlatformSession(tab) {
    const platformAccountRef = this.derivePlatformAccountRef(document, tab?.url);
    return {
      adapter_key: this.adapterKey,
      platform_key: this.platformKey,
      tab_id: String(tab?.id ?? ''),
      tab_url: tab?.url || null,
      platform_account_ref: platformAccountRef,
      session_ref: platformAccountRef ? `${this.platformKey}:${platformAccountRef}` : null,
      status: 'active',
      capabilities: this.capabilities,
      metadata: {
        title: tab?.title || document.title || null,
      },
    };
  }

  derivePlatformAccountRef(doc = document, url = window.location.href) {
    const fromDom = pickText(FUNDINGPIPS_SELECTORS.accountRef, doc);
    if (fromDom) return fromDom;

    try {
      const parsed = new URL(url || window.location.href);
      const queryAccount = parsed.searchParams.get('account') || parsed.searchParams.get('accountId');
      if (queryAccount) return queryAccount;
    } catch (_error) {
      // ignore URL parsing issue
    }

    return null;
  }

  async readAccountState(tab) {
    const platformAccountRef = this.derivePlatformAccountRef(document, tab?.url) || `tab-${tab?.id || 'unknown'}`;
    return {
      adapter_key: this.adapterKey,
      platform_key: this.platformKey,
      platform_name: 'FundingPips',
      platform_account_ref: platformAccountRef,
      display_label: `FundingPips ${platformAccountRef}`,
      snapshot: {
        timestamp: new Date().toISOString(),
        balance: toNumber(pickText(FUNDINGPIPS_SELECTORS.balance)),
        equity: toNumber(pickText(FUNDINGPIPS_SELECTORS.equity)),
      },
      positions: [],
      orders: [],
      tab_id: String(tab?.id ?? ''),
      session_ref: `${this.platformKey}:${platformAccountRef}`,
    };
  }

  async placeOrder(_request) {
    throw new AdapterError('not_implemented', 'placeOrder wiring pending selector hardening');
  }

  async closePosition(_request) {
    throw new AdapterError('not_implemented', 'closePosition wiring pending selector hardening');
  }

  async modifyPosition(_request) {
    throw new AdapterError('not_implemented', 'modifyPosition wiring pending selector hardening');
  }

  async cancelOrder(_request) {
    throw new AdapterError('not_implemented', 'cancelOrder wiring pending selector hardening');
  }
}
