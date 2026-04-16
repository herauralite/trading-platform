// ─────────────────────────────────────────────────────────────────────────────
// TaliTrade Extension — content.js
// Scrapes FundingPips every 5s, enforces risk rules, syncs closed trades to DB.
//
// Data flow:
//   poll() every 5s  →  extractData()  →  detectTradeEvents()  →  checkRules()
//                    →  POST /extension/data  (live state + Telegram alerts)
//   scrapeAndSyncHistory() every 60s
//                    →  scrapeClosedPositionRows()
//                    →  POST /extension/trade  (sole DB writer for analytics)
// ─────────────────────────────────────────────────────────────────────────────

const BASE_URL     = 'https://trading-platform-production-70e0.up.railway.app';
const BACKEND_URL  = BASE_URL + '/extension/data';
const JOURNAL_URL  = BASE_URL + '/extension/trade';
const POLL_MS      = 5000;
const SCRAPE_MS    = 60000;
const TALI_STORAGE_KEY = 'tali_telegram_uid';  // key in chrome.storage.local
const TALI_SESSION_TOKEN_KEY = 'tali_session_token';

// ─── Telegram user helpers ────────────────────────────────────────────────────
// The web app (talitrade.com) and this content script run on different origins,
// so localStorage is NOT shared. Instead, talitrade.com writes the telegramUserId
// to chrome.storage.local via externally_connectable messaging (see manifest.json),
// and we read it here. Falls back to null gracefully if not set yet.
let _cachedTelegramUserId = null;
let _telegramUserIdLoaded = false;
let _cachedSessionToken = null;
let _sessionTokenLoaded = false;

function normalizeTelegramUserId(value) {
  if (value == null) return null;
  const str = String(value).trim();
  return str || null;
}

function setCachedTelegramUserId(value) {
  _cachedTelegramUserId = normalizeTelegramUserId(value);
  _telegramUserIdLoaded = true;
}

function clearTelegramUserIdCache() {
  _cachedTelegramUserId = null;
  _telegramUserIdLoaded = false;
}

function normalizeSessionToken(value) {
  if (value == null) return null;
  const str = String(value).trim();
  return str || null;
}

function setCachedSessionToken(value) {
  _cachedSessionToken = normalizeSessionToken(value);
  _sessionTokenLoaded = true;
}

function clearSessionTokenCache() {
  _cachedSessionToken = null;
  _sessionTokenLoaded = false;
}

async function getTelegramUserId() {
  if (_telegramUserIdLoaded) return _cachedTelegramUserId;
  try {
    const result = await chrome.storage.local.get(TALI_STORAGE_KEY);
    setCachedTelegramUserId(result[TALI_STORAGE_KEY]);
    return _cachedTelegramUserId;
  } catch(e) { return null; }
}

async function getSessionToken() {
  if (_sessionTokenLoaded) return _cachedSessionToken;
  try {
    const result = await chrome.storage.local.get(TALI_SESSION_TOKEN_KEY);
    setCachedSessionToken(result[TALI_SESSION_TOKEN_KEY]);
    return _cachedSessionToken;
  } catch(e) { return null; }
}

if (chrome?.storage?.onChanged) {
  chrome.storage.onChanged.addListener((changes, areaName) => {
    if (areaName !== 'local') return;

    if (changes[TALI_STORAGE_KEY]) {
      const nextUid = normalizeTelegramUserId(changes[TALI_STORAGE_KEY].newValue);
      const prevUid = normalizeTelegramUserId(changes[TALI_STORAGE_KEY].oldValue);
      if (nextUid) setCachedTelegramUserId(nextUid);
      else clearTelegramUserIdCache();
      if (nextUid !== prevUid) linkedAccountsThisSession.clear();
    }

    if (changes[TALI_SESSION_TOKEN_KEY]) {
      const nextToken = normalizeSessionToken(changes[TALI_SESSION_TOKEN_KEY].newValue);
      if (nextToken) setCachedSessionToken(nextToken);
      else clearSessionTokenCache();
    }
  });
}

// Auto-link an account to the logged-in Telegram user (fire-and-forget).
// Called once per account per extension session to keep prop_accounts table current.
const linkedAccountsThisSession = new Set();
async function ensureAccountLinked(accountId, accountType, accountSize, accountLabel) {
  const tgUid = await getTelegramUserId();
  const sessionToken = await getSessionToken();
  const sessionLinkKey = tgUid && accountId ? `${tgUid}:${accountId}` : null;
  if (!sessionLinkKey || !sessionToken || linkedAccountsThisSession.has(sessionLinkKey)) return;
  linkedAccountsThisSession.add(sessionLinkKey);  // optimistic — avoids duplicate calls
  try {
    const params = new URLSearchParams({
      account_id:       accountId,
      account_type:     accountType  || '',
      account_size:     accountSize  || 0,   // 0 not '' — FastAPI expects int
      label:            accountLabel || accountId,
      broker:           'fundingpips',
    });
    const res = await fetch(BASE_URL + '/auth/link-account?' + params.toString(), {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + sessionToken },
    });
    if (!res.ok) throw new Error(`link_failed_${res.status}`);
    console.log(`TaliTrade: account ${accountId} linked via canonical session contract`);
  } catch(e) {
    linkedAccountsThisSession.delete(sessionLinkKey);  // allow retry next cycle
  }
}

