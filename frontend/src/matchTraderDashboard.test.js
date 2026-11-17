// Match-Trader API accounts in the served dashboard (public/app.html + two public scripts).
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import vm from 'node:vm'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const pub = path.resolve(here, '../public')
const apiSrc = fs.readFileSync(path.join(pub, 'tali-matchtrader-api.js'), 'utf8')
const bridgeSrc = fs.readFileSync(path.join(pub, 'tali-matchtrader-dashboard.js'), 'utf8')
const appHtml = fs.readFileSync(path.join(pub, 'app.html'), 'utf8')
const BROKER_TOKEN = 'broker-token-SECRET-0123456789-abcdefghij'
const NOW = Date.parse('2026-11-14T22:00:30Z')
const READY = { secure_storage: true, worker: true, connect_enabled: true, accepting_connections: true }
const req = (state, extra = {}) => ({ id: 1, state, detail: null, resources: {}, pending: state === 'queued' || state === 'running', requested_at: '2026-11-14T22:00:00Z', started_at: null, finished_at: null, ...extra })
const settle = async () => { for (let i = 0; i < 25; i++) await new Promise((r) => setTimeout(r, 0)) }

// ── tiny fake DOM: only what the bridge touches ──
function makeDocument() {
  const byId = new Map()
  function node(tag) {
    const n = {
      tagName: tag, children: [], parentNode: null, style: { cssText: '', display: '' }, className: '', value: '', _id: '',
      _text: '',
      get textContent() { return this.children.length ? this.children.map((c) => c.textContent).join('') : this._text },
      set textContent(v) { this.children = []; this._text = String(v) },
      set innerHTML(_v) { throw new Error('innerHTML must not be used for provider data') },
      get id() { return this._id },
      set id(v) { this._id = v; byId.set(v, this) },
      get firstChild() { return this.children[0] || null },
      get nextSibling() { const s = this.parentNode ? this.parentNode.children : []; return s[s.indexOf(this) + 1] || null },
      appendChild(c) { c.parentNode = this; this.children.push(c); return c },
      removeChild(c) { this.children = this.children.filter((x) => x !== c); c.parentNode = null; return c },
      insertBefore(c, ref) { c.parentNode = this; const i = ref ? this.children.indexOf(ref) : -1; if (i < 0) this.children.push(c); else this.children.splice(i, 0, c); return c },
      attrs: {}, listeners: {},
      setAttribute(k, v) { this.attrs[k] = String(v) }, getAttribute(k) { return k in this.attrs ? this.attrs[k] : null },
      addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn) },
      click() { (this.listeners.click || []).forEach((fn) => fn()) },
      querySelectorAll(sel) {
        const m = /^\[([a-z-]+)(?:="([^"]*)")?\]$/.exec(sel); const out = []
        const walk = (n) => n.children.forEach((c) => { if (m && c.getAttribute(m[1]) !== null && (m[2] === undefined || c.getAttribute(m[1]) === m[2])) out.push(c); walk(c) })
        walk(this); return out
      },
    }
    return n
  }
  const doc = { createElement: node, getElementById: (id) => byId.get(id) || null, querySelectorAll: (sel) => root.querySelectorAll(sel) }
  const root = node('div')
  for (const id of ['overviewCards', 'journalEntries', 'journalMeta', 'mtApiTokenInput']) { const n = node('div'); n.id = id; root.appendChild(n) }
  return doc
}

function card(id, extra = {}) {
  return { id, account_key: `mtapi:${id}`, connector_type: 'matchtrader_api', broker_name: 'Broker One', display_label: 'Broker One · …0001', masked_login: '…0001',
    currency: 'USD', provider_account_type: 'DEMO', status: 'active', display_state: 'ready', balance: '98000.5', last_synced_at: '2026-11-14T22:00:00Z', ...extra }
}
const fresh = (at = '2026-11-14T22:00:00Z', status = 'fresh') => ({ status, last_success_at: at, last_attempt_at: at, last_error: null, is_stale: status === 'stale' || status === 'failed' })
function snapshot(id, extra = {}) {
  return {
    connection: card(id), summary: { balance: '98000.5', equity: '97990.25', free_funds: '97990.25', margin: '0', profit: '-10.25', credit: '0', margin_level: null, fetched_at: '2026-11-14T22:00:00Z' },
    positions: [], pending_orders: [],
    freshness: { summary: fresh(), positions: fresh(), pending_orders: fresh(), closed_positions: fresh(), ledgers: fresh() },
    history: { coverage_status: 'complete_for_requested_range', requested_from: '2026-11-07T22:00:00Z', requested_to: '2026-11-14T22:00:00Z', queued_intervals: 0, gap_count: 0, closed_entry_count: 4,
      period_stats: { net_profit: '-12.5', closed_entries: 4, positions: 3, winning_entries: 1, losing_entries: 3, first_close_time: '2026-11-08T10:00:00Z', last_close_time: '2026-11-14T10:00:00Z', is_complete_for_requested_range: true } },
    background_refresh: { enabled: true, live_interval_seconds: 30, history_interval_seconds: 120 }, server_time: '2026-11-14T22:00:30Z', ...extra,
  }
}
const trade = (uid, extra = {}) => ({ entry_key: `mtapi:7:${uid}`, uid, position_id: 'P1', symbol: 'US30', alias: 'US30', side: 'BUY', volume: '0.1', open_price: '39000', close_price: '39050.5',
  open_time: '2026-11-14T09:00:00Z', close_time: '2026-11-14T10:00:00Z', profit: '11', commission: '-0.5', swap: '0', net_profit: '10.5', close_reason: 'USER', ...extra })

