// Market ticker and price chart for Match-Trader API accounts in the TaliTrade dashboard.
// Requires tali-matchtrader-api.js and tali-matchtrader-dashboard.js.
//
// Rules this file keeps:
//  * it only reads TaliTrade's own backend (cached broker data); the broker token never reaches
//    the browser and no third-party price source is used;
//  * nothing here is "live streaming": prices auto-update by polling, and the page says so;
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

  var pure = { formatPrice: formatPrice, toSeries: toSeries, toLine: toLine, diffSeries: diffSeries, tickerView: tickerView,
               chartStatusText: chartStatusText, utcTime: utcTime, TICKER_STATE_TEXT: TICKER_STATE_TEXT,
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
                  chart: null, mode: 'line', interval: core.DEFAULT_INTERVAL, shown: [], shownKey: '', chartSeq: 0, inMarket: false, available: null, tickerAt: 0, tickerKey: '' };

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
      var key = account.key + '|' + state.interval;
      var seq = ++state.chartSeq;
      var ctx = context();
      if (state.shownKey !== key) {                                   // different account or interval: never leave the old series on screen
        state.shown = []; state.shownKey = key;
        ensureCandleSeries().setData([]); state.chart.lineSeries.setData([]);
        host.showChartState('Loading ' + state.interval + ' candles…', 'Reading the broker’s candles saved by TaliTrade.');
        setText('ph-sym', 'Market price · ' + state.interval + ' candles'); setText('ph-price', '—'); setText('ph-change', 'Loading…');
      }
      return deps.api.getCandles(account.id, { interval: state.interval }).then(function (payload) {
        if (!current(ctx) || seq !== state.chartSeq || state.shownKey !== key || !state.inMarket) return;      // an older answer never overwrites the current view
        if (payload.interval !== state.interval) return;
        var series = core.toSeries(payload.candles);
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
      return true;
    }
    function leaveChart() {
      if (!state.inMarket) return;
      state.inMarket = false; state.chartSeq += 1; state.shown = []; state.shownKey = '';
      if (state.chartTimer) { deps.clearInterval(state.chartTimer); state.chartTimer = null; }
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
      state.shownKey = ''; state.tickerKey = '';
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
             setInterval: setInterval_, onContextChange: onContextChange, reset: reset,
             refreshChart: function () { return state.inMarket ? loadChart() : Promise.resolve(); },
             inMarketMode: function () { return state.inMarket; }, interval: function () { return state.interval; }, mode: function () { return state.mode; } };
  }

  function defaultDeps() {
    var tokenOf = function () { try { return global.localStorage.getItem('tali_session_token_v1') || ''; } catch { return ''; } };
    var base = { fetch: function (url, init) { return global.fetch(url, init); }, getBaseApiUrl: function () { return global.TaliApiBase.resolveApiBase(); }, getSessionToken: tokenOf };
    return { api: global.TaliMatchTraderApi.create(base), getSessionToken: tokenOf, document: global.document,
             setInterval: function (fn, ms) { return global.setInterval(fn, ms); }, clearInterval: function (id) { return global.clearInterval(id); } };
  }

  var instance = null;
  var facade = { create: createMarket };
  ['attach', 'startTicker', 'refreshTicker', 'showChart', 'leaveChart', 'setMode', 'setInterval', 'onContextChange', 'reset', 'refreshChart', 'inMarketMode', 'interval', 'mode'].forEach(function (name) {
    facade[name] = function () {
      if (!instance) instance = createMarket(defaultDeps());
      return instance[name].apply(instance, arguments);
    };
  });
  global.TaliMatchTraderMarket = facade;
})(typeof window !== 'undefined' ? window : globalThis);