// ─── Rule tables ──────────────────────────────────────────────────────────────
const ACCOUNT_RULES = {
  '2step_master':    { dailyLossPct: 0.05, overallLossPct: 0.10, trailingOverall: false },
  '2step_eval':      { dailyLossPct: 0.05, overallLossPct: 0.10, trailingOverall: false },
  '2steppro_master': { dailyLossPct: 0.03, overallLossPct: 0.06, trailingOverall: false },
  '2steppro_eval':   { dailyLossPct: 0.03, overallLossPct: 0.06, trailingOverall: false },
  '1step_master':    { dailyLossPct: 0.03, overallLossPct: 0.06, trailingOverall: false },
  'zero_master':     { dailyLossPct: 0.03, overallLossPct: 0.05, trailingOverall: true  },
  'eval':            { dailyLossPct: 0.05, overallLossPct: 0.10, trailingOverall: false },
};

function getTradeIdeaLimit(accountSize, isMaster) {
  if (!isMaster) return null;
  return accountSize < 50000 ? accountSize * 0.03 : accountSize * 0.02;
}

const THRESHOLDS = { warn: 0.50, danger: 0.80, critical: 0.90 };

// ─── State ────────────────────────────────────────────────────────────────────
const state = {
  // Alert dedup — tracks last fired level per type per account
  lastAlerts:   {},

  // Live trade tracking — for risk calc and Telegram close notifications
  openTrades:       {},   // `${symbol}_${direction}` → trade details
  lastPositions:    [],
  lastBalance:      null,
  lastProfit:       null,

  // Daily loss — opening balance captured once per calendar day per account
  openingBalance:          null,
  openingBalanceDate:      null,
  openingBalanceAccountId: null,

  // 10-minute trade idea risk window
  recentLosses: [],

  // Real-time close notifications queued for next /extension/data poll
  // Cleared only after successful send so network failures retry naturally
  pendingNotifications: [],

  // Closed position scraper
  scrapedTradeKeys: new Set(),  // dedup: symbol+direction+closeTime+profit
  historyScraped:   false,      // true after first 365-day backfill
  derivedBalances:  {},         // accountId → accountSize + sum(scraped pnl)
  lastMetricsExpandAt: 0,       // throttle header dropdown clicks

  // Config passed to scraper (set in scrapeAndSyncHistory)
  scrapeConfig: null,
};


// ─── Utilities ────────────────────────────────────────────────────────────────
function parseMoneyLoose(raw) {
  if (raw == null) return null;
  const cleaned = String(raw).replace(/[^\d.+\-]/g, '');
  if (!cleaned || cleaned === '+' || cleaned === '-') return null;
  const n = parseFloat(cleaned);
  return Number.isFinite(n) ? n : null;
}

function tryExpandHeaderMetrics() {
  const now = Date.now();
  if (now - state.lastMetricsExpandAt < 15000) return false;
  state.lastMetricsExpandAt = now;
  try {
    // Find the Profit label text node, then look for a nearby clickable toggle
    const profitNode = [...document.querySelectorAll('span, div, p')]
      .find(el => el.childElementCount === 0 && el.textContent?.trim() === 'Profit');
    if (!profitNode) return false;
    const profitRect = profitNode.getBoundingClientRect();
    const toggle = [...document.querySelectorAll('button, [role="button"]')]
      .find(el => {
        const r = el.getBoundingClientRect?.();
        if (!r) return false;
        return Math.abs(r.left - profitRect.right) < 180
            && Math.abs(r.top  - profitRect.top)   < 80
            && r.width > 8 && r.height > 8;
      });
    if (toggle) {
      toggle.click();
      console.log('TaliTrade: Expanded top metrics dropdown');
      return true;
    }
  } catch (e) {
    console.warn('TaliTrade: tryExpandHeaderMetrics failed', e);
  }
  return false;
}


// ─── Account config detection ─────────────────────────────────────────────────
function detectAccountConfig() {
  const text = document.body.innerText;

  // Account ID — 7-digit number in the page (format FundingPips uses)
  const idMatch = text.match(/\b(\d{7})\b/);
  const accountId = idMatch ? idMatch[1] : null;

  // Label / phase name
  let accountLabel = '';
  for (const p of [/Evaluation-[\w-]+/i, /Student\s+\d+K/i, /Master[\w\s-]*/i]) {
    const m = text.match(p);
    if (m) { accountLabel = m[0]; break; }
  }

  // Account size from label or page header
  const sizeMap = [
    { re: /200k/i, size: 200000 }, { re: /100k/i, size: 100000 },
    { re: /50k/i,  size: 50000  }, { re: /25k/i,  size: 25000  },
    { re: /10k/i,  size: 10000  }, { re: /5k/i,   size: 5000   },
  ];
  let accountSize = 10000;
  const searchText = accountLabel + ' ' + text.slice(0, 500);
  for (const { re, size } of sizeMap) {
    if (re.test(searchText)) { accountSize = size; break; }
  }

  // Account type from label keywords
  const lbl = accountLabel.toLowerCase();
  let accountType = '2step_master';
  if      (lbl.includes('zero'))                                    accountType = 'zero_master';
  else if (lbl.includes('pro') && lbl.includes('master'))           accountType = '2steppro_master';
  else if (lbl.includes('pro'))                                     accountType = '2steppro_eval';
  else if (lbl.includes('1step') || lbl.includes('1-step'))        accountType = '1step_master';
  else if (lbl.includes('master'))                                  accountType = '2step_master';
  else if (lbl.includes('phase') || lbl.includes('student') || lbl.includes('evaluation'))
                                                                    accountType = '2step_eval';

  const isMaster = accountType.includes('master');
  const rules    = ACCOUNT_RULES[accountType] || ACCOUNT_RULES['eval'];

  return {
    accountId, accountSize, accountType, accountLabel, isMaster,
    trailingOverall: rules.trailingOverall,
    limits: {
      dailyLoss:  accountSize * rules.dailyLossPct,
      overallLoss: accountSize * rules.overallLossPct,
      tradeIdea:  getTradeIdeaLimit(accountSize, isMaster),
    },
  };
}


