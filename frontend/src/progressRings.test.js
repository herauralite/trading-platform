// Progress circles on the account card and clearing optional rules in the edit sheet.
// Runs the real tali-account-setup.js in jsdom against a fake host and a fake /account-setup server.
// Synthetic data only; no layout or animation.
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import jsdomPkg from 'jsdom'
const { JSDOM } = jsdomPkg

const here = path.dirname(fileURLToPath(import.meta.url))
const src = fs.readFileSync(path.join(here, '../public/tali-account-setup.js'), 'utf8')
const tick = (ms = 0) => new Promise((r) => setTimeout(r, ms))
const until = async (fn, ms = 3000) => { const t = Date.now(); while (Date.now() - t < ms) { if (fn()) return true; await tick(10) } return false }
const clone = (o) => JSON.parse(JSON.stringify(o))
const pct = (v) => ({ value: String(v), unit: 'percent', base: 'starting' })
const amt = (v) => ({ value: String(v), unit: 'amount', base: 'starting' })

const BASE_PROFILES = {
  1: { status: 'saved', type: 'evaluation', size: { amount: '100000', currency: 'USD' }, evaluation: { phaseCount: 1, phaseTarget: pct(8) },
    firmRules: { dailyLoss: pct(5), dailyReset: '17:00', dailyResetZone: 'America/New_York', overall: pct(10), overallMode: 'static', overallRef: 'balance' },
    personal: { sessionBudget: amt(500), riskPerTrade: pct(0.5) }, done: { basics: true, phase: true, firm: true, personal: true }, draft: {} },
  2: { status: 'saved', type: 'master', size: { amount: '2000000', currency: 'USD' }, master: { goal: { id: 'payout', text: '' }, payoutTarget: pct(5), firmMin: pct(1), profitSplit: '80' },
    firmRules: { dailyLoss: pct(2), dailyReset: '17:00', dailyResetZone: 'America/New_York' }, done: { basics: true, payout: true, firm: true }, draft: {} },
  3: { status: 'saved', type: 'master', size: { amount: '25000', currency: 'USD' }, master: { goal: { id: 'limits', text: '' } }, personal: { sessionBudget: amt(300) }, done: { basics: true, personal: true }, draft: {} },
}
const target = (o) => ({ id: 'phase_target', kind: 'target', group: 'target', title: 'Profit target', source: 'Firm target', entered: pct(8),
  measurable: true, status: 'in_progress', status_label: 'In progress', tone: 'cyan', target: '8000.00', current: '3252.90', remaining: '4747.10', fraction: 0.4066, ratio_percent: '40.7',
  baseline: { kind: 'starting_size', amount: '100000.00' }, definition: 'target', ...o })
const daily = (o) => ({ id: 'daily_loss', kind: 'limit', group: 'firm', title: 'Daily loss limit', source: 'Firm rule', entered: pct(2),
  measurable: true, status: 'breached', status_label: 'Limit breached', tone: 'red', used: '52000.00', limit: '40000.00', remaining: '-12000.00', fraction: 1, ratio_percent: '130.0',
  reset: '17:00', reset_zone: 'America/New_York', realized_since_reset: '-52000.00', open_pnl: '0.00', reference: {}, definition: 'daily_loss', ...o })
const overall = (o) => ({ id: 'overall_drawdown', kind: 'limit', group: 'firm', title: 'Static drawdown limit', source: 'Firm rule', entered: pct(10), measurable: true, status: 'within',
  status_label: 'Within limit', tone: 'green', used: '0.00', limit: '10000.00', remaining: '10000.00', fraction: 0, ratio_percent: '0.0', mode: 'static', floor: '90000.00', measured_on: 'balance', reference: {}, ...o })
const budget = (o) => ({ id: 'session_budget', kind: 'limit', group: 'personal', title: 'Session loss budget', source: 'Your limit', measurable: false, status: 'unavailable',
  status_label: 'Not measured', tone: 'neutral', reason: 'Session losses are not measured.', action: { id: 'flow', flow: 'personal', label: 'Edit personal limits' }, ...o })
