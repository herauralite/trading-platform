// Browser-flow test for optional account setup inside the served /app dashboard.
// Loads the BUILT page (dist/app.html) in jsdom with a scripted fake backend (account-setup-fake.mjs)
// and drives the real page: real buttons, real sheet, real dashboard code. Synthetic data only.
// Not a real browser (no layout), not real Telegram, not a real broker: those are checked by hand.
//   node browser-flow/account-setup-flow.mjs dist
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import jsdomPkg from 'jsdom'
import { createServer, TOKENS } from './account-setup-fake.mjs'
const { JSDOM, VirtualConsole } = jsdomPkg

const dist = path.resolve(process.argv[2] || path.join(path.dirname(fileURLToPath(import.meta.url)), '../dist'))
const html = fs.readFileSync(path.join(dist, 'app.html'), 'utf8')
  .replace(/<script src="(\/tali-[a-z-]+\.js)"><\/script>/g, (_m, src) => `<script>${fs.readFileSync(path.join(dist, src), 'utf8')}</script>`)
  .replace(/<script[^>]*src="https?:[^"]*"[^>]*><\/script>/g, '')
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const pageErrors = []
process.on('unhandledRejection', (e) => pageErrors.push('unhandled rejection: ' + String((e && e.message) || e).slice(0, 160)))
let pass = 0, fail = 0
function check(name, cond, detail = '') { if (cond) pass++; else fail++; console.log((cond ? 'PASS ' : 'FAIL ') + name + (cond ? '' : '  [' + String(detail).slice(0, 300) + ']')) }
function section(t) { console.log('\n== ' + t + ' ==') }
const until = async (fn, ms = 12000) => { const t = Date.now(); while (Date.now() - t < ms) { try { if (fn()) return true } catch { /* not yet */ } await sleep(100) } return false }

async function boot(server, sessionToken = 'session-alice', { wait = 1500, lang = null, storage = null } = {}) {
  const errors = []
  const vc = new VirtualConsole()
  vc.on('jsdomError', (e) => { const t = String(e.message || e); if (!/Could not load|Not implemented|Could not parse CSS/.test(t)) errors.push(t.slice(0, 200)) })
  vc.on('error', (...a) => errors.push('console.error: ' + a.map(String).join(' ').slice(0, 200)))
  const dom = new JSDOM(html, { url: 'http://localhost:5173/app', runScripts: 'dangerously', virtualConsole: vc, pretendToBeVisual: true,
    beforeParse(w) {
      w.__TALI_CONFIG__ = { apiBase: 'http://localhost:8000' }
      w.fetch = server.handle
      if (lang) { Object.defineProperty(w.navigator, 'language', { value: lang, configurable: true }); Object.defineProperty(w.navigator, 'languages', { value: [lang], configurable: true }) }
      if (storage) for (const [k, v] of Object.entries(storage)) w.localStorage.setItem(k, v)
      w.localStorage.setItem('tali_session_token_v1', sessionToken)
      w.localStorage.setItem('tali_user_v1', JSON.stringify({ telegramUserId: sessionToken.includes('bob') ? '2002' : '1001', telegram_user_id: sessionToken.includes('bob') ? '2002' : '1001', firstName: 'T' }))
      w.sessionStorage.setItem('tali_tg_done', '1'); w.sessionStorage.setItem('tali_pw_ok', '1')
      w.matchMedia = w.matchMedia || (() => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }))
      w.scrollTo = () => {}
      w.Element.prototype.scrollIntoView = () => {}
      const series = () => ({ setData() {}, update() {}, setMarkers() {}, applyOptions() {} })
      w.LightweightCharts = { CrosshairMode: { Normal: 0 }, createChart: () => ({ addLineSeries: series, addCandlestickSeries: series, applyOptions() {}, timeScale: () => ({ getVisibleLogicalRange: () => null, setVisibleLogicalRange() {}, fitContent() {} }) }) }
      w.ResizeObserver = w.ResizeObserver || class { observe() {} disconnect() {} }
    } })
  await sleep(wait)
  const w = dom.window, $ = (q) => w.document.querySelector(q)
  const p = { w, $, errors, server,
    open: () => !!$('#setup-sheet.open'), title: () => ($('#setupTitle') || {}).textContent || '', q: () => ($('#setupQuestion') || {}).textContent || '',
    step: () => ($('#setupStep') || {}).textContent || '', body: () => ($('#setupBody') || {}).textContent || '',
    act: (a) => $('#setup-sheet [data-act="' + a + '"]').click(),
    pick: (name, value) => $('#setup-sheet [data-name="' + name + '"][data-value="' + value + '"]').click(),
    type: (sel, v) => { const i = $(sel); i.value = v; i.dispatchEvent(new w.Event('input', { bubbles: true })) },
    primary: () => $('#setupPrimary'),
    next: async () => { $('#setupPrimary').click(); await sleep(60) },
    card: () => ($('#mtApiSetupCard') || {}).textContent || '', cardShown: () => !!$('#mtApiSetupCard') && $('#mtApiSetupCard').style.display !== 'none' && !!$('#mtApiSetupCard').textContent,
    cardAction: () => ($('#setupNextAction') || {}).textContent || '',
    clickCard: async (what) => { $('#mtApiSetupCard [data-setup="' + what + '"]').click(); await sleep(60) },
    link: async (token) => {
      w.eval('openAddAccountFlow()'); await sleep(120)
      $('#addAcctFlowBody [data-broker-card="matchtraderApi"]').click(); await sleep(120)
      $('#mtApiTokenInput').value = token
      $('#addAcctFooter .btn-blue').click(); await sleep(250)
    },
    active: () => w.eval('activeAcct'), switchTo: async (key) => { w.eval(`switchAccount('${key}')`); await sleep(300) },
    profileOnServer: (user, login) => (server.profiles[user + '|' + login] || {}).data }
  return p
}

