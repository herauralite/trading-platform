// Browser-flow test for Match-Trader API accounts in the served dashboard.
//
// Loads the BUILT page (dist/app.html) in jsdom with a scripted fake TaliTrade backend and drives
// the real page: real clicks on the real buttons, real page functions, real DOM. No network, no
// broker, no real credentials. Run with:  npm run test:flow
//
// What this is not: it is not a real browser (no layout, no CSS rendering), not a real Telegram
// sign-in and not a real broker. Those are checked by hand; see MATCHTRADER_API_INTEGRATION.md.
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import jsdomPkg from 'jsdom'
const { JSDOM, VirtualConsole } = jsdomPkg

const dist = path.resolve(process.argv[2] || path.join(path.dirname(fileURLToPath(import.meta.url)), '../dist'))
const TOKEN = 'broker-token-SYNTHETIC-0123456789-abcdefghij'
const READY = { secure_storage: true, worker: true, connect_enabled: true, accepting_connections: true }
const rawHtml = fs.readFileSync(path.join(dist, 'app.html'), 'utf8')
// jsdom does not fetch sub-resources here: the local scripts are inlined exactly as built and
// external scripts (chart library, Telegram widget) are dropped.
const html = rawHtml
  .replace(/<script src="(\/tali-[a-z-]+\.js)"><\/script>/g, (_m, src) => `<script>${fs.readFileSync(path.join(dist, src), 'utf8')}</script>`)
  .replace(/<script[^>]*src="https?:[^"]*"[^>]*><\/script>/g, '')

const pageErrors = []
process.on('unhandledRejection', (e) => pageErrors.push('unhandled rejection: ' + String((e && e.message) || e).slice(0, 160)))
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const results = []
function check(name, cond, detail = '') {
  results.push(!!cond)
  console.log((cond ? 'PASS ' : 'FAIL ') + name + (cond ? '' : '  [' + String(detail).slice(0, 300) + ']'))
}
function section(title) { console.log('\n== ' + title + ' ==') }