function load({ session = 'session-1' } = {}) {
  const calls = []
  const routes = []
  const env = { session, active: '', timers: [], delays: [], syncStates: [], flow: { step: 2, path: 'matchtraderApi', loading: false, error: '', success: '' }, log: [], accounts: {}, sync: {}, history: { filter: 'all', offset: 0, rows: [], hasMore: true, loading: false, error: null, accountId: '', reqId: 0 } }
  const document = makeDocument()
  const window = { document, localStorage: { getItem: () => env.session }, TaliApiBase: { resolveApiBase: () => 'http://localhost:8000' } }
  vm.runInNewContext(apiSrc, { window, Intl, Date, JSON, Number, String, Object, Promise, Error, encodeURIComponent, Math, Array })
  vm.runInNewContext(bridgeSrc, { window, Date, JSON, Number, String, Object, Promise, Error, Math, Array, AbortController })
  const respond = (status, body) => ({ ok: status >= 200 && status < 300, status, text: async () => (body === undefined ? '' : JSON.stringify(body)) })
  const deps = {
    fetch: async (url, init) => {
      const call = { url, method: init.method, headers: init.headers, body: init.body ? JSON.parse(init.body) : undefined }
      calls.push(call)
      for (const [match, handler] of routes) if (match(call)) return handler(call, respond)
      return respond(404, { detail: 'Not Found' })
    },
    getBaseApiUrl: () => 'http://localhost:8000', getSessionToken: () => env.session, document, now: () => NOW,
    setInterval: (fn, ms) => { env.timers.push({ fn, ms }); return env.timers.length }, clearInterval: (id) => { if (env.timers[id - 1]) env.timers[id - 1].cleared = true },
    delay: async (ms) => { env.delays.push(ms) },                            // sync-status polling waits; instant in tests
  }
  const bridge = window.TaliMatchTrader.createBridge(deps)
  const applied = []
  bridge.attach({
    getActiveAccount: () => env.active, historyState: env.history,
    rehydrateAccounts: () => { bridge.merge(env.accounts); env.log.push('rehydrate') },
    getAddFlowState: () => env.flow, renderAddAccountFlow: () => { document.getElementById('mtApiTokenInput').value = ''; env.log.push('renderFlow') },
    openAddAccountFlow: () => env.log.push('openFlow'), closeAddAccountFlow: () => env.log.push('closeFlow'),
    renderHistoryPanel: () => env.log.push('renderHistory'), reloadHistory: () => env.log.push('reloadHistory'), rerenderAccountSheet: () => env.log.push('sheet'),
    setManualSyncState: (id, next) => { env.sync[id] = next; env.syncStates.push(`${next.status}/${next.label}`) }, showAccountActionStatus: (message, tone) => env.log.push(['status', tone, message]),
    setTopStatus: (text) => { env.top = text }, activateAccount: (key) => { env.active = key; env.log.push(['activate', key]) },
    afterDisconnect: (key) => env.log.push(['afterDisconnect', key]), applyApiState: (view) => applied.push(view),
  })
  // a route written without a query also matches the same path with one (the list is requested as ?view=cards)
  const on = (method, suffix, handler) => routes.push([(c) => c.method === method && (c.url.endsWith(suffix) || (!suffix.includes('?') && c.url.split('?')[0].endsWith(suffix))), handler])
  return { core: window.TaliMatchTraderCore, api: window.TaliMatchTraderApi, bridge, env, calls, on, respond, applied, document, deps }
}

// ── formatting and derived state ──
test('money formatting keeps sign, currency and zero; unknown stays unknown', () => {
  const { core } = load()
  assert.equal(core.formatMoney('12345.67', 'USD'), '$12,345.67')
  assert.equal(core.formatMoney('-10.25', 'USD'), '-$10.25')
  assert.equal(core.formatMoney('0', 'USD'), '$0.00')
  assert.equal(core.formatMoney('12.5', 'USD', { signed: true }), '+$12.50')
  assert.equal(core.formatMoney('-12.5', 'USD', { signed: true }), '-$12.50')
  assert.equal(core.formatMoney('0', 'USD', { signed: true }), '$0.00')
  assert.equal(core.formatMoney('1500', 'EUR'), '€1,500.00')
  for (const unknown of [null, undefined, '', 'abc']) assert.equal(core.formatMoney(unknown, 'USD'), '—')
})

test('a synced snapshot maps to the dashboard cards without invented values', () => {
  const { core } = load()
  const view = core.deriveDisplayState(snapshot(7), NOW)
  assert.equal(view.overviewStatus.balanceText, '$98,000.50')
  assert.equal(view.overviewStatus.equityText, '$97,990.25')
  assert.match(view.overviewStatus.equitySub, /floating -\$10\.25/)
  assert.equal(view.overviewStatus.positionsVal, '0 open')          // fetched and empty
  assert.equal(view.overviewStatus.syncVal, 'Synced')
  assert.match(view.overviewStatus.syncSub, /Updated 30s ago · Broker One API/)   // age from the server timestamp, not the browser fetch
  assert.equal(view.analytics.overallPnlText, '-$12.50')
  assert.match(view.analytics.overallSub, /^Net of fees · closed trades from Nov 8 · history imported from Nov 7$/)   // first close vs. where the import starts
  assert.equal(view.analytics.winRate, 25)
  assert.equal(view.analytics.winRateSub, '1 of 4 closed entries')
  assert.equal(view.analytics.dailyPnl, null)
  assert.equal(view.payout.progressVal, '—')
  assert.deepEqual({ ...view.tiles }, { wr: '25%', netpl: '-$12.50', totalTrades: '4', pf: '—' })
  assert.ok(!JSON.stringify(view).includes('10000') && !JSON.stringify(view).includes('10,000'))   // no default account size anywhere
})

