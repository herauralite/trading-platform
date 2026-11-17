/* TaliTrade account setup.
   Optional questions a trader answers about a linked account: what kind of account it is, the
   starting size the firm issued, a goal, and (separately, later) firm rules and personal limits.
   Answers are the trader's own words, saved per trader and per account on the server
   (/account-setup, behind a server flag). Broker-reported facts are shown next to them and are
   never turned into answers: Match-Trader reports REAL/DEMO, currency and a current balance, not
   a starting size, an evaluation phase or program rules.

   Saving: every save carries the version it was based on and a save id. A newer save elsewhere
   (another tab or device) is never overwritten; a repeated save (retry) is harmless. A failed save
   keeps what the trader typed on screen and offers Retry. Saves for one account never touch the
   screen of another. */
(function (global) {
  'use strict';
  var doc = global.document;
  var host = null;
  var ui = { open: false, mode: null, key: null, flow: null, step: null, confirm: null, snapshot: null, notice: '', saving: false, error: null, retry: null };
  var pendingInvite = {};          // memory only: a reload never brings an invitation back
  var cache = {};                  // account key -> { loaded, loading, version, saved, working, detected, fetchedAt, unsaved, chain }
  var session = null;              // cache belongs to one signed-in session
  var listLoad = null;
  var imports = {};                // account key -> { status, fetchedAt, loading } : the history import, as the server reports it
  var IMPORT_POLL_MS = 2000, CARD_POLL_MS = 5000;
  var PRESETS = [5000, 10000, 25000, 50000, 100000];
  var REFRESH_MS = 15000;          // re-read an account's answers when its card is shown again (other devices)

  // ── server ──
  function connectionId(key) { var m = /^mtapi:(\d+)$/.exec(String(key || '')); return m ? Number(m[1]) : null; }
  function token() { return host && host.getSessionToken ? host.getSessionToken() : ''; }
  function checkSession() { var t = token(); if (t !== session) { session = t; cache = {}; imports = {}; pendingInvite = {}; listLoad = null; } return t; }
  function request(method, path, body) { return call(method, '/account-setup' + path, body); }
  function call(method, path, body) {
    var t = checkSession();
    if (!t) return Promise.reject({ status: 401, message: 'You are signed out.' });
    var init = { method: method, headers: { Authorization: 'Bearer ' + t }, cache: 'no-store' };
    if (body !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(body); }
    return global.fetch(host.getApiBase() + path, init).then(function (res) {
      return res.text().then(function (raw) {
        var data = null; try { data = raw ? JSON.parse(raw) : null; } catch (e) { data = null; }
        if (!res.ok) {
          var detail = data && data.detail;
          throw { status: res.status, data: data, message: typeof detail === 'string' ? detail : detail && detail.message ? detail.message : 'Request failed (' + res.status + ').' };
        }
        return data || {};
      });
    }, function () { throw { status: 0, message: 'Could not reach TaliTrade. Check your connection and try again.' }; });
  }
  function editing(key) { return ui.open && ui.key === key && ui.mode === 'flow'; }
  function enabled() { return !!(host && host.isEnabled && host.isEnabled()); }
  function entry(key) {
    checkSession();
    return cache[key] || (cache[key] = { loaded: false, loading: null, version: 0, saved: null, working: null, detected: null, fetchedAt: 0, unsaved: false, chain: Promise.resolve() });
  }
  function adopt(key, version, profile, extra) {
    var e = entry(key);
    e.version = version || 0;
    e.saved = profile ? clone(profile) : null;
    e.working = normalize(profile);
    e.loaded = true; e.fetchedAt = Date.now(); e.unsaved = false;
    if (extra && extra.detected) e.detected = extra.detected;
  }
  function loadAll() {
    if (!enabled()) return Promise.resolve();
    if (listLoad) return listLoad;
    var mine = checkSession();
    listLoad = request('GET', '/profiles').then(function (body) {
      if (mine !== token()) return;
      (body.profiles || []).forEach(function (p) {
        var key = 'mtapi:' + p.connection_id, e = entry(key);
        if (!e.unsaved && !editing(key)) adopt(key, p.version, p.profile);
      });
      refreshVisible();
    }, function (err) { listLoad = null; if (err && err.status === 401 && host.onSessionRejected) host.onSessionRejected(); });
    return listLoad;
  }
  function loadOne(key, force) {
    var e = entry(key), id = connectionId(key);
    if (id === null || !enabled()) return Promise.resolve();
    if (e.loading) return e.loading;
    if (!force && e.loaded && e.detected && Date.now() - e.fetchedAt < REFRESH_MS) return Promise.resolve();
    if (e.unsaved || editing(key)) return Promise.resolve();   // never replace answers being edited
    var mine = session;
    e.loading = request('GET', '/connections/' + id).then(function (body) {
      e.loading = null;
      if (mine !== token() || e.unsaved || editing(key)) return;
      adopt(key, body.version, body.profile, body);
      refreshVisible();
    }, function (err) {
      e.loading = null;
      if (err && err.status === 401 && host.onSessionRejected) host.onSessionRejected();
      e.loadError = err && err.message; refreshVisible();
    });
    return e.loading;
  }
  function newSaveId() {
    var a = new Uint8Array(12);
    try { global.crypto.getRandomValues(a); } catch (e) { for (var i = 0; i < a.length; i++) a[i] = Math.floor(Math.random() * 256); }
    return 's' + Array.prototype.map.call(a, function (b) { return ('0' + b.toString(16)).slice(-2); }).join('');
  }
  // Saves for one account run one after another, each based on the version the previous one produced.
  // ``attempt`` = { key, profile, saveId }: resending the same attempt (Retry) reuses its save id.
  function save(attempt) {
    var e = entry(attempt.key), id = connectionId(attempt.key), mine = session;
    e.unsaved = true;
    var run = e.chain.then(function () {
      if (mine !== token()) throw { status: 401, message: 'You are signed out.' };
      return request('PUT', '/connections/' + id, { base_version: e.version, save_id: attempt.saveId, profile: toServer(attempt.profile) });
    }).then(function (body) {
      if (mine !== token()) return { ok: false };
      e.version = body.version; e.saved = clone(body.profile); e.fetchedAt = Date.now(); e.unsaved = false;
      return { ok: true, profile: body.profile };
    }, function (err) {
      if (mine !== token()) return { ok: false };
      if (err && err.status === 409 && err.data) {
        e.version = err.data.version; e.saved = err.data.profile ? clone(err.data.profile) : null; e.fetchedAt = Date.now();
        return { ok: false, conflict: true, profile: err.data.profile };
      }
      if (err && err.status === 401 && host.onSessionRejected) host.onSessionRejected();
      return { ok: false, message: err && err.message || 'Could not save.' };
    });
    e.chain = run.then(function () {}, function () {});
    return run;
  }

  // ── trade-history import (Match-Trader routes; the server owns the job and its progress) ──
  function importPath(key) { return '/connectors/matchtrader-api/connections/' + connectionId(key) + '/history-import'; }
  function importOf(key) { return imports[key] || (imports[key] = { status: null, fetchedAt: 0, loading: null, error: null }); }
  function adoptImport(key, body) {
    var im = importOf(key);
    im.status = body; im.fetchedAt = Date.now(); im.error = null;
    if (ui.open && ui.key === key && ui.mode === 'import') rerender();
    refresh(key, false);
  }
  function loadImport(key, force) {
    var im = importOf(key), mine = session;
    if (connectionId(key) === null || !enabled()) return Promise.resolve();
    if (im.loading) return im.loading;
    if (!force && im.status && Date.now() - im.fetchedAt < (im.status.state === 'importing' ? IMPORT_POLL_MS : CARD_POLL_MS * 6)) return Promise.resolve();
    im.loading = call('GET', importPath(key)).then(function (body) { im.loading = null; if (mine === token()) adoptImport(key, body); },
      function (err) { im.loading = null; im.error = err && err.message; if (err && err.status === 401 && host.onSessionRejected) host.onSessionRejected(); });
    return im.loading;
  }
  function startImport(key, retry) {
    var im = importOf(key), mine = session;
    im.starting = true; im.error = null;
    if (ui.open && ui.key === key) rerender();
    return call('POST', importPath(key), retry ? { retry: true } : {}).then(function (body) {
      im.starting = false; if (mine === token()) adoptImport(key, body);
    }, function (err) {
      im.starting = false; im.error = (err && err.message) || 'Could not start the import.';
      if (ui.open && ui.key === key) rerender();
      refresh(key, false);
    });
  }
  // While an import runs, its own screen and the visible card keep reading progress from the server.
  var importTimer = null;
  function tickImports() {
    importTimer = null;
    if (!enabled() || !host) return;
    var keys = {};
    if (ui.open && ui.mode === 'import' && ui.key) keys[ui.key] = true;
    var active = host.getActiveAccount(); if (active && imports[active] && imports[active].status && imports[active].status.state === 'importing') keys[active] = true;
    Object.keys(keys).forEach(function (k) { loadImport(k, true); });
    if (Object.keys(keys).length) importTimer = global.setTimeout(tickImports, IMPORT_POLL_MS);
  }
  function keepPolling() { if (!importTimer) importTimer = global.setTimeout(tickImports, IMPORT_POLL_MS); }

  // ── helpers ──
  function clone(o) { return o == null ? o : JSON.parse(JSON.stringify(o)); }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function num(v) { var n = parseFloat(String(v == null ? '' : v).replace(/[,\s]/g, '')); return isFinite(n) ? n : null; }
  function money(amount, currency) {
    if (amount == null) return '—';
    try { return new Intl.NumberFormat('en-US', { style: 'currency', currency: currency || 'USD', minimumFractionDigits: amount % 1 ? 2 : 0, maximumFractionDigits: 2 }).format(amount); }
    catch (e) { return (currency || '') + ' ' + amount; }
  }
  function trim(n) { return String(Math.round(n * 100) / 100); }
  function account(key) { return (host && host.getAccount && host.getAccount(key)) || { broker: 'Account', ending: '', currency: 'USD' }; }
  function cur(key, p) { return (p && p.size && p.size.currency) || account(key).currency || 'USD'; }
  function accountTitle(key) { var a = account(key); return a.broker + (a.ending ? ' · ending ' + a.ending : ''); }
  function when(iso) {
    var d = iso ? new Date(iso) : null;
    if (!d || isNaN(d.getTime())) return '—';
    return d.toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
  }
  function reportedKind(t) { t = String(t || '').toUpperCase(); return t === 'DEMO' ? 'Demo account' : t === 'REAL' ? 'Real (live) account' : ''; }

  // The working copy always has every section, so the flow code never checks for missing objects.
  function normalize(p) {
    p = clone(p) || {};
    var out = { status: p.status || 'new', type: p.type || null, size: null, generalGoal: p.generalGoal || null,
                evaluation: p.evaluation || {}, master: p.master || {}, firmRules: p.firmRules || {}, personal: p.personal || {},
                done: p.done || {}, resume: p.resume || null, draft: p.draft || {} };
    if (p.size && num(p.size.amount)) out.size = { amount: num(p.size.amount), currency: p.size.currency };
    ['evaluation', 'master', 'firmRules', 'personal'].forEach(function (s) {
      Object.keys(out[s]).forEach(function (k) { var v = out[s][k]; if (v && typeof v === 'object' && 'unit' in v) v.value = num(v.value); });
    });
    return out;
  }
  // What the server stores: empty sections dropped, money as strings.
  function toServer(p) {
    var out = clone(p);
    if (out.size) out.size = { amount: String(out.size.amount), currency: out.size.currency };
    ['evaluation', 'master', 'firmRules', 'personal'].forEach(function (s) {
      var sec = out[s] || {};
      Object.keys(sec).forEach(function (k) {
        var v = sec[k];
        if (v == null || v === '') delete sec[k];
        else if (typeof v === 'object' && 'unit' in v) sec[k] = { value: String(v.value), unit: v.unit, base: v.base || undefined };
      });
      if (!Object.keys(sec).length) delete out[s]; else out[s] = sec;
    });
    ['type', 'size', 'generalGoal', 'resume'].forEach(function (k) { if (out[k] == null) delete out[k]; });
    return out;
  }

  // ── catalogue ──
  var TYPES = { evaluation: 'Evaluation', master: 'Master / funded' };
  var GOALS = {
    payout: { label: 'Earn a payout',         action: 'Set my payout target', flow: 'payout' },
    grow:   { label: 'Grow the account',      action: 'Set my growth target', flow: 'growth' },
    limits: { label: 'Stay within my limits', action: 'Set my risk limits',   flow: 'personal' },
    other:  { label: 'Other',                 action: 'Set my risk limits',   flow: 'personal' }
  };
  function goalChoices(type) { return type === 'master' ? ['payout', 'grow', 'limits', 'other'] : ['grow', 'limits', 'other']; }
  function goalOf(p) { return p.type === 'master' ? p.master.goal || null : p.type ? null : p.generalGoal; }
  function goalText(g) { return g ? (g.id === 'other' ? g.text : GOALS[g.id].label) : ''; }
  var BASES = {
    starting:          { label: 'starting size',           known: function (p) { return p.size ? p.size.amount : null; } },
    sod_balance:       { label: 'start-of-day balance',    known: function () { return null; } },
    sod_equity:        { label: 'start-of-day equity',     known: function () { return null; } },
    high_water:        { label: 'highest balance reached', known: function () { return null; } },
    high_water_equity: { label: 'highest equity reached',  known: function () { return null; } }
  };
  var FLOW_TITLES = { basics: 'Personalize this account', phase: 'Phase target', payout: 'Payout target', growth: 'Growth target', firm: 'Firm rules', personal: 'Personal risk limits' };
  function stepsFor(flow, p) {
    var d = p.draft;
    if (flow === 'basics' || flow === 'phase') {
      var type = d.type !== undefined ? d.type : p.type, steps = flow === 'basics' ? ['type', 'size'] : [];
      if (type === 'evaluation') {
        var n = d.phaseCount || p.evaluation.phaseCount;
        return steps.concat(n && n > 1 ? ['phaseCount', 'currentPhase', 'phaseTarget'] : ['phaseCount', 'phaseTarget']);
      }
      return flow === 'basics' ? steps.concat(['goal']) : ['phaseCount', 'phaseTarget'];
    }
    if (flow === 'payout') return ['payoutTarget', 'firmMin'];
    if (flow === 'growth') return ['growthTarget'];
    if (flow === 'firm') return ['firmDaily', 'firmOverall'];
    return ['personal', 'refine'];
  }

  // A money entry keeps what the trader typed: { value, unit: 'amount'|'percent', base }.
  // The other unit is shown only when its reference is a confirmed number; the entry itself never changes.
  function convert(entry, p) {
    if (!entry || entry.value == null) return null;
    var base = BASES[entry.base] ? BASES[entry.base].known(p) : null;
    if (!base) return null;
    return entry.unit === 'amount' ? { unit: 'percent', value: entry.value / base * 100 } : { unit: 'amount', value: entry.value * base / 100 };
  }
  function entryText(entry, p, key) {
    if (!entry || entry.value == null) return '';
    var c = cur(key, p), baseLabel = BASES[entry.base] ? BASES[entry.base].label : 'reference', other = convert(entry, p);
    if (entry.unit === 'amount') return money(entry.value, c) + (other ? ' (' + trim(other.value) + '% of ' + baseLabel + ')' : '');
    return trim(entry.value) + '% of ' + baseLabel + (other ? ' (' + money(other.value, c) + ')' : '');
  }

  // ── widgets (HTML strings; every trader-supplied value goes through esc) ──
  var CHECK = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="20 6 9 17 4 12"/></svg>';
  function choices(name, label, options, selected, chips) {
    var anySelected = options.some(function (o) { return String(o.id) === String(selected); });
    return '<div class="su-group' + (chips ? ' su-chips' : '') + '" role="radiogroup" aria-label="' + esc(label) + '">' + options.map(function (o, i) {
      var on = String(o.id) === String(selected);
      return '<button type="button" role="radio" aria-checked="' + on + '" tabindex="' + (on || (!anySelected && i === 0) ? 0 : -1) + '" class="su-opt rip' + (on ? ' on' : '') +
        '" data-act="pick" data-name="' + esc(name) + '" data-value="' + esc(o.id) + '"><span class="su-opt-text"><span class="su-opt-label">' + esc(o.label) + '</span>' +
        (o.sub ? '<span class="su-opt-sub">' + esc(o.sub) + '</span>' : '') + '</span><span class="su-check">' + CHECK + '</span></button>';
    }).join('') + '</div>';
  }
  function baseOf(name, p) {
    var d = p.draft;
    if (name === 'firmDaily') return d.firmDailyBase || null;
    if (name === 'firmOverall') return d.overallMode === 'static' ? 'starting' : d.overallMode === 'trailing' ? (d.overallRef === 'equity' ? 'high_water_equity' : d.overallRef === 'balance' ? 'high_water' : null) : null;
    return 'starting';
  }
  function draftEntry(name, p) {
    var m = p.draft[name], v = m ? num(m.raw) : null;
    return v != null && v > 0 ? { value: v, unit: m.unit, base: baseOf(name, p) } : null;
  }
  function moneyShown(name, p) {
    var m = p.draft[name] || { raw: '', unit: 'amount', view: 'amount' };
    if (m.view === m.unit) return m.raw;
    var other = convert(draftEntry(name, p), p);
    return other ? trim(other.value) : '';
  }
  function moneyNote(name, p, key) {
    var m = p.draft[name] || { raw: '', unit: 'amount', view: 'amount' };
    var entry = draftEntry(name, p), c = cur(key, p), baseId = baseOf(name, p), base = BASES[baseId];
    if (!entry) return base ? 'Percent is measured against your ' + base.label + '.' : 'Choose what this is measured from to compare amount and percent.';
    var typed = entry.unit === 'amount' ? money(entry.value, c) : trim(entry.value) + '%';
    var other = convert(entry, p);
    if (other) return typed + ' = ' + (other.unit === 'amount' ? money(other.value, c) : trim(other.value) + '%') + ' · using the ' + base.label + ' you entered, ' + money(base.known(p), c) + '.';
    var why = !base ? 'what this is measured from' : baseId === 'starting' ? 'your starting size, which you have not entered' : 'your ' + base.label + ', which TaliTrade does not have';
    if (m.view !== m.unit) return 'Showing this as ' + (m.view === 'percent' ? 'a percent' : 'an amount') + ' needs ' + why + '. Your entry of ' + typed + ' is kept as entered.';
    return 'Saved as ' + typed + (entry.unit === 'percent' && base ? ' of ' + base.label : '') + '. Converting needs ' + why + '.';
  }
  function moneyField(name, label, p, key) {
    var m = p.draft[name] || { raw: '', unit: 'amount', view: 'amount' }, c = cur(key, p);
    return '<div class="su-field" data-money="' + name + '"><div class="su-field-top"><label class="su-label" for="su-in-' + name + '">' + esc(label) + '</label>' +
      '<div class="su-seg" role="radiogroup" aria-label="' + esc(label) + ' unit">' + ['percent', 'amount'].map(function (u) {
        var on = m.view === u;
        return '<button type="button" role="radio" aria-checked="' + on + '" tabindex="' + (on ? 0 : -1) + '" class="su-seg-btn' + (on ? ' on' : '') + '" data-act="unit" data-name="' + name + '" data-value="' + u + '">' + (u === 'amount' ? 'Amount' : '%') + '</button>';
      }).join('') + '</div></div>' +
      '<div class="su-input-wrap"><span class="su-affix">' + (m.view === 'amount' ? esc(c) : '%') + '</span>' +
      '<input class="su-input" id="su-in-' + name + '" inputmode="decimal" autocomplete="off" data-act="money" data-name="' + name + '" value="' + esc(moneyShown(name, p)) + '" placeholder="' + (m.view === 'amount' ? '0' : '0.0') + '" aria-describedby="su-note-' + name + '"></div>' +
      '<div class="su-note" id="su-note-' + name + '" aria-live="polite">' + esc(moneyNote(name, p, key)) + '</div></div>';
  }
  function fromEntry(e) { return e && e.value != null ? { raw: String(e.value), unit: e.unit, view: e.unit } : undefined; }

  // ── steps ──
  var MONEY_STEPS = { phaseTarget: 'evaluation', payoutTarget: 'master', firmMin: 'master', growthTarget: 'master' };
  var DRAFT_KEYS = { type: ['type'], size: ['size'], goal: ['goal'], phaseCount: ['phaseCount'], currentPhase: ['currentPhase'], phaseTarget: ['phaseTarget'],
    payoutTarget: ['payoutTarget'], firmMin: ['firmMin'], growthTarget: ['growthTarget'], firmDaily: ['firmDaily', 'firmDailyBase', 'firmDailyReset'],
    firmOverall: ['firmOverall', 'overallMode', 'overallRef'], personal: ['sessionBudget', 'riskPerTrade'], refine: ['style', 'reminder', 'alerts', 'previewShown'] };
  function seed(step, p) {                  // entering a step shows the answer already saved for it
    var d = p.draft, ev = p.evaluation, ms = p.master, fr = p.firmRules, pr = p.personal;
    function put(k, v) { if (d[k] === undefined && v !== undefined && v !== null) d[k] = v; }
    if (step === 'type') put('type', p.type);
    else if (step === 'size' && p.size) put('size', PRESETS.indexOf(p.size.amount) >= 0 ? { preset: p.size.amount, other: '' } : { preset: 'other', other: String(p.size.amount) });
    else if (step === 'goal') { var g = goalOf(p); put('goal', g ? { id: g.id, text: g.text || '' } : undefined); }
    else if (step === 'phaseCount') put('phaseCount', ev.phaseCount);
    else if (step === 'currentPhase') put('currentPhase', ev.currentPhase);
    else if (MONEY_STEPS[step]) put(step, fromEntry(p[MONEY_STEPS[step]][step]));
    else if (step === 'firmDaily') { put('firmDaily', fromEntry(fr.dailyLoss)); put('firmDailyBase', fr.dailyLoss && fr.dailyLoss.base); put('firmDailyReset', fr.dailyReset); }
    else if (step === 'firmOverall') { put('firmOverall', fromEntry(fr.overall)); put('overallMode', fr.overallMode); put('overallRef', fr.overallRef); }
    else if (step === 'personal') { put('sessionBudget', fromEntry(pr.sessionBudget)); put('riskPerTrade', fromEntry(pr.riskPerTrade)); }
    else if (step === 'refine') { put('style', pr.style); put('reminder', pr.reminder); put('alerts', pr.alerts ? { daily: !!pr.alerts.daily, budget: !!pr.alerts.budget } : undefined); }
  }
  function valid(step, p) {
    var d = p.draft;
    if (step === 'type') return !!d.type;
    if (step === 'size') return !!d.size && (d.size.preset === 'other' ? num(d.size.other) > 0 && num(d.size.other) <= 1e10 : !!d.size.preset);
    if (step === 'goal') return !!d.goal && !!d.goal.id && (d.goal.id !== 'other' || !!String(d.goal.text || '').trim());
    if (step === 'phaseCount') return !!d.phaseCount;
    if (step === 'currentPhase') return !!d.currentPhase && d.currentPhase <= (d.phaseCount || p.evaluation.phaseCount || 1);
    if (MONEY_STEPS[step]) { var e = draftEntry(step, p); return !!e && (e.unit !== 'percent' || e.value <= 1000); }
    if (step === 'firmDaily') return !!draftEntry('firmDaily', p) && !!d.firmDailyBase;
    if (step === 'firmOverall') return !!draftEntry('firmOverall', p) && !!d.overallMode && (d.overallMode === 'static' || !!d.overallRef);
    if (step === 'personal') return !!draftEntry('sessionBudget', p) || !!draftEntry('riskPerTrade', p);
    return true;
  }
  function commit(step, p, key) {           // a confirmed answer moves from the draft into the profile
    var d = p.draft, ev = p.evaluation, fr = p.firmRules, pr = p.personal;
    function drop() { Array.prototype.forEach.call(arguments, function (k) { delete d[k]; }); }
    if (step === 'type') { p.type = d.type; drop('type'); }
    else if (step === 'size') {
      p.size = { amount: d.size.preset === 'other' ? num(d.size.other) : Number(d.size.preset), currency: (p.size && p.size.currency) || account(key).currency || 'USD' };
      drop('size');
    }
    else if (step === 'goal') {
      var g = { id: d.goal.id, text: d.goal.id === 'other' ? String(d.goal.text).trim().slice(0, 80) : '' };
      if (p.type === 'master') p.master.goal = g; else p.generalGoal = g;
      drop('goal');
    }
    else if (step === 'phaseCount') { ev.phaseCount = d.phaseCount; if (ev.phaseCount === 1 || ev.currentPhase > ev.phaseCount) delete ev.currentPhase; drop('phaseCount'); if (ev.phaseCount === 1) drop('currentPhase'); }
    else if (step === 'currentPhase') { ev.currentPhase = d.currentPhase; drop('currentPhase'); }
    else if (MONEY_STEPS[step]) { p[MONEY_STEPS[step]][step] = draftEntry(step, p); drop(step); }
    else if (step === 'firmDaily') { fr.dailyLoss = draftEntry('firmDaily', p); if (d.firmDailyBase !== 'starting' && d.firmDailyReset) fr.dailyReset = d.firmDailyReset; else delete fr.dailyReset; drop('firmDaily', 'firmDailyBase', 'firmDailyReset'); }
    else if (step === 'firmOverall') { fr.overall = draftEntry('firmOverall', p); fr.overallMode = d.overallMode; if (d.overallMode === 'trailing') fr.overallRef = d.overallRef; else delete fr.overallRef; drop('firmOverall', 'overallMode', 'overallRef'); }
    else if (step === 'personal') {
      var b = draftEntry('sessionBudget', p), r = draftEntry('riskPerTrade', p);
      if (b) pr.sessionBudget = b; if (r) pr.riskPerTrade = r; drop('sessionBudget', 'riskPerTrade');
    }
    else if (step === 'refine') {
      if (d.style) pr.style = d.style;
      if (String(d.reminder || '').trim()) pr.reminder = String(d.reminder).trim().slice(0, 120); else delete pr.reminder;
      if (d.alerts) pr.alerts = { daily: !!d.alerts.daily, budget: !!d.alerts.budget };
      drop('style', 'reminder', 'alerts', 'previewShown');
    }
  }
  function brokerBalance(key) {
    var snap = host.getSnapshot && host.getSnapshot(key), det = entry(key).detected;
    if (snap && snap.summary && num(snap.summary.balance) != null) return { amount: num(snap.summary.balance), at: snap.summary.fetched_at };
    if (det && num(det.balance) != null) return { amount: num(det.balance), at: det.observed_at };
    return null;
  }
  function view(step, p, key) {
    var d = p.draft, c = cur(key, p);
    switch (step) {
      case 'type': return { title: 'Which account did you connect?', sub: 'Your broker reports only whether this is a real or demo account, not whether it is an evaluation or funded. Tell us which one it is.',
        body: choices('type', 'Account type', [{ id: 'evaluation', label: 'Evaluation', sub: "I'm working through a challenge" }, { id: 'master', label: 'Master / funded', sub: "I'm trading a funded account" }], d.type) };
      case 'size': {
        var sz = d.size || { preset: null, other: '' }, bal = brokerBalance(key);
        return { title: 'What size did your firm issue?', sub: (bal ? 'Your broker reports a current balance of ' + money(bal.amount, account(key).currency) + '. ' : '') + 'The starting size is not reported, and it can differ from your balance.',
          body: choices('sizePreset', 'Starting size in ' + c, PRESETS.map(function (n) { return { id: n, label: (n / 1000) + 'K' }; }).concat([{ id: 'other', label: 'Other' }]), sz.preset, true) +
            (sz.preset === 'other' ? '<div class="su-field"><label class="su-label" for="su-size-other">Starting size (' + esc(c) + ')</label><div class="su-input-wrap"><span class="su-affix">' + esc(c) +
              '</span><input class="su-input" id="su-size-other" inputmode="decimal" autocomplete="off" data-act="sizeOther" value="' + esc(sz.other) + '" placeholder="e.g. 15000"></div></div>' : '<div class="su-note">Amounts are in ' + esc(c) + ', the account currency.</div>') };
      }
      case 'goal': {
        var g = d.goal || { id: null, text: '' };
        return { title: "What's your main goal for this account?", sub: 'Pick the one that matters most right now. You can change it any time.',
          body: choices('goal', 'Main goal', goalChoices(d.type !== undefined ? d.type : p.type).map(function (id) { return { id: id, label: GOALS[id].label }; }), g.id) +
            (g.id === 'other' ? '<div class="su-field"><label class="su-label" for="su-goal-text">In your own words</label><input class="su-input su-input-text" id="su-goal-text" maxlength="80" autocomplete="off" data-act="goalText" value="' + esc(g.text) + '" placeholder="e.g. Trade only the London open"></div>' : '') };
      }
      case 'phaseCount': return { title: 'How many phases does this evaluation have?', sub: 'As your firm describes the challenge.',
        body: choices('phaseCount', 'Number of phases', [1, 2, 3].map(function (n) { return { id: n, label: n + (n === 1 ? ' phase' : ' phases') }; }), d.phaseCount, true) };
      case 'currentPhase': {
        var total = d.phaseCount || p.evaluation.phaseCount || 2, opts = [];
        for (var i = 1; i <= total; i++) opts.push({ id: i, label: 'Phase ' + i });
        return { title: 'Which phase are you in?', sub: 'Of the ' + total + ' phases in this evaluation.', body: choices('currentPhase', 'Current phase', opts, d.currentPhase, true) };
      }
      case 'phaseTarget': {
        var n = (p.evaluation.phaseCount > 1 && p.evaluation.currentPhase) ? 'Phase ' + p.evaluation.currentPhase : 'this phase';
        return { title: "What's the profit target for " + n + '?', sub: 'The profit your firm asks for before this phase is passed.', body: moneyField('phaseTarget', 'Profit target', p, key) };
      }
      case 'payoutTarget': return { title: 'How much profit do you want before your next payout?', sub: "Your own target. Your firm's minimum is the next question.", body: moneyField('payoutTarget', 'My payout target', p, key) };
      case 'firmMin': return { title: "What's your firm's minimum profit for a payout?", sub: 'Kept separately from your own target. Reaching it does not confirm payout eligibility — your firm decides that.', body: moneyField('firmMin', 'Firm minimum profit', p, key) };
      case 'growthTarget': return { title: 'How much do you want to grow this account?', sub: 'Profit you are aiming for, as an amount or a percent.', body: moneyField('growthTarget', 'Growth target', p, key) };
      case 'firmDaily': return { title: "What's your firm's daily loss limit?", sub: 'Only the daily rule here. Overall drawdown is the next question.',
        body: moneyField('firmDaily', 'Daily loss limit', p, key) + '<div class="su-label su-gap">Measured from</div>' +
          choices('firmDailyBase', 'Daily limit measured from', [{ id: 'starting', label: 'Starting size' }, { id: 'sod_balance', label: 'Start-of-day balance' }, { id: 'sod_equity', label: 'Start-of-day equity' }], d.firmDailyBase, true) +
          (d.firmDailyBase && d.firmDailyBase !== 'starting' ? '<div class="su-field"><label class="su-label" for="su-reset">Daily reset time (your firm\'s server time, optional)</label><input class="su-input su-input-text" id="su-reset" type="time" data-act="reset" value="' + esc(d.firmDailyReset || '') + '"></div>' : '') };
      case 'firmOverall': return { title: "What's your firm's overall drawdown limit?", sub: 'The most the account may lose in total.',
        body: moneyField('firmOverall', 'Overall drawdown', p, key) + '<div class="su-label su-gap">How it moves</div>' +
          choices('overallMode', 'Drawdown type', [{ id: 'static', label: 'Static' }, { id: 'trailing', label: 'Trailing' }], d.overallMode, true) +
          (d.overallMode === 'trailing' ? '<div class="su-label su-gap">Trails the highest</div>' + choices('overallRef', 'Trailing reference', [{ id: 'balance', label: 'Balance' }, { id: 'equity', label: 'Equity' }], d.overallRef, true) : '') };
      case 'personal': return { title: 'What are your own limits?', sub: "Yours, not the firm's. Fill in either one or both.",
        body: moneyField('sessionBudget', 'Loss budget per session', p, key) + moneyField('riskPerTrade', 'Risk per trade', p, key) };
      default: {
        var al = d.alerts || { daily: false, budget: false }, budget = draftEntry('sessionBudget', p) || p.personal.sessionBudget;
        return { title: 'Anything else? (optional)', sub: 'Trading style and reminders are optional. Skip any of them.',
          body: '<div class="su-label">Trading style</div>' + choices('style', 'Trading style', ['Scalping', 'Day trading', 'Swing'].map(function (x) { return { id: x, label: x }; }), d.style, true) +
            '<div class="su-field"><label class="su-label" for="su-reminder">A reminder in your words</label><input class="su-input su-input-text" id="su-reminder" maxlength="120" autocomplete="off" data-act="reminder" value="' + esc(d.reminder || '') + '" placeholder="e.g. Two losses in a row, walk away"></div>' +
            '<div class="su-label su-gap">Alert preferences</div>' +
            '<label class="su-toggle"><input type="checkbox" data-act="alert" data-name="daily"' + (al.daily ? ' checked' : '') + '><span>Tell me when I am close to the daily loss limit</span></label>' +
            '<label class="su-toggle"><input type="checkbox" data-act="alert" data-name="budget"' + (al.budget ? ' checked' : '') + '><span>Tell me when my session budget is used up</span></label>' +
            '<button type="button" class="btn btn-ghost rip su-preview-btn" data-act="preview">Show a preview alert</button>' +
            (d.previewShown ? '<div class="su-preview" role="status"><div class="su-preview-tag">PREVIEW · NOT A LIVE ALERT</div><div class="su-preview-title">' + esc(accountTitle(key)) + '</div><div>Session budget reached' +
              (budget ? ' · ' + esc(entryText(budget, p, key)) : '') + '.' + (d.reminder ? ' “' + esc(d.reminder) + '”' : '') + '</div><div class="su-preview-foot">Nothing was sent. Alerts are not connected yet.</div></div>' : '') };
      }
    }
  }

  // ── styles (reuses the dashboard palette and sheet look) ──
  var CSS = '#setup-overlay{position:fixed;inset:0;background:rgba(0,0,0,0);z-index:370;pointer-events:none;transition:background .28s}' +
    '#setup-overlay.open{background:rgba(0,0,0,.5);pointer-events:all}' +
    '#setup-sheet{position:fixed;left:0;right:0;bottom:0;z-index:371;background:var(--s1);border-top:1px solid var(--border);border-radius:20px 20px 0 0;max-width:680px;margin:0 auto;transform:translateY(102%);transition:transform .38s var(--out);padding-bottom:max(14px,env(safe-area-inset-bottom));display:flex;flex-direction:column;max-height:88vh;visibility:hidden}' +
    '#setup-sheet.open{transform:translateY(0);visibility:visible}#setup-sheet:focus{outline:none}' +
    '#setup-sheet .aaf-body{padding:2px 16px 10px;overflow-y:auto;flex:1;min-height:0}' +
    '#setup-sheet .aaf-footer{padding:10px 16px 0;flex-wrap:wrap;border-top:1px solid var(--border)}' +
    '.su-q{font-size:17px;font-weight:700;color:var(--text);line-height:1.3;margin:4px 0 4px}.su-sub{font-size:12.5px;color:var(--t2);line-height:1.45;margin-bottom:14px}' +
    '.su-group{display:flex;flex-direction:column;gap:8px;margin-bottom:12px}.su-chips{flex-direction:row;flex-wrap:wrap}' +
    '.su-opt{display:flex;align-items:center;justify-content:space-between;gap:10px;width:100%;text-align:left;padding:14px;border-radius:12px;background:var(--s2);border:1.5px solid var(--border);color:var(--text);font:inherit;cursor:pointer;min-height:48px}' +
    '.su-chips .su-opt{width:auto;flex:1 1 28%;justify-content:center;padding:11px 10px;text-align:center}.su-chips .su-check{display:none}' +
    '.su-opt.on{border-color:var(--blue);background:var(--blue-dim)}.su-opt:focus-visible,.su-seg-btn:focus-visible,.su-input:focus-visible,.su-select:focus-visible,.su-link:focus-visible{outline:2px solid var(--blue);outline-offset:2px}' +
    '.su-opt-text{display:flex;flex-direction:column;gap:3px}.su-opt-label{font-size:14px;font-weight:600}.su-opt-sub{font-size:12px;color:var(--t2)}' +
    '.su-check{width:22px;height:22px;border-radius:50%;background:var(--blue);color:#fff;display:flex;align-items:center;justify-content:center;flex:none;opacity:0}.su-opt.on .su-check{opacity:1}' +
    '.su-field{margin-bottom:14px}.su-field-top{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:6px}.su-label{font-size:11px;color:var(--t2);font-weight:600;letter-spacing:.3px;margin-bottom:6px;display:block}.su-field-top .su-label{margin:0}.su-gap{margin-top:4px}' +
    '.su-seg{display:inline-flex;background:var(--s3);border-radius:9px;padding:3px}.su-seg-btn{border:none;background:none;color:var(--t2);font:inherit;font-size:12px;font-weight:600;padding:6px 12px;border-radius:7px;cursor:pointer}.su-seg-btn.on{background:var(--blue);color:#fff}' +
    '.su-input-wrap{display:flex;align-items:center;background:var(--s2);border:1.5px solid var(--border2);border-radius:12px;padding:0 12px}.su-affix{font-family:"JetBrains Mono",monospace;font-size:12px;color:var(--t3);margin-right:8px}' +
    '.su-input{flex:1;min-width:0;background:none;border:none;color:var(--text);font:inherit;font-size:16px;padding:13px 0}.su-input-wrap .su-input:focus,.su-input-wrap .su-input:focus-visible{outline:none}.su-input-wrap:focus-within{border-color:var(--blue)}.su-input-text{width:100%;background:var(--s2);border:1.5px solid var(--border2);border-radius:12px;padding:13px 12px;color-scheme:dark}' +
    '.su-select{background:var(--s2);border:1.5px solid var(--border2);border-radius:9px;color:var(--text);font:inherit;font-size:13px;padding:7px 10px}' +
    '.su-note{font-size:12px;color:var(--t2);line-height:1.45;margin-top:7px;min-height:17px}.su-toggle{display:flex;gap:10px;align-items:center;font-size:13px;color:var(--text);padding:8px 0}.su-toggle input{width:18px;height:18px;accent-color:var(--blue)}' +
    '.su-preview-btn{margin-top:8px;width:100%}.su-preview{margin-top:10px;border:1px dashed var(--orange);border-radius:12px;padding:12px;font-size:12.5px;color:var(--text);line-height:1.5}.su-preview-tag{font-family:"JetBrains Mono",monospace;font-size:10px;letter-spacing:1px;color:var(--orange);margin-bottom:6px}.su-preview-title{font-weight:700}.su-preview-foot{color:var(--t3);margin-top:6px;font-size:11.5px}' +
    '.su-facts{background:var(--s2);border:1px solid var(--border);border-radius:12px;padding:4px 14px;margin-bottom:8px}.su-fact{display:flex;justify-content:space-between;gap:12px;padding:10px 0;border-top:1px solid var(--border);font-size:13px}.su-fact:first-child{border-top:none}.su-fact span:first-child{color:var(--t2)}.su-fact span:last-child{font-weight:600;text-align:right}' +
    '.su-links{display:flex;gap:16px;justify-content:center;width:100%;padding-top:10px}.su-link{background:none;border:none;color:var(--t2);font:inherit;font-size:12.5px;cursor:pointer;padding:6px 4px;text-decoration:underline;text-underline-offset:3px}' +
    '.su-notice{background:var(--blue-dim);border:1px solid var(--border2);border-radius:10px;padding:10px 12px;font-size:12.5px;color:var(--text);margin-bottom:12px;line-height:1.45}' +
    '.su-list{margin:0 0 12px;padding-left:18px;font-size:13px;color:var(--text);line-height:1.6}' +
    '#mtApiSetupCard{margin-top:10px}.su-card-title{font-size:14px;font-weight:700}.su-card-line{font-size:12.5px;color:var(--t2);margin-top:4px;line-height:1.45}.su-card-line b{color:var(--text);font-weight:600}.su-card-actions{display:flex;gap:10px;align-items:center;margin-top:12px;flex-wrap:wrap}.su-card-flash{animation:suFlash 1.6s ease-out}' +
    '@keyframes suFlash{0%{box-shadow:0 0 0 2px var(--blue)}100%{box-shadow:0 0 0 2px rgba(42,171,238,0)}}' +
    '.su-motion{height:64px;margin:0 18px;border-radius:10px;overflow:hidden;background:radial-gradient(ellipse at 50% 95%,#143c50,transparent 70%),#0a1722;color:#64dded}.su-motion:empty{display:none}.su-motion svg{display:block;width:100%;height:100%;fill:none;stroke:currentColor;stroke-linecap:round;stroke-linejoin:round}.su-track{stroke:#234456}.su-sweep{transform-origin:160px 32px;animation:suAcquire .85s cubic-bezier(.2,.7,.25,1) both}.su-dollar{stroke:#b9fff0;stroke-width:2.1}.su-scanner{animation:suScan 3.2s ease-in-out infinite}.su-corners{animation:suBright 3.2s infinite}.su-packet{stroke-width:2.5;stroke-dasharray:10 110;animation:suFeed 2.7s linear infinite}.su-row{stroke:#a2f6eb;stroke-width:2;stroke-dasharray:15;animation:suWrite 2.7s infinite}.su-delay{animation-delay:-1.35s}@keyframes suAcquire{from{transform:scale(1.6) rotate(-35deg);opacity:0}to{transform:none;opacity:1}}@keyframes suScan{0%{transform:translateY(-19px);opacity:0}15%,65%{opacity:.85}83%,100%{transform:translateY(19px);opacity:0}}@keyframes suBright{0%,60%,100%{stroke:#498393}76%{stroke:#b7fff7}}@keyframes suFeed{0%{stroke-dashoffset:10;opacity:0}12%{opacity:1}78%,100%{stroke-dashoffset:-96;opacity:0}}@keyframes suWrite{0%,45%{stroke-dashoffset:15;opacity:.2}72%,95%{stroke-dashoffset:0;opacity:1}100%{opacity:.2}}@media(prefers-reduced-motion:reduce){.su-motion svg *{animation:none!important}.su-scanner{opacity:0}.su-packet{display:none}}' +
    '.su-big{display:flex;align-items:baseline;gap:10px;margin:6px 0 12px}.su-big-n{font-family:"JetBrains Mono",monospace;font-size:30px;font-weight:600;color:var(--text)}.su-big-l{font-size:13px;color:var(--t2)}' +
    '.su-hist{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap}.su-hist .btn{padding:8px 14px;font-size:12px}' +
    '.su-error{background:var(--red-dim);border:1px solid rgba(224,92,92,.4);border-radius:10px;padding:10px 12px;font-size:12.5px;color:var(--text);margin-bottom:12px;line-height:1.45;display:flex;gap:10px;align-items:center;justify-content:space-between;flex-wrap:wrap}' +
    '.su-error .btn{padding:6px 12px;font-size:12px;flex:none}' +
    '.su-src{font-size:10px;color:var(--t3);text-transform:uppercase;letter-spacing:.6px;margin:12px 0 4px;font-family:"JetBrains Mono",monospace}.su-src:first-of-type{margin-top:8px}' +
    '.su-sec{display:flex;gap:6px 14px;flex-wrap:wrap;margin-top:10px;padding-top:10px;border-top:1px solid var(--border)}' +
    '.su-sec .su-link{padding:4px 0;text-align:left}.su-sec-state{color:var(--t3);font-size:11.5px;margin-left:4px;text-decoration:none}' +
    '.btn[disabled]{opacity:.45;cursor:default}' +
    '@media(max-width:480px){#setup-sheet .su-stack{flex-direction:column-reverse}#setup-sheet .su-stack .btn{flex:none;width:100%}}' +
    '@media(min-width:900px){#setup-sheet{bottom:auto;top:50%;left:50%;right:auto;width:520px;max-height:84vh;border-radius:16px;border:1px solid var(--border);transform:translate(-50%,-46%);opacity:0;transition:transform .25s var(--out),opacity .2s;padding-bottom:16px}#setup-sheet.open{transform:translate(-50%,-50%);opacity:1}#setup-sheet .as-handle{display:none}}' +
    '@media(prefers-reduced-motion:reduce){#setup-sheet,#setup-overlay{transition:none}.su-card-flash{animation:none}}';


  // ── sheet ──
  var opener = null;
  function $(id) { return doc.getElementById(id); }
  function ensureDom() {
    if ($('setup-sheet')) return;
    var style = doc.createElement('style'); style.id = 'setup-style'; style.textContent = CSS; doc.head.appendChild(style);
    var overlay = doc.createElement('div'); overlay.id = 'setup-overlay';
    var sheet = doc.createElement('div'); sheet.id = 'setup-sheet';
    sheet.setAttribute('role', 'dialog'); sheet.setAttribute('aria-modal', 'true'); sheet.setAttribute('aria-labelledby', 'setupTitle'); sheet.tabIndex = -1;
    sheet.innerHTML = '<div class="as-handle"></div><div class="aaf-header"><div class="aaf-title-wrap"><div class="aaf-title" id="setupTitle"></div><div class="aaf-step" id="setupStep"></div></div>' +
      '<button type="button" class="aaf-close rip" id="setupClose" data-act="later" aria-label="Close and finish later" style="border:none;cursor:pointer"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button></div>' +
      '<div id="setupMotion" class="su-motion" aria-hidden="true"></div><div class="aaf-body" id="setupBody"></div><div class="aaf-footer" id="setupFooter"></div>';
    doc.body.appendChild(overlay); doc.body.appendChild(sheet);
    overlay.addEventListener('click', function () { act('later'); });
    sheet.addEventListener('click', function (e) { var t = e.target.closest ? e.target.closest('[data-act]') : null; if (t && t.tagName !== 'INPUT' && t.tagName !== 'SELECT') act(t.getAttribute('data-act'), t); });
    sheet.addEventListener('input', onInput);
    sheet.addEventListener('change', onInput);
    sheet.addEventListener('keydown', onKey);
    if (global.visualViewport) { global.visualViewport.addEventListener('resize', fitViewport); global.visualViewport.addEventListener('scroll', fitViewport); }
  }
  // Phones: the sheet rides above the on-screen keyboard so the input and the primary button stay reachable.
  function fitViewport() {
    var sheet = $('setup-sheet'); if (!sheet) return;
    var vv = global.visualViewport;
    if (!ui.open || !vv || global.innerWidth >= 900) { sheet.style.bottom = ''; sheet.style.maxHeight = ''; return; }
    var hidden = Math.max(0, global.innerHeight - vv.height - vv.offsetTop);
    sheet.style.bottom = hidden + 'px';
    sheet.style.maxHeight = Math.max(240, vv.height - 8) + 'px';
    var f = doc.activeElement;
    if (hidden > 0 && f && sheet.contains(f) && f.scrollIntoView && /INPUT|SELECT/.test(f.tagName)) f.scrollIntoView({ block: 'nearest' });
  }
  function show() {
    ensureDom();
    if (!ui.open) opener = doc.activeElement;
    ui.open = true;
    render();
    $('setup-overlay').classList.add('open'); $('setup-sheet').classList.add('open');
    fitViewport();
    focusFirst();
  }
  function hide() {
    ui.open = false; ui.mode = null; ui.confirm = null; ui.notice = '';
    var sheet = $('setup-sheet'); if (!sheet) return;
    sheet.classList.remove('open'); $('setup-overlay').classList.remove('open');
    sheet.style.bottom = ''; sheet.style.maxHeight = '';
    if (opener && opener.focus && doc.contains(opener)) { try { opener.focus({ preventScroll: true }); } catch (e) { /* ignore */ } }
    opener = null;
  }
  function focusFirst() {
    var body = $('setupBody');
    // An unanswered question starts with focus on the sheet itself, so a focus ring is never mistaken for a preselected answer.
    var target = body.querySelector('[role="radio"][aria-checked="true"]') || (ui.mode === 'invite' || ui.confirm ? $('setupFooter').querySelector('.btn-blue') : null) || $('setup-sheet');
    try { target.focus({ preventScroll: true }); } catch (e) { /* ignore */ }
  }
  function work(key) { var e = entry(key); if (!e.working) e.working = normalize(e.saved); return e.working; }
  function branchSummary(p, type) {
    var out = [];
    if (type === 'evaluation') {
      var ev = p.evaluation;
      if (ev.phaseCount) out.push(ev.phaseCount === 1 ? 'One-phase evaluation' : ev.phaseCount + ' phases');
      if (ev.currentPhase) out.push('Current phase: Phase ' + ev.currentPhase);
      if (ev.phaseTarget) out.push('Phase target: ' + entryText(ev.phaseTarget, p, ui.key));
    } else {
      var ms = p.master;
      if (ms.goal) out.push('Goal: ' + goalText(ms.goal));
      if (ms.payoutTarget) out.push('Payout target: ' + entryText(ms.payoutTarget, p, ui.key));
      if (ms.firmMin) out.push('Firm minimum for payout: ' + entryText(ms.firmMin, p, ui.key));
      if (ms.growthTarget) out.push('Growth target: ' + entryText(ms.growthTarget, p, ui.key));
    }
    return out;
  }
  function footerLinks() { return '<div class="su-links"><button type="button" class="su-link" data-act="skip">Skip</button><button type="button" class="su-link" data-act="later">Finish later</button></div>'; }
  function renderMotion(stage, key) {
    var box = $('setupMotion');
    var identity = stage ? key + '/' + stage : '';
    if (box.dataset.scene === identity) return;
    box.dataset.scene = identity;
    var rails = '<path class="su-track" d="M14 32h83l9-7h24m60 0h24l9 7h83"/>';
    var detected = '<defs><clipPath id="suScanMask"><rect x="121" y="11" width="78" height="42" rx="10"/></clipPath></defs><circle cx="160" cy="32" r="13.5" fill="#0c2431"/><path class="su-dollar" transform="translate(160 32) scale(.75) translate(-160 -32)" d="M160 23v18m4-14c-2-3-9-3-9 1 0 5 10 2 10 7 0 4-7 5-10 1"/><g clip-path="url(#suScanMask)"><g class="su-scanner"><path stroke-width="7" opacity=".12" d="M125 32h70"/><path d="M125 32h70"/></g></g><g class="su-sweep"><path class="su-corners" d="M141 25v-8h8m22 0h8v8m0 14v8h-8m-22 0h-8v-8"/></g>';
    var feed = '<path class="su-track" d="M48 37h58l12-11h28m126 11h-58l-12-11h-28"/><path class="su-packet" d="M48 37h58l12-11h28"/><path class="su-packet su-delay" d="M272 37h-58l-12-11h-28"/><rect x="146" y="15" width="28" height="34" rx="5" fill="#102e3b"/><path class="su-track" d="M153 25h14m-14 7h14m-14 7h14"/><path class="su-row" d="M153 25h14"/><path class="su-row su-delay" d="M153 32h14"/><path d="M153 39h8" opacity=".65"/>';
    box.innerHTML = stage ? '<svg viewBox="0 0 320 64" aria-hidden="true">' + (stage === 'detect' ? rails + detected : feed) + '</svg>' : '';
  }
  function render() {
    var key = ui.key, body = $('setupBody'), foot = $('setupFooter'), e = entry(key);
    if (ui.mode !== 'import') renderMotion(ui.mode === 'invite' ? 'detect' : '', key);
    foot.className = 'aaf-footer';
    var err = ui.error ? '<div class="su-error" role="alert"><span>' + esc(ui.error) + '</span><span style="display:flex;gap:8px">' +
      (ui.retry ? '<button type="button" class="btn btn-blue rip" data-act="retry">Retry</button>' : '') +
      (ui.closeAnyway ? '<button type="button" class="btn btn-ghost rip" data-act="closeAnyway">Close</button>' : '') + '</span></div>' : '';
    if (ui.mode === 'invite') {
      var a = account(key), sum = (ui.snapshot || {}).summary || {}, kind = reportedKind(a.accountType), im0 = importOf(key);
      $('setupTitle').textContent = 'Account detected';
      $('setupStep').textContent = 'Connected · read-only';
      body.innerHTML = '<div class="su-sub" style="margin-top:2px">This is what your broker reported.</div><div class="su-facts">' +
        [['Broker', a.broker], ['Account', a.ending ? '···· ' + a.ending : '—'], ['Current balance', money(num(sum.balance), a.currency)], ['Currency', a.currency || '—'],
         kind ? ['Account kind', kind] : null, ['As of', when(sum.fetched_at)]].filter(Boolean)
          .map(function (r) { return '<div class="su-fact"><span>' + esc(r[0]) + '</span><span>' + esc(r[1]) + '</span></div>'; }).join('') +
        '</div><div class="su-note" style="margin-bottom:6px">Import your closed trades so your stats start from your first trade, not from today. TaliTrade asks your broker for everything back to when this account was opened.</div>' +
        (im0.error ? '<div class="su-error" role="alert"><span>' + esc(im0.error) + '</span></div>' : '');
      foot.className = 'aaf-footer su-stack';
      foot.innerHTML = '<button type="button" class="btn btn-blue rip" id="setupPrimary" data-act="import"' + (im0.starting ? ' disabled' : '') + '>' + (im0.starting ? 'Starting…' : 'Import trade history') + '</button>' +
        '<div class="su-links"><button type="button" class="su-link" data-act="dashboard">Later</button></div>';
      return;
    }
    if (ui.mode === 'import') {
      var im = importOf(key), st = im.status, v;
      // Until the server has the request, nothing is known yet: say so, show no numbers.
      if (!st || st.state === 'not_requested' || im.starting) v = { title: 'Importing trade history\u2026', big: null, facts: [['Dates checked', 'Starting\u2026']], notes: ['Asking your broker for every closed trade since this account was opened.'] }, st = { state: 'importing' };
      else v = importView(st, key);
      $('setupTitle').textContent = v.title;
      $('setupStep').textContent = accountTitle(key);
      renderMotion(st.state === 'importing' ? 'import' : '', key);
      body.innerHTML = (im.error ? '<div class="su-error" role="alert"><span>' + esc(im.error) + '</span></div>' : '') +
        (v.big != null ? '<div class="su-big" aria-live="polite"><span class="su-big-n">' + esc(v.big) + '</span><span class="su-big-l">' + esc(v.bigLabel) + '</span></div>' : '') +
        '<div class="su-facts">' + v.facts.map(function (r) { return '<div class="su-fact"><span>' + esc(r[0]) + '</span><span>' + esc(r[1]) + '</span></div>'; }).join('') + '</div>' +
        v.notes.map(function (n) { return '<div class="su-note">' + esc(n) + '</div>'; }).join('');
      foot.className = 'aaf-footer su-stack';
      var setupDone = work(key).status === 'saved', cont = setupDone ? 'Go to dashboard' : (st.state === 'importing' ? 'Continue to account setup' : v.retry ? 'Continue setup' : 'Continue to account setup');
      foot.innerHTML = (v.retry ? '<button type="button" class="btn btn-blue rip" id="setupPrimary" data-act="retryImport"' + (im.starting ? ' disabled' : '') + '>' + (im.starting ? 'Retrying…' : 'Retry') + '</button>' : '') +
        '<button type="button" class="btn ' + (v.retry || st.state === 'importing' ? 'btn-ghost' : 'btn-blue') + ' rip" data-act="' + (setupDone ? 'dashboard' : 'continueSetup') + '">' + cont + '</button>';
      return;
    }
    if (ui.mode === 'loading') {
      $('setupTitle').textContent = FLOW_TITLES[ui.flow] || 'Account setup';
      $('setupStep').textContent = accountTitle(key);
      body.innerHTML = err || '<div class="su-sub" style="padding:18px 0">Loading your saved answers…</div>';
      foot.innerHTML = '<button type="button" class="btn btn-ghost rip" data-act="later">Close</button>';
      return;
    }
    var p = work(key);
    if (ui.confirm === 'switch') {
      var from = p.type, to = p.draft.type;
      $('setupTitle').textContent = 'Change account type?';
      $('setupStep').textContent = accountTitle(key);
      body.innerHTML = err + '<h2 class="su-q" id="setupQuestion">Switch to ' + esc(TYPES[to]) + '?</h2><div class="su-sub">Your ' + esc(TYPES[from]) + ' answers will be set aside, not deleted:</div><ul class="su-list">' +
        branchSummary(p, from).map(function (x) { return '<li>' + esc(x) + '</li>'; }).join('') + '</ul><div class="su-note">If you switch back to ' + esc(TYPES[from]) +
        ', they come back as they were. Your starting size, firm rules and personal limits stay the same.' + (branchSummary(p, to).length ? ' Your earlier ' + esc(TYPES[to]) + ' answers will be used again.' : '') + '</div>';
      foot.innerHTML = '<button type="button" class="btn btn-ghost rip" data-act="keepType">Keep ' + esc(TYPES[from]) + '</button><button type="button" class="btn btn-blue rip" id="setupPrimary" data-act="confirmSwitch">Switch to ' + esc(TYPES[to]) + '</button>';
      busyState();
      return;
    }
    var steps = stepsFor(ui.flow, p), idx = Math.max(0, steps.indexOf(ui.step)), v = view(ui.step, p, key), last = idx === steps.length - 1;
    $('setupTitle').textContent = FLOW_TITLES[ui.flow];
    $('setupStep').textContent = accountTitle(key) + (steps.length > 1 ? ' · ' + (idx + 1) + ' of ' + steps.length : '');
    body.innerHTML = err + (ui.notice ? '<div class="su-notice" role="status">' + esc(ui.notice) + '</div>' : '') + '<h2 class="su-q" id="setupQuestion">' + esc(v.title) + '</h2><div class="su-sub">' + esc(v.sub) + '</div>' + v.body;
    foot.innerHTML = (idx > 0 ? '<button type="button" class="btn btn-ghost rip" data-act="back" style="flex:0 0 auto">Back</button>' : '') +
      '<button type="button" class="btn btn-blue rip" id="setupPrimary" data-act="next">' + (last ? (ui.flow === 'basics' ? 'Save to account' : 'Save') : 'Continue') + '</button>' + footerLinks();
    busyState();
  }
  function busyState() {
    var b = $('setupPrimary');
    if (b && ui.mode === 'flow' && !ui.confirm) {
      var ok = valid(ui.step, work(ui.key));
      b.disabled = !ok || ui.saving; b.setAttribute('aria-disabled', String(b.disabled));
    }
    if (b && ui.saving) b.textContent = 'Saving…';
    Array.prototype.forEach.call($('setupFooter').querySelectorAll('[data-act="skip"],[data-act="later"],[data-act="back"],[data-act="keepType"]'), function (n) { n.disabled = ui.saving; });
  }
  function rerender(focusSelector) {
    var body = $('setupBody'), top = body.scrollTop;
    render(); body.scrollTop = top;
    var t = focusSelector ? $('setup-sheet').querySelector(focusSelector) : null;
    if (t) { try { t.focus({ preventScroll: true }); } catch (e) { /* ignore */ } }
  }
  function closeSheet() { ui.saving = false; ui.error = null; ui.retry = null; ui.closeAnyway = false; hide(); }

  // ── import wording: only committed numbers and dates the server reports ──
  function day(iso) { var d = iso ? new Date(iso) : null; return d && !isNaN(d.getTime()) ? d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' }) : '—'; }
  function checkedText(st) {
    if (!st.checked_from) return 'Nothing yet';
    var to = st.checked_to && Date.now() - new Date(st.checked_to).getTime() < 2 * 86400000 ? 'today' : day(st.checked_to);
    return day(st.checked_from) + ' – ' + to;
  }
  function trades(n) { return n === 1 ? '1 closed trade' : (n || 0).toLocaleString('en-US') + ' closed trades'; }
  var REASONS = { worker_off: 'Background importing is switched off on this server.', transient: 'The broker did not answer.', rejected: 'The broker refused a request.',
    contract: 'The broker sent an answer TaliTrade could not read.', unauthorized: 'The broker no longer accepts this token.', forbidden: 'This token is not allowed to read trade history.',
    stalled: 'No answer from the broker for a while.', internal: 'Something went wrong on TaliTrade’s side.' };
  function importView(st, key) {
    // While importing, "fully checked" is the range the broker has answered completely; trades
    // from further back can already be saved while their range is still being split and re-read.
    var running = st.state === 'importing' || st.state === 'interrupted';
    var notes = [], facts = [[running ? 'Fully checked so far' : 'Dates checked', checkedText(st)]], basis = st.earliest_boundary;
    if (st.first_trade_at) facts.push([running ? 'Earliest trade so far' : 'Earliest trade found', day(st.first_trade_at)]);
    if (basis === 'not_reported') notes.push('Your broker did not report when this account was opened, so TaliTrade checked back to ' + day(st.target_from) + '.');
    if (st.window_limited) notes.push('Your broker limits how many days one request can cover, so TaliTrade asked in smaller date windows.');
    var retention = 'Match-Trader does not say how long it keeps old trades. This is everything it returned for these dates.';
    if (st.state === 'importing') {
      notes.unshift(st.reason === 'rate_limited' ? 'Waiting for the broker’s request limit. The import continues on its own.' : 'You can leave this screen, reload or switch accounts. The import keeps running, and your balances keep updating.');
      return { title: 'Importing trade history…', big: (st.closed_trades || 0).toLocaleString('en-US'), bigLabel: st.closed_trades === 1 ? 'closed trade imported so far' : 'closed trades imported so far', facts: facts, notes: notes };
    }
    if (st.state === 'imported') { notes.push(retention); return { title: 'Trade history imported', big: (st.closed_trades || 0).toLocaleString('en-US'), bigLabel: st.closed_trades === 1 ? 'closed trade' : 'closed trades', facts: facts, notes: notes }; }
    if (st.state === 'empty') {
      notes.unshift(basis === 'account_created_at' ? 'Your broker returned no closed trades for any date since this account was opened.' : 'Your broker returned no closed trades for these dates.');
      notes.push(retention);
      return { title: 'No closed trades found', big: null, facts: facts, notes: notes };
    }
    if (st.state === 'limited') {
      notes.unshift('Some moments had more trades than the broker returns in one answer, so they could not be read in full: ' + (st.gaps || []).map(function (g) { return day(g[0]); }).join(', ') + '.');
      return { title: 'Trade history imported with limits', big: (st.closed_trades || 0).toLocaleString('en-US'), bigLabel: 'closed trades saved', facts: facts, notes: notes, retry: true };
    }
    if (st.state === 'unavailable') return { title: 'Trade history not available', big: null, facts: [], notes: ['This token is not allowed to read closed trades. Create a token with read access to import history.'] };
    notes.unshift((REASONS[st.reason] || 'The import stopped.') + ' What was saved is kept; Retry continues from where it stopped.');
    return { title: 'Import paused', big: (st.closed_trades || 0).toLocaleString('en-US'), bigLabel: 'closed trades saved so far', facts: facts, notes: notes, retry: true };
  }

  // ── saving a step ──
  // ``next`` is the full working copy to store. The screen moves on only after the server has it.
  function persist(next, keepDraftKeys, onSaved) {
    var key = ui.key, before = clone(work(key).draft);
    var attempt = { key: key, profile: next, saveId: newSaveId(), keep: keepDraftKeys || [], before: before, onSaved: onSaved };
    return send(attempt);
  }
  function send(attempt) {
    var key = attempt.key;
    if (ui.open && ui.key === key) { ui.saving = true; ui.error = null; ui.retry = null; busyState(); }
    return save(attempt).then(function (r) {
      var e = entry(key), here = ui.open && ui.key === key;
      if (here) ui.saving = false;
      if (r.ok) {
        e.working = normalize(r.profile); e.failed = null;
        if (here && attempt.onSaved) attempt.onSaved(); else refresh(key, false);
        return true;
      }
      if (r.conflict) {
        // Someone saved newer answers elsewhere. Show those; keep only what was typed on this question.
        var merged = normalize(r.profile);
        attempt.keep.forEach(function (k) { if (attempt.before[k] !== undefined) merged.draft[k] = attempt.before[k]; });
        e.working = merged; e.unsaved = false; e.failed = null;
        if (here) { ui.notice = 'These answers were changed in another tab or on another device. Showing the latest — check this question and save again.'; rerender(); focusFirst(); }
        else refresh(key, false);
        return false;
      }
      e.failed = attempt;              // typed answers stay in the working copy
      if (here) { ui.error = (r.message || 'Could not save.') + ' Your answer is still here.'; ui.retry = attempt; rerender(); try { $('setup-sheet').querySelector('[data-act="retry"]').focus(); } catch (x) { /* ignore */ } }
      else refresh(key, false);
      return false;
    });
  }
  function advanceFrom(step, skip) {
    var key = ui.key, p = work(key), next = clone(p), flow = ui.flow;
    if (skip) DRAFT_KEYS[step].forEach(function (k) { delete next.draft[k]; }); else commit(step, next, key);
    var steps = stepsFor(flow, next), idx = steps.indexOf(step);
    var nextStep = idx >= 0 && idx < steps.length - 1 ? steps[idx + 1] : null;
    if (flow === 'basics' && step === 'type' && !next.type) nextStep = 'size';
    if (nextStep) {
      next.resume = { flow: flow, step: nextStep };
      if (next.status === 'new') next.status = 'in_progress';
      seed(nextStep, next);
      return persist(next, DRAFT_KEYS[step], function () { ui.step = nextStep; ui.notice = ''; render(); $('setupBody').scrollTop = 0; focusFirst(); });
    }
    if (next.resume && next.resume.flow === flow) next.resume = null;
    stepsFor(flow, next).forEach(function (s) { (DRAFT_KEYS[s] || []).forEach(function (k) { delete next.draft[k]; }); });
    if (flow === 'basics') { next.status = 'saved'; next.done.basics = true; if (next.type === 'evaluation' && next.evaluation.phaseTarget) next.done.phase = true; }
    else next.done[flow] = true;
    return persist(next, DRAFT_KEYS[step], function () { closeSheet(); refresh(key, true); });
  }
  // Finish later: keep the answers and the place. ``background`` (the trader switched accounts)
  // closes at once and saves behind the scenes; a failure then shows on that account's card.
  function later(background) {
    // A save already in flight carries this step's progress; a second save queued behind it would
    // be based on answers from before it, so the sheet just closes.
    if (ui.mode !== 'flow' || ui.saving) { var k0 = ui.key; closeSheet(); refresh(k0, false); return; }
    var key = ui.key, p = work(key), next = clone(p), steps = stepsFor(ui.flow, p), idx = steps.indexOf(ui.step);
    var typed = (DRAFT_KEYS[ui.step] || []).some(function (k) { return p.draft[k] !== undefined; });
    if (idx > 0 || typed || ui.flow !== 'basics' || p.status !== 'new') next.resume = { flow: ui.flow, step: ui.step };
    if (next.status === 'new' && next.resume) next.status = 'in_progress';
    var unchanged = JSON.stringify(toServer(next)) === JSON.stringify(toServer(normalize(entry(key).saved)));
    if (unchanged) { closeSheet(); refresh(key, false); return; }
    if (background) { closeSheet(); send({ key: key, profile: next, saveId: newSaveId(), keep: [], before: {}, onSaved: null }); return; }
    ui.closeAnyway = true;
    persist(next, [], function () { closeSheet(); refresh(key, false); });
  }
  function act(name, t) {
    var key = ui.key, p;
    if (name === 'later' || name === 'dashboard') return later();
    if (name === 'closeAnyway') { closeSheet(); refresh(key, false); return; }
    if (name === 'personalize' || name === 'continueSetup') { var e0 = work(key); return open(key, 'basics', e0.resume && e0.resume.flow === 'basics' ? e0.resume.step : null); }
    if (name === 'import' || name === 'retryImport') {
      // The screen changes at once; the numbers on it come only from the server.
      var im = importOf(key);
      if (name === 'import' && im.status && im.status.state !== 'not_requested' && im.status.state !== 'unavailable') { openImport(key); return; }
      ui.mode = 'import'; render(); focusFirst();
      return startImport(key, name === 'retryImport').then(function () { keepPolling(); });
    }
    if (name === 'retry') { if (ui.retry) send(ui.retry); return; }
    if (ui.saving) return;
    if (name === 'back') { var steps = stepsFor(ui.flow, work(key)), i = steps.indexOf(ui.step); ui.notice = ''; ui.error = null; if (i > 0) { ui.step = steps[i - 1]; seed(ui.step, work(key)); render(); focusFirst(); } return; }
    if (name === 'skip') { ui.notice = ''; return advanceFrom(ui.step, true); }
    if (name === 'next') {
      p = work(key);
      if (!valid(ui.step, p)) return;
      if (ui.step === 'type' && p.type && p.draft.type !== p.type && branchSummary(p, p.type).length) { ui.confirm = 'switch'; ui.error = null; render(); focusFirst(); return; }
      return advanceFrom(ui.step, false);
    }
    if (name === 'keepType') { work(key).draft.type = work(key).type; ui.confirm = null; ui.error = null; render(); focusFirst(); return; }
    if (name === 'confirmSwitch') { ui.confirm = null; return advanceFrom('type', false).then(function () { if (ui.open && ui.key === key && ui.step !== 'type') ui.notice = ''; }); }
    if (name === 'preview') { work(key).draft.previewShown = true; return rerender('[data-act="preview"]'); }
    if (name === 'unit') {
      var mn = t.getAttribute('data-name'), u = t.getAttribute('data-value'), d0 = work(key).draft;
      var m = d0[mn] || { raw: '', unit: u, view: u }; m.view = u; if (!String(m.raw).trim()) m.unit = u; d0[mn] = m;
      return rerender('#su-in-' + mn);
    }
    if (name === 'pick') {
      var n = t.getAttribute('data-name'), v = t.getAttribute('data-value'), focus = '[data-name="' + n + '"][data-value="' + v + '"]', d = work(key).draft;
      if (n === 'sizePreset') { d.size = d.size || { preset: null, other: '' }; d.size.preset = v === 'other' ? 'other' : Number(v); if (v === 'other') focus = '#su-size-other'; }
      else if (n === 'goal') { d.goal = { id: v, text: d.goal ? d.goal.text : '' }; if (v === 'other') focus = '#su-goal-text'; }
      else if (n === 'phaseCount' || n === 'currentPhase') { d[n] = Number(v); if (n === 'phaseCount' && d.currentPhase > d.phaseCount) delete d.currentPhase; }
      else d[n] = v;
      return rerender(focus);
    }
  }
  function onInput(ev) {
    var t = ev.target, a = t.getAttribute && t.getAttribute('data-act'), key = ui.key;
    if (!a || ui.mode !== 'flow') return;
    var p = work(key), d = p.draft;
    if (a === 'money') {
      var n = t.getAttribute('data-name'), m = d[n] || { raw: '', unit: 'amount', view: 'amount' };
      if (m.view === m.unit) m.raw = t.value;
      else if (String(t.value).trim() !== '') { m.unit = m.view; m.raw = t.value; }   // typing in the other unit makes that the entry; clearing it keeps the original
      d[n] = m;
      var note = $('su-note-' + n); if (note) note.textContent = moneyNote(n, p, key);
    }
    else if (a === 'sizeOther') { d.size = d.size || { preset: 'other', other: '' }; d.size.other = t.value; }
    else if (a === 'goalText') { d.goal = d.goal || { id: 'other', text: '' }; d.goal.text = t.value; }
    else if (a === 'reminder') d.reminder = t.value;
    else if (a === 'reset') d.firmDailyReset = t.value;
    else if (a === 'alert') { d.alerts = d.alerts || { daily: false, budget: false }; d.alerts[t.getAttribute('data-name')] = !!t.checked; }
    busyState();
  }
  function onKey(e) {
    var sheet = $('setup-sheet'), t = e.target;
    if (e.key === 'Escape') { e.preventDefault(); return later(); }
    if (e.key === 'Enter' && t.tagName === 'INPUT' && t.type !== 'checkbox') { e.preventDefault(); var b = $('setupPrimary'); if (b && !b.disabled) act(b.getAttribute('data-act')); return; }
    if (t.getAttribute && t.getAttribute('role') === 'radio' && /^Arrow/.test(e.key)) {
      var group = Array.prototype.slice.call(t.parentNode.querySelectorAll('[role="radio"]')), i = group.indexOf(t);
      var next = group[(i + (e.key === 'ArrowDown' || e.key === 'ArrowRight' ? 1 : group.length - 1)) % group.length];
      e.preventDefault(); if (next) act(next.getAttribute('data-act'), next);
      return;
    }
    if (e.key === 'Tab') {
      var items = Array.prototype.filter.call(sheet.querySelectorAll('button,input,select,[tabindex="0"]'), function (n) { return !n.disabled && n.tabIndex !== -1 && n.offsetParent !== null; });
      if (!items.length) return;
      var first = items[0], lastItem = items[items.length - 1];
      if (e.shiftKey && doc.activeElement === first) { e.preventDefault(); lastItem.focus(); }
      else if (!e.shiftKey && doc.activeElement === lastItem) { e.preventDefault(); first.focus(); }
    }
  }

  // ── entry points ──
  function open(key, flow, step) {
    if (!enabled()) return;
    var e = entry(key);
    ui.key = key; ui.flow = flow; ui.confirm = null; ui.notice = ''; ui.error = null; ui.retry = null; ui.closeAnyway = false; ui.saving = false;
    delete pendingInvite[key];
    if (!e.loaded) {
      ui.mode = 'loading'; show();
      var mine = session;
      request('GET', '/connections/' + connectionId(key)).then(function (body) {
        if (mine !== token()) return;
        adopt(key, body.version, body.profile, body);
        if (ui.open && ui.key === key && ui.mode === 'loading') open(key, flow, step);
      }, function (err) {
        if (ui.open && ui.key === key && ui.mode === 'loading') { ui.error = (err && err.message) || 'Could not load your saved answers.'; ui.retry = null; render(); }
      });
      return;
    }
    var p = work(key), steps = stepsFor(flow, p);
    if (!step && p.resume && p.resume.flow === flow) step = p.resume.step;
    ui.mode = 'flow';
    ui.step = step && steps.indexOf(step) >= 0 ? step : steps[0];
    seed(ui.step, p);
    if (ui.open) { render(); focusFirst(); } else show();
    $('setupBody').scrollTop = 0;
  }
  function openImport(key) {
    if (!enabled()) return;
    delete pendingInvite[key];
    ui.key = key; ui.mode = 'import'; ui.confirm = null; ui.error = null;
    if (ui.open) { render(); focusFirst(); } else show();
    loadImport(key, true); keepPolling();
  }
  function openInvite(key, snapshot) {
    delete pendingInvite[key];
    ui.key = key; ui.mode = 'invite'; ui.snapshot = snapshot; ui.confirm = null; ui.error = null;
    loadOne(key, true);                // the saved answers are usually in by the time setup starts
    loadImport(key, true);
    show();
    if (host.refreshSwitcher) host.refreshSwitcher();
  }
  var pendingTimer = null;
  function tryPending() {
    pendingTimer = null;
    Object.keys(pendingInvite).forEach(function (k) { if (!host.getAccount(k)) delete pendingInvite[k]; });
    var key = host.getActiveAccount();
    if (key && pendingInvite[key] && !ui.open && !(host.isBusy && host.isBusy())) openInvite(key, pendingInvite[key]);
    if (Object.keys(pendingInvite).length) pendingTimer = global.setTimeout(tryPending, 700);
  }
  // Called once per connection made in this page session, when its first successful snapshot arrives.
  // If the trader is looking at another account, the invitation waits on the account that synced.
  function onFirstSnapshot(key, snapshot) {
    if (!host || !enabled() || !snapshot || !snapshot.summary) return;
    if (host.getActiveAccount() === key && !ui.open && !(host.isBusy && host.isBusy())) return openInvite(key, snapshot);
    pendingInvite[key] = snapshot;
    if (host.refreshSwitcher) host.refreshSwitcher();
    if (!pendingTimer) pendingTimer = global.setTimeout(tryPending, 700);
  }
  function onContextChange() { if (ui.open) later(true); }

  // ── account card ──
  function flowHasDraft(p, flow) { return stepsFor(flow, p).some(function (s) { return (DRAFT_KEYS[s] || []).some(function (k) { return p.draft[k] !== undefined; }); }); }
  function nextAction(p) {
    if (p.resume && p.resume.flow === 'basics') return { label: 'Resume setup', flow: 'basics', step: p.resume.step };
    if (p.status !== 'saved') return { label: p.status === 'in_progress' ? 'Resume setup' : 'Set up account', flow: 'basics' };
    if (p.type === 'evaluation' && !p.evaluation.phaseTarget && !p.done.phase) return { label: p.resume && p.resume.flow === 'phase' ? 'Resume phase target' : 'Set phase target', flow: 'phase' };
    var g = goalOf(p);
    if (g) {
      var f = GOALS[g.id].flow;
      if (!p.done[f]) return { label: p.resume && p.resume.flow === f ? 'Resume ' + FLOW_TITLES[f].toLowerCase() : GOALS[g.id].action, flow: f };
    } else if (p.type === 'master') return { label: 'Choose my goal', flow: 'basics', step: 'goal' };
    if (p.resume) return { label: 'Resume ' + FLOW_TITLES[p.resume.flow].toLowerCase(), flow: p.resume.flow, step: p.resume.step };
    // The goal's own step is done: the next useful thing is the firm's rules, then the trader's limits.
    if (!p.done.firm) return { label: 'Add firm rules', flow: 'firm' };
    if (!p.done.personal) return { label: 'Set my risk limits', flow: 'personal' };
    return null;
  }
  function sectionState(p, flow) { return (p.resume && p.resume.flow === flow) || flowHasDraft(p, flow) ? 'Resume' : p.done[flow] ? 'Edit' : 'Add'; }
  function brokerLine(key) {
    var a = account(key), bal = brokerBalance(key), kind = reportedKind(a.accountType);
    return [kind, a.currency, bal ? 'balance ' + money(bal.amount, a.currency) + ' · ' + when(bal.at) : 'not synced yet'].filter(Boolean).join(' · ');
  }
  function cardLines(p, key) {
    var out = [], c = cur(key, p), ev = p.evaluation, ms = p.master, fr = p.firmRules, pr = p.personal;
    function line(label, e, extra) { if (e) out.push(esc(label) + ' · <b>' + esc(entryText(e, p, key)) + '</b>' + (extra ? esc(extra) : '')); }
    var head = [p.type ? TYPES[p.type] : null, p.size ? money(p.size.amount, c) + ' starting size' : null].filter(Boolean).join(' · ');
    if (head) out.push('<b>' + esc(head) + '</b>');
    var g = goalOf(p);
    if (g) out.push('My goal · <b>' + esc(goalText(g)) + '</b>');
    if (p.type === 'evaluation' && ev.phaseCount) out.push(ev.phaseCount === 1 ? 'One-phase evaluation' : ev.currentPhase ? 'Phase ' + ev.currentPhase + ' of ' + ev.phaseCount : ev.phaseCount + '-phase evaluation');
    if (p.type === 'evaluation') line('Phase target', ev.phaseTarget);
    if (p.type !== 'evaluation') {
      line('My payout target', ms.payoutTarget);
      line('Firm minimum for payout', ms.firmMin);
      if (ms.payoutTarget || ms.firmMin) out.push('<span style="color:var(--t3)">Reaching these numbers does not confirm payout eligibility.</span>');
      line('Growth target', ms.growthTarget);
    }
    line('Firm daily loss', fr.dailyLoss, (fr.dailyReset ? ' · resets ' + fr.dailyReset : '') + (fr.dailyLoss && fr.dailyLoss.base && fr.dailyLoss.base !== 'starting' ? ' · needs each day’s reference' : ''));
    line('Firm overall drawdown', fr.overall, fr.overallMode ? ' · ' + fr.overallMode + (fr.overallRef ? ' on ' + fr.overallRef : '') : '');
    line('My session loss budget', pr.sessionBudget);
    line('My risk per trade', pr.riskPerTrade);
    if (pr.style) out.push('Style · <b>' + esc(pr.style) + '</b>');
    if (pr.reminder) out.push('Reminder · <b>“' + esc(pr.reminder) + '”</b>');
    var other = p.type === 'evaluation' ? branchSummary(p, 'master') : p.type === 'master' ? branchSummary(p, 'evaluation') : [];
    if (other.length) out.push('<span style="color:var(--t3)">' + esc(TYPES[p.type === 'evaluation' ? 'master' : 'evaluation']) + ' answers are set aside on this account.</span>');
    return out;
  }
  // The account's trade history: imported counts and checked dates as the server reports them.
  function historyBlock(key) {
    var im = imports[key], st = im && im.status;
    if (!st) return '<div class="su-src">Trade history</div><div class="su-card-line">' + esc(im && im.error ? im.error : 'Checking…') + '</div>';
    var line, action = '';
    if (st.state === 'not_requested') {
      line = st.closed_trades ? trades(st.closed_trades) + ' from the last few days. Older trades are not imported yet.' : 'Only recent trades are imported so far.';
      action = '<button type="button" class="btn btn-blue rip" id="importHistoryAction" data-setup="import"' + (im.starting ? ' disabled' : '') + '>Import trade history</button>';
    } else if (st.state === 'importing') {
      line = 'Importing… ' + trades(st.closed_trades) + ' so far · checked ' + checkedText(st);
      action = '<button type="button" class="su-link" data-setup="viewImport">View</button>';
      keepPolling();
    } else if (st.state === 'imported') {
      line = trades(st.closed_trades) + ' · checked ' + checkedText(st);
    } else if (st.state === 'empty') {
      line = 'No closed trades found · checked ' + checkedText(st);
    } else if (st.state === 'unavailable') {
      line = 'This token cannot read closed trades.';
    } else {
      line = (st.state === 'limited' ? 'Imported with limits: ' : 'Import paused: ') + trades(st.closed_trades) + ' saved · checked ' + checkedText(st);
      action = '<button type="button" class="su-link" data-setup="viewImport">Details</button>';
    }
    return '<div class="su-src">Trade history</div><div class="su-hist"><div class="su-card-line" style="margin:0">' + esc(line) + '</div>' + action + '</div>';
  }
  function renderCard(container, key) {
    if (!container) return;
    if (!enabled()) { container.style.display = 'none'; container.innerHTML = ''; return; }
    ensureDom();
    loadAll(); loadOne(key);
    var e = entry(key);
    container.setAttribute('data-setup-key', key);
    if (!e.loaded) { container.innerHTML = '<div class="su-card-title">' + esc(accountTitle(key)) + '</div><div class="su-card-line">' + esc(e.loadError || 'Loading account setup…') + '</div>'; bindCard(container); return; }
    var p = work(key), next = nextAction(p), lines = cardLines(p, key);
    var html = '<div class="su-card-title">' + esc(accountTitle(key)) + '</div>' +
      '<div class="su-src">From your broker</div><div class="su-card-line">' + esc(brokerLine(key)) + '</div>' +
      '<div class="su-src">You entered</div>' + (lines.length ? lines.map(function (l) { return '<div class="su-card-line">' + l + '</div>'; }).join('') :
        '<div class="su-card-line">' + (p.status === 'in_progress' ? 'Setup started. Your answers so far are saved on this account.' : 'Nothing yet. Tell TaliTrade what kind of account this is, so targets and limits are measured your way.') + '</div>');
    if (e.failed) html += '<div class="su-error" role="alert" style="margin-top:10px"><span>Your latest answers for this account are not saved yet.</span><button type="button" class="btn btn-blue rip" data-setup="retry">Retry</button></div>';

    html += '<div class="su-card-actions">' + (next ? '<button type="button" class="btn ' + 'btn-blue' + ' rip" id="setupNextAction" data-setup="next">' + esc(next.label) + '</button>' : '') +
      (p.status === 'saved' ? '<button type="button" class="su-link" data-setup="edit">Edit answers</button>' : '') + '</div>';
    if (p.status === 'saved') html += '<div class="su-sec">' + ['firm', 'personal'].map(function (f) {
      return '<button type="button" class="su-link" data-setup="' + f + '">' + esc(FLOW_TITLES[f]) + '<span class="su-sec-state">' + sectionState(p, f) + '</span></button>';
    }).join('') + '</div>';
    container.innerHTML = html;
    bindCard(container);
  }
  function bindCard(container) {
    if (container._setupBound) return;
    container._setupBound = true;
    container.addEventListener('click', function (ev) {
      var t = ev.target.closest ? ev.target.closest('[data-setup]') : null; if (!t) return;
      var k = container.getAttribute('data-setup-key'), what = t.getAttribute('data-setup'), e = entry(k);
      if (!e.loaded) return;
      if (what === 'retry') { if (e.failed) { var a = e.failed; a.onSaved = null; send(a).then(function () { refresh(k, false); }); } return; }
      if (what === 'import') { if (!(imports[k] && imports[k].starting)) { ui.key = k; ui.mode = 'import'; ui.confirm = null; ui.error = null; show(); startImport(k, false).then(keepPolling); } return; }
      if (what === 'viewImport') return openImport(k);
      var p = work(k), n = nextAction(p);
      if (what === 'edit') return open(k, 'basics', 'type');
      if (what === 'firm' || what === 'personal') return open(k, what);
      if (n) open(k, n.flow, n.step);
    });
  }
  function refresh(key, flash) {
    var card = $('mtApiSetupCard');
    if (card && host.getActiveAccount() === key && card.style.display !== 'none') {
      renderCard(card, key);
      if (flash) { card.classList.remove('su-card-flash'); void card.offsetWidth; card.classList.add('su-card-flash'); if (card.scrollIntoView) card.scrollIntoView({ block: 'nearest', behavior: 'auto' }); }
    }
    if (host.refreshSwitcher && !ui.open) host.refreshSwitcher();
  }
  function refreshVisible() { if (host) { var k = host.getActiveAccount(); if (k) refresh(k, false); } }
  function switcherLine(key) {
    if (!enabled()) return '';
    if (pendingInvite[key]) return 'Ready to personalize';
    var e = cache[key];
    if (!e || !e.loaded) return '';
    var p = work(key);
    if (e.failed) return 'Answers not saved yet';
    if (p.status !== 'saved') return p.status === 'in_progress' ? 'Setup in progress' : 'Set up account';
    return [p.type ? TYPES[p.type] : null, p.size ? money(p.size.amount, cur(key, p)) : null].filter(Boolean).join(' · ') || 'Set up';
  }

  global.TaliAccountSetup = {
    attach: function (h) { host = h; },
    onFirstSnapshot: onFirstSnapshot, onContextChange: onContextChange, renderCard: renderCard, switcherLine: switcherLine,
    open: open, isOpen: function () { return ui.open; }, isEnabled: enabled,
    hasPendingInvite: function (key) { return !!pendingInvite[key]; },
    profile: function (key) { return cache[key] && cache[key].loaded ? clone(toServer(work(key))) : null; },
    _internals: { convert: convert, nextAction: nextAction, stepsFor: stepsFor, goalChoices: goalChoices, normalize: normalize, toServer: toServer }
  };
})(typeof window !== 'undefined' ? window : globalThis);