// ─── Page data extraction ─────────────────────────────────────────────────────
function extractData(config) {
  const text = document.body.innerText;

  // Profit (floating P&L on open positions)
  let profit = null;
  const profitMatch = text.match(/Profit\s*\n?\s*([+-]?\d[\d\s]*\.?\d*)/i);
  if (profitMatch) profit = parseFloat(profitMatch[1].replace(/\s/g, ''));

  // Balance
  let balance = null;
  const balMatch = text.match(/Balance\s+([\d\s]+\.?\d*)\s*\n/i);
  if (balMatch) balance = parseFloat(balMatch[1].replace(/\s/g, ''));
  if (!balance) {
    const fb = text.match(/Balance[^\n]*\n\s*([\d\s,]+\.?\d{2})/i);
    if (fb) balance = parseFloat(fb[1].replace(/[\s,]/g, ''));
  }

  // Equity
  let equity = null;
  const eqMatch = text.match(/Equity\s+([\d\s]+\.?\d*)/i);
  if (eqMatch) equity = parseFloat(eqMatch[1].replace(/\s/g, ''));

  // Open positions list
  let openPositionCount = 0;
  const posCountMatch = text.match(/Open Positions\s*\n?\s*(\d+)/i);
  if (posCountMatch) openPositionCount = parseInt(posCountMatch[1]);

  const positions = [];
  const posPattern = /([A-Z0-9]{3,10})\n(Buy|Sell)\n([\d.]+)\n([\d.]+)\n(?:TP:[^\n]+\n)?(?:SL:[^\n]+\n)?([+-]?[\d.]+)/gi;
  let m;
  while ((m = posPattern.exec(text)) !== null) {
    positions.push({
      symbol:    m[1], direction: m[2],
      volume:    parseFloat(m[3]), openPrice: parseFloat(m[4]),
      profit:    parseFloat(m[5]),
    });
  }

  // Try to expand the header dropdown if balance/equity are hidden
  if ((balance == null || equity == null) && tryExpandHeaderMetrics()) {
    /* next poll will catch the revealed values */
  }

  // Fallbacks
  const derived = (config?.accountId && state.derivedBalances[config.accountId]) || config?.accountSize || null;
  if (balance == null) balance = derived ?? state.lastBalance;
  if (equity == null && balance != null && profit != null) equity = balance + profit;

  return { profit, balance, equity, hasPositions: openPositionCount > 0, openPositionCount, positions };
}


// ─── Trade event detection ────────────────────────────────────────────────────
// Purpose: track open/close state for risk calc + queue Telegram close alerts.
// NOT a DB writer — the scraper owns the trades table.
function detectTradeEvents(data, config) {
  const { positions, balance, profit } = data;
  const now = new Date().toISOString();

  // Detect opened positions
  positions.forEach(pos => {
    const key = `${pos.symbol}_${pos.direction}`;
    if (!state.openTrades[key]) {
      state.openTrades[key] = {
        symbol:    pos.symbol, direction: pos.direction,
        volume:    pos.volume, openPrice: pos.openPrice,
        openTime:  now,        openBalance: balance,
      };
      console.log(`TaliTrade: OPENED ${pos.direction} ${pos.symbol} @ ${pos.openPrice}`);
    }
  });

  // Detect closed positions
  state.lastPositions.forEach(prev => {
    const key       = `${prev.symbol}_${prev.direction}`;
    const stillOpen = positions.find(p => p.symbol === prev.symbol && p.direction === prev.direction);
    if (stillOpen || !state.openTrades[key]) return;

    const openInfo  = state.openTrades[key];
    // P&L = balance delta; fall back to last known floating profit if balance unavailable
    const closedPnl = (balance != null && state.lastBalance != null)
      ? balance - state.lastBalance
      : (prev.profit ?? 0);

    console.log(`TaliTrade: CLOSED ${prev.direction} ${prev.symbol} P&L: $${closedPnl.toFixed(2)}`);

    // Track for 10-min trade idea risk window
    if (closedPnl < 0) {
      state.recentLosses.push({ loss: Math.abs(closedPnl), closedAt: Date.now() });
    }

    // Queue Telegram notification — flushed via next /extension/data poll (within 5s)
    state.pendingNotifications.push({
      accountId:     config.accountId,
      accountType:   config.accountType,
      accountSize:   config.accountSize,
      symbol:        prev.symbol,
      direction:     prev.direction,
      volume:        prev.volume,
      openPrice:     openInfo.openPrice,
      pnl:           parseFloat(closedPnl.toFixed(2)),
      balanceAfter:  balance,
      dailyLossUsed: state.openingBalance != null && balance != null
                       ? Math.max(0, state.openingBalance - balance) : null,
      dailyLossLimit:  config.limits.dailyLoss,
      overallLossUsed: balance != null ? Math.max(0, config.accountSize - balance) : null,
      overallLossLimit: config.limits.overallLoss,
    });

    delete state.openTrades[key];
  });

  state.lastPositions = [...positions];
  state.lastBalance   = balance;
  state.lastProfit    = profit;
}