// One tap import, wait for "Trade history imported", then Continue to account setup.
async function importThenSetup(p) {
  p.act('import'); await until(() => p.title() === 'Trade history imported' || p.title() === 'No closed trades found')
  p.act('continueSetup'); await until(() => /Which account/.test(p.q()))
}

// ── 1. flag off: the existing dashboard flow, with an account linked earlier ──
section('flag off (server-controlled, default)')
{
  const server = createServer({ setupEnabled: false })
  const a = server.addLinked('1001', 'fp')
  server.profiles['1001|1842'] = { version: 3, data: { status: 'saved', type: 'master' }, lastSaveId: 'old' }   // saved while the flag was on
  const p = await boot(server)
  check('dashboard loads the linked account', p.active() === 'mtapi:' + a.id && !!p.$('#mtApiPanel'), p.active())
  await sleep(300)
  check('no setup card, no setup requests', !p.cardShown() && server.setupRequests.length === 0, server.setupRequests.length)
  await p.link(TOKENS.eu)
  check('linking still closes the token sheet', !p.$('#add-acct-sheet.open'))
  await sleep(6500)
  check('no invitation after the first sync', !p.open())
  check('saved profile left untouched', server.profiles['1001|1842'].version === 3)
  check('flag off: no one-tap import, no custom history controls', server.importPosts.length === 0 && !p.$('#mtApiHistoryControls'))
  check('no page errors (flag off)', p.errors.length === 0, p.errors.join(' | '))
  p.w.close()
}

// ── 2. an account linked before setup existed ──
section('existing linked account, flag on')
{
  const server = createServer()
  server.addLinked('1001', 'fp')
  const p = await boot(server)
  await until(() => p.cardAction() === 'Set up account')
  check('card offers Set up account without reconnecting', p.cardAction() === 'Set up account', p.card())
  check('card separates broker facts from trader entries', /From your broker/.test(p.card()) && /Real \(live\) account · USD · balance \$49,820/.test(p.card()) && /You entered/.test(p.card()), p.card())
  check('no invitation for an account that was already synced', !p.open())
  p.w.close()
}