// ── scripted backend (one per scenario) ──
function makeBackend(options = {}) {
  const db = { mode: options.mode || 'on', service: options.service || READY, connected: !!options.connected, synced: !!options.synced, backfilled: !!options.synced,
    balance: '50123.45', syncedAt: options.synced ? new Date().toISOString() : null, request: null, script: [], requests: [], holdList: null, news: options.news || [],
    trades: [], syncPosts: 0, refuseSync: null, historyPosts: [], analyticsReads: [], analyticsCoverage: 'complete_for_requested_range', noCreationTime: !!options.noCreationTime, market: options.market || null, marketReads: { ticker: 0, candles: 0 }, authMode: options.authMode || 'ok' }
  const now = () => new Date().toISOString()
  const card = () => ({ id: 7, account_key: 'mtapi:7', connector_type: 'matchtrader_api', broker_name: 'Fake Broker', display_label: 'Fake Broker · …0001', masked_login: '…0001', currency: 'USD',
    provider_account_type: 'DEMO', status: 'active', display_state: db.synced ? 'ready' : 'first_sync_pending', balance: db.synced ? db.balance : null, last_synced_at: db.syncedAt })
  const fr = (ok) => ({ status: ok ? 'fresh' : 'never_synced', last_success_at: ok ? db.syncedAt : null, last_attempt_at: ok ? db.syncedAt : null, last_error: null, is_stale: false })
  const tr = (uid, net, symbol = 'XAUUSD') => ({ entry_key: 'mtapi:7:' + uid, uid, position_id: 'P' + uid, symbol, alias: symbol, side: 'SELL', volume: '0.1', open_price: '2650.1', close_price: '2648.2',
    open_time: now(), close_time: now(), profit: net, commission: '0', swap: '0', net_profit: net, close_reason: 'USER' })
  if (db.synced) db.trades = [tr('c', '40'), tr('b', '-18.75'), tr('a', '10', '<script>alert(1)</script>')]
  const req = (state, extra = {}) => ({ id: 1, state, detail: null, resources: {}, pending: state === 'queued' || state === 'running', requested_at: now(), started_at: null, finished_at: null, ...extra })
  function applyDone(state) {   // what the worker would have stored by the time the request reaches a final state
    if (state === 'completed' || state === 'partial') {
      db.synced = true; db.syncedAt = now(); db.balance = db.nextBalance || db.balance
      db.trades = [tr('c', '40'), tr('b', '-18.75'), tr('a', '10', '<script>alert(1)</script>')]
    }
  }
  function snapshot() {
    const n = db.trades.length
    return { connection: card(), sync_request: db.request,
      summary: db.synced ? { balance: db.balance, equity: '50100.00', free_funds: '50100.00', margin: '0', profit: '-23.45', credit: '0', margin_level: null, fetched_at: db.syncedAt } : null,
      positions: db.synced ? [{ id: '1', symbol: 'US30<b>', alias: 'US30', side: 'BUY', volume: '0.2', open_price: '39000.5', current_price: '39010', commission: '-0.5', swap: '0', profit: '-23', net_profit: '-23.45', margin: '0', open_time: now(), tp_price: null, sl_price: null }] : null,
      pending_orders: db.synced ? [] : null,
      freshness: { summary: fr(db.synced), positions: fr(db.synced), pending_orders: fr(db.synced), closed_positions: fr(db.synced), ledgers: fr(db.synced) },
      history: { coverage_status: !db.synced ? 'pending' : db.historyRunning ? 'in_progress' : db.backfilled ? 'complete_for_requested_range' : 'in_progress', requested_from: now(), requested_to: now(), queued_intervals: 0, gap_count: 0, closed_entry_count: db.synced ? n : null,
        period_stats: db.synced ? { net_profit: '31.25', closed_entries: n, positions: n, winning_entries: 2, losing_entries: 1, first_close_time: '2026-11-10T10:00:00Z', last_close_time: now(), is_complete_for_requested_range: db.backfilled } : null },
      background_refresh: { enabled: true }, service: db.service, server_time: now() }
  }
  async function handle(url, init = {}) {
    const u = new URL(url); const method = init.method || 'GET'; const p = u.pathname
    db.requests.push({ method, path: p + u.search, auth: (init.headers || {}).Authorization, body: init.body || null })
    const json = (status, body) => ({ ok: status < 300, status, headers: { get: () => 'application/json' }, json: async () => body, text: async () => JSON.stringify(body) })
    if (db.sessionRejected && (init.headers || {}).Authorization) return json(401, { detail: 'Session token expired' })   // every authenticated call
    if (p === '/auth/me' && db.authMode === 'down') throw new TypeError('Failed to fetch')       // network failure
    if (p === '/auth/me' && db.authMode === '401') return json(401, { detail: 'Session token expired' })
    if (p === '/auth/me' && db.authMode === '500') return json(500, { detail: 'Internal server error' })
    // The real /auth/me returns database rows (snake_case), unlike the sign-in payload (camelCase).
    const meRow = (a) => ({ id: 1, telegram_user_id: '1001', account_id: a.account_id || a.accountId, broker: a.broker || 'fundingpips', account_type: a.account_type || a.accountType || null,
      account_size: a.account_size ?? a.accountSize ?? null, label: a.label || null, is_active: a.is_active ?? a.isActive ?? true, is_primary: false, connector_type: a.connector_type || a.connectorType || 'fundingpips_extension' })
    if (p === '/auth/me') return json(200, { user: { telegram_user_id: '1001', first_name: 'Tali' }, accounts: (options.legacyAccounts || []).map(meRow) })
    if (p === '/extension/news') return json(200, { events: db.news })
    if (options.legacyAccounts && p.startsWith('/extension/journal')) return json(200, { trades: [], entries: [], total: 0 })   // the older account's own (empty) data
    if (p === '/leaderboard') return json(200, { leaderboard: [], total: 0 })
    const base = '/connectors/matchtrader-api/connections'
    if (!p.startsWith(base)) return json(404, { detail: 'Not Found' })
    if (db.mode === 'off') return json(404, { detail: 'Not Found' })               // TALITRADE_MT_API_ENABLED is off
    if (p === base && method === 'GET') {
      if (db.holdList) await db.holdList                                           // lets a test keep availability "unknown" for a while
      return json(200, { service: db.service, connections: db.connected ? [{ id: 7, card: card() }] : [] })
    }
    if (p === base && method === 'POST') {
      if (!db.service.accepting_connections) return json(503, { detail: 'New Match-Trader connections are not open on this server yet' })
      if (JSON.parse(init.body).token !== TOKEN) return json(422, { detail: 'Match-Trader rejected this token.' })
      db.connected = true; db.request = req('queued'); db.backfilled = true      // the server queues the first refresh and the 7-day import itself
      return json(200, { ok: true, connection: { id: 7, status: 'active' }, card: card(), sync_request: db.request,
        initial_backfill: { days: 7, resources: ['closed_positions', 'ledgers'] }, timings: { verify_seconds: 1.1, store_seconds: 0.2 } })
    }
    if (!db.connected) return json(404, { detail: 'Connection not found' })
    if (p === base + '/7/sync' && method === 'POST') {
      db.syncPosts++
      if (db.refuseSync) return json(db.refuseSync[0], { detail: db.refuseSync[1] })
      const pending = db.request && db.request.pending
      if (!pending) db.request = req('queued', { id: (db.request ? db.request.id : 0) + 1 })
      return json(202, { accepted: true, already_pending: !!pending, request: db.request })
    }
    if (p === base + '/7/sync' && method === 'GET') {
      if (db.script.length) { const next = db.script.shift(); db.request = { ...db.request, ...req(next.state, next), id: db.request ? db.request.id : 1 }; applyDone(next.state) }
      return json(200, { request: db.request })
    }
    if (p === base + '/7/market/ticker' || p === base + '/7/market/candles') {
      const m = db.market
      if (!m || m.mode === 'off') return json(404, { detail: 'Not Found' })
      db.marketReads[p.endsWith('ticker') ? 'ticker' : 'candles']++
      const status = m.mode === 'closed' ? 'market_closed' : m.mode === 'denied' ? 'permission_denied' : 'fresh'
      const detail = m.mode === 'denied' ? 'This API token is not allowed to read market data. Ask your broker to enable market data for Trader API, or create a token that includes it.' : m.mode === 'closed' ? 'Market closed: these are the last prices the broker published.' : null
      if (p.endsWith('ticker')) {
        const open = m.mode !== 'closed'
        const items = m.mode === 'denied' ? [] : [
          { symbol: 'EURUSD.x', alias: 'EURUSD', bid: '1.08512', ask: '1.08519', price_precision: 5, provider_time: '2026-11-13T20:00:00Z', fetched_at: now(), session_open: open, state: status },
          { symbol: 'GOLD', alias: 'XAU<b>USD</b>', bid: '4100.1', ask: '4100.55', price_precision: 2, provider_time: '2026-11-13T20:00:00Z', fetched_at: now(), session_open: open, state: status }]
        return json(200, { connection_id: 7, price_basis: 'bid_ask', updating: 'auto', poll_interval_seconds: 15, status, detail, last_success_at: now(), last_error: m.mode === 'denied' ? 'forbidden' : null, items, server_time: now() })
      }
      const interval = u.searchParams.get('interval') || 'M5'
      if (m.hold && m.hold[interval]) await m.hold[interval]
      const candles = m.mode === 'denied' ? [] : (m.candles[interval] || [])
      return json(200, { connection_id: 7, interval, available_intervals: ['M1', 'M5', 'H1'], updating: 'auto', poll_interval_seconds: 60, price_side: 'not_specified_by_provider', server_time: now(),
        symbol: m.mode === 'denied' ? null : { symbol: 'EURUSD.x', alias: 'EURUSD', description: 'Euro vs US Dollar', type: 'FOREX', price_precision: 5, session_open: m.mode !== 'closed' },
        candles, window: { count: candles.length, limit: 300, is_complete_history: false, first_open_time: candles.length ? new Date(candles[0].t).toISOString() : null, last_open_time: candles.length ? new Date(candles[candles.length - 1].t).toISOString() : null },
        status: candles.length || m.mode === 'denied' ? status : 'loading', detail, last_success_at: now(), last_error: m.mode === 'denied' ? 'forbidden' : null })
    }
    if (p === base + '/7/backfill') {
      const b = JSON.parse(init.body || '{}'); db.historyPosts.push(b)
      if (b.range === 'all' && db.noCreationTime) return json(409, { detail: { reason: 'start_date_required', message: 'The broker did not report when this account was created. Choose a start date instead.' } })
      db.backfilled = true; db.historyRunning = true; db.analyticsCoverage = 'in_progress'
      return json(200, { ok: true, queued: ['closed_positions', 'ledgers'], requested_from: b.from_ms ? new Date(b.from_ms).toISOString() : '2026-10-16T00:00:00Z', clamped_to_account_creation: false })
    }
    if (p === base + '/7/analytics') {
      db.analyticsReads.push(u.searchParams.get('tz'))
      return json(200, { connection_id: 7, currency: 'USD', time_zone: u.searchParams.get('tz'), positive_day_streak: 3,
        summary: { net_profit: '-1250.5', closed_entries: 230, entries_with_result: 230, entries_without_result: 0, positions: 190, winning_entries: 92, losing_entries: 120,
          breakeven_entries: 18, entry_win_rate_percent: '40.0', profit_factor: '0.82', average_win: '61.2', average_loss: '-57.33' },
        best_symbol: { symbol: 'XAU<i>USD</i>', net_profit: '410' }, cumulative_realized: [{ cumulative_net: '50' }, { cumulative_net: '-400' }, { cumulative_net: '-1250.5' }],
        coverage: { status: db.analyticsCoverage, requested_from: '2026-11-07T00:00:00Z', gap_count: 0 }, counting_rules: {}, not_derived: [] })
    }
    if (p === base + '/7/snapshot') return json(200, snapshot())
    if (p === base + '/7/trades') return json(200, { trades: db.trades, next_cursor: null, has_more: false })
    if (p === base + '/7' && method === 'DELETE') { db.connected = false; return json(200, { ok: true }) }
    return json(404, { detail: 'Not Found' })
  }
  return { db, handle }
}

async function boot(backend, { wait = 1500, session = true, notice = null } = {}) {
  const errors = []
  const vc = new VirtualConsole()
  vc.on('jsdomError', (e) => errors.push(String(e.message || e).slice(0, 200)))
  vc.on('error', (...a) => errors.push('console.error: ' + a.map(String).join(' ').slice(0, 200)))
  const before = pageErrors.length
  const dom = new JSDOM(html, { url: 'http://localhost:5173/app', runScripts: 'dangerously', virtualConsole: vc, pretendToBeVisual: true,
    beforeParse(window) {
      window.__TALI_CONFIG__ = { apiBase: 'http://localhost:8000' }
      window.fetch = backend.handle
      if (session) {
        window.localStorage.setItem('tali_session_token_v1', 'fake-session')
        window.localStorage.setItem('tali_user_v1', JSON.stringify({ telegramUserId: '1001', telegram_user_id: '1001', firstName: 'Tali' }))
        window.sessionStorage.setItem('tali_tg_done', '1')
      }
      if (notice) window.sessionStorage.setItem('tali_session_notice', notice)
      window.sessionStorage.setItem('tali_pw_ok', '1')
      window.matchMedia = window.matchMedia || (() => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }))
      window.scrollTo = () => {}
      // The chart library is an external script (not loaded here). This stand-in records what the page draws.
      const series = (kind) => ({ kind, data: [], updates: [], sets: 0, options: { visible: kind === 'line' },
        setData(d) { this.data = d.slice(); this.sets++ }, update(bar) { this.updates.push(bar); const i = this.data.findIndex((x) => x.time === bar.time); if (i >= 0) this.data[i] = bar; else this.data.push(bar) },
        setMarkers() {}, applyOptions(o) { Object.assign(this.options, o) } })
      window.__chart = { line: null, candle: null, range: { from: 3, to: 9 }, rangeSets: 0, fits: 0 }
      window.LightweightCharts = { CrosshairMode: { Normal: 0 }, createChart: () => ({
        addLineSeries: () => (window.__chart.line = series('line')), addCandlestickSeries: () => (window.__chart.candle = series('candle')), applyOptions() {},
        timeScale: () => ({ getVisibleLogicalRange: () => window.__chart.range, setVisibleLogicalRange: (r) => { window.__chart.range = r; window.__chart.rangeSets++ }, fitContent: () => { window.__chart.fits++ } }) }) }
      window.ResizeObserver = window.ResizeObserver || class { observe() {} disconnect() {} }
    } })
  await sleep(wait)
  const w = dom.window
  const $ = (sel) => w.document.querySelector(sel)
  const text = (id) => (w.document.getElementById(id) || {}).textContent || ''
  const cardButton = (key) => $(`#addAcctFlowBody [data-broker-card="${key}"]`)
  const allErrors = () => errors.concat(pageErrors.slice(before))
  return { w, $, text, cardButton, allErrors, db: backend.db }
}

