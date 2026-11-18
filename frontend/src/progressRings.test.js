// Progress circles on the account card: rendering, selection, refreshes and account switching.
// Runs the real tali-account-setup.js in jsdom against a fake host. Synthetic data only; no layout or animation.
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

const PROFILES = {
  1: { status: 'saved', type: 'evaluation', size: { amount: '100000', currency: 'USD' }, evaluation: { phaseCount: 1, phaseTarget: { value: '8', unit: 'percent', base: 'starting' } }, done: { basics: true, phase: true, firm: true, personal: true }, draft: {} },
  2: { status: 'saved', type: 'master', size: { amount: '2000000', currency: 'USD' }, master: { goal: { id: 'payout', text: '' }, payoutTarget: { value: '5', unit: 'percent', base: 'starting' } }, done: { basics: true, payout: true, firm: true, personal: true }, draft: {} },
  3: { status: 'saved', type: 'master', size: { amount: '25000', currency: 'USD' }, master: {}, done: { basics: true }, draft: {} },
}
const target = (o) => ({ id: 'phase_target', kind: 'target', group: 'target', title: 'Profit target', source: 'Firm target', entered: { value: '8', unit: 'percent', base: 'starting' },
  measurable: true, status: 'in_progress', status_label: 'In progress', tone: 'cyan', target: '8000.00', current: '3252.90', remaining: '4747.10', fraction: 0.4066, ratio_percent: '40.7',
  baseline: { kind: 'starting_size', amount: '100000.00' }, definition: 'target', ...o })
const daily = (o) => ({ id: 'daily_loss', kind: 'limit', group: 'firm', title: 'Daily loss limit', source: 'Firm rule', entered: { value: '2', unit: 'percent', base: 'starting' },
  measurable: true, status: 'breached', status_label: 'Limit breached', tone: 'red', used: '52000.00', limit: '40000.00', remaining: '-12000.00', fraction: 1, ratio_percent: '130.0',
  reset: '17:00', reset_zone: 'America/New_York', realized_since_reset: '-52000.00', open_pnl: '0.00', reference: {}, definition: 'daily_loss', ...o })
const budget = (o) => ({ id: 'session_budget', kind: 'limit', group: 'personal', title: 'Session loss budget', source: 'Your limit', measurable: false, status: 'unavailable',
  status_label: 'Not measured', tone: 'neutral', reason: 'Session losses are not measured.', action: { id: 'flow', flow: 'personal', label: 'Edit personal limits' }, ...o })
const payout = (o) => target({ id: 'payout_target', title: 'Your payout target', source: 'Your target', target: '100000.00', current: '-71200.00', remaining: '171200.00',
  fraction: 0, ratio_percent: '-71.2', status: 'unconfirmed', status_label: 'Unconfirmed', tone: 'neutral', unconfirmed_reason: "Deposit and withdrawal history isn't imported back to this baseline yet.", ...o })
const risk = (o) => ({ id: 'risk_per_trade', kind: 'limit', group: 'personal', title: 'Risk per trade', source: 'Your limit', measurable: true, status: 'estimate_at', status_label: 'Estimate at limit',
  tone: 'amber', used: '500.00', limit: '500.00', remaining: '0.00', fraction: 1, ratio_percent: '100.0', worst_symbol: 'XAUUSD', estimate: true, unmeasured_count: 1,
  unmeasured: [{ symbol: 'US30', reason: 'no_stop', reason_text: 'no stop loss' }], ...o })