test('unknown, stale, failed and reconnect states are shown as such', () => {
  const { core } = load()
  const never = { status: 'never_synced', last_success_at: null, last_attempt_at: null, last_error: null, is_stale: false }
  const pending = core.deriveDisplayState(snapshot(7, { summary: null, positions: null, pending_orders: null, freshness: { summary: never, positions: never, pending_orders: never, closed_positions: never, ledgers: never },
    history: { coverage_status: 'pending', closed_entry_count: null, period_stats: null } }), NOW)
  assert.equal(pending.overviewStatus.balanceText, '—')
  assert.equal(pending.overviewStatus.balance, null)
  assert.equal(pending.overviewStatus.positionsVal, '—')
  assert.equal(pending.overviewStatus.positionsSub, 'Positions unavailable')      // not "0 open"
  assert.equal(pending.overviewStatus.syncSub, 'First sync pending')
  assert.deepEqual({ ...pending.tiles }, { wr: '—', netpl: '—', totalTrades: '—', pf: '—' })

  const failedFirst = core.deriveDisplayState(snapshot(7, { summary: null, freshness: { ...snapshot(7).freshness, summary: { ...never, status: 'failed', last_error: 'transient', is_stale: true } } }), NOW)
  assert.match(failedFirst.overviewStatus.syncSub, /First sync failed \(transient\)/)

  const stale = core.deriveDisplayState(snapshot(7, { freshness: { ...snapshot(7).freshness, summary: fresh('2026-11-14T21:40:00Z', 'stale'), positions: fresh('2026-11-14T21:40:00Z', 'stale') } }), NOW)
  assert.equal(stale.overviewStatus.syncVal, 'Delayed')
  assert.match(stale.overviewStatus.syncSub, /Last synced 21m ago/)
  assert.match(stale.overviewStatus.balanceSub, /delayed/)                         // old value kept, visibly stale
  assert.equal(stale.overviewStatus.balanceText, '$98,000.50')
  assert.match(stale.overviewStatus.positionsSub, /delayed/)

  const reauth = core.deriveDisplayState(snapshot(7, { connection: card(7, { status: 'needs_reauth' }) }), NOW)
  assert.equal(reauth.overviewStatus.syncVal, 'Reconnect needed')

  const importing = core.deriveDisplayState(snapshot(7, { history: { ...snapshot(7).history, coverage_status: 'in_progress' } }), NOW)
  assert.match(importing.analytics.overallSub, /still importing/)
  assert.match(importing.analytics.winRateSub, /still importing/)
  const gaps = core.deriveDisplayState(snapshot(7, { history: { ...snapshot(7).history, coverage_status: 'partial_gaps' } }), NOW)
  assert.match(gaps.analytics.overallSub, /incomplete/)

  const negative = core.deriveDisplayState(snapshot(7, { summary: { ...snapshot(7).summary, balance: '-250.75', equity: '0' } }), NOW)
  assert.equal(negative.overviewStatus.balanceText, '-$250.75')
  assert.equal(negative.overviewStatus.equityText, '$0.00')
  const off = core.deriveDisplayState(snapshot(7, { background_refresh: { enabled: false } }), NOW)
  assert.match(off.overviewStatus.syncSub, /background refresh is off/)
})

test('account merge keeps legacy accounts and never keys an API account by broker login', () => {
  const { core } = load()
  const accounts = { '12340001': { label: '100K Eval', connectorType: 'fundingpips_extension', idx: 1 } }   // legacy card for the same broker login
  core.mergeAccounts(accounts, [card(7), card(8, { status: 'needs_reauth', balance: null }), { id: 9, account_key: 'mtapi:10' }, { id: 5, account_key: '12340001' }, null])
  assert.deepEqual(Object.keys(accounts), ['12340001', 'mtapi:7', 'mtapi:8'])           // mismatched / login-keyed cards are refused
  assert.equal(accounts['12340001'].connectorType, 'fundingpips_extension')              // untouched, no merge of facts
  const api = accounts['mtapi:7']
  assert.equal(api.connectorType, 'matchtrader_api')
  assert.equal(api.apiConnectionId, 7)
  assert.equal(api.displayId, '…0001')
  assert.equal(api.balanceText, '$98,000.50')
  assert.equal(api.accountSize, null)
  assert.equal(api.accountType, '')
  assert.equal(api.idx, 2)
  assert.equal(accounts['mtapi:8'].balanceText, null)
  core.mergeAccounts(accounts, [card(8)])                                                // re-merge replaces only API entries
  assert.deepEqual(Object.keys(accounts), ['12340001', 'mtapi:8'])
  assert.equal(core.connectionIdFromKey('mtapi:12'), 12)
  for (const bad of ['12', 'mtapi:', 'mtapi:1;drop', 'MTAPI:1', null, 12]) assert.equal(core.isApiAccountKey(bad), false)
})

test('imported trades map to history rows without fabricated legacy fields', () => {
  const { core } = load()
  const row = core.tradeToHistoryRow(trade('u1', { side: null, net_profit: '-3' }), 'USD')
  assert.equal(row.direction, '—')
  assert.equal(row.pnl, -3)
  assert.equal(row.pnlText, '-$3.00')
  assert.equal(row.entryKey, 'mtapi:7:u1')
  assert.equal(row.readOnly, true)
  for (const absent of ['balanceAfter', 'accountSize', 'dailyPct', 'id']) assert.ok(!(absent in row), absent)
})

