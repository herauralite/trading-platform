// Market ticker and price chart for Match-Trader API accounts in the TaliTrade dashboard.
// Requires tali-matchtrader-api.js and tali-matchtrader-dashboard.js.
//
// Rules this file keeps:
//  * it only reads TaliTrade's own backend (cached broker data); the broker token never reaches
//    the browser and no third-party price source is used;
//  * nothing here is "live streaming": prices auto-update by polling, and the page says so. For a
//    live instrument (US30) the forming candle follows quotes polled from TaliTrade's cache, only on
//    the candle price side the backend has verified; the broker's candle snapshots stay the record;
//  * a price is shown with its basis (bid / ask) and never invented: no sample numbers, no
//    percentage change (there is no agreed baseline to compute one from);
//  * provider text (symbols, aliases) is written with textContent only;
//  * a market-price chart is labelled as such and is never presented as account equity.
(function initTaliMatchTraderMarket(global) {
  'use strict';

  var TICKER_STATE_TEXT = {
    loading: 'Loading prices…',
    permission_denied: 'Market data is not permitted for this API token',
    unavailable: 'Prices unavailable right now',
    feature_off: 'Market prices are not enabled on this server'
  };

  // ── pure helpers ──
  function formatPrice(value, precision) {
    if (value === null || value === undefined || value === '') return '—';
    var text = String(value);
    if (!/^-?\d+(\.\d+)?$/.test(text)) return '—';
    if (typeof precision !== 'number' || precision < 0 || precision > 10) return text;
    var parts = text.split('.');
    var fraction = (parts[1] || '');
    if (fraction.length > precision) return text;                 // never round away digits the provider sent
    while (fraction.length < precision) fraction += '0';
    return precision ? parts[0] + '.' + fraction : parts[0];
  }

  // Candles as the chart library (lightweight-charts v4) needs them: ascending, one per opening
  // time, time in UTC SECONDS. The API sends opening times in epoch MILLISECONDS.
  function toSeries(candles) {
    var byTime = {};
    function num(v) { return v === null || v === undefined || v === '' || typeof v === 'boolean' ? NaN : Number(v); }   // null is "missing", not zero
    (candles || []).forEach(function (c) {
      if (!c || typeof c !== 'object') return;
      var ms = num(c.t);
      var o = num(c.o), h = num(c.h), l = num(c.l), cl = num(c.c);
      if (!isFinite(ms) || ![o, h, l, cl].every(isFinite)) return;      // incomplete candle: skipped, never guessed
      byTime[Math.floor(ms / 1000)] = { time: Math.floor(ms / 1000), open: o, high: h, low: l, close: cl, ticks: typeof c.ticks === 'number' ? c.ticks : null };
    });
    return Object.keys(byTime).map(Number).sort(function (a, b) { return a - b; }).map(function (t) { return byTime[t]; });
  }
  function toLine(series) { return series.map(function (c) { return { time: c.time, value: c.close }; }); }

  // What changed between what the chart shows and a fresh answer: 'same', 'tail' (only the last
  // candle changed and/or newer ones arrived: update in place, pan/zoom untouched) or 'reset'.
  function diffSeries(shown, next) {
    if (!shown.length || !next.length) return shown.length === next.length ? 'same' : 'reset';
    var lastShown = shown[shown.length - 1].time;
    var index = {};
    next.forEach(function (c) { index[c.time] = c; });
    for (var i = 0; i < shown.length - 1; i++) {
      var match = index[shown[i].time];
      if (match && (match.open !== shown[i].open || match.high !== shown[i].high || match.low !== shown[i].low || match.close !== shown[i].close)) return 'reset';
    }
    if (next[0].time > lastShown) return 'reset';                    // no overlap: a different window
    var tail = next.filter(function (c) { return c.time >= lastShown; });
    var last = shown[shown.length - 1];
    if (tail.length === 1 && tail[0].open === last.open && tail[0].high === last.high && tail[0].low === last.low && tail[0].close === last.close) return 'same';
    return tail.length ? 'tail' : 'same';
  }

  function tickerView(payload) {
    if (!payload) return { message: TICKER_STATE_TEXT.loading, items: [], note: '' };
    var items = (payload.items || []).map(function (item) {
      return { label: item.alias || item.symbol,
               price: formatPrice(item.bid, item.price_precision) + ' / ' + formatPrice(item.ask, item.price_precision),
               closed: item.state === 'market_closed' || item.session_open === false, stale: item.state === 'stale' };
    });
    var every = payload.poll_interval_seconds ? 'auto-updating about every ' + payload.poll_interval_seconds + ' s' : 'auto-updating';
    var note = payload.status === 'market_closed' ? 'Bid / Ask · market closed · last published prices'
      : payload.status === 'stale' ? 'Bid / Ask · delayed'
      : 'Bid / Ask · ' + every;
    if (!items.length) return { message: TICKER_STATE_TEXT[payload.status] || TICKER_STATE_TEXT.unavailable, items: [], note: '', detail: payload.detail || '' };
    return { message: '', items: items, note: note, detail: payload.detail || '' };
  }

  function utcTime(iso) {
    if (!iso) return '';
    var d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    return d.toLocaleString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'UTC' }) + ' UTC';
  }

  function chartStatusText(payload) {
    var every = 'Auto-updating about every ' + (payload.poll_interval_seconds || 60) + ' s';
    var last = payload.window && payload.window.last_open_time ? ' · last candle opened ' + utcTime(payload.window.last_open_time) : '';
    if (payload.status === 'market_closed') return 'Market closed' + last + '. No new prices until the session opens.';
    if (payload.status === 'stale') return 'Delayed' + last + '. ' + (payload.detail || '');
    if (payload.status === 'fresh') return every + last + '.';
    return payload.detail || TICKER_STATE_TEXT[payload.status] || TICKER_STATE_TEXT.unavailable;
  }

  // ── live instrument (US30): quotes move the forming candle between candle snapshots ──
  var INTERVAL_SECONDS = { M1: 60, M5: 300, H1: 3600 };

  // The price a quote contributes to a candle, on the side the backend VERIFIED the broker's candles
  // are built from. Unverified side => null: nothing is drawn from quotes.
  function quotePrice(quote, side) {
    if (!quote) return null;
    function num(v) { return v === null || v === undefined || v === '' ? NaN : Number(v); }
    var bid = num(quote.bid), ask = num(quote.ask);
    var value = side === 'bid' ? bid : side === 'ask' ? ask : side === 'mid' ? (bid + ask) / 2 : NaN;
    return isFinite(value) ? value : null;
  }

  // What one quote does to the series. Returns { action, candle }:
  //  'update'    the tick is inside the forming candle: high / low / close move, open stays;
  //  'roll'      the tick opened the next candle: a new candle starts at that price;
  //  'gap'       candles are missing between the last one and this tick: reload candles (backfill) first;
  //  'duplicate' / 'out_of_order'  same or older provider time than the last applied tick: ignored;
  //  'unverified' / 'invalid' / 'empty'  nothing applied.
  function applyQuote(series, quote, intervalSec, side, lastTickMs) {
    if (!series || !series.length) return { action: 'empty', candle: null };
    var price = quotePrice(quote, side);
    if (side !== 'bid' && side !== 'ask' && side !== 'mid') return { action: 'unverified', candle: null };
    var tickMs = quote && typeof quote.provider_time_ms === 'number' ? quote.provider_time_ms : NaN;
    if (price === null || !isFinite(tickMs) || !(intervalSec > 0)) return { action: 'invalid', candle: null };
    if (typeof lastTickMs === 'number' && tickMs === lastTickMs) return { action: 'duplicate', candle: null };
    if (typeof lastTickMs === 'number' && tickMs < lastTickMs) return { action: 'out_of_order', candle: null };
    var last = series[series.length - 1];
    var bucket = Math.floor(tickMs / 1000 / intervalSec) * intervalSec;
    if (bucket < last.time) return { action: 'out_of_order', candle: null };      // belongs to a candle already closed
    if (bucket === last.time) {
      return { action: 'update', candle: { time: last.time, open: last.open, high: Math.max(last.high, price), low: Math.min(last.low, price), close: price, ticks: last.ticks } };
    }
    if (bucket === last.time + intervalSec) return { action: 'roll', candle: { time: bucket, open: price, high: price, low: price, close: price, ticks: null } };
    return { action: 'gap', candle: null };
  }

  // A fresh candle snapshot meets what quotes already drew. Candles from the broker are the record;
  // a quote newer than the snapshot only keeps the forming candle from stepping back in time.
  function mergeSnapshot(series, live, snapshotAtMs) {
    if (!live || !live.candle || !series.length || !(live.tickMs > snapshotAtMs)) return series;
    var out = series.slice();
    var last = out[out.length - 1], c = live.candle;
    if (c.time === last.time) {
      out[out.length - 1] = { time: last.time, open: last.open, high: Math.max(last.high, c.close), low: Math.min(last.low, c.close), close: c.close, ticks: last.ticks };
    } else if (c.time > last.time && c.time - last.time <= (live.intervalSec || 0)) {
      out.push({ time: c.time, open: c.open, high: c.high, low: c.low, close: c.close, ticks: null });
    }
    return out;
  }

  // Header line and corner tag for a live instrument. Says exactly what is happening.
  function liveStatusView(payload, info) {
    info = info || {};
    if (info.offline) return { tag: 'OFFLINE', text: 'Disconnected from TaliTrade. Showing the last prices received; candles will be reloaded when the connection returns.' };
    if (!payload) return { tag: 'LOADING', text: 'Connecting to live prices…' };
    var res = payload.resolution || {};
    if (res.status === 'ambiguous' || res.status === 'missing') return { tag: 'UNAVAILABLE', text: payload.detail || 'This instrument is not available on this account.' };
    if (payload.status === 'market_closed') return { tag: 'CLOSED', text: 'Market closed. Last published price' + (payload.quote && payload.quote.provider_time ? ' at ' + utcTime(payload.quote.provider_time) : '') + '. No new prices until the session opens.' };
    if (payload.status === 'permission_denied' || payload.status === 'unavailable') return { tag: 'UNAVAILABLE', text: payload.detail || 'Prices unavailable right now.' };
    if (payload.status === 'loading') return { tag: 'LOADING', text: 'Waiting for the first quote from the broker…' };
    var every = 'refreshed about every ' + (payload.poll_interval_seconds || 6) + ' s (polling)';
    if (payload.status === 'stale') return { tag: 'DELAYED', text: 'Delayed: prices have not refreshed recently. ' + (payload.detail || '') };
    var basis = payload.price_basis || {};
    if (basis.side !== 'bid' && basis.side !== 'ask' && basis.side !== 'mid') {
      return { tag: 'VERIFYING', text: 'Quotes ' + every + '. Checking which price the broker builds candles from; until then candles refresh every minute.' };
    }
    return { tag: 'LIVE', text: 'Forming candle follows the ' + basis.side + ' price, ' + every + '.' };
  }

  function percentile(values, p) {
    if (!values.length) return null;
    var sorted = values.slice().sort(function (a, b) { return a - b; });
    return sorted[Math.min(sorted.length - 1, Math.floor(p * sorted.length))];
  }

  var pure = { formatPrice: formatPrice, toSeries: toSeries, toLine: toLine, diffSeries: diffSeries, tickerView: tickerView,
               chartStatusText: chartStatusText, utcTime: utcTime, TICKER_STATE_TEXT: TICKER_STATE_TEXT,
               quotePrice: quotePrice, applyQuote: applyQuote, mergeSnapshot: mergeSnapshot, liveStatusView: liveStatusView, percentile: percentile,
               INTERVAL_SECONDS: INTERVAL_SECONDS, LIVE_INSTRUMENTS: ['US30'], LIVE_POLL_MS: 2000, LIVE_OFFLINE_AFTER_FAILURES: 2,
               TICKER_POLL_MS: 15000, CHART_POLL_MS: 30000, INTERVALS: ['M1', 'M5', 'H1'], DEFAULT_INTERVAL: 'M5' };
  global.TaliMatchTraderMarketCore = pure;
  global.TaliMatchTraderMarket = { create: null };   // the controller is defined in the second half of this file
})(typeof window !== 'undefined' ? window : globalThis);