const payout = (o) => target({ id: 'payout_target', title: 'Your payout target', source: 'Your target', target: '100000.00', current: '-71200.00', remaining: '171200.00',
  fraction: 0, ratio_percent: '-71.2', status: 'unconfirmed', status_label: 'Unconfirmed', tone: 'neutral', unconfirmed_reason: "Deposit and withdrawal history isn't imported back to this baseline yet.", ...o })
const firmMin = (o) => target({ id: 'firm_min', title: 'Firm minimum profit for payout', source: 'Firm rule', target: '20000.00', current: '-71200.00', remaining: '91200.00', fraction: 0, ratio_percent: '-356.0',
  status: 'below_start', status_label: 'Below starting point', tone: 'amber', ...o })
const risk = (o) => ({ id: 'risk_per_trade', kind: 'limit', group: 'personal', title: 'Risk per trade', source: 'Your limit', measurable: true, status: 'estimate_at', status_label: 'Estimate at limit',
  tone: 'amber', used: '500.00', limit: '500.00', remaining: '0.00', fraction: 1, ratio_percent: '100.0', worst_symbol: 'XAUUSD', estimate: true, unmeasured_count: 1,
  unmeasured: [{ symbol: 'US30', reason: 'no_stop', reason_text: 'no stop loss' }], ...o })
const riskIdle = () => ({ id: 'risk_per_trade', kind: 'limit', title: 'Risk per trade', source: 'Your limit', measurable: false, status: 'idle', status_label: 'No open trades', tone: 'neutral', reason: 'None are open now.' })

// A server whose answers persist across page loads in one test; ``failNext`` makes the next save fail.
function makeServer() {
  const db = { profiles: clone(BASE_PROFILES), versions: { 1: 1, 2: 1, 3: 1 }, puts: [], failNext: 0 }
  db.handle = async (url, init = {}) => {
    const u = String(url), json = (status, b) => ({ ok: status < 400, status, json: async () => b, text: async () => JSON.stringify(b) })
    if (u.endsWith('/account-setup/profiles')) return json(200, { profiles: Object.entries(db.profiles).map(([id, p]) => ({ connection_id: Number(id), version: db.versions[id], profile: p })) })
    const m = /account-setup\/connections\/(\d+)$/.exec(u)
    if (m && (init.method || 'GET') === 'GET') return json(200, { version: db.versions[m[1]], profile: db.profiles[m[1]], detected: { balance: '1000' } })
    if (m && init.method === 'PUT') {
      const body = JSON.parse(init.body)
      db.puts.push(body.profile)
      if (db.failNext) { db.failNext--; return json(503, { detail: 'Saving is unavailable right now.' }) }
      if (body.base_version !== db.versions[m[1]]) return json(409, { detail: 'conflict', version: db.versions[m[1]], profile: db.profiles[m[1]] })
      db.versions[m[1]]++; db.profiles[m[1]] = body.profile
      return json(200, { version: db.versions[m[1]], profile: body.profile })
    }
    return json(404, {})
  }
  return db
}