function setup() {
  const dom = new JSDOM('<!doctype html><body><div id="card" class="card"></div></body>', { url: 'http://localhost/app', runScripts: 'outside-only', pretendToBeVisual: true })
  const w = dom.window
  const progress = {
    'mtapi:1': { currency: 'USD', rows: [target(), daily({ status: 'within', status_label: 'Within limit', tone: 'green', used: '160.00', limit: '5000.00', remaining: '4840.00', fraction: 0.032, ratio_percent: '3.2' }), budget(), risk()], labels: [], definitions: { target: 'Closed profit since the baseline.', daily_loss: 'Closed and open losses since the reset.' } },
    'mtapi:2': { currency: 'USD', rows: [payout(), daily()], labels: [{ title: 'Trading style', value: 'Swing' }], definitions: { daily_loss: 'Closed and open losses since the reset.' } },
    'mtapi:3': { currency: 'USD', rows: [], labels: [] },
  }
  let active = 'mtapi:1'
  w.fetch = async (url) => {
    const u = String(url), json = (b) => ({ ok: true, status: 200, json: async () => b, text: async () => JSON.stringify(b) })
    if (u.endsWith('/account-setup/profiles')) return json({ profiles: Object.entries(PROFILES).map(([id, p]) => ({ connection_id: Number(id), version: 1, profile: p })) })
    const m = /connections\/(\d+)$/.exec(u)
    if (m) return json({ version: 1, profile: PROFILES[m[1]], detected: { balance: '1000' } })
    return { ok: false, status: 404, json: async () => ({}), text: async () => '' }
  }
  w.eval(src)
  const host = {
    getUserId: () => 'u1', getActiveAccount: () => active, isEnabled: () => true, getSessionToken: () => 'session-1', getApiBase: () => 'http://api.test',
    getAccount: (k) => ({ broker: k === 'mtapi:2' ? 'Sample Prop Firm International Funded Trader Programme' : 'FundingPips', ending: k.slice(-1).repeat(4), currency: 'USD', accountType: 'DEMO' }),
    getSnapshot: () => ({ summary: { balance: '1928800.00', fetched_at: '2026-11-18T14:00:00Z' } }),
    getProgress: (k) => progress[k], refreshSwitcher() {}, isBusy: () => false, onProfileSaved() {},
  }
  w.TaliAccountSetup.attach(host)
  const card = w.document.getElementById('card')
  const t = {
    w, card, progress,
    async render(k = active) { active = k; w.TaliAccountSetup.renderCard(card, k); await tick(20); w.TaliAccountSetup.renderCard(card, k) },
    rings: () => [...card.querySelectorAll('.pg-ring')],
    ring: (id) => card.querySelector('.pg-ring[data-id="' + id + '"]'),
    selected: () => t.rings().filter((b) => b.classList.contains('is-on')).map((b) => b.dataset.id),
    drawer: () => card.querySelector('.pg-drawer'),
    detail: () => (card.querySelector('.pg-detail') || {}).textContent || '',
    key: (el, key) => el.dispatchEvent(new w.KeyboardEvent('keydown', { key, bubbles: true })),
  }
  return t
}

test('circles show only the configured rules, with honest centres', async () => {
  const t = setup()
  await t.render('mtapi:1')
  assert.deepEqual(t.rings().map((b) => b.dataset.id), ['phase_target', 'daily_loss', 'session_budget', 'risk_per_trade'])
  assert.deepEqual(t.rings().map((b) => b.querySelector('.pg-val').textContent), ['41%', '3%', '—', '100%'])
  assert.ok(t.ring('session_budget').classList.contains('is-unknown'), 'an unmeasured rule is a neutral circle, not an empty one')
  assert.equal(t.ring('session_budget').querySelector('.pg-arc'), null)
  assert.equal(t.ring('session_budget').querySelector('.pg-state').textContent, 'Needs info')
  assert.equal(t.card.querySelector('.pg-bar'), null, 'no horizontal bar anywhere')
  assert.match(t.ring('phase_target').getAttribute('aria-label'), /target, 40\.7% complete\. In progress/)
  assert.match(t.ring('daily_loss').getAttribute('aria-label'), /limit, 3\.2% of the limit used\. Within limit/)
  assert.match(t.ring('session_budget').getAttribute('aria-label'), /not measured/)
  assert.match(t.ring('risk_per_trade').getAttribute('aria-label'), /estimate\. Estimate at limit/)
  assert.equal(t.ring('phase_target').querySelector('.pg-name').textContent, 'Profit target')
  assert.ok(t.rings().every((b) => b.tagName === 'BUTTON' && b.type === 'button' && b.getAttribute('aria-expanded') === 'false'))
  assert.ok(t.drawer().hasAttribute('inert') && !t.drawer().classList.contains('is-open'))
})