// ─── Rule engine ──────────────────────────────────────────────────────────────
function getLevel(pct) {
  if (pct >= THRESHOLDS.critical) return 'critical';
  if (pct >= THRESHOLDS.danger)   return 'danger';
  if (pct >= THRESHOLDS.warn)     return 'warn';
  return null;
}

function calcTradeRisk(positions, profit, config) {
  if (!config.limits.tradeIdea) return null;

  let floatingLoss = 0;
  if (positions.length > 0) {
    positions.forEach(p => { if (p.profit < 0) floatingLoss += Math.abs(p.profit); });
  } else if (profit !== null && profit < 0) {
    floatingLoss = Math.abs(profit);
  }

  const now = Date.now();
  state.recentLosses = state.recentLosses.filter(l => now - l.closedAt < 600_000);
  const recentClosed = state.recentLosses.reduce((s, l) => s + l.loss, 0);
  const combined     = floatingLoss + recentClosed;

  return {
    floatingLoss, recentClosed, combined,
    remaining: config.limits.tradeIdea - combined,
    limit:     config.limits.tradeIdea,
    pct:       combined / config.limits.tradeIdea,
  };
}

function checkRules(data, config) {
  const alerts = [];
  const { profit, balance, equity, positions } = data;
  const { accountId, limits, isMaster } = config;

  if (!state.lastAlerts[accountId]) {
    state.lastAlerts[accountId] = { riskPerTrade: null, dailyLoss: null, overallLoss: null };
  }
  const last = state.lastAlerts[accountId];

  // Trade idea risk (master accounts only)
  if (isMaster) {
    const risk = calcTradeRisk(positions, profit, config);
    if (risk) {
      const level = getLevel(risk.pct);
      if (level && level !== last.riskPerTrade) {
        const msgs = {
          warn:     `⚠️ RISK PER TRADE IDEA — 50%\nLoss: $${risk.combined.toFixed(2)} / $${risk.limit}\nRemaining: $${risk.remaining.toFixed(2)}\nAccount: ${accountId}`,
          danger:   `🔴 RISK PER TRADE IDEA — 75%\nLoss: $${risk.combined.toFixed(2)} / $${risk.limit}\nRemaining: $${risk.remaining.toFixed(2)}\n⛔ DO NOT add!\nAccount: ${accountId}`,
          critical: `🚨 BREACH IMMINENT!\nRisk: $${risk.combined.toFixed(2)} / $${risk.limit}\nOnly $${risk.remaining.toFixed(2)} left!\n🚨 CLOSE ALL — Account: ${accountId}`,
        };
        alerts.push({ type: 'risk_per_trade_idea', level, message: msgs[level] });
        last.riskPerTrade = level;
      }
      if (!level) last.riskPerTrade = null;
    }
  }

  // Daily loss — based on opening balance (correct), not floating equity
  const dailyUsed = state.openingBalance != null && balance != null
    ? Math.max(0, state.openingBalance - balance) : null;
  if (dailyUsed !== null) {
    const pct   = dailyUsed / limits.dailyLoss;
    const rem   = (limits.dailyLoss - dailyUsed).toFixed(2);
    const level = getLevel(pct);
    if (level && level !== last.dailyLoss) {
      const msgs = {
        warn:     `⚠️ DAILY LOSS 50%\n$${dailyUsed.toFixed(2)} / $${limits.dailyLoss}\nRemaining: $${rem}\nAccount: ${accountId}`,
        danger:   `🔴 DAILY LOSS 80%\n$${dailyUsed.toFixed(2)} / $${limits.dailyLoss}\nRemaining: $${rem}\nAccount: ${accountId}`,
        critical: `🚨 DAILY LOSS 90%+\n$${dailyUsed.toFixed(2)} / $${limits.dailyLoss}\nOnly $${rem} left!\n🚨 STOP — Account: ${accountId}`,
      };
      alerts.push({ type: 'daily_loss', level, message: msgs[level] });
      last.dailyLoss = level;
    }
    if (!level) last.dailyLoss = null;
  }

  // Overall loss
  if (balance !== null) {
    const used  = Math.max(0, config.accountSize - balance);
    const pct   = used / limits.overallLoss;
    const rem   = (limits.overallLoss - used).toFixed(2);
    const level = getLevel(pct);
    if (level && level !== last.overallLoss) {
      const msgs = {
        warn:     `⚠️ OVERALL LOSS 50%\n$${used.toFixed(2)} / $${limits.overallLoss}\nBuffer: $${rem}\nAccount: ${accountId}`,
        danger:   `🔴 OVERALL LOSS 80%\n$${used.toFixed(2)} / $${limits.overallLoss}\nBuffer: $${rem}\nAccount: ${accountId}`,
        critical: `🚨 ACCOUNT CRITICAL\n$${used.toFixed(2)} / $${limits.overallLoss}\nOnly $${rem} left!\nAccount: ${accountId}`,
      };
      alerts.push({ type: 'overall_loss', level, message: msgs[level] });
      last.overallLoss = level;
    }
    if (!level) last.overallLoss = null;
  }

  return alerts;
}


