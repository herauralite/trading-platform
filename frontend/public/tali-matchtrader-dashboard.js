// Provider bridge between the TaliTrade dashboard (public/app.html) and Match-Trader API
// connections. Requires tali-matchtrader-api.js. See that file for the rules both keep.
(function initTaliMatchTraderBridge(global) {
  'use strict';

  var core = global.TaliMatchTraderCore;
  var formatMoney = core.formatMoney, relativeTime = core.relativeTime, toNumber = core.toNumber;

  function createBridge(deps) {
    var api = global.TaliMatchTraderApi.create(deps);
    var doc = deps.document;
    var host = null;
    var state = freshState();

    function freshState() {
      return { available: null, service: null, syncWatch: {}, lastConnectTimings: null, cards: [], snapshots: {}, lastFetch: {}, generation: 0, controller: null, historyCursor: {}, pollTimer: null, pollCount: 0, listError: null, pendingDisconnect: null, historyRequest: null, progress: {}, progressInFlight: {}, rolloverTimer: null };
    }
    function now() { return deps.now ? deps.now() : Date.now(); }
    function controller() {
      if (!state.controller && typeof AbortController !== 'undefined') state.controller = new AbortController();
      return state.controller;
    }
    function captureContext(accountKey) {
      return { session: deps.getSessionToken(), accountKey: accountKey, generation: state.generation, signal: controller() ? controller().signal : undefined };
    }
    function isCurrent(context) {
      return !!host && context.generation === state.generation && context.session === deps.getSessionToken() && context.accountKey === host.getActiveAccount();
    }
    // Called whenever the selected account or the session changes: late responses are dropped.
    function onContextChange() {
      state.generation += 1;
      if (state.controller) { try { state.controller.abort(); } catch { /* already aborted */ } }
      state.controller = null;
      state.pendingDisconnect = null;
      state.analyticsAt = 0;
      state.historyRequest = null;
      setHistoryStatus('History keeps importing in the background while the connection is active. You do not need to press Sync to keep your records.');
      var date = doc && doc.getElementById('mtApiHistoryFrom');
      if (date) date.value = '';
      renderPanel(null);
    }
    function reset() {
      onContextChange();
      if (state.pollTimer) deps.clearInterval(state.pollTimer);
      state = freshState();
      firstSync = {};
      clearTokenInput();
    }
    function attach(pageHost) { host = pageHost; }
    function card(accountKey) {
      for (var i = 0; i < state.cards.length; i++) if (state.cards[i].account_key === accountKey) return state.cards[i];
      return null;
    }
    function isApiAccount(accountKey) { return core.isApiAccountKey(accountKey) && !!card(accountKey); }
    function connectionIdForAccount(accountKey) {
      var c = card(accountKey);   // validated metadata from the server, not parsed from a bare login
      return c ? c.id : null;
    }
    function fingerprint() { return JSON.stringify(state.cards.map(function (c) { return [c.account_key, c.status, c.balance, c.last_synced_at]; })); }

    function loadConnections() {
      var session = deps.getSessionToken();
      if (!session) { state.cards = []; return Promise.resolve(false); }
      return api.listConnections({}).then(function (body) {
        if (session !== deps.getSessionToken()) return false;
        // The form is offered only when the server says every part is ready (storage, worker, gate).
        state.service = body.service || null;
        state.available = !!(body.service && body.service.accepting_connections === true);
        state.listError = null;
        var before = fingerprint();
        state.cards = (body.connections || []).map(function (c) { return c.card; }).filter(Boolean);
        if (before !== fingerprint() && host) host.rehydrateAccounts();
        return true;
      }, function (err) {
        if (err && err.status === 404) {   // feature is off on this server: hide it, break nothing
          state.available = false;
          state.service = null;
          if (state.cards.length) { state.cards = []; if (host) host.rehydrateAccounts(); }
          return false;
        }
        state.listError = err && err.message ? err.message : 'unavailable';   // keep known cards; a blip must not erase them
        return false;
      });
    }
    function merge(accounts) { return core.mergeAccounts(accounts, state.cards); }
    // A card the server just returned for a connection this session created.
    function adoptCard(newCard) {
      state.cards = state.cards.filter(function (c) { return c.account_key !== newCard.account_key; }).concat([newCard]);
      if (host) host.rehydrateAccounts();
      return true;
    }

    // ── snapshot -> screen ──
    function refreshSnapshot(accountKey, options) {
      options = options || {};
      var id = connectionIdForAccount(accountKey);
      if (id === null) return Promise.resolve();
      var last = state.lastFetch[accountKey] || 0;
      if (!options.force && now() - last < core.SNAPSHOT_MIN_INTERVAL_MS) {
        if (state.snapshots[accountKey] && host && host.getActiveAccount() === accountKey) render(state.snapshots[accountKey]);   // keeps "updated Xs ago" ageing
        return Promise.resolve();
      }
      state.lastFetch[accountKey] = now();
      var context = captureContext(accountKey);
      return api.getSnapshot(id, { signal: context.signal }).then(function (snapshot) {
        if (!isCurrent(context)) return;
        if (!snapshot.connection || snapshot.connection.id !== id || snapshot.connection.account_key !== accountKey) return;   // identity check
        state.snapshots[accountKey] = snapshot;
        render(snapshot);
        refreshProgress(accountKey, false);
      }, function (err) {
        if (!isCurrent(context) || (err && err.name === 'AbortError')) return;
        if (err && err.status === 404) { loadConnections(); return; }
        // The backend rejected the session itself (expired or revoked): the page returns to sign-in.
        if (err && err.status === 401 && err.code !== 'no_session' && host && host.onSessionRejected) { host.onSessionRejected(); return; }
        var previous = state.snapshots[accountKey] ? core.deriveDisplayState(state.snapshots[accountKey], now()).overviewStatus : {};
        host.setTopStatus('connection error', 'var(--red)');
        host.applyApiState({ overviewStatus: Object.assign({}, previous, { syncVal: 'Error', syncSub: 'Could not load saved data from TaliTrade', syncTone: 'txt-red' }) });
      });
    }

    function render(snapshot) {
      var view = core.deriveDisplayState(snapshot, now());
      overlayProgress(view, snapshot.connection.account_key);
      host.setTopStatus(view.topStatus.text, view.topStatus.color);
      host.applyApiState({ overviewStatus: view.overviewStatus, analytics: view.analytics, payout: view.payout, tiles: view.tiles,
                           balanceText: view.balanceText, accountKey: snapshot.connection.account_key, label: snapshot.connection.display_label });
      renderPanel(snapshot);
    }

    function el(tag, text, style) {
      var node = doc.createElement(tag);
      if (text !== undefined && text !== null) node.textContent = String(text);
      if (style) node.style.cssText = style;
      return node;
    }
    function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

    // Positions / pending orders / import status. Built with DOM nodes + textContent only.
    var recentPages = new WeakMap();
    function renderPanel(snapshot) {
      if (!doc) return;
      var anchor = doc.getElementById('overviewCards');
      var panel = doc.getElementById('mtApiPanel');
      var setupCard = doc.getElementById('mtApiSetupCard');
      if (!snapshot) { if (panel) panel.style.display = 'none'; if (setupCard) setupCard.style.display = 'none'; return; }
      if (!anchor || !anchor.parentNode) return;
      if (!panel) {
        panel = el('div', null, 'margin-top:10px');
        panel.id = 'mtApiPanel';
        panel.className = 'card card-p';
        anchor.parentNode.insertBefore(panel, anchor.nextSibling);
      }
      panel.style.display = 'block';
      // Account setup summary (what the trader told us about this account), above the broker data.
      if (host && host.renderSetupCard) {
        if (!setupCard) {
          setupCard = el('div');
          setupCard.id = 'mtApiSetupCard';
          setupCard.className = 'card card-p';
          anchor.parentNode.insertBefore(setupCard, panel);
        }
        setupCard.style.display = snapshot.summary ? 'block' : 'none';
        if (snapshot.summary) host.renderSetupCard(setupCard, snapshot.connection.account_key);
      }
      // The panel holds two parts: content redrawn on every snapshot, and the history controls,
      // built once so a date being typed is not wiped by the next refresh.
      var content = doc.getElementById('mtApiPanelContent');
      if (!content) { content = el('div'); content.id = 'mtApiPanelContent'; panel.appendChild(content); }
      clear(content);
      var outer = panel;
      panel = content;
      var currency = (snapshot.connection && snapshot.connection.currency) || 'USD';
      var nowMs = now();
      var head = el('div', null, 'display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;gap:8px');
      head.appendChild(el('div', snapshot.connection.display_label, 'font-size:12px;font-weight:700'));
      head.appendChild(el('div', 'Read-only · ' + (snapshot.connection.provider_account_type || 'account'), 'font-size:10px;color:var(--t3)'));
      panel.appendChild(head);

      function section(title, rows, fresh, emptyText, describe) {
        panel.appendChild(el('div', title, 'font-size:10px;color:var(--t3);text-transform:uppercase;letter-spacing:.6px;margin:10px 0 4px'));
        if (fresh && fresh.status === 'not_permitted') { panel.appendChild(el('div', 'Not permitted by this token.', 'font-size:12px;color:var(--t3)')); return; }
        if (!Array.isArray(rows)) { panel.appendChild(el('div', title + ' unavailable — not synced yet.', 'font-size:12px;color:var(--t3)')); return; }
        if (fresh && fresh.is_stale) panel.appendChild(el('div', 'Delayed · as of ' + (relativeTime(fresh.last_success_at, nowMs) || 'earlier'), 'font-size:11px;color:var(--orange);margin-bottom:4px'));
        if (!rows.length) { panel.appendChild(el('div', emptyText, 'font-size:12px;color:var(--t2)')); return; }
        rows.forEach(function (row) {
          var line = el('div', null, 'display:flex;justify-content:space-between;gap:10px;padding:6px 0;border-top:1px solid var(--border);font-size:12px');
          var parts = describe(row);
          line.appendChild(el('div', parts[0]));
          line.appendChild(el('div', parts[1], parts[2] || ''));
          panel.appendChild(line);
        });
      }
      var fresh = snapshot.freshness || {};
      section('Open positions', snapshot.positions, fresh.positions, 'No open positions.', function (p) {
        var net = toNumber(p.net_profit);
        return [(p.symbol || '—') + ' · ' + (p.side || 'side n/a') + ' · ' + (p.volume || '—') + ' lots @ ' + (p.open_price || '—'),
                net === null ? '—' : formatMoney(p.net_profit, currency, { signed: true }) + ' net',
                'font-family:JetBrains Mono,monospace;color:' + (net === null ? 'var(--t3)' : net >= 0 ? 'var(--green)' : 'var(--red)')];
      });
      section('Pending orders', snapshot.pending_orders, fresh.pending_orders, 'No pending orders.', function (o) {
        return [(o.symbol || '—') + ' · ' + (o.side || 'side n/a') + ' ' + (o.type || '') + ' · ' + (o.volume || '—') + ' lots', '@ ' + (o.activation_price || '—'), 'font-family:JetBrains Mono,monospace;color:var(--t2)'];
      });
      var h = snapshot.history || {};
      panel.appendChild(el('div', 'Recent trades', 'font-size:13px;font-weight:700;margin:14px 0 6px'));
      var recent = el('div', 'Loading recent trades…', 'font-size:12px;color:var(--t2)');
      panel.appendChild(recent);
      var ctx = captureContext(snapshot.connection.account_key);
      if (!recentPages.has(snapshot)) {
        recentPages.set(snapshot, api.listTrades(snapshot.connection.id, {limit: 5, signal: ctx.signal}));
      }
      recentPages.get(snapshot).then(function (page) {
        if (!isCurrent(ctx) || !recent.isConnected) return;
        clear(recent);
        if (!page.trades.length) recent.textContent = 'No closed trades yet.';
        page.trades.forEach(function (trade) {
          var line = el('div', null, 'display:flex;justify-content:space-between;gap:8px;padding:7px 0;border-top:1px solid var(--border)');
          line.appendChild(el('span', [trade.symbol, trade.side, trade.close_time && new Date(trade.close_time).toLocaleDateString()].filter(Boolean).join(' · ')));
          line.appendChild(el('span', formatMoney(trade.net_profit, currency, {signed:true})));
          recent.appendChild(line);
        });
      }, function () {
        recentPages.delete(snapshot);
        if (isCurrent(ctx)) recent.textContent = 'Recent trades are unavailable.';
      });
      var pending = snapshot.sync_request && snapshot.sync_request.pending ? core.interpretSyncRequest(snapshot.sync_request) : null;
      if (pending) panel.appendChild(el('div', 'Refresh ' + pending.label + '…', 'font-size:11px;color:var(--blue);margin-top:4px'));
      if (snapshot.background_refresh && snapshot.background_refresh.enabled === false) {
        panel.appendChild(el('div', 'Background refresh is turned off on this server. Saved data is shown and cannot be refreshed until it is turned on.', 'font-size:11px;color:var(--orange);margin-top:4px'));
      }
      var controls = doc.getElementById('mtApiHistoryControls');
      if (controls) controls.style.display = snapshot.connection && snapshot.connection.status === 'active' && h.coverage_status !== 'not_permitted' ? '' : 'none';
      outer.setAttribute('data-coverage', h.coverage_status || '');
      var req = state.historyRequest;
      if (req && snapshot.connection && snapshot.connection.account_key === req.accountKey) {
        if (h.coverage_status === 'complete_for_requested_range') {
          setHistoryStatus('History since ' + req.since + ' is imported: complete for the requested range.', 'ok');
          state.historyRequest = null;
        } else if (h.coverage_status === 'partial_gaps') {
          setHistoryStatus('History since ' + req.since + ' was imported with ' + h.gap_count + ' period(s) the broker did not return. Figures may be incomplete.', 'error');
          state.historyRequest = null;
        }
      }
    }

    // ── older history ──
    function buildHistoryControls() {
      var box = el('div', null, 'margin-top:10px;padding-top:10px;border-top:1px solid var(--border)');
      box.id = 'mtApiHistoryControls';
      box.appendChild(el('div', 'Load older history', 'font-size:10px;color:var(--t3);text-transform:uppercase;letter-spacing:.6px;margin-bottom:6px'));
      var row = el('div', null, 'display:flex;flex-wrap:wrap;gap:6px;align-items:center');
      [['30d', '30 days'], ['90d', '90 days'], ['all', 'All available']].forEach(function (choice) {
        var b = el('button', choice[1], 'padding:4px 10px;font-size:11px');
        b.type = 'button'; b.className = 'btn btn-ghost rip'; b.setAttribute('data-history-range', choice[0]);
        b.addEventListener('click', function () { loadOlderHistory(choice[0]); });
        row.appendChild(b);
      });
      var date = el('input', null, 'padding:3px 6px;font-size:11px;background:var(--s2);color:var(--text);border:1px solid var(--border);border-radius:6px');
      date.type = 'date'; date.id = 'mtApiHistoryFrom'; date.setAttribute('aria-label', 'Import history from this date');
      row.appendChild(date);
      var go = el('button', 'From date', 'padding:4px 10px;font-size:11px');
      go.type = 'button'; go.className = 'btn btn-ghost rip'; go.setAttribute('data-history-range', 'custom');
      go.addEventListener('click', function () { loadOlderHistory('custom'); });
      row.appendChild(go);
      box.appendChild(row);
      var status = el('div', 'History keeps importing in the background while the connection is active. You do not need to press Sync to keep your records.', 'font-size:11px;color:var(--t3);margin-top:6px;line-height:1.5');
      status.id = 'mtApiHistoryStatus';
      box.appendChild(status);
      return box;
    }
    function setHistoryStatus(text, tone) {
      var node = doc && doc.getElementById('mtApiHistoryStatus');
      if (!node) return;
      node.textContent = text;
      node.style.color = tone === 'error' ? 'var(--red)' : tone === 'ok' ? 'var(--green)' : 'var(--t3)';
    }
    function dateToUtcMs(value) {
      var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value || ''));
      return m ? Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])) : null;   // start of that calendar day, UTC
    }
    function loadOlderHistory(range) {
      var accountKey = host.getActiveAccount();
      var id = connectionIdForAccount(accountKey);
      if (id === null) return Promise.resolve();
      var fromMs = null;
      if (range === 'custom') {
        var input = doc.getElementById('mtApiHistoryFrom');
        fromMs = dateToUtcMs(input && input.value);
        if (fromMs === null) { setHistoryStatus('Choose a start date first.', 'error'); return Promise.resolve(); }
      }
      var context = captureContext(accountKey);
      setHistoryStatus('Requesting older history…');
      return api.requestHistory(id, { range: range, fromMs: fromMs }).then(function (body) {
        if (!isCurrent(context)) return;
        var since = body.requested_from ? new Date(body.requested_from).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' }) : '';
        if (body.queued && body.queued.length) {
          setHistoryStatus('Importing history since ' + since + (body.clamped_to_account_creation ? ' (when the account was created)' : '') + '. It continues in the background; figures update as it arrives.', 'ok');
          state.historyRequest = { accountKey: accountKey, since: since };   // the note follows the server's coverage until it settles
          startImportPolling(accountKey);
        } else {
          setHistoryStatus('History since ' + since + ' is already requested. Nothing new to import.');
        }
        state.analyticsAt = 0;
        refreshSnapshot(accountKey, { force: true });
      }, function (err) {
        if (!isCurrent(context)) return;
        if (err && err.code === 'start_date_required') {
          setHistoryStatus(err.message, 'error');
          var input = doc.getElementById('mtApiHistoryFrom');
          if (input && input.focus) input.focus();
          return;
        }
        setHistoryStatus((err && err.message) || 'Could not request older history.', 'error');
      });
    }

    // ── analytics page ──
    function renderAnalytics(force) {
      var accountKey = host.getActiveAccount();
      var id = connectionIdForAccount(accountKey);
      if (id === null || !host.applyAnalytics) return Promise.resolve();
      if (!force && state.analyticsKey === accountKey && now() - state.analyticsAt < 20000) return Promise.resolve();
      state.analyticsKey = accountKey; state.analyticsAt = now();
      var tz = 'UTC';
      try { tz = (global.Intl && Intl.DateTimeFormat().resolvedOptions().timeZone) || 'UTC'; } catch { tz = 'UTC'; }
      var context = captureContext(accountKey);
      return api.getAnalytics(id, { tz: deps.timeZone || tz, signal: context.signal }).then(function (payload) {
        if (!isCurrent(context) || !payload || payload.connection_id !== id) return;
        host.applyAnalytics(core.analyticsView(payload));
      }, function (err) {
        if (!isCurrent(context) || (err && err.name === 'AbortError')) return;
        state.analyticsAt = 0;
        host.applyAnalytics({ error: (err && err.message) || 'Could not load analytics.' });
      });
    }

    // ── overview progress: Daily P&L, Challenge / payout, the setup card's progress rows ──
    // Fetched after a snapshot only when something it depends on changed (balance, equity, closed
    // trades, history coverage, open positions), at least once a minute, and when the calendar day
    // ends. A response is applied only to the account, session and context it was asked for.
    function timeZone() {
      if (deps.timeZone) return deps.timeZone;
      try { return (global.Intl && Intl.DateTimeFormat().resolvedOptions().timeZone) || 'UTC'; } catch { return 'UTC'; }
    }
    function progressSig(snapshot) {
      var s = snapshot.summary || {}, h = snapshot.history || {}, st = h.period_stats || {}, fr = (snapshot.freshness || {}).closed_positions || {};
      return [s.fetched_at, s.balance, s.equity, h.closed_entry_count, st.last_close_time, fr.last_success_at, h.coverage_status, h.queued_intervals,
              (snapshot.positions || []).map(function (p) { return p.id + ':' + p.sl_price + ':' + p.profit; }).join(',')].join('|');
    }
    function overlayProgress(view, accountKey) {
      var entry = state.progress[accountKey];
      if (!entry || !entry.data) return view;
      var tiles = core.progressTiles(entry.data, view.currency);
      if (tiles.analytics) Object.assign(view.analytics, tiles.analytics);
      if (tiles.payout) view.payout = tiles.payout;
      return view;
    }
    function applyTiles(accountKey) {
      var snapshot = state.snapshots[accountKey];
      if (!snapshot || !host || host.getActiveAccount() !== accountKey) return;
      var view = overlayProgress(core.deriveDisplayState(snapshot, now()), accountKey);
      host.applyApiState({ overviewStatus: view.overviewStatus, analytics: view.analytics, payout: view.payout });
    }
    function scheduleRollover(accountKey) {
      var entry = state.progress[accountKey], setT = deps.setTimeout || global.setTimeout, clearT = deps.clearTimeout || global.clearTimeout;
      if (state.rolloverTimer) { clearT(state.rolloverTimer); state.rolloverTimer = null; }
      if (!entry || !entry.dayEnd) return;
      var wait = Math.min(Math.max(entry.dayEnd - now() + 1500, 1000), 6 * 3600 * 1000);
      state.rolloverTimer = setT(function () {
        state.rolloverTimer = null;
        if (host && host.getActiveAccount() === accountKey) refreshProgress(accountKey, true);
      }, wait);
    }
    function refreshProgress(accountKey, force) {
      var id = connectionIdForAccount(accountKey);
      if (id === null || !host || host.getActiveAccount() !== accountKey) return Promise.resolve();
      var entry = state.progress[accountKey], snapshot = state.snapshots[accountKey];
      var sig = snapshot ? progressSig(snapshot) : '';
      var dayOver = entry && entry.dayEnd && now() >= entry.dayEnd;
      if (!force && entry && entry.sig === sig && now() - entry.at < 60000 && !dayOver) return Promise.resolve();
      if (state.progressInFlight[accountKey] && !force) return state.progressInFlight[accountKey];
      var context = captureContext(accountKey);
      var request = api.getProgress(id, { tz: timeZone(), signal: context.signal }).then(function (body) {
        if (!isCurrent(context) || !body || body.connection_id !== id) return;          // late answer for another account or session
        state.progress[accountKey] = { data: body, at: now(), sig: sig, dayEnd: Date.parse(body.daily_pnl && body.daily_pnl.day_end) || null };
        scheduleRollover(accountKey);
        applyTiles(accountKey);
        if (host.onProgress) host.onProgress(accountKey, body);
      }, function (err) {
        if (!isCurrent(context) || (err && err.name === 'AbortError')) return;
        if (state.progress[accountKey]) state.progress[accountKey].at = 0;                // keep the last figures; the next snapshot retries
      }).then(function () { if (state.progressInFlight[accountKey] === request) delete state.progressInFlight[accountKey]; });
      state.progressInFlight[accountKey] = request;
      return request;
    }

    // ── history / journal ──
    function loadHistoryPage(reset) {
      var accountKey = host.getActiveAccount();
      var id = connectionIdForAccount(accountKey);
      var hs = host.historyState;
      if (id === null || hs.loading) return Promise.resolve();
      if (reset) { hs.offset = 0; hs.rows = []; hs.hasMore = true; hs.error = null; state.historyCursor[accountKey] = null; }
      if (!hs.hasMore) return Promise.resolve();
      hs.loading = true; hs.accountId = accountKey;
      var requestId = ++hs.reqId;
      host.renderHistoryPanel();
      var context = captureContext(accountKey);
      var currency = (card(accountKey) || {}).currency || 'USD';
      return api.listTrades(id, { limit: 20, cursor: state.historyCursor[accountKey] || null, signal: context.signal }).then(function (page) {
        if (!isCurrent(context) || requestId !== hs.reqId) return;
        var seen = {};
        hs.rows.forEach(function (r) { if (r.entryKey) seen[r.entryKey] = true; });
        (page.trades || []).forEach(function (t) { if (!seen[t.entry_key]) { hs.rows.push(core.tradeToHistoryRow(t, currency)); seen[t.entry_key] = true; } });
        hs.offset = hs.rows.length;
        state.historyCursor[accountKey] = page.next_cursor || null;
        hs.hasMore = !!page.has_more;
        hs.error = null;
      }, function (err) {
        if (requestId !== hs.reqId || (err && err.name === 'AbortError')) return;
        hs.error = 'Could not load imported trades. Tap retry.';
      }).then(function () {
        if (requestId === hs.reqId) { hs.loading = false; if (host.getActiveAccount() === accountKey) host.renderHistoryPanel(); }
      });
    }

    function renderJournal() {
      var accountKey = host.getActiveAccount();
      var id = connectionIdForAccount(accountKey);
      var target = doc.getElementById('journalEntries');
      var meta = doc.getElementById('journalMeta');
      if (id === null || !target) return Promise.resolve();
      var context = captureContext(accountKey);
      var currency = (card(accountKey) || {}).currency || 'USD';
      return api.listTrades(id, { limit: 50, signal: context.signal }).then(function (page) {
        if (!isCurrent(context)) return;
        clear(target);
        var trades = page.trades || [];
        if (meta) meta.textContent = trades.length + (page.has_more ? '+' : '') + ' imported entries · read-only';
        if (!trades.length) { target.appendChild(el('div', 'No imported trades yet.', 'text-align:center;padding:32px;color:var(--t3);font-size:12px')); return; }
        trades.forEach(function (t) {
          var net = toNumber(t.net_profit);
          var color = net === null ? 'var(--t3)' : net > 0 ? 'var(--green)' : net < 0 ? 'var(--red)' : 'var(--t2)';
          var row = el('div', null, 'padding:12px 14px');
          row.className = 'journal-entry';
          var headRow = el('div', null, 'display:flex;justify-content:space-between;gap:10px;align-items:center');
          headRow.appendChild(el('div', (t.symbol || '?') + ' · ' + (t.side || 'side n/a'), 'font-weight:700;color:' + color));
          headRow.appendChild(el('div', formatMoney(t.net_profit, currency, { signed: true }), 'font-family:JetBrains Mono,monospace;color:' + color));
          row.appendChild(headRow);
          var when = t.close_time ? new Date(t.close_time).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', timeZoneName: 'short' }) : '—';
          row.appendChild(el('div', when + ' · ' + (t.volume || '—') + ' lots · ' + (t.open_price || '—') + ' → ' + (t.close_price || '—'), 'font-size:11px;color:var(--t3);margin-top:4px'));
          row.appendChild(el('div', 'Net of commission ' + formatMoney(t.commission, currency) + ' and swap ' + formatMoney(t.swap, currency) + ' · position ' + (t.position_id || '—'), 'font-size:10px;color:var(--t3);margin-top:2px'));
          target.appendChild(row);
        });
      }, function (err) {
        if (!isCurrent(context) || (err && err.name === 'AbortError')) return;
        clear(target);
        target.appendChild(el('div', 'Could not load imported trades.', 'text-align:center;padding:20px;color:var(--red);font-size:12px'));
      });
    }

    // ── sync ──
    function delay(ms) { return deps.delay ? deps.delay(ms) : new Promise(function (resolve) { global.setTimeout(resolve, ms); }); }

    // Follow one queued refresh until the server reports a final state. The POST only queued it.
    // Accounts connected in this page session whose first successful snapshot has not arrived yet.
    // Memory only: a reload, a routine refresh or a failed sync never announces anything.
    var firstSync = {};
    function announceFirstSnapshot(accountKey, id, view) {
      if (!firstSync[accountKey] || !view || view.status !== 'success' || !host || !host.onFirstSnapshot) return;
      var session = deps.getSessionToken();
      api.getSnapshot(id, {}).then(function (snapshot) {
        if (!firstSync[accountKey] || session !== deps.getSessionToken() || !card(accountKey)) return;
        if (!snapshot || !snapshot.summary || !snapshot.connection || snapshot.connection.account_key !== accountKey) return;
        delete firstSync[accountKey];
        state.snapshots[accountKey] = snapshot;
        host.onFirstSnapshot(accountKey, snapshot);
      }, function () { return null; });
    }
    function watchSync(accountKey, id, firstRequest) {
      var session = deps.getSessionToken();
      var generation = state.generation;
      var token = (state.syncWatch[accountKey] || 0) + 1;
      state.syncWatch[accountKey] = token;                       // a newer watch for the same account replaces this one
      var polls = 0;
      function alive() { return session === deps.getSessionToken() && state.syncWatch[accountKey] === token && !!card(accountKey); }
      function show(view) { host.setManualSyncState(accountKey, { status: view.status, label: view.label, message: view.message, updatedAt: now() }); }
      function settle(view) {
        show(view);
        // Always re-read what is actually stored; never assume the request changed anything.
        return loadConnections().then(function () {
          if (alive()) announceFirstSnapshot(accountKey, id, view);
          if (!alive() || host.getActiveAccount() !== accountKey) return view;
          if (generation !== state.generation) return view;      // the user moved on; their next view loads its own data
          return refreshSnapshot(accountKey, { force: true }).then(function () { host.reloadHistory(); return view; });
        });
      }
      function step(request) {
        if (!alive()) return Promise.resolve({ status: 'idle', pending: false, liveReady: false });
        var view = core.interpretSyncRequest(request);
        if (!view.pending) return settle(view);
        show(view);
        if (++polls > core.SYNC_POLL_MAX) {
          return settle({ status: 'idle', label: view.label, pending: true, liveReady: false,
                          message: 'Still ' + view.label + ' on the server. Saved data is shown; it will update when the sync runs.' });
        }
        return delay(core.SYNC_POLL_MS).then(function () {
          if (!alive()) return { status: 'idle', pending: false, liveReady: false };
          return api.getSyncStatus(id, {}).then(function (body) { return step(body && body.request); }, function (err) {
            if (err && err.status === 404) return settle({ status: 'failed', label: 'failed', pending: false, liveReady: false, message: 'This connection is no longer available.' });
            return step(request);                                 // a blip while polling is not a sync failure: keep waiting
          });
        });
      }
      return step(firstRequest);
    }

    function manualSync(accountKey) {
      var id = connectionIdForAccount(accountKey);
      if (id === null) return Promise.resolve();
      host.setManualSyncState(accountKey, { status: 'syncing', label: 'queued', message: 'Requesting a refresh…', updatedAt: now() });
      return api.sync(id, {}).then(function (body) {
        return watchSync(accountKey, id, body && body.request);
      }, function (err) {
        var view = { status: 'failed', label: 'failed', pending: false, liveReady: false, message: 'Failed. ' + ((err && err.message) || 'The refresh could not be requested.') };
        host.setManualSyncState(accountKey, { status: view.status, label: view.label, message: view.message, updatedAt: now() });
        return view;
      });
    }

    function startImportPolling(accountKey) {
      if (state.pollTimer) deps.clearInterval(state.pollTimer);
      state.pollCount = 0;
      state.pollTimer = deps.setInterval(function () {
        state.pollCount += 1;
        var snapshot = state.snapshots[accountKey];
        var done = snapshot && snapshot.history && snapshot.history.coverage_status !== 'in_progress' && snapshot.history.coverage_status !== 'pending';
        if (done || state.pollCount > core.IMPORT_POLL_MAX || !card(accountKey)) {
          deps.clearInterval(state.pollTimer); state.pollTimer = null;
          if (done && host.getActiveAccount() === accountKey) host.reloadHistory();
          return;
        }
        if (host.getActiveAccount() === accountKey) refreshSnapshot(accountKey, { force: true });   // reads saved data only
      }, core.IMPORT_POLL_MS);
    }

    // ── token form ──
    function clearTokenInput() {
      var input = doc && doc.getElementById('mtApiTokenInput');
      if (input) input.value = '';
    }
    function flow() { return host.getAddFlowState(); }
    function fail(message) { var f = flow(); f.loading = false; f.error = message; f.success = ''; host.renderAddAccountFlow(); }

    function submitToken() {
      var f = flow();
      if (f.loading) return Promise.resolve();
      var input = doc.getElementById('mtApiTokenInput');
      var token = input ? String(input.value || '').trim() : '';
      if (input) input.value = '';                      // cleared from the page before anything else happens
      if (token.length < 20 || /\s/.test(token)) { token = ''; fail('Paste the read-only API token from Match-Trader (API Access).'); return Promise.resolve(); }
      var rotateKey = f.rotateAccountKey || null;
      var rotateId = rotateKey ? connectionIdForAccount(rotateKey) : null;
      f.loading = true; f.error = ''; f.success = '';
      host.renderAddAccountFlow();
      var submittedAt = now();
      var request = rotateId !== null ? api.rotate(rotateId, { token: token }) : api.connect({ token: token });
      token = '';                                        // not kept after the request is built
      return request.then(function (body) {
        var connection = body.connection || {};
        var key = 'mtapi:' + connection.id;
        if (connection.status !== 'active') {
          return loadConnections().then(function () {
            fail(connection.status === 'forbidden' ? 'Connected, but this token cannot read everything TaliTrade needs (' + String(connection.status_detail || '').replace('missing_capabilities:', '') + '). Create a token with read access and reconnect.'
              : connection.status === 'pending_review' ? 'The account was saved but the broker platform could not be verified, so it will not sync yet.'
              : 'The account was saved but is not active (' + connection.status + ').');
          });
        }
        // The account is bound. The server already queued the first refresh and the recent-history
        // import in the same transaction, and returned the switcher card, so nothing else has to
        // finish before the sheet closes: no further request is awaited here.
        var timings = { verify_and_store_ms: Math.round(now() - submittedAt), server: body.timings || null };
        var firstRequest = body.sync_request || null;
        var ready = body.card && body.card.account_key === key
          ? Promise.resolve(adoptCard(body.card))
          : loadConnections();                           // older server without the card: one list read instead
        return ready.then(function () {
          f.loading = false;
          host.closeAddAccountFlow();
          host.activateAccount(key);
          timings.sheet_closed_ms = Math.round(now() - submittedAt);
          state.lastConnectTimings = timings;            // durations only; readable via TaliMatchTrader.lastConnectTimings()
          host.showAccountActionStatus('Match-Trader account connected. The first sync is queued; balances appear when it completes.', 'success');
          firstSync[key] = true;                         // this page session made the connection: its first snapshot gets announced once
          startImportPolling(key);
          // Everything below runs after the sheet is closed.
          var imported = body.initial_backfill && body.initial_backfill.resources && body.initial_backfill.resources.length;
          if (!imported) api.startRecentBackfill(connection.id, { fromMs: now() - core.RECENT_BACKFILL_DAYS * 86400000 }).then(null, function () { return null; });
          var started = firstRequest ? Promise.resolve({ request: firstRequest }) : api.sync(connection.id, {});
          started.then(function (queued) { return watchSync(key, connection.id, queued && queued.request); }, function (err) {
            host.setManualSyncState(key, { status: 'failed', label: 'failed', message: 'Failed. ' + ((err && err.message) || 'The first sync could not be requested.'), updatedAt: now() });
          });
          loadConnections();                             // refresh readiness/cards in the background
        });
      }, function (err) {
        fail((err && err.message) || 'Could not connect this account.');
      });
    }

    function openReconnect(accountKey) {
      host.openAddAccountFlow();
      var f = flow();
      f.path = 'matchtraderApi'; f.step = 2; f.rotateAccountKey = accountKey;
      host.renderAddAccountFlow();
    }

    function disconnect(accountKey) {
      var id = connectionIdForAccount(accountKey);
      if (id === null) return Promise.resolve();
      if (state.pendingDisconnect !== accountKey) {      // two taps instead of a blocking browser dialog
        state.pendingDisconnect = accountKey;
        host.rerenderAccountSheet();
        return Promise.resolve();
      }
      state.pendingDisconnect = null;
      return api.disconnect(id, {}).then(function () {
        delete state.snapshots[accountKey];
        return loadConnections().then(function () {
          host.showAccountActionStatus('Disconnected. The saved token was deleted; imported history was kept. Revoke the token in Match-Trader to disable it there.', 'success');
          host.afterDisconnect(accountKey);
        });
      }, function (err) {
        host.showAccountActionStatus((err && err.message) || 'Could not disconnect right now.', 'error');
      });
    }

    function sheetSubtitle(accountKey) {
      var c = card(accountKey);
      if (!c) return '';
      var special = core.CONNECTION_STATUS_TEXT[c.status];
      if (special) return special[0];
      if (!c.last_synced_at) return 'Connected · first sync pending';
      return 'Synced ' + (relativeTime(c.last_synced_at, now()) || '');
    }

    return {
      attach: attach, reset: reset, onContextChange: onContextChange, loadConnections: loadConnections, merge: merge,
      isApiAccount: isApiAccount, connectionIdForAccount: connectionIdForAccount, refreshSnapshot: refreshSnapshot,
      loadHistoryPage: loadHistoryPage, renderJournal: renderJournal, manualSync: manualSync, submitToken: submitToken,
      openReconnect: openReconnect, disconnect: disconnect, sheetSubtitle: sheetSubtitle, clearTokenInput: clearTokenInput,
      isAvailable: function () { return state.available === true; },
      serviceStatus: function () { return state.service; },
      lastConnectTimings: function () { return state.lastConnectTimings || null; },
      loadOlderHistory: loadOlderHistory, renderAnalytics: renderAnalytics,
      isDisconnectPending: function (accountKey) { return state.pendingDisconnect === accountKey; },
      needsReconnect: function (accountKey) { var c = card(accountKey); return !!c && (c.status === 'needs_reauth' || c.status === 'error' || c.status === 'forbidden'); },
      cardFor: card,
      snapshotFor: function (accountKey) { return state.snapshots[accountKey] || null; },
      progressFor: function (accountKey) { var e = state.progress[accountKey]; return e ? e.data : null; },
      refreshProgress: function (accountKey, force) { return refreshProgress(accountKey, force !== false); }
    };
  }

  function defaultDeps() {
    return {
      fetch: function (url, init) { return global.fetch(url, init); },
      getBaseApiUrl: function () { return global.TaliApiBase.resolveApiBase(); },
      getSessionToken: function () { try { return global.localStorage.getItem('tali_session_token_v1') || ''; } catch { return ''; } },
      document: global.document,
      setInterval: function (fn, ms) { return global.setInterval(fn, ms); },
      clearInterval: function (id) { return global.clearInterval(id); }
    };
  }

  global.TaliMatchTrader = Object.assign(createBridge(defaultDeps()), { createBridge: createBridge });
})(typeof window !== 'undefined' ? window : globalThis);