const noLegacyCredentialFields = (p) => !p.$('#fpPasswordInput') && !p.$('#fpEmailInput') && !p.$('#tlPasswordInput')
const tokenInput = (p) => p.w.document.getElementById('mtApiTokenInput')
const syncState = (p) => JSON.parse(p.w.eval("JSON.stringify(getManualSyncState('mtapi:7'))"))

// ── 1. startup with no account: no boot errors, notifications still work ──
section('startup, no account')
{
  const news = [{ title: 'Synthetic CPI release', currency: 'USD', impact: 'high', time_et: '8:30 AM', start_at: new Date(Date.now() + 30 * 60000).toISOString() }]
  const p = await boot(makeBackend({ news }))
  check('empty-account startup has no page errors (drawerNotifs / chartInited were used before initialization)', p.allErrors().length === 0, p.allErrors().join(' | '))
  check('no-account state finished rendering', p.w.eval('activeAcct') === '' && p.w.eval('Object.keys(ACCOUNTS).length') === 0 && p.text('ovBalance') !== undefined)
  p.w.eval('pushNewsToDrawer(' + JSON.stringify(news) + ')')
  check('notifications still collect items', p.w.eval('drawerNotifs.news.length') === 1)
  p.w.document.getElementById('btnBell').click()
  const drawer = p.w.document.getElementById('notif-drawer')
  check('bell opens the drawer and lists the item', drawer.classList.contains('open') && drawer.textContent.includes('Synthetic CPI release'), drawer.textContent.slice(0, 120))
  p.w.eval('closeDrawer(); resetDrawerForContext()')
  check('drawer resets for a new context without errors', p.w.eval('drawerNotifs.news.length') === 0 && p.allErrors().length === 0, p.allErrors().join(' | '))
  p.w.close()
}