function setup(db = makeServer()) {
  const dom = new JSDOM('<!doctype html><body><div id="card" class="card"></div></body>', { url: 'http://localhost/app', runScripts: 'outside-only', pretendToBeVisual: true })
  const w = dom.window
  const progress = {
    'mtapi:1': { currency: 'USD', rows: [target(), daily({ status: 'within', status_label: 'Within limit', tone: 'green', used: '160.00', limit: '5000.00', remaining: '4840.00', fraction: 0.032, ratio_percent: '3.2' }), overall(), budget(), risk()], labels: [], definitions: { target: 'Closed profit since the baseline.', daily_loss: 'Closed and open losses since the reset.' } },
    'mtapi:2': { currency: 'USD', rows: [payout(), firmMin(), daily()], labels: [{ title: 'Trading style', value: 'Swing' }], definitions: { daily_loss: 'Closed and open losses since the reset.' } },
    'mtapi:3': { currency: 'USD', rows: [budget(), riskIdle()], labels: [] },
  }
  let active = 'mtapi:1'
  w.fetch = db.handle
  w.eval(src)
  const host = {
    getUserId: () => 'u1', getActiveAccount: () => active, isEnabled: () => true, getSessionToken: () => 'session-1', getApiBase: () => 'http://api.test',
    getAccount: (k) => ({ broker: k === 'mtapi:2' ? 'Sample Prop Firm International Funded Trader Programme' : 'FundingPips', ending: k.slice(-1).repeat(4), currency: 'USD', accountType: 'DEMO' }),
    getSnapshot: () => ({ summary: { balance: '1928800.00', fetched_at: '2026-11-18T14:00:00Z' } }),
    getProgress: (k) => progress[k], refreshSwitcher() {}, isBusy: () => false, onProfileSaved() {},
  }
  w.TaliAccountSetup.attach(host)
  const card = w.document.getElementById('card')
  card.id = 'mtApiSetupCard'
  const $ = (q) => w.document.querySelector(q)
  const t = {
    w, card, progress, db, $, host,
    async render(k = active) { active = k; w.TaliAccountSetup.renderCard(card, k); await until(() => !/Loading account setup/.test(card.textContent)); w.TaliAccountSetup.renderCard(card, k) },
    rings: () => [...card.querySelectorAll('.pg-ring')],
    ids: () => t.rings().map((b) => b.dataset.id),
    ring: (id) => card.querySelector('.pg-ring[data-id="' + id + '"]'),
    selected: () => t.rings().filter((b) => b.classList.contains('is-on')).map((b) => b.dataset.id),
    drawer: () => card.querySelector('.pg-drawer'),
    detail: () => (card.querySelector('.pg-detail') || {}).textContent || '',
    key: (el, key) => el.dispatchEvent(new w.KeyboardEvent('keydown', { key, bubbles: true })),
    // the edit sheet
    open: async (k, flow, step) => { active = k; w.TaliAccountSetup.open(k, flow, step); await until(() => !!$('#setup-sheet.open') && !!$('#setupPrimary')) },
    q: () => ($('#setupQuestion') || {}).textContent || '',
    type: (sel, v) => { const i = $(sel); i.value = v; i.dispatchEvent(new w.Event('input', { bubbles: true })) },
    primary: () => $('#setupPrimary'),
    next: async () => {
      const before = db.puts.length, b = $('#setupPrimary')
      if (!b || b.disabled) throw new Error('Continue is disabled on: ' + (($('#setupQuestion') || {}).textContent || ''))
      b.click()
      await until(() => db.puts.length > before || !!$('#setup-sheet [data-act="retry"]'))
      await until(() => !$('#setup-sheet.open') || (($('#setupPrimary') || {}).textContent || '') !== 'Saving…'); await tick(5)
    },
    skip: async () => { const before = db.puts.length; $('#setup-sheet [data-act="skip"]').click(); await until(() => db.puts.length > before); await tick(20) },
    sheetOpen: () => !!$('#setup-sheet.open'),
    saved: (id) => db.profiles[id],
  }
  return t
}

test('refresh halo only runs for a newer fresh snapshot, never first load or repeated data', async () => {
  const t = setup()
  let stamp = new Date(Date.now() - 10000).toISOString()
  t.host.getSnapshot = () => ({summary:{balance:'100000',fetched_at:stamp}})
  await t.render()
  assert.equal(t.$('.su-ring-space').classList.contains('is-refreshed'), false)
  stamp = new Date(Date.now()).toISOString()
  await t.render()
  assert.equal(t.$('.su-ring-space').classList.contains('is-refreshed'), true)
  t.$('.su-ring-space').classList.remove('is-refreshed')
  await t.render()
  assert.equal(t.$('.su-ring-space').classList.contains('is-refreshed'), false)
  t.w.matchMedia = () => ({matches:true})
  stamp = new Date(Date.now() + 1000).toISOString()
  await t.render()
  assert.equal(t.$('.su-ring-space').classList.contains('is-refreshed'), false)
  t.w.close()
})

