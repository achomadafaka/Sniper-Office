// Operator page. Everything a seat wrote is untrusted and may have come from the web: it is only ever set with textContent.
'use strict';
(function () {
  var view = document.getElementById('view'), status = document.getElementById('status'), tokenInput = document.getElementById('token');
  var inFlight = false, writable = false, lastOk = null, timer = null, draft = {};

  function el(tag, text, cls) {
    var node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = String(text);
    if (cls) node.className = cls;
    return node;
  }
  function add(parent) { for (var i = 1; i < arguments.length; i++) if (arguments[i]) parent.appendChild(arguments[i]); return parent; }
  function when(at) {
    if (typeof at !== 'number' || !isFinite(at)) return 'time unknown';
    var d = new Date(at * 1000);
    return d.toISOString().slice(0, 19).replace('T', ' ') + 'Z';
  }
  function ago(at, now) {
    if (typeof at !== 'number' || typeof now !== 'number') return '';
    var s = Math.max(0, Math.round(now - at));
    return s < 90 ? s + ' s ago' : s < 5400 ? Math.round(s / 60) + ' min ago' : s < 172800 ? Math.round(s / 3600) + ' h ago' : Math.round(s / 86400) + ' d ago';
  }
  function until(at, now) {
    if (typeof at !== 'number' || typeof now !== 'number') return '';
    var s = Math.round(at - now);
    return s <= 0 ? 'due now' : s < 90 ? 'in ' + s + ' s' : s < 5400 ? 'in ' + Math.round(s / 60) + ' min' : 'in ' + Math.round(s / 3600) + ' h';
  }
  function value(v, reason) {            // unknown is said, never drawn as zero or blank
    if (v === null || v === undefined) return el('span', 'unknown' + (reason ? ' (' + reason.replace(/_/g, ' ') + ')' : ''), 'unknown-value');
    return el('span', typeof v === 'number' ? v.toLocaleString('en-US') : v);
  }
  function timeCell(at, now) { var t = el('time', when(at)); if (typeof at === 'number') t.dateTime = new Date(at * 1000).toISOString(); t.title = ago(at, now); return t; }
  function table(headers, rows) {
    var wrap = el('div', null, 'wrap'), t = el('table'), head = el('tr');
    headers.forEach(function (h) { var th = el('th', h); th.scope = 'col'; head.appendChild(th); });
    add(t, add(el('thead'), head));
    var body = el('tbody');
    rows.forEach(function (cells) { var tr = el('tr'); cells.forEach(function (c) { var td = el('td'); if (c && c.num) { td.className = 'num'; c = c.node; } add(td, typeof c === 'string' ? document.createTextNode(c) : c); tr.appendChild(td); }); body.appendChild(tr); });
    return add(wrap, add(t, body));
  }
  function num(node) { return { num: true, node: node }; }
  function describe(seat, now) {
    if (seat.status === 'parked') return 'parked: ' + (seat.reason || 'reason unknown').replace(/_/g, ' ');
    if (seat.status === 'awake') return 'awake (' + (seat.reason || 'wake').replace(/_/g, ' ') + ')';
    if (seat.status === 'asleep') return 'asleep' + (seat.sleep_until ? ', next wake ' + until(seat.sleep_until, now) : '');
    return 'no signal';
  }

  function api(path, options) {
    return fetch(path, options || { cache: 'no-store' }).then(function (r) { return r.json().then(function (body) { return { ok: r.ok, status: r.status, body: body }; }); });
  }
  function post(path, body) {
    return api(path, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Office-Token': tokenInput.value }, body: JSON.stringify(body) });
  }
  function note(text) { return el('p', text, 'note'); }
  function problem(data) {
    var box = el('div');
    add(box, el('h2', 'Cannot read the store'), note('Reason: ' + String(data.reason || 'unknown').replace(/_/g, ' ') + '. Nothing is shown rather than an empty office.'));
    return box;
  }

  function renderOverview(data) {
    var box = el('div'), now = data.as_of;
    if (data.state === 'unknown') return problem(data);
    if (data.alerts.length) {
      var list = el('ul', null, 'alerts');
      data.alerts.forEach(function (a) {
        var li = el('li', null, a.kind === 'parked' ? 'bad' : ''), link = el('a', a.role); link.href = '#/seat/' + encodeURIComponent(a.actor_id);
        add(li, link, document.createTextNode(a.kind === 'parked' ? ' is parked: ' + a.text.replace(/_/g, ' ') + (a.at ? ' (' + ago(a.at, now) + ')' : '') : ': ' + a.text));
        list.appendChild(li);
      });
      add(box, el('h2', 'Needs you'), list);
    }
    add(box, el('h2', 'Seats'));
    var rows = data.seats.map(function (s) {
      var link = el('a', s.role); link.href = '#/seat/' + encodeURIComponent(s.actor_id);
      return [link, el('span', describe(s, now), 'state ' + s.status), s.last_wake ? add(el('span'), timeCell(s.last_wake.ended_at, now), document.createTextNode(' ' + (s.last_wake.outcome || 'outcome unknown'))) : value(null, 'none recorded'),
        num(value(s.wakes_today, s.usage_reason)), num(value(s.tokens_today, s.usage_reason)), num(value(s.unread_mail)), num(value(s.requests))];
    });
    add(box, table(['Seat', 'State', 'Last wake ended', 'Wakes today', 'Tokens today', 'Unread mail', 'Requests'], rows));
    add(box, note('State is what the host last recorded for each seat, not a live process check. Today is the UTC day. ' + (data.limited ? 'More than 64 seats exist; only 64 are shown. ' : '') +
      'Messages addressed to you: ' + data.operator_inbox + '.' + (data.quota === 'unavailable' ? ' Usage database not available, so wakes and tokens are unknown.' : '')));
    return box;
  }

  function actionForm(title, fields, button, send) {
    var form = el('form', null, 'action'), result = el('p', '', 'result'); result.setAttribute('role', 'status');
    add(form, el('h2', title));
    fields.forEach(function (f) { var label = el('label', f.label); label.htmlFor = f.node.id; add(form, label, f.node); });
    var submit = el('button', button); submit.type = 'submit';
    add(form, add(el('div', null, 'row'), submit, result));
    if (!writable) { submit.disabled = true; result.textContent = 'This page is read-only: the server was started without a token file.'; }
    form.addEventListener('submit', function (ev) {
      ev.preventDefault();
      if (!tokenInput.value) { result.className = 'result bad'; result.textContent = 'Enter the token under Actions first.'; document.getElementById('access').open = true; tokenInput.focus(); return; }
      submit.disabled = true; result.className = 'result'; result.textContent = 'Sending';
      send().then(function (r) {
        submit.disabled = false;
        if (r.ok) { result.className = 'result ok'; result.textContent = r.body.state === 'duplicate' ? 'Already sent.' : 'Done.'; draft = {}; load(); }
        else { result.className = 'result bad'; result.textContent = 'Refused: ' + String(r.body.reason || r.body.error || r.status).replace(/_/g, ' ') + '.'; }
      }).catch(function () { submit.disabled = false; result.className = 'result bad'; result.textContent = 'Could not reach the server. Nothing was sent.'; });
    });
    return form;
  }
  function field(tag, id, key) {
    var node = el(tag); node.id = id; node.required = true; node.value = draft[key] || '';
    node.addEventListener('input', function () { draft[key] = node.value; });        // a refresh must not eat what you are typing
    return node;
  }
  function clientId() { var a = new Uint8Array(12); crypto.getRandomValues(a); return 'ui-' + Array.prototype.map.call(a, function (b) { return ('0' + b.toString(16)).slice(-2); }).join(''); }

  function renderSeat(data, actor) {
    var box = el('div'), now = data.as_of, s = data.seat;
    if (data.state === 'unknown') return data.reason === 'unknown_seat' ? add(box, el('h2', 'No such seat'), note('It is not on the roster, or it has been sealed.')) : problem(data);
    add(box, el('h2', s.role));
    var cards = el('dl', null, 'cards');
    [['State', describe(s, now)], ['Since', s.since ? when(s.since) + ' (' + ago(s.since, now) + ')' : null], ['Wakes today', s.wakes_today], ['Tokens today', s.tokens_today],
     ['Epoch', s.epoch_id], ['Session', 'generation ' + s.generation + ', incarnation ' + s.incarnation]].forEach(function (pair) {
      add(cards, add(el('div', null, 'card'), el('dt', pair[0]), add(el('dd'), value(pair[1], pair[0] === 'Tokens today' || pair[0] === 'Wakes today' ? s.usage_reason : null))));
    });
    add(box, cards, el('p', s.actor_id, 'id'));
    if (s.status === 'parked') {
      var reason = field('input', 'park-reason', 'park');
      add(box, actionForm('Clear this park', [{ label: 'Why it is safe to clear (logged with your action)', node: reason }], 'Clear park', function () { return post('/api/clear-park', { actor: actor, reason: reason.value }); }),
        note('The seat host checks again on its next tick. If the cause is still true, the seat parks again.'));
    }
    var text = field('textarea', 'mail-text', 'mail:' + actor), id = clientId();
    add(box, actionForm('Write to this seat', [{ label: 'Message (the seat reads it on its next wake)', node: text }], 'Send', function () { return post('/api/mail', { to: actor, client_id: id, text: text.value }); }));
    add(box, el('h2', 'Timeline'));
    var list = el('ol', null, 'timeline');
    data.events.forEach(function (e) {
      var li = el('li', null, e.earlier_identity ? 'earlier' : ''), body = el('div'), tags = el('div', null, 'tags');
      add(body, el('p', e.text === null ? '(no text recorded)' : e.text, 'text'));
      if (e.web_tainted) add(tags, el('span', 'from the web', 'tag web'));
      if (e.truncated) add(tags, el('span', 'cut at 4 KB', 'tag'));
      if (e.state !== 'known') add(tags, el('span', e.state, 'tag'));
      if (e.earlier_identity) add(tags, el('span', 'earlier session', 'tag'));
      Object.keys(e.refs).slice(0, 6).forEach(function (k) { var v = e.refs[k]; if (typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') add(tags, el('span', k + ': ' + String(v).slice(0, 80), 'tag')); });
      if (tags.childNodes.length) add(body, tags);
      add(list, add(li, timeCell(e.at, now), el('span', e.kind.replace(/_/g, ' '), 'kind'), body));
    });
    add(box, data.events.length ? list : note('Nothing recorded for this seat yet.'));
    add(box, note('Newest first, last ' + data.events.length + ' events.' + (data.limited ? ' Older events exist and are not shown.' : '')));
    if (data.mail.length) add(box, el('h2', 'Mail'), mailTable(data.mail, now, null));
    if (data.wakes.length) add(box, el('h2', 'Recent wakes'), table(['Ended', 'Outcome', 'Tokens', 'Model'], data.wakes.map(function (w) { return [timeCell(w.ended_at, now), value(w.outcome), num(value(w.used, 'not measured')), value(w.model)]; })));
    return box;
  }

  function mailTable(items, now, roles) {     // a list, not a table: a message needs the width, also on a phone
    var list = el('ol', null, 'timeline mail');
    items.forEach(function (m) {
      var who = function (id, role) { return role || (id === 'operator' ? 'operator' : roles === null ? id : 'not on roster'); };
      var body = el('div'), tags = el('div', null, 'tags');
      add(body, el('p', m.text, 'text'));
      if (m.unread) add(body, add(tags, el('span', 'unread', 'tag')));
      add(list, add(el('li'), timeCell(m.at, now), el('span', who(m.sender, m.sender_role) + ' to ' + who(m.to, m.to_role), 'kind'), body));
    });
    return list;
  }
  function renderMail(data) {
    var box = el('div'), now = data.as_of;
    if (data.state === 'unknown') return problem(data);
    var to = field('select', 'mail-to', 'mailto'), text = field('textarea', 'mail-all-text', 'mailall'), id = clientId();
    data.seats.forEach(function (s) { var o = el('option', s.role); o.value = s.actor_id; to.appendChild(o); });
    if (draft.mailto) to.value = draft.mailto;
    add(box, actionForm('Write to a seat', [{ label: 'Seat', node: to }, { label: 'Message', node: text }], 'Send', function () { return post('/api/mail', { to: to.value, client_id: id, text: text.value }); }));
    add(box, el('h2', 'Messages'), data.items.length ? mailTable(data.items, now, {}) : note('No mail yet.'));
    add(box, note('Newest first, last ' + data.items.length + ' messages.' + (data.limited ? ' Older messages exist and are not shown.' : '') + ' Sender names are assigned by the host, never taken from the message.'));
    return box;
  }

  function route() {
    var hash = location.hash.replace(/^#\/?/, ''), parts = hash.split('/');
    if (parts[0] === 'seat' && parts[1]) return { name: 'seat', actor: decodeURIComponent(parts[1]) };
    return { name: parts[0] === 'mail' ? 'mail' : 'overview' };
  }
  function load(focus) {
    if (inFlight) return;
    var r = route(), path = r.name === 'seat' ? '/api/seat?actor=' + encodeURIComponent(r.actor) : r.name === 'mail' ? '/api/mail' : '/api/overview';
    inFlight = true;
    document.getElementById('nav-overview').toggleAttribute('aria-current', false); document.getElementById('nav-mail').toggleAttribute('aria-current', false);
    document.getElementById(r.name === 'mail' ? 'nav-mail' : 'nav-overview').setAttribute('aria-current', 'page');
    var active = document.activeElement, typing = !focus && active && view.contains(active) && /^(TEXTAREA|INPUT|SELECT)$/.test(active.tagName);
    api(path).then(function (res) {
      inFlight = false;
      var data = res.body;
      if (typeof data.writable === 'boolean') writable = data.writable;
      if (typing) { status.textContent = 'Paused while you type. Last read ' + (lastOk ? when(lastOk) : 'never'); return; }   // never rebuild a form under the cursor
      var node = !res.ok ? problem({ reason: data.error }) : r.name === 'seat' ? renderSeat(data, r.actor) : r.name === 'mail' ? renderMail(data) : renderOverview(data);
      view.replaceChildren(node);
      lastOk = data.as_of || lastOk;
      status.textContent = (data.state === 'known' ? 'As of ' : data.state === 'partial' ? 'Partial, as of ' : 'Unknown, as of ') + when(data.as_of);
      document.getElementById('access-note').textContent = writable ? 'Sending mail and clearing a park need the token from the server\'s token file. It is kept in this tab only.' : 'Read-only: the server was started without a token file.';
      tokenInput.disabled = !writable;
      if (focus) view.focus();
    }).catch(function () {
      inFlight = false;
      status.textContent = 'Server not reachable. Showing the last read' + (lastOk ? ' from ' + when(lastOk) : '') + '.';
    });
  }
  function schedule() {
    clearInterval(timer);
    timer = setInterval(function () { if (!document.hidden) load(); }, 5000);
  }
  window.addEventListener('hashchange', function () { draft = {}; load(true); });
  document.addEventListener('visibilitychange', function () { if (!document.hidden) load(); });
  document.getElementById('refresh').addEventListener('click', function () { if (document.activeElement) document.activeElement.blur(); load(); });
  load(); schedule();
})();
