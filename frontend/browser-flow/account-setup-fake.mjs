// Scripted fake TaliTrade backend for the account-setup browser flow. Same rules as the real
// /account-setup routes (session user only, 404 for accounts the user does not own, optimistic
// versions with a 409 for an older base, replayed save ids answered with the stored result) and
// just enough of the Match-Trader routes for the dashboard. Synthetic data only; no network.
export const USERS = { 'session-alice': '1001', 'session-alice-device-2': '1001', 'session-bob': '2002' }
const SAMPLES = {
  fp: { broker: 'FundingPips', login: '1842', currency: 'USD', type: 'REAL', balance: '49820.00', equity: '49820.00', outcome: 'completed' },
  eu: { broker: 'Sample Broker', login: '7730', currency: 'EUR', type: 'DEMO', balance: '10214.50', equity: '10198.10', outcome: 'completed' },
  bad: { broker: 'FundingPips', login: '5567', currency: 'USD', type: 'DEMO', balance: '25010.00', equity: '25010.00', outcome: 'failed' },
  empty: { broker: 'FundingPips', login: '3301', currency: 'USD', type: 'REAL', balance: '100000.00', equity: '100000.00', outcome: 'completed' },
}
// Closed-trade history per sample: recent (inside the 7-day import), older, and the account's age in days.
const HISTORY = { fp: { recent: 12, older: 560, ageDays: 430 }, eu: { recent: 3, older: 41, ageDays: 190 }, bad: { recent: 0, older: 25, ageDays: 60 }, empty: { recent: 0, older: 0, ageDays: 90 } }
export const TOKENS = { fp: 'synthetic-token-fp-0000000000000001', eu: 'synthetic-token-eu-0000000000000002', bad: 'synthetic-token-bad-000000000000003', empty: 'synthetic-token-empty-00000000000004' }