test('only saved rules with a usable figure get a circle; a real 0% stays', async () => {
  const t = setup()
  await t.render('mtapi:1')
  assert.deepEqual(t.ids(), ['phase_target', 'daily_loss', 'overall_drawdown', 'risk_per_trade'], 'the unmeasurable session budget has no circle')
  assert.deepEqual(t.rings().map((b) => b.querySelector('.pg-val').textContent), ['41%', '3%', '0%', '100%'])
  assert.equal(t.ring('overall_drawdown').querySelector('.pg-arc'), null, 'a real zero draws an empty track, not a dashed placeholder')
  assert.equal(t.card.querySelectorAll('.is-unknown').length, 0)
  assert.doesNotMatch(t.card.textContent, /—/)
  assert.equal(t.card.querySelector('.pg-bar'), null)
  assert.match(t.ring('phase_target').getAttribute('aria-label'), /target, 40\.7% complete\. In progress/)
  assert.match(t.ring('daily_loss').getAttribute('aria-label'), /limit, 3\.2% of the limit used\. Within limit/)
  assert.match(t.ring('risk_per_trade').getAttribute('aria-label'), /estimate\. Estimate at limit/, 'estimates keep their qualification')
  assert.equal(t.ring('risk_per_trade').querySelector('.pg-state').textContent, 'Estimate at limit')
  assert.ok(t.card.querySelector('[data-setup="personal"]'), 'the hidden rule stays editable from Personal risk limits')
  assert.ok(t.rings().every((b) => b.tagName === 'BUTTON' && b.type === 'button' && b.getAttribute('aria-expanded') === 'false'))
})

test('a rule the server still reports but the trader never saved gets no circle', async () => {
  const db = makeServer()
  delete db.profiles[2].master.firmMin
  const t = setup(db)
  await t.render('mtapi:2')
  assert.deepEqual(t.ids(), ['payout_target', 'daily_loss'])
})

test('no eligible rules: no circles area at all', async () => {
  const t = setup()
  await t.render('mtapi:3')
  assert.equal(t.card.querySelector('.pg-rings'), null)
  assert.equal(t.card.querySelector('.pg-drawer'), null)
  assert.doesNotMatch(t.card.textContent, /Targets and limits/)
  assert.ok(t.card.querySelector('[data-setup="personal"]'))
})

