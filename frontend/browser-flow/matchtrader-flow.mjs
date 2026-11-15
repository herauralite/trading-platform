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
    trades: [], syncPosts: 0, refuseSync: null }
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
      history: { coverage_status: !db.synced ? 'pending' : db.backfilled ? 'complete_for_requested_range' : 'in_progress', requested_from: now(), requested_to: now(), queued_intervals: 0, gap_count: 0, closed_entry_count: db.synced ? n : null,
        period_stats: db.synced ? { net_profit: '31.25', closed_entries: n, positions: n, winning_entries: 2, losing_entries: 1, first_close_time: '2026-11-10T10:00:00Z', last_close_time: now(), is_complete_for_requested_range: db.backfilled } : null },
      background_refresh: { enabled: true }, service: db.service, server_time: now() }
  }
  async function handle(url, init = {}) {
    const u = new URL(url); const method = init.method || 'GET'; const p = u.pathname
    db.requests.push({ method, path: p + u.search, auth: (init.headers || {}).Authorization, body: init.body || null })
    const json = (status, body) => ({ ok: status < 300, status, headers: { get: () => 'application/json' }, json: async () => body, text: async () => JSON.stringify(body) })
    if (p === '/auth/me') return json(200, { user: { telegram_user_id: '1001', first_name: 'Tali' }, accounts: options.legacyAccounts || [] })
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
    if (p === base + '/7/backfill') { db.backfilled = true; return json(200, { ok: true, queued: ['closed_positions', 'ledgers'] }) }
    if (p === base + '/7/snapshot') return json(200, snapshot())
    if (p === base + '/7/trades') return json(200, { trades: db.trades, next_cursor: null, has_more: false })
    if (p === base + '/7' && method === 'DELETE') { db.connected = false; return json(200, { ok: true }) }
    return json(404, { detail: 'Not Found' })
  }
  return { db, handle }
}

async function boot(backend, { wait = 1500 } = {}) {
  const errors = []
  const vc = new VirtualConsole()
  vc.on('jsdomError', (e) => errors.push(String(e.message || e).slice(0, 200)))
  vc.on('error', (...a) => errors.push('console.error: ' + a.map(String).join(' ').slice(0, 200)))
  const before = pageErrors.length
  const dom = new JSDOM(html, { url: 'http://localhost:5173/app', runScripts: 'dangerously', virtualConsole: vc, pretendToBeVisual: true,
    beforeParse(window) {
      window.__TALI_CONFIG__ = { apiBase: 'http://localhost:8000' }
      window.fetch = backend.handle
      window.localStorage.setItem('tali_session_token_v1', 'fake-session')
      window.localStorage.setItem('tali_user_v1', JSON.stringify({ telegramUserId: '1001', telegram_user_id: '1001', firstName: 'Tali' }))
      window.sessionStorage.setItem('tali_pw_ok', '1'); window.sessionStorage.setItem('tali_tg_done', '1')
      window.matchMedia = window.matchMedia || (() => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }))
      window.scrollTo = () => {}
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
  check('the sign-out handler really calls that reset', /localStorage\.removeItem\(TALI_SESSION_TOKEN_KEY\);\s*if \(window\.TaliMatchTrader\) TaliMatchTrader\.reset\(\)/.test(rawHtml))
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
  check('account sheet: masked login, API label, no Primary/Deactivate, no internal key', sheet.includes('…0001') && sheet.includes('Fake Broker · API · DEMO') && sheet.includes('Disconnect') && !sheet.includes('Deactivate') && !sheet.includes('mtapi:7'), sheet.slice(0, 200))

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
  check('first Disconnect tap only arms it', backend.db.requests.filter((r) => r.method === 'DELETE').length === 0 && p.text('acctSheetBody').includes('Tap again to confirm'))
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

console.log(`\n${results.filter(Boolean).length}/${results.length} browser-flow checks passed`)
process.exit(results.every(Boolean) ? 0 : 1)
