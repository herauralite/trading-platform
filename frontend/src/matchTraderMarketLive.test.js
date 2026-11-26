// Live US30 candle updates on the Charts tab (public/tali-matchtrader-market.js): quote → forming
// candle, rollover, stale / duplicate / out-of-order ticks, reconnect backfill, account switching and
// one shared poll across tabs. Synthetic prices only; the API and the chart library are fakes.
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import vm from 'node:vm'
import { fileURLToPath } from 'node:url'

const pub = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../public')
const src = fs.readFileSync(path.join(pub, 'tali-matchtrader-market.js'), 'utf8')
const appHtml = fs.readFileSync(path.join(pub, 'app.html'), 'utf8')
const window = {}
vm.runInNewContext(src, { window, Date, JSON, Number, String, Object, Promise, Math, Array, isFinite, isNaN })
const core = window.TaliMatchTraderMarketCore
const market = window.TaliMatchTraderMarket
const plain = (v) => JSON.parse(JSON.stringify(v))

const T = 1_794_600_000          // a minute boundary, UTC seconds (synthetic)
const candle = (time, o, h, l, c) => ({ time, open: o, high: h, low: l, close: c, ticks: null })
const quote = (sec, bid, ask) => ({ bid: String(bid), ask: String(ask), provider_time_ms: Math.round(sec * 1000) })

// ── pure helpers ─────────────────────────────────────────────────────────────
test('a quote moves only the forming candle: high / low / close inside the interval, open never changes', () => {
  const series = [candle(T - 60, 100, 101, 99, 100.5), candle(T, 100.5, 101, 100, 100.8)]
  const up = core.applyQuote(series, quote(T + 20, 102, 102.4), 60, 'bid', null)
  assert.equal(up.action, 'update')
  assert.deepEqual(plain(up.candle), { time: T, open: 100.5, high: 102, low: 100, close: 102, ticks: null })
  const down = core.applyQuote([series[0], up.candle], quote(T + 30, 99.2, 99.6), 60, 'bid', T * 1000 + 20000)
  assert.deepEqual(plain(down.candle), { time: T, open: 100.5, high: 102, low: 99.2, close: 99.2, ticks: null })
  assert.equal(core.applyQuote(series, quote(T + 20, 102, 102.4), 60, 'ask', null).candle.close, 102.4)     // the verified side is used
  assert.equal(core.applyQuote(series, quote(T + 20, 102, 102.4), 60, 'mid', null).candle.close, 102.2)
})

test('rollover at the interval boundary starts a new candle; a missing candle in between is a gap, never invented', () => {
  const series = [candle(T, 100, 101, 99, 100.5)]
  const roll = core.applyQuote(series, quote(T + 60, 100.7, 101), 60, 'bid', T * 1000 + 59000)
  assert.equal(roll.action, 'roll')
  assert.deepEqual(plain(roll.candle), { time: T + 60, open: 100.7, high: 100.7, low: 100.7, close: 100.7, ticks: null })
  assert.equal(core.applyQuote(series, quote(T + 59.999, 100.6, 101), 60, 'bid', null).action, 'update')    // last millisecond still belongs to T
  assert.equal(core.applyQuote(series, quote(T + 180, 100.7, 101), 60, 'bid', null).action, 'gap')          // two minutes skipped: backfill
  assert.equal(core.applyQuote(series, quote(T + 300, 100.7, 101), 300, 'bid', null).action, 'roll')        // M5 boundary
})

test('duplicate, out-of-order, unverified and broken quotes change nothing', () => {
  const series = [candle(T, 100, 101, 99, 100.5)]
  const last = T * 1000 + 30000
  assert.equal(core.applyQuote(series, quote(T + 30, 105, 106), 60, 'bid', last).action, 'duplicate')
  assert.equal(core.applyQuote(series, quote(T + 10, 105, 106), 60, 'bid', last).action, 'out_of_order')
  assert.equal(core.applyQuote(series, quote(T - 5, 105, 106), 60, 'bid', null).action, 'out_of_order')     // belongs to a closed candle
  assert.equal(core.applyQuote(series, quote(T + 40, 105, 106), 60, 'unverified', null).action, 'unverified')
  assert.equal(core.applyQuote(series, { bid: null, ask: '1', provider_time_ms: T * 1000 }, 60, 'bid', null).action, 'invalid')
  assert.equal(core.applyQuote(series, { bid: '1', ask: '1' }, 60, 'bid', null).action, 'invalid')          // no provider time
  assert.equal(core.applyQuote([], quote(T, 1, 2), 60, 'bid', null).action, 'empty')
})