test('negative, exceeded and unconfirmed figures keep their real values and labels', async () => {
  const t = setup()
  await t.render('mtapi:2')
  assert.match(t.card.querySelector('.su-card-title').textContent, /International Funded Trader Programme/)
  assert.equal(t.ring('payout_target').querySelector('.pg-val').textContent, '−71%')
  assert.equal(t.ring('payout_target').querySelector('.pg-arc'), null, 'negative progress draws no fill')
  assert.equal(t.ring('payout_target').querySelector('.pg-state').textContent, 'Unconfirmed')
  assert.equal(t.ring('firm_min').querySelector('.pg-val').textContent, '−356%')
  assert.equal(t.ring('daily_loss').querySelector('.pg-val').textContent, '130%')
  assert.equal(t.ring('daily_loss').querySelector('.pg-arc').style.strokeDashoffset, '0', 'the fill stops at a full circle')
  t.ring('daily_loss').click()
  assert.match(t.detail(), /\$52,000 used of \$40,000 limit/)
  assert.match(t.detail(), /130% of the limit used · \$12,000 past the limit/)
  t.ring('payout_target').click()
  assert.match(t.detail(), /−\$71,200 of \$100,000 target/)
  assert.match(t.detail(), /isn't imported back to this baseline/)
})

test('tap opens one circle, tap again closes, another replaces it', async () => {
  const t = setup()
  await t.render('mtapi:1')
  t.ring('daily_loss').click()
  assert.deepEqual(t.selected(), ['daily_loss'])
  assert.equal(t.ring('daily_loss').getAttribute('aria-expanded'), 'true')
  assert.equal(t.ring('daily_loss').getAttribute('aria-controls'), t.drawer().id)
  assert.ok(t.drawer().classList.contains('is-open') && !t.drawer().hasAttribute('inert'))
  assert.match(t.detail(), /\$160 used of \$5,000 limit/)
  t.ring('risk_per_trade').click()
  assert.deepEqual(t.selected(), ['risk_per_trade'])
  assert.match(t.detail(), /Estimate: XAUUSD isn't priced in USD/)
  assert.match(t.detail(), /Not measured: US30 \(no stop loss\)/)
  t.ring('risk_per_trade').click()
  assert.deepEqual(t.selected(), [])
  assert.ok(!t.drawer().classList.contains('is-open') && t.drawer().hasAttribute('inert'))
})

test('routine refreshes keep the DOM, focus, selection and open explanation', async () => {
  const t = setup()
  await t.render('mtapi:1')
  t.ring('daily_loss').click()
  t.ring('daily_loss').focus()
  const node = t.ring('daily_loss')
  await t.render('mtapi:1')
  assert.equal(t.ring('daily_loss'), node, 'same figures: nothing is redrawn')
  t.card.querySelector('.pg-help').open = true
  t.progress['mtapi:1'].rows[1] = daily({ status: 'within', status_label: 'Within limit', tone: 'green', used: '260.00', limit: '5000.00', remaining: '4740.00', fraction: 0.052, ratio_percent: '5.2' })
  await t.render('mtapi:1')
  assert.notEqual(t.ring('daily_loss'), node)
  assert.deepEqual(t.selected(), ['daily_loss'])
  assert.match(t.detail(), /\$260 used/)
  assert.equal(t.w.document.activeElement, t.ring('daily_loss'))
  assert.ok(t.card.querySelector('.pg-help').open)
  t.ring('daily_loss').click()
  t.progress['mtapi:1'].rows[1] = daily({ status: 'within', status_label: 'Within limit', tone: 'green', used: '300.00', limit: '5000.00', remaining: '4700.00', fraction: 0.06, ratio_percent: '6.0' })
  await t.render('mtapi:1')
  assert.deepEqual(t.selected(), [], 'a collapsed circle is not reopened by a refresh')
})

test('a selected circle that becomes unmeasurable closes and disappears; focus moves only if it was there', async () => {
  const t = setup()
  await t.render('mtapi:1')
  t.ring('overall_drawdown').click()
  t.ring('overall_drawdown').focus()
  t.progress['mtapi:1'].rows[2] = overall({ measurable: false, status: 'pending', status_label: 'Waiting for data', fraction: undefined, ratio_percent: undefined })
  await t.render('mtapi:1')
  assert.equal(t.ring('overall_drawdown'), null)
  assert.deepEqual(t.selected(), [])
  assert.equal(t.card.querySelector('.pg-drawer.is-open'), null)
  assert.equal(t.w.document.activeElement, t.ring('risk_per_trade'), 'focus goes to the circle now in its place')
  // focus elsewhere: a disappearing circle doesn't move it
  t.ring('daily_loss').click()
  const edit = t.card.querySelector('[data-setup="edit"]'); edit.focus()
  t.progress['mtapi:1'].rows[1] = daily({ measurable: false, status: 'unavailable', status_label: 'Not measured', fraction: undefined, ratio_percent: undefined })
  await t.render('mtapi:1')
  assert.equal(t.ring('daily_loss'), null)
  assert.equal(t.w.document.activeElement.dataset.setup, 'edit')
  // the last circle goes: the area goes, focus lands on the card's own action
  t.ring('risk_per_trade').focus()
  t.progress['mtapi:1'].rows = [budget()]
  await t.render('mtapi:1')
  assert.equal(t.card.querySelector('.pg-rings'), null)
  assert.ok(['edit', 'next'].includes(t.w.document.activeElement.dataset.setup), t.w.document.activeElement.outerHTML)
})

test('each account keeps its own selection; switching never shows another account’s details', async () => {
  const t = setup()
  await t.render('mtapi:2')
  t.ring('daily_loss').click()
  await t.render('mtapi:1')
  assert.deepEqual(t.selected(), [])
  assert.doesNotMatch(t.card.textContent, /52,000|Funded Trader Programme/)
  t.ring('phase_target').click()
  await t.render('mtapi:2')
  assert.deepEqual(t.selected(), ['daily_loss'])
  await t.render('mtapi:1')
  assert.deepEqual(t.selected(), ['phase_target'])
})

test('keyboard: arrows move between circles, Escape closes and returns to the circle', async () => {
  const t = setup()
  await t.render('mtapi:1')
  t.ring('phase_target').focus()
  t.key(t.ring('phase_target'), 'ArrowRight')
  assert.equal(t.w.document.activeElement, t.ring('daily_loss'))
  t.key(t.ring('daily_loss'), 'End')
  assert.equal(t.w.document.activeElement, t.ring('risk_per_trade'))
  t.key(t.ring('risk_per_trade'), 'ArrowRight')
  assert.equal(t.w.document.activeElement, t.ring('phase_target'))
  t.ring('daily_loss').click()
  const summary = t.card.querySelector('.pg-help summary')
  summary.focus()
  t.key(summary, 'Escape')
  assert.deepEqual(t.selected(), [])
  assert.equal(t.w.document.activeElement, t.ring('daily_loss'))
})

// ── clearing optional rules through the edit sheet ──
test('emptying the session budget removes it; the risk limit is untouched; it stays removed after reload', async () => {
  const t = setup()
  await t.render('mtapi:1')
  await t.open('mtapi:1', 'personal')
  assert.equal(t.$('#su-in-sessionBudget').value, '500')
  t.type('#su-in-sessionBudget', '')
  assert.match(t.$('#su-note-sessionBudget').textContent, /saved \$500 is removed when you continue\. Skip keeps it/)
  assert.ok(!t.primary().disabled)
  await t.next()
  assert.equal(t.db.puts.at(-1).personal.sessionBudget, undefined, 'the removal is sent to the server')
  assert.equal(Number(t.db.puts.at(-1).personal.riskPerTrade.value), 0.5)
  while (t.sheetOpen()) await t.next()
  assert.equal(t.saved(1).personal.sessionBudget, undefined)
  assert.ok(t.saved(1).personal.riskPerTrade)
  assert.doesNotMatch(t.card.textContent, /session loss budget/i)
  const again = setup(t.db)                       // a fresh page load against the same server
  await again.render('mtapi:1')
  await again.open('mtapi:1', 'personal')
  assert.equal(again.$('#su-in-sessionBudget').value, '')
  assert.equal(again.$('#su-in-riskPerTrade').value, '0.5')
})

test('emptying in the other unit view is a clear too; Skip keeps the saved value', async () => {
  const t = setup()
  await t.render('mtapi:1')
  await t.open('mtapi:1', 'personal')
  t.$('#setup-sheet [data-act="unit"][data-name="riskPerTrade"][data-value="amount"]').click()
  assert.equal(t.$('#su-in-riskPerTrade').value, '500')
  t.type('#su-in-riskPerTrade', '')
  await t.next()
  assert.equal(t.db.puts.at(-1).personal.riskPerTrade, undefined)
  assert.ok(t.db.puts.at(-1).personal.sessionBudget, 'the untouched field keeps its value')
  const u = setup()
  await u.render('mtapi:1')
  await u.open('mtapi:1', 'personal')
  u.type('#su-in-sessionBudget', '')
  await u.skip()
  assert.deepEqual(u.db.puts.at(-1).personal.sessionBudget, amt(500), 'Skip leaves the saved answer alone')
})

test('zero is not a clear: the server only accepts amounts above zero', async () => {
  const t = setup()
  await t.render('mtapi:1')
  await t.open('mtapi:1', 'personal')
  t.type('#su-in-sessionBudget', '0')
  assert.ok(t.primary().disabled)
  assert.match(t.$('#su-note-sessionBudget').textContent, /above zero, or leave it empty/)
})

test('every optional target and limit can be removed from its own question', async () => {
  const cases = [
    { acct: 1, flow: 'phase', step: 'phaseTarget', input: '#su-in-phaseTarget', gone: (p) => p.evaluation.phaseTarget },
    { acct: 1, flow: 'firm', step: 'firmDaily', input: '#su-in-firmDaily', gone: (p) => p.firmRules.dailyLoss || p.firmRules.dailyReset || p.firmRules.dailyResetZone, kept: (p) => p.firmRules.overall },
    { acct: 1, flow: 'firm', step: 'firmOverall', input: '#su-in-firmOverall', gone: (p) => p.firmRules.overall || p.firmRules.overallMode || p.firmRules.overallRef, kept: (p) => p.firmRules.dailyLoss },
    { acct: 1, flow: 'personal', step: 'personal', input: '#su-in-riskPerTrade', gone: (p) => p.personal.riskPerTrade, kept: (p) => p.personal.sessionBudget },
    { acct: 2, flow: 'payout', step: 'payoutTarget', input: '#su-in-payoutTarget', gone: (p) => p.master.payoutTarget, kept: (p) => p.master.firmMin },
    { acct: 2, flow: 'payout', step: 'firmMin', input: '#su-in-firmMin', gone: (p) => p.master.firmMin, kept: (p) => p.master.payoutTarget },
    { acct: 2, flow: 'payout', step: 'profitSplit', input: '#su-amt-profitSplit', gone: (p) => p.master.profitSplit, kept: (p) => p.master.payoutTarget },
  ]
  for (const c of cases) {
    const t = setup()
    await t.render('mtapi:' + c.acct)
    await t.open('mtapi:' + c.acct, c.flow, c.step)
    assert.ok(t.$(c.input), c.step + ': input shown')
    assert.notEqual(t.$(c.input).value, '', c.step + ': the saved answer is shown')
    t.type(c.input, '')
    assert.ok(!t.primary().disabled, c.step + ': an emptied saved answer can be saved')
    await t.next()
    while (t.sheetOpen()) await t.next()
    const p = t.saved(c.acct)
    assert.ok(!c.gone(p), c.step + ': removed on the server ' + JSON.stringify(p))
    if (c.kept) assert.ok(c.kept(p), c.step + ': the other answers stay')
  }
})

test('the growth target can be removed, and each saved target keeps an edit link on the card', async () => {
  const db = makeServer()
  db.profiles[3] = { ...db.profiles[3], master: { goal: { id: 'grow', text: '' }, growthTarget: amt(2500) }, done: { basics: true, growth: true } }
  const t = setup(db)
  await t.render('mtapi:3')
  assert.ok(t.card.querySelector('[data-setup="growth"]'), 'Growth target link')
  await t.render('mtapi:2')
  assert.ok(t.card.querySelector('[data-setup="payout"]'), 'Payout target link')
  await t.render('mtapi:1')
  assert.ok(t.card.querySelector('[data-setup="phase"]'), 'Phase target link')
  await t.render('mtapi:3')
  t.card.querySelector('[data-setup="growth"]').click()
  await until(() => !!t.$('#su-in-growthTarget'))
  assert.equal(t.$('#su-in-growthTarget').value, '2500')
  t.type('#su-in-growthTarget', '')
  await t.next()
  assert.equal(t.saved(3).master.growthTarget, undefined)
})

test('an empty field with nothing saved cannot be "saved"; use Skip', async () => {
  const db = makeServer()
  delete db.profiles[1].personal
  const t = setup(db)
  await t.render('mtapi:1')
  await t.open('mtapi:1', 'personal')
  assert.ok(t.primary().disabled)
})

test('a failed removal keeps the edit, offers Retry, and the card shows what is saved until it succeeds', async () => {
  const t = setup()
  await t.render('mtapi:1')
  await t.open('mtapi:1', 'personal')
  t.type('#su-in-sessionBudget', '')
  t.db.failNext = 1
  await t.next()
  assert.ok(t.$('#setup-sheet [data-act="retry"]'), 'Retry is offered')
  assert.match(t.$('#setup-sheet').textContent, /Your answer is still here/)
  assert.equal(t.$('#su-in-sessionBudget').value, '', 'the emptied field stays empty')
  assert.deepEqual(t.saved(1).personal.sessionBudget, amt(500), 'nothing changed on the server')
  assert.equal(t.w.TaliAccountSetup.profile('mtapi:1').personal.sessionBudget.value, '500', 'the card works from the saved answers')
  t.$('#setup-sheet [data-act="retry"]').click()
  await until(() => t.saved(1).personal.sessionBudget === undefined)
  assert.equal(t.saved(1).personal.sessionBudget, undefined, 'Retry sends the same removal')
})
