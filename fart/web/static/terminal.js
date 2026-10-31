/* KingDegen radar, the Terminal (milestone 12). One live table of every coin in view, fed by the server's event stream.
   The server renders the skeleton (toolbar, table header, panels, help); this script fills it. Rules of the file:
   - every cell is built with DOM calls from the feed's JSON; no HTML string is ever built from data, nothing is evaluated,
     nothing external is loaded (the content-security policy of the site allows only this origin);
   - one EventSource per tab (`/api/public/stream`); the browser resumes with Last-Event-ID on its own, a changed follow
     list reconnects with `?follow=` and `?last=`; without EventSource, or while it fails, the snapshot is polled;
   - filters, saved filters, follows, notes, columns, density and the sound switch live in this browser (localStorage) and
     the filters also in the URL, so a view can be shared as a link;
   - nothing here places an order or signs anything: every action is a copy or a link to another site in a new tab. */
(function () {
  'use strict';
  var root = document.getElementById('terminal');
  if (!root) { return; }
  var cfg = {};
  try { cfg = JSON.parse(root.getAttribute('data-config') || '{}') || {}; } catch (e) { cfg = {}; }
  var TERMINALS = (cfg.terminals || []).slice(0, 5);
  var LINKS = cfg.links || {};
  var PRESETS = cfg.presets || [];
  var COLUMNS = cfg.columns || [];
  var FOLLOW_MAX = cfg.follow_max || 50;
  var STREAM = cfg.stream || '/api/public/stream';
  var SNAPSHOT = cfg.snapshot || '/api/public/terminal';
  var POLL_MS = 3000;
  var RENDER_MIN_MS = 250;
  var STORE = { filters: 'fart.terminal.filters', follow: 'fart.terminal.follow', notes: 'fart.terminal.notes', cols: 'fart.terminal.cols',
                density: 'fart.terminal.density', sound: 'fart.terminal.sound', sort: 'fart.terminal.sort' };
  var STATUS_PILL = { probation: 'pill-soon', early_buyers: 'pill-ok', meta_burst: 'pill-stale', observe: 'pill-fresh', graduated: 'pill-winner', unwatched: 'pill-unknown' };
  var STATUS_ORDER = { observe: 0, early_buyers: 1, meta_burst: 2, probation: 3, graduated: 4, unwatched: 5 };
  var SORT_DEFAULT_DIR = { age: -1, coin: 1, mcap: -1, prog: -1, buyers: -1, net: -1, bs: -1, share: -1, dev: -1, bundle: -1, maker: -1, theme: -1, x: -1, last: 1, status: 1, plays: -1 };

  /* ----- small helpers ------------------------------------------------------------------------------------------------- */
  function $(id) { return document.getElementById(id); }
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) { e.className = cls; } if (text !== undefined && text !== null) { e.textContent = String(text); } return e; }
  function clear(node) { while (node && node.firstChild) { node.removeChild(node.firstChild); } }
  function load(key, dflt) { try { var v = window.localStorage.getItem(key); return v === null ? dflt : JSON.parse(v); } catch (e) { return dflt; } }
  function save(key, val) { try { if (val === null || val === undefined) { window.localStorage.removeItem(key); } else { window.localStorage.setItem(key, JSON.stringify(val)); } } catch (e) { /* private mode: the page works without memory */ } }
  function isNum(x) { return typeof x === 'number' && isFinite(x); }
  function fmt(x, d) {
    if (!isNum(x)) { return '–'; }
    d = d === undefined ? 2 : d;
    try { return Number(x).toLocaleString('nl-NL', { minimumFractionDigits: d, maximumFractionDigits: d }); }
    catch (e) { return Number(x).toFixed(d).replace('.', ','); }
  }
  function fmtInt(x) { return fmt(x, 0); }
  function fmtPct(x, d) { return isNum(x) ? fmt(x * 100, d === undefined ? 0 : d) + ' %' : '–'; }
  function fmtSol(x, d) { return isNum(x) ? fmt(x, d === undefined ? 2 : d) + ' SOL' : '–'; }
  function fmtSigned(x, d) { return isNum(x) ? (x > 0 ? '+' : '') + fmt(x, d === undefined ? 2 : d) : '–'; }
  function fmtUsd(x) { if (!isNum(x)) { return ''; } if (x >= 1e6) { return '$ ' + fmt(x / 1e6, 2) + ' mln'; } if (x >= 1e4) { return '$ ' + fmt(x / 1e3, 1) + 'k'; } return '$ ' + fmt(x, 0); }
  function fmtAge(s) {
    if (!isNum(s)) { return '–'; }
    s = Math.max(0, Math.round(s));
    if (s < 60) { return s + ' s'; }
    if (s < 600) { return Math.floor(s / 60) + ' min ' + (s % 60) + ' s'; }
    if (s < 3600) { return Math.floor(s / 60) + ' min'; }
    if (s < 86400) { return Math.floor(s / 3600) + ' u ' + Math.floor((s % 3600) / 60) + ' min'; }
    return Math.floor(s / 86400) + ' d ' + Math.floor((s % 86400) / 3600) + ' u';
  }
  function clock(d) { d = d || new Date(); function p(n) { return (n < 10 ? '0' : '') + n; } return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds()); }
  function tsOf(iso) { var t = Date.parse(iso); return isNaN(t) ? null : t / 1000; }
  function short(m) { return !m ? '' : (m.length <= 12 ? m : m.slice(0, 4) + '…' + m.slice(-4)); }
  function link(pattern, mint) { return String(pattern).split('{mint}').join(encodeURIComponent(mint)); }
  function same(a, b) { return JSON.stringify(a || {}) === JSON.stringify(b || {}); }
  function openExternal(url) { var w = window.open(url, '_blank', 'noopener,noreferrer'); if (w) { w.opener = null; } }

  /* ----- state ---------------------------------------------------------------------------------------------------------- */
  var rows = {};                 // mint -> row (plus _at: server time of the row, _v: version)
  var rowCount = 0;
  var version = 0;
  var serverOffset = null;       // Date.now()/1000 - server time, from the last event
  var follow = load(STORE.follow, []);
  if (!Array.isArray(follow)) { follow = []; }
  follow = follow.filter(function (m) { return typeof m === 'string' && m.length >= 30 && m.length <= 50; }).slice(0, FOLLOW_MAX);
  var notes = load(STORE.notes, {}) || {};
  var filter = {};
  var sort = load(STORE.sort, null) || { key: 'age', dir: -1 };
  var colsOn = load(STORE.cols, null);
  var density = load(STORE.density, 'compact');
  var soundOn = load(STORE.sound, false) === true;
  var selected = null;           // mint of the selected row
  var detailMint = null;
  var detailRow = null;          // the last known row of the detail coin (it may leave the view)
  var plays = { rows: [], rules: [] };
  var seenPlays = null;          // ids seen; new ones ring the bell
  var paused = false;
  var lastEventId = null;
  var es = null;
  var connected = false;
  var pollTimer = null;
  var hideTimer = null;
  var renderTimer = null;
  var lastRender = 0;
  var dirty = { table: true, follow: true, plays: true, meta: true };
  var engineAge = null;
  var solUsd = { price: null, known: false };
  var meta = { t: null, viewers: null, note: null };
  var els = {};                  // mint -> { tr, td: {col: td}, v }
  var wantMint = null;           // ?mint= from the URL: open its details once it is in view
  var audio = null;

  var tbody = $('t-body'), table = $('t-table'), detail = $('t-detail');
  var toast = el('div', 't-toast'); toast.setAttribute('role', 'status'); toast.setAttribute('aria-live', 'polite'); root.appendChild(toast);
  var toastTimer = null;
  function say(text) { toast.textContent = text; toast.classList.add('is-on'); if (toastTimer) { clearTimeout(toastTimer); } toastTimer = setTimeout(function () { toast.classList.remove('is-on'); }, 1800); }

  function serverNow() { var t = Date.now() / 1000; return serverOffset === null ? t : t - serverOffset; }
  function noteServerTime(iso) { var t = tsOf(iso); if (t !== null) { serverOffset = Date.now() / 1000 - t; meta.t = t; } }

  /* ----- columns -------------------------------------------------------------------------------------------------------- */
  function colOn(id) {
    if (colsOn && typeof colsOn === 'object' && Object.prototype.hasOwnProperty.call(colsOn, id)) { return !!colsOn[id]; }
    for (var i = 0; i < COLUMNS.length; i++) { if (COLUMNS[i].id === id) { return !!COLUMNS[i]['default']; } }
    return true;
  }
  function applyCols() {
    var cells = table.querySelectorAll('[data-col]');
    for (var i = 0; i < cells.length; i++) { var id = cells[i].getAttribute('data-col'); cells[i].hidden = !colOn(id); }
    var boxes = document.querySelectorAll('.col-toggles input[data-col]');
    for (var j = 0; j < boxes.length; j++) { boxes[j].checked = colOn(boxes[j].getAttribute('data-col')); }
  }
  function applyDensity() {
    table.classList.remove('density-comfortable', 'density-compact', 'density-dense');
    table.classList.add('density-' + (density === 'comfortable' || density === 'dense' ? density : 'compact'));
    var sel = $('t-density'); if (sel) { sel.value = density; }
  }

  /* ----- filters -------------------------------------------------------------------------------------------------------- */
  var FIELDS = { q: 'f-q', max_age_min: 'f-maxage', min_buyers: 'f-minbuyers', min_net: 'f-minnet', max_share_pct: 'f-maxshare', max_dev_pct: 'f-maxdev',
                 min_curve_sol: 'f-mincurve', bundled: 'f-bundled', status: 'f-status', theme: 'f-theme', maker: 'f-maker', rising: 'f-rising', plays: 'f-plays' };
  function num(v) { if (v === '' || v === null || v === undefined) { return null; } var x = parseFloat(String(v).replace(',', '.')); return isFinite(x) ? x : null; }
  function cleanFilter(f) {
    var out = {};
    if (!f || typeof f !== 'object') { return out; }
    if (typeof f.q === 'string' && f.q.trim()) { out.q = f.q.trim().slice(0, 80); }
    if (isNum(f.max_age_s) && f.max_age_s >= 0) { out.max_age_s = f.max_age_s; }
    if (isNum(f.min_buyers) && f.min_buyers >= 0) { out.min_buyers = f.min_buyers; }
    if (isNum(f.min_net)) { out.min_net = f.min_net; }
    if (isNum(f.max_share) && f.max_share >= 0) { out.max_share = f.max_share; }
    if (isNum(f.max_dev_frac) && f.max_dev_frac >= 0) { out.max_dev_frac = f.max_dev_frac; }
    if (isNum(f.min_curve_sol) && f.min_curve_sol >= 0) { out.min_curve_sol = f.min_curve_sol; }
    if (f.bundled === 'no' || f.bundled === 'yes' || f.bundled === 'unknown') { out.bundled = f.bundled; }
    if (typeof f.status === 'string' && f.status) { out.status = [f.status]; }
    else if (Array.isArray(f.status) && f.status.length) { out.status = f.status.filter(function (s) { return typeof s === 'string'; }).slice(0, 8); }
    if (f.theme === 'burning' || f.theme === 'any') { out.theme = f.theme; }
    if (f.maker === 'track' || f.maker === 'clean') { out.maker = f.maker; }
    if (f.rising === true) { out.rising = true; }
    if (f.plays === true) { out.plays = true; }
    return out;
  }
  function filterFromForm() {
    var f = {};
    var q = $(FIELDS.q); f.q = q ? q.value : '';
    var a = num($(FIELDS.max_age_min).value); if (a !== null) { f.max_age_s = a * 60; }
    f.min_buyers = num($(FIELDS.min_buyers).value);
    f.min_net = num($(FIELDS.min_net).value);
    var sh = num($(FIELDS.max_share_pct).value); if (sh !== null) { f.max_share = sh / 100; }
    var dv = num($(FIELDS.max_dev_pct).value); if (dv !== null) { f.max_dev_frac = dv / 100; }
    f.min_curve_sol = num($(FIELDS.min_curve_sol).value);
    f.bundled = $(FIELDS.bundled).value;
    var st = $(FIELDS.status).value;
    if (st) { f.status = [st]; } else if (filter.status && filter.status.length > 1) { f.status = filter.status; }   /* a preset's list stays until the select changes */
    f.theme = $(FIELDS.theme).value;
    f.maker = $(FIELDS.maker).value;
    f.rising = $(FIELDS.rising).checked;
    f.plays = $(FIELDS.plays).checked;
    return cleanFilter(f);
  }
  function formFromFilter(f) {
    $(FIELDS.q).value = f.q || '';
    $(FIELDS.max_age_min).value = isNum(f.max_age_s) ? String(Math.round(f.max_age_s / 60)) : '';
    $(FIELDS.min_buyers).value = isNum(f.min_buyers) ? String(f.min_buyers) : '';
    $(FIELDS.min_net).value = isNum(f.min_net) ? String(f.min_net) : '';
    $(FIELDS.max_share_pct).value = isNum(f.max_share) ? String(Math.round(f.max_share * 1000) / 10) : '';
    $(FIELDS.max_dev_pct).value = isNum(f.max_dev_frac) ? String(Math.round(f.max_dev_frac * 1000) / 10) : '';
    $(FIELDS.min_curve_sol).value = isNum(f.min_curve_sol) ? String(f.min_curve_sol) : '';
    $(FIELDS.bundled).value = f.bundled || '';
    $(FIELDS.status).value = (f.status && f.status.length === 1) ? f.status[0] : '';
    $(FIELDS.theme).value = f.theme || '';
    $(FIELDS.maker).value = f.maker || '';
    $(FIELDS.rising).checked = f.rising === true;
    $(FIELDS.plays).checked = f.plays === true;
    var chips = document.querySelectorAll('.t-presets .chip');
    for (var i = 0; i < chips.length; i++) {
      var id = chips[i].getAttribute('data-preset');
      var p = presetById(id);
      var on = p ? same(cleanFilter(p.filter), f) : (!id && Object.keys(f).length === 0);
      chips[i].setAttribute('aria-pressed', on ? 'true' : 'false');
    }
  }
  function presetById(id) { for (var i = 0; i < PRESETS.length; i++) { if (PRESETS[i].id === id) { return PRESETS[i]; } } return null; }
  function setFilter(f, fromUrl) {
    filter = cleanFilter(f);
    formFromFilter(filter);
    dirty.table = true; schedule();
    if (!fromUrl) { writeUrl(); }
  }
  function passes(r, now) {
    var f = filter;
    if (f.q) {
      var q = f.q.toLowerCase();
      var hay = ((r.name || '') + ' ' + (r.symbol || '') + ' ' + r.mint).toLowerCase();
      if (hay.indexOf(q) < 0) { return false; }
    }
    if (isNum(f.max_age_s) && ageOf(r, now) > f.max_age_s) { return false; }
    if (isNum(f.min_buyers) && !(isNum(r.buyers_5m) && r.buyers_5m >= f.min_buyers)) { return false; }
    if (isNum(f.min_net) && !(isNum(r.net_sol_5m) && r.net_sol_5m >= f.min_net)) { return false; }
    if (isNum(f.max_share) && !(isNum(r.share) && r.share <= f.max_share)) { return false; }         /* unknown stays unknown: it does not pass a bound */
    if (isNum(f.max_dev_frac) && !(isNum(r.dev_frac) && r.dev_frac <= f.max_dev_frac)) { return false; }
    if (isNum(f.min_curve_sol) && !(isNum(r.curve_sol) && r.curve_sol >= f.min_curve_sol)) { return false; }
    if (f.bundled === 'no' && r.bundled !== false) { return false; }
    if (f.bundled === 'yes' && r.bundled !== true) { return false; }
    if (f.bundled === 'unknown' && r.bundled !== null && r.bundled !== undefined) { return false; }
    if (f.status && f.status.indexOf(r.status) < 0) { return false; }
    if (f.theme === 'burning' && !(r.theme && r.theme.bursting)) { return false; }
    if (f.theme === 'any' && !r.theme) { return false; }
    if (f.maker === 'track' && !(r.maker_stats && ((r.maker_stats.grads || 0) > 0 || (r.maker_stats.winners || 0) > 0))) { return false; }
    if (f.maker === 'clean' && !(r.maker_stats && r.maker_stats.bundled === 0 && r.maker_stats.dumped === 0)) { return false; }
    if (f.rising && !(isNum(r.d_buyers) && r.d_buyers > 0 && isNum(r.d_net) && r.d_net > 0)) { return false; }
    if (f.plays && !(r.plays && r.plays.length)) { return false; }
    return true;
  }

  /* ----- the URL as a saved view ------------------------------------------------------------------------------------------ */
  var URL_KEYS = { q: 'q', max_age_s: 'maxage', min_buyers: 'minbuyers', min_net: 'minnet', max_share: 'maxshare', max_dev_frac: 'maxdev', min_curve_sol: 'mincurve',
                   bundled: 'bundel', status: 'status', theme: 'thema', maker: 'maker', rising: 'momentum', plays: 'plays' };
  function readUrl() {
    var out = { filter: null, sort: null, mint: null, preset: null };
    var sp;
    try { sp = new URLSearchParams(window.location.search); } catch (e) { return out; }
    var f = {}, any = false;
    if (sp.get('preset')) { var p = presetById(sp.get('preset')); if (p) { f = JSON.parse(JSON.stringify(p.filter)); any = true; out.preset = p.id; } }
    if (sp.has('q')) { f.q = sp.get('q'); any = true; }
    if (sp.has('maxage')) { var a = num(sp.get('maxage')); if (a !== null) { f.max_age_s = a * 60; any = true; } }
    if (sp.has('minbuyers')) { f.min_buyers = num(sp.get('minbuyers')); any = true; }
    if (sp.has('minnet')) { f.min_net = num(sp.get('minnet')); any = true; }
    if (sp.has('maxshare')) { var s = num(sp.get('maxshare')); if (s !== null) { f.max_share = s / 100; any = true; } }
    if (sp.has('maxdev')) { var d = num(sp.get('maxdev')); if (d !== null) { f.max_dev_frac = d / 100; any = true; } }
    if (sp.has('mincurve')) { f.min_curve_sol = num(sp.get('mincurve')); any = true; }
    if (sp.has('bundel')) { f.bundled = sp.get('bundel'); any = true; }
    if (sp.has('status')) { f.status = sp.get('status').split(',').filter(Boolean); any = true; }
    if (sp.has('thema')) { f.theme = sp.get('thema'); any = true; }
    if (sp.has('maker')) { f.maker = sp.get('maker'); any = true; }
    if (sp.has('momentum')) { f.rising = sp.get('momentum') === '1'; any = true; }
    if (sp.has('plays')) { f.plays = sp.get('plays') === '1'; any = true; }
    if (any) { out.filter = cleanFilter(f); }
    if (sp.has('sort') && SORT_DEFAULT_DIR[sp.get('sort')] !== undefined) { out.sort = { key: sp.get('sort'), dir: sp.get('dir') === 'asc' ? 1 : (sp.get('dir') === 'desc' ? -1 : SORT_DEFAULT_DIR[sp.get('sort')]) }; }
    var m = sp.get('mint'); if (m && m.length >= 30 && m.length <= 50 && /^[A-Za-z0-9]+$/.test(m)) { out.mint = m; }
    return out;
  }
  var urlTimer = null;
  function writeUrl() {
    if (!window.history || !window.history.replaceState) { return; }
    if (urlTimer) { clearTimeout(urlTimer); }
    urlTimer = setTimeout(function () {
      var sp = new URLSearchParams();
      var keys = Object.keys(URL_KEYS);
      for (var i = 0; i < keys.length; i++) {
        var k = keys[i], v = filter[k];
        if (v === undefined || v === null || v === false) { continue; }
        if (k === 'max_age_s') { v = Math.round(v / 60); }
        else if (k === 'max_share' || k === 'max_dev_frac') { v = Math.round(v * 1000) / 10; }
        else if (k === 'status') { v = v.join(','); }
        else if (v === true) { v = '1'; }
        sp.set(URL_KEYS[k], String(v));
      }
      if (!(sort.key === 'age' && sort.dir === -1)) { sp.set('sort', sort.key); sp.set('dir', sort.dir > 0 ? 'asc' : 'desc'); }
      if (detailMint) { sp.set('mint', detailMint); }
      var qs = sp.toString();
      var url = window.location.pathname + (qs ? '?' + qs : '');
      if (url !== window.location.pathname + window.location.search) { window.history.replaceState(null, '', url); }
    }, 300);
  }

  /* ----- sorting ---------------------------------------------------------------------------------------------------------- */
  function sortValue(r, key, now) {
    switch (key) {
      case 'age': return isNum(r.created_t) ? r.created_t : null;
      case 'coin': return (r.name || '').toLowerCase() || null;
      case 'mcap': return isNum(r.mcap_sol) ? r.mcap_sol : null;
      case 'prog': return isNum(r.prog) ? r.prog : null;
      case 'buyers': return isNum(r.buyers_5m) ? r.buyers_5m : null;
      case 'net': return isNum(r.net_sol_5m) ? r.net_sol_5m : null;
      case 'bs': return isNum(r.buys_5m) ? r.buys_5m - (r.sells_5m || 0) : null;
      case 'share': return isNum(r.share) ? r.share : null;
      case 'dev': return isNum(r.dev_frac) ? r.dev_frac : null;
      case 'bundle': return r.bundled === true ? 1 : (r.bundled === false ? 0 : null);
      case 'maker': return r.maker_stats ? ((r.maker_stats.grads || 0) * 10 + (r.maker_stats.winners || 0)) : null;
      case 'theme': return r.theme ? ((r.theme.bursting ? 1000 : 0) + (r.theme.n_coins || 0)) : null;
      case 'x': return isNum(r.x_1h) ? r.x_1h : null;
      case 'last': return lastAgeOf(r, now);
      case 'status': return STATUS_ORDER[r.status] === undefined ? 9 : STATUS_ORDER[r.status];
      case 'plays': return r.plays ? r.plays.length : 0;
      default: return null;
    }
  }
  function compare(a, b, now) {
    var va = sortValue(a, sort.key, now), vb = sortValue(b, sort.key, now);
    if (va === null && vb === null) { return b.created_t - a.created_t; }
    if (va === null) { return 1; }
    if (vb === null) { return -1; }
    var c = va < vb ? -1 : (va > vb ? 1 : 0);
    if (c === 0) { return b.created_t - a.created_t; }
    return c * sort.dir;
  }
  function setSort(key) {
    if (SORT_DEFAULT_DIR[key] === undefined) { return; }
    if (sort.key === key) { sort.dir = -sort.dir; } else { sort = { key: key, dir: SORT_DEFAULT_DIR[key] }; }
    save(STORE.sort, sort);
    markSort();
    dirty.table = true; schedule(); writeUrl();
  }
  function markSort() {
    var ths = table.querySelectorAll('th[data-sort]');
    for (var i = 0; i < ths.length; i++) {
      var k = ths[i].getAttribute('data-sort');
      if (k === sort.key) { ths[i].setAttribute('aria-sort', sort.dir > 0 ? 'ascending' : 'descending'); ths[i].classList.add('is-sorted'); }
      else { ths[i].removeAttribute('aria-sort'); ths[i].classList.remove('is-sorted'); }
    }
  }

  /* ----- ages that keep ticking ------------------------------------------------------------------------------------------ */
  function ageOf(r, now) { return isNum(r.created_t) ? Math.max(0, now - r.created_t) : (r.age_s || 0); }
  function lastAgeOf(r, now) { if (!isNum(r.last_trade_age_s)) { return null; } return Math.max(0, r.last_trade_age_s + (now - r._at)); }

  /* ----- building cells ----------------------------------------------------------------------------------------------------- */
  function pill(cls, text, title) { var p = el('span', 'pill ' + cls, text); if (title) { p.title = title; } return p; }
  function stack(top, bottom, cls) { var s = el('span', 'stack-2' + (cls ? ' ' + cls : '')); s.appendChild(el('b', null, top)); if (bottom) { s.appendChild(el('span', 'caption', bottom)); } return s; }
  function delta(d, unit) {
    if (!isNum(d) || d === 0) { return null; }
    var s = el('span', 'delta ' + (d > 0 ? 'is-up' : 'is-down'), (d > 0 ? '↑' : '↓') + fmt(Math.abs(d), unit === 'sol' ? 2 : 0));
    s.title = 'tegenover een minuut eerder';
    return s;
  }
  function anchor(text, url, title) {
    var a = el('a', 'tlink', text); a.href = url; a.target = '_blank'; a.rel = 'noopener noreferrer nofollow'; if (title) { a.title = title; }
    return a;
  }
  function linksFor(mint, withCopy) {
    var wrap = el('span', 't-links');
    if (withCopy) { var b = el('button', 'tlink tlink-btn', 'kopieer'); b.type = 'button'; b.title = 'kopieer het mint-adres'; b.addEventListener('click', function (e) { e.stopPropagation(); copyMint(mint); }); wrap.appendChild(b); }
    if (LINKS.pumpfun) { wrap.appendChild(anchor('pump', link(LINKS.pumpfun, mint), 'open op pump.fun (nieuw tabblad)')); }
    if (LINKS.dexscreener) { wrap.appendChild(anchor('dex', link(LINKS.dexscreener, mint), 'open op DexScreener (nieuw tabblad)')); }
    if (LINKS.solscan) { wrap.appendChild(anchor('scan', link(LINKS.solscan, mint), 'open op Solscan (nieuw tabblad)')); }
    for (var i = 0; i < TERMINALS.length; i++) {
      var t = TERMINALS[i];
      wrap.appendChild(anchor(t.name, link(t.url, mint), 'open in ' + t.name + ' (toets ' + (i + 1) + '; nieuw tabblad; dit platform plaatst nooit een order' + (t.verified ? ')' : '; linkpatroon nog niet geverifieerd)')));
    }
    return wrap;
  }
  function fillCell(td, col, r, now) {
    clear(td);
    switch (col) {
      case 'age':
        td.textContent = fmtAge(ageOf(r, now)); td.setAttribute('data-created', isNum(r.created_t) ? String(r.created_t) : ''); break;
      case 'coin': {
        var star = el('button', 'star' + (isFollowed(r.mint) ? ' is-on' : ''), isFollowed(r.mint) ? '★' : '☆'); star.type = 'button';
        star.title = isFollowed(r.mint) ? 'ontvolg' : 'volg deze munt (alleen in deze browser)'; star.setAttribute('aria-pressed', isFollowed(r.mint) ? 'true' : 'false');
        star.addEventListener('click', function (e) { e.stopPropagation(); toggleFollow(r.mint); });
        td.appendChild(star);
        var body = el('span', 'coin-body');
        body.appendChild(el('span', 'coin-title', r.name || 'naam onbekend'));
        body.appendChild(el('span', 'coin-mint', (r.symbol ? r.symbol + ' · ' : '') + short(r.mint)));
        if (notes[r.mint]) { var nt = el('span', 'coin-note', '✎ ' + String(notes[r.mint]).slice(0, 60)); nt.title = notes[r.mint]; body.appendChild(nt); }
        td.appendChild(body); break;
      }
      case 'mcap': td.appendChild(stack(fmtSol(r.mcap_sol, 1), r.mcap_usd !== null && r.mcap_usd !== undefined ? fmtUsd(r.mcap_usd) : (solUsd.known ? '' : 'SOL-prijs onbekend'))); break;
      case 'prog': {
        var nb = el('span', 'num-bar');
        nb.appendChild(el('b', null, isNum(r.curve_sol) ? fmt(r.curve_sol, 1) + ' / 85' : (r.graduated ? 'gegradueerd' : '–')));
        var bar = el('span', 'bar'); var fill = el('span', 'bar-fill'); fill.style.width = (isNum(r.prog) ? Math.max(0, Math.min(100, r.prog * 100)) : 0) + '%'; bar.appendChild(fill); nb.appendChild(bar);
        td.appendChild(nb); td.title = isNum(r.prog) ? fmtPct(r.prog, 1) + ' van de graduatie' : 'nog geen trade gezien'; break;
      }
      case 'buyers': td.appendChild(el('b', null, fmtInt(r.buyers_5m))); var db = delta(r.d_buyers); if (db) { td.appendChild(db); } break;
      case 'net': { var n = el('b', isNum(r.net_sol_5m) ? (r.net_sol_5m > 0 ? 'is-up' : (r.net_sol_5m < 0 ? 'is-down' : '')) : '', fmtSigned(r.net_sol_5m, 2)); td.appendChild(n); var dn = delta(r.d_net, 'sol'); if (dn) { td.appendChild(dn); } break; }
      case 'bs': td.textContent = fmtInt(r.buys_5m) + ' / ' + fmtInt(r.sells_5m); td.title = fmtInt(r.trades_total) + ' trades in totaal'; break;
      case 'share': td.textContent = fmtPct(r.share); td.className = 'num' + (isNum(r.share) && r.share > 0.3 ? ' is-bad' : ''); td.title = 'grootste koper, laatste 15 min'; break;
      case 'dev': {
        td.textContent = isNum(r.dev_frac) ? fmtPct(r.dev_frac, 1) : (isNum(r.dev_buy_sol) ? fmtSol(r.dev_buy_sol, 2) : '–');
        if (r.dev_sold) { td.appendChild(el('span', 'caption is-bad', ' dev verkocht' + (isNum(r.dev_sold_frac) ? ' ' + fmtPct(r.dev_sold_frac) : ''))); }
        td.title = 'de koop van de maker als deel van de curve' + (isNum(r.dev_buy_sol) ? ' (' + fmtSol(r.dev_buy_sol, 2) + ')' : ''); break;
      }
      case 'bundle':
        if (r.bundled === true) { td.appendChild(pill('pill-fail', 'gebundeld', (r.bundle_reasons || []).join(', ') || 'bundel')); }
        else if (r.bundled === false) { td.appendChild(pill('pill-ok', 'nee', 'geen bundelkenmerk in de eerste trades')); }
        else { td.appendChild(pill('pill-unknown', 'onbekend', 'nog te weinig trades om het te zeggen')); }
        break;
      case 'maker': {
        var ms = r.maker_stats;
        if (!ms) { td.textContent = '–'; td.title = 'geen eerdere munt van deze maker in onze data'; }
        else {
          var txt = fmtInt(ms.coins) + ' munt' + (ms.coins === 1 ? '' : 'en') + ' · ' + fmtInt(ms.grads) + ' grad · ' + fmtInt(ms.winners) + ' win';
          td.appendChild(el('span', null, txt));
          if (ms.track === 'good') { td.appendChild(pill('pill-ok', 'track record')); }
          else if (ms.track === 'bad') { td.appendChild(pill('pill-fail', (ms.bundled ? 'bundelde' : '') + (ms.bundled && ms.dumped ? ', ' : '') + (ms.dumped ? 'dumpte' : ''))); }
          td.title = 'eerdere munten van dezelfde maker: ' + fmtInt(ms.bundled) + ' gebundeld, ' + fmtInt(ms.dumped) + ' met vroege dev-verkoop';
        }
        break;
      }
      case 'theme':
        if (!r.theme) { td.textContent = '–'; }
        else { td.appendChild(el('span', 'theme-term-sm', r.theme.term)); if (r.theme.bursting) { td.appendChild(pill('pill-stale', r.theme.burst_is_new ? 'brandt, nieuw' : 'brandt', 'volgens de engine ' + fmtInt(r.theme.n_coins) + ' munten met dit thema')); } else { td.appendChild(el('span', 'caption', ' ' + fmtInt(r.theme.n_coins) + ' munten')); } }
        break;
      case 'x': td.textContent = isNum(r.x_1h) ? fmtInt(r.x_1h) : '–'; td.title = isNum(r.x_1h) ? 'X-vermeldingen, laatste uur' : 'X-meting niet beschikbaar voor deze munt'; break;
      case 'last': { var la = lastAgeOf(r, now); td.textContent = la === null ? '–' : fmtAge(la); td.setAttribute('data-last', la === null ? '' : String(now - la)); break; }
      case 'status': td.appendChild(pill(STATUS_PILL[r.status] || 'pill-unknown', r.status_label || r.status)); break;
      case 'plays':
        if (r.plays && r.plays.length) { for (var i = 0; i < r.plays.length; i++) { td.appendChild(pill('pill-go', ruleName(r.plays[i]), 'voldoet nu aan de bewezen regel ' + r.plays[i])); } }
        else { td.textContent = '–'; }
        break;
      case 'links': td.appendChild(linksFor(r.mint, true)); break;
      default: td.textContent = '';
    }
  }
  function ruleName(id) { for (var i = 0; i < plays.rules.length; i++) { if (plays.rules[i].id === id) { return plays.rules[i].name || id; } } return id; }
  function buildRow(r, now) {
    var tr = el('tr'); tr.setAttribute('data-mint', r.mint); tr.tabIndex = -1;
    var tds = {};
    for (var i = 0; i < COLUMNS.length; i++) {
      var id = COLUMNS[i].id;
      var td = el('td', ['mcap', 'prog', 'buyers', 'net', 'bs', 'share', 'dev', 'x', 'age', 'last'].indexOf(id) >= 0 ? 'num' : '');
      td.setAttribute('data-col', id); td.setAttribute('data-label', COLUMNS[i].label); td.hidden = !colOn(id);
      fillCell(td, id, r, now); tds[id] = td; tr.appendChild(td);
    }
    tr.addEventListener('click', function (e) { if (e.target.closest('a, button, textarea, input')) { return; } select(r.mint, true); });
    return { tr: tr, td: tds, v: r._v, mint: r.mint };
  }
  function updateRow(entry, r, now) {
    for (var i = 0; i < COLUMNS.length; i++) { var id = COLUMNS[i].id; if (id !== 'links' || entry.v === undefined) { fillCell(entry.td[id], id, r, now); } }
    entry.v = r._v;
  }
  function rowClasses(tr, r) {
    tr.className = (r.mint === selected ? 'is-selected ' : '') + (isFollowed(r.mint) ? 'is-followed ' : '') + (r.plays && r.plays.length ? 'is-play ' : '') + (r.graduated ? 'is-grad ' : '') + (r.observed ? 'is-observed' : '');
    tr.setAttribute('aria-selected', r.mint === selected ? 'true' : 'false');
  }

  /* ----- rendering ---------------------------------------------------------------------------------------------------------- */
  function schedule() {
    if (renderTimer) { return; }
    var wait = Math.max(0, RENDER_MIN_MS - (Date.now() - lastRender));
    renderTimer = setTimeout(function () { renderTimer = null; render(); }, wait);
  }
  function visibleRows(now) {
    var out = [];
    var keys = Object.keys(rows);
    for (var i = 0; i < keys.length; i++) { var r = rows[keys[i]]; if (passes(r, now)) { out.push(r); } }
    out.sort(function (a, b) { return compare(a, b, now); });
    return out;
  }
  function render() {
    lastRender = Date.now();
    renderMeta();
    if (paused) { return; }
    var now = serverNow();
    if (dirty.table) { dirty.table = false; renderTable(now); }
    if (dirty.follow) { dirty.follow = false; renderFollow(now); }
    if (dirty.plays) { dirty.plays = false; renderPlays(now); }
    if (detailMint) { renderDetail(now); }
  }
  function renderTable(now) {
    var list = visibleRows(now);
    var keep = {};
    var frag = document.createDocumentFragment();
    for (var i = 0; i < list.length; i++) {
      var r = list[i];
      var entry = els[r.mint];
      if (!entry) { entry = buildRow(r, now); els[r.mint] = entry; }
      else if (entry.v !== r._v) { updateRow(entry, r, now); }
      rowClasses(entry.tr, r);
      keep[r.mint] = true;
      frag.appendChild(entry.tr);
    }
    var old = Object.keys(els);
    for (var j = 0; j < old.length; j++) { if (!keep[old[j]]) { var e = els[old[j]]; if (e.tr.parentNode) { e.tr.parentNode.removeChild(e.tr); } delete els[old[j]]; } }
    var emptyTr = $('t-empty') ? $('t-empty').parentNode : null;
    if (!list.length) {
      if (!emptyTr) { emptyTr = el('tr', 'empty'); var td = el('td'); td.id = 't-empty'; td.colSpan = Math.max(1, COLUMNS.length); emptyTr.appendChild(td); }
      $('t-empty') ? ($('t-empty').textContent = emptyText()) : null;
      emptyTr.firstChild.textContent = emptyText();
      frag.appendChild(emptyTr);
    } else if (emptyTr && emptyTr.parentNode) { emptyTr.parentNode.removeChild(emptyTr); }
    tbody.appendChild(frag);
    var count = $('t-count');
    if (count) { count.textContent = (list.length === rowCount ? fmtInt(rowCount) + ' munten' : fmtInt(list.length) + ' van ' + fmtInt(rowCount) + ' munten') + (follow.length ? ' · ' + fmtInt(follow.length) + ' gevolgd' : ''); }
  }
  function emptyText() {
    if (!connected && !meta.t) { return 'verbinden met de live-bron …'; }
    if (meta.note) { return meta.note; }
    if (rowCount === 0) { return 'Nog geen munt in beeld: de watchlist is leeg en er is in de laatste ' + Math.round((cfg.creates_window_s || 1800) / 60) + ' minuten geen munt gemaakt, of de engine staat stil.'; }
    return 'Geen munt voldoet aan de filters (' + fmtInt(rowCount) + ' in beeld). Druk op "wis" om alles te tonen.';
  }
  function renderMeta() {
    var conn = $('t-conn');
    if (conn) {
      if (connected) { conn.textContent = 'live'; conn.className = 'pill pill-fresh'; }
      else if (pollTimer) { conn.textContent = 'peiling elke ' + Math.round(POLL_MS / 1000) + ' s'; conn.className = 'pill pill-stale'; }
      else { conn.textContent = 'opnieuw verbinden …'; conn.className = 'pill pill-stale'; }
      if (paused) { conn.textContent += ' · gepauzeerd'; }
    }
    var eng = $('t-engine');
    if (eng) { eng.textContent = engineAge === null ? 'engine: onbekend' : 'engine: ' + fmtAge(engineAge) + ' geleden'; eng.className = engineAge !== null && engineAge > 120 ? 'is-bad' : ''; }
    var sol = $('t-sol');
    if (sol) { sol.textContent = solUsd.known ? 'SOL ' + '$ ' + fmt(solUsd.price, 2) : 'SOL-prijs onbekend'; }
    var upd = $('t-updated');
    if (upd) { upd.textContent = meta.t ? 'bijgewerkt ' + clock(new Date(meta.t * 1000)) : '–'; }
  }
  function tick() {
    var now = serverNow();
    if (!paused) {
      var ages = tbody.querySelectorAll('td[data-col="age"]');
      for (var i = 0; i < ages.length; i++) { var c = parseFloat(ages[i].getAttribute('data-created')); if (isFinite(c)) { ages[i].textContent = fmtAge(now - c); } }
      var lasts = tbody.querySelectorAll('td[data-col="last"]');
      for (var j = 0; j < lasts.length; j++) { var l = parseFloat(lasts[j].getAttribute('data-last')); if (isFinite(l)) { lasts[j].textContent = fmtAge(now - l); } }
      var since = document.querySelectorAll('[data-since]');
      for (var k = 0; k < since.length; k++) { var s = parseFloat(since[k].getAttribute('data-since')); if (isFinite(s)) { since[k].textContent = fmtAge(now - s); } }
    }
    if (engineAge !== null && meta.t) { /* the engine age also ages between heartbeats */ }
  }

  /* ----- selection and details ------------------------------------------------------------------------------------------------ */
  function select(mint, open) {
    var prev = selected;
    selected = mint;
    if (prev && els[prev]) { rowClasses(els[prev].tr, rows[prev] || { mint: prev }); }
    if (mint && els[mint]) { rowClasses(els[mint].tr, rows[mint]); if (els[mint].tr.scrollIntoView) { els[mint].tr.scrollIntoView({ block: 'nearest' }); } }
    if (open) { openDetail(mint); }
  }
  function openDetail(mint) {
    detailMint = mint;
    detailRow = rows[mint] || detailRow;
    detail.hidden = false;
    renderDetail(serverNow());
    writeUrl();
  }
  function closeDetail() { detailMint = null; detail.hidden = true; writeUrl(); }
  var noteTimer = null;
  function renderDetail(now) {
    var r = rows[detailMint] || (detailRow && detailRow.mint === detailMint ? detailRow : null);
    if (!r) {
      $('d-title').textContent = 'nog niet in beeld';
      $('d-mint').textContent = detailMint;
      clear($('d-links')); $('d-links').appendChild(linksFor(detailMint, true));
      var dl = $('d-facts'); clear(dl);
      addFact(dl, 'status', isFollowed(detailMint) ? 'gevolgd; de munt verschijnt zodra de live-bron hem kent' : 'niet in beeld: niet op de watchlist en ouder dan het venster. Volg de munt (f) om hem live te houden.');
      bindNote(detailMint);
      return;
    }
    detailRow = r;
    $('d-title').textContent = (r.name || 'naam onbekend') + (r.symbol ? ' (' + r.symbol + ')' : '');
    $('d-mint').textContent = r.mint;
    var links = $('d-links'); clear(links); links.appendChild(linksFor(r.mint, true));
    var fb = el('button', 'tlink tlink-btn', isFollowed(r.mint) ? 'ontvolg' : 'volg'); fb.type = 'button'; fb.addEventListener('click', function () { toggleFollow(r.mint); }); links.appendChild(fb);
    var facts = $('d-facts'); clear(facts);
    addFact(facts, 'status', r.status_label || r.status);
    addFact(facts, 'aangemaakt', (r.created_at ? clock(new Date(r.created_t * 1000)) : '–') + ' · ' + fmtAge(ageOf(r, now)) + ' geleden' + (rows[r.mint] ? '' : ' (laatst bekende stand)'));
    addFact(facts, 'marktwaarde', fmtSol(r.mcap_sol, 2) + (isNum(r.mcap_usd) ? ' · ' + fmtUsd(r.mcap_usd) : ''));
    addFact(facts, 'curve', isNum(r.curve_sol) ? fmt(r.curve_sol, 2) + ' van 85 SOL (' + fmtPct(r.prog, 1) + ')' : (r.graduated ? 'gegradueerd' : 'nog geen trade'));
    addFact(facts, 'kopers 5 min', fmtInt(r.buyers_5m) + (isNum(r.d_buyers) ? ' (' + fmtSigned(r.d_buyers, 0) + ' t.o.v. een minuut eerder)' : ''));
    addFact(facts, 'netto SOL 5 min', fmtSigned(r.net_sol_5m, 3) + ' SOL' + (isNum(r.d_net) ? ' (' + fmtSigned(r.d_net, 2) + ')' : ''));
    addFact(facts, 'kopen / verkopen 5 min', fmtInt(r.buys_5m) + ' / ' + fmtInt(r.sells_5m) + ' · ' + fmtInt(r.trades_total) + ' trades in totaal');
    addFact(facts, 'grootste koper 15 min', fmtPct(r.share, 1));
    addFact(facts, 'dev-buy', (isNum(r.dev_buy_sol) ? fmtSol(r.dev_buy_sol, 3) : 'onbekend') + (isNum(r.dev_frac) ? ' · ' + fmtPct(r.dev_frac, 2) + ' van de curve' : '') + (r.dev_sold ? ' · dev verkocht' + (isNum(r.dev_sold_frac) ? ' ' + fmtPct(r.dev_sold_frac) : '') : ''));
    addFact(facts, 'bundel', r.bundled === true ? 'ja: ' + ((r.bundle_reasons || []).join(', ') || 'bundel') : (r.bundled === false ? 'geen kenmerk in de eerste trades' : 'onbekend (te weinig trades)'));
    var ms = r.maker_stats;
    addFact(facts, 'maker', (r.maker ? short(r.maker) + ' · ' : '') + (ms ? fmtInt(ms.coins) + ' eerdere munten, ' + fmtInt(ms.grads) + ' graduaties, ' + fmtInt(ms.winners) + ' winnaars, ' + fmtInt(ms.bundled) + ' gebundeld, ' + fmtInt(ms.dumped) + ' met vroege dev-verkoop' : 'geen eerdere munt in onze data'));
    addFact(facts, 'thema', r.theme ? r.theme.term + (r.theme.bursting ? ' · brandt' : '') + (isNum(r.theme.n_coins) ? ' · ' + fmtInt(r.theme.n_coins) + ' munten' : '') : '–');
    addFact(facts, 'X (1 u)', isNum(r.x_1h) ? fmtInt(r.x_1h) + ' vermeldingen' : 'meting niet beschikbaar');
    var la = lastAgeOf(r, now); addFact(facts, 'laatste trade', la === null ? '–' : fmtAge(la) + ' geleden');
    addFact(facts, 'plays', r.plays && r.plays.length ? r.plays.map(ruleName).join(', ') : 'geen regel vuurt');
    /* milestone 13: whether the radar follows the coin (24 h of price points) and where its last point came from */
    var fw = r.followed;
    addFact(facts, 'gevolgd', fw ? (fw.state_label || fw.state) + ' · sinds ' + fmtAge(isNum(fw.since_age_s) ? fw.since_age_s + Math.max(0, now - serverNow()) : 0) + ' geleden · ' + (fw.reasons_label || '–') : 'nee: geen signaal van een vooraf vastgelegde hypothese, regel of observatie');
    var lp = r.last_point;
    addFact(facts, 'laatste punt', lp ? (lp.source_label || lp.source) + ' · ' + fmtAge(isNum(lp.age_s) ? lp.age_s + Math.max(0, now - serverNow()) : 0) + ' geleden' : 'nog geen punt');
    bindNote(r.mint);
  }
  function addFact(dl, k, v) { dl.appendChild(el('dt', null, k)); dl.appendChild(el('dd', null, v)); }
  function bindNote(mint) {
    var ta = $('d-note');
    if (ta.getAttribute('data-mint') !== mint) { ta.value = notes[mint] || ''; ta.setAttribute('data-mint', mint); }
  }
  $('d-note').addEventListener('input', function () {
    var mint = this.getAttribute('data-mint'); if (!mint) { return; }
    var v = this.value.slice(0, 500);
    if (noteTimer) { clearTimeout(noteTimer); }
    noteTimer = setTimeout(function () {
      if (v.trim()) { notes[mint] = v; } else { delete notes[mint]; }
      save(STORE.notes, notes);
      if (els[mint] && rows[mint]) { fillCell(els[mint].td.coin, 'coin', rows[mint], serverNow()); }
      dirty.follow = true; schedule();
    }, 400);
  });
  $('d-close').addEventListener('click', closeDetail);

  /* ----- following -------------------------------------------------------------------------------------------------------- */
  function isFollowed(mint) { return follow.indexOf(mint) >= 0; }
  function toggleFollow(mint) {
    var i = follow.indexOf(mint);
    if (i >= 0) { follow.splice(i, 1); say('ontvolgd'); }
    else { if (follow.length >= FOLLOW_MAX) { say('volglijst vol (' + FOLLOW_MAX + ')'); return; } follow.push(mint); say('gevolgd: blijft live in dit tabblad'); }
    save(STORE.follow, follow);
    if (els[mint] && rows[mint]) { fillCell(els[mint].td.coin, 'coin', rows[mint], serverNow()); rowClasses(els[mint].tr, rows[mint]); }
    dirty.follow = true; dirty.table = true; schedule();
    if (detailMint) { renderDetail(serverNow()); }
    reconnect(true);   /* the server keeps followed coins in the universe for this connection */
  }
  function renderFollow(now) {
    var box = $('t-follow'); clear(box);
    var n = $('t-follow-n'); if (n) { n.textContent = fmtInt(follow.length); n.className = 'pill ' + (follow.length ? 'pill-fresh' : 'pill-unknown'); }
    if (!follow.length) { box.appendChild(el('p', 'empty-state', 'Nog geen gevolgde munten. Druk op f of de ster bij een munt.')); return; }
    var ul = el('ul', 'follow-list');
    for (var i = 0; i < follow.length; i++) {
      var mint = follow[i], r = rows[mint];
      var li = el('li', 'follow-item' + (r ? '' : ' is-away'));
      var head = el('div', 'follow-head');
      var title = el('button', 'follow-title', r ? (r.name || 'naam onbekend') + (r.symbol ? ' · ' + r.symbol : '') : short(mint)); title.type = 'button'; title.setAttribute('data-mint', mint);
      title.addEventListener('click', (function (m) { return function () { select(m, true); }; })(mint));
      head.appendChild(title);
      if (r) { head.appendChild(pill(STATUS_PILL[r.status] || 'pill-unknown', r.status_label || r.status)); }
      else { head.appendChild(pill('pill-unknown', 'wacht op de live-bron')); }
      var rm = el('button', 'tlink tlink-btn', 'ontvolg'); rm.type = 'button'; rm.addEventListener('click', (function (m) { return function () { toggleFollow(m); }; })(mint)); head.appendChild(rm);
      li.appendChild(head);
      if (r) {
        li.appendChild(el('div', 'follow-meta', fmtSol(r.mcap_sol, 1) + ' · curve ' + (isNum(r.curve_sol) ? fmt(r.curve_sol, 1) : '–') + ' / 85 · ' + fmtInt(r.buyers_5m) + ' kopers 5 min · netto ' + fmtSigned(r.net_sol_5m, 2) + ' SOL · ' + fmtAge(ageOf(r, now)) + ' oud'));
      }
      if (notes[mint]) { li.appendChild(el('div', 'follow-note', '✎ ' + notes[mint])); }
      li.appendChild(linksFor(mint, true));
      ul.appendChild(li);
    }
    box.appendChild(ul);
  }

  /* ----- plays ------------------------------------------------------------------------------------------------------------ */
  function renderPlays(now) {
    var box = $('t-plays'), n = $('t-plays-n');
    var rules = plays.rules || [];
    if (n) { n.textContent = fmtInt(rules.length) + ' regel' + (rules.length === 1 ? '' : 's'); n.className = 'pill ' + (rules.length ? 'pill-fresh' : 'pill-unknown'); }
    if (!rules.length) { return; }   /* the server-rendered explanation stays */
    clear(box);
    var list = plays.rows || [];
    if (!list.length) { box.appendChild(el('p', 'empty-state', 'Geen munt voldoet nu aan een bewezen regel (' + fmtInt(rules.length) + ' actief). Zodra er een vuurt, verschijnt hij hier en in de kolom plays.')); return; }
    for (var i = 0; i < list.length; i++) {
      var p = list[i];
      var art = el('article', 'play');
      var head = el('div', 'play-head');
      head.appendChild(el('strong', null, p.name || p.rule));
      head.appendChild(pill(p.status === 'open' ? 'pill-go' : 'pill-pending', p.status_label || p.status));
      var since = el('span', 'caption'); since.appendChild(document.createTextNode('sinds het signaal '));
      var sinceVal = el('span', null, fmtAge(p.since_s)); var asOf = tsOf(p.as_of); if (asOf !== null) { sinceVal.setAttribute('data-since', String(asOf)); } since.appendChild(sinceVal);
      head.appendChild(since);
      art.appendChild(head);
      var coin = el('button', 'follow-title', (p.coin || 'naam onbekend') + (p.symbol ? ' · ' + p.symbol : '') + ' · ' + short(p.mint)); coin.type = 'button';
      coin.addEventListener('click', (function (m) { return function () { select(m, true); }; })(p.mint));
      art.appendChild(coin);
      var dl = el('dl', 'kv');
      addFact(dl, 'gemeten trefkans', fmtPct(p.hit_rate, 1));
      addFact(dl, 'verwachte waarde na kosten', (isNum(p.ev_sol) ? fmtSigned(p.ev_sol, 3) + ' SOL' : '–') + (isNum(p.ev_pct) ? ' (' + fmtSigned(p.ev_pct * 100, 1) + ' %)' : '') + ' per positie, gemeten in het onderzoek');
      addFact(dl, 'curve nu / bij het signaal', (isNum(p.now_curve_sol) ? fmt(p.now_curve_sol, 1) : '–') + ' / ' + (isNum(p.entry_curve_sol) ? fmt(p.entry_curve_sol, 1) : '–') + ' SOL' + (isNum(p.change_pct) ? ' · prijs ' + fmtSigned(p.change_pct * 100, 1) + ' % sinds de vulling' : ''));
      addFact(dl, 'uitstapregel', p.exit_text || '–');
      art.appendChild(dl);
      art.appendChild(linksFor(p.mint, true));
      box.appendChild(art);
    }
  }
  function notePlays(d) {
    var ids = {}; var fresh = [];
    var list = d.rows || [];
    for (var i = 0; i < list.length; i++) { ids[list[i].id] = true; if (seenPlays && !seenPlays[list[i].id]) { fresh.push(list[i]); } }
    if (seenPlays && fresh.length) {
      say('nieuwe play: ' + (fresh[0].name || fresh[0].rule) + ' op ' + (fresh[0].coin || short(fresh[0].mint)));
      if (soundOn) { beep(); }
    }
    seenPlays = ids;
    plays = { rows: list, rules: d.rules || plays.rules || [] };
    dirty.plays = true; dirty.table = true; schedule();
  }
  function beep() {
    try {
      var Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) { return; }
      if (!audio) { audio = new Ctx(); }
      if (audio.state === 'suspended' && audio.resume) { audio.resume(); }
      var t0 = audio.currentTime;
      [[880, 0], [1320, 0.16]].forEach(function (p) {
        var o = audio.createOscillator(), g = audio.createGain();
        o.type = 'sine'; o.frequency.value = p[0];
        g.gain.setValueAtTime(0.0001, t0 + p[1]); g.gain.exponentialRampToValueAtTime(0.25, t0 + p[1] + 0.02); g.gain.exponentialRampToValueAtTime(0.0001, t0 + p[1] + 0.15);
        o.connect(g); g.connect(audio.destination); o.start(t0 + p[1]); o.stop(t0 + p[1] + 0.16);
      });
    } catch (e) { /* no sound is fine */ }
  }

  /* ----- the feed ----------------------------------------------------------------------------------------------------------- */
  function applySnapshot(snap) {
    if (!snap || typeof snap !== 'object') { return; }
    if (snap.computing) { meta.note = snap.message || 'de live-bron wordt opgestart'; dirty.table = true; schedule(); return; }
    noteServerTime(snap.t || snap.generated_at);
    var at = meta.t || serverNow();
    rows = {}; rowCount = 0;
    var list = snap.rows || [];
    for (var i = 0; i < list.length; i++) { var r = list[i]; if (!r || !r.mint) { continue; } r._at = at; r._v = ++version; rows[r.mint] = r; rowCount++; }
    engineAge = isNum(snap.engine_age_s) ? snap.engine_age_s : null;
    if (snap.sol_usd && typeof snap.sol_usd === 'object') { solUsd = snap.sol_usd; }
    meta.note = snap.note || null;
    if (snap.plays_rules && !plays.rules.length) { plays.rules = snap.plays_rules; dirty.plays = true; }
    if (wantMint && rows[wantMint]) { select(wantMint, true); wantMint = null; }
    dirty.table = true; dirty.follow = true; dirty.meta = true; schedule();
  }
  function applyRows(d) {
    noteServerTime(d.t);
    var at = meta.t || serverNow();
    var upd = d.upd || [];
    for (var i = 0; i < upd.length; i++) { var r = upd[i]; if (!r || !r.mint) { continue; } if (!rows[r.mint]) { rowCount++; } r._at = at; r._v = ++version; rows[r.mint] = r; }
    var del = d.del || [];
    for (var j = 0; j < del.length; j++) { if (rows[del[j]]) { delete rows[del[j]]; rowCount--; } }
    if (isNum(d.n)) { rowCount = d.n; }
    if (wantMint && rows[wantMint]) { select(wantMint, true); wantMint = null; }
    dirty.table = true; dirty.follow = true; schedule();
  }
  function applyHeartbeat(d) {
    noteServerTime(d.t);
    engineAge = isNum(d.engine_age_s) ? d.engine_age_s : null;
    meta.viewers = d.viewers;
    schedule();
  }
  function parse(ev) { try { return JSON.parse(ev.data); } catch (e) { return null; } }
  function onEvent(name, ev) {
    if (ev.lastEventId) { var id = parseInt(ev.lastEventId, 10); if (isFinite(id)) { lastEventId = id; } }
    var d = parse(ev); if (!d) { return; }
    if (name === 'snapshot') { applySnapshot(d); }
    else if (name === 'rows') { applyRows(d); }
    else if (name === 'plays') { notePlays(d); }
    else if (name === 'heartbeat') { applyHeartbeat(d); }
  }
  function streamUrl(resume) {
    var q = [];
    if (follow.length) { q.push('follow=' + encodeURIComponent(follow.join(','))); }
    if (resume && lastEventId !== null) { q.push('last=' + encodeURIComponent(String(lastEventId))); }
    return q.length ? STREAM + '?' + q.join('&') : STREAM;
  }
  var reconnectTimer = null;
  function connect(resume) {
    if (es) { es.close(); es = null; }
    connected = false;
    if (!window.EventSource) { startPolling(); renderMeta(); return; }
    try { es = new EventSource(streamUrl(resume)); } catch (e) { es = null; startPolling(); renderMeta(); return; }
    es.addEventListener('open', function () { connected = true; stopPolling(); renderMeta(); });
    ['snapshot', 'rows', 'plays', 'heartbeat'].forEach(function (name) { es.addEventListener(name, function (ev) { onEvent(name, ev); }); });
    es.addEventListener('dropped', function () { if (es) { es.close(); es = null; } connected = false; lastEventId = null; renderMeta(); setTimeout(function () { connect(false); }, 1000); });
    es.addEventListener('error', function () {
      connected = false; renderMeta();
      if (es && es.readyState === 2) {   /* closed for good (a 503 or a network that is gone): poll, and try the stream again later */
        es = null; startPolling();
        if (!reconnectTimer) { reconnectTimer = setTimeout(function () { reconnectTimer = null; connect(true); }, 30000); }
      } else if (!pollTimer) { setTimeout(function () { if (!connected) { startPolling(); } }, 10000); }
    });
  }
  function reconnect(resume) { if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; } connect(resume); }
  function startPolling() {
    if (pollTimer) { return; }
    var poll = function () {
      if (document.hidden) { return; }
      fetch(SNAPSHOT, { credentials: 'same-origin', cache: 'no-store', headers: { 'Accept': 'application/json' } })
        .then(function (resp) { return resp.ok ? resp.json() : null; })
        .then(function (snap) { if (snap && !connected) { applySnapshot(snap); } })
        .catch(function () { /* the next poll tries again */ });
    };
    poll();
    pollTimer = setInterval(poll, POLL_MS);
    renderMeta();
  }
  function stopPolling() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) { hideTimer = setTimeout(function () { if (es) { es.close(); es = null; connected = false; } hideTimer = null; }, 90000); }
    else { if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; } if (!es) { connect(true); } }
  });

  /* ----- copying ------------------------------------------------------------------------------------------------------------- */
  function copyMint(mint) {
    var done = function () { say('mint gekopieerd: ' + short(mint)); };
    var fallback = function () {
      try {
        var ta = el('textarea'); ta.value = mint; ta.setAttribute('readonly', ''); ta.className = 'sr-only'; document.body.appendChild(ta); ta.select();
        var ok = document.execCommand && document.execCommand('copy'); document.body.removeChild(ta);
        if (ok) { done(); } else { say('kopiëren mislukt; selecteer het adres in de details'); }
      } catch (e) { say('kopiëren mislukt; selecteer het adres in de details'); }
    };
    if (navigator.clipboard && navigator.clipboard.writeText) { navigator.clipboard.writeText(mint).then(done, fallback); } else { fallback(); }
  }

  /* ----- keyboard ------------------------------------------------------------------------------------------------------------ */
  function visibleMints() { var out = []; for (var tr = tbody.firstChild; tr; tr = tr.nextSibling) { var m = tr.getAttribute && tr.getAttribute('data-mint'); if (m) { out.push(m); } } return out; }
  function move(step) {
    var list = visibleMints(); if (!list.length) { return; }
    var i = list.indexOf(selected);
    var j = i < 0 ? (step > 0 ? 0 : list.length - 1) : Math.max(0, Math.min(list.length - 1, i + step));
    select(list[j], !detail.hidden);
  }
  document.addEventListener('keydown', function (e) {
    var tag = (e.target && e.target.tagName || '').toLowerCase();
    var inField = tag === 'input' || tag === 'textarea' || tag === 'select';
    if (e.key === 'Escape') { if (inField) { e.target.blur(); } else if (!detail.hidden) { closeDetail(); } return; }
    if (inField || e.ctrlKey || e.metaKey || e.altKey) { return; }
    var r = selected ? (rows[selected] || detailRow) : null;
    switch (e.key) {
      case 'j': case 'ArrowDown': move(1); e.preventDefault(); break;
      case 'k': case 'ArrowUp': move(-1); e.preventDefault(); break;
      case 'Enter': if (selected) { if (e.shiftKey || detail.hidden) { openDetail(selected); } if (!e.shiftKey && LINKS.pumpfun) { openExternal(link(LINKS.pumpfun, selected)); } e.preventDefault(); } break;
      case 'c': if (selected) { copyMint(selected); } break;
      case 'f': if (selected) { toggleFollow(selected); } break;
      case '/': { var q = $('f-q'); if (q) { q.focus(); q.select(); e.preventDefault(); } break; }
      case 'p': { var pz = $('t-pause'); if (pz) { pz.checked = !pz.checked; setPaused(pz.checked); } break; }
      case '1': case '2': case '3': case '4': case '5': {
        var t = TERMINALS[parseInt(e.key, 10) - 1];
        if (selected && t) { openExternal(link(t.url, selected)); }
        break;
      }
      default: return;
    }
    if (r) { /* nothing else */ }
  });

  /* ----- toolbar wiring --------------------------------------------------------------------------------------------------------- */
  var chips = document.querySelectorAll('.t-presets .chip');
  for (var ci = 0; ci < chips.length; ci++) {
    chips[ci].addEventListener('click', function () {
      var id = this.getAttribute('data-preset');
      var p = presetById(id);
      setFilter(p ? JSON.parse(JSON.stringify(p.filter)) : {}, false);
    });
  }
  var form = $('t-filters');
  var formTimer = null;
  function onForm() { if (formTimer) { clearTimeout(formTimer); } formTimer = setTimeout(function () { setFilter(filterFromForm(), false); }, 150); }
  form.addEventListener('input', onForm);
  form.addEventListener('change', onForm);
  form.addEventListener('submit', function (e) { e.preventDefault(); setFilter(filterFromForm(), false); });
  $('f-save').addEventListener('click', function () { save(STORE.filters, filter); say(Object.keys(filter).length ? 'filter bewaard in deze browser' : 'leeg filter bewaard: de pagina opent voortaan zonder filter'); });
  $('f-share').addEventListener('click', function () {
    if (urlTimer) { clearTimeout(urlTimer); urlTimer = null; }
    writeUrl();
    setTimeout(function () {
      var url = window.location.href;
      var done = function () { say('link gekopieerd'); };
      if (navigator.clipboard && navigator.clipboard.writeText) { navigator.clipboard.writeText(url).then(done, function () { window.prompt('Kopieer deze link', url); }); } else { window.prompt('Kopieer deze link', url); }
    }, 350);
  });
  $('f-reset').addEventListener('click', function () { save(STORE.filters, null); setFilter({}, false); say('filters gewist'); });
  var boxes = document.querySelectorAll('.col-toggles input[data-col]');
  for (var bi = 0; bi < boxes.length; bi++) {
    boxes[bi].addEventListener('change', function () {
      colsOn = colsOn && typeof colsOn === 'object' ? colsOn : {};
      colsOn[this.getAttribute('data-col')] = this.checked;
      save(STORE.cols, colsOn); applyCols();
    });
  }
  $('t-density').addEventListener('change', function () { density = this.value; save(STORE.density, density); applyDensity(); });
  var soundBox = $('t-sound');
  soundBox.checked = soundOn;
  soundBox.addEventListener('change', function () { soundOn = this.checked; save(STORE.sound, soundOn); if (soundOn) { beep(); say('geluid aan bij een nieuwe play'); } });
  function setPaused(on) { paused = on; renderMeta(); if (!on) { dirty.table = dirty.follow = dirty.plays = true; schedule(); } }
  $('t-pause').addEventListener('change', function () { setPaused(this.checked); });
  var ths = table.querySelectorAll('th[data-sort] .sort');
  for (var ti = 0; ti < ths.length; ti++) { ths[ti].addEventListener('click', function () { setSort(this.parentNode.getAttribute('data-sort')); }); }

  /* ----- start ------------------------------------------------------------------------------------------------------------------- */
  var fromUrl = readUrl();
  if (fromUrl.sort) { sort = fromUrl.sort; }
  markSort();
  applyCols();
  applyDensity();
  if (fromUrl.mint) {
    wantMint = fromUrl.mint;
    if (!isFollowed(wantMint) && follow.length < FOLLOW_MAX) { follow.push(wantMint); save(STORE.follow, follow); }   /* an alert link: keep the coin live */
    detailMint = wantMint; detail.hidden = false;
  }
  setFilter(fromUrl.filter || load(STORE.filters, {}) || {}, true);
  renderMeta();
  connect(false);
  setInterval(tick, 1000);
})();
