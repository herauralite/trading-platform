// Daily P&L and Challenge / payout tiles built from the /progress payload (pure helpers).
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const sandbox = { console, Intl, Date, Math, JSON, Number, String, Object, Array, Promise, setTimeout, clearTimeout }
sandbox.window = sandbox
vm.createContext(sandbox)
vm.runInContext(fs.readFileSync(path.join(here, '../public/tali-matchtrader-api.js'), 'utf8'), sandbox)
const core = sandbox.TaliMatchTraderCore

const day = (extra) => ({ basis: 'realized_net', date: '2026-11-17', time_zone: 'America/Los_Angeles', day_start: '2026-11-17T08:00:00Z', day_end: '2026-11-18T08:00:00Z',
  closed_entries: 3, positions: 2, entries_without_result: 0, checked_through: '2026-11-17T20:30:00Z', open_pnl: '-500.00', ...extra })

test('daily P&L: realized value in the account currency with the day and zone', () => {
  const t = core.progressTiles({ currency: 'USD', daily_pnl: day({ value: '-210.00', status: 'verified' }) }, 'USD')
  assert.equal(t.analytics.dailyPnlText, '-$210.00')
  assert.equal(t.analytics.dailyTone, 'txt-red')
  assert.equal(t.analytics.dailySub, 'Realized · Nov 17 PST')
  assert.match(t.analytics.dailyTitle, /Open positions are not included/)
})

test('daily P&L: a verified day without closes is zero; EUR stays EUR whatever the browser language', () => {
  const t = core.progressTiles({ currency: 'EUR', daily_pnl: day({ value: '0.00', status: 'verified', closed_entries: 0 }) }, 'USD')
  assert.equal(t.analytics.dailyPnlText, '€0.00')
  assert.equal(t.analytics.dailyTone, 'txt-dim')
})

test('daily P&L: incomplete or unknown history carries its status', () => {
  assert.match(core.progressTiles({ currency: 'USD', daily_pnl: day({ value: '40.00', status: 'importing' }) }).analytics.dailySub, /still importing/)
  assert.match(core.progressTiles({ currency: 'USD', daily_pnl: day({ value: '40.00', status: 'gaps' }) }).analytics.dailySub, /unchecked/)
  assert.match(core.progressTiles({ currency: 'USD', daily_pnl: day({ value: '40.00', status: 'delayed' }) }).analytics.dailySub, /delayed, checked/)
  const pending = core.progressTiles({ currency: 'USD', daily_pnl: day({ value: null, status: 'pending' }) }).analytics
  assert.equal(pending.dailyPnlText, '—')
  assert.match(pending.dailySub, /first trade sync/)
})

test('challenge / payout: percent of the target, then "Target reached"', () => {
  const p = core.progressTiles({ currency: 'USD', overview: { state: 'in_progress', label: 'Phase 1 profit target', target: '8000.00', current: '4000.00', ratio_percent: '50.0' } }).payout
  assert.equal(p.progressVal, '50%')
  assert.equal(p.progressSub, 'Phase 1 profit target · $4,000.00 of $8,000.00')
  const r = core.progressTiles({ currency: 'USD', overview: { state: 'reached', label: 'Your payout target', target: '2500.00', current: '2600.00', ratio_percent: '104.0' } }).payout
  assert.equal(r.progressVal, 'Target reached')
  assert.equal(r.progressTone, 'txt-green')
})

test('challenge / payout: no number means an action, never a percentage', () => {
  const g = core.progressTiles({ currency: 'USD', overview: { state: 'no_target', label: 'Grow the account', action: { id: 'flow', flow: 'growth', label: 'Set growth amount' } } }).payout
  assert.equal(g.progressVal, '—')
  assert.equal(g.progressSub, 'Grow the account · Set growth amount')
  assert.equal(g.action.flow, 'growth')
  const u = core.progressTiles({ currency: 'USD', overview: { state: 'unavailable', label: 'Phase 2 profit target', action: { id: 'flow', flow: 'phaseStart', label: 'Confirm Phase 2 starting balance' } } }).payout
  assert.equal(u.progressVal, '—')
  assert.match(u.progressSub, /Confirm Phase 2 starting balance/)
  assert.equal(core.progressTiles({ currency: 'USD', overview: null }).payout, null)
})

test('challenge / payout: unconfirmed figures show the number without a settled status', () => {
  const p = core.progressTiles({ currency: 'USD', overview: { state: 'unconfirmed', label: 'Phase 1 profit target', target: '8000.00', current: '8100.00', ratio_percent: '101.3' } }).payout
  assert.equal(p.progressVal, '101.3%')
  assert.notEqual(p.progressVal, 'Target reached')
  assert.match(p.progressSub, /unconfirmed$/)
  assert.equal(p.progressTone, 'txt-dim')
})