test('a candle snapshot is the record; a newer quote only stops the forming candle from stepping back', () => {
  const snap = [candle(T - 60, 100, 101, 99, 100.5), candle(T, 100.5, 101, 100, 100.6)]
  const live = { candle: candle(T, 100.5, 102, 100, 101.9), tickMs: T * 1000 + 50000, intervalSec: 60 }
  assert.deepEqual(plain(core.mergeSnapshot(snap, live, T * 1000 + 40000).at(-1)), { time: T, open: 100.5, high: 101.9, low: 100, close: 101.9, ticks: null })
  assert.deepEqual(plain(core.mergeSnapshot(snap, live, T * 1000 + 55000)), plain(snap))                    // snapshot newer: it wins
  const ahead = { candle: candle(T + 60, 101, 101, 101, 101), tickMs: T * 1000 + 61000, intervalSec: 60 }
  assert.equal(core.mergeSnapshot(snap, ahead, T * 1000 + 40000).length, 3)                                  // rolled ahead of the snapshot: kept
})

test('status line says polling, verification, delayed, closed and disconnected plainly', () => {
  const base = { status: 'fresh', poll_interval_seconds: 6, resolution: { status: 'resolved', symbol: { symbol: 'US30' } }, quote: quote(T, 1, 2) }
  assert.equal(core.liveStatusView({ ...base, price_basis: { side: 'bid' } }).tag, 'LIVE')
  assert.match(core.liveStatusView({ ...base, price_basis: { side: 'bid' } }).text, /bid price, refreshed about every 6 s \(polling\)/)
  assert.equal(core.liveStatusView({ ...base, price_basis: { side: 'unverified' } }).tag, 'VERIFYING')
  assert.equal(core.liveStatusView({ ...base, status: 'stale' }).tag, 'DELAYED')
  assert.equal(core.liveStatusView({ ...base, status: 'market_closed' }).tag, 'CLOSED')
  assert.equal(core.liveStatusView(base, { offline: true }).tag, 'OFFLINE')
  const ambiguous = core.liveStatusView({ status: 'unavailable', detail: 'several: US30, US30.f', resolution: { status: 'ambiguous' } })
  assert.equal(ambiguous.tag, 'UNAVAILABLE')
  assert.match(ambiguous.text, /US30, US30\.f/)
})