(function initTaliMatchTraderMarketController(global) {
  'use strict';
  var core = global.TaliMatchTraderMarketCore;

  function createMarket(deps) {
    var doc = deps.document;
    var host = null;                       // page hooks, set by attach()
    var state = { generation: 0, tickerTimer: null, chartTimer: null, tickerPayload: null, tickerBusy: false,
                  chart: null, mode: 'line', interval: core.DEFAULT_INTERVAL, shown: [], shownKey: '', chartSeq: 0, inMarket: false, available: null, tickerAt: 0, tickerKey: '',
                  instrument: null, live: null, liveTimer: null, channel: null };
    function freshLive() {
      return { payload: null, key: '', busy: false, at: 0, failures: 0, offline: false, lastTickMs: null, candle: null, available: null, pendingQuote: null,
               stats: { polls: 0, shared: 0, applied: 0, rolled: 0, duplicates: 0, outOfOrder: 0, gaps: 0, backfills: 0, latenciesMs: [] } };
    }
    state.live = freshLive();
    function nowMs() { return deps.now ? deps.now() : Date.now(); }

    function el(tag, text, className) {
      var node = doc.createElement(tag);
      if (className) node.className = className;
      if (text !== undefined && text !== null) node.textContent = String(text);
      return node;
    }
    function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
    function byId(id) { return doc.getElementById(id); }
    function setText(id, text) { var node = byId(id); if (node) node.textContent = text; }
    function show(id, visible) { var node = byId(id); if (node) node.style.display = visible ? '' : 'none'; }
    function hidden() { return deps.isHidden ? deps.isHidden() : !!doc.hidden; }
    function apiAccount() {
      var key = host ? host.getActiveAccount() : '';
      return key && host.isApiAccount(key) ? { key: key, id: host.connectionIdForAccount(key) } : null;
    }
    function context() { return { session: deps.getSessionToken(), account: host ? host.getActiveAccount() : '', generation: state.generation }; }
    function current(ctx) { return !!host && ctx.generation === state.generation && ctx.session === deps.getSessionToken() && ctx.account === host.getActiveAccount(); }

    // ── ticker ──
    // The strip scrolls by showing its content twice in a row; both copies are always rendered from the same data.
    function renderTicker(view) {
      var strip = byId('tickerInner');
      if (!strip) return;
      clear(strip);
      for (var copy = 0; copy < 2; copy++) {
        if (view.message) {
          var only = el('span', null, 'tick-i');
          only.appendChild(el('span', 'Markets', 'tick-s'));
          only.appendChild(el('span', view.message, 't-flat'));
          strip.appendChild(only);
          strip.appendChild(el('span', '·', 't-sep'));
          continue;
        }
        var basis = el('span', null, 'tick-i');
        basis.appendChild(el('span', view.note, 'tick-s'));
        strip.appendChild(basis);
        strip.appendChild(el('span', '·', 't-sep'));
        view.items.forEach(function (item) {
          var cell = el('span', null, 'tick-i');
          cell.appendChild(el('span', item.label, 'tick-s'));
          cell.appendChild(el('span', item.price + (item.closed ? ' · closed' : item.stale ? ' · delayed' : ''), item.closed || item.stale ? 't-flat' : 't-px'));
          strip.appendChild(cell);
          strip.appendChild(el('span', '·', 't-sep'));
        });
      }
      strip.setAttribute('data-market-status', view.message ? 'message' : 'prices');
      strip.title = view.detail || '';
    }
    function renderTickerIdle() {
      renderTicker({ message: host && host.getActiveAccount() ? 'Prices are shown for Match-Trader API accounts' : 'Connect a Match-Trader account to see its prices', items: [], note: '' });
    }
    function refreshTicker(force) {
      var account = apiAccount();
      if (!account) { state.tickerPayload = null; renderTickerIdle(); return Promise.resolve(); }
      if (state.available === false) { renderTicker({ message: core.TICKER_STATE_TEXT.feature_off, items: [], note: '' }); return Promise.resolve(); }
      if (hidden() || state.tickerBusy) return Promise.resolve();          // nobody is looking, or a read is already in flight
      var nowMs = deps.now ? deps.now() : Date.now();
      if (!force && state.tickerKey === account.key && nowMs - state.tickerAt < core.TICKER_POLL_MS - 500) return Promise.resolve();   // one read per interval, whoever asks
      state.tickerAt = nowMs; state.tickerKey = account.key;
      state.tickerBusy = true;
      var ctx = context();
      return deps.api.getTicker(account.id, {}).then(function (payload) {
        if (!current(ctx)) return;
        state.available = true;
        state.tickerPayload = payload;
        renderTicker(core.tickerView(payload));
      }, function (err) {
        if (!current(ctx)) return;
        if (err && err.status === 404) { state.available = err.message === 'Connection not found' ? state.available : false; }
        var kept = state.tickerPayload ? core.tickerView(Object.assign({}, state.tickerPayload, { status: 'stale' })) : null;
        renderTicker(kept && kept.items.length ? kept : { message: state.available === false ? core.TICKER_STATE_TEXT.feature_off : core.TICKER_STATE_TEXT.unavailable, items: [], note: '' });
      }).then(function () { state.tickerBusy = false; });
    }
    function startTicker() {
      if (state.tickerTimer) return;
      state.tickerTimer = deps.setInterval(function () { refreshTicker(false); }, core.TICKER_POLL_MS);
      refreshTicker(true);
    }

    // ── chart ──
    function chartVisible() { var page = byId('page-chart'); return !!page && page.classList.contains('active'); }
    function ensureCandleSeries() {
      if (!state.chart.candleSeries) {
        state.chart.candleSeries = state.chart.chart.addCandlestickSeries({ upColor: '#4dcd8a', downColor: '#e05c5c', borderVisible: false, wickUpColor: '#4dcd8a', wickDownColor: '#e05c5c', visible: false });
      }
      return state.chart.candleSeries;
    }
    function applyMode() {
      var candle = ensureCandleSeries();
      candle.applyOptions({ visible: state.mode === 'candle' });
      state.chart.lineSeries.applyOptions({ visible: state.mode === 'line' });
      var a = byId('toolCandle'), b = byId('toolLine');
      if (a) a.classList.toggle('active', state.mode === 'candle');
      if (b) b.classList.toggle('active', state.mode === 'line');
    }
    function setSeries(series, how) {
      var candle = ensureCandleSeries(), line = state.chart.lineSeries;
      if (how === 'tail' && state.shown.length) {
        var lastShown = state.shown[state.shown.length - 1].time;
        series.filter(function (c) { return c.time >= lastShown; }).forEach(function (c) {      // forming candle updated in place, new ones appended
          candle.update({ time: c.time, open: c.open, high: c.high, low: c.low, close: c.close });
          line.update({ time: c.time, value: c.close });
        });
      } else {
        var scale = state.chart.chart.timeScale();
        var keep = how === 'reset-keep-view' && scale.getVisibleLogicalRange ? scale.getVisibleLogicalRange() : null;
        candle.setData(series.map(function (c) { return { time: c.time, open: c.open, high: c.high, low: c.low, close: c.close }; }));
        line.setData(core.toLine(series));
        if (line.setMarkers) line.setMarkers([]);
        if (keep && scale.setVisibleLogicalRange) scale.setVisibleLogicalRange(keep); else if (scale.fitContent) scale.fitContent();
      }
      state.shown = series;
    }
    function labelsForMarket(payload) {
      var symbol = payload.symbol;
      var name = symbol ? (symbol.alias || symbol.symbol) : 'Market price';
      var precision = symbol ? symbol.price_precision : null;
      var last = state.shown.length ? state.shown[state.shown.length - 1] : null;
      setText('ph-sym', name + ' · Market price · ' + state.interval + ' candles');
      setText('ph-price', last ? core.formatPrice(String(last.close), precision) : '—');
      setText('ph-change', core.chartStatusText(payload));
      ['Open', 'High', 'Low', 'Close'].forEach(function (label, i) { setText('ohlcLabel' + i, label); });
      setText('ohlc-o', last ? core.formatPrice(String(last.open), precision) : '—');
      setText('ohlc-h', last ? core.formatPrice(String(last.high), precision) : '—');
      setText('ohlc-l', last ? core.formatPrice(String(last.low), precision) : '—');
      setText('ohlc-v', last ? core.formatPrice(String(last.close), precision) : '—');
      setText('instSourceVal', 'Broker candles');
      setText('instSourceSub', 'Price side not specified by the provider');
      setText('instMarkersVal', symbol && symbol.symbol !== name ? symbol.symbol : (symbol && symbol.description) || 'Provider symbol');
      setText('instMarkersSub', symbol && symbol.session_open === false ? 'Session closed' : symbol ? 'Session open' : '');
      setText('instMarkersLabel', 'Instrument');
      setText('chartLiveTag', payload.status === 'market_closed' ? 'CLOSED' : payload.status === 'fresh' ? 'AUTO' : 'DELAYED');
      if (state.instrument) renderLiveStatus();
      // Describes what is actually drawn, not what was requested.
      var drawn = state.shown.length;
      var firstIso = drawn ? new Date(state.shown[0].time * 1000).toISOString() : null, lastIso = drawn ? new Date(last.time * 1000).toISOString() : null;
      setText('chartWindowNote', drawn ? 'Showing the latest ' + drawn + ' ' + state.interval + ' candles (' + core.utcTime(firstIso) + ' → ' + core.utcTime(lastIso) + '). Older candles are not loaded. Read-only: TaliTrade cannot place orders.'
        : 'Read-only: TaliTrade cannot place orders.');
      var row = byId('chartIntervalRow');
      if (row) Array.prototype.forEach.call(row.querySelectorAll('[data-interval]'), function (b) {
        var on = b.getAttribute('data-interval') === state.interval;
        b.classList.toggle('active', on);
        b.setAttribute('aria-pressed', on ? 'true' : 'false');             // announced selected state
      });
    }
    function setMarketLayout(on) {
      show('chartIntervalRow', on); show('chartWindowNote', on);
      show('chartRangeRow', !on); show('orderPanel', true); show('depthCard', !on);       // Keep the order ticket visible; never show synthetic market depth.
      var candleTool = byId('toolCandle'), lineTool = byId('toolLine');
      if (candleTool) {
        var tip = on ? 'Candles: open, high, low and close for each period' : 'Candle view is available for Match-Trader API accounts';
        candleTool.setAttribute('title', tip); candleTool.setAttribute('aria-label', tip);
      }
      if (lineTool) { var lineTip = on ? 'Line: closing price of each period' : 'Line: account equity over time'; lineTool.setAttribute('title', lineTip); lineTool.setAttribute('aria-label', lineTip); }
      var low = byId('ohlc-l'), close = byId('ohlc-v');                                    // these cells are green/red for wins/losses, not for prices
      if (low) low.classList.toggle('txt-green', !on);
      if (close) close.classList.toggle('txt-red', !on);
      if (!on) {
        ['Points', 'Trades', 'Wins', 'Losses'].forEach(function (label, i) { setText('ohlcLabel' + i, label); });
        setText('instSourceVal', 'Trade timeline'); setText('instSourceSub', 'balanceAfter / equity');
        setText('instMarkersLabel', 'Markers'); setText('instMarkersVal', 'Closed trades'); setText('instMarkersSub', 'Win/Loss overlays');
        setText('chartLiveTag', 'LIVE');
      }
    }
    function loadChart() {
      var account = apiAccount();
      if (!account || !state.chart) return Promise.resolve();
      var liveSymbol = liveSymbolFor(account);
      if (state.instrument && !liveSymbol) return refreshLive(true);       // the broker's symbol is resolved first; candles follow
      var key = account.key + '|' + state.interval + '|' + (liveSymbol || '');
      var seq = ++state.chartSeq;
      var ctx = context();
      if (state.shownKey !== key) {                                   // different account or interval: never leave the old series on screen
        state.shown = []; state.shownKey = key;
        ensureCandleSeries().setData([]); state.chart.lineSeries.setData([]);
        host.showChartState('Loading ' + state.interval + ' candles…', 'Reading the broker’s candles saved by TaliTrade.');
        setText('ph-sym', 'Market price · ' + state.interval + ' candles'); setText('ph-price', '—'); setText('ph-change', 'Loading…');
      }
      return deps.api.getCandles(account.id, liveSymbol ? { interval: state.interval, symbol: liveSymbol } : { interval: state.interval }).then(function (payload) {
        if (!current(ctx) || seq !== state.chartSeq || state.shownKey !== key || !state.inMarket) return;      // an older answer never overwrites the current view
        if (payload.interval !== state.interval) return;
        var series = core.toSeries(payload.candles);
        if (liveSymbol && state.live.candle) {
          var snapshotAt = payload.last_success_at ? Date.parse(payload.last_success_at) : 0;
          series = core.mergeSnapshot(series, { candle: state.live.candle, tickMs: state.live.lastTickMs, intervalSec: core.INTERVAL_SECONDS[state.interval] }, snapshotAt);
        }
        if (!series.length) {
          labelsForMarket(payload);
          host.showChartState(payload.status === 'loading' ? 'Loading candles…' : payload.status === 'permission_denied' ? 'Market data not permitted' : 'No candles available',
                              payload.detail || (payload.status === 'loading' ? 'The first candles are being fetched from your broker.' : 'The broker returned no candles for this instrument.'));
          return;
        }
        var how = state.shown.length ? core.diffSeries(state.shown, series) : 'reset';
        if (how !== 'same') setSeries(series, how === 'reset' && state.shown.length ? 'reset-keep-view' : how);
        host.hideChartState();
        labelsForMarket(payload);
        if (liveSymbol && state.live.pendingQuote) { var waiting = state.live.pendingQuote; state.live.pendingQuote = null; applyLive(waiting); }
      }, function (err) {
        if (!current(ctx) || seq !== state.chartSeq || !state.inMarket) return;
        if (err && err.status === 404 && err.message !== 'Connection not found') {
          host.showChartState('Market chart not enabled', 'Market prices are not enabled on this server. Balance, positions and trades are on the dashboard.');
          setText('ph-change', 'Market data is not enabled on this server');
          return;
        }
        if (!state.shown.length) host.showChartState('Could not load candles', 'TaliTrade could not be reached. It will be retried.');
        setText('ph-change', 'Could not refresh. Showing the last loaded candles.');
      });
    }
    // ── live instrument (US30) ──
    function liveSymbolFor(account) {
      var p = state.live.payload;
      if (!state.instrument || !p || state.live.key !== account.key + '|' + state.instrument) return null;
      return p.resolution && p.resolution.status === 'resolved' && p.resolution.symbol ? p.resolution.symbol.symbol : null;
    }
    function renderLiveStatus() {
      var view = core.liveStatusView(state.live.payload, { offline: state.live.offline });
      setText('chartLiveTag', view.tag);
      setText('ph-change', view.text);
      var p = state.live.payload, res = p && p.resolution;
      if (res && res.symbol) {
        setText('instMarkersVal', res.symbol.symbol !== state.instrument ? res.symbol.symbol + ' (' + state.instrument + ')' : res.symbol.symbol);
        var side = p.price_basis && p.price_basis.side;
        setText('instSourceSub', side === 'bid' || side === 'ask' || side === 'mid' ? 'Candles built from the ' + side + ' price (verified)' : 'Candle price side being verified');
      }
      var row = byId('chartIntervalRow');
      if (row) Array.prototype.forEach.call(row.querySelectorAll('[data-instrument]'), function (b) {
        var on = b.getAttribute('data-instrument') === state.instrument;
        b.classList.toggle('active', on); b.setAttribute('aria-pressed', on ? 'true' : 'false');
      });
    }
    function drawCandle(candle) {
      ensureCandleSeries().update({ time: candle.time, open: candle.open, high: candle.high, low: candle.low, close: candle.close });
      state.chart.lineSeries.update({ time: candle.time, value: candle.close });            // update(): pan and zoom stay as the user left them
      var shown = state.shown.slice();
      if (shown.length && shown[shown.length - 1].time === candle.time) shown[shown.length - 1] = candle; else shown.push(candle);
      state.shown = shown;
      var res = state.live.payload && state.live.payload.resolution;
      var precision = res && res.symbol ? res.symbol.price_precision : null;
      setText('ph-price', core.formatPrice(String(candle.close), precision));
      setText('ohlc-o', core.formatPrice(String(candle.open), precision)); setText('ohlc-h', core.formatPrice(String(candle.high), precision));
      setText('ohlc-l', core.formatPrice(String(candle.low), precision)); setText('ohlc-v', core.formatPrice(String(candle.close), precision));
    }
    function applyLive(payload) {
      if (!state.inMarket || !state.chart || !payload || !payload.quote) return;
      if (payload.status !== 'fresh' && payload.status !== 'stale') return;        // closed market: the last candle is already final
      if (!state.shown.length) { state.live.pendingQuote = payload; return; }      // candles not loaded yet: apply once they are
      var side = payload.price_basis && payload.price_basis.side;
      var result = core.applyQuote(state.shown, payload.quote, core.INTERVAL_SECONDS[state.interval], side, state.live.lastTickMs);
      var stats = state.live.stats;
      if (result.action === 'duplicate') { stats.duplicates += 1; return; }
      if (result.action === 'out_of_order') { stats.outOfOrder += 1; return; }
      if (result.action === 'gap') {                                                // candles missing: backfill from the broker's candles, never invent them
        stats.gaps += 1; state.live.pendingQuote = payload; backfill(); return;
      }
      if (result.action !== 'update' && result.action !== 'roll') return;
      state.live.lastTickMs = payload.quote.provider_time_ms;
      state.live.candle = result.candle;
      drawCandle(result.candle);
      stats.applied += 1;
      if (result.action === 'roll') stats.rolled += 1;
      stats.latenciesMs.push(Math.max(0, nowMs() - payload.quote.provider_time_ms));   // quote-to-chart: broker tick time to drawn
      if (stats.latenciesMs.length > 500) stats.latenciesMs.shift();
    }
    // Re-read the candle window without clearing the chart: missing candles are filled from the broker's
    // own candles and the view (pan / zoom) is kept. At most one reload every few seconds.
    function backfill() {
      var t = nowMs();
      if (t - (state.live.backfillAt || 0) < 5000) return;
      state.live.backfillAt = t; state.live.stats.backfills += 1;
      loadChart();
    }
    function acceptLive(payload, accountKey, shared) {
      var wasOffline = state.live.offline;
      state.live.payload = payload; state.live.key = accountKey + '|' + state.instrument; state.live.at = nowMs();
      state.live.failures = 0; state.live.offline = false; state.live.available = true;
      if (shared) state.live.stats.shared += 1;
      var res = payload && payload.resolution;
      var symbol = res && res.status === 'resolved' && res.symbol ? res.symbol.symbol : null;
      if (!state.inMarket) return;
      if (!symbol) {
        state.shown = []; state.shownKey = ''; ensureCandleSeries().setData([]); state.chart.lineSeries.setData([]);
        host.showChartState(res && res.status === 'pending' ? 'Loading instruments…' : state.instrument + ' not available', (payload && payload.detail) || '');
        renderLiveStatus();
        return;
      }
      var expected = accountKey + '|' + state.interval + '|' + symbol;
      if (state.shownKey !== expected) {                        // first resolution or a changed symbol: load its candles
        state.live.pendingQuote = payload;
        loadChart();
      } else if (wasOffline) {                                  // back after a disconnect: reload candles in place (keeps zoom), then apply
        state.live.pendingQuote = payload;
        backfill();
      } else {
        applyLive(payload);
      }
      renderLiveStatus();
    }
    function refreshLive(force) {
      var account = apiAccount();
      if (!account || !state.instrument || state.live.available === false) return Promise.resolve();
      if (state.live.busy) return Promise.resolve();
      if (!force && (hidden() || !chartVisible())) return Promise.resolve();          // nobody is looking
      if (!force && state.live.key === account.key + '|' + state.instrument && nowMs() - state.live.at < core.LIVE_POLL_MS - 250) return Promise.resolve();   // another tab just shared one
      state.live.busy = true; state.live.stats.polls += 1;
      var ctx = context(), instrument = state.instrument, live = state.live;
      return deps.api.getLive(account.id, { instrument: instrument }).then(function (payload) {
        if (!current(ctx) || instrument !== state.instrument || live !== state.live) return;
        acceptLive(payload, account.key, false);
        if (state.channel) { try { state.channel.postMessage({ type: 'live', account: account.key, instrument: instrument, payload: payload }); } catch { /* closed */ } }
      }, function (err) {
        if (!current(ctx) || live !== state.live) return;
        if (err && err.status === 404 && err.message !== 'Connection not found') {     // switched off on this server: snapshots only
          state.live.available = false; state.instrument = null; state.shownKey = ''; stopLive();
          var row = byId('chartIntervalRow');
          if (row) Array.prototype.forEach.call(row.querySelectorAll('[data-instrument]'), function (b) {
            b.classList.remove('active'); b.setAttribute('aria-pressed', 'false'); b.disabled = true; b.title = 'Live prices are not enabled on this server';
          });
          if (state.inMarket) { loadChart(); setText('ph-change', 'Live prices are not enabled on this server. Candles refresh about every minute.'); }
          return;
        }
        state.live.failures += 1;
        if (state.live.failures >= core.LIVE_OFFLINE_AFTER_FAILURES) { state.live.offline = true; if (state.inMarket) renderLiveStatus(); }
      }).then(function () { live.busy = false; });
    }
    function onShared(message) {                                       // another tab of this browser read the same account: reuse its answer
      var data = message && message.data;
      var account = apiAccount();
      if (!data || data.type !== 'live' || !account || data.account !== account.key || data.instrument !== state.instrument || !data.payload) return;
      acceptLive(data.payload, account.key, true);
    }
    function startLive() {
      if (!state.channel && deps.openChannel) { state.channel = deps.openChannel('tali-market-live'); if (state.channel) state.channel.onmessage = onShared; }
      if (!state.liveTimer) state.liveTimer = deps.setInterval(function () { if (state.inMarket && state.instrument) refreshLive(false); }, core.LIVE_POLL_MS);
      refreshLive(true);
    }
    function stopLive() {
      if (state.liveTimer) { deps.clearInterval(state.liveTimer); state.liveTimer = null; }
      if (state.channel) { try { state.channel.close(); } catch { /* already closed */ } state.channel = null; }
    }
    function setInstrument(instrument) {
      var next = instrument && core.LIVE_INSTRUMENTS.indexOf(instrument) >= 0 ? instrument : null;
      if (next === state.instrument) return;
      state.instrument = next; state.live = freshLive(); state.shownKey = ''; state.chartSeq += 1;
      if (!state.inMarket) return;
      if (next) { startLive(); renderLiveStatus(); } else { stopLive(); loadChart(); }
      var row = byId('chartIntervalRow');
      if (row && !next) Array.prototype.forEach.call(row.querySelectorAll('[data-instrument]'), function (b) { b.classList.remove('active'); b.setAttribute('aria-pressed', 'false'); });
    }
    function liveStats() {
      var s = state.live.stats, l = s.latenciesMs, p = state.live.payload;
      return { instrument: state.instrument, mode: 'polling', polls: s.polls, shared: s.shared, applied: s.applied, rolled: s.rolled, duplicates: s.duplicates,
               out_of_order: s.outOfOrder, gaps: s.gaps, backfills: s.backfills, samples: l.length,
               quote_to_chart_ms: { p50: core.percentile(l, 0.5), p95: core.percentile(l, 0.95), max: l.length ? Math.max.apply(null, l) : null },
               poll_interval_seconds: p ? p.poll_interval_seconds : null, price_basis: p && p.price_basis ? p.price_basis.side : null,
               status: p ? p.status : null, offline: state.live.offline };
    }

    // Called by the page whenever the chart should reflect the active account (often: every status poll).
    function showChart(chartRefs) {
      if (!apiAccount()) return false;
      state.chart = state.chart && state.chart.chart === chartRefs.chart ? state.chart : { chart: chartRefs.chart, lineSeries: chartRefs.lineSeries, candleSeries: null };
      var entering = !state.inMarket;
      state.inMarket = true;
      if (entering) { setMarketLayout(true); applyMode(); }
      var key = host.getActiveAccount() + '|' + state.interval;
      if (entering || state.shownKey !== key) loadChart();
      if (!state.chartTimer) {
        state.chartTimer = deps.setInterval(function () { if (state.inMarket && chartVisible() && !hidden()) loadChart(); }, core.CHART_POLL_MS);
      }
      if (state.instrument) startLive();
      return true;
    }
    function leaveChart() {
      if (!state.inMarket) return;
      state.inMarket = false; state.chartSeq += 1; state.shown = []; state.shownKey = '';
      if (state.chartTimer) { deps.clearInterval(state.chartTimer); state.chartTimer = null; }
      stopLive();
      if (state.chart) {
        if (state.chart.candleSeries) { state.chart.candleSeries.setData([]); state.chart.candleSeries.applyOptions({ visible: false }); }
        state.chart.lineSeries.setData([]); state.chart.lineSeries.applyOptions({ visible: true });
      }
      setMarketLayout(false);
    }
    function setMode(mode) { if (mode !== 'line' && mode !== 'candle') return; state.mode = mode; if (state.inMarket && state.chart) applyMode(); }
    function setInterval_(interval) {
      if (core.INTERVALS.indexOf(interval) < 0 || interval === state.interval) return;
      state.interval = interval;
      if (state.inMarket) loadChart();
    }
    function onContextChange() {                                       // account switched or session changed
      state.generation += 1; state.chartSeq += 1; state.tickerPayload = null; state.tickerBusy = false;
      state.shownKey = ''; state.tickerKey = ''; state.live = freshLive();     // never carry one account's quotes onto another's chart
      if (host) refreshTicker(true);
    }
    function reset() {
      leaveChart(); onContextChange();
      if (state.tickerTimer) { deps.clearInterval(state.tickerTimer); state.tickerTimer = null; }
      state.available = null; state.chart = null;
      renderTickerIdle();
    }
    function attach(pageHost) { host = pageHost; }

    return { attach: attach, startTicker: startTicker, refreshTicker: refreshTicker, showChart: showChart, leaveChart: leaveChart, setMode: setMode,
             setInterval: setInterval_, onContextChange: onContextChange, reset: reset, setInstrument: setInstrument, liveStats: liveStats,
             refreshLive: function () { return refreshLive(true); }, instrument: function () { return state.instrument; },
             refreshChart: function () { return state.inMarket ? loadChart() : Promise.resolve(); },
             inMarketMode: function () { return state.inMarket; }, interval: function () { return state.interval; }, mode: function () { return state.mode; } };
  }

  function defaultDeps() {
    var tokenOf = function () { try { return global.localStorage.getItem('tali_session_token_v1') || ''; } catch { return ''; } };
    var base = { fetch: function (url, init) { return global.fetch(url, init); }, getBaseApiUrl: function () { return global.TaliApiBase.resolveApiBase(); }, getSessionToken: tokenOf };
    return { api: global.TaliMatchTraderApi.create(base), getSessionToken: tokenOf, document: global.document,
             setInterval: function (fn, ms) { return global.setInterval(fn, ms); }, clearInterval: function (id) { return global.clearInterval(id); },
             // Tabs of one browser share live answers, so N open charts read TaliTrade about once per interval, not N times.
             openChannel: function (name) { try { return typeof global.BroadcastChannel === 'function' ? new global.BroadcastChannel(name) : null; } catch { return null; } } };
  }

  var instance = null;
  var facade = { create: createMarket };
  ['attach', 'startTicker', 'refreshTicker', 'showChart', 'leaveChart', 'setMode', 'setInterval', 'onContextChange', 'reset', 'refreshChart', 'inMarketMode', 'interval', 'mode',
   'setInstrument', 'liveStats', 'refreshLive', 'instrument'].forEach(function (name) {
    facade[name] = function () {
      if (!instance) instance = createMarket(defaultDeps());
      return instance[name].apply(instance, arguments);
    };
  });
  global.TaliMatchTraderMarket = facade;
})(typeof window !== 'undefined' ? window : globalThis);