// ── HTTP client ──
test('client uses the current session, sends the token only in the connect body, and sanitises errors', async () => {
  const t = load()
  const client = t.api.create(t.deps)
  t.on('POST', '/connections', (c, r) => r(200, { ok: true, connection: { id: 7, status: 'active' } }))
  t.on('GET', '/connections/7/snapshot', (c, r) => r(200, snapshot(7)))
  t.on('GET', '/connections/8/snapshot', (c, r) => r(500, { detail: { internal: 'stack trace' } }))
  t.on('POST', '/connections/7/sync', (c, r) => r(409, { detail: 'This connection is not active' }))
  await client.connect({ token: BROKER_TOKEN })
  t.env.session = 'session-2'
  await client.getSnapshot(7)
  assert.equal(t.calls[0].headers.Authorization, 'Bearer session-1')
  assert.equal(t.calls[1].headers.Authorization, 'Bearer session-2')       // read at request time, never cached
  assert.deepEqual(t.calls[0].body, { token: BROKER_TOKEN })
  assert.ok(!t.calls[0].url.includes(BROKER_TOKEN) && !JSON.stringify(t.calls[0].headers).includes(BROKER_TOKEN))
  assert.ok(!JSON.stringify(t.calls[1]).includes(BROKER_TOKEN))
  await assert.rejects(client.sync(7), (e) => e.status === 409 && e.message === 'This connection is not active')
  await assert.rejects(client.getSnapshot(8), (e) => e.status === 500 && e.message === 'Request failed (500).')   // no server internals
  for (const bad of ['7/../8', 'abc', -1, 1.5, '7?x=1']) assert.throws(() => client.getSnapshot(bad), (e) => e.code === 'bad_id')
  t.env.session = ''
  await assert.rejects(client.listConnections(), (e) => e.code === 'no_session')
  const offline = t.api.create({ ...t.deps, getSessionToken: () => 's', fetch: async () => { throw new TypeError(`fetch failed for ${BROKER_TOKEN}`) } })
  await assert.rejects(offline.connect({ token: BROKER_TOKEN }), (e) => e.code === 'network' && !e.message.includes(BROKER_TOKEN))
})

// ── bridge ──
test('feature off (404) hides the provider without breaking anything; a network blip keeps known cards', async () => {
  const t = load()
  assert.equal(await t.bridge.loadConnections(), false)
  assert.equal(t.bridge.isAvailable(), false)
  assert.equal(t.bridge.isApiAccount('mtapi:7'), false)
  const u = load()
  u.on('GET', '/connections', (c, r) => r(200, { service: READY, connections: [{ id: 7, card: card(7) }] }))
  await u.bridge.loadConnections()
  assert.equal(u.bridge.isAvailable(), true)
  assert.deepEqual(Object.keys(u.env.accounts), ['mtapi:7'])
  u.deps.fetch = async () => { throw new TypeError('offline') }
  await u.bridge.loadConnections()
  assert.equal(u.bridge.isApiAccount('mtapi:7'), true)
})

test('the token form is offered only when the server reports every part ready; existing accounts stay visible', async () => {
  for (const service of [{ ...READY, worker: false, accepting_connections: false }, { ...READY, connect_enabled: false, accepting_connections: false },
    { ...READY, secure_storage: false, accepting_connections: false }, undefined, { ...READY, accepting_connections: 'yes' }]) {
    const t = load()
    t.on('GET', '/connections', (c, r) => r(200, { service, connections: [{ id: 7, card: card(7) }] }))
    await t.bridge.loadConnections()
    assert.equal(t.bridge.isAvailable(), false)                             // routes answering is not enough
    assert.equal(t.bridge.isApiAccount('mtapi:7'), true)                     // but a connected account is still shown
  }
})