// ─── Main poll loop ───────────────────────────────────────────────────────────
async function poll() {
  const config = detectAccountConfig();
  if (!config.accountId) return;

  const data = extractData(config);
  detectTradeEvents(data, config);

  // Opening balance — captured once per calendar day per account.
  // This is the correct daily loss basis: how far balance has dropped today.
  const todayStr      = new Date().toISOString().slice(0, 10);
  const dayChanged    = state.openingBalanceDate !== todayStr;
  const acctChanged   = state.openingBalanceAccountId !== config.accountId;
  if (data.balance != null && (dayChanged || acctChanged || state.openingBalance == null)) {
    state.openingBalance          = data.balance;
    state.openingBalanceDate      = todayStr;
    state.openingBalanceAccountId = config.accountId;
    console.log(`TaliTrade: Opening balance $${data.balance} set for ${config.accountId} on ${todayStr}`);
  }

  const alerts  = checkRules(data, config);
  const risk    = calcTradeRisk(data.positions, data.profit, config);
  const dailyUsed   = state.openingBalance != null && data.balance != null
    ? Math.max(0, state.openingBalance - data.balance) : null;
  const overallUsed = data.balance != null
    ? Math.max(0, config.accountSize - data.balance) : null;

  // Snapshot pending notifications — cleared only after successful send
  const closedTrades = [...state.pendingNotifications];

  const telegramUserId = await getTelegramUserId();

  const payload = {
    profit:           data.profit,
    balance:          data.balance,
    equity:           data.equity,
    accountId:        config.accountId,
    accountType:      config.accountType,
    accountSize:      config.accountSize,
    accountLabel:     config.accountLabel,
    isMaster:         config.isMaster,
    hasPositions:     data.hasPositions,
    openPositionCount: data.openPositionCount,
    positions:        data.positions,
    closedTrades,
    riskPerTradeIdea: risk ? {
      combined:    risk.combined, floatingLoss: risk.floatingLoss,
      recentClosed: risk.recentClosed, remaining: risk.remaining,
      limit:       risk.limit, pct: Math.round(risk.pct * 100), applicable: true,
    } : { applicable: false },
    dailyLoss: {
      used:      dailyUsed,
      remaining: dailyUsed != null ? config.limits.dailyLoss - dailyUsed : null,
      limit:     config.limits.dailyLoss,
      pct:       dailyUsed != null ? Math.round(dailyUsed / config.limits.dailyLoss * 100) : null,
    },
    overallLoss: {
      used:      overallUsed,
      remaining: overallUsed != null ? config.limits.overallLoss - overallUsed : null,
      limit:     config.limits.overallLoss,
      pct:       overallUsed != null ? Math.round(overallUsed / config.limits.overallLoss * 100) : null,
      trailing:  config.trailingOverall,
    },
    alerts,
    timestamp:       new Date().toISOString(),
    url:             window.location.href,
    telegramUserId,  // null if user hasn't linked Telegram yet
  };

  // Auto-link account to Telegram user (no-op if already done this session or not logged in)
  ensureAccountLinked(config.accountId, config.accountType, config.accountSize, config.accountLabel);

  console.log(`TaliTrade [${config.accountId}] ${config.accountType} ${config.accountSize/1000}K:`, {
    balance: data.balance, equity: data.equity, profit: data.profit,
    daily:   dailyUsed   != null ? `$${dailyUsed.toFixed(2)}/$${config.limits.dailyLoss}` : 'N/A',
    overall: overallUsed != null ? `$${overallUsed.toFixed(2)}/$${config.limits.overallLoss}` : 'N/A',
    openPositions: data.openPositionCount, alerts: alerts.length || 'none',
  });

  try {
    const res = await fetch(BACKEND_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(payload),
    });
    if (!res.ok) {
      console.warn(`TaliTrade: extension/data returned HTTP ${res.status}`);
      return;
    }
    const ack = await res.json().catch(() => ({}));
    if (!telegramUserId || !ack.telegramLinked) {
      console.warn(`TaliTrade: Telegram not linked for account ${config.accountId}; heartbeat sent without telegramUserId`);
    }
    // Clear only the notifications we just sent — new ones queued during the fetch stay
    if (closedTrades.length) {
      state.pendingNotifications = state.pendingNotifications.filter(n => !closedTrades.includes(n));
    }
  } catch (e) {
    console.error('TaliTrade: poll send failed', e);
    // pendingNotifications NOT cleared — retries on next poll
  }
}


// ─── Closed positions scraper