// ── 2. onboarding through the one supported integration ──
section('Match-Trader API onboarding')
{
  const backend = makeBackend()
  let release
  backend.db.holdList = new Promise((r) => { release = r })           // availability is still unknown when the sheet opens
  const p = await boot(backend, { wait: 900 })
  p.w.document.getElementById('btnAccounts').click()                  // the header Accounts button
  check('header Accounts button opens the account sheet', p.w.document.getElementById('acct-sheet') ? p.w.document.getElementById('acct-sheet').classList.contains('open') : p.w.eval("typeof openAcctSheet") === 'function')
  p.w.eval('openAddAccountFlow()')
  const cards = () => [...p.w.document.querySelectorAll('#addAcctFlowBody [data-broker-card]')].map((b) => b.getAttribute('data-broker-card'))
  let mt = p.cardButton('matchtraderApi')
  check('the picker offers exactly one integration: Match-Trader API', JSON.stringify(cards()) === '["matchtraderApi"]' && !/FundingPips|FundedPips|TradeLocker|MetaTrader|MT5/i.test(p.text('addAcctFlowBody')), JSON.stringify(cards()) + ' ' + p.text('addAcctFlowBody').slice(0, 160))
  check('while availability is being checked the card is disabled and says so', !!mt && mt.disabled && /Checking availability/.test(mt.textContent), mt && mt.textContent)
  mt.click()
  check('a click during the check does nothing: no form, no password fallback', p.w.eval('addAcctFlowState.step') === 1 && !tokenInput(p) && noLegacyCredentialFields(p))
  for (const legacy of ['fundingpips', 'tradelocker', 'mt5', 'brokerApi']) p.w.eval(`setAddAccountPath('${legacy}')`)
  check('other providers cannot be opened from this flow, even by calling the page function', p.w.eval('addAcctFlowState.step') === 1 && noLegacyCredentialFields(p) && !p.$('#addAcctIdInput'))
  release(); await sleep(400)
  mt = p.cardButton('matchtraderApi')
  check('once the server confirms, the card is enabled as a read-only API connection', !!mt && !mt.disabled && /read-only API token/.test(mt.textContent) && /READ-ONLY/.test(mt.textContent), mt && mt.textContent)
  check('compatibility is not overstated: Trader API must be enabled by the broker', /Trader API<\/b> enabled|have Trader API enabled/.test(p.w.document.getElementById('addAcctFlowBody').innerHTML) && /Not every Match-Trader broker does/.test(p.text('addAcctFlowBody')), p.text('addAcctFlowBody').slice(-200))
  mt.click()
  const input = tokenInput(p)
  const label = p.$('label[for="mtApiTokenInput"]')
  check('the card opens the labelled read-only token form', p.w.eval('addAcctFlowState.step') === 2 && !!input && input.type === 'password' && input.getAttribute('autocomplete') === 'off' && !!label && /read-only API token/i.test(label.textContent), label && label.textContent)
  check('the form has exactly one field, no broker password or email, and names no specific broker', p.w.document.querySelectorAll('#addAcctFlowBody input').length === 1 && noLegacyCredentialFields(p) && !/FundingPips|FundedPips/i.test(p.text('addAcctFlowBody')), p.text('addAcctFlowBody').slice(0, 200))
  const details = p.$('#addAcctFlowBody details')
  check('instructions start collapsed', !!details && details.open === false)
  details.querySelector('summary').click()
  if (!details.open) details.open = true                              // jsdom may not implement the summary toggle; the attribute is what the browser sets
  const steps = [...details.querySelectorAll('ol li')].map((li) => li.textContent)
  check('instructions expand to the four generic steps', details.open && steps.length === 4 && /broker or prop firm.*Match-Trader/.test(steps[0]) && /API Access/.test(steps[1]) && /TaliTrade.*expiry.*Read-only/.test(steps[2]) && /paste/i.test(steps[3]) && /shown only once/.test(details.textContent) && /Trader API/.test(details.textContent), steps.join(' / '))

  p.w.eval('backAddAccountStep()')
  const selected = () => [...p.w.document.querySelectorAll('#addAcctFlowBody [data-broker-card].selected')].map((b) => b.getAttribute('data-broker-card'))
  check('going back shows the chosen card highlighted', JSON.stringify(selected()) === '["matchtraderApi"]' && p.cardButton('matchtraderApi').getAttribute('aria-pressed') === 'true', JSON.stringify(selected()))

  // token field clears on dismissal and on logout
  p.cardButton('matchtraderApi').click()
  tokenInput(p).value = TOKEN
  p.w.eval('closeAddAccountFlow()')
  check('dismissing the sheet clears the token field', tokenInput(p).value === '' && !p.w.document.getElementById('add-acct-sheet').classList.contains('open'))
  p.w.eval('openAddAccountFlow()'); p.cardButton('matchtraderApi').click()
  check('reopening shows an empty field', tokenInput(p).value === '')
  tokenInput(p).value = TOKEN
  p.w.eval('backAddAccountStep()')
  check('going back removes the field from the page', !tokenInput(p) && !p.w.document.documentElement.outerHTML.includes(TOKEN))
  p.cardButton('matchtraderApi').click()
  tokenInput(p).value = TOKEN
  p.w.TaliMatchTrader.reset()                                         // what the sign-out handler calls
  check('logout clears the token field and the provider state', tokenInput(p).value === '' && p.w.TaliMatchTrader.isAvailable() === false)
  check('the sign-out handler really calls that reset', /localStorage\.removeItem\(TALI_SESSION_TOKEN_KEY\);\s*if \(window\.TaliMatchTraderMarket\) TaliMatchTraderMarket\.reset\(\);\s*if \(window\.TaliMatchTrader\) TaliMatchTrader\.reset\(\)/.test(rawHtml))
  await p.w.TaliMatchTrader.loadConnections()
  p.w.eval('closeAddAccountFlow(); openAddAccountFlow()'); await sleep(200)
  p.cardButton('matchtraderApi').click()

  // submission: field cleared at once, token sent once, first sync followed through its states
  backend.db.script = [{ state: 'queued' }, { state: 'running' }, { state: 'completed', resources: { summary: 'ok', positions: 'ok', pending_orders: 'ok' } }]
  tokenInput(p).value = TOKEN
  const mark = backend.db.requests.length
  let releaseLists
  backend.db.holdList = new Promise((r) => { releaseLists = r })       // every list read hangs: closing the sheet must not depend on one
  p.$('#addAcctFooter .btn-blue').click()                             // the real Connect button
  check('token cleared from the page the moment Connect is pressed', (tokenInput(p) || { value: '' }).value === '')
  await sleep(300)
  check('connected: the sheet closes and the account is selected before any balance exists, with the list read still pending', p.w.eval('activeAcct') === 'mtapi:7' && !p.w.document.getElementById('add-acct-sheet').classList.contains('open'), p.w.eval('activeAcct'))
  check('only the connect request was needed to close the sheet (no import request from the browser)', !backend.db.requests.slice(mark).some((r) => r.path.endsWith('/backfill')) && backend.db.requests.slice(mark)[0].method === 'POST')
  const timings = p.w.TaliMatchTrader.lastConnectTimings()
  check('connect phases are measured as durations only', !!timings && typeof timings.verify_and_store_ms === 'number' && typeof timings.sheet_closed_ms === 'number' && timings.sheet_closed_ms - timings.verify_and_store_ms < 250 && !JSON.stringify(timings).includes(TOKEN), JSON.stringify(timings))
  releaseLists(); backend.db.holdList = null
  await sleep(300)
  const early = syncState(p)
  check('first sync is shown as queued or running, never as synced', early.status === 'syncing' && ['queued', 'running'].includes(early.label) && p.text('ovSync') !== 'Synced', JSON.stringify(early) + ' ' + p.text('ovSync'))
  await sleep(5200)
  const done = syncState(p)
  check('first sync reaches completed and only then the balance appears', done.label === 'completed' && done.status === 'success' && p.text('ovBalance') === '$50,123.45', JSON.stringify(done) + ' ' + p.text('ovBalance'))
  const since = backend.db.requests.slice(mark)
  check('token sent exactly once, only in the connect body', since.filter((r) => (r.body || '').includes(TOKEN)).length === 1 && !backend.db.requests.some((r) => r.path.includes(TOKEN)) && !JSON.stringify(Object.assign({}, p.w.localStorage)).includes(TOKEN) && !JSON.stringify(Object.assign({}, p.w.sessionStorage)).includes(TOKEN) && !p.w.document.documentElement.outerHTML.includes(TOKEN))
  check('no extra sync job was posted: the server queued the first one', backend.db.syncPosts === 0)
  check('no legacy route was used: no link-account, no broker password login, no extension status', !since.some((r) => /link-account|prop-firm|fundingpips|\/extension\/(status|journal|payout)/.test(r.path)), since.map((r) => r.path).join(' '))

  // the synced account on screen
  check('equity, positions, sync, period P&L and win rate cards', p.text('ovEquity') === '$50,100.00' && p.text('ovPositions') === '1 open' && p.text('ovSync') === 'Synced' && p.text('ovOverallPnl') === '+$31.25' && p.text('ovWinRate') === '67%', [p.text('ovEquity'), p.text('ovPositions'), p.text('ovSync'), p.text('ovOverallPnl'), p.text('ovWinRate')].join(' | '))
  check('prop progress is unavailable, not guessed', p.text('ovProgress') === '—', p.text('ovProgress'))
  const panel = p.w.document.getElementById('mtApiPanel')
  check('positions panel escapes provider text', !!panel && panel.textContent.includes('US30<b>') && !panel.querySelector('b') && panel.textContent.includes('-$23.45 net') && panel.textContent.includes('No pending orders.'), panel && panel.textContent.slice(0, 200))
  const hist = p.w.document.getElementById('recentTradesEl')
  check('history lists imported trades, escaped', hist.textContent.includes('XAUUSD') && hist.textContent.includes('<script>alert(1)</script>') && !hist.querySelector('script') && hist.textContent.includes('-$18.75'), hist.textContent.slice(0, 160))
  p.w.eval('renderAcctSheet()')
  const sheet = p.text('acctSheetBody')
  check('account sheet: masked login, API label, no Primary/Deactivate, no internal key', sheet.includes('…0001') && sheet.includes('Fake Broker · API · DEMO') && sheet.includes('Remove account') && !sheet.includes('Deactivate') && !sheet.includes('mtapi:7'), sheet.slice(0, 200))

  // Sync now: queued -> running -> partial, then a failure, then a refusal
  section('Sync now states')
  backend.db.nextBalance = '50200.00'
  backend.db.script = [{ state: 'queued' }, { state: 'running' }, { state: 'partial', resources: { summary: 'ok', positions: 'ok', pending_orders: 'transient' } }]
  const seen = new Set()
  const watcher = setInterval(() => { const s = syncState(p); seen.add(s.status + '/' + s.label) }, 40)
  p.w.eval("triggerManualSync('mtapi:7')")
  await sleep(5400)
  const partial = syncState(p)
  check('Sync now shows queued, then running, as separate states', seen.has('syncing/queued') && seen.has('syncing/running'), [...seen].join(' '))
  check('a partial result is labelled partially updated and names what was missed', partial.label === 'partial' && /^Partially updated\. Could not refresh: pending orders\.$/.test(partial.message), JSON.stringify(partial))
  check('saved data is re-read after the final state', p.text('ovBalance') === '$50,200.00', p.text('ovBalance'))
  backend.db.script = [{ state: 'failed', detail: 'rate_limited' }]
  p.w.eval("triggerManualSync('mtapi:7')"); await sleep(2200)
  const failed = syncState(p)
  check('a failed sync says failed and why; the old balance stays', failed.status === 'failed' && /rate limit/i.test(failed.message) && p.text('ovBalance') === '$50,200.00', JSON.stringify(failed))
  backend.db.refuseSync = [503, 'Background sync is not running on this server']
  p.w.eval("triggerManualSync('mtapi:7')"); await sleep(400)
  const refused = syncState(p)
  check('a refused request (no worker) is reported, not shown as queued', refused.status === 'failed' && /Background sync is not running/.test(refused.message), JSON.stringify(refused))
  clearInterval(watcher)
  backend.db.refuseSync = null

  // disconnect needs two taps
  p.w.eval('renderAcctSheet()')
  await p.w.TaliMatchTrader.disconnect('mtapi:7')
  check('first Disconnect tap only arms it', backend.db.requests.filter((r) => r.method === 'DELETE').length === 0 && p.text('acctSheetBody').includes('Confirm removal'))
  await p.w.TaliMatchTrader.disconnect('mtapi:7'); await sleep(500)
  check('second tap disconnects; card and data are gone', backend.db.requests.filter((r) => r.method === 'DELETE').length === 1 && p.w.eval('Object.keys(ACCOUNTS).length') === 0 && p.w.eval('activeAcct') === '' && panel.style.display === 'none')
  check('the whole onboarding run produced no page errors', p.allErrors().length === 0, p.allErrors().join(' | '))
  p.w.close()
}

