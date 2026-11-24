// The extension's real poll() against the backend's closed-trade alert responses: the queue of
// pending close notifications is cleared only by a 2xx and kept on 503 / network errors.
// Runs the actual content.js in a VM; only the page scrapers and browser APIs are stubbed.
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'

const SOURCE = fs.readFileSync(new URL('./content.js', import.meta.url), 'utf8')

function loadExtension(respond) {
  const dataRequests = []
  const sandbox = {
    console: { log() {}, warn() {}, error() {}, info() {} },
    setInterval: () => 0, setTimeout: () => 0, clearInterval() {}, clearTimeout() {},
    URLSearchParams,
    window: { location: { href: 'https://app.fundingpips.com/dashboard' } },
    document: { body: { innerText: '', click() {} }, querySelectorAll: () => [], querySelector: () => null },
    chrome: {
      storage: {
        local: { get: async () => ({ tali_ingest_token: 'tti_test', tali_session_token: 'session', tali_telegram_uid: '5550001' }) },
        onChanged: { addListener() {} },
      },
    },
    fetch: async (url, init) => {
      if (!String(url).endsWith('/extension/data')) return { ok: true, status: 200, json: async () => ({}) }
      const body = JSON.parse(init.body)
      dataRequests.push(body)
      const next = await respond(dataRequests.length, body)
      if (next instanceof Error) throw next
      return { ok: next.status >= 200 && next.status < 300, status: next.status, json: async () => next.body }
    },
  }
  vm.createContext(sandbox)
  vm.runInContext(SOURCE, sandbox, { filename: 'content.js' })
  // Replace only the FundingPips page readers with a fixed account and empty page state.
  vm.runInContext(`
    detectAccountConfig = () => ({ accountId: 'FP-1000001', accountType: '2_step_phase1', accountSize: 100000,
      accountLabel: 'FP 100K', isMaster: false, trailingOverall: false, limits: { dailyLoss: 5000, overallLoss: 10000 } });
    extractData = () => ({ balance: 100250, equity: 100180, profit: -70, hasPositions: false, openPositionCount: 0, positions: [] });
    detectTradeEvents = () => {};
    checkRules = () => [];
    calcTradeRisk = () => null;
    ensureAccountLinked = () => {};
  `, sandbox)
  return {
    sent: () => dataRequests.map((b) => b.closedTrades.map((c) => c.symbol)),
    queue: () => JSON.parse(vm.runInContext('JSON.stringify(state.pendingNotifications.map((n) => n.symbol))', sandbox)),
    enqueue: (n) => vm.runInContext(`state.pendingNotifications.push(${JSON.stringify(n)})`, sandbox),
    poll: () => vm.runInContext('poll()', sandbox),
  }
}

const scripted = (...replies) => async () => replies.shift() ?? ok()
const close = (symbol, pnl) => ({ accountId: 'FP-1000001', symbol, direction: 'Buy', volume: 1, openPrice: 1, pnl, balanceAfter: 100000 + pnl })
// Bodies exactly as the backend now answers POST /extension/data.
const ok = (rejected = 0) => ({ status: 200, body: { status: 'ok', telegramLinked: true, rejected_closed_trades: rejected } })
const unavailable = (undelivered, rejected = 0) => ({
  status: 503, body: { detail: 'Telegram delivery failed; resend to retry', retryable: true, undelivered_closed_trades: undelivered, rejected_closed_trades: rejected },
})

test('the poll loop is live in this harness (the boot poll did not send without an account)', async () => {
  const ext = loadExtension(scripted())
  await new Promise((r) => setImmediate(r))
  assert.deepEqual(ext.sent(), [])
})

test('a permanent rejection answered with 200 clears the queue: the alert is not resent', async () => {
  const ext = loadExtension(scripted(ok(1)))
  ext.enqueue(close('US30', 50))
  await ext.poll()
  assert.deepEqual(ext.queue(), [])
  await ext.poll()
  assert.deepEqual(ext.sent(), [['US30'], []])
})

test('a transient failure (503) keeps the alert queued; the next poll resends it and a 200 clears it', async () => {
  const ext = loadExtension(scripted(unavailable(1)))
  ext.enqueue(close('US30', 50))
  await ext.poll()
  assert.deepEqual(ext.queue(), ['US30'])
  await ext.poll()
  assert.deepEqual(ext.queue(), [])
  await ext.poll()
  assert.deepEqual(ext.sent(), [['US30'], ['US30'], []])
})

test('a network error keeps the queue as well', async () => {
  const ext = loadExtension(scripted(new TypeError('Failed to fetch')))
  ext.enqueue(close('US30', 50))
  await ext.poll()
  assert.deepEqual(ext.queue(), ['US30'])
  await ext.poll()
  assert.deepEqual(ext.queue(), [])
})

test('a mixed batch answered 503 is resent whole, then cleared by the 200 that follows', async () => {
  // The server settles the delivered and the rejected alert with receipts, so only the transient one is
  // attempted again on the resend (covered by the backend tests); the extension simply resends all three.
  const ext = loadExtension(scripted(unavailable(1, 1)))
  ext.enqueue(close('US30', 50))
  ext.enqueue(close('XAUUSD', -20))
  ext.enqueue(close('GER40', 10))
  await ext.poll()
  assert.deepEqual(ext.queue(), ['US30', 'XAUUSD', 'GER40'])
  await ext.poll()
  assert.deepEqual(ext.queue(), [])
  await ext.poll()
  assert.deepEqual(ext.sent(), [['US30', 'XAUUSD', 'GER40'], ['US30', 'XAUUSD', 'GER40'], []])
})

test('a close queued while a request is in flight survives that request being acknowledged', async () => {
  let release
  const gate = new Promise((resolve) => { release = resolve })
  const ext = loadExtension(async (n) => { if (n === 1) await gate; return ok() })
  ext.enqueue(close('US30', 50))
  const inFlight = ext.poll()
  for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r))   // let poll reach the fetch
  ext.enqueue(close('NAS100', -5))
  release()
  await inFlight
  assert.deepEqual(ext.queue(), ['NAS100'])
  assert.deepEqual(ext.sent(), [['US30']])
})