function getClosedPositionsRoot() {
  const selectors = [
    'trade-closed-positions-desktop',
    'trade-closed-positions',
    '[data-testid="closed-positions-desktop"]',
    '[data-testid*="closed-positions"]',
  ];
  for (const sel of selectors) {
    const el = document.querySelector(sel);
    if (el) return el;
  }
  return null;
}

// ─────────────────────────────────────────────────
// Sole writer to the trades table. Reads from the FundingPips closed positions
// tab so closed_at timestamps and pnl values are authoritative.


function getClosedPositionsTab() {
  const selectors = [
    '[tabid="closed"]',
    '[data-testid*="closed"][role="tab"]',
    '[role="tab"][aria-controls*="closed"]',
  ];
  for (const sel of selectors) {
    const el = document.querySelector(sel);
    if (el) return el;
  }
  const byText = [...document.querySelectorAll('button, [role="tab"], [class*="tab"]')]
    .find(el => /closed\s*positions/i.test(el.innerText || ''));
  return byText || null;
}

async function waitForClosedPositionsReadiness({ timeoutMs = 6000, pollMs = 250 } = {}) {
  const startedAt = Date.now();
  let tabSeen = false;
  let rootSeen = false;

  while (Date.now() - startedAt < timeoutMs) {
    const closedTab = getClosedPositionsTab();
    const closedRoot = getClosedPositionsRoot();

    if (closedTab) tabSeen = true;
    if (closedRoot) rootSeen = true;

    if (closedTab && closedRoot) {
      console.log(`TaliTrade: closed positions readiness ok after ${Date.now() - startedAt}ms`);
      return { closedTab, closedRoot };
    }

    await new Promise(r => setTimeout(r, pollMs));
  }

  const waitedMs = Date.now() - startedAt;
  console.warn(`TaliTrade: closed positions readiness timed out after ${waitedMs}ms`, {
    tabSeen,
    rootSeen,
  });
  return {
    closedTab: getClosedPositionsTab(),
    closedRoot: getClosedPositionsRoot(),
  };
}

function scrapeClosedPositionRows() {
  const container = getClosedPositionsRoot();
  if (!container) return [];

  const acctSz = state.scrapeConfig?.accountSize || 10000;
  const rows   = [];

  const rowSelectors = [
    '.ui-list__row-wrapper > *',
    'ui-list-row',
    '[data-testid*="closed"][data-testid*="row"]',
    '[data-testid*="row"]',
    '[role="row"]',
    '.ui-list__inner-container > div:not(.ui-list__header-wrapper)',
  ];
  let rowEls = [];
  let rowSelectorUsed = '';
  for (const sel of rowSelectors) {
    const matched = [...container.querySelectorAll(sel)];
    if (matched.length) {
      rowEls = matched;
      rowSelectorUsed = sel;
      break;
    }
  }

  if (!rowEls.length) {
    console.warn('TaliTrade: closed positions root found but no rows matched', {
      tag: container.tagName?.toLowerCase(),
      className: container.className || '',
    });
  } else {
    console.log(`TaliTrade: closed positions rows selector = ${rowSelectorUsed} (${rowEls.length} rows)`);
  }

  if (!rowEls.length) {
    console.warn('TaliTrade: closed positions root found but no rows matched', {
      tag: container.tagName?.toLowerCase(),
      className: container.className || '',
    });
  }

  rowEls.forEach(row => {
    const text = row.innerText || row.textContent || '';

    // Symbol — prefer explicit element, fall back to pattern match
    const symbolEl = row.querySelector('[data-testid*="symbol"], [class*="symbol"]');
    const symMatch = text.match(/\b(DJI30|NAS100|SP500|XAUUSD|BTCUSD|EURUSD|GBPUSD|USDJPY|[A-Z]{3,10})\b/);
    const symbol   = symbolEl?.innerText?.trim() || (symMatch ? symMatch[1] : null);
    if (!symbol) return;

    // Direction
    const dirMatch  = text.match(/\b(Buy|Sell)\b/i);
    const direction = dirMatch ? dirMatch[1] : null;

    // Numeric suffix values in grid order: Volume | OpenPrice | ClosePrice | [Swap] | Profit
    const suffixVals = [...row.querySelectorAll('ui-suffix [data-testid="ui-suffix"], [data-testid="ui-suffix"]')]
      .map(el => el.innerText?.replace(/[^\d.\-+]/g, '').trim())
      .filter(v => v && v !== '');

    const volume    = suffixVals[0] ? parseFloat(suffixVals[0]) : null;
    const openPrice = suffixVals[1] ? parseFloat(suffixVals[1]) : null;
    const closePrice= suffixVals[2] ? parseFloat(suffixVals[2]) : null;

    // ── Profit extraction ────────────────────────────────────────────────────
    // Key insight: index CFD prices (DJI30 ~47k, NAS100 ~21k) are always larger
    // than account_size. P&L on a funded account is always << account_size.
    // We use this to cleanly separate close price from profit.
    let profit = null;

    // 1. Element with color class (FundingPips puts ui-color-success/danger on the profit span)
    const colorEl = row.querySelector(
      '[class*="color-success"],[class*="color-danger"],' +
      '[class*="txt-success"],[class*="txt-danger"],' +
      '[class*="positive"],[class*="negative"]'
    );
    if (colorEl) {
      const v = parseMoneyLoose(colorEl.innerText?.trim());
      if (v !== null && Math.abs(v) <= acctSz) profit = v;
    }

    // 2. Explicitly signed value (reliable for losses — always show the minus sign)
    if (profit === null) {
      const signed = text.match(/[+\-]\s*\d[\d,.]*(?:\.\d+)?/g) || [];
      if (signed.length) {
        const v = parseMoneyLoose(signed[signed.length - 1]);
        if (v !== null && Math.abs(v) <= acctSz) profit = v;
      }
    }

    // 3. Scan suffixVals from the end — take the last value that fits within accountSize
    //    Works regardless of whether FundingPips renders 4 or 5 columns (with/without swap)
    if (profit === null) {
      for (let i = suffixVals.length - 1; i >= 0; i--) {
        const v = parseMoneyLoose(suffixVals[i]);
        if (v !== null && Math.abs(v) <= acctSz) { profit = v; break; }
      }
    }

    // Hard reject — if value is still price-sized, skip the row entirely
    if (profit !== null && Math.abs(profit) > acctSz) {
      console.warn(`TaliTrade: Rejected suspicious pnl=${profit} for ${symbol}`);
      profit = null;
    }

    // Close time — take the LAST timestamp in the row (open time comes first)
    const timePats  = text.match(/\d{2}[.\/\-]\d{2}[.\/\-]\d{4}[\s,]+\d{2}:\d{2}(?::\d{2})?/g) || [];
    const closeTime = timePats.length ? timePats[timePats.length - 1].trim() : null;

    if (!symbol || profit === null) return;
    rows.push({ symbol, direction, volume, openPrice, closePrice, closeTime, profit });
  });

  return rows;
}