// ── 3. feature disabled ──
section('feature disabled')
for (const [name, options] of [
  ['flag off (routes answer 404)', { mode: 'off' }],
  ['routes on, connect gate closed', { service: { ...READY, connect_enabled: false, accepting_connections: false } }],
  ['routes on, worker off', { service: { ...READY, worker: false, accepting_connections: false } }],
]) {
  const backend = makeBackend(options)
  const p = await boot(backend, { wait: 900 })
  p.w.eval('openAddAccountFlow()'); await sleep(300)
  const card = p.cardButton('matchtraderApi')
  check(`${name}: the one card is disabled with a clear reason`, !!card && card.disabled && /Not available on this server right now/.test(card.textContent), card && card.textContent)
  card.click(); p.w.eval("setAddAccountPath('matchtraderApi')")
  check(`${name}: no token is requested and there is no password fallback`, p.w.eval('addAcctFlowState.step') === 1 && !tokenInput(p) && noLegacyCredentialFields(p))
  check(`${name}: nothing was posted`, !backend.db.requests.some((r) => r.method === 'POST'))
  check(`${name}: no page errors`, p.allErrors().length === 0, p.allErrors().join(' | '))
  p.w.close()
}
{
  const backend = makeBackend({ connected: true, synced: true, service: { ...READY, connect_enabled: false, accepting_connections: false } })
  const p = await boot(backend, { wait: 2200 })
  check('gate closed after rollout: an already connected account still loads and shows data', p.w.eval('activeAcct') === 'mtapi:7' && p.text('ovBalance') === '$50,123.45', p.w.eval('activeAcct') + ' ' + p.text('ovBalance'))
  p.w.close()
}

// ── 4. startup with a saved connection ──
section('startup with a saved connection')
{
  const backend = makeBackend({ connected: true, synced: true })
  const p = await boot(backend, { wait: 2500 })
  check('saved-connection startup has no page errors', p.allErrors().length === 0, p.allErrors().join(' | '))
  check('the account is restored from the server without a token', p.w.eval('activeAcct') === 'mtapi:7' && p.text('ovBalance') === '$50,123.45' && !backend.db.requests.some((r) => r.method === 'POST'), p.w.eval('activeAcct') + ' ' + p.text('ovBalance'))
  // (One account-less /extension/status call happens at boot, before any account is known. That is the
  //  legacy page's own first poll; it carries no account id and stops once the API account is selected.)
  const firstSnapshot = backend.db.requests.findIndex((r) => r.path.endsWith('/7/snapshot'))
  check('no scraper route is polled for the API account', firstSnapshot >= 0 && !backend.db.requests.some((r) => /mtapi/.test(r.path)) && !backend.db.requests.slice(firstSnapshot).some((r) => /\/extension\/(status|journal|payout)/.test(r.path)), backend.db.requests.map((r) => r.path).join(' ').slice(0, 300))
  p.w.document.getElementById('btnBell').click()
  check('notification drawer opens with a saved connection too', p.w.document.getElementById('notif-drawer').classList.contains('open') && p.allErrors().length === 0, p.allErrors().join(' | '))
  p.w.close()
}

// ── 5. existing accounts are kept; the picker change only affects new connections ──
section('existing accounts with the simplified picker')
{
  const legacy = { account_id: '900100', accountId: '900100', label: 'Legacy 100K', account_type: 'evaluation', accountType: 'evaluation', account_size: 100000, accountSize: 100000,
    connector_type: 'fundingpips_extension', connectorType: 'fundingpips_extension', broker: 'fundingpips', is_active: true, isActive: true }
  const backend = makeBackend({ connected: true, synced: true, legacyAccounts: [legacy] })
  const p = await boot(backend, { wait: 2500 })
  const keys = JSON.parse(p.w.eval('JSON.stringify(Object.keys(ACCOUNTS))'))
  check('an account linked before this change and an API account are both still listed', keys.includes('900100') && keys.includes('mtapi:7'), JSON.stringify(keys))
  p.w.eval('renderAcctSheet()')
  const sheet = p.text('acctSheetBody')
  check('the API account card shows the broker name detected from the provider', sheet.includes('Fake Broker · API · DEMO') && sheet.includes('…0001') && sheet.includes('Legacy 100K'), sheet.slice(0, 260))
  p.w.eval("switchAccount('mtapi:7')"); await sleep(700)
  check('switching to the API account shows its data', p.w.eval('activeAcct') === 'mtapi:7' && p.text('ovBalance') === '$50,123.45', p.w.eval('activeAcct') + ' ' + p.text('ovBalance'))
  check('an API account does not claim its risk limits are safe or monitored', p.text('alertTitle') === 'Risk alerts not available' && p.text('alertSub') === 'Risk limits are not monitored for API accounts yet' && !/monitoring active/.test(p.text('alertSub')), p.text('alertTitle') + ' | ' + p.text('alertSub'))
  check('Overall P&L separates the first closed trade from where the import starts', /^Net of fees · closed trades from Nov 10 · history imported from /.test(p.text('ovOverallSub') || p.w.document.body.textContent.match(/Net of fees · closed trades from[^\n]*/)?.[0] || ''), (p.w.document.body.textContent.match(/(Net of fees|Imported period)[^·]*·[^·]*/) || [''])[0])
  const before = backend.db.requests.length
  p.w.eval("switchAccount('900100')"); await sleep(700)
  const afterLegacy = backend.db.requests.slice(before)
  check('switching to the older account uses its own routes and hides the API panel', p.w.eval('activeAcct') === '900100' && afterLegacy.some((r) => r.path.startsWith('/extension/')) && !afterLegacy.some((r) => /\/7\/(snapshot|trades)/.test(r.path)) && (p.w.document.getElementById('mtApiPanel') || { style: { display: 'none' } }).style.display === 'none', afterLegacy.map((r) => r.path).join(' ').slice(0, 300))
  p.w.eval("switchAccount('mtapi:7')"); await sleep(700)
  check('switching back restores the API account without reconnecting', p.w.eval('activeAcct') === 'mtapi:7' && p.text('ovBalance') === '$50,123.45' && !backend.db.requests.some((r) => r.method === 'POST' || r.method === 'DELETE'))
  p.w.eval('openAddAccountFlow()'); await sleep(300)
  check('the picker still offers only Match-Trader API', [...p.w.document.querySelectorAll('#addAcctFlowBody [data-broker-card]')].map((b) => b.getAttribute('data-broker-card')).join() === 'matchtraderApi')
  check('no page errors while switching between accounts', p.allErrors().length === 0, p.allErrors().join(' | '))
  p.w.close()
}

section('signing in with an older account already linked')
{
  // The sign-in path (finishTelegramLogin) must load API connections too, not only page boot. Before the
  // fix, startAccountLinkWatcher returned early for users with any linked account, so a freshly signed-in
  // user saw their API account only after a reload.
  const legacy = { account_id: '900100', accountId: '900100', label: 'Legacy 100K', account_type: 'evaluation', accountType: 'evaluation', account_size: 100000, accountSize: 100000,
    connector_type: 'fundingpips_extension', connectorType: 'fundingpips_extension', broker: 'fundingpips', is_active: true, isActive: true }
  const backend = makeBackend({ connected: false, synced: true, legacyAccounts: [legacy] })
  const p = await boot(backend, { wait: 1500 })
  check('before sign-in only the older account is known', !JSON.parse(p.w.eval('JSON.stringify(Object.keys(ACCOUNTS))')).includes('mtapi:7'))
  backend.db.connected = true            // the API account exists on the server for this user
  const reads = backend.db.requests.length
  await p.w.eval("finishTelegramLogin({ ok: true, access_token: 'fake-session', user: { telegramUserId: '1001', firstName: 'Tali' }, accounts: [" + JSON.stringify(legacy) + "] })")
  await sleep(1200)
  const keys = JSON.parse(p.w.eval('JSON.stringify(Object.keys(ACCOUNTS))'))
  check('signing in loads the API account without a reload', keys.includes('mtapi:7') && keys.includes('900100'), JSON.stringify(keys))
  check('the sign-in read the Match-Trader connections list', backend.db.requests.slice(reads).some((r) => r.method === 'GET' && r.path.startsWith('/connectors/matchtrader-api/connections?')), backend.db.requests.slice(reads).map((r) => r.path).join(' ').slice(0, 300))
  check('no page errors during sign-in', p.allErrors().length === 0, p.allErrors().join(' | '))
  p.w.close()
}

