// Dashboard live status: stale feeds are shown with their last values and age, never as live.
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'

const app = fs.readFileSync(new URL('../public/app.html', import.meta.url), 'utf8')

function helpers() {
  const start = app.indexOf('function taliFeedAgeLabel(')
  const end = app.indexOf('function prettyRelativeFromIso(')
  assert.ok(start > 0 && end > start, 'freshness helpers present in the served page')
  const ctx = {}
  vm.createContext(ctx)
  vm.runInContext(app.slice(start, end) + ';this.age=taliFeedAgeLabel;this.clock=taliFeedClock;', ctx)
  return ctx
}

test('age label uses the server-computed age', () => {
  const { age } = helpers()
  assert.equal(age(null), 'unknown')
  assert.equal(age({ age_seconds: 12 }), 'just now')
  assert.equal(age({ age_seconds: 600 }), '10m ago')
  assert.equal(age({ age_seconds: 7300 }), '2h ago')
  assert.equal(age({ age_seconds: 200000 }), '2d ago')
})

test('clock reads the naive-UTC last_updated as UTC, not as local time', () => {
  const { clock } = helpers()
  const expected = new Date('2026-11-23T15:04:00Z').toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' })
  assert.equal(clock({ last_updated: '2026-11-23T15:04:00' }), expected)
  assert.equal(clock({ last_updated: '2026-11-23T15:04:00+00:00' }), expected)
  assert.match(clock(null), /\d{1,2}:\d{2}/)
})

test('fetchStatus falls back to stale_accounts and labels them stale, never live', () => {
  const start = app.indexOf('async function fetchStatus(')
  const body = app.slice(start, app.indexOf('\n}\n', start))
  assert.match(body, /const staleAcct=!liveAcct&&data\.stale_accounts\?data\.stale_accounts\[activeAcct\]:null/)
  assert.match(body, /syncVal:isStale\?'Stale':'Live'/)
  assert.match(body, /if\(isStale\)\{ statusEl\.textContent='stale · last update '\+feedAge;/)
  // The "Updated" time comes from the server's receipt time, not the browser clock.
  assert.doesNotMatch(body, /syncSub:'Updated '\+new Date\(\)/)
  // Only live entries feed the live-accounts cache.
  assert.match(body, /if\(data\.accounts\) Object\.assign\(liveAccountsCache, data\.accounts\)/)
})
