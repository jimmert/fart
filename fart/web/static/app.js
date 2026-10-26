/* fart website (milestone 8): refresh the JSON endpoints every 60 s and fill the page.
   Every element with data-src (the endpoint) and data-path (a dotted path into its JSON) gets the value as text;
   an element with data-kind="rows" is a table body that is rebuilt from the list at the path with the columns in
   data-cols. The server renders the same values on the first load (fart/web/pages.py); this file only repeats that.
   No external code, no evaluation of strings, no HTML built from data: everything goes through textContent. */
(function () {
  'use strict';
  var API = { status: '/api/status', coverage: '/api/coverage', decisions: '/api/decisions', x: '/api/x',
              report: '/api/report/latest', week: '/api/week/latest' };
  var INTERVAL_MS = 60000;

  function get(obj, path) {
    if (!path) { return obj; }
    var parts = path.split('.');
    var cur = obj;
    for (var i = 0; i < parts.length; i++) {
      if (cur === null || cur === undefined) { return undefined; }
      cur = cur[parts[i]];
    }
    return cur;
  }

  function fmt(v) {
    if (v === null || v === undefined || v === '') { return '–'; }
    if (v === true) { return 'ja'; }
    if (v === false) { return 'nee'; }
    if (Array.isArray(v)) { return v.join(', '); }
    if (typeof v === 'object') { return JSON.stringify(v); }
    return String(v);
  }

  function renderRows(tbody, rows) {
    var cols;
    try { cols = JSON.parse(tbody.getAttribute('data-cols') || '[]'); } catch (e) { return; }
    var frag = document.createDocumentFragment();
    if (!Array.isArray(rows) || rows.length === 0) {
      var tr0 = document.createElement('tr');
      var td0 = document.createElement('td');
      td0.colSpan = cols.length || 1;
      td0.className = 'muted';
      td0.textContent = tbody.getAttribute('data-empty') || 'geen';
      tr0.appendChild(td0);
      frag.appendChild(tr0);
    } else {
      rows.forEach(function (r) {
        var tr = document.createElement('tr');
        if (r && r._class) { tr.className = r._class; }
        cols.forEach(function (c) {
          var td = document.createElement('td');
          var key = Array.isArray(c) ? c[0] : c;
          var cls = Array.isArray(c) ? (c[2] || '') : '';
          if (cls) { td.className = cls; }
          td.textContent = fmt(get(r, key));
          tr.appendChild(td);
        });
        frag.appendChild(tr);
      });
    }
    while (tbody.firstChild) { tbody.removeChild(tbody.firstChild); }
    tbody.appendChild(frag);
  }

  function fill(src, data) {
    var els = document.querySelectorAll('[data-src="' + src + '"]');
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      var v = get(data, el.getAttribute('data-path'));
      if (el.getAttribute('data-kind') === 'rows') { renderRows(el, v); } else { el.textContent = fmt(v); }
    }
  }

  function setState(text, bad) {
    var el = document.getElementById('refresh-state');
    if (!el) { return; }
    el.textContent = text;
    el.className = bad ? 'error' : '';
  }

  function refresh() {
    var srcs = {};
    var els = document.querySelectorAll('[data-src]');
    for (var i = 0; i < els.length; i++) { srcs[els[i].getAttribute('data-src')] = true; }
    var names = Object.keys(srcs).filter(function (s) { return API[s]; });
    if (names.length === 0) { return; }
    var failed = [];
    var done = 0;
    names.forEach(function (name) {
      fetch(API[name], { credentials: 'same-origin', cache: 'no-store', headers: { 'Accept': 'application/json' } })
        .then(function (resp) {
          if (resp.status === 401) { window.location.reload(); return null; }
          if (!resp.ok) { throw new Error('HTTP ' + resp.status); }
          return resp.json();
        })
        .then(function (data) { if (data) { fill(name, data); } })
        .catch(function () { failed.push(name); })
        .then(function () {
          done += 1;
          if (done === names.length) {
            if (failed.length) { setState('Verversen mislukt voor: ' + failed.join(', ') + ' (laatste poging ' + new Date().toLocaleTimeString() + ')', true); }
            else { setState('Ververst ' + new Date().toLocaleTimeString() + ' (elke 60 s)', false); }
          }
        });
    });
  }

  if (document.querySelector('[data-src]')) {
    setState('Ververst elke 60 s', false);
    window.setInterval(refresh, INTERVAL_MS);
  }
})();