section('reloading with a stored session')
{
  // A stored session is checked with the backend before the gate decides. Before this change the gate
  // always showed the Telegram step after a reload, even with a valid session.
  const gateShown = (p) => p.w.document.getElementById('gate').style.display !== 'none'
  const token = (p) => p.w.localStorage.getItem('tali_session_token_v1')
  const legacy = { account_id: '900100', accountId: '900100', label: 'Legacy 100K', accountType: 'evaluation', account_type: 'evaluation', is_active: true, isActive: true }

  // accepted
  let backend = makeBackend({ connected: true, synced: true, legacyAccounts: [legacy] })
  let p = await boot(backend, { wait: 2500 })
  const paths = backend.db.requests.map((r) => r.path)
  check('a valid stored session opens the dashboard without signing in again', !gateShown(p), p.w.document.getElementById('gate').style.display)
  check('the session is checked with /auth/me before any account data is requested', paths.indexOf('/auth/me') >= 0 && paths.findIndex((x) => x.startsWith('/connectors/')) > paths.indexOf('/auth/me'), paths.slice(0, 6).join(' '))
  const keys = JSON.parse(p.w.eval('JSON.stringify(Object.keys(ACCOUNTS))'))
  check('accounts still load after the session is accepted', keys.includes('mtapi:7') && keys.includes('900100'), JSON.stringify(keys))
  check('the restored older account keeps its label from the /auth/me row', p.w.eval("ACCOUNTS['900100'] && ACCOUNTS['900100'].label") === 'Legacy 100K' && p.w.eval("ACCOUNTS['900100'].accountType") === 'evaluation')
  check('no page errors when restoring a session', p.allErrors().length === 0, p.allErrors().join(' | '))
  // the session expires while the page is open: the API account's next refresh returns 401
  backend.db.sessionRejected = true
  await p.w.TaliMatchTrader.refreshSnapshot('mtapi:7', { force: true }); await sleep(100)
  check('a 401 on the API account refresh ends the session and returns to sign-in', !token(p) && p.w.sessionStorage.getItem('tali_session_notice') === 'expired')
  p.w.close()
  // the same for an older account's status poll
  backend = makeBackend({ connected: true, synced: true, legacyAccounts: [legacy] })
  p = await boot(backend, { wait: 2500 })
  p.w.eval("switchAccount('900100')"); await sleep(500)
  backend.db.sessionRejected = true
  await p.w.eval('fetchStatus()'); await sleep(100)
  check('a 401 on the older account status poll ends the session too', !token(p) && p.w.sessionStorage.getItem('tali_session_notice') === 'expired')
  p.w.close()
  // the account watcher (users with no linked accounts) sees the expiry on /auth/me
  backend = makeBackend({ connected: false, synced: true })
  p = await boot(backend, { wait: 2000 })
  backend.db.authMode = '401'
  await p.w.eval("syncLinkedAccountsFromBackend('1001')"); await sleep(100)
  check('a session the backend rejects mid-use is cleared and flagged for the sign-in screen', !token(p) && !p.w.localStorage.getItem('tali_user_v1') && p.w.sessionStorage.getItem('tali_session_notice') === 'expired')
  p.w.close()

  // rejected at load
  backend = makeBackend({ connected: true, synced: true, legacyAccounts: [legacy], authMode: '401' })
  p = await boot(backend, { wait: 2000 })
  check('an expired or rejected session shows sign-in with a clear message', gateShown(p) && /session has expired or is no longer valid/.test(p.text('gate-tg-error')) && p.w.document.getElementById('gate-step2').style.display === 'block', p.text('gate-tg-error'))
  check('the rejected session is removed from storage', !token(p) && !p.w.localStorage.getItem('tali_user_v1'))
  check('no account data is requested with a rejected session', !backend.db.requests.some((r) => r.path.startsWith('/connectors/') || r.path.startsWith('/extension/status')) && Object.keys(JSON.parse(p.w.eval('JSON.stringify(ACCOUNTS)'))).length === 0, backend.db.requests.map((r) => r.path).join(' '))
  p.w.close()

  // network failure, then recovery with "Try again"
  backend = makeBackend({ connected: true, synced: true, legacyAccounts: [legacy], authMode: 'down' })
  p = await boot(backend, { wait: 2000 })
  check('when the backend cannot be reached the gate stays closed and keeps the session', gateShown(p) && token(p) === 'fake-session' && /could not be reached/.test(p.text('gate-tg-error')), p.text('gate-tg-error'))
  check('the network failure offers "Try again" and requests no account data', (p.$('#gate-session-retry') || { style: {} }).style.display === 'block' && !backend.db.requests.some((r) => r.path.startsWith('/connectors/') || r.path.startsWith('/extension/status')), backend.db.requests.map((r) => r.path).join(' '))
  backend.db.authMode = 'ok'
  if (p.$('#gate-session-retry')) p.$('#gate-session-retry').click()
  await sleep(1200)
  check('"Try again" opens the dashboard once the backend answers', !gateShown(p) && JSON.parse(p.w.eval('JSON.stringify(Object.keys(ACCOUNTS))')).includes('mtapi:7'))
  p.w.close()

  // server error
  backend = makeBackend({ connected: true, synced: true, authMode: '500' })
  p = await boot(backend, { wait: 2000 })
  check('a server error keeps the session and says so', gateShown(p) && token(p) === 'fake-session' && /server error 500/.test(p.text('gate-tg-error')), p.text('gate-tg-error'))
  p.w.close()

  // signing out, then the next load
  backend = makeBackend({ connected: true, synced: true })
  p = await boot(backend, { wait: 2000 })
  try { p.w.document.getElementById('btnProfile').click() } catch (_) { /* jsdom cannot reload */ }
  check('avatar opens profile and keeps session', !!token(p) && p.$('#page-account').classList.contains('active'))
  p.$('#profileSignOut').click()
  check('signing out removes the stored session', !token(p) && !p.w.localStorage.getItem('tali_user_v1') && !p.w.sessionStorage.getItem('tali_tg_done'))
  p.w.close()
  backend = makeBackend({ connected: true, synced: true })
  p = await boot(backend, { wait: 1500, session: false, notice: 'expired' })
  check('without a session the gate asks for sign-in and requests no account data', gateShown(p) && !backend.db.requests.some((r) => r.path === '/auth/me' || r.path.startsWith('/connectors/') || r.path.startsWith('/extension/status')), backend.db.requests.map((r) => r.path).join(' '))
  check('after an expiry the sign-in screen explains why', /session has expired/.test(p.text('gate-tg-error')) && !p.w.sessionStorage.getItem('tali_session_notice'), p.text('gate-tg-error'))
  p.w.close()
}