test('no rules: no circles; long names, negative and exceeded figures', async () => {
  const t = setup()
  await t.render('mtapi:3')
  assert.equal(t.rings().length, 0)
  assert.doesNotMatch(t.card.textContent, /Targets and limits/)
  await t.render('mtapi:2')
  assert.match(t.card.querySelector('.su-card-title').textContent, /International Funded Trader Programme/)
  assert.equal(t.ring('payout_target').querySelector('.pg-val').textContent, '−71%')
  assert.equal(t.ring('payout_target').querySelector('.pg-arc'), null, 'negative progress draws no fill')
  assert.equal(t.ring('payout_target').querySelector('.pg-name').textContent, 'Payout target')
  assert.equal(t.ring('daily_loss').querySelector('.pg-val').textContent, '130%')
  assert.equal(t.ring('daily_loss').querySelector('.pg-arc').style.strokeDashoffset, '0', 'the fill stops at a full circle')
  assert.equal(t.ring('daily_loss').querySelector('.pg-name').textContent, 'Daily loss')
  t.ring('daily_loss').click()
  assert.match(t.detail(), /\$52,000 used of \$40,000 limit/)
  assert.match(t.detail(), /130% of the limit used · \$12,000 past the limit/)
  assert.match(t.detail(), /Limit breached/)
  t.ring('payout_target').click()
  assert.match(t.detail(), /−\$71,200 of \$100,000 target/)
  assert.match(t.detail(), /−71\.2% complete/)
  assert.match(t.detail(), /isn't imported back to this baseline/, 'the unconfirmed reason stays in the essentials')
})

test('tap opens one circle, tap again closes, another replaces it', async () => {
  const t = setup()
  await t.render('mtapi:1')
  t.ring('daily_loss').click()
  assert.deepEqual(t.selected(), ['daily_loss'])
  assert.equal(t.ring('daily_loss').getAttribute('aria-expanded'), 'true')
  assert.equal(t.ring('daily_loss').getAttribute('aria-controls'), t.drawer().id)
  assert.ok(t.card.querySelector('.pg-rings').classList.contains('has-on'))
  assert.ok(t.drawer().classList.contains('is-open') && !t.drawer().hasAttribute('inert'))
  assert.match(t.detail(), /Daily loss limit/)
  assert.match(t.detail(), /\$160 used of \$5,000 limit/)
  assert.match(t.detail(), /How it's measured/)
  t.ring('risk_per_trade').click()
  assert.deepEqual(t.selected(), ['risk_per_trade'])
  assert.match(t.detail(), /at risk on XAUUSD/)
  assert.match(t.detail(), /Estimate: XAUUSD isn't priced in USD/)
  assert.match(t.detail(), /Not measured: US30 \(no stop loss\)/)
  assert.doesNotMatch(t.detail(), /Within limit/)
  t.ring('risk_per_trade').click()
  assert.deepEqual(t.selected(), [])
  assert.ok(!t.drawer().classList.contains('is-open') && t.drawer().hasAttribute('inert'))
  t.ring('session_budget').click()
  assert.match(t.detail(), /Session losses are not measured/)
  assert.ok(t.card.querySelector('.pg-detail [data-setup="act"]'), 'a missing measurement offers its setup action')
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
  assert.notEqual(t.ring('daily_loss'), node, 'new figures are drawn')
  assert.deepEqual(t.selected(), ['daily_loss'])
  assert.match(t.detail(), /\$260 used/)
  assert.equal(t.w.document.activeElement, t.ring('daily_loss'), 'focus stays on the same circle')
  assert.ok(t.card.querySelector('.pg-help').open, 'an open explanation stays open')
  t.ring('daily_loss').click()
  t.progress['mtapi:1'].rows[1] = daily({ status: 'within', status_label: 'Within limit', tone: 'green', used: '300.00', limit: '5000.00', remaining: '4700.00', fraction: 0.06, ratio_percent: '6.0' })
  await t.render('mtapi:1')
  assert.deepEqual(t.selected(), [], 'a collapsed circle is not reopened by a refresh')
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
  assert.match(t.detail(), /\$52,000 used/)
  await t.render('mtapi:1')
  assert.deepEqual(t.selected(), ['phase_target'])
})

test('a removed rule clears its selection and focus moves to the circle in its place', async () => {
  const t = setup()
  await t.render('mtapi:1')
  t.ring('session_budget').click()
  t.ring('session_budget').focus()
  t.progress['mtapi:1'].rows.splice(2, 1)
  await t.render('mtapi:1')
  assert.equal(t.ring('session_budget'), null)
  assert.deepEqual(t.selected(), [])
  assert.ok(!t.drawer().classList.contains('is-open'))
  assert.equal(t.w.document.activeElement, t.ring('risk_per_trade'))
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
  assert.equal(t.w.document.activeElement, t.ring('phase_target'), 'wraps around')
  t.ring('daily_loss').click()
  const summary = t.card.querySelector('.pg-help summary')
  summary.focus()
  t.key(summary, 'Escape')
  assert.deepEqual(t.selected(), [])
  assert.equal(t.w.document.activeElement, t.ring('daily_loss'))
})
