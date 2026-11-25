// Operator page. Everything a seat wrote is untrusted and may have come from the web: it is only ever set with textContent.
'use strict';
(function () {
  var view = document.getElementById('view'), status = document.getElementById('status'), tokenInput = document.getElementById('token');
  var inFlight = false, writable = false, lastOk = null, timer = null, draft = {}, lastChains = null;

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

  function part(section, emptyText, build) {     // one section of a view: unavailable is said, and is different from empty
    if (section.state !== 'known') return note('Not available here: ' + String(section.reason || 'unknown').replace(/_/g, ' ') + '.');
    if (!section.items.length) return note(emptyText);
    var node = build(section.items);
    return section.limited ? add(el('div'), node, note('Older rows exist and are not shown.')) : node;
  }
  function percent(v) { return v === null || v === undefined ? value(null) : el('span', (Math.round(v * 10) / 10) + '%'); }
  function fixed(v, digits) { return typeof v === 'number' ? el('span', v.toFixed(digits)) : value(v); }
  function seatLink(id, role) { if (!role) return el('span', id || 'unknown', 'id'); var a = el('a', role); a.href = '#/seat/' + encodeURIComponent(id); return a; }

  function renderRequests(data) {
    var box = el('div'), now = data.as_of;
    if (data.state === 'unknown') return problem(data);
    add(box, el('h2', 'Asked by seats'), part(data.asks, 'No seat has asked for anything.', function (items) {
      return table(['When', 'Seat', 'Kind', 'Asks for', 'Why'], items.map(function (r) { return [timeCell(r.at, now), seatLink(r.actor_id, r.role), value(r.kind), el('p', r.text, 'text'), el('p', r.why === null ? '' : r.why, 'text')]; }));
    }));
    add(box, el('h2', 'Tickets to humans'), part(data.tickets, 'No tickets.', function (items) {
      return table(['State', 'Problem', 'Done when', 'Evidence', 'Steps', 'Filed'], items.map(function (r) {
        return [el('span', String(r.state || 'unknown').replace(/_/g, ' '), 'state'), el('p', r.problem, 'text'), el('p', r.done_when, 'text'), el('p', (r.evidence_refs || []).join('\n'), 'text'), num(value(r.steps)), timeCell(r.known_at, now)];
      }));
    }));
    add(box, note('A request is what a seat wrote; nothing is granted by showing it. Ticket states move only through the host.'));
    return box;
  }

  function renderBudget(data) {
    var box = el('div'), now = data.as_of;
    if (data.state === 'unknown') return problem(data);
    add(box, el('h2', 'Seats'), table(['Seat', 'Wakes, last hour', 'Wakes today', 'Tokens today', 'Largest wake today', 'Unmeasured wakes'], data.seats.map(function (s) {
      return [seatLink(s.actor_id, s.role), num(value(s.wakes_last_hour, s.usage_reason)), num(value(s.wakes_today, s.usage_reason)), num(value(s.tokens_today, s.usage_reason)), num(value(s.largest_wake_today, 'none measured')), num(value(s.unmeasured_wakes_today, s.usage_reason))];
    })));
    add(box, el('h2', 'Accounts'), part(data.accounts, 'No account reading has been recorded.', function (items) {
      var rows = [];
      items.forEach(function (a) {
        if (!a.windows.length) rows.push([a.account, value(null, a.reading_reason || 'no window read'), '', '', add(el('span'), timeCell(a.observed_at, now), document.createTextNode(' (' + ago(a.observed_at, now) + ')'))]);
        a.windows.forEach(function (w) { rows.push([a.account, w.name.replace(/_/g, ' '), num(percent(w.used_percent)), add(el('span'), timeCell(w.resets_at, now), document.createTextNode(' (' + until(w.resets_at, now) + ')')), add(el('span'), timeCell(a.observed_at, now), document.createTextNode(' (' + ago(a.observed_at, now) + ')'))]); });
      });
      return table(['Account', 'Window', 'Used', 'Resets', 'Measured'], rows);
    }));
    add(box, el('h2', 'Parks cleared'), part(data.clears, 'None.', function (items) {
      return table(['When', 'Seat', 'By', 'Reason'], items.map(function (c) { return [timeCell(c.at, now), seatLink(c.actor_id, c.role), c.by, el('p', c.reason, 'text')]; }));
    }));
    add(box, note('Tokens are a total only when every wake today was measured. Today is the UTC day. An account reading is a measurement at the time shown, not a live value.'));
    return box;
  }

  function renderEvaluation(data) {
    var box = el('div'), now = data.as_of;
    if (data.state === 'unknown') return problem(data);
    add(box, el('h2', 'Released verdicts'), part(data.verdicts, 'No verdict has been released.', function (items) {
      var wrap = el('div');
      items.forEach(function (v) {
        var cards = el('dl', null, 'cards');
        [['Verdict', String(v.state).replace(/_/g, ' ')], ['Hypothesis', v.hypothesis_id], ['Attempt', v.k === null ? null : v.k + ' at significance ' + v.alpha_k],
         ['Lift, lower bound', v.lift_lower === null || v.lift_lower === undefined ? null : v.lift_lower.toFixed(3) + ' ' + v.unit + ' (needs above ' + v.delta0 + ')'],
         ['Net, lower bound', v.net_lower === null || v.net_lower === undefined ? null : v.net_lower.toFixed(3) + ' ' + v.unit], ['Cases', v.cases === null ? null : v.graded + ' graded of ' + v.cases + ', ' + v.censored + ' censored'],
         ['Clusters', v.clusters], ['Config custody', v.custody], ['Released', when(v.released_at)]].forEach(function (pair) { add(cards, add(el('div', null, 'card'), el('dt', pair[0]), add(el('dd'), value(pair[1])))); });
        add(wrap, cards);
        if (v.reasons && v.reasons.length) add(wrap, note('Reasons: ' + v.reasons.join(', ').replace(/_/g, ' ') + '.'));
      });
      return wrap;
    }));
    add(box, el('h2', 'Trial registry'), part(data.trials, 'No evaluated epoch has been opened.', function (items) {
      return table(['Row', 'Attempt', 'Significance', 'Hypothesis', 'Status', 'Epoch'], items.map(function (r) { return [num(value(r.seq)), num(value(r.k)), num(value(r.alpha_k)), value(r.hypothesis_id), el('span', String(r.status || 'unknown').replace(/_/g, ' ').toLowerCase(), 'state'), value(r.epoch_id)]; }));
    }));
    add(box, el('h2', 'Epochs'), part(data.epochs, 'No epoch recorded.', function (items) {
      return table(['Epoch', 'Kind', 'Opened', 'Closed', 'Status'], items.map(function (e) { return [value(e.epoch_id), value(e.kind), timeCell(e.opened_at, now), e.closed_at === null ? 'open' : timeCell(e.closed_at, now), value(e.status || (e.closed_at === null ? 'open' : 'closed'))]; }));
    }));
    add(box, note('Only released aggregates are shown. Per-case results stay in the evaluation file, which this page never opens. A pass files a ticket for a human and changes nothing by itself.'));
    return box;
  }

  function renderHealth(data, verified) {
    var box = el('div'), now = data.as_of;
    if (data.state === 'unknown') return problem(data);
    var cards = el('dl', null, 'cards'), lease = data.lease, beat = data.heartbeat;
    [['Supervisor lease', lease === null ? null : lease.held ? 'held by ' + lease.holder : 'not held'], ['Lease expires', lease && lease.expires_at ? when(lease.expires_at) + ' (' + until(lease.expires_at, now) + ')' : null],
     ['Last heartbeat', beat && beat.at ? when(beat.at) + ' (' + ago(beat.at, now) + ')' : null], ['Progress', beat ? beat.progress : null]].forEach(function (pair) {
      add(cards, add(el('div', null, 'card'), el('dt', pair[0]), add(el('dd'), value(pair[1], lease === null && beat === null ? 'no supervisor in this store' : 'not recorded'))));
    });
    add(box, el('h2', 'Supervisor'), cards);
    add(box, el('h2', 'Turns'), part(data.turns, 'No turns recorded.', function (items) { return table(['State', 'Count (last 500)'], items.map(function (r) { return [r.state, num(value(r.count))]; })); }));
    add(box, el('h2', 'Record chains'));
    var button = el('button', verified ? 'Check again' : 'Check the append-only tables'); button.type = 'button';
    button.addEventListener('click', function () { verifyOnce = true; load(); });
    add(box, button);
    if (data.chains.state === 'known') add(box, table(['Table', 'Rows', 'Result'], data.chains.items.map(function (c) { return [c.table, num(value(c.rows, 'too many to count here')), el('span', c.result.replace(/_/g, ' '), 'state ' + (c.result === 'intact' ? 'awake' : 'parked'))]; })));
    add(box, note('The check re-hashes every row, so it runs only when asked. It proves no row was edited or removed from the middle. It cannot see a removed tail: that needs the last hash kept somewhere outside this database.'));
    return box;
  }

  function chart(points, unit) {
    var canvas = document.createElement('canvas'), css = getComputedStyle(document.documentElement);
    // Drawn at the width it is shown at, so the labels stay 13 px on a phone instead of shrinking with the picture.
    var cw = Math.max(260, Math.min(900, view.clientWidth || 900)), ch = 220, dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(cw * dpr); canvas.height = Math.round(ch * dpr); canvas.style.width = cw + 'px'; canvas.style.height = ch + 'px'; canvas.setAttribute('role', 'img');
    var xs = points.map(function (p) { return p[0]; }), ys = points.map(function (p) { return p[1]; });
    var x0 = Math.min.apply(null, xs), x1 = Math.max.apply(null, xs), y0 = Math.min.apply(null, ys), y1 = Math.max.apply(null, ys);
    canvas.setAttribute('aria-label', points.length + ' points from ' + when(x0) + ' to ' + when(x1) + ', lowest ' + y0.toFixed(2) + ', highest ' + y1.toFixed(2) + (unit ? ' ' + unit : ''));
    if (y1 === y0) { y1 += 1; y0 -= 1; } if (x1 === x0) x1 += 1;
    var g = canvas.getContext('2d'), L = 52, R = 10, T = 10, B = 26, W = cw - L - R, H = ch - T - B;
    g.scale(dpr, dpr);
    g.font = '13px system-ui, sans-serif'; g.fillStyle = css.getPropertyValue('--dim'); g.strokeStyle = css.getPropertyValue('--line'); g.lineWidth = 1;
    [0, .5, 1].forEach(function (f) { var y = T + H - f * H; g.beginPath(); g.moveTo(L, y); g.lineTo(L + W, y); g.stroke(); g.fillText((y0 + f * (y1 - y0)).toFixed(2), 4, y + 4); });
    g.fillText(when(x0).slice(5, 16), L, ch - 6); var end = when(x1).slice(5, 16); g.fillText(end, L + W - g.measureText(end).width, ch - 6);
    g.strokeStyle = css.getPropertyValue('--accent'); g.lineWidth = 2; g.beginPath();
    points.forEach(function (p, i) { var x = L + (p[0] - x0) / (x1 - x0) * W, y = T + H - (p[1] - y0) / (y1 - y0) * H; if (i) g.lineTo(x, y); else g.moveTo(x, y); });
    g.stroke();
    return canvas;
  }
  function renderPanel(data) {
    var box = el('div'), now = data.as_of_page;
    add(box, el('h2', data.title));
    if (data.state !== 'known') return add(box, note('This panel is unavailable: ' + String(data.reason).replace(/_/g, ' ') + '. The rest of the page is unaffected.'));
    if (data.shape === 'table') add(box, data.rows.length ? table(data.columns, data.rows.map(function (r) { return r.map(function (c) { return typeof c === 'number' ? num(value(c)) : value(c === true ? 'yes' : c === false ? 'no' : c); }); })) : note('No rows.'));
    if (data.shape === 'facts') { var cards = el('dl', null, 'cards'); data.items.forEach(function (i) { add(cards, add(el('div', null, 'card'), el('dt', i.label), add(el('dd'), value(i.value === null ? null : (typeof i.value === 'number' ? i.value.toLocaleString('en-US') : i.value) + (i.unit ? ' ' + i.unit : ''))), i.as_of ? el('dt', 'as of ' + when(i.as_of)) : null)); }); add(box, cards); }
    if (data.shape === 'series') add(box, data.points.length > 1 ? chart(data.points, data.unit) : note('Fewer than two points.'), data.points.length > 1 ? note(data.points.length + ' points' + (data.unit ? ', ' + data.unit : '') + (data.dropped ? '; ' + data.dropped + ' unreadable points left out' : '') + '.') : null);
    add(box, note((data.note ? data.note + ' ' : '') + 'Supplied by the project' + (data.as_of ? ', as of ' + when(data.as_of) : '') + (data.limited ? '. Cut to the newest rows' : '') + '.'));
    return box;
  }

  var VIEWS = { mail: ['/api/mail', renderMail], requests: ['/api/requests', renderRequests], budget: ['/api/budget', renderBudget], evaluation: ['/api/evaluation', renderEvaluation], overview: ['/api/overview', renderOverview] };
  var verifyOnce = false, panelsKey = '';
  function route() {
    var hash = location.hash.replace(/^#\/?/, ''), parts = hash.split('/');
    if (parts[0] === 'seat' && parts[1]) return { name: 'seat', actor: decodeURIComponent(parts[1]) };
    if (parts[0] === 'panel' && parts[1]) return { name: 'panel', panel: decodeURIComponent(parts[1]) };
    if (parts[0] === 'health') return { name: 'health' };
    return { name: VIEWS[parts[0]] ? parts[0] : 'overview' };
  }
  function panelLinks(panels) {
    var key = JSON.stringify(panels || []);
    if (key === panelsKey) return;
    panelsKey = key;
    var holder = document.getElementById('panel-links'); holder.replaceChildren();
    (panels || []).forEach(function (p) { var a = el('a', p.title); a.href = '#/panel/' + encodeURIComponent(p.name); a.dataset.view = 'panel:' + p.name; holder.appendChild(a); });
  }
  function load(focus) {
    if (inFlight) return;
    var r = route(), verified = verifyOnce; verifyOnce = false;
    var path = r.name === 'seat' ? '/api/seat?actor=' + encodeURIComponent(r.actor) : r.name === 'panel' ? '/api/panel?name=' + encodeURIComponent(r.panel) : r.name === 'health' ? '/api/health' + (verified ? '?verify=1' : '') : VIEWS[r.name][0];
    var current = r.name === 'panel' ? 'panel:' + r.panel : r.name === 'seat' ? 'overview' : r.name;
    inFlight = true;
    function mark() { Array.prototype.forEach.call(document.querySelectorAll('#nav a'), function (a) { if (a.dataset.view === current) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current'); }); }
    mark();
    var active = document.activeElement, typing = !focus && active && view.contains(active) && /^(TEXTAREA|INPUT|SELECT)$/.test(active.tagName);
    api(path).then(function (res) {
      inFlight = false;
      var data = res.body;
      if (typeof data.writable === 'boolean') writable = data.writable;
      if (data.panels) { panelLinks(data.panels); mark(); }
      if (r.name === 'health' && !verified && view.querySelector('table') && data.chains && data.chains.state !== 'known' && lastChains) data.chains = lastChains;   // keep the last check on screen between polls
      if (r.name === 'health' && data.chains && data.chains.state === 'known') lastChains = data.chains;
      if (typing) { status.textContent = 'Paused while you type. Last read ' + (lastOk ? when(lastOk) : 'never'); return; }   // never rebuild a form under the cursor
      var node = !res.ok ? problem({ reason: data.error }) : r.name === 'seat' ? renderSeat(data, r.actor) : r.name === 'panel' ? renderPanel(data) : r.name === 'health' ? renderHealth(data, !!lastChains) : VIEWS[r.name][1](data);
      view.replaceChildren(node);
      lastOk = data.as_of || data.as_of_page || lastOk;
      var asOf = data.as_of || data.as_of_page;
      status.textContent = (data.state === 'known' ? 'As of ' : data.state === 'partial' ? 'Partial, as of ' : 'Unavailable, as of ') + when(asOf);
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
  window.addEventListener('hashchange', function () { draft = {}; lastChains = null; load(true); });
  document.addEventListener('visibilitychange', function () { if (!document.hidden) load(); });
  document.getElementById('refresh').addEventListener('click', function () { if (document.activeElement) document.activeElement.blur(); load(); });
  load(); schedule();
})();