test('token submit: clears the field, binds the account, closes the sheet without waiting for anything else, then follows the queued first sync', async () => {
  const t = load()
  const states = [req('running'), req('completed', { resources: { summary: 'ok', positions: 'ok', pending_orders: 'ok' } })]
  const newCard = card(7, { balance: null, last_synced_at: null, display_state: 'first_sync_pending' })
  let releaseList
  const listGate = new Promise((r) => { releaseList = r })
  t.on('POST', '/connections', (c, r) => r(200, { ok: true, connection: { id: 7, status: 'active' }, card: newCard, sync_request: req('queued'),
    initial_backfill: { days: 7, resources: ['closed_positions', 'ledgers'] }, timings: { verify_seconds: 1.2, store_seconds: 0.4 } }))
  t.on('GET', '/connections/7/sync', (c, r) => r(200, { request: states.shift() || req('completed') }))
  t.on('GET', '/connections/7/snapshot', (c, r) => r(200, snapshot(7)))
  t.on('GET', '/connections', async (c, r) => { await listGate; return r(200, { service: READY, connections: [{ id: 7, card: card(7) }] }) })   // deliberately slow
  const input = t.document.getElementById('mtApiTokenInput')
  input.value = `  ${BROKER_TOKEN}  `
  const pending = t.bridge.submitToken()
  assert.equal(input.value, '')                                           // gone from the page before the request resolves
  await pending
  const paths = () => t.calls.map((c) => `${c.method} ${c.url.replace('http://localhost:8000/connectors/matchtrader-api', '')}`)
  // The sheet is closed and the account selected after ONE request; the slow list read has not even answered yet.
  assert.ok(t.env.log.includes('closeFlow'))
  assert.equal(t.env.active, 'mtapi:7')
  assert.equal(paths()[0], 'POST /connections')
  assert.deepEqual(t.calls[0].body, { token: BROKER_TOKEN })
  assert.deepEqual(Object.keys(t.env.accounts), ['mtapi:7'])               // card came with the connect response
  assert.ok(!paths().some((x) => x.endsWith('/backfill')))                  // the server queued the 7-day import itself
  assert.equal(t.bridge.sheetSubtitle('mtapi:7'), 'Connected · first sync pending')
  const status = t.env.log.find((l) => Array.isArray(l) && l[0] === 'status')
  assert.match(status[2], /first sync is queued/)                          // connected is not announced as synced
  const timings = t.bridge.lastConnectTimings()
  assert.deepEqual(Object.keys(timings).sort(), ['server', 'sheet_closed_ms', 'verify_and_store_ms'])
  assert.deepEqual(Array.from(Object.keys(timings.server)), ['verify_seconds', 'store_seconds'])   // durations only
  assert.equal(t.env.timers.length, 1)                                     // import polling started...
  assert.equal(t.env.timers[0].ms, 10000)
  releaseList()
  await settle()
  assert.equal(paths().filter((x) => x === 'POST /connections/7/sync').length, 0)   // the server already queued it: no second job
  assert.deepEqual(t.env.syncStates, ['syncing/queued', 'syncing/running', 'success/completed'])
  assert.ok(paths().includes('GET /connections/7/snapshot'))               // saved data re-read once it completed
  assert.ok(paths().includes('GET /connections?view=cards'))               // the light list view
  // ...and nothing anywhere still holds the token
  const everything = JSON.stringify([t.env.flow, t.env.log, t.env.accounts, t.env.sync, t.calls.slice(1), timings])
  assert.ok(!everything.includes(BROKER_TOKEN))
  for (const src of [apiSrc, bridgeSrc]) assert.ok(!/(local|session)Storage\.(setItem|\w+\s*=)/.test(src) && !/document\.cookie/.test(src))   // nothing is persisted in the browser
  assert.ok(!bridgeSrc.includes('console.') && !apiSrc.includes('console.'))

  // An older server (no card, no automatic import): the page falls back to one list read and requests the import itself, after closing.
  const old = load()
  old.on('POST', '/connections', (c, r) => r(200, { ok: true, connection: { id: 7, status: 'active' }, sync_request: req('queued') }))
  old.on('POST', '/connections/7/backfill', (c, r) => r(200, { ok: true }))
  old.on('GET', '/connections/7/sync', (c, r) => r(200, { request: req('completed') }))
  old.on('GET', '/connections/7/snapshot', (c, r) => r(200, snapshot(7)))
  old.on('GET', '/connections', (c, r) => r(200, { service: READY, connections: [{ id: 7, card: card(7) }] }))
  old.document.getElementById('mtApiTokenInput').value = BROKER_TOKEN
  await old.bridge.submitToken()
  await settle()
  const backfill = old.calls.find((c) => c.url.endsWith('/backfill'))
  assert.equal(backfill.body.from_ms, NOW - 7 * 86400000)                  // seven days, not all-time
  assert.equal(old.env.active, 'mtapi:7')
})

test('connect that fails or saves a non-active connection is reported accurately', async () => {
  const t = load()
  t.on('POST', '/connections', (c, r) => r(422, { detail: 'Match-Trader rejected this token. Create a new read-only token and try again.' }))
  t.document.getElementById('mtApiTokenInput').value = BROKER_TOKEN
  await t.bridge.submitToken()
  assert.match(t.env.flow.error, /rejected this token/)
  assert.equal(t.env.flow.loading, false)
  assert.equal(t.env.active, '')
  assert.ok(!JSON.stringify(t.env.flow).includes(BROKER_TOKEN))

  const short = load()
  short.document.getElementById('mtApiTokenInput').value = 'short'
  await short.bridge.submitToken()
  assert.equal(short.calls.length, 0)
  assert.match(short.env.flow.error, /read-only API token/)

  const failing = load()
  failing.on('POST', '/connections', (c, r) => r(200, { ok: true, connection: { id: 7, status: 'active' }, sync_request: req('queued') }))
  failing.on('GET', '/connections/7/sync', (c, r) => r(200, { request: req('failed', { detail: 'failed', resources: { summary: 'transient' } }) }))
  failing.on('POST', '/connections/7/backfill', (c, r) => r(200, { ok: true }))
  failing.on('GET', '/connections/7/snapshot', (c, r) => r(200, snapshot(7, { summary: null })))
  failing.on('GET', '/connections', (c, r) => r(200, { service: READY, connections: [{ id: 7, card: card(7, { balance: null, last_synced_at: null, display_state: 'first_sync_pending' }) }] }))
  failing.document.getElementById('mtApiTokenInput').value = BROKER_TOKEN
  await failing.bridge.submitToken()
  await settle()
  assert.deepEqual(failing.env.syncStates, ['syncing/queued', 'failed/failed'])
  assert.match(failing.env.sync['mtapi:7'].message, /^Failed\./)          // connected, but never presented as synced
  assert.equal(failing.bridge.sheetSubtitle('mtapi:7'), 'Connected · first sync pending')
})