// ── 3. link → first sync → invitation → Evaluation (one phase, custom size) ──
section('Evaluation: link, invitation, one-phase, custom size')
let evalId
{
  const server = createServer()
  const p = await boot(server)
  const t0 = Date.now(); await p.link(TOKENS.eu)
  check('token sheet closes promptly', !p.$('#add-acct-sheet.open') && Date.now() - t0 < 1500, Date.now() - t0)
  check('no invitation while syncing', !p.open())
  await until(() => p.open())
  check('Account detected appears on the first successful sync', p.title() === 'Account detected', p.title())
  const facts = p.body()
  check('invitation: broker, masked account, balance, currency, time', /Sample Broker/.test(facts) && /···· 7730/.test(facts) && /€10,214\.50/.test(facts) && /EUR/.test(facts) && /As of/.test(facts), facts)
  check('Demo is not called evaluation; no size is suggested', /Demo account/.test(facts) && !/10K|Evaluation|Master/.test(facts), facts)
  check('Import trade history is the primary action, Later is quiet', p.$('#setupPrimary').textContent === 'Import trade history' && !!p.$('#setup-sheet .su-link[data-act="dashboard"]') && /Later/.test(p.$('#setupFooter').textContent))
  check('no date picker or 30/90-day choice anywhere in onboarding', !/30 days|90 days|From date/.test(p.$('#setup-sheet').textContent))
  await importThenSetup(p)
  check('first question, nothing preselected', /Which account/.test(p.q()) && !p.$('#setup-sheet .su-opt.on') && p.primary().disabled)
  p.pick('type', 'evaluation'); await p.next()
  check('size question: no preset, balance named as broker-reported', /What size/.test(p.q()) && !p.$('#setup-sheet .su-opt.on') && /broker reports a current balance of €10,214\.50/.test(p.body()), p.body())
  p.pick('sizePreset', 'other')
  check('Other reveals an input in the account currency', !!p.$('#su-size-other') && /EUR/.test(p.body()) && p.primary().disabled)
  p.type('#su-size-other', '15000'); await p.next()
  check('phase count asked', /How many phases/.test(p.q()))
  p.act('back'); await sleep(50)
  check('Back keeps the custom size', p.$('#su-size-other') && p.$('#su-size-other').value === '15000')
  await p.next(); p.pick('phaseCount', '1')
  check('one-phase path: "which phase" is not counted', / 3 of 4/.test(p.step()), p.step())
  await p.next()
  check('one-phase: straight to the profit target', /profit target for this phase/.test(p.q()), p.q())
  p.pick('phaseTarget', 'percent'); p.type('#su-in-phaseTarget', '8')
  check('percent converts with the entered starting size, and names it', /8% = €1,200 · using the starting size you entered, €15,000/.test(p.$('#su-note-phaseTarget').textContent), p.$('#su-note-phaseTarget').textContent)
  check('last step says Save to account', p.primary().textContent === 'Save to account')
  await p.next(); await until(() => !p.open())
  evalId = server.accounts.find((a) => a.login === '7730').id
  const saved = p.profileOnServer('1001', '7730')
  check('saved on the server for this trader and account', saved && saved.type === 'evaluation' && saved.size.amount === '15000' && saved.size.currency === 'EUR' && saved.evaluation.phaseCount === 1 && !saved.evaluation.currentPhase && saved.status === 'saved', JSON.stringify(saved))
  check('card shows the result', /Evaluation · €15,000 starting size/.test(p.card()) && /One-phase evaluation/.test(p.card()) && /Phase target · 8% of starting size \(€1,200\)/.test(p.card()), p.card())
  check('firm rules and personal limits are separate actions', /Firm rules/.test(p.card()) && /Personal risk limits/.test(p.card()))
  check('one next action after the phase target: Add firm rules', p.cardAction() === 'Add firm rules', p.cardAction())
  check('no page errors (evaluation)', p.errors.length === 0, p.errors.join(' | '))
  p.w.close()
}