// ── 6. market ticker and price chart ──
section('market ticker and price chart')
{
  const T = Date.parse('2026-11-13T19:35:00Z'), M5 = 300000
  const bar = (i, o, h, l, c, ticks = 20) => ({ t: T + i * M5, o: String(o), h: String(h), l: String(l), c: String(c), ticks })
  const m5 = [bar(2, 1.0803, 1.0808, 1.0801, 1.0806), bar(0, 1.08, 1.0805, 1.0798, 1.0802), bar(1, 1.0802, 1.0806, 1.08, 1.0803), bar(1, 1.0802, 1.0806, 1.08, 1.0803),
              bar(3, 1.0806, 1.0811, 1.0804, 1.0809), bar(4, 1.0809, 1.0812, 1.0807, 1.081), bar(5, 1.081, 1.0811, 1.0809, 1.08105, 3)]      // out of order, one duplicate
  const h1 = [0, 1, 2].map((i) => ({ t: T - (3 - i) * 3600000, o: '1.07', h: '1.09', l: '1.06', c: String(1.075 + i / 1000), ticks: 900 }))
  const market = { mode: 'on', candles: { M5: m5, H1: h1, M1: [] }, hold: {} }
  const backend = makeBackend({ connected: true, synced: true, market, legacyAccounts: [{ account_id: '900100', accountId: '900100', label: 'Legacy 100K', accountType: 'evaluation', account_type: 'evaluation', is_active: true, isActive: true }] })
  const p = await boot(backend, { wait: 2500 })
  p.w.eval("switchAccount('mtapi:7')"); await sleep(600)
  const strip = () => p.w.document.getElementById('tickerInner')
  const stripText = () => strip().textContent
  check('the ticker shows the account’s real instruments with bid / ask', /EURUSD\s*1\.08512 \/ 1\.08519/.test(stripText()) && /4100\.10 \/ 4100\.55/.test(stripText()), stripText().slice(0, 200))
  check('the price basis and the polling are stated; nothing claims streaming', /Bid \/ Ask · auto-updating about every 15 s/.test(stripText()) && !/live|stream|tick-by-tick/i.test(stripText()), stripText().slice(0, 120))
  const cells = [...strip().querySelectorAll('.tick-i')].map((n) => n.textContent)
  check('both scrolling copies carry the same content', cells.length % 2 === 0 && cells.slice(0, cells.length / 2).join('|') === cells.slice(cells.length / 2).join('|'), cells.join(' | '))
  check('no sample numbers, no percentage change, provider text escaped', !/2,341\.80|67,450|18\.34|%/.test(stripText()) && !/2,341\.80|67,450|>18\.34</.test(rawHtml) && stripText().includes('XAU<b>USD</b>') && !strip().querySelector('b'))
  const readsBefore = backend.db.marketReads.ticker
  for (let i = 0; i < 6; i++) p.w.TaliMatchTraderMarket.refreshTicker(false)
  await sleep(200)
  check('repeated refreshes inside one interval do not add requests', backend.db.marketReads.ticker === readsBefore, `${readsBefore} -> ${backend.db.marketReads.ticker}`)

  // chart
  p.w.document.querySelector('.bnav-item[data-page="chart"]').click(); await sleep(500)
  const chart = p.w.__chart
  const header = () => p.text('ph-sym') + ' | ' + p.text('ph-change')
  check('the chart is a labelled market-price chart, not account equity', /^EURUSD · Market price · M5 candles/.test(p.text('ph-sym')) && !/Equity/i.test(header()) && p.text('instSourceVal') === 'Broker candles' && /not specified/.test(p.text('instSourceSub')), header())
  const lineTimes = chart.line.data.map((d) => d.time)
  check('candles are converted from epoch milliseconds to seconds, sorted and de-duplicated', lineTimes.length === 6 && lineTimes[0] === T / 1000 && lineTimes.every((t, i) => i === 0 || t > lineTimes[i - 1]), JSON.stringify(lineTimes))
  check('line mode plots candle closes', chart.line.data[5].value === 1.08105 && chart.line.options.visible === true && chart.candle.options.visible === false)
  check('the header shows the last candle and the provider’s timing, in UTC', p.text('ph-price') === '1.08105' && /Auto-updating about every 60 s · last candle opened Nov 13, 20:00 UTC/.test(p.text('ph-change')) && p.text('chartLiveTag') === 'AUTO', p.text('ph-price') + ' ' + p.text('ph-change'))
  check('the loaded window is stated and not presented as complete history', /Showing the latest 6 M5 candles/.test(p.text('chartWindowNote')) && /Older candles are not loaded/.test(p.text('chartWindowNote')), p.text('chartWindowNote'))
  check('order entry and sample depth are hidden beside real prices', p.w.document.getElementById('orderPanel').style.display === 'none' && p.w.document.getElementById('depthCard').style.display === 'none' && /cannot place orders/.test(p.text('chartWindowNote')))
  check('the chart names the account by its readable label, never the internal key', p.text('chartAcctId') === 'Fake Broker · …0001' && !/mtapi:/.test(p.text('chartAcctId')), p.text('chartAcctId'))
  const ivButtons = [...p.w.document.querySelectorAll('#chartIntervalRow [data-interval]')]
  check('M1/M5/H1 are real buttons in a labelled group, M5 announced as selected', ivButtons.every((b) => b.tagName === 'BUTTON' && b.type === 'button') && p.$('#chartIntervalRow').getAttribute('role') === 'group' && ivButtons.map((b) => b.getAttribute('aria-pressed')).join() === 'false,true,false', ivButtons.map((b) => b.tagName + ':' + b.getAttribute('aria-pressed')).join())
  check('the candle control describes real candles in market mode', /open, high, low and close/.test(p.$('#toolCandle').getAttribute('title')) && p.$('#toolCandle').getAttribute('aria-label') === p.$('#toolCandle').getAttribute('title') && !/requires market OHLC/.test(p.$('#toolCandle').getAttribute('title')), p.$('#toolCandle').getAttribute('title'))
  p.$('#toolCandle').dispatchEvent(new p.w.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
  const c0 = chart.candle.data[0]
  check('candle mode shows real OHLC', chart.candle.options.visible === true && chart.line.options.visible === false && c0.open === 1.08 && c0.high === 1.0805 && c0.low === 1.0798 && c0.close === 1.0802 && p.w.document.getElementById('toolCandle').classList.contains('active'), JSON.stringify(c0))

  // the forming candle changes and a new candle opens: updated in place, view untouched
  market.candles.M5 = [bar(4, 1.0809, 1.0812, 1.0807, 1.081), bar(5, 1.081, 1.0816, 1.0809, 1.0814, 9), bar(6, 1.0814, 1.0815, 1.0813, 1.08145, 1)]
  const sets = chart.candle.sets, rangeSets = chart.rangeSets, fits = chart.fits
  await p.w.TaliMatchTraderMarket.refreshChart(); await sleep(100)
  check('a refresh updates the forming candle and appends the new one without redrawing', chart.candle.sets === sets && chart.candle.updates.length === 2 && chart.candle.data.length === 7 && chart.candle.data[5].close === 1.0814 && chart.candle.data[6].time === (T + 6 * M5) / 1000, JSON.stringify({ sets: chart.candle.sets, updates: chart.candle.updates.length, n: chart.candle.data.length }))
  check('pan and zoom are left alone by a refresh', chart.rangeSets === rangeSets && chart.fits === fits && chart.range.from === 3)
  check('no duplicate candles after the refresh', new Set(chart.candle.data.map((d) => d.time)).size === chart.candle.data.length)

  // interval switch while an older answer is still in flight
  let releaseM5
  market.hold.M5 = new Promise((r) => { releaseM5 = r })
  const slowRefresh = p.w.TaliMatchTraderMarket.refreshChart()            // an M5 read that will answer late
  p.w.document.querySelector('#chartIntervalRow [data-interval="H1"]').click()
  await sleep(300)
  check('the selected interval is announced after a switch', [...p.w.document.querySelectorAll('#chartIntervalRow [data-interval]')].map((b) => b.getAttribute('aria-pressed')).join() === 'false,false,true')
  check('switching interval loads that interval’s own candles', /H1 candles/.test(p.text('ph-sym')) && chart.candle.data.length === 3 && backend.db.requests.some((r) => r.path.includes('market/candles?interval=H1')), p.text('ph-sym') + ' ' + chart.candle.data.length)
  releaseM5(); market.hold = {}; await slowRefresh; await sleep(100)
  check('the late M5 answer does not overwrite the H1 chart', /H1 candles/.test(p.text('ph-sym')) && chart.candle.data.length === 3 && chart.candle.data[2].close === 1.077, p.text('ph-sym') + ' ' + JSON.stringify(chart.candle.data.map((d) => d.close)))
  p.w.document.querySelector('#chartIntervalRow [data-interval="M1"]').click(); await sleep(300)
  check('an interval with no candles yet says so instead of drawing anything', chart.candle.data.length === 0 && /Loading candles|No candles/.test(p.text('chartStateOverlay')), p.text('chartStateOverlay'))

  // closed market
  market.mode = 'closed'
  p.w.document.querySelector('#chartIntervalRow [data-interval="M5"]').click(); await sleep(300)
  p.w.TaliMatchTraderMarket.refreshTicker(true); await sleep(200)
  check('a closed market is shown as closed, with the last published prices', /Market closed · last candle opened/.test(p.text('ph-change')) && p.text('chartLiveTag') === 'CLOSED' && /market closed · last published prices/.test(stripText()) && /· closed/.test(stripText()), p.text('ph-change') + ' || ' + stripText().slice(0, 140))

  // account switch: the older account gets its own chart back and no market requests
  const marketBefore = backend.db.marketReads.ticker + backend.db.marketReads.candles
  p.w.eval("switchAccount('900100')"); await sleep(700)
  check('outside market mode the candle control says where candles are available', /available for Match-Trader API accounts/.test(p.$('#toolCandle').getAttribute('title')) && p.text('chartAcctId') === '900100', p.$('#toolCandle').getAttribute('title') + ' | ' + p.text('chartAcctId'))
  check('switching to the older account leaves market mode and restores its labels', p.w.TaliMatchTraderMarket.inMarketMode() === false && p.text('instSourceVal') === 'Trade timeline' && p.w.document.getElementById('orderPanel').style.display !== 'none' && p.w.document.getElementById('chartIntervalRow').style.display === 'none' && chart.candle.data.length === 0 && p.text('chartLiveTag') === 'LIVE', p.text('instSourceVal'))
  check('no market data is requested or shown for an account that is not an API account', backend.db.marketReads.ticker + backend.db.marketReads.candles === marketBefore && /Prices are shown for Match-Trader API accounts/.test(stripText()) && !/1\.08512/.test(stripText()), stripText().slice(0, 120))
  p.w.eval("switchAccount('mtapi:7')"); await sleep(700)
  check('switching back shows the market chart again', p.w.TaliMatchTraderMarket.inMarketMode() === true && /EURUSD · Market price/.test(p.text('ph-sym')) && chart.candle.data.length === 3, p.text('ph-sym') + ' ' + chart.candle.data.length)

  // permission denied, then feature off
  market.mode = 'denied'
  p.w.TaliMatchTraderMarket.refreshTicker(true); await p.w.TaliMatchTraderMarket.refreshChart(); await sleep(200)
  p.w.document.querySelector('#chartIntervalRow [data-interval="H1"]').click(); await sleep(300)
  check('a token without market-data permission is reported as that, on the ticker and the chart', /Market data is not permitted for this API token/.test(stripText()) && /Market data not permitted/.test(p.text('chartStateOverlay')) && /not allowed to read market data/.test(p.text('chartStateOverlay')), stripText().slice(0, 100) + ' || ' + p.text('chartStateOverlay'))
  check('account data is unaffected by the market-data problem', p.w.eval("showPage('dash'); 1") === 1 && p.text('ovBalance') === '$50,123.45', p.text('ovBalance'))
  check('no page errors during the market flow', p.allErrors().length === 0, p.allErrors().join(' | '))
  p.w.close()

  const off = makeBackend({ connected: true, synced: true })              // market feature off on the server (404)
  const q2 = await boot(off, { wait: 2500 })
  check('with the feature off the ticker says so and shows no numbers', /Market prices are not enabled on this server/.test(q2.w.document.getElementById('tickerInner').textContent) && !/\d\.\d/.test(q2.w.document.getElementById('tickerInner').textContent), q2.w.document.getElementById('tickerInner').textContent.slice(0, 120))
  q2.w.document.querySelector('.bnav-item[data-page="chart"]').click(); await sleep(500)
  check('with the feature off the chart explains it instead of showing an equity chart', /Market chart not enabled/.test(q2.text('chartStateOverlay')) && q2.allErrors().length === 0, q2.text('chartStateOverlay') + q2.allErrors().join('|'))
  q2.w.close()
}

// ── 7. analytics and older history ──
section('analytics and older history')
{
  const legacy = { account_id: '900100', accountId: '900100', label: 'Legacy 100K', accountType: 'evaluation', account_type: 'evaluation', is_active: true, isActive: true }
  const backend = makeBackend({ connected: true, synced: true, noCreationTime: true, legacyAccounts: [legacy] })
  const p = await boot(backend, { wait: 2500 })
  p.w.eval("switchAccount('mtapi:7')"); await sleep(500)
  p.w.document.querySelector('.bnav-item[data-page="analytics"]').click(); await sleep(500)
  check('the analytics page shows the server aggregate for the selected account', p.text('wr') === '40.0%' && p.text('wrSub') === '92 of 230 closed entries (18 breakeven)' && p.text('pf') === '0.82' && p.text('netpl') === '-$1,250.50' && p.text('totalTrades') === '230', [p.text('wr'), p.text('wrSub'), p.text('pf'), p.text('netpl'), p.text('totalTrades')].join(' | '))
  check('breakevens, positions, averages and best symbol are shown; provider text is escaped', p.text('wlSub') === 'W:92 L:120 BE:18 · 190 positions' && p.text('avgWin') === '+$61.20' && p.text('avgLoss') === '-$57.33' && p.text('bestSym') === 'XAU<i>USD</i>' && !p.$('#bestSym i') && p.text('greenStreak') === '3d', [p.text('wlSub'), p.text('avgWin'), p.text('bestSym')].join(' | '))
  check('the curve is labelled realized P&L, not account equity, and is drawn from the series', /Cumulative realized P&L .*not account equity/.test(p.text('curveLabel')) && /^M0\.0,/.test(p.$('#equityPath').getAttribute('d')) && p.$('#equityPath').getAttribute('d').split('L').length === 3, p.text('curveLabel') + ' ' + p.$('#equityPath').getAttribute('d'))
  check('coverage is stated next to the figures', /complete for the requested range/.test(p.text('analyticsCoverage')) && p.$('#analyticsCoverage').style.display !== 'none', p.text('analyticsCoverage'))
  check('days are grouped in the browser’s own time zone', typeof backend.db.analyticsReads.at(-1) === 'string' && backend.db.analyticsReads.at(-1).length > 0, String(backend.db.analyticsReads.at(-1)))

  // older history from the dashboard panel
  p.w.document.querySelector('.bnav-item[data-page="dash"]').click(); await sleep(300)
  check('recent trades replace the custom import controls', !p.$('#mtApiHistoryControls') && /Recent trades/.test(p.text('mtApiPanelContent')))

  // the older account: its own analytics, labels restored, no API figures
  const reads = backend.db.analyticsReads.length
  p.w.eval("switchAccount('900100')"); await sleep(800)
  check('switching to the older account restores its own labels and does not show API figures', p.text('curveLabel') === 'Equity Curve' && p.$('#analyticsCoverage').style.display === 'none' && p.text('wr') !== '40.0%' && backend.db.analyticsReads.length === reads, [p.text('curveLabel'), p.text('wr')].join(' | '))
  p.w.eval("switchAccount('mtapi:7')"); await sleep(700)
  check('switching back loads the API account’s analytics again', p.text('wr') === '40.0%' && backend.db.analyticsReads.length > reads, p.text('wr'))
  await sleep(500)
  check('recent trades for the API account load after switching back, without a sync', !/Loading history/.test(p.text('recentTradesEl')) && /XAUUSD/.test(p.text('recentTradesEl')), p.text('recentTradesEl').slice(0, 120))
  check('no page errors in the analytics flow', p.allErrors().length === 0, p.allErrors().join(' | '))
  p.w.close()
}

console.log(`\n${results.filter(Boolean).length}/${results.length} browser-flow checks passed`)
process.exit(results.every(Boolean) ? 0 : 1)