test('responses for a previous account or session are never rendered', async () => {
  const t = load()
  let release
  t.on('GET', '/connections', (c, r) => r(200, { service: READY, connections: [{ id: 7, card: card(7) }, { id: 8, card: card(8) }] }))
  t.on('GET', '/connections/7/snapshot', (c, r) => new Promise((resolve) => { release = () => resolve(r(200, snapshot(7))) }))
  t.on('GET', '/connections/8/snapshot', (c, r) => r(200, snapshot(8)))
  await t.bridge.loadConnections()
  t.env.active = 'mtapi:7'
  const slow = t.bridge.refreshSnapshot('mtapi:7', { force: true })
  t.env.active = 'mtapi:8'                                                 // user switches while A is still loading
  t.bridge.onContextChange()
  await t.bridge.refreshSnapshot('mtapi:8', { force: true })
  release()
  await slow
  assert.equal(t.applied.length, 1)
  assert.equal(t.applied[0].accountKey, 'mtapi:8')                         // A never painted over B

  // same account, but the session changed (logout / different user) while loading
  const u = load()
  let releaseU
  u.on('GET', '/connections', (c, r) => r(200, { service: READY, connections: [{ id: 7, card: card(7) }] }))
  u.on('GET', '/connections/7/snapshot', (c, r) => new Promise((resolve) => { releaseU = () => resolve(r(200, snapshot(7))) }))
  await u.bridge.loadConnections()
  u.env.active = 'mtapi:7'
  const inflight = u.bridge.refreshSnapshot('mtapi:7', { force: true })
  u.env.session = ''
  u.bridge.reset()
  releaseU()
  await inflight
  assert.equal(u.applied.length, 0)
  assert.equal(u.bridge.isApiAccount('mtapi:7'), false)                    // cached cards dropped on logout

  // a snapshot whose identity does not match the request is ignored
  const v = load()
  v.on('GET', '/connections', (c, r) => r(200, { service: READY, connections: [{ id: 7, card: card(7) }] }))
  v.on('GET', '/connections/7/snapshot', (c, r) => r(200, snapshot(8)))
  await v.bridge.loadConnections()
  v.env.active = 'mtapi:7'
  await v.bridge.refreshSnapshot('mtapi:7', { force: true })
  assert.equal(v.applied.length, 0)
})

test('status polling reads saved data at a bounded rate and never triggers a broker sync', async () => {
  const t = load()
  t.on('GET', '/connections', (c, r) => r(200, { service: READY, connections: [{ id: 7, card: card(7) }] }))
  t.on('GET', '/connections/7/snapshot', (c, r) => r(200, snapshot(7)))
  await t.bridge.loadConnections()
  t.env.active = 'mtapi:7'
  for (let i = 0; i < 6; i++) await t.bridge.refreshSnapshot('mtapi:7')    // the page calls this every 5 seconds
  assert.equal(t.calls.filter((c) => c.url.endsWith('/snapshot')).length, 1)
  assert.equal(t.calls.filter((c) => c.method !== 'GET').length, 0)
  assert.equal(t.applied.length, 6)                                        // still re-rendered so "updated Xs ago" keeps ageing
})

test('history pages by cursor without duplicates; manual sync is interpreted, not assumed', async () => {
  const t = load()
  t.on('GET', '/connections', (c, r) => r(200, { service: READY, connections: [{ id: 7, card: card(7) }] }))
  t.on('GET', '/trades?limit=20', (c, r) => r(200, { trades: [trade('u3'), trade('u2')], next_cursor: 'CUR1', has_more: true }))
  t.on('GET', '/trades?limit=20&cursor=CUR1', (c, r) => r(200, { trades: [trade('u2'), trade('u1', { symbol: '<img src=x onerror=alert(1)>' })], next_cursor: null, has_more: false }))
  t.on('GET', '/connections/7/snapshot', (c, r) => r(200, snapshot(7)))
  t.on('POST', '/connections/7/sync', (c, r) => r(200, { ok: false, result: { state: 'busy', live_ready: false } }))
  await t.bridge.loadConnections()
  t.env.active = 'mtapi:7'
  await t.bridge.loadHistoryPage(true)
  await t.bridge.loadHistoryPage(false)
  assert.deepEqual(Array.from(t.env.history.rows, (r) => r.entryKey), ['mtapi:7:u3', 'mtapi:7:u2', 'mtapi:7:u1'])   // overlap de-duplicated
  assert.equal(t.env.history.hasMore, false)
  assert.ok(t.calls.every((c) => !c.url.includes('/extension/')))                                           // never the scraper routes

})