export function createServer({ setupEnabled = true } = {}) {
  const s = { setupEnabled, accounts: [], profiles: {}, nextId: 1, puts: [], setupRequests: [], failNextPut: 0, holdPut: null, now: () => Date.now(),
    importPosts: [], importSteps: 3, failImport: {} }
  const DAYMS = 86400000
  // The server's import, simplified: each read after a start moves one step back through time
  // (newest first) and commits that step's trades. Counts and dates only ever come from here.
  const importStatus = (a) => {
    const h = HISTORY[a.sample], created = a.connectedAt - h.ageDays * DAYMS, to = a.connectedAt
    const base = { connection_id: a.id, earliest_boundary: a.imp ? 'account_created_at' : null, account_created_at: iso(created), target_from: a.imp ? iso(created) : null,
      window_limited: false, gaps: [], retention_known: false, attempts: a.imp ? a.imp.attempts : 0, queued_ranges: 0 }
    const sevenFrom = to - 7 * DAYMS
    if (!a.imp) return { ...base, state: 'not_requested', reason: null, closed_trades: h.recent, checked_from: iso(sevenFrom), checked_to: iso(to), first_trade_at: null }
    const step = Math.min(a.imp.step, s.importSteps), frac = step / s.importSteps
    const checkedFrom = Math.round(sevenFrom - frac * (sevenFrom - created))
    const count = h.recent + Math.floor(frac * h.older)
    const done = step >= s.importSteps
    let state = done ? (count ? 'imported' : 'empty') : 'importing', reason = null
    if (!done && s.failImport[a.id] && step >= s.failImport[a.id]) { state = 'interrupted'; reason = 'transient' }
    return { ...base, state, reason, closed_trades: count, checked_from: iso(done ? created : checkedFrom), checked_to: iso(to), first_trade_at: count ? iso(done ? created + 3 * DAYMS : checkedFrom) : null, queued_ranges: done ? 0 : 1 }
  }
  const iso = (ms) => new Date(ms).toISOString()
  const SERVICE = () => ({ secure_storage: true, worker: true, connect_enabled: true, accepting_connections: true, market_data: false, account_setup: s.setupEnabled })
  const phase = (a) => { const age = s.now() - a.connectedAt; return age < 1500 ? 'queued' : age < 4000 ? 'running' : a.outcome }
  const syncedAt = (a) => (a.outcome === 'completed' && phase(a) === 'completed' ? a.connectedAt + 4000 : null)
  const request = (a) => { const p = phase(a); return { id: a.requestId, state: p, detail: p === 'failed' ? 'broker_unavailable' : null, resources: {}, pending: p === 'queued' || p === 'running', requested_at: iso(a.connectedAt), started_at: null, finished_at: null } }
  const card = (a) => { const at = syncedAt(a); return { id: a.id, account_key: 'mtapi:' + a.id, connector_type: 'matchtrader_api', broker_name: a.broker, display_label: a.broker + ' · …' + a.login, masked_login: '…' + a.login,
    currency: a.currency, provider_account_type: a.type, status: 'active', display_state: at ? 'ready' : 'first_sync_pending', balance: at ? a.balance : null, last_synced_at: at ? iso(at) : null } }
  const fresh = (at) => ({ status: at ? 'fresh' : 'never_synced', last_success_at: at ? iso(at) : null, last_attempt_at: at ? iso(at) : null, last_error: null, is_stale: false })
  const snapshot = (a) => { const at = syncedAt(a); return { connection: card(a), sync_request: request(a),
    summary: at ? { balance: a.balance, equity: a.equity, free_funds: a.equity, margin: '0', profit: '0', credit: '0', margin_level: null, fetched_at: iso(at) } : null,
    positions: at ? [] : null, pending_orders: at ? [] : null, freshness: { summary: fresh(at), positions: fresh(at), pending_orders: fresh(at), closed_positions: fresh(at), ledgers: fresh(at) },
    history: { coverage_status: at ? 'complete_for_requested_range' : 'pending', requested_from: iso(a.connectedAt), requested_to: iso(a.connectedAt), queued_intervals: 0, gap_count: 0, closed_entry_count: at ? 0 : null, period_stats: null },
    background_refresh: { enabled: true }, service: SERVICE(), server_time: iso(s.now()) } }
  const profileKey = (user, a) => user + '|' + a.login
  const detected = (a) => { const at = syncedAt(a); return { broker: a.broker, masked_login: '…' + a.login, currency: a.currency, reported_account_type: a.type, balance: at ? a.balance : null, equity: at ? a.equity : null, observed_at: at ? iso(at) : null,
    not_supplied: ['starting_size', 'evaluation_or_funded', 'evaluation_phase', 'profit_target', 'loss_limits', 'payout_rules'] } }
  const json = (status, body) => ({ ok: status >= 200 && status < 300, status, headers: { get: () => 'application/json' }, json: async () => body, text: async () => JSON.stringify(body) })

  // An account that was linked before this page loaded (e.g. before account setup existed).
  s.addLinked = (user, sample) => { const a = { id: s.nextId++, user, sample, ...SAMPLES[sample], connectedAt: s.now() - 60000, requestId: 1, imp: null }; s.accounts.push(a); return a }

  s.handle = async (url, init = {}) => {
    const u = new URL(url), p = u.pathname, method = init.method || 'GET'
    const tok = String((init.headers || {}).Authorization || '').replace('Bearer ', ''), user = USERS[tok]
    if (p === '/auth/telegram/config') return json(200, { ok: true, config: { botUsername: 'TestBot', loginDomain: 'localhost', authMode: 'legacy_widget', hasBotToken: false } })
    if (p === '/auth/me') return user ? json(200, { user: { telegram_user_id: user, first_name: user === '1001' ? 'Alice' : 'Bob' }, accounts: [] }) : json(401, { detail: 'Session token expired' })
    if (p === '/extension/news') return json(200, { events: [] })
    if (p === '/extension/status') return json(200, { accounts: {}, count: 0 })
    if (!user) return json(401, { detail: 'Missing authenticated session' })
    const mine = s.accounts.filter((a) => a.user === user && !a.gone)
    if (p.startsWith('/account-setup')) {
      s.setupRequests.push({ method, path: p, user })
      if (!s.setupEnabled) return json(404, { detail: 'Not Found' })
      if (p === '/account-setup/profiles') return json(200, { profiles: mine.map((a) => { const r = s.profiles[profileKey(user, a)]; return { connection_id: a.id, version: r ? r.version : 0, profile: r ? r.data : null } }) })
      const m = /^\/account-setup\/connections\/(\d+)$/.exec(p), a = m && mine.find((x) => x.id === Number(m[1]))
      if (!a) return json(404, { detail: 'Account not found' })
      const key = profileKey(user, a), row = s.profiles[key] || { version: 0, data: null, lastSaveId: null }
      if (method === 'GET') return json(200, { connection_id: a.id, version: row.version, profile: row.data, detected: detected(a) })
      if (method === 'PUT') {
        const body = JSON.parse(init.body)
        s.puts.push({ user, id: a.id, body })
        if (s.holdPut) await s.holdPut
        if (s.failNextPut > 0) { s.failNextPut--; return json(503, { detail: 'Service temporarily unavailable' }) }
        if (row.lastSaveId === body.save_id) return json(200, { version: row.version, profile: row.data, replayed: true })
        if (row.version !== body.base_version) return json(409, { detail: 'These answers were changed in another tab or on another device.', version: row.version, profile: row.data })
        s.profiles[key] = { version: row.version + 1, data: JSON.parse(JSON.stringify(body.profile)), lastSaveId: body.save_id }
        return json(200, { version: row.version + 1, profile: body.profile, replayed: false })
      }
    }
    const BASE = '/connectors/matchtrader-api/connections'
    if (p === BASE && method === 'GET') return json(200, { service: SERVICE(), connections: mine.map((a) => ({ id: a.id, card: card(a) })) })
    if (p === BASE && method === 'POST') {
      const tokenSent = JSON.parse(init.body).token, sample = Object.keys(TOKENS).find((k) => TOKENS[k] === tokenSent)
      if (!sample) return json(422, { detail: 'Match-Trader rejected this token. Create a new read-only token and try again.' })
      const a = { id: s.nextId++, user, sample, ...SAMPLES[sample], connectedAt: s.now(), requestId: 1, imp: null }
      s.accounts.push(a)
      return json(200, { ok: true, connection: { id: a.id, status: 'active' }, card: card(a), sync_request: request(a), initial_backfill: { days: 7, resources: ['closed_positions', 'ledgers'] }, timings: {} })
    }
    const m = /^\/connectors\/matchtrader-api\/connections\/(\d+)(\/.*)?$/.exec(p), a = m && mine.find((x) => x.id === Number(m[1]))
    if (!a) return json(404, { detail: 'Connection not found' })
    const rest = m[2] || ''
    if (rest === '/history-import' && method === 'POST') {
      const retry = !!(init.body && JSON.parse(init.body).retry)
      s.importPosts.push({ id: a.id, user, retry })
      if (!a.imp) a.imp = { step: 0, attempts: 1 }
      else if (retry) { a.imp.attempts++; delete s.failImport[a.id] }
      return json(200, importStatus(a))
    }
    if (rest === '/history-import' && method === 'GET') {
      if (a.imp && !(s.failImport[a.id] && a.imp.step >= s.failImport[a.id])) a.imp.step++
      return json(200, importStatus(a))
    }
    if (rest === '/snapshot') return json(200, snapshot(a))
    if (rest === '/sync' && method === 'GET') return json(200, { request: request(a) })
    if (rest === '/sync' && method === 'POST') {
      if (!request(a).pending) { a.connectedAt = s.now() - 1500; a.outcome = 'completed'; a.requestId++ }   // the retry succeeds
      return json(202, { accepted: true, already_pending: false, request: request(a) })
    }
    if (rest === '/trades') return json(200, { trades: [], next_cursor: null, has_more: false })
    if (rest === '/backfill') return json(200, { ok: true, queued: [], requested_from: iso(a.connectedAt), clamped_to_account_creation: false })
    if (rest === '/analytics') return json(200, { connection_id: a.id, currency: a.currency, summary: { net_profit: '0', closed_entries: 0, entries_with_result: 0, entries_without_result: 0, positions: 0, winning_entries: 0, losing_entries: 0, breakeven_entries: 0 },
      cumulative_realized: [], coverage: { status: 'complete_for_requested_range', gap_count: 0 }, counting_rules: {}, not_derived: [] })
    return json(404, { detail: 'Not Found' })
  }
  return s
}