function makeTradeKey(row) {
  return `${row.symbol}_${row.direction}_${row.closeTime}_${row.profit}`;
}

function resolveClosedPositionsScrollTarget() {
  const root = getClosedPositionsRoot();
  if (!root) {
    console.warn('TaliTrade: closed positions root not found');
    return null;
  }

  const isScrollable = (el) => !!el && ((el.scrollHeight - el.clientHeight) > 20);

  // Some FundingPips builds keep the scroll container on a parent wrapper.
  let parent = root.parentElement;
  while (parent) {
    if (isScrollable(parent)) {
      console.log(`TaliTrade: closed positions scroll target parent = ${parent.tagName.toLowerCase()}${parent.className ? '.' + parent.className.toString().replace(/\s+/g, '.') : ''}`);
      return parent;
    }
    parent = parent.parentElement;
  }

  const candidates = [
    '.cdk-virtual-scroll-viewport',
    '.ui-list__inner-container',
    '.ui-list__content',
    '.ui-list',
    '[class*="virtual-scroll"]',
    '[class*="scroll"]',
  ];

  // Prefer the first element that is actually scrollable.
  for (const selector of candidates) {
    for (const el of root.querySelectorAll(selector)) {
      if (isScrollable(el)) {
        console.log(`TaliTrade: closed positions scroll target = ${selector} (h=${el.clientHeight}, sh=${el.scrollHeight})`);
        return el;
      }
    }
  }

  if (isScrollable(root)) {
    console.log(`TaliTrade: closed positions scroll target = root (h=${root.clientHeight}, sh=${root.scrollHeight})`);
    return root;
  }

  // Fallback: inspect descendants and find the largest scrollable region.
  let best = null;
  for (const el of root.querySelectorAll('*')) {
    const delta = el.scrollHeight - el.clientHeight;
    if (delta > 20 && (!best || delta > (best.scrollHeight - best.clientHeight))) {
      best = el;
    }
  }

  if (best) {
    console.log(`TaliTrade: closed positions scroll target fallback = ${best.tagName.toLowerCase()}${best.className ? '.' + best.className.toString().replace(/\s+/g, '.') : ''}`);
    return best;
  }

  console.warn('TaliTrade: no scrollable closed positions container found; using root element');
  return root;
}