test('a refresh is queued, then followed: queued, running, partially updated, completed and failed are distinct', async () => {
  const setup = (sequence, post = (c, r) => r(202, { accepted: true, already_pending: false, request: req('queued') })) => {
    const t = load()
    t.on('GET', '/connections', (c, r) => r(200, { service: READY, connections: [{ id: 7, card: card(7) }] }))
    t.on('GET', '/connections/7/snapshot', (c, r) => r(200, snapshot(7)))
    t.on('POST', '/connections/7/sync', post)
    t.on('GET', '/connections/7/sync', (c, r) => { const next = sequence.length > 1 ? sequence.shift() : sequence[0]; return typeof next === 'number' ? r(next, { detail: 'x' }) : r(200, { request: next }) })
    return t
  }
  const run = async (t) => { await t.bridge.loadConnections(); t.env.active = 'mtapi:7'; return t.bridge.manualSync('mtapi:7') }

  const done = setup([req('queued'), req('running'), req('completed')])
  const view = await run(done)
  assert.deepEqual(done.env.syncStates, ['syncing/queued', 'syncing/queued', 'syncing/queued', 'syncing/running', 'success/completed'])
  assert.equal(view.liveReady, true)
  assert.equal(done.calls.filter((c) => c.method === 'POST' && c.url.endsWith('/sync')).length, 1)      // one request, then only status reads
  assert.ok(done.env.delays.every((ms) => ms === done.core.SYNC_POLL_MS) && done.env.delays.length === 3)
  const order = done.calls.map((c) => `${c.method} ${c.url.split('/connections')[1]}`)
  assert.ok(order.lastIndexOf('GET /7/snapshot') > order.lastIndexOf('GET /7/sync'))                     // saved data re-read only after the final state
  assert.ok(done.env.log.includes('reloadHistory'))

  const partial = setup([req('partial', { resources: { summary: 'ok', positions: 'ok', pending_orders: 'transient' } })])
  await run(partial)
  assert.equal(partial.env.sync['mtapi:7'].label, 'partial')
  assert.match(partial.env.sync['mtapi:7'].message, /^Partially updated\. Could not refresh: pending orders\.$/)

  for (const [detail, text] of [['unauthorized', /Reconnect with a new read-only token/], ['rate_limited', /rate limit/], ['failed', /could not be reached/]]) {
    const failed = setup([req('failed', { detail })])
    const result = await run(failed)
    assert.equal(failed.env.sync['mtapi:7'].status, 'failed')
    assert.match(failed.env.sync['mtapi:7'].message, text)
    assert.equal(result.liveReady, false)
  }
  const cancelled = setup([req('cancelled', { detail: 'connection_changed' })])
  await run(cancelled)
  assert.match(cancelled.env.sync['mtapi:7'].message, /cancelled because the connection changed/)

  // Accepted by the server but never picked up: the page stops waiting and says so. It is not "completed" and not "failed".
  const stuck = setup([req('queued')])
  const waiting = await run(stuck)
  assert.equal(stuck.env.delays.length, stuck.core.SYNC_POLL_MAX)
  assert.equal(stuck.env.sync['mtapi:7'].status, 'idle')
  assert.match(stuck.env.sync['mtapi:7'].message, /Still queued on the server/)
  assert.equal(waiting.liveReady, false)

  // A failed status read is a blip, not a failed sync.
  const blip = setup([500, req('running'), req('completed')])
  await run(blip)
  assert.equal(blip.env.sync['mtapi:7'].label, 'completed')

  // The server refuses (no worker): reported as failed with the server's reason; nothing is polled.
  const refused = setup([req('queued')], (c, r) => r(503, { detail: 'Background sync is not running on this server' }))
  await run(refused)
  assert.equal(refused.env.sync['mtapi:7'].status, 'failed')
  assert.match(refused.env.sync['mtapi:7'].message, /Background sync is not running/)
  assert.equal(refused.calls.filter((c) => c.method === 'GET' && c.url.endsWith('/sync')).length, 0)

  // Logging out (or switching user) stops the follow-up: nothing is written for the old session.
  const left = setup([req('queued')])
  left.deps.delay = async () => { left.env.session = '' }
  await run(left)
  assert.deepEqual(left.env.syncStates, ['syncing/queued', 'syncing/queued'])
  assert.equal(left.calls.filter((c) => c.method === 'GET' && c.url.endsWith('/sync')).length, 0)

  const c = done.core
  assert.equal(c.interpretSyncRequest(null).status, 'failed')              // no request object: never assumed fine
  assert.equal(c.interpretSyncRequest({ state: 'queued' }).pending, true)
  assert.equal(c.interpretSyncRequest({ state: 'completed' }).liveReady, true)
  assert.equal(c.interpretSyncRequest({ state: 'something-new' }).status, 'failed')
})

test('provider text is written as text, positions distinguish unknown from empty, disconnect needs two taps', async () => {
  const t = load()
  assert.ok(!apiSrc.includes('innerHTML') && !bridgeSrc.includes('innerHTML') && !bridgeSrc.includes('insertAdjacentHTML'))
  t.on('GET', '/connections', (c, r) => r(200, { service: READY, connections: [{ id: 7, card: card(7) }] }))
  const evil = '<img src=x onerror=alert(1)>'
  t.on('GET', '/connections/7/snapshot', (c, r) => r(200, snapshot(7, { positions: [{ id: '1', symbol: evil, side: null, volume: '0.1', open_price: '1', net_profit: '-2.5' }], pending_orders: null,
    connection: card(7, { display_label: `${evil} · …0001` }) })))
  t.on('DELETE', '/connections/7', (c, r) => r(200, { ok: true }))
  await t.bridge.loadConnections()
  t.env.active = 'mtapi:7'
  await t.bridge.refreshSnapshot('mtapi:7', { force: true })               // the fake DOM throws if innerHTML is assigned
  const panelText = t.document.getElementById('mtApiPanel').textContent
  assert.ok(panelText.includes(evil))                                      // shown literally, as text
  assert.ok(panelText.includes('side n/a') && panelText.includes('-$2.50 net'))
  assert.ok(panelText.includes('Pending orders unavailable'))              // never fetched: not "No pending orders"
  await t.bridge.disconnect('mtapi:7')
  assert.equal(t.calls.filter((c) => c.method === 'DELETE').length, 0)     // first tap only arms it
  assert.equal(t.bridge.isDisconnectPending('mtapi:7'), true)
  await t.bridge.disconnect('mtapi:7')
  assert.equal(t.calls.filter((c) => c.method === 'DELETE').length, 1)
})

