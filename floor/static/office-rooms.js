(() => {
  'use strict';
  // The six rooms of this office, drawn from /api/office/newsroom. A board reads only what the store recorded; what it does not hold says so.
  const make = (tag, text, cls) => {
    const node = document.createElement(tag);
    if (text != null) node.textContent = text;
    if (cls) node.className = cls;
    return node;
  };
  const finite = v => typeof v === 'number' && Number.isFinite(v);
  const line = (...parts) => parts.filter(p => p != null && p !== '').join(' · ');
  const short = (text, n = 60) => {
    const clean = String(text ?? '').replace(/\s+/g, ' ').trim();
    return clean.length > n ? clean.slice(0, n - 1).trimEnd() + '…' : clean;
  };
  const age = at => {
    if (!finite(at)) return null;
    const s = Math.max(0, (Date.now() / 1000 - at) | 0);
    return s < 60 ? s + 's ago' : s < 3600 ? (s / 60 | 0) + 'm ago' : s < 86400 ? (s / 3600 | 0) + 'h ago' : (s / 86400 | 0) + 'd ago';
  };
  const name = seat => window.OfficeFloor.seatName(seat);
  const compact = n => finite(n) ? new Intl.NumberFormat('en', {notation: 'compact', maximumFractionDigits: 1}).format(n) : 'unknown';
  const usd = n => finite(n) ? (n < 0 ? '-$' : '+$') + Math.abs(n).toFixed(2) : 'unknown';
  const figure = (value, label, n) => value == null ? null : {value, label, tone: !finite(n) || n === 0 ? '' : n > 0 ? 'positive' : 'negative'};
  const seatsOf = (d, id) => (d.rooms?.[id] ?? []).filter(s => d.seats?.[s]);
  const total = (rows, key) => rows.length && rows.every(r => finite(r[key])) ? rows.reduce((n, r) => n + r[key], 0) : null;
  const inRoom = (d, id) => new Set(d.rooms?.[id] ?? []);

  const thoughts = (title, id) => [title, 'paper', d => seatsOf(d, id).filter(s => d.seats[s].last_thought).map(s => line(name(s), short(d.seats[s].last_thought, 70), age(d.seats[s].last_thought_at)))];
  const mail = (title, id, material) => [title, material, d => {
    const mine = inRoom(d, id);
    return (d.mail ?? []).filter(m => mine.has(m.from) || mine.has(m.to)).slice(0, 4).map(m => line(`${m.from === 'operator' ? 'operator' : name(m.from)} to ${m.to === 'operator' ? 'operator' : name(m.to)}`, short(m.text, 60), age(m.at)));
  }];
  const requests = (title, id, material) => [title, material, d => {
    const mine = inRoom(d, id), rows = (d.requests ?? []).filter(r => mine.has(r.seat));
    return {figure: figure(d.requests ? String(rows.length) : null, 'recorded, latest 10 per seat'), lines: rows.slice(0, 3).map(r => line(name(r.seat), short(r.text, 60)))};
  }];
  const tools = (title, id, material) => [title, material, d => {
    const mine = inRoom(d, id);
    return (d.activity ?? []).filter(e => e.kind === 'tool' && mine.has(e.seat)).slice(0, 4).map(e => line(name(e.seat), e.title, age(e.at)));
  }];
  const book = [['Paper book', 'screen', d => {
    if (d.book?.state !== 'known') return {lines: ['Unknown: ' + (d.book?.reason ?? 'no ledger given to this page').replaceAll('_', ' ')]};
    const rows = d.book.books;
    return {figure: figure(usd(rows.reduce((n, b) => n + (b.net_usd ?? 0), 0)), 'closed, net of costs', rows.reduce((n, b) => n + (b.net_usd ?? 0), 0)),
      lines: rows.map(b => line(b.name, b.open + ' open', b.closed + ' closed', b.wins + ' won'))};
  }], ['Last closed', 'notebook', d => (d.book?.last_closed ?? []).slice(0, 2).map(o => line(o.book, usd(o.net_usd), short((o.exit_reason ?? '').replaceAll('_', ' '), 40), age(o.closed_at)))]];

  Object.assign(window.OfficeFloor.furniture, {
    newsroom: [thoughts('Latest thoughts', 'newsroom'), mail('Mail', 'newsroom', 'cabinet')],
    research: [thoughts('Last findings', 'research'), requests('Seat requests', 'research', 'gold')],
    debate: [mail('Mail', 'debate', 'cabinet'), tools('Tool calls', 'debate', 'notebook')],
    decision: [requests('Requests', 'decision', 'gold'), mail('Mail', 'decision', 'paper'), thoughts('Latest thought', 'decision')],
    trading: book,
  });

  const table = (head, rows) => {
    const t = make('table'), tr = make('tr');
    for (const h of head) tr.append(make('th', h));
    t.append(tr);
    for (const row of rows) {
      const r = make('tr');
      for (const cell of row) r.append(make('td', cell ?? 'unknown'));
      t.append(r);
    }
    return t;
  };
  const section = (title, ...nodes) => {
    const s = make('section');
    s.append(make('h3', title), ...nodes);
    return s;
  };
  const empty = text => make('p', text);
  const stamp = at => finite(at) ? new Date(at * 1000).toISOString().replace('T', ' ').slice(0, 19) + ' UTC' : 'unknown';

  function seatsSection(d, id) {
    const rows = seatsOf(d, id).map(s => {
      const r = d.seats[s];
      return [name(s), r.status ?? 'unknown', r.last_action, r.last_tool, stamp(r.last_at), r.wakes_today, finite(r.tokens_today) ? compact(r.tokens_today) : 'unknown', r.unread_mail];
    });
    return section('Seats', rows.length ? table(['Seat', 'State', 'Doing', 'Last tool', 'Last event', 'Wakes today', 'Tokens today', 'Unread mail'], rows) : empty('No seat of this office sits in this room.'));
  }
  function mailSection(d, id) {
    const mine = inRoom(d, id), rows = (d.mail ?? []).filter(m => mine.has(m.from) || mine.has(m.to));
    return section('Mail', rows.length ? table(['When', 'From', 'To', 'Text'], rows.map(m => [stamp(m.at), m.from, m.to, short(m.text, 300)])) : empty('No mail recorded for these seats.'));
  }
  function requestSection(d, id) {
    const mine = inRoom(d, id), rows = (d.requests ?? []).filter(r => mine.has(r.seat));
    return section('Requests', rows.length ? table(['When', 'Seat', 'Request'], rows.map(r => [stamp(r.at), r.seat, short(r.text, 400)])) : empty('No requests recorded. A reply does not close a request here.'));
  }
  function toolSection(d, id) {
    const mine = inRoom(d, id), rows = (d.activity ?? []).filter(e => mine.has(e.seat) && e.kind !== 'mail');
    return section('Activity', rows.length ? table(['When', 'Seat', 'Kind', 'What'], rows.map(e => [stamp(e.at), e.seat, e.kind, e.title])) : empty('No wakes, requests or tool calls recorded.'));
  }
  function ledgerSection(d) {
    if (d.book?.state !== 'known') return section('Paper book', empty('Unknown: ' + (d.book?.reason ?? 'no ledger given to this page').replaceAll('_', ' ') + '. Start the page with --ledger-db to read the desk plug-in ledgers.'));
    return section('Paper book', table(['Ledger', 'Open', 'Closed', 'Won', 'Net, closed'], d.book.books.map(b => [b.name, b.open, b.closed, b.wins, usd(b.net_usd)])),
      empty('Modelled paper fills, no landing proof; small samples do not show skill. Net counts closed orders that have a recorded exit price.'),
      table(['Closed', 'Ledger', 'Net', 'Exit'], (d.book.last_closed ?? []).map(o => [stamp(o.closed_at), o.book, usd(o.net_usd), (o.exit_reason ?? '').replaceAll('_', ' ')])));
  }
  const detail = (id, ...extra) => d => {
    const box = make('div');
    box.append(seatsSection(d, id), ...extra.map(f => f(d, id)));
    return box;
  };
  const rooms = [['decision', 'Decision', 10, [mailSection, requestSection, toolSection]], ['debate', 'Debate', 20, [mailSection, toolSection]],
    ['trading', 'Trading', 30, []], ['newsroom', 'Newsroom', 40, [mailSection, toolSection]], ['research', 'Research', 50, [requestSection, mailSection, toolSection]]];
  for (const [id, title, order, parts] of rooms) {
    window.OfficeRooms.register({id, title, order, seats: [], summary: () => make('p', 'unknown'),
      detail: id === 'trading' ? d => { const box = make('div'); box.append(ledgerSection(d)); return box; } : detail(id, ...parts)});
  }
})();