test('served page offers the US30 live toggle next to the intervals', () => {
  assert.match(appHtml, /data-instrument="US30"[^>]*aria-pressed="false"/)
  assert.match(appHtml, /TaliMatchTraderMarket\.setInstrument\(/)
})

// ── controller with fake DOM, API, chart and tabs ───────────────────────────
function fakeDom() {
  const nodes = {}
  const node = (id) => nodes[id] || (nodes[id] = {
    id, textContent: '', style: {}, title: '', disabled: false, attrs: {}, children: [],
    classList: { set: new Set(), toggle(c, on) { on ? this.set.add(c) : this.set.delete(c) }, add(c) { this.set.add(c) }, remove(c) { this.set.delete(c) }, contains(c) { return this.set.has(c) } },
    setAttribute(k, v) { this.attrs[k] = v }, getAttribute(k) { return this.attrs[k] }, querySelectorAll() { return [] },
    appendChild(c) { this.children.push(c) }, removeChild() { this.children.shift() }, get firstChild() { return this.children[0] || null },
  })
  node('page-chart').classList.add('active')
  return { nodes, document: { getElementById: node, createElement: () => node('x' + Math.random()), hidden: false } }
}

function fakeChart() {
  const log = { setData: [], update: [], fit: 0, keepRange: 0 }
  const series = (name) => ({ setData(d) { log.setData.push([name, d.length]) }, update(c) { log.update.push([name, plain(c)]) }, applyOptions() {}, setMarkers() {} })
  const line = series('line')
  return {
    log, refs: { lineSeries: line, chart: { addCandlestickSeries: () => series('candle'),
      timeScale: () => ({ getVisibleLogicalRange: () => ({ from: 10, to: 50 }), setVisibleLogicalRange: () => { log.keepRange += 1 }, fitContent: () => { log.fit += 1 } }) } },
  }
}

const bars = (fromSec, n, step = 60) => Array.from({ length: n }, (_, i) => ({ t: (fromSec + i * step) * 1000, o: '100', h: '101', l: '99', c: '100.5', ticks: 5 }))
const livePayload = (q, extra = {}) => ({ instrument: 'US30', status: 'fresh', poll_interval_seconds: 6, mode: 'polling', quote: q,
  resolution: { status: 'resolved', symbol: { symbol: 'DJI30', alias: 'DJI30', price_precision: 1 } }, price_basis: { side: 'bid' }, ...extra })

function setup({ bus, now } = {}) {
  const dom = fakeDom()
  const chart = fakeChart()
  const clock = now || { t: (T + 5) * 1000 }
  const calls = { live: [], candles: [] }
  const answers = { live: [], candles: () => ({ interval: 'M1', status: 'fresh', candles: bars(T - 540, 10), last_success_at: new Date((T + 1) * 1000).toISOString(), symbol: { symbol: 'DJI30' } }) }
  let account = 'mt:1'
  const states = []
  const api = {
    getTicker: () => Promise.resolve({ status: 'fresh', items: [] }),
    getCandles: (id, o) => { calls.candles.push(plain(o)); return Promise.resolve(answers.candles(o)) },
    getLive: (id, o) => { calls.live.push([id, plain(o)]); const next = answers.live.shift(); return next instanceof Error ? Promise.reject(next) : Promise.resolve(next) },
  }
  const channels = []
  const timers = []
  const mk = market.create({
    api, document: dom.document, getSessionToken: () => 'session', now: () => clock.t,
    setInterval: (fn, ms) => { timers.push({ fn, ms }); return timers.length }, clearInterval: () => {},
    openChannel: bus ? () => { const ch = bus.open(); channels.push(ch); return ch } : undefined,
  })
  mk.attach({ getActiveAccount: () => account, isApiAccount: () => true, connectionIdForAccount: (k) => Number(k.split(':')[1]),
    showChartState: (title, sub) => states.push([title, sub]), hideChartState: () => {} })
  const fireLiveTimer = () => timers.filter((t) => t.ms === core.LIVE_POLL_MS).forEach((t) => t.fn())
  return { mk, dom, chart, clock, calls, answers, states, fireLiveTimer, setAccount: (a) => { account = a } }
}
const settle = () => new Promise((r) => setTimeout(r, 0))

test('US30 resolves to the broker symbol, its candles load once, and quotes update the open candle in place (zoom kept)', async () => {
  const s = setup()
  s.answers.live.push(livePayload(quote(T + 5, 100.9, 101.3)))
  s.mk.showChart(s.chart.refs); await settle()
  s.mk.setInterval('M1')
  s.mk.setInstrument('US30'); await settle(); await settle()
  assert.deepEqual(s.calls.candles.at(-1), { interval: 'M1', symbol: 'DJI30' })                // history comes from the broker's candles for the resolved symbol
  const setDataBefore = s.chart.log.setData.length, fitBefore = s.chart.log.fit
  assert.deepEqual(s.chart.log.update.at(-1)[1], { time: T, value: 100.9 })                 // first tick moved the forming T candle
  s.answers.live.push(livePayload(quote(T + 20, 102.2, 102.6)))
  s.clock.t = (T + 22) * 1000
  await s.mk.refreshLive()
  const candleUpdate = s.chart.log.update.filter(([n]) => n === 'candle').at(-1)[1]
  assert.deepEqual(candleUpdate, { time: T, open: 100, high: 102.2, low: 99, close: 102.2 })    // open from the broker's candle, never from a quote
  s.answers.live.push(livePayload(quote(T + 61, 101.5, 101.9)))                               // next minute: rollover
  await s.mk.refreshLive()
  assert.deepEqual(s.chart.log.update.filter(([n]) => n === 'candle').at(-1)[1], { time: T + 60, open: 101.5, high: 101.5, low: 101.5, close: 101.5 })
  assert.equal(s.chart.log.setData.length, setDataBefore)                                     // never redrawn from scratch...
  assert.equal(s.chart.log.fit, fitBefore)                                                    // ...and the user's zoom is untouched
  assert.equal(s.dom.nodes.chartLiveTag.textContent, 'LIVE')
  assert.equal(s.dom.nodes['ph-price'].textContent, '101.5')
  const stats = s.mk.liveStats()
  assert.equal(stats.mode, 'polling'); assert.equal(stats.rolled, 1); assert.equal(stats.applied, 3)
  assert.equal(stats.quote_to_chart_ms.max, 2000)                                            // drawn 2 s after the broker's tick time (fake clock)
})

test('stale, duplicate and out-of-order quotes are ignored; unverified price side leaves candles to the snapshots', async () => {
  const s = setup()
  s.answers.live.push(livePayload(quote(T + 30, 101, 101.4)))
  s.mk.showChart(s.chart.refs); s.mk.setInterval('M1'); s.mk.setInstrument('US30'); await settle(); await settle()
  const updates = () => s.chart.log.update.length
  const before = updates()
  s.answers.live.push(livePayload(quote(T + 30, 101, 101.4)))                                // same tick again (e.g. shared by another tab)
  s.answers.live.push(livePayload(quote(T + 12, 99, 99.4)))                                  // older tick arriving late
  await s.mk.refreshLive(); await s.mk.refreshLive()
  assert.equal(updates(), before)
  assert.equal(s.mk.liveStats().duplicates, 1); assert.equal(s.mk.liveStats().out_of_order, 1)
  s.answers.live.push(livePayload(quote(T + 40, 103, 103.4), { price_basis: { side: 'unverified' } }))
  await s.mk.refreshLive()
  assert.equal(updates(), before)
  assert.equal(s.dom.nodes.chartLiveTag.textContent, 'VERIFYING')
  s.answers.live.push(livePayload(quote(T + 50, 103, 103.4), { status: 'market_closed' }))
  await s.mk.refreshLive()
  assert.equal(updates(), before); assert.equal(s.dom.nodes.chartLiveTag.textContent, 'CLOSED')
  s.answers.live.push(livePayload(quote(T + 55, 103, 103.4), { status: 'stale' }))
  await s.mk.refreshLive()
  assert.equal(s.dom.nodes.chartLiveTag.textContent, 'DELAYED')
})

test('disconnect shows OFFLINE; reconnect reloads candles in place (backfill) before applying the new tick', async () => {
  const s = setup()
  s.answers.live.push(livePayload(quote(T + 10, 101, 101.4)))
  s.mk.showChart(s.chart.refs); s.mk.setInterval('M1'); s.mk.setInstrument('US30'); await settle(); await settle()
  s.answers.live.push(new Error('network'), new Error('network'))
  await s.mk.refreshLive(); await s.mk.refreshLive()
  assert.equal(s.dom.nodes.chartLiveTag.textContent, 'OFFLINE')
  const loads = s.calls.candles.length, cleared = s.chart.log.setData.filter(([, n]) => n === 0).length
  s.clock.t = (T + 200) * 1000
  s.answers.candles = () => ({ interval: 'M1', status: 'fresh', candles: bars(T - 360, 9), last_success_at: new Date((T + 199) * 1000).toISOString() })   // backfilled to T+120
  s.answers.live.push(livePayload(quote(T + 190, 104, 104.4)))
  await s.mk.refreshLive(); await settle(); await settle()
  assert.equal(s.calls.candles.length, loads + 1)                                             // candles re-read after reconnecting
  assert.equal(s.chart.log.setData.filter(([, n]) => n === 0).length, cleared)                // chart not cleared
  assert.equal(s.mk.liveStats().backfills >= 1, true)
  assert.deepEqual(s.chart.log.update.filter(([n]) => n === 'candle').at(-1)[1].time, T + 180)  // the waiting tick lands after the backfill
  assert.equal(s.dom.nodes.chartLiveTag.textContent, 'LIVE')
})

test('missing or ambiguous US30 is shown and nothing is charted or subscribed', async () => {
  const s = setup()
  s.answers.live.push({ status: 'unavailable', resolution: { status: 'ambiguous', candidates: [{ symbol: 'US30' }, { symbol: 'US30.f' }] }, detail: 'This account offers several instruments that could be US30: US30, US30.f. None was picked.' })
  s.mk.showChart(s.chart.refs); await settle()
  const candleLoads = s.calls.candles.length
  s.mk.setInstrument('US30'); await settle()
  assert.equal(s.calls.candles.length, candleLoads)
  assert.deepEqual(s.states.at(-1), ['US30 not available', 'This account offers several instruments that could be US30: US30, US30.f. None was picked.'])
  assert.equal(s.dom.nodes.chartLiveTag.textContent, 'UNAVAILABLE')
})

test('switching accounts drops the old account\'s quotes, even an answer still in flight', async () => {
  const s = setup()
  s.answers.live.push(livePayload(quote(T + 10, 101, 101.4)))
  s.mk.showChart(s.chart.refs); s.mk.setInterval('M1'); s.mk.setInstrument('US30'); await settle(); await settle()
  s.answers.live.push(livePayload(quote(T + 20, 999, 999.4)))
  const pending = s.mk.refreshLive()
  s.setAccount('mt:2'); s.mk.onContextChange()                                               // switched while the read was in flight
  await pending
  assert.ok(!s.chart.log.update.some(([, c]) => c.close === 999 || c.value === 999))
  assert.equal(s.mk.liveStats().applied, 0)
  s.answers.live.push(livePayload(quote(T + 25, 50, 50.4), { resolution: { status: 'resolved', symbol: { symbol: 'US30.cash' } } }))
  await s.mk.refreshLive(); await settle(); await settle()
  assert.deepEqual(s.calls.live.at(-1), [2, { instrument: 'US30' }])                         // the new account, its own resolution
  assert.deepEqual(s.calls.candles.at(-1), { interval: 'M1', symbol: 'US30.cash' })
})

test('tabs share one poll: a tab that just received another tab\'s answer does not ask again', async () => {
  const listeners = []
  const bus = { open: () => { const ch = { onmessage: null, postMessage: (m) => listeners.filter((l) => l !== ch).forEach((l) => l.onmessage && l.onmessage({ data: plain(m) })), close() {} }; listeners.push(ch); return ch } }
  const clock = { t: (T + 5) * 1000 }
  const a = setup({ bus, now: clock }), b = setup({ bus, now: clock })
  a.answers.live.push(livePayload(quote(T + 5, 101, 101.4)))
  b.answers.live.push(livePayload(quote(T + 5, 101, 101.4)))
  for (const t of [a, b]) { t.mk.showChart(t.chart.refs); t.mk.setInterval('M1'); t.mk.setInstrument('US30') }
  await settle(); await settle()
  const before = a.calls.live.length + b.calls.live.length
  a.answers.live.push(livePayload(quote(T + 8, 102, 102.4)))
  clock.t = (T + 9) * 1000
  await a.mk.refreshLive(); await settle()                                                    // tab A polls and shares
  assert.equal(b.mk.liveStats().shared >= 1, true)
  assert.deepEqual(b.chart.log.update.filter(([n]) => n === 'candle').at(-1)[1].close, 102)  // B drew A's tick
  // B's own timer fires within the same interval: it skips the request.
  const bPolls = b.mk.liveStats().polls
  b.fireLiveTimer(); await settle()
  assert.equal(b.calls.live.length, 1)
  assert.equal(a.calls.live.length + b.calls.live.length, before + 1)
  assert.equal(b.mk.liveStats().polls, bPolls)
  // Once the shared answer is older than one interval, B polls itself (A's tab may have closed).
  clock.t += core.LIVE_POLL_MS
  b.answers.live.push(livePayload(quote(T + 11, 102.5, 102.9)))
  b.fireLiveTimer(); await settle()
  assert.equal(b.calls.live.length, 2)
})