async function scrapeAndSyncHistory(config) {
  // Store config for scrapeClosedPositionRows profit sanity guard
  state.scrapeConfig = config;

  // Wait briefly for the Closed Positions UI to mount on startup.
  const { closedTab } = await waitForClosedPositionsReadiness();

  // Ensure the Closed Positions tab is active
  if (!closedTab) {
    console.warn('TaliTrade: closed positions tab not found');
    return;
  }
  closedTab.click();
  await new Promise(r => setTimeout(r, 500)); // allow lazy table mount after tab switch

  // First run: set filter to Last 365 days for full history backfill.
  // All subsequent runs keep whatever filter is set (usually Last 24h is fine
  // since scrapedTradeKeys deduplicates already-seen rows).
  if (!state.historyScraped) {
    await new Promise(r => setTimeout(r, 800));
    const filterBtn = document.querySelector('[data-testid="closed-positions-actions-desktop-filter-button"]');
    if (filterBtn) {
      filterBtn.click();
      await new Promise(r => setTimeout(r, 600));

      // "Last 365 days" option may render in a portal — search entire document
      const opt365 = [...document.querySelectorAll('label, li, [role="option"], [class*="option"], [class*="item"], [class*="range"]')]
        .find(el => el.innerText?.trim() === 'Last 365 days');
      if (opt365) {
        opt365.click();
        console.log('TaliTrade: Set filter → Last 365 days');
      } else {
        const span365 = [...document.querySelectorAll('span, div, label')]
          .find(el => el.innerText?.trim() === 'Last 365 days');
        if (span365) {
          (span365.closest('label, li, [class*="item"], [class*="option"]') || span365).click();
          console.log('TaliTrade: Set filter via span → Last 365 days');
        }
      }
      await new Promise(r => setTimeout(r, 400));
      document.body.click(); // close dropdown
    }
  }

  // Wait for Angular to render the rows
  await new Promise(r => setTimeout(r, 1500));

  // Scroll through the virtualized list to load ALL rows
  const allRowsMap = new Map(); // key → row, deduped
  const scrollContainer = resolveClosedPositionsScrollTarget();

  if (scrollContainer) {
    let lastCount = -1;
    let staleRounds = 0;
    scrollContainer.scrollTop = 0;
    await new Promise(r => setTimeout(r, 400));

    while (staleRounds < 3) {
      const batch = scrapeClosedPositionRows();
      batch.forEach(row => {
        const k = makeTradeKey(row);
        if (!allRowsMap.has(k)) allRowsMap.set(k, row);
      });

      if (allRowsMap.size === lastCount) {
        staleRounds++;
      } else {
        staleRounds = 0;
        lastCount = allRowsMap.size;
      }

      // Scroll down by one page height.
      // Some virtualized lists only react to wheel events, so dispatch one as backup.
      const step = Math.max(240, scrollContainer.clientHeight || 400);
      const before = scrollContainer.scrollTop;
      scrollContainer.scrollTop += step;
      scrollContainer.dispatchEvent(new WheelEvent('wheel', { deltaY: step, bubbles: true }));
      if (scrollContainer.scrollTop === before) {
        scrollContainer.scrollBy?.({ top: step, behavior: 'auto' });
        scrollContainer.dispatchEvent(new Event('scroll', { bubbles: true }));
      }
      await new Promise(r => setTimeout(r, 600));

      const atBottom = (scrollContainer.scrollTop + scrollContainer.clientHeight) >= (scrollContainer.scrollHeight - 4);
      if (atBottom && staleRounds >= 1) break;
    }

    // Reset scroll to top so the UI looks normal
    scrollContainer.scrollTop = 0;
  }

  const rows = allRowsMap.size > 0 ? [...allRowsMap.values()] : scrapeClosedPositionRows();

  if (!rows.length) {
    if (!state.historyScraped) {
      console.log('TaliTrade: No closed positions found (new account?)');
      state.historyScraped = true;
    }
    return;
  }

  console.log(`TaliTrade: Scraped ${rows.length} closed positions`);

  // Update derived balance from scraped history
  const derivedBal = config.accountSize + rows.reduce((sum, r) => sum + (Number.isFinite(r.profit) ? r.profit : 0), 0);
  state.derivedBalances[config.accountId] = parseFloat(derivedBal.toFixed(2));
  if (state.lastBalance == null) state.lastBalance = state.derivedBalances[config.accountId];

  // POST each unseen trade to the backend
  let sent = 0;
  for (const row of rows) {
    const key = makeTradeKey(row);
    if (state.scrapedTradeKeys.has(key)) continue;

    let closedAt = null;
    if (row.closeTime) {
      try {
        const normalized = row.closeTime.replace(/(\d{2})[.\/](\d{2})[.\/](\d{4})/, '$3-$2-$1');
        closedAt = new Date(normalized).toISOString();
      } catch (e) { /* leave null */ }
    }

    const trade = {
      accountId:        config.accountId,
      accountType:      config.accountType,
      accountSize:      config.accountSize,
      symbol:           row.symbol,
      direction:        row.direction,
      volume:           row.volume,
      openPrice:        row.openPrice,
      closePrice:       row.closePrice,
      pnl:              row.profit,
      balanceAfter:     null,  // not available from closed positions tab
      equityAfter:      null,
      dailyLossUsed:    null,
      dailyLossLimit:   config.limits.dailyLoss,
      overallLossUsed:  null,
      overallLossLimit: config.limits.overallLoss,
      closedAt,
      source: 'scraper',
    };

    try {
      await fetch(JOURNAL_URL, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify(trade),
      });
      state.scrapedTradeKeys.add(key);
      sent++;
    } catch (e) {
      console.error('TaliTrade: Failed to sync closed position', e);
    }
  }

  if (sent > 0) console.log(`TaliTrade: Synced ${sent} new trades to backend`);
  state.historyScraped = true;
}


// ─── Boot ─────────────────────────────────────────────────────────────────────
console.log('%cTaliTrade Active', 'color:#00ff88;font-size:14px;font-weight:bold;');

poll();
setInterval(poll, POLL_MS);

setTimeout(async () => {
  const config = detectAccountConfig();
  if (config.accountId) await scrapeAndSyncHistory(config);
}, 8000);

setInterval(async () => {
  const config = detectAccountConfig();
  if (config.accountId) await scrapeAndSyncHistory(config);
}, SCRAPE_MS);
