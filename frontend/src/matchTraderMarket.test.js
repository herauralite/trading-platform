// Market ticker / chart helpers for the served dashboard (public/tali-matchtrader-market.js).
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
const plain = (v) => JSON.parse(JSON.stringify(v))
const bar = (t, o, h, l, c, ticks) => ({ t, o: String(o), h: String(h), l: String(l), c: String(c), ticks })

test('candles: epoch milliseconds become UTC seconds, sorted, one per opening time, incomplete ones dropped', () => {
  const series = plain(core.toSeries([bar(1794600300000, 1.2, 1.3, 1.1, 1.25, 7), bar(1794600000000, 1.1, 1.2, 1.0, 1.15, 5), bar(1794600300000, 1.2, 1.4, 1.1, 1.35, 9),
    { t: 1794600600000, o: '1.3', h: '1.4', l: '1.2' }, { t: 'x', o: '1', h: '1', l: '1', c: '1' }, null,
    { t: 1794600900000, o: null, h: '1.4', l: '1.2', c: '1.3' }, { t: null, o: '1', h: '1', l: '1', c: '1' }]))   // a null price or time is missing, never zero
  assert.deepEqual(series, [{ time: 1794600000, open: 1.1, high: 1.2, low: 1, close: 1.15, ticks: 5 }, { time: 1794600300, open: 1.2, high: 1.4, low: 1.1, close: 1.35, ticks: 9 }])
  assert.deepEqual(plain(core.toLine(core.toSeries([bar(2000, 1, 2, 0.5, 1.5)]))), [{ time: 2, value: 1.5 }])     // line mode = candle closes
  assert.deepEqual(plain(core.toSeries(undefined)), [])
})

test('refresh decides between leaving the chart alone, updating the tail in place, and redrawing', () => {
  const s = (rows) => core.toSeries(rows.map((r) => bar(r[0] * 1000, r[1], r[1] + 1, r[1] - 1, r[2])))
  const shown = s([[100, 10, 10.5], [200, 11, 11.5], [300, 12, 12.2]])
  assert.equal(core.diffSeries(shown, s([[200, 11, 11.5], [300, 12, 12.2]])), 'same')                 // nothing new: do not touch the chart
  assert.equal(core.diffSeries(shown, s([[200, 11, 11.5], [300, 12, 12.9]])), 'tail')                 // forming candle moved
  assert.equal(core.diffSeries(shown, s([[300, 12, 12.2], [400, 13, 13.1]])), 'tail')                 // a new candle opened
  assert.equal(core.diffSeries(shown, s([[200, 11, 99], [300, 12, 12.2]])), 'reset')                  // an older candle was corrected
  assert.equal(core.diffSeries(shown, s([[900, 1, 1]])), 'reset')                                     // no overlap: another window
  assert.equal(core.diffSeries([], s([[100, 1, 1]])), 'reset')
  assert.equal(core.diffSeries([], []), 'same')
})

test('prices keep the digits the provider sent and are padded to the instrument precision, never rounded', () => {
  assert.equal(core.formatPrice('1.085', 5), '1.08500')
  assert.equal(core.formatPrice('151.2', 3), '151.200')
  assert.equal(core.formatPrice('4100', 2), '4100.00')
  assert.equal(core.formatPrice('1.085123', 5), '1.085123')            // more digits than the precision: shown as sent
  assert.equal(core.formatPrice('67450', 0), '67450')
  assert.equal(core.formatPrice('1.5', null), '1.5')
  for (const bad of [null, undefined, '', 'abc', '1e5', NaN]) assert.equal(core.formatPrice(bad, 2), '—')
})

test('ticker view: bid / ask with its basis, honest states, no invented change', () => {
  const item = (extra = {}) => ({ symbol: 'EURUSD.x', alias: 'EURUSD', bid: '1.08512', ask: '1.08519', price_precision: 5, session_open: true, state: 'fresh', ...extra })
  const fresh = core.tickerView({ status: 'fresh', poll_interval_seconds: 15, items: [item()] })
  assert.deepEqual(plain(fresh.items), [{ label: 'EURUSD', price: '1.08512 / 1.08519', closed: false, stale: false }])
  assert.equal(fresh.note, 'Bid / Ask · auto-updating about every 15 s')
  assert.ok(!JSON.stringify(fresh).includes('%'))
  const closed = core.tickerView({ status: 'market_closed', items: [item({ state: 'market_closed', session_open: false })] })
  assert.equal(closed.items[0].closed, true)
  assert.match(closed.note, /market closed · last published prices/)
  assert.equal(core.tickerView({ status: 'stale', items: [item({ state: 'stale' })] }).note, 'Bid / Ask · delayed')
  assert.equal(core.tickerView({ status: 'permission_denied', items: [] }).message, 'Market data is not permitted for this API token')
  assert.equal(core.tickerView({ status: 'loading', items: [] }).message, 'Loading prices…')
  assert.equal(core.tickerView({ status: 'unavailable', items: [] }).message, 'Prices unavailable right now')
  assert.equal(core.tickerView(null).message, 'Loading prices…')
  assert.equal(core.tickerView({ status: 'fresh', items: [item({ bid: null })] }).items[0].price, '— / 1.08519')   // a missing side is shown as missing
  assert.equal(core.tickerView({ status: 'fresh', items: [item({ alias: null })] }).items[0].label, 'EURUSD.x')    // provider symbol when there is no alias
})

test('chart status text distinguishes a closed market from a delayed or failing feed', () => {
  const w = { last_open_time: '2026-11-13T20:00:00Z' }
  assert.equal(core.chartStatusText({ status: 'fresh', poll_interval_seconds: 60, window: w }), 'Auto-updating about every 60 s · last candle opened Nov 13, 20:00 UTC.')
  assert.match(core.chartStatusText({ status: 'market_closed', window: w }), /^Market closed · last candle opened Nov 13, 20:00 UTC\. No new prices until the session opens\.$/)
  assert.match(core.chartStatusText({ status: 'stale', window: w, detail: 'Prices have not refreshed recently.' }), /^Delayed/)
  assert.equal(core.chartStatusText({ status: 'permission_denied', detail: 'Not allowed.' }), 'Not allowed.')
  assert.equal(core.utcTime('nonsense'), '')
})

test('served dashboard omits market Charts, the order ticket and market polling', () => {
  assert.doesNotMatch(appHtml, /data-page="chart"|id="page-chart"|id="orderPanel"|id="depthCard"|id="tickerInner"|tali-matchtrader-market\.js/)
  assert.match(appHtml, /id="curveLabel">Equity Curve/)
  for (const forbidden of ['innerHTML', 'localStorage.setItem', 'WebSocket', 'console.']) assert.ok(!src.includes(forbidden), forbidden)
  assert.ok(!/https?:\/\//.test(src))                                           // no third-party price source
})