// ── 4. Master with a payout goal; amount/percent; finish later; reload; another device ──
section('Master: payout goal, amount/percent, resume across reload and devices')
{
  const server = createServer()
  const p = await boot(server)
  await p.link(TOKENS.fp); await until(() => p.open())
  await importThenSetup(p); p.pick('type', 'master'); await p.next()
  check('$49,820 balance does not preselect 50K', !p.$('#setup-sheet .su-opt.on'))
  p.pick('sizePreset', '50000'); await p.next()
  const goals = Array.from(p.w.document.querySelectorAll('#setup-sheet .su-opt-label')).map((n) => n.textContent)
  check('master goals', goals.join('|') === 'Earn a payout|Grow the account|Stay within my limits|Other', goals.join('|'))
  p.pick('goal', 'other'); p.type('#su-goal-text', 'Trade only the London open'); p.pick('goal', 'payout'); await p.next(); await until(() => !p.open())
  check('first session ends after profile + goal', /Master \/ funded · \$50,000 starting size/.test(p.card()) && /My goal · Earn a payout/.test(p.card()) && p.cardAction() === 'Set my payout target', p.card())
  await p.clickCard('next')
  p.type('#su-in-payoutTarget', '2500')
  check('$2,500 = 5% of the confirmed $50,000', /\$2,500 = 5% · using the starting size you entered, \$50,000/.test(p.$('#su-note-payoutTarget').textContent), p.$('#su-note-payoutTarget').textContent)
  p.pick('payoutTarget', 'percent')
  check('% view shows 5, the entry stays $2,500', p.$('#su-in-payoutTarget').value === '5' && p.w.TaliAccountSetup.profile('mtapi:1').draft.payoutTarget.unit === 'amount')
  p.pick('payoutTarget', 'amount'); await p.next()
  check("firm minimum asked separately, no eligibility claim", /firm's minimum profit for a payout/.test(p.q()) && /does not confirm payout eligibility/.test(p.body()))
  p.pick('firmMin', 'percent'); p.type('#su-in-firmMin', '1')
  p.act('later'); await until(() => !p.open())
  const stored = p.profileOnServer('1001', '1842')
  check('finish later stores the confirmed answer, the typed one and the resume point', stored.master.payoutTarget.value === '2500' && stored.resume.step === 'firmMin' && stored.draft.firmMin.raw === '1', JSON.stringify(stored))
  check('card: Resume', /Resume payout target/.test(p.cardAction()), p.cardAction())
  p.w.close()

  const reload = await boot(server)
  await until(() => reload.cardAction())
  check('reload: no invitation, setup still on the card', !reload.open() && /Resume payout target/.test(reload.cardAction()), reload.cardAction())
  reload.w.close()

  const device2 = await boot(server, 'session-alice-device-2')
  await until(() => /Resume/.test(device2.cardAction()))
  await device2.clickCard('next'); await sleep(100)
  check('another device resumes at the same question with the typed value', /minimum profit/.test(device2.q()) && device2.$('#su-in-firmMin').value === '1', device2.q())
  await device2.next(); await until(() => !device2.open())
  check('next action after the payout target: Add firm rules', device2.cardAction() === 'Add firm rules', device2.cardAction())
  check('payout saved from device 2; next: firm rules', /My payout target · \$2,500 \(5% of starting size\)/.test(device2.card()) && /Firm minimum for payout · 1% of starting size \(\$500\)/.test(device2.card()), device2.card())
  // ── unknown reference: daily limit on start-of-day equity ──
  device2.$('#mtApiSetupCard [data-setup="firm"]').click(); await sleep(80)
  device2.pick('firmDaily', 'percent'); device2.type('#su-in-firmDaily', '4'); device2.pick('firmDailyBase', 'sod_equity')
  check('unknown reference: says why it cannot convert', /start-of-day equity, which TaliTrade does not have/.test(device2.$('#su-note-firmDaily').textContent), device2.$('#su-note-firmDaily').textContent)
  device2.pick('firmDaily', 'amount')
  check('amount view stays empty; entry kept as typed', device2.$('#su-in-firmDaily').value === '' && /Your entry of 4% is kept/.test(device2.$('#su-note-firmDaily').textContent))
  device2.pick('firmDaily', 'percent'); await device2.next()
  device2.type('#su-in-firmOverall', '2500'); device2.pick('overallMode', 'static'); await device2.next(); await until(() => !device2.open())
  const fr = device2.profileOnServer('1001', '1842').firmRules
  check('daily limit saved as entered with its reference', fr.dailyLoss.value === '4' && fr.dailyLoss.unit === 'percent' && fr.dailyLoss.base === 'sod_equity', JSON.stringify(fr))
  check('card shows both firm rules', /Firm daily loss · 4% of start-of-day equity · needs each day’s reference/.test(device2.card()) && /Firm overall drawdown · \$2,500 \(5% of starting size\) · static/.test(device2.card()), device2.card())
  check('no page errors (master)', device2.errors.length === 0, device2.errors.join(' | '))
  device2.w.close()
}

// ── 5. failed save keeps the answer and retries; older tab cannot overwrite newer answers ──
section('save failure, retry, conflict')
{
  const server = createServer()
  const a = server.addLinked('1001', 'fp')
  const tabA = await boot(server), tabB = await boot(server)
  await until(() => tabA.cardAction() === 'Set up account' && tabB.cardAction() === 'Set up account')
  await tabA.clickCard('next'); tabA.pick('type', 'master')
  server.failNextPut = 1
  await tabA.next(); await sleep(150)
  check('failed save: still on the question, answer still selected, Retry offered', /Which account/.test(tabA.q()) && !!tabA.$('#setup-sheet [data-name="type"][data-value="master"].on') && !!tabA.$('#setup-sheet [data-act="retry"]') && /Your answer is still here/.test(tabA.body()), tabA.body())
  const putsBefore = server.puts.length
  tabA.act('retry'); await sleep(200)
  check('retry saves (same save id) and moves on', /What size/.test(tabA.q()) && server.puts[putsBefore].body.save_id === server.puts[putsBefore - 1].body.save_id, tabA.q())
  check('server holds version 1', server.profiles['1001|1842'].version === 1)
  // tab B is still on version 0 from its load
  await tabB.clickCard('next'); tabB.pick('type', 'evaluation'); await tabB.next(); await sleep(150)
  check('older tab is refused and shown the newer answers', /changed in another tab or on another device/.test(tabB.body()) && server.profiles['1001|1842'].data.type === 'master', tabB.body())
  check('older tab stays on its question with its choice kept', /Which account/.test(tabB.q()) && !!tabB.$('#setup-sheet [data-name="type"][data-value="evaluation"].on'), tabB.q())
  tabA.w.close(); tabB.w.close()
  void a
}

// ── 6. switching accounts: during sync, and during a save ──
section('switching accounts during sync and during a save')
{
  const server = createServer()
  const first = server.addLinked('1001', 'fp')
  const p = await boot(server)
  await p.link(TOKENS.eu)
  const second = server.accounts.find((x) => x.login === '7730')
  await p.switchTo('mtapi:' + first.id)
  await until(() => p.w.TaliAccountSetup.hasPendingInvite('mtapi:' + second.id))
  await sleep(900)
  check('no invitation over the account being viewed', !p.open())
  p.w.eval('openAcctSheet()'); await sleep(100)
  check('switcher marks the synced account Ready to personalize', /Ready to personalize/.test(p.$('#acct-sheet').textContent))
  p.w.eval('closeAcctSheet()')
  await p.switchTo('mtapi:' + second.id); await until(() => p.open())
  check('invitation waits for the trader to return to that account', p.title() === 'Account detected' && /···· 7730/.test(p.body()))
  p.act('dashboard'); await sleep(100)
  // a save that is slow to answer, then a switch to the other account while it is in flight
  await p.clickCard('next'); p.pick('type', 'master')
  let release; server.holdPut = new Promise((r) => { release = r })
  await p.next()
  await p.switchTo('mtapi:' + first.id)
  release(); server.holdPut = null; await sleep(300)
  check("the other account's card never shows this save", !/Master/.test(p.card()) && /FundingPips/.test(p.card()) && /Set up account/.test(p.cardAction()), p.card())
  check('the save landed on its own account', (p.profileOnServer('1001', '7730') || {}).type === 'master' && !p.profileOnServer('1001', '1842'))
  await p.switchTo('mtapi:' + second.id); await until(() => /Resume setup/.test(p.cardAction()))
  check('back on the saving account: its own progress', /Resume setup/.test(p.cardAction()) && /Master \/ funded/.test(p.card()), p.card())
  check('no page errors (switching)', p.errors.length === 0, p.errors.join(' | '))
  p.w.close()
}

// ── 7. first sync fails, the existing retry succeeds ──
section('first sync failure and retry')
{
  const server = createServer()
  const p = await boot(server)
  await p.link(TOKENS.bad); await sleep(6500)
  const bad = server.accounts.find((x) => x.login === '5567')
  check('failed first sync: no invitation, nothing pending', !p.open() && !p.w.TaliAccountSetup.hasPendingInvite('mtapi:' + bad.id))
  p.w.eval(`TaliMatchTrader.manualSync('mtapi:${bad.id}')`)
  await until(() => p.open())
  check('invitation after the retry succeeds', p.title() === 'Account detected' && /···· 5567/.test(p.body()))
  p.w.close()
}

// ── 8. changing the account type explains first and keeps both sets of answers ──
section('Evaluation ⇄ Master')
{
  const server = createServer()
  server.addLinked('1001', 'fp')
  server.profiles['1001|1842'] = { version: 1, lastSaveId: 'seed', data: { status: 'saved', type: 'evaluation', size: { amount: '50000', currency: 'USD' },
    evaluation: { phaseCount: 2, currentPhase: 1, phaseTarget: { value: '8', unit: 'percent', base: 'starting' } }, done: { basics: true, phase: true } } }
  const p = await boot(server)
  await until(() => /Evaluation/.test(p.card()))
  await p.clickCard('edit'); p.pick('type', 'master'); await p.next()
  check('explains before switching', p.title() === 'Change account type?' && /2 phases/.test(p.body()) && /Current phase: Phase 1/.test(p.body()) && /set aside, not deleted/.test(p.body()), p.body())
  check('nothing saved before confirming', server.profiles['1001|1842'].version === 1)
  p.act('confirmSwitch'); await sleep(150)
  p.pick('sizePreset', '50000'); await p.next(); p.pick('goal', 'grow'); await p.next(); await until(() => !p.open())
  let d = p.profileOnServer('1001', '1842')
  check('master answers saved, evaluation answers kept', d.type === 'master' && d.master.goal.id === 'grow' && d.evaluation.phaseCount === 2, JSON.stringify(d))
  check('card notes the set-aside answers', /Evaluation answers are set aside/.test(p.card()), p.card())
  await p.clickCard('edit'); p.pick('type', 'evaluation'); await p.next()
  check('switching back explains too', p.title() === 'Change account type?' && /Goal: Grow the account/.test(p.body()) && /earlier Evaluation answers will be used again/.test(p.body()), p.body())
  p.act('confirmSwitch'); await sleep(150); p.act('later'); await until(() => !p.open())
  check('evaluation answers come back', /Phase 1 of 2/.test(p.card()) && /Phase target · 8% of starting size \(\$4,000\)/.test(p.card()), p.card())
  check('no page errors (type switch)', p.errors.length === 0, p.errors.join(' | '))
  p.w.close()
}

// ── 9. two traders ──
section('two traders')
{
  const server = createServer()
  server.addLinked('1001', 'fp')
  server.profiles['1001|1842'] = { version: 1, lastSaveId: 'seed', data: { status: 'saved', type: 'master', size: { amount: '50000', currency: 'USD' }, master: { goal: { id: 'payout' } }, done: { basics: true } } }
  const bob = server.addLinked('2002', 'eu')
  const p = await boot(server, 'session-bob')
  await until(() => p.cardAction() === 'Set up account')
  check("Bob sees only his account, with no profile", p.active() === 'mtapi:' + bob.id && !/50,000|Earn a payout/.test(p.card()), p.card())
  const res = await server.handle('http://localhost:8000/account-setup/connections/1', { method: 'GET', headers: { Authorization: 'Bearer session-bob' } })
  check("Bob's session cannot read Alice's account", res.status === 404)
  const put = await server.handle('http://localhost:8000/account-setup/connections/1', { method: 'PUT', headers: { Authorization: 'Bearer session-bob' }, body: JSON.stringify({ base_version: 1, save_id: 'bob-00000001', profile: { type: 'evaluation' } }) })
  check("…or write it", put.status === 404 && server.profiles['1001|1842'].data.type === 'master')
  check('every setup request from this page used Bob\'s session', server.setupRequests.every((r) => r.user === '2002'))
  p.w.close()
}

// ── 10. USD first; every amount in the account's own currency, whatever the browser language ──
section('USD first, currency per account')
{
  const server = createServer()
  const usd = server.addLinked('1001', 'fp'), eur = server.addLinked('1001', 'eu')
  const p = await boot(server, 'session-alice', { lang: 'de-DE' })
  await until(() => /From your broker/.test(p.card()))
  check('a fresh browser opens on the USD sample', p.active() === 'mtapi:' + usd.id, p.active())
  check('USD balance in dollars under a German browser', /balance \$49,820/.test(p.card()) && !/€/.test(p.card()), p.card())
  await p.switchTo('mtapi:' + eur.id); await until(() => /Sample Broker/.test(p.card()))
  check('EUR balance stays in euros', /€10,214\.50/.test(p.card()) && !/\$10,214/.test(p.card()), p.card())
  const stored = {}; for (let i = 0; i < p.w.localStorage.length; i++) { const k = p.w.localStorage.key(i); stored[k] = p.w.localStorage.getItem(k) }
  p.w.close()
  const again = await boot(server, 'session-alice', { lang: 'en-GB', storage: stored })
  check('the next visit opens the account selected last', again.active() === 'mtapi:' + eur.id, again.active())
  again.w.close()
}

// ── 11. Account detected → Importing → Imported ──
section('one-tap trade-history import')
{
  const server = createServer()
  const p = await boot(server)
  await p.link(TOKENS.fp); await until(() => p.open())
  check('Account detected shows broker, masked account and balance', p.title() === 'Account detected' && /FundingPips/.test(p.body()) && /···· 1842/.test(p.body()) && /\$49,820/.test(p.body()), p.body())
  p.$('#setupPrimary').click(); p.$('#setupPrimary') && p.$('#setupPrimary').click()       // double tap
  check('Importing appears at once, before any number is known', p.title() === 'Importing trade history…', p.title())
  await sleep(300)
  check('a double tap starts one import', server.importPosts.length === 1, server.importPosts.length)
  const seen = []
  await until(() => { seen.push([p.title(), (p.$('.su-big-n') || {}).textContent, p.body()]); return p.title() === 'Trade history imported' }, 20000)
  const counts = seen.filter((x) => x[0] === 'Importing trade history…' && x[1]).map((x) => Number(x[1].replace(/,/g, '')))
  check('progress shows committed counts that only go up, with the dates checked', counts.length >= 1 && counts.every((n, i) => !i || n >= counts[i - 1]) && seen.some((x) => /Fully checked so far/.test(x[2]) && /today/.test(x[2])), JSON.stringify(counts))
  check('no invented percentages', !seen.some((x) => /%/.test(x[2])))
  check('Imported: count, covered dates, Continue to account setup', (p.$('.su-big-n') || {}).textContent === '572' && /Dates checked/.test(p.body()) && /Earliest trade found/.test(p.body()) && !!p.$('#setup-sheet [data-act="continueSetup"]'), p.body())
  check('says what is not known about retention', /does not say how long it keeps old trades/.test(p.body()))
  p.act('continueSetup'); await until(() => /Which account/.test(p.q()))
  check('Continue goes to the approved account questions', /Which account did you connect\?/.test(p.q()))
  p.act('later'); await sleep(200)
  check('account card has no duplicate importer', !p.$('#importHistoryAction'))
  check('no page errors (import)', p.errors.length === 0, p.errors.join(' | '))
  p.w.close()
}

// ── 12. Later, leaving the sheet, reload, switching accounts mid-import ──
section('Later, leave, reload and switch during an import')
{
  const server = createServer()
  server.importSteps = 6
  const other = server.addLinked('1001', 'eu')
  const p = await boot(server)
  await p.link(TOKENS.fp); await until(() => p.open())
  const fp = server.accounts.find((a) => a.login === '1842')
  p.act('import'); await until(() => /Importing/.test(p.title()))
  p.act('later'); await sleep(200)
  check('dashboard shows recent trades instead of import controls', !p.$('#importHistoryAction') && !p.$('#mtApiHistoryControls') && /Recent trades/.test(p.$('#mtApiPanelContent').textContent))
  await p.switchTo('mtapi:' + other.id); await sleep(400)
  check("the other account's card never shows this import", !/Importing/.test(p.card()) && /Sample Broker/.test(p.card()), p.card())
  const stored = {}; for (let i = 0; i < p.w.localStorage.length; i++) { const k = p.w.localStorage.key(i); stored[k] = p.w.localStorage.getItem(k) }
  p.w.close()
  stored.tali_pref_active_acct = 'mtapi:' + fp.id
  const reload = await boot(server, 'session-alice', { storage: stored })
  await until(() => /FundingPips/.test(reload.card()))
  check('returning to the account does not reopen onboarding', !reload.open())
  check('one import job for this account', server.importPosts.filter((x) => x.id === fp.id).length === 1)
  reload.w.close()
}

// ── 13. interrupted with partial progress, Retry; and a genuinely empty account ──
section('paused import and retry; empty account')
{
  const server = createServer()
  const p = await boot(server)
  await p.link(TOKENS.fp); await until(() => p.open())
  const fp = server.accounts.find((a) => a.login === '1842')
  server.failImport[fp.id] = 1
  p.act('import'); await until(() => p.title() === 'Import paused')
  check('paused: what was saved, the dates checked, Retry or Continue setup', /closed trades saved so far/.test(p.body()) && /Fully checked so far/.test(p.body()) && !!p.$('#setup-sheet [data-act="retryImport"]') && !!p.$('#setup-sheet [data-act="continueSetup"]'), p.body())
  p.act('retryImport'); await until(() => p.title() === 'Trade history imported', 20000)
  check('Retry resumes and finishes', p.title() === 'Trade history imported' && server.importPosts.filter((x) => x.retry).length === 1)
  p.act('later'); await sleep(100)
  await p.link(TOKENS.empty); await until(() => p.open() && /···· 3301/.test(p.body()))
  p.act('import'); await until(() => p.title() === 'No closed trades found', 20000)
  check('empty account: No closed trades found, with dates and the reason', /no closed trades for any date since this account was opened/.test(p.body()) && /Dates checked/.test(p.body()) && !!p.$('#setup-sheet [data-act="continueSetup"]'), p.body())
  p.w.close()
}

// ── 14. another trader ──
section('another trader and the import')
{
  const server = createServer()
  const a = server.addLinked('1001', 'fp')
  const post = await server.handle(`http://localhost:8000/connectors/matchtrader-api/connections/${a.id}/history-import`, { method: 'POST', headers: { Authorization: 'Bearer session-bob' }, body: '{}' })
  const get = await server.handle(`http://localhost:8000/connectors/matchtrader-api/connections/${a.id}/history-import`, { method: 'GET', headers: { Authorization: 'Bearer session-bob' } })
  check('another signed-in trader can neither start nor read the import', post.status === 404 && get.status === 404 && server.importPosts.length === 0)
}

check('no unhandled rejections', pageErrors.length === 0, pageErrors.join(' | '))
console.log(`\n${pass}/${pass + fail} account-setup flow checks passed`)
process.exit(fail ? 1 : 0)
