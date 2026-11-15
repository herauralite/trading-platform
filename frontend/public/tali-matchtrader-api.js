// Match-Trader API accounts for the TaliTrade dashboard (served at /app from public/app.html).
//
// window.TaliMatchTraderApi  - small HTTP client for /connectors/matchtrader-api/*
// window.TaliMatchTrader     - provider bridge: account cards, snapshot -> dashboard state,
//                              history paging, manual sync, token form handling
//
// Rules this file keeps:
//  * the broker token is sent once to the backend and never kept: not in a variable that outlives
//    the request, not in localStorage/sessionStorage, not in a URL, not in a log line;
//  * every screen value comes from the TaliTrade backend's stored data. GETs never contact the
//    broker; only Connect and "Sync now" do;
//  * unknown is shown as unknown ("—"), never as 0 or a guessed default;
//  * text from the broker (symbols, broker name) is written with textContent, never as HTML;
//  * a response is applied only if the session, the selected account and the context generation
//    are still the ones it was requested for.
(function initTaliMatchTrader(global) {
  'use strict';

  var BASE_PATH = '/connectors/matchtrader-api';
  var ACCOUNT_KEY = /^mtapi:(\d+)$/;
  var SNAPSHOT_MIN_INTERVAL_MS = 8000;
  var IMPORT_POLL_MS = 10000;
  var IMPORT_POLL_MAX = 30;
  var SYNC_POLL_MS = 1500;      // how often a queued refresh is checked
  var SYNC_POLL_MAX = 60;       // ...and for how long (90 s) before the page stops waiting
  var RECENT_BACKFILL_DAYS = 7;

  // ───────────────────────── HTTP client ─────────────────────────
  function ApiError(status, message, code) {
    this.name = 'TaliMatchTraderApiError';
    this.status = status;
    this.message = message;
    this.code = code || null;
  }
  ApiError.prototype = Object.create(Error.prototype);

  function createClient(deps) {
    // deps are read at request time so a new session is always used.
    function request(method, path, options) {
      options = options || {};
      var session = deps.getSessionToken();
      if (!session) return Promise.reject(new ApiError(401, 'You are signed out.', 'no_session'));
      var init = { method: method, headers: { Authorization: 'Bearer ' + session }, signal: options.signal, cache: 'no-store' };
      if (options.body !== undefined) {
        init.headers['Content-Type'] = 'application/json';
        init.body = JSON.stringify(options.body);
      }
      return deps.fetch(deps.getBaseApiUrl() + BASE_PATH + path, init).then(function (res) {
        return res.text().then(function (raw) {
          var data = null;
          try { data = raw ? JSON.parse(raw) : null; } catch { data = null; }
          if (!res.ok) {
            // Only the server's own short message is surfaced; never the request or its body.
            var detail = data && typeof data.detail === 'string' ? data.detail : 'Request failed (' + res.status + ').';
            throw new ApiError(res.status, detail, res.status === 404 ? 'not_found' : null);
          }
          return data || {};
        });
      }, function (err) {
        if (err && err.name === 'AbortError') throw err;
        throw new ApiError(0, 'Could not reach TaliTrade. Check your connection and try again.', 'network');
      });
    }
    function id(connectionId) {
      var n = Number(connectionId);
      if (!Number.isInteger(n) || n <= 0) throw new ApiError(400, 'Invalid connection.', 'bad_id');
      return String(n);
    }
    return {
      listConnections: function (o) { return request('GET', '/connections', o); },
      connect: function (o) { return request('POST', '/connections', { body: { token: o.token }, signal: o.signal }); },
      // Queues a refresh (HTTP 202). It has NOT synchronized anything yet: follow up with getSyncStatus().
      sync: function (connectionId, o) { return request('POST', '/connections/' + id(connectionId) + '/sync', o); },
      getSyncStatus: function (connectionId, o) { return request('GET', '/connections/' + id(connectionId) + '/sync', o); },
      getSnapshot: function (connectionId, o) { return request('GET', '/connections/' + id(connectionId) + '/snapshot', o); },
      listTrades: function (connectionId, o) {
        o = o || {};
        var query = '?limit=' + encodeURIComponent(String(o.limit || 20)) + (o.cursor ? '&cursor=' + encodeURIComponent(o.cursor) : '');
        return request('GET', '/connections/' + id(connectionId) + '/trades' + query, o);
      },
      startRecentBackfill: function (connectionId, o) { return request('POST', '/connections/' + id(connectionId) + '/backfill', { body: { from_ms: o.fromMs }, signal: o.signal }); },
      rotate: function (connectionId, o) { return request('POST', '/connections/' + id(connectionId) + '/rotate', { body: { token: o.token }, signal: o.signal }); },
      disconnect: function (connectionId, o) { return request('DELETE', '/connections/' + id(connectionId), o); }
    };
  }

  // ───────────────────────── pure helpers (unit tested) ─────────────────────────
  function toNumber(value) {
    if (value === null || value === undefined || value === '') return null;
    var n = Number(value);
    return Number.isFinite(n) ? n : null;
  }

  // Currency-aware, sign-preserving. Display only: no arithmetic is done on the result.
  function formatMoney(value, currency, options) {
    var n = toNumber(value);
    if (n === null) return '—';
    options = options || {};
    var formatted;
    try {
      formatted = new Intl.NumberFormat('en-US', { style: 'currency', currency: currency || 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(Math.abs(n));
    } catch {
      formatted = Math.abs(n).toFixed(2) + (currency ? ' ' + currency : '');
    }
    if (n < 0) return '-' + formatted;
    return options.signed && n > 0 ? '+' + formatted : formatted;
  }

  function relativeTime(iso, nowMs) {
    if (!iso) return null;
    var t = new Date(iso).getTime();
    if (!Number.isFinite(t)) return null;
    var seconds = Math.max(0, Math.round((nowMs - t) / 1000));
    if (seconds < 10) return 'just now';
    if (seconds < 90) return seconds + 's ago';
    var minutes = Math.round(seconds / 60);
    if (minutes < 90) return minutes + 'm ago';
    var hours = Math.round(minutes / 60);
    return hours < 48 ? hours + 'h ago' : Math.round(hours / 24) + 'd ago';
  }

  function isApiAccountKey(key) {
    return typeof key === 'string' && ACCOUNT_KEY.test(key);
  }

  function connectionIdFromKey(key) {
    var match = typeof key === 'string' ? ACCOUNT_KEY.exec(key) : null;
    return match ? Number(match[1]) : null;
  }

  var CONNECTION_STATUS_TEXT = {
    needs_reauth: ['Reconnect needed', 'The broker rejected the saved token. Add a new read-only token.', 'txt-red'],
    paused: ['Paused', 'Syncing is paused for this account.', 'txt-dim'],
    forbidden: ['Not permitted', 'The token cannot read the data TaliTrade needs.', 'txt-orange'],
    pending_review: ['Needs review', 'The broker platform could not be verified.', 'txt-orange'],
    error: ['Needs attention', 'The saved token could not be used. Reconnect this account.', 'txt-red']
  };

  // Everything the dashboard shows for an API account, derived from one stored snapshot.
  function deriveDisplayState(snapshot, nowMs) {
    var c = snapshot.connection || {};
    var currency = c.currency || 'USD';
    var s = snapshot.summary;
    var fresh = snapshot.freshness || {};
    var summaryFresh = fresh.summary || {};
    var positionsFresh = fresh.positions || {};
    var age = relativeTime(summaryFresh.last_success_at, nowMs);
    var sourceLabel = (c.broker_name || 'Match-Trader') + ' API';

    var syncVal, syncSub, syncTone, topText, topColor;
    var special = CONNECTION_STATUS_TEXT[c.status];
    if (special) {
      syncVal = special[0]; syncSub = special[1]; syncTone = special[2];
      topText = special[0].toLowerCase(); topColor = 'var(--orange)';
    } else if (!summaryFresh.last_success_at) {
      syncVal = 'Connected'; syncTone = 'txt-dim';
      syncSub = summaryFresh.last_error ? 'First sync failed (' + summaryFresh.last_error + '). Try Sync now.' : 'First sync pending';
      topText = 'connected · first sync pending'; topColor = 'var(--t3)';
    } else if (summaryFresh.is_stale) {
      syncVal = 'Delayed'; syncTone = 'txt-orange';
      syncSub = 'Last synced ' + age + (summaryFresh.last_error ? ' · ' + summaryFresh.last_error : '');
      topText = 'delayed · ' + age; topColor = 'var(--orange)';
    } else {
      syncVal = 'Synced'; syncTone = 'txt-green';
      syncSub = 'Updated ' + age + ' · ' + sourceLabel;
      topText = 'synced ' + age; topColor = 'var(--blue)';
    }
    if (snapshot.background_refresh && snapshot.background_refresh.enabled === false && !special) {
      syncSub += ' · background refresh is off';
    }

    var positionsVal = '—', positionsSub = 'Positions unavailable', positionsTone = 'txt-dim';
    if (positionsFresh.status === 'not_permitted') {
      positionsSub = 'Not permitted by this token';
    } else if (Array.isArray(snapshot.positions)) {
      positionsVal = String(snapshot.positions.length) + ' open';
      positionsSub = positionsFresh.is_stale ? 'As of ' + relativeTime(positionsFresh.last_success_at, nowMs) + ' (delayed)' : 'Open positions';
      positionsTone = snapshot.positions.length ? 'txt-blue' : 'txt-dim';
    }

    var stale = s && summaryFresh.is_stale ? ' · delayed' : '';
    var overviewStatus = {
      balance: s ? toNumber(s.balance) : null,
      balanceText: s ? formatMoney(s.balance, currency) : '—',
      balanceSub: s ? 'Broker balance' + stale : 'First sync pending',
      balanceTone: s ? 'txt-white' : 'txt-dim',
      equity: s ? toNumber(s.equity) : null,
      equityText: s ? formatMoney(s.equity, currency) : '—',
      equitySub: s ? 'Broker equity' + (toNumber(s.profit) !== null ? ' · floating ' + formatMoney(s.profit, currency, { signed: true }) : '') + stale : 'First sync pending',
      equityTone: s ? 'txt-white' : 'txt-dim',
      syncVal: syncVal, syncSub: syncSub, syncTone: syncTone,
      positionsVal: positionsVal, positionsSub: positionsSub, positionsTone: positionsTone
    };

    var history = snapshot.history || {};
    var stats = history.period_stats;
    var coverage = history.coverage_status;
    var coverageNote = coverage === 'complete_for_requested_range' ? '' : coverage === 'in_progress' ? ' · still importing' : coverage === 'partial_gaps' ? ' · incomplete (gaps)' : '';
    var since = stats && stats.first_close_time ? new Date(stats.first_close_time).toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) : null;
    var analytics = {
      dailyPnl: null, dailyPnlText: '—', dailySub: 'Not available for API accounts yet', dailyTone: 'txt-dim',
      overallPnl: null, overallPnlText: '—', overallSub: coverage === 'not_permitted' ? 'Trade history not permitted by this token' : 'Imported period · waiting for trades', overallTone: 'txt-dim',
      winRate: null, winRateSub: 'Waiting for imported trades', winRateTone: 'txt-dim'
    };
    if (stats && stats.closed_entries > 0) {
      var net = toNumber(stats.net_profit);
      analytics.overallPnl = net;
      analytics.overallPnlText = formatMoney(stats.net_profit, currency, { signed: true });
      analytics.overallSub = 'Imported period' + (since ? ' since ' + since : '') + ' · net of fees' + coverageNote;
      analytics.overallTone = net > 0 ? 'txt-green' : net < 0 ? 'txt-red' : 'txt-dim';
      analytics.winRate = Math.round((stats.winning_entries / stats.closed_entries) * 100);
      analytics.winRateSub = stats.winning_entries + ' of ' + stats.closed_entries + ' closed entries' + coverageNote;
      analytics.winRateTone = 'txt-white';
    } else if (stats) {
      analytics.overallSub = 'No closed trades in the imported period' + coverageNote;
      analytics.winRateSub = 'No closed trades imported';
    }
    return {
      currency: currency,
      overviewStatus: overviewStatus,
      analytics: analytics,
      payout: { progressVal: '—', progressSub: 'Prop rules are not set up for API accounts yet', progressTone: 'txt-dim' },
      topStatus: { text: topText, color: topColor },
      balanceText: s ? formatMoney(s.balance, currency) : null,
      tiles: stats && stats.closed_entries > 0
        ? { wr: analytics.winRate + '%', netpl: analytics.overallPnlText, totalTrades: String(stats.closed_entries), pf: '—' }
        : { wr: '—', netpl: '—', totalTrades: stats ? '0' : '—', pf: '—' },
      coverage: coverage, coverageNote: coverageNote
    };
  }

  // Adds API connections to the page's account map without touching legacy entries.
  // Keys are "mtapi:<connectionId>": never the broker login, never sent to legacy routes.
  function mergeAccounts(accounts, cards) {
    Object.keys(accounts).forEach(function (key) { if (isApiAccountKey(key)) delete accounts[key]; });
    var index = Object.keys(accounts).length;
    (cards || []).forEach(function (card) {
      if (!card || !isApiAccountKey(card.account_key) || connectionIdFromKey(card.account_key) !== card.id) return;
      index += 1;
      accounts[card.account_key] = {
        label: (card.broker_name || 'Match-Trader') + ' · API' + (card.provider_account_type ? ' · ' + card.provider_account_type : ''),
        short: 'API', color: 'var(--blue)', idx: index,
        accountType: '', accountSize: null,             // never a default size or prop stage for API accounts
        connectorType: 'matchtrader_api', apiConnectionId: card.id,
        displayId: card.masked_login || '…', currency: card.currency || 'USD',
        providerAccountType: card.provider_account_type || '', apiStatus: card.status, apiDisplayState: card.display_state,
        balanceText: card.balance != null ? formatMoney(card.balance, card.currency) : null,
        lastSyncedAt: card.last_synced_at || null,
        isPrimary: false, isActive: true, status: card.status, deactivated: false
      };
    });
    return accounts;
  }

  // Imported closed entry -> the row shape the existing history panel reads. Fields the import
  // does not have (balanceAfter, accountSize, dailyPct, legacy journal id) are left absent.
  function tradeToHistoryRow(trade, currency) {
    var net = toNumber(trade.net_profit);
    return {
      symbol: trade.symbol || '—', direction: trade.side || '—',
      volume: trade.volume, openPrice: trade.open_price, closePrice: trade.close_price,
      pnl: net === null ? 0 : net, pnlText: formatMoney(trade.net_profit, currency, { signed: true }),
      closedAt: trade.close_time, openedAt: trade.open_time,
      entryKey: trade.entry_key, positionId: trade.position_id, source: 'matchtrader_api', readOnly: true
    };
  }

  var SYNC_RESOURCE_NAMES = { summary: 'balance', positions: 'open positions', pending_orders: 'pending orders' };
  var SYNC_FAILURE_TEXT = {
    unauthorized: 'The broker rejected the saved token. Reconnect with a new read-only token.',
    needs_reauth: 'The broker rejected the saved token. Reconnect with a new read-only token.',
    rate_limited: 'Broker rate limit reached. Try again in a minute.',
    not_active: 'This connection is not active.',
    credential_unreadable: 'The saved token can no longer be read. Reconnect with a new read-only token.',
    internal: 'The sync could not be completed. Showing the last saved data.'
  };

  // One "refresh now" request as the server reports it. ``status`` drives the existing sync chip
  // (syncing | success | failed | idle); ``label`` is the precise state shown to the user:
  // queued | running | partial | completed | failed. Accepted (HTTP 202) is never "completed".
  function interpretSyncRequest(request) {
    if (!request || !request.state) return { status: 'failed', label: 'failed', pending: false, liveReady: false, message: 'Sync could not be started. Showing the last saved data.' };
    var resources = request.resources || {};
    switch (request.state) {
      case 'queued': return { status: 'syncing', label: 'queued', pending: true, liveReady: false, message: 'Queued. Waiting for the sync worker…' };
      case 'running': return { status: 'syncing', label: 'running', pending: true, liveReady: false, message: 'Running. Reading balance and positions from the broker…' };
      case 'completed': return { status: 'success', label: 'completed', pending: false, liveReady: true, message: 'Completed. Balance and positions are up to date.' };
      case 'partial': {
        var missed = Object.keys(resources).filter(function (name) { return resources[name] !== 'ok' && resources[name] !== 'not_permitted'; })
          .map(function (name) { return SYNC_RESOURCE_NAMES[name] || name; });
        return { status: 'success', label: 'partial', pending: false, liveReady: true,
                 message: 'Partially updated. Could not refresh: ' + (missed.join(', ') || 'some data') + '.' };
      }
      case 'cancelled': return { status: 'failed', label: 'failed', pending: false, liveReady: false, message: 'Sync was cancelled because the connection changed.' };
      default: return { status: 'failed', label: 'failed', pending: false, liveReady: false,
                        message: 'Failed. ' + (SYNC_FAILURE_TEXT[request.detail] || 'The broker could not be reached. Showing the last saved data.') };
    }
  }

  var pure = { formatMoney: formatMoney, relativeTime: relativeTime, deriveDisplayState: deriveDisplayState, mergeAccounts: mergeAccounts,
               tradeToHistoryRow: tradeToHistoryRow, interpretSyncRequest: interpretSyncRequest, isApiAccountKey: isApiAccountKey, connectionIdFromKey: connectionIdFromKey,
               toNumber: toNumber, CONNECTION_STATUS_TEXT: CONNECTION_STATUS_TEXT,
               SNAPSHOT_MIN_INTERVAL_MS: SNAPSHOT_MIN_INTERVAL_MS, IMPORT_POLL_MS: IMPORT_POLL_MS, IMPORT_POLL_MAX: IMPORT_POLL_MAX, SYNC_POLL_MS: SYNC_POLL_MS, SYNC_POLL_MAX: SYNC_POLL_MAX, RECENT_BACKFILL_DAYS: RECENT_BACKFILL_DAYS };
  global.TaliMatchTraderApi = { create: createClient, ApiError: ApiError };
  global.TaliMatchTraderCore = pure;
})(typeof window !== 'undefined' ? window : globalThis);