// ── the served page is actually wired ──
test('served dashboard loads the provider scripts once and dispatches before any extension request', () => {
  for (const script of ['/tali-matchtrader-api.js', '/tali-matchtrader-dashboard.js']) assert.equal(appHtml.split(`<script src="${script}"></script>`).length - 1, 1)
  assert.ok(appHtml.indexOf('/tali-api-base.js') < appHtml.indexOf('/tali-matchtrader-api.js'))
  assert.ok(appHtml.indexOf('/tali-matchtrader-api.js') < appHtml.indexOf('/tali-matchtrader-dashboard.js'))
  const body = (name) => { const start = appHtml.indexOf(`function ${name}(`); return appHtml.slice(start, start + 900) }
  for (const [fn, marker] of [['fetchStatus', '/extension/status'], ['loadHistoryPage', '/extension/journal'], ['fetchAllTrades', '/extension/journal'], ['fetchPayout', '/extension/payout']]) {
    const text = body(fn)
    assert.ok(text.includes('TaliMatchTrader.isApiAccount('), fn)
    assert.ok(text.indexOf('TaliMatchTrader.isApiAccount(') < text.indexOf(marker), `${fn} must dispatch before ${marker}`)
  }
  for (const fn of ['fetchJournal', 'fetchAnalytics', 'fetchChallenges', 'setPrimaryAccount', 'deactivateAccount']) assert.ok(body(fn).includes('TaliMatchTrader.isApiAccount('), fn)
  const input = appHtml.match(/<input[^>]*id="mtApiTokenInput"[^>]*>/)[0]
  assert.ok(input.includes('type="password"') && input.includes('autocomplete="off"') && !input.includes('value='))
  assert.ok(appHtml.includes("const ready=!!(window.TaliMatchTrader&&TaliMatchTrader.isAvailable());"))   // card is disabled unless the server reports ready
  assert.ok(appHtml.includes("if(!(window.TaliMatchTrader&&TaliMatchTrader.isAvailable())) return;"))       // and the form cannot be opened either
  assert.ok(appHtml.includes('TaliMatchTrader.merge(ACCOUNTS)') && appHtml.includes('TaliMatchTrader.reset()'))
})

test('the Add Account picker offers one supported integration; other providers are not offered or reachable from it', () => {
  const picker = appHtml.slice(appHtml.indexOf("stepEl.textContent='Step 1"), appHtml.indexOf("footer.innerHTML=`<button class=\"btn btn-ghost rip\" onclick=\"closeAddAccountFlow()\">Cancel</button>`"))
  assert.ok(picker.includes("SUPPORTED_ADD_PATHS.map(brokerCard)"))
  assert.ok(!/brokerCard\('(fundingpips|tradelocker|mt5)'\)/.test(appHtml))                 // no card for another provider anywhere
  assert.match(appHtml, /const SUPPORTED_ADD_PATHS=\['matchtraderApi'\];/)
  assert.match(appHtml, /if\(!SUPPORTED_ADD_PATHS\.includes\(path\)\) return;/)           // the page function itself refuses other paths
  assert.ok(!/Connect FundingPips, TradeLocker, or MetaTrader/.test(appHtml))                // onboarding text no longer advertises them
  const form = appHtml.slice(appHtml.indexOf("addAcctFlowState.path==='matchtraderApi'"), appHtml.indexOf("addAcctFlowState.path==='brokerApi'"))
  assert.ok(!/FundingPips|FundedPips/i.test(form))                                           // generic instructions
  assert.match(form, /Trader API<\/b> enabled/)
})

test('analytics view: stated counting, undefined profit factor, coverage that says when figures may change', () => {
  const t = load()
  const v = t.core.analyticsView({ currency: 'USD', time_zone: 'America/Los_Angeles', positive_day_streak: 2,
    summary: { net_profit: '-3301.3', closed_entries: 21, entries_with_result: 20, entries_without_result: 1, positions: 18, winning_entries: 5, losing_entries: 14,
      breakeven_entries: 1, entry_win_rate_percent: '25.0', profit_factor: '0.41', average_win: '480.12', average_loss: '-407.33' },
    best_symbol: { symbol: 'XAUUSD', net_profit: '812.5' }, cumulative_realized: [{ cumulative_net: '100' }, { cumulative_net: '-3301.3' }],
    coverage: { status: 'in_progress', requested_from: '2026-08-17T00:00:00Z' } })
  assert.equal(v.wr, '25.0%'); assert.equal(v.wrTone, 'txt-red')
  assert.equal(v.wrSub, '5 of 20 closed entries (1 breakeven)')
  assert.equal(v.netpl, '-$3,301.30'); assert.equal(v.netTone, 'txt-red')
  assert.equal(v.wlSub, 'W:5 L:14 BE:1 · 18 positions')
  assert.equal(v.pf, '0.41'); assert.equal(v.avgLoss, '-$407.33'); assert.equal(v.bestSymPnl, '+$812.50 net')
  assert.match(v.coverage, /^Importing older history since Aug 1[67], 2026 · figures may change as it arrives · 1 entry without a reported result not counted$/)
  assert.match(v.curveLabel, /Cumulative realized P&L .*not account equity/)
  assert.deepEqual(Array.from(v.curve), [100, -3301.3])
  assert.equal(v.meta, 'Imported closed entries · America/Los_Angeles days')
  const wins = t.core.analyticsView({ summary: { entries_with_result: 2, winning_entries: 2, closed_entries: 2, profit_factor: null, profit_factor_undefined_reason: 'no_losses', net_profit: '5' }, coverage: { status: 'complete_for_requested_range' } })
  assert.equal(wins.pf, 'No losses'); assert.match(wins.coverage, /complete for the requested range$/)
  const empty = t.core.analyticsView({ summary: { closed_entries: 0, entries_with_result: 0 }, coverage: { status: 'not_requested' } })
  assert.equal(empty.wr, '—'); assert.equal(empty.wrSub, 'No closed entries in this range'); assert.equal(empty.pf, '—'); assert.equal(empty.avgWin, '—')
  assert.match(empty.coverage, /import range is not known/)
})

test('the dashboard replaces custom history controls with Recent trades', async () => {
  const t = load()
  t.on('GET', '/connections', (c, r) => r(200, {service: READY, connections: [{id: 7, card: card(7)}]}))
  t.on('GET', '/connections/7/snapshot', (c, r) => r(200, snapshot(7)))
  await t.bridge.loadConnections()
  t.env.active = 'mtapi:7'
  await t.bridge.refreshSnapshot('mtapi:7', {force:true})
  assert.equal(t.document.querySelectorAll('[data-history-range]').length, 0)
})
