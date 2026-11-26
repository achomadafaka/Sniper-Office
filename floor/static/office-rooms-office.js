(() => {
  'use strict';
  // What this office's store holds, added to each room: its boards on the wall and its tabs inside. The rooms the store has nothing for keep their own boards and say so.
  const {table, section, block, line, time, text} = window.OfficeDetail;
  const finite = v => typeof v === 'number' && Number.isFinite(v);
  const short = (value, n = 70) => {
    const clean = String(value ?? '').replace(/\s+/g, ' ').trim();
    return clean.length > n ? clean.slice(0, n - 1).trimEnd() + '…' : clean;
  };
  const age = at => {
    if (!finite(at)) return null;
    const s = Math.max(0, (Date.now() / 1000 - at) | 0);
    return s < 60 ? s + 's ago' : s < 3600 ? (s / 60 | 0) + 'm ago' : s < 86400 ? (s / 3600 | 0) + 'h ago' : (s / 86400 | 0) + 'd ago';
  };
  const name = seat => window.OfficeFloor.seatName(seat);
  const coin = (chain, mint, ticker) => window.OfficeNames.node(chain, mint, ticker);       // Name ($SYMBOL), the address small and secondary
  const coinText = (chain, mint, ticker) => window.OfficeNames.text(chain, mint, ticker);
  const join = (...parts) => parts.filter(p => p != null && p !== '').join(' · ');
  const usd = n => finite(n) ? (n < 0 ? '-$' : '+$') + Math.abs(n).toFixed(2) : null;
  const dollars = n => finite(n) ? '$' + n.toFixed(2) : null;
  const figure = (value, label, n) => value == null ? null : {value, label, tone: !finite(n) || n === 0 ? '' : n > 0 ? 'positive' : 'negative'};
  const stamp = at => finite(at) ? time(at) : text('span', 'unknown');
  const human = v => String(v ?? 'unknown').replaceAll('_', ' ');
  const mine = (d, id) => new Set(d.rooms?.[id] ?? []);
  const seatsOf = (d, id) => (d.rooms?.[id] ?? []).filter(s => d.seats?.[s]);
  const floorOf = d => d.floor ?? {};
  const allBooks = d => (floorOf(d).book_list ?? []).filter(([key]) => key.startsWith('book:')).map(([key, label]) => [label, d[key]]);
  // Paused seats (the Control room's list) drop out of the working tables. An order's id is its request's id; an order with no request in view is kept.
  const paused = d => new Set(d.rooms?.control ?? []);
  const roleOfActor = (d, actor) => d.seats ? Object.keys(d.seats).find(k => d.seats[k].actor_id === actor) ?? actor : actor;
  const requester = d => Object.fromEntries((floorOf(d).paper_requests ?? []).map(r => [r.request_id, roleOfActor(d, r.actor_id)]));
  const orderRows = d => { const off = paused(d), who = requester(d); return (floorOf(d).orders ?? []).filter(o => !off.has(who[o.order_id])); };

  // ---- the wall
  const thoughts = (title, id, material) => [title, material, d => seatsOf(d, id).filter(s => d.seats[s].last_thought).map(s => join(name(s), short(d.seats[s].last_thought, 90), age(d.seats[s].last_thought_at)))];
  const mail = (title, id, material) => [title, material, d => {
    const set = mine(d, id);
    return (d.floor?.mail ?? []).filter(m => set.has(m.from) || (m.to_all ?? [m.to]).some(t => set.has(t))).slice(0, 4).map(m => join((m.from === 'operator' ? 'operator' : name(m.from)) + ' to ' + (m.to_all ?? [m.to]).map(t => t === 'operator' ? 'operator' : name(t)).join(', '), short(mailParts(m.text).first, 80), age(m.at)));
  }];
  const requests = (title, id, material) => [title, material, d => {
    const set = mine(d, id), rows = (d.floor?.requests ?? []).filter(r => set.has(r.seat));
    return {figure: figure(d.floor?.requests ? String(rows.length) : null, 'recorded, latest 10 per seat'), lines: rows.slice(0, 3).map(r => join(name(r.seat), short(r.text, 70)))};
  }];
  const tools = (title, id, material) => [title, material, d => {
    const set = mine(d, id);
    return (d.activity ?? []).filter(e => e.kind === 'tool' && set.has(e.seat)).slice(0, 4).map(e => join(name(e.seat), e.title, age(e.at)));
  }];
  const standups = (title, material) => [title, material, d => (d.floor?.standups ?? []).slice(0, 3).map(s => join(name(s.role), short(s.text, 90), age(s.at)))];
  const notebook = (title, id, material) => [title, material, d => seatsOf(d, id).map(s => d.floor.notebooks?.[s]).filter(Boolean).flatMap(n => [short(n.text, 160), age(n.at)])];
  const bookBoard = ([label, book]) => [label, 'screen', () => {
    const s = book?.summary;
    if (!s) return [];
    return {figure: figure(usd(s.net_usd), 'closed, net of costs', s.net_usd), lines: [join(s.open + ' open', s.scored + ' closed', s.wins + ' won'), s.censored ? s.censored + ' never filled' : null]};
  }];
  const trading = d => [...allBooks(d).slice(0, 3).map(bookBoard), ['Last closed', 'notebook', () => {
    const closed = allBooks(d).flatMap(([label, b]) => (b?.calls ?? []).filter(c => c.outcome && finite(c.exit_at)).map(c => ({...c, label}))).sort((a, b) => b.exit_at - a.exit_at).slice(0, 2);
    return closed.flatMap(c => [join(c.symbol, usd(c.net_usd), human(c.outcome)), join(c.label, age(c.exit_at))]);
  }]];
  Object.assign(window.OfficeFloor.furniture, {
    newsroom: [['Feed tickets', 'screen', d => {
      const tickets = d.floor?.tickets, rows = d.floor?.candidates, passed = (d.floor?.ticket_counts ?? []).find(c => c.verdict === 'PASS');
      if (tickets) return {figure: figure(String(passed?.n ?? 0), 'passed the feed filter'), lines: tickets.slice(0, 3).map(t => join(t.verdict, coinText(t.network_id, t.mint), short(t.reason, 40), age(Date.parse(t.ts) / 1000)))};
      return rows ? {figure: figure(String(rows.length), 'candidate coins recorded'), lines: rows.slice(0, 3).map(r => join(coinText(r.network_id, r.mint), r.network_id, age(r.known_at)))} : [];
    }], thoughts('Latest thoughts', 'newsroom', 'paper'), mail('Mail', 'newsroom', 'cabinet')],
    research: [thoughts('Last findings', 'research', 'paper'), requests('Seat requests', 'research', 'gold'),
      ['Browsing', 'screen', d => (d.floor?.visits ?? []).slice(0, 4).map(v => join(v.host, short(v.path, 30), age(v.at)))]],
    explorer: [notebook('Newest notebook', 'explorer', 'notebook'), requests('Requests', 'explorer', 'gold')],
    memes: [thoughts('Narrative thoughts', 'memes', 'screen'), ['Hit rate', 'paper', d => {
      const rows = orderRows(d).filter(o => o.state === 'closed' && finite(o.exit_usd) && finite(o.spend_usd)), wins = rows.filter(o => o.exit_usd - o.spend_usd - (o.costs_usd ?? 0) > 0).length;
      return rows.length ? {figure: figure(String(wins), 'paper orders won'), lines: [join('of ' + rows.length + ' closed', 'all ledgers')]} : [];
    }]],
    debate: [mail('Mail', 'debate', 'cabinet'), tools('Tool calls', 'debate', 'notebook'), standups('Standups', 'paper')],
    decision: [['Open now', 'paper', d => {
      const open = orderRows(d).filter(o => o.state === 'open');
      return {figure: figure(String(open.length), 'orders open in the books'), lines: open.slice(0, 4).map(o => join(coinText(o.network_id, o.mint, o.ticker), dollars(o.spend_usd), age(o.filled_at)))};
    }], ['Finished', 'gold', d => {
      const closed = orderRows(d).filter(o => o.state === 'closed' && finite(o.exit_usd) && finite(o.spend_usd));
      const net = closed.reduce((n, o) => n + o.exit_usd - o.spend_usd - (o.costs_usd ?? 0), 0);
      return closed.length ? {figure: figure(usd(net), 'net, ' + closed.length + ' closed orders', net), lines: [closed.filter(o => o.exit_usd - o.spend_usd - (o.costs_usd ?? 0) > 0).length + ' made money']} : [];
    }], requests('Requests', 'decision', 'notebook'), mail('Head directions', 'decision', 'cabinet')],
    trading,
    control: [window.OfficeFloor.furniture.control[0], ['Wakes', 'screen', d => (d.floor?.wakes ?? []).slice(0, 4).map(w => join(name(w.role), w.outcome, w.model, age(w.at)))], window.OfficeFloor.furniture.control[1]],
  });

  // ---- inside
  const seatTable = (d, roles) => table('Seats', ['Seat', 'State', 'Doing', 'Model', 'Last event', 'Wakes today', 'Tokens today', 'Unread mail', 'Last tool'],
    roles.filter(r => d.seats?.[r]), r => { const s = d.seats[r]; return [name(r), s.office_status, s.last_action, s.model, stamp(s.last_at), s.wakes_today, s.tokens_today, s.unread_mail, s.last_tool]; });
  const mailParts = (value, n = 200) => { const clean = String(value ?? '').replace(/\s+/g, ' ').trim(), cut = clean.search(/[.!?](\s|$)/), first = cut < 0 ? clean : clean.slice(0, cut + 1), rest = clean.slice(first.length).trim(); return {first, rest, preview: rest.length > n ? rest.slice(0, n - 1).trimEnd() + '…' : rest}; };
  const who = role => role === 'operator' ? 'operator' : name(role);
  const mailBody = value => {          // first sentence bold, the rest cut to ~200 chars; click opens the whole text. text() keeps the address Copy buttons.
    const {first, rest, preview} = mailParts(value);
    const lead = text('b', first);
    if (rest === preview) return block(lead, rest ? text('span', ' ' + rest) : null);
    const box = text('details'), summary = text('summary'), more = text('span', ' ' + preview);
    summary.append(lead, more);
    box.append(summary, text('p', rest));
    box.addEventListener('toggle', () => { more.hidden = box.open; });
    return box;
  };
  const mailTable = (d, roles) => {
    const set = new Set(roles);
    return table('Mail', ['When', 'From', 'To', 'Text'], (d.floor?.mail ?? []).filter(m => !roles || set.has(m.from) || (m.to_all ?? [m.to]).some(t => set.has(t))),
      m => [stamp(m.at), who(m.from), (m.to_all ?? [m.to]).map(who).join(', '), mailBody(m.text)]);
  };
  const requestTable = (d, roles) => {
    const set = new Set(roles);
    return table('Requests', ['When', 'Seat', 'Request'], (d.floor?.requests ?? []).filter(r => !roles || set.has(r.seat)), r => [stamp(r.at), r.seat, short(r.text, 600)]);
  };
  const activityTable = (d, roles) => {
    const set = new Set(roles);
    return table('Activity', ['When', 'Seat', 'Kind', 'What'], (d.activity ?? []).filter(e => e.kind !== 'mail' && (!roles || set.has(e.seat))), e => [stamp(e.at), e.seat, e.kind, e.title]);
  };
  const toolTable = (d, roles) => {
    const set = new Set(roles);
    return table('Tools used', ['Seat', 'Tool', 'Calls', 'Last call'], (d.floor?.tools ?? []).filter(t => !roles || set.has(t.role)), t => [name(t.role), t.tool, t.n, stamp(t.at)]);
  };
  const notebookSection = (d, roles) => {
    const items = roles.map(r => [r, d.floor?.notebooks?.[r]]).filter(([, n]) => n?.text);
    if (!items.length) return text('div');
    return section('Notebooks', ...items.map(([role, n]) => {
      const box = text('div'), body = text('p', n.text.slice(0, 6000));
      body.className = 'od-notebook';
      box.append(text('h4', name(role)), line('Saved', age(n.at)), body);
      return box;
    }));
  };
  const taskTable = (d, roles) => {
    const set = new Set(roles);
    return table('Tasks', ['Seat', 'Task', 'Status', 'Note'], (d.floor?.tasks ?? []).filter(t => !roles || set.has(t.seat)), t => [name(t.seat), t.title, t.status, t.note]);
  };
  const standupTable = (d, roles) => {
    const set = new Set(roles);
    return table('Standups', ['When', 'Seat', 'Said'], (d.floor?.standups ?? []).filter(s => !roles || set.has(s.role)), s => [stamp(s.at), name(s.role), short(s.text, 700)]);
  };
  const visitTable = (d, roles) => {
    const set = new Set(roles);
    return table('Pages read', ['When', 'Seat', 'Site', 'Path'], (d.floor?.visits ?? []).filter(v => !roles || set.has(v.role)), v => [stamp(v.at), name(v.role), v.host, v.path]);
  };
  const orderTable = (title, d, wanted) => table(title, ['Ledger', 'Coin', 'Chain', 'Requested', 'Filled', 'Closed', 'Spent', 'Exit value', 'Costs', 'Net', 'Why it ended'],
    orderRows(d).filter(o => wanted.includes(o.state)).sort((a, b) => b.requested_at - a.requested_at),
    o => [o._source, coin(o.network_id, o.mint, o.ticker), o.network_id, stamp(o.requested_at), stamp(o.filled_at), stamp(o.closed_at), dollars(o.spend_usd), dollars(o.exit_usd), dollars(o.costs_usd),
      usd(o.state === 'closed' && finite(o.exit_usd) && finite(o.spend_usd) ? o.exit_usd - o.spend_usd - (o.costs_usd ?? 0) : null), human(o.exit_reason ?? o.reason)], o => o.order_id);
  const filler = d => {
    const f = d.floor?.filler;
    return section('Paper filler', f && (finite(f.last_mark_at) || finite(f.last_outcome_at)) ? block(line('Last mark', finite(f.last_mark_at) ? age(f.last_mark_at) : null), line('Last request outcome', finite(f.last_outcome_at) ? age(f.last_outcome_at) : null), line('Last fill attempt', finite(f.last_attempt_at) ? age(f.last_attempt_at) : null)) : line('The filler has recorded no mark or outcome in this ledger.'));
  };
  const paperRequests = d => table('Requests to the filler', ['When', 'Kind', 'Seat', 'Coin', 'Chain', 'Reason'], (d.floor?.paper_requests ?? []).filter(r => !paused(d).has(roleOfActor(d, r.actor_id))), r => [stamp(r.at), r.kind, roleOfActor(d, r.actor_id), coin(r.network_id, r.mint), r.network_id, r.reason], r => r.request_id);
  const outcomes = d => table('Request outcomes', ['When', 'Kind', 'Outcome', 'Reason'], d.floor?.outcomes, r => [stamp(r.at), r.kind, r.outcome, r.reason], r => r.request_id);
  const capital = d => table('Capital', ['Ledger', 'Book', 'When', 'Kind', 'Amount', 'Note'], d.floor?.capital, c => [c._source, c.book_id, stamp(c.at), c.kind, dollars(c.usd), c.note]);
  const halts = d => table('Halts', ['When', 'Action', 'By', 'Reason'], d.floor?.halts, h => [stamp(h.at), h.action, h.actor_id, h.reason], h => 'halt-' + h.seq);
  const feedTickets = d => table('Feed tickets', ['When', 'Verdict', 'Coin', 'Chain', 'Stage', 'Why', 'Seat'], d.floor?.tickets, t => [stamp(Date.parse(t.ts) / 1000), t.verdict, coin(t.network_id, t.mint), t.network_id, t.stage, t.reason, t.seat], t => t.ticket_id);
  const candidates = d => table('Candidate coins', ['Known', 'Coin', 'Chain', 'Pool', 'Seat'], d.floor?.candidates, c => [stamp(c.corrected_known_at ?? c.known_at), coin(c.network_id, c.mint), c.network_id, c.pool, c.actor_id]);
  const filters = d => table('Feed filters', ['When', 'Seat', 'Reason', 'Rules'], (d.floor?.filters ?? []).filter(f => !paused(d).has(roleOfActor(d, f.actor_id))), f => [stamp(f.at), name(roleOfActor(d, f.actor_id)), f.reason, ruleCount(f.rules_json)], f => 'filter-' + f.seq);
  const ruleCount = raw => { try { const v = JSON.parse(raw); const n = Array.isArray(v) ? v.length : v && typeof v === 'object' ? Object.keys(v).length : null; return n == null ? null : n + (n === 1 ? ' rule' : ' rules'); } catch { return null; } };
  const reads = d => table('Results reads', ['Seat', 'First read', 'Last read', 'Items', 'Reads'], d.floor?.reads, r => [r.actor_id, stamp(r.first_at), stamp(r.last_at), r.items_n, r.reads]);
  const wakeTable = (d, roles) => {
    const set = roles && new Set(roles);
    return table('Wakes', ['Ended', 'Seat', 'Model', 'Outcome', 'Tokens', 'Seconds'], (d.floor?.wakes ?? []).filter(w => !set || set.has(w.role)),
      w => [stamp(w.at), name(w.role), w.model, w.outcome, w.tokens, finite(w.started_at) && finite(w.at) ? Math.round(w.at - w.started_at) : null]);
  };

  const extras = {
    newsroom: (d, r) => [seatTable(d, r), feedTickets(d), candidates(d), filters(d), mailTable(d, r), activityTable(d, r), notebookSection(d, r)],
    research: (d, r) => [seatTable(d, r), requestTable(d, r), visitTable(d, r), mailTable(d, r), toolTable(d, r), notebookSection(d, r), activityTable(d, r)],
    explorer: (d, r) => [seatTable(d, r), notebookSection(d, r), requestTable(d, r), visitTable(d, r), toolTable(d, r), activityTable(d, r)],
    memes: (d, r) => [seatTable(d, r), notebookSection(d, r), mailTable(d, r), orderTable('Paper orders, all ledgers', d, ['open', 'closed', 'censored']), activityTable(d, r)],
    debate: (d, r) => [seatTable(d, r), mailTable(d, r), standupTable(d), toolTable(d, r), wakeTable(d, r), activityTable(d, r)],
    decision: (d, r) => [seatTable(d, r), orderTable('Open orders', d, ['open', 'pending']), orderTable('Closed orders', d, ['closed', 'censored']), requestTable(d, r), mailTable(d, r), taskTable(d, r), halts(d), activityTable(d, r)],
    trading: d => [d.mk ? null : section('Perps and stocks', line('This office holds no margin books, so perps and stock margin returns, tickets and queued entries are unknown.')), orderTable('Orders, every ledger', d, ['open', 'pending', 'closed', 'censored']), paperRequests(d), outcomes(d), capital(d), filler(d), reads(d)],
    control: d => [seatTable(d, Object.keys(d.seats ?? {})), wakeTable(d), toolTable(d), taskTable(d), halts(d), standupTable(d), mailTable(d), requestTable(d)],
  };
  for (const [id, build] of Object.entries(extras)) {
    const base = window.OfficeRooms.get(id);
    if (!base) continue;
    window.OfficeRooms.register({...base, detail(d) {
      const box = block();
      const original = base.detail(d);
      const added = build(d, d.rooms?.[id] ?? []).filter(node => node?.textContent.trim());
      const first = original?.tagName === 'DIV' ? [...original.children] : [];
      box.append(...(id === 'trading' ? [...first, ...added] : [...added, ...first]));
      return box.children.length || !original ? box : original;   // a base detail may return null (nothing to show); the desk needs a node
    }});
  }
})();
