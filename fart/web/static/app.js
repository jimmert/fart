/* KingDegen radar (milestone 9): keep the page current without a second renderer.
   Every REFRESH_MS the script fetches the page it is on (the server renders it from a 30 s cache), parses the answer
   in an inert document and swaps every element that carries data-live="<name>" for its fresh twin. The server stays
   the only renderer: no HTML is built from data here, nothing is evaluated, nothing external is loaded, and the inert
   document never runs a script. A hidden tab does not refresh; it refreshes once when it becomes visible again. */
(function () {
  'use strict';
  var body = document.body;
  var REFRESH_MS = parseInt(body.getAttribute('data-refresh-ms') || '30000', 10);
  if (!(REFRESH_MS >= 5000)) { REFRESH_MS = 30000; }
  var state = document.getElementById('refresh-state');
  var busy = false;

  function clock() {
    var d = new Date();
    var h = d.getHours(), m = d.getMinutes(), s = d.getSeconds();
    return (h < 10 ? '0' : '') + h + ':' + (m < 10 ? '0' : '') + m + ':' + (s < 10 ? '0' : '') + s;
  }

  function setState(text, bad) {
    state = document.getElementById('refresh-state');
    if (!state) { return; }
    state.textContent = text;
    state.className = bad ? 'is-bad' : '';
  }

  function swap(doc) {
    var fresh = doc.querySelectorAll('[data-live]');
    var n = 0;
    for (var i = 0; i < fresh.length; i++) {
      var name = fresh[i].getAttribute('data-live');
      var cur = document.querySelector('[data-live="' + name.replace(/"/g, '') + '"]');
      if (!cur) { continue; }
      if (cur.contains(document.activeElement)) { continue; }   /* never pull the keyboard focus away */
      cur.replaceWith(document.importNode(fresh[i], true));
      n += 1;
    }
    return n;
  }

  function refresh() {
    if (busy || document.hidden) { return; }
    busy = true;
    var url = window.location.pathname + window.location.search;
    fetch(url, { credentials: 'same-origin', cache: 'no-store', headers: { 'Accept': 'text/html' } })
      .then(function (resp) {
        if (resp.status === 401) { window.location.reload(); return null; }
        if (!resp.ok) { throw new Error('HTTP ' + resp.status); }
        return resp.text();
      })
      .then(function (html) {
        if (html === null) { return; }
        var doc = new DOMParser().parseFromString(html, 'text/html');
        var n = swap(doc);
        setState('Bijgewerkt ' + clock() + (n ? '' : ' (niets te verversen)') + ' · ververst elke ' + Math.round(REFRESH_MS / 1000) + ' s', false);
      })
      .catch(function () {
        setState('Verversen mislukt om ' + clock() + '; de cijfers kunnen verouderd zijn. Nieuwe poging over ' + Math.round(REFRESH_MS / 1000) + ' s.', true);
      })
      .then(function () { busy = false; });
  }

  if (document.querySelector('[data-live]') && window.fetch && window.DOMParser) {
    window.setInterval(refresh, REFRESH_MS);
    document.addEventListener('visibilitychange', function () { if (!document.hidden) { refresh(); } });
  }
})();
