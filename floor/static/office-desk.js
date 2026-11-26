(() => {
  'use strict';
  const root = document.getElementById('office-desk');
  if (!root) return;
  const rooms = new Map();
  const unknown = 'unknown';
  const value = v => v ?? unknown;
  const el = (tag, text, cls) => {
    const node = document.createElement(tag);
    if (text != null) node.textContent = text;
    if (cls) node.className = cls;
    return node;
  };
  const age = at => {
    if (!Number.isFinite(at)) return unknown;
    const seconds = Math.max(0, Math.floor(Date.now() / 1000 - at));
    if (seconds < 60) return `${seconds} s ago`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
    return `${Math.floor(seconds / 3600)} h ago`;
  };
  function clock(at) {
    const time = el('time', age(at));
    if (Number.isFinite(at)) {
      const date = new Date(at * 1000);
      time.dateTime = date.toISOString();
      time.title = `${date.toUTCString()} / ${date.toLocaleString()}`;
    }
    return time;
  }
  const header = el('header', null, 'od-header');
  const grid = el('section', null, 'od-grid od-floor');
  const seats = el('section', null, 'od-desk-now');
  const feed = el('div', null, 'od-feed');
  const floor = el('section', null, 'od-floor-view');
  const activity = el('details', null, 'od-floor-activity');
  activity.append(el('summary', 'Recent activity'), feed);
  floor.append(el('h2', 'Office floor'), grid, activity);
  const pnl = el('section', null, 'od-desk-pnl');
  const records = el('section', null, 'od-desk-records');
  const body = el('div', null, 'od-body');
  const detail = el('section', null, 'od-detail');
  body.append(pnl, seats, floor, records, detail);
  root.append(header, body);
  detail.hidden = true;
  grid.setAttribute('aria-label', 'Office rooms');
  let data = {}, failed = false, active = null, opener = null, busy = false, roomScroll = 0;
  let seen = null, incoming = new Set(), changedRooms = new Set();
  const eventKey = event => JSON.stringify([event.kind, event.room, event.item_id, event.at]);
  const placeholders = [
    ['decision', 'Decision', 10], ['debate', 'Debate', 20], ['trading', 'Trading', 30],
    ['newsroom', 'Newsroom', 40], ['research', 'Research', 50], ['explorer', 'Explorer', 60],
    ['memes', 'Memes', 70], ['sports', 'Sports', 80], ['lab', 'Lab', 90],
    ['coach', 'Coach and skills', 100], ['control', 'Control', 110], ['strategies', 'Strategy Lab', 12],
    ['situation', 'Situation', 35],
  ];
  for (const [id, title, order] of placeholders) {
    rooms.set(id, {id, title, order, seats: [], summary: () => el('p', unknown), detail: () => el('p', unknown)});
  }
  window.OfficeRooms = {
    get: id => rooms.get(id),
    register(room) {
      if (!/^[a-z][a-z0-9-]*$/.test(room.id) || !Number.isFinite(room.order)
          || typeof room.summary !== 'function' || typeof room.detail !== 'function') {
        throw new TypeError('Invalid Office room');
      }
      rooms.set(room.id, room);
      render();
    },
  };
  const activeRoles = () => new Set((data.desk_now?.seats ?? []).map(row => row.role));
  const staffed = room => room.seats?.some(seat => activeRoles().has(seat));
  const canOpen = id => id === 'control' || Boolean(rooms.has(id) && staffed(rooms.get(id)));
  const seatName = role => window.OfficeFloor.seatName(role);
  const clean = input => typeof input === 'string' ? window.OfficeNames.scrub(input.replace(/\s+/g, ' ').trim()) : '';
  const toolLabels = {
    browse: 'Checking a source', web_search: 'Looking for sources', db_read: 'Checking stored data',
    mail_read: 'Reading mail', mail_send: 'Sending a handoff', shell: 'Working in the notebook',
    brain_read: 'Checking earlier findings', brain_note: 'Saving a finding', story_list: 'Scanning stories',
    story_read: 'Checking a story', sleep: 'Finishing this turn',
  };
  const toolLabel = tool => toolLabels[tool] ?? 'Using ' + tool.replaceAll('_', ' ');
  function renderHeader() {
    const identity = el('a', 'Office', 'od-brand');
    identity.href = location.pathname + location.search;
    const status = el('p', null, 'od-snapshot');
    const asOf = data.desk_now?.as_of ?? data.as_of;
    status.append(failed ? 'Refresh failed · last read ' : 'Desk updated ', clock(asOf));
    status.classList.toggle('od-snapshot-stale', failed || (Number.isFinite(asOf) && Date.now() / 1000 - asOf > 45));
    const control = el('a', 'Control', 'od-control-link');
    control.href = '#room=control';
    header.replaceChildren(identity, status, control);
    window.OfficeLive?.header(data);
  }
  function localClock(at) {
    if (!Number.isFinite(at)) return unknown;
    const date = new Date(at * 1000);
    const local = new Date();
    const settings = {hour: 'numeric', minute: '2-digit'};
    if (date.toDateString() !== local.toDateString()) Object.assign(settings, {month: 'short', day: 'numeric'});
    return date.toLocaleString(undefined, settings);
  }
  function seatState(row) {
    if (row.status === 'working') return 'Working';
    if (row.status === 'sleeping') return Number.isFinite(row.sleep_until) ? 'Sleeping until ' + localClock(row.sleep_until) : 'Sleeping';
    if (row.status === 'parked') return 'Parked';
    return '';
  }
  const sentences = new Intl.Segmenter('en', {granularity: 'sentence'});
  const statement = text => text && (/[.!?]$/.test(text) ? text : text + '.');
  function actionOptions(row, thought) {
    const options = [...sentences.segment(thought)].map(part => part.segment.trim()).filter(Boolean);
    const useful = options.find(sentence => sentence.length >= 16) ?? options[0];
    const tool = clean(row.current_tool);
    if (!useful && !tool && !seatState(row)) {
      const at = data.seats?.[row.role]?.last_at ?? row.last_output?.at;
      return [Number.isFinite(at) ? 'Idle since ' + localClock(at) : 'No activity since restart'];
    }
    return [...new Set([
      statement(useful?.split(/;\s/)[0]), useful, statement(useful?.split(/:\s/)[0]),
      statement(tool ? toolLabel(tool) : ''), ...options.slice(1),
      thought ? 'Recorded a work update.' : '',
    ].filter(Boolean))];
  }
  let activityLines = [];
  function fitActions() {
    for (const [line, options] of activityLines) {
      // Prefer a full thought, then a complete shorter sentence. Never clip the action.
      const height = 2 * parseFloat(getComputedStyle(line).lineHeight);
      for (const text of options) {
        line.textContent = text;
        if (line.scrollHeight <= height + 1) break;
      }
    }
  }
  const outputNames = {order: 'Paper order', call: 'Paper call', news_call: 'Paper call', brief: 'Brief', strategy: 'Strategy', review: 'Review', loss_review: 'Loss review'};
  function seatTarget(row) {
    const today = row.today ?? {};
    const target = row.assignment?.target;
    const unit = clean(target?.what);
    if (!Number.isInteger(target?.count) || !unit) return null;
    const key = /^(paper )?orders?$/i.test(unit) ? 'orders'
      : /^(work )?outputs?$/i.test(unit) ? 'outputs'
      : /^(mails?|messages?)$/i.test(unit) ? 'mails'
      : ({calls: 'news_call', briefs: 'brief', strategies: 'strategy', reviews: 'review'})[unit.toLowerCase()];
    const count = today[key] ?? today.by_kind?.[key];
    const shortUnit = unit.length <= 27 ? unit : '';
    const wording = Number.isInteger(count) ? count + ' of ' + target.count + ' ' + unit
      : shortUnit ? 'Target: ' + target.count + ' ' + shortUnit : 'Target from Head';
    const panel = el('div', null, 'od-seat-target');
    panel.append(el('span', wording));
    if (Number.isInteger(count) && target.count > 0) {
      const bar = el('progress');
      bar.max = target.count;
      bar.value = count;
      bar.setAttribute('aria-label', wording);
      panel.append(bar);
    }
    return panel;
  }
  function seatBadges(row) {
    const badges = el('div', null, 'od-seat-meta');
    const output = row.last_output;
    const type = outputNames[output?.kind];
    if (type) {
      const badge = el('span', type, 'od-output-badge');
      if (Number.isFinite(output.at)) badge.append(' ', clock(output.at));
      badges.append(badge);
    }
    const counts = Object.entries({orders: 'order', outputs: 'output'})
      .filter(([key]) => Number.isInteger(row.today?.[key]))
      .map(([key, word]) => row.today[key] + ' ' + (row.today[key] === 1 ? word : key));
    if (counts.length) badges.append(el('span', counts.join(' · ') + ' today', 'od-output-badge'));
    return badges;
  }
  const seatDialog = el('dialog', null, 'od-seat-dialog');
  seatDialog.setAttribute('aria-labelledby', 'od-seat-dialog-title');
  root.append(seatDialog);
  function showSeat(row, thought) {
    const title = el('h2', seatName(row.role));
    title.id = 'od-seat-dialog-title';
    const closeButton = el('button', 'Close');
    closeButton.type = 'button';
    closeButton.addEventListener('click', () => seatDialog.close());
    const heading = el('header');
    heading.append(title, closeButton);
    seatDialog.replaceChildren(heading);
    const entries = [
      ['Current tool', row.current_tool ? toolLabel(clean(row.current_tool)) : ''],
      [row.status === 'working' ? 'Current action' : 'Last action', thought, row.thought_at],
      ['Why parked', clean(row.parked_reason)],
      ['From Head', clean(row.assignment?.text), row.assignment?.at],
      [outputNames[row.last_output?.kind] ?? 'Last output', clean(row.last_output?.text), row.last_output?.at],
      ['Target', row.assignment?.target ? [row.assignment.target.count, clean(row.assignment.target.what)].join(' ') : ''],
      ['Target due', clean(row.assignment?.target?.due)],
    ];
    for (const [label, text, at] of entries) {
      if (!text) continue;
      const item = el('section');
      const line = el('h3', label);
      if (Number.isFinite(at)) line.append(' ', clock(at));
      item.append(line, el('p', text));
      seatDialog.append(item);
    }
    seatDialog.addEventListener('close', () => {
      seats.querySelector(`[data-desk-seat="${CSS.escape(row.role)}"] .od-seat-more`)?.focus();
    }, {once: true});
    seatDialog.showModal();
  }
  const avatarLabels = {head: 'H', pm_trader: 'PM', perps_trader: 'PX'};
  const seatOrder = ['head', 'scout', 'pm_trader', 'perps_trader', 'newsdesk', 'researcher', 'coach'];
  function renderSeats() {
    const view = data.desk_now;
    const grid = el('div', null, 'od-seat-cards');
    const ranked = role => (seatOrder.indexOf(role) + 1 || seatOrder.length + 1);
    activityLines = [];
    for (const row of [...(view?.seats ?? [])].sort((a, b) => ranked(a.role) - ranked(b.role))) {
      const name = seatName(row.role);
      const card = el('article', null, 'od-seat-card');
      card.dataset.deskSeat = row.role;
      card.dataset.state = row.status ?? 'unknown';
      const top = el('div', null, 'od-seat-card-top');
      const avatar = el('span', avatarLabels[row.role] ?? name[0], 'od-seat-avatar');
      avatar.setAttribute('aria-hidden', 'true');
      const identity = el('div');
      identity.append(el('h3', name));
      const state = seatState(row);
      if (state) identity.append(el('p', state, 'od-seat-state'));
      top.append(avatar, identity);
      const thought = clean(row.thought);
      if (thought || row.current_tool || row.assignment?.text || row.last_output?.text || row.parked_reason) {
        const more = el('button', 'Details', 'od-seat-more');
        more.type = 'button';
        more.setAttribute('aria-label', 'Details for ' + name);
        more.addEventListener('click', () => showSeat(row, thought));
        top.append(more);
      }
      card.append(top);
      const options = actionOptions(row, thought);
      if (options.length) {
        const action = el('p', options[0], 'od-seat-action');
        activityLines.push([action, options]);
        card.append(action);
      }
      const badges = seatBadges(row);
      const target = seatTarget(row);
      const footer = el('div', null, 'od-seat-footer');
      if (badges.children.length) footer.append(badges);
      if (target) footer.append(target);
      if (footer.children.length) card.append(footer);
      grid.append(card);
    }
    seats.replaceChildren(el('h1', 'Desk now'), grid);
    fitActions();
    if (!grid.children.length) seats.append(el('p', failed ? 'Desk data unavailable.' : 'Waiting for seat activity.'));
    if (view?.state === 'limited') seats.append(el('p', 'Assignments and output counts are temporarily unavailable.', 'od-paused'));
    if (view?.paused?.length) seats.append(el('p', 'Paused: ' + view.paused.map(seatName).join(', '), 'od-paused'));
  }
  const laneBooks = {polymarket: ['PM paper', 'pm_trader'], perps: ['Perps paper', 'perps_trader']};
  const paperStartUsd = 1000; // default start_cash for the floor's markets policy
  const amount = (n, digits = 2) => Number.isFinite(n)
    ? new Intl.NumberFormat('en-US', {style: 'currency', currency: 'USD', maximumFractionDigits: digits}).format(n) : null;
  const returnPct = n => Number.isFinite(n) ? n.toFixed(2) + '%' : null;
  const add = (rows, field) => rows.every(row => Number.isFinite(row[field]))
    ? rows.reduce((total, row) => total + row[field], 0) : null;
  function marketPosition(position, book) {
    const quote = position.live;
    const price = n => Number.isFinite(n) ? position.lane === 'polymarket' ? (n * 100).toFixed(1) + '¢'
      : amount(n, n < 1 ? 6 : 2) : null;
    return {id: position.pos_id, label: position.label ?? position.instrument, book, side: position.side, entry: price(position.entry_price),
      mark: price(quote?.price), markAt: quote?.mark_at, unrealized: quote?.unrealized, pct: quote?.pnl_pct_of_margin,
      opened: position.entry_at, closed: position.exit_at, exit: price(position.exit_price), net: position.realized,
      strategy: position.strategy_id};
  }
  function coinPosition(row, book) {
    const mark = Number.isFinite(row.mark_value) && Number.isFinite(row.spend_usd) && Number.isFinite(row.mark_at)
      ? row.mark_value - row.spend_usd - (row.costs_usd ?? 0) : null;
    const closed = Number.isFinite(row.exit_usd) && Number.isFinite(row.spend_usd)
      ? row.exit_usd - row.spend_usd - (row.costs_usd ?? 0) : null;
    const price = n => Number.isFinite(n) ? amount(n, Math.abs(n) < 1 ? 8 : 2) : null;
    return {id: row.order_id, label: window.OfficeNames.text(row.network_id, row.mint, row.ticker), book, side: 'Long',
      entry: price(row.entry_price_usd), mark: price(row.mark_price), markAt: row.mark_at, unrealized: mark,
      pct: Number.isFinite(mark) && row.spend_usd > 0 ? 100 * mark / row.spend_usd : null, opened: row.filled_at,
      closed: row.closed_at, net: closed, strategy: row.strategy_id};
  }
  function paperBooks() {
    const books = [];
    const labs = data.mk?.paper_labs;
    for (const [lane, [title, role]] of Object.entries(laneBooks)) {
      if (!activeRoles().has(role)) continue;
      const sameLane = (labs?.books ?? []).filter(book => book.lane === lane);
      const orders = data.desk_now?.seats.find(seat => seat.role === role)?.today?.orders;
      if (!sameLane.length) {
        const empty = Array.isArray(labs?.books);
        books.push({title, start: empty ? paperStartUsd : null, equity: empty ? paperStartUsd : null,
          realized: empty ? 0 : null, unrealized: empty ? 0 : null, orders: empty ? 0 : null,
          open: [], closes: [], empty, known: empty});
        continue;
      }
      for (const book of sameLane) {
        const label = sameLane.length === 1 ? title : title + ' · ' + book.book_id;
        books.push({title: label, start: book.start_cash, equity: book.equity, realized: book.realized_total,
          unrealized: book.unrealized_total, orders: sameLane.length === 1 ? orders : null, known: true,
          open: (book.open ?? []).map(row => marketPosition(row, label)),
          closes: (book.closed_recent ?? []).map(row => marketPosition(row, label))});
      }
    }
    const coin = data.sources?.orders;
    const starts = (data.floor?.capital ?? []).filter(row => activeRoles().has('scout')
      && row.kind === 'initial' && row.book_id === data.office?.epoch);
    if (activeRoles().has('scout') && !starts.length) books.push({title: 'Coin desk paper', open: [], closes: [], known: false});
    for (const start of starts) {
      const title = starts.length === 1 ? 'Coin desk paper' : 'Coin desk · ' + start._source;
      const rows = (coin?.rows ?? []).filter(row => row.book_id === start.book_id && row._source === start._source);
      const open = rows.filter(row => row.state === 'open').map(row => coinPosition(row, title));
      const closes = rows.filter(row => row.state === 'closed').map(row => coinPosition(row, title));
      const flows = data.floor.capital.filter(row => row.book_id === start.book_id && row._source === start._source);
      const capital = add(flows, 'usd');
      // The named order source is bounded at 150 rows. A full page cannot prove lifetime totals.
      const complete = coin?.state === 'known' && coin.rows.length < 150;
      const realized = complete ? add(closes, 'net') : null;
      const unrealized = complete ? add(open, 'unrealized') : null;
      const priced = [capital, realized, unrealized].every(Number.isFinite);
      const dayStart = data.desk_now?.day_start;
      const orders = complete && Number.isFinite(dayStart) ? rows.filter(row => row.filled_at >= dayStart).length : null;
      books.push({title, start: start.usd, equity: priced ? capital + realized + unrealized : null,
        realized, unrealized, orders, open, closes, empty: complete && !rows.length, known: complete,
        chartCapital: flows.length === 1 ? start.usd : null,
        olderMark: open.length ? Math.min(...open.map(row => row.markAt ?? Infinity)) : null});
    }
    return books;
  }
  function equityChart(book) {
    // Rebuild only the available sequence of settled closes, then add the current mark.
    const capital = book.chartCapital === undefined ? book.start : book.chartCapital;
    if (!Number.isFinite(capital) || !Number.isFinite(book.realized)) return null;
    const closed = [...book.closes].sort((a, b) => a.closed - b.closed);
    const recent = add(closed, 'net');
    if (!Number.isFinite(recent)) return null;
    let previous = capital + book.realized - recent;
    const values = [previous, ...closed.map(row => previous += row.net)];
    if (Number.isFinite(book.equity)) values.push(book.equity);
    if (values.length < 2) return null;
    const min = Math.min(...values), range = Math.max(...values) - min;
    const namespace = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(namespace, 'svg');
    svg.setAttribute('viewBox', '0 0 132 38');
    svg.setAttribute('class', 'od-equity-spark');
    svg.setAttribute('role', 'img');
    const label = book.empty ? 'Starting paper balance: ' + amount(book.start)
      : 'Equity across recent closes and the latest available mark';
    svg.setAttribute('aria-label', label);
    const title = document.createElementNS(namespace, 'title');
    title.textContent = label;
    const line = document.createElementNS(namespace, 'polyline');
    line.setAttribute('points', values.map((v, index) => [3 + index * 126 / (values.length - 1), range ? 34 - (v - min) * 30 / range : 19].join(',')).join(' '));
    svg.append(title, line);
    return svg;
  }
  function pnlCard(book) {
    const card = el('article', null, 'od-pnl-card');
    const top = el('div', null, 'od-pnl-card-top');
    const title = el('div');
    title.append(el('h3', book.title + ' · Equity'), el('strong', amount(book.equity) ?? 'Equity unavailable', 'od-pnl-equity'));
    top.append(title);
    const chart = equityChart(book);
    if (chart) top.append(chart);
    const metrics = el('dl', null, 'od-pnl-metrics');
    const fields = [['Realized', amount(book.realized)], ['Unrealized', amount(book.unrealized)],
      ['Open', book.known ? book.open.length : null], ['Orders today (UTC)', book.orders]];
    for (const [name, reading] of fields) {
      const item = el('div');
      item.append(el('dt', name), el('dd', reading ?? '—'));
      metrics.append(item);
    }
    card.append(top, metrics);
    if (book.empty) card.append(el('p', 'No trades yet.', 'od-pnl-note'));
    else if (Number.isFinite(book.olderMark)) {
      const freshness = el('p', 'Oldest open mark ', 'od-pnl-note');
      freshness.append(clock(book.olderMark));
      card.append(freshness);
    }
    return card;
  }
  function deskTable(title, empty, columns, rows, values, key) {
    return rows?.length ? window.OfficeDetail.table(title, columns, rows, values, key)
      : window.OfficeDetail.section(title, el('p', empty));
  }
  function renderPnl() {
    if (!window.OfficeDetail?.table) return;
    const books = paperBooks();
    const title = el('div', null, 'od-pnl-heading');
    title.append(el('h2', 'Desk P&L'), el('p', 'Paper books · USD'));
    const tiles = el('div', null, 'od-pnl-cards');
    tiles.append(...books.map(pnlCard));
    pnl.replaceChildren(title, tiles);
    if (!books.length) pnl.append(el('p', 'Waiting for the paper ledgers.'));
    const ordered = field => books.flatMap(book => book[field]);
    const strategies = data.mk?.strategies?.strategies;
    const strategyNames = new Map((strategies ?? []).map(row => [row.strategy_id, row.name]));
    const opens = ordered('open').sort((a, b) => b.opened - a.opened);
    const closes = ordered('closes').sort((a, b) => b.closed - a.closed);
    const complete = books.length && books.every(book => book.known);
    const mark = row => {
      if (!row.mark) return null;
      const node = el('span', row.mark);
      if (Number.isFinite(row.markAt)) node.append(' · ', clock(row.markAt));
      return node;
    };
    records.replaceChildren(
      deskTable('Open positions', complete ? 'No open paper positions.' : 'Open positions are unavailable for one or more books.',
        ['Book', 'Instrument', 'Side', 'Entry', 'Mark', 'Unrealized (USD)', 'Unrealized (%)', 'Opened', 'Strategy'],
        opens, row => [row.book, row.label, row.side, row.entry, mark(row), amount(row.unrealized), returnPct(row.pct),
          clock(row.opened), strategyNames.get(row.strategy) ?? row.strategy], row => row.id),
      deskTable('Recent closes', complete ? 'No paper closes in the current ledger.' : 'Close history is unavailable for one or more books.',
        ['Book', 'Instrument', 'Side', 'Entry', 'Exit', 'Realized (USD)', 'Closed', 'Strategy'], closes,
        row => [row.book, row.label, row.side, row.entry, row.exit, amount(row.net), clock(row.closed),
          strategyNames.get(row.strategy) ?? row.strategy], row => row.id),
      deskTable('Strategies', Array.isArray(strategies) ? 'No strategies recorded.' : 'Strategy results unavailable.',
        ['Name', 'Lane', 'Owner', 'Status', 'Settled trades', 'Open trades', 'Win rate', 'Sum of net returns (% of margin)'],
        strategies?.filter(row => !row.owner || activeRoles().has(row.owner)).filter(row => row.book !== 'stocks')
          .sort((a, b) => Number(Boolean(b.owner)) - Number(Boolean(a.owner))),
        row => [row.name ?? row.strategy_id, row.book, row.owner ? seatName(row.owner) : null, row.status ?? 'Catalog',
          row.settled_n, row.open_n, row.settled_n > 0 ? returnPct(100 * row.wins / row.settled_n) : 'Unscored',
          row.settled_n > 0 ? returnPct(row.settled_sum_pct) : 'No closes'], row => row.book + ':' + row.strategy_id),
    );
    limitTables(records, 'desk-records');
  }

  let operatorChat = null;
  const chatReplies = new Map();
  const chatPath = '/api/office/operator-chat';

  function askBadge(seats) {
    const count = seats.reduce((sum, seat) => sum + (operatorChat?.open_by_seat?.[seat] ?? 0), 0);
    if (!Number.isInteger(count) || count < 1) return null;
    const badge = el('a', count + (count === 1 ? ' ask' : ' asks'), 'od-ask-badge');
    badge.href = '#room=control&item=operator-chat';
    return badge;
  }

  let toolRequests = null, readingTools = false;
  async function refreshToolRequests() {
    if (active !== 'control' || readingTools) return;
    readingTools = true;
    try {
      const response = await fetch('/api/office/tool-requests', {cache: 'no-store', signal: AbortSignal.timeout(2500)});
      toolRequests = response.ok ? await response.json() : {state: 'unknown'};
    } catch {
      toolRequests = {state: 'unknown'};
    } finally {
      readingTools = false;
      if (active === 'control') render();
    }
  }
  function toolRequestsPanel() {
    const panel = el('section');
    panel.dataset.itemId = 'tools-requests';
    panel.append(el('h3', 'Tools / Requests'));
    const queue = toolRequests;
    if (queue?.state !== 'known' || !Array.isArray(queue.items)) {
      const reasons = {
        dispositions_unavailable: 'The tool status file is unavailable.',
        dispositions_invalid: 'The tool status file could not be read.',
        requests_unavailable: 'The seat request log is unavailable.',
      };
      panel.append(el('p', queue ? queue.reason?.startsWith('dispositions_invalid:') ? 'Invalid tool status for request ' + queue.reason.split(':')[1] + '.' : reasons[queue.reason] ?? 'The tool request queue is unavailable.' : 'Reading requests…'));
      return panel;
    }
    const summary = el('p', (queue.limited ? 'Latest ' : '') + queue.items.length + (queue.items.length === 1 ? ' request · updated ' : ' requests · updated '));
    summary.append(clock(queue.as_of));
    panel.append(summary);
    if (queue.dispositions === 'unavailable') panel.append(el('p', 'Status file missing. Requests appear as New until status can be read.'));
    if (!queue.items.length) return panel;
    const labels = {new: 'New', building: 'Building', shipped: 'Shipped', refused: 'Refused'};
    panel.append(el('p', 'Shipped means the approved host source landed.'));
    const table = window.OfficeDetail.table('Request queue', ['Requested', 'Seat', 'Kind', 'Request', 'State', 'Decision', 'Tool'],
      queue.items, row => {
        const request = el('div');
        request.append(el('p', clean(row.text)));
        if (row.why) request.append(el('p', 'Why: ' + clean(row.why)));
        return [clock(row.at), row.role ? seatName(row.role) : null, clean(row.kind).replaceAll('_', ' '),
          request, labels[row.state], clean(row.reason), row.state === 'shipped' && row.tool_name ? el('code', row.tool_name) : null];
      }, row => row.id);
    panel.append(table);
    return panel;
  }

  function operatorChatPanel() {
    if (!operatorChat) return null;
    const panel = el('section');
    panel.dataset.itemId = 'operator-chat';
    panel.append(el('h3', 'Operator chat'));
    if (operatorChat.status === 'unavailable') {
      panel.append(el('p', 'Operator chat unavailable.'));
      return panel;
    }
    const roles = activeRoles();
    const asks = (operatorChat.asks ?? []).filter(ask => roles.has(ask.seat));
    if (!asks.length) panel.append(el('p', 'No asks recorded.'));
    if (operatorChat.damaged_rows) panel.append(el('p', operatorChat.damaged_rows + ' unreadable rows'));
    for (const ask of [...asks].reverse()) {
      const item = el('article');
      item.append(el('h4', '#' + ask.id + ' · ' + ask.seat + ' · ' + ask.mark),
        el('p', ask.text));
      if (ask.choices?.length) item.append(el('p', 'Choices: ' + ask.choices.join(' · ')));
      if (ask.if_unanswered) item.append(el('p', 'If unanswered: ' + ask.if_unanswered));
      if (Number.isFinite(ask.deadline_at)) {
        const due = el('p', ask.status === 'deadline_passed' ? 'Deadline passed · ' : 'Due ');
        due.append(clock(ask.deadline_at));
        item.append(due);
      }
      if (ask.answer) {
        item.append(el('p', 'Answered via ' + (ask.answer.path === 'telegram' ? 'Telegram' : 'Office')),
          el('p', ask.answer.text), clock(ask.answer.at));
        chatReplies.delete(ask.id);
      } else if (ask.mark !== 'i') {
        item.append(operatorAnswerForm(ask.id));
      }
      panel.append(item);
    }
    for (const message of [...(operatorChat.messages ?? [])].reverse()) {
      const recipients = message.seats.filter(seat => roles.has(seat));
      if (!recipients.length) continue;
      const row = el('article');
      row.append(el('h4', 'Message to ' + recipients.map(seatName).join(', ')),
        el('p', message.text), clock(message.at));
      panel.append(row);
    }
    return panel;
  }

  function operatorAnswerForm(askId) {
    const form = el('form');
    const input = el('textarea');
    input.setAttribute('aria-label', 'Answer #' + askId);
    input.required = true;
    const button = el('button', 'Send answer');
    button.type = 'submit';
    const status = el('p');
    status.setAttribute('role', 'status');
    const draft = chatReplies.get(askId) ?? {text: '', busy: false};
    input.value = draft.text;
    input.disabled = button.disabled = draft.busy || Boolean(draft.done);
    status.textContent = draft.status ?? '';
    input.addEventListener('input', () => {
      if (input.value !== draft.text) draft.id = null;
      draft.text = input.value;
      chatReplies.set(askId, draft);
    });
    form.append(input, button, status);
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (draft.busy || draft.done || !input.value.trim()) return;
      draft.text = input.value;
      draft.id ??= crypto.randomUUID();
      draft.busy = input.disabled = button.disabled = true;
      chatReplies.set(askId, draft);
      try {
        const response = await fetch(chatPath, {method: 'POST', credentials: 'same-origin',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ask_id: askId, client_id: draft.id, text: draft.text})});
        const result = await response.json();
        draft.done = response.ok || result.status === 'already_answered';
        draft.status = response.ok ? 'Answer saved.'
          : result.status === 'already_answered' ? 'Already answered via ' + (result.path === 'telegram' ? 'Telegram.' : 'Office.')
            : 'Answer refused.';
      } catch {
        draft.status = 'Delivery unknown. Retry to check.';
      } finally {
        draft.busy = false;
        input.disabled = button.disabled = Boolean(draft.done);
        status.textContent = draft.status;
        if (draft.done) refresh();
      }
    });
    return form;
  }

  function roomContent(room, mode) {
    try {
      return room[mode](data) ?? el("div");   // a room with nothing to show returns null
    } catch (error) {
      console.error('Office room failed', room.id, error);
      return el('p', unknown);
    }
  }
  const seatHome = {head: 'decision', scout: 'memes', pm_trader: 'trading', perps_trader: 'trading',
    newsdesk: 'newsroom', researcher: 'research', coach: 'coach'};
  function renderGrid() {
    const focused = document.activeElement?.dataset.room;
    const lag = Date.now() / 1000 - data.desk?.heartbeat_at;
    const live = !failed && data.desk?.state === 'running' && lag >= 0 && lag <= 900;
    grid.replaceChildren(...[...rooms.values()].filter(staffed).sort((a, b) => a.order - b.order).map(room => {
      const tile = el('article', null, 'od-tile od-floor-room');
      tile.dataset.tile = room.id;
      if (changedRooms.has(room.id)) tile.classList.add('od-new-event');
      const button = el('button', room.title, 'od-room-open');
      button.type = 'button';
      button.dataset.room = room.id;
      button.addEventListener('click', () => open(room.id, button));
      const boards = window.OfficeFloor.boards(room.id, data);
      const preferred = {decision: 'Head directions', memes: 'Candidates'};
      const panels = [...boards.children];
      for (const panel of panels) {
        for (const line of panel.querySelectorAll('.vo-board-lines p')) {
          const text = line.textContent.trim();
          if (/^(nothing recorded yet|unknown)$/i.test(text)) line.remove();
          else {
            line.textContent = text.replace(/(?:^|\s+)\S*…(?=\s|$)/g, '').trim();
            if (!line.textContent) line.remove();
          }
        }
      }
      const populated = panels.filter(panel => panel.querySelector('.od-figure, .vo-board-lines p'));
      const selected = populated.find(panel => panel.dataset.furniture === preferred[room.id]) ?? populated[0];
      boards.replaceChildren(...(selected ? [selected] : []));
      const stations = el('div', null, 'od-floor-stations');
      const everyone = (room.seats ?? []).filter(seat => activeRoles().has(seat) && seatHome[seat] === room.id);
      stations.style.setProperty('--ol-cols', String(Math.max(everyone.length, 1)));
      for (const [index, seat] of everyone.entries()) {
        if (window.OfficeFloor) {
          const station = window.OfficeFloor.station(seat, data.seats?.[seat], index,
            room.id === 'debate' ? data.cases?.[0]?.seats?.[seat]?.probability_yes : null, live, data.health?.seats?.[seat]);
          const display = {head: 'Head', perps_trader: 'Perps'}[seat];
          if (display) station.querySelector('.vo-agent-name').textContent = display;
          const reading = data.desk_now?.seats.find(row => row.role === seat);
          const tool = clean(reading?.current_tool);
          station.querySelector('.vo-seat-bubble')?.remove();
          if (reading?.status === 'working' && tool) station.append(el('span', toolLabel(tool), 'vo-seat-bubble'));
          const badge = askBadge([seat]);
          if (badge) station.append(badge);
          stations.append(station);
        }
      }
      if (room.id === 'situation') {
        const map = roomContent(room, 'summary');
        const board = el('div', null, 'od-floor-board vo-situation-wall');
        board.append(map);
        tile.append(button, board);
      } else tile.append(button, boards, stations);
      if (room.id === 'control' && data.agent_map) {
        const mapLink = el('a', 'Agent map');
        mapLink.href = '#room=control&item=agent-map';
        tile.append(mapLink);
      }

      const roomBadge = askBadge(room.seats ?? []);
      if (roomBadge) tile.append(roomBadge);
      tile.addEventListener('click', event => {
        if (!event.target.closest('a, button, input, select, textarea, summary')) open(room.id, button);
      });
      return tile;
    }));
    window.OfficeFloor.fitBoards(grid);
    if (live && incoming.size) {
      const fresh = data.activity.filter(event => canOpen(event.room) && (!event.seat || activeRoles().has(event.seat)) && incoming.has(eventKey(event)));
      const replay = [...['story', 'mail', 'tool'].flatMap(kind => fresh.filter(event => event.kind === kind).slice(0, 1)),
        ...fresh.filter(event => !['story', 'mail', 'tool'].includes(event.kind)).slice(0, 2)];
      window.OfficeFloor.replay(grid, replay.map(event => event.kind === 'tool' && event.seat
        ? {...event, room: seatHome[event.seat] ?? event.room, title: toolLabel(clean(event.title) || 'tool')} : event));
    }
    if (focused) grid.querySelector(`[data-room="${CSS.escape(focused)}"]`)?.focus({preventScroll: true});
  }
  const feedLabels = {
    headline: 'Headline', headline_matched: 'Headline', headline_unmatched: 'Headline',
    debate: 'Debate', debate_outcome: 'Debate', call_opened: 'Call', call_refused: 'Refusal',
    thesis_review: 'Review', review_raised: 'Review', trade_opened: 'Trade', trade_closed: 'Trade',
    skill_changed: 'Skill', seat_reset: 'Reset', story: 'Story', j7: 'J7', mail: 'Mail',
  };
  function renderFeed() {
    const scroll = feed.scrollTop;
    feed.replaceChildren();
    const events = Array.isArray(data.activity) ? [...data.activity].filter(e => canOpen(e.room) && (!e.seat || activeRoles().has(e.seat))).sort((a, b) => b.at - a.at).slice(0, 50) : [];
    if (!events.length) feed.append(el('p', Array.isArray(data.activity) ? 'No recorded events' : unknown));
    const rows = [];
    for (const event of events) {
      const label = feedLabels[event.kind] ?? value(event.type ?? event.kind).replaceAll('_', ' ');
      const item = el('button', null, 'od-feed-item');
      item.type = 'button';
      if (incoming.has(eventKey(event))) item.classList.add('od-new-feed');
      item.hidden = rows.length >= 10;
      const meta = el('span', null, 'od-feed-meta');
      meta.append(el('strong', label, `od-feed-type type-${label.toLowerCase()}`), clock(event.at));
      item.append(meta, el('span', window.OfficeNames.scrub(value(event.title))));
      item.addEventListener('click', () => open(event.room, item, event.item_id));
      rows.push(item);
      feed.append(item);
    }
    if (rows.length > 10) showMore(feed, rows);
    feed.scrollTop = scroll;
  }
  const chosenTab = new Map(), expandedTables = new Map();
  function scene(room) {
    const box = el('div', null, 'od-scene');
    box.setAttribute('role', 'region');
    box.setAttribute('aria-label', room.title + ' room');
    if (room.id === 'situation') {
      // the room's own detail draws the one map (with its story list); a second, compact copy here showed the world twice
    } else if (window.OfficeFloor) box.append(window.OfficeFloor.boards(room.id, data));
    const people = el('div', null, 'od-scene-people');
    for (const [index, seat] of (room.seats ?? []).filter(role => activeRoles().has(role)).entries()) {
      if (!window.OfficeFloor) break;
      const reading = data.seats?.[seat], person = el('div', null, 'od-scene-person');
      const live = (data.desk?.heartbeat_at ?? 0) > Date.now() / 1000 - 900;
      person.append(window.OfficeFloor.station(seat, reading, index, null, live, data.health?.seats?.[seat]));
      const said = el('p', null, 'od-scene-said');
      said.append(el('strong', window.OfficeFloor.seatName(seat)));
      if (reading?.last_action) said.append(' ' + String(reading.last_action).replaceAll('_', ' '));
      person.append(said);
      const badge = askBadge([seat]);
      if (badge) said.append(' ', badge);
      people.append(person);
    }
    if (people.children.length) box.append(people);
    return box;
  }
  const rowLimit = 10;
  function showMore(container, rows, reveal = rowLimit, onChange = () => {}) {
    const more = el('button', null, 'od-show-all');
    more.type = 'button';
    function update() {
      rows.forEach((row, index) => { row.hidden = index >= reveal; });
      more.hidden = reveal >= rows.length;
      more.textContent = 'Show more (' + Math.min(rowLimit, Math.max(0, rows.length - reveal)) + ')';
    }
    more.addEventListener('click', () => { reveal += rowLimit; onChange(reveal); update(); });
    container.append(more);
    update();
  }
  function limitTables(panel, key) {
    for (const [index, table] of [...panel.querySelectorAll('table')].entries()) {
      const rows = [...table.tBodies].flatMap(body => [...body.rows]);
      const id = key + '|' + index;
      const wrap = el('div', null, 'od-table-wrap');
      wrap.tabIndex = 0;
      wrap.setAttribute('role', 'region');
      wrap.setAttribute('aria-label', 'Scrollable table');
      table.replaceWith(wrap);
      wrap.append(table);
      if (rows.length <= rowLimit) continue;
      showMore(wrap, rows, expandedTables.get(id) ?? rowLimit, count => expandedTables.set(id, count));
    }
  }
  const diagnostic = /^(tools used|pages read|activity|timed jobs|universe|pipeline state|wakes|services|book events|feeds?|markets feeds health|news sources|halts and retirements)\b/i;
  function renderDetail() {
    const room = rooms.get(active);
    const view = detail.querySelector('.od-detail-scroll');
    const content = roomContent(room, 'detail');
    if (active === 'control') {
      content.prepend(toolRequestsPanel());
      const chat = operatorChatPanel();
      if (chat) content.append(chat);
    }
    const sections = [...content.children].filter(node => node.tagName === 'SECTION' && node.querySelector(':scope > h3'));
    const body = el('div', null, 'od-room-body');
    view.replaceChildren(scene(room), body);
    const rest = [...content.children].filter(node => !sections.includes(node) && node.textContent.trim());
    const diagnostics = sections.filter(node => diagnostic.test(node.querySelector(':scope > h3').textContent));
    if (diagnostics.length) {
      const more = el('details', null, 'od-room-diagnostics');
      more.append(el('summary', 'Diagnostics'), ...diagnostics);
      body.after(more);
      limitTables(more, room.id + '|diagnostics');
      for (const node of diagnostics) sections.splice(sections.indexOf(node), 1);
    }
    if (sections.length < 2) {
      const panel = el('div', null, 'od-room-panel');
      panel.append(content);
      limitTables(panel, room.id);
      body.append(panel);
      return;
    }
    const tabs = el('div', null, 'od-tabs'), panel = el('div', null, 'od-room-panel');
    tabs.setAttribute('role', 'tablist');
    tabs.setAttribute('aria-label', 'Inside ' + room.title);
    panel.setAttribute('role', 'tabpanel');
    panel.id = 'od-room-panel';
    const names = sections.map(node => node.querySelector(':scope > h3').textContent);
    const item = new URLSearchParams(location.hash.slice(1)).get('item');
    const linked = item != null && !chosenTab.has(room.id) ? sections.findIndex(node => node.dataset.itemId === item || [...node.querySelectorAll('[data-item-id]')].some(n => n.dataset.itemId === item)) : -1;
    const selected = Math.max(0, linked >= 0 ? linked : names.indexOf(chosenTab.get(room.id)));
    if (linked >= 0) chosenTab.set(room.id, names[linked]);
    const choose = index => {
      chosenTab.set(room.id, names[index]);
      renderDetail();
      detail.querySelectorAll('.od-tabs [role="tab"]')[index]?.focus({preventScroll: true});
    };
    names.forEach((name, index) => {
      const tab = el('button', name);
      tab.type = 'button';
      tab.setAttribute('role', 'tab');
      tab.setAttribute('aria-selected', String(index === selected));
      tab.setAttribute('aria-controls', 'od-room-panel');
      tab.tabIndex = index === selected ? 0 : -1;
      tab.addEventListener('click', () => choose(index));
      tab.addEventListener('keydown', event => {
        const step = {ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1}[event.key];
        if (step) { event.preventDefault(); choose((index + step + names.length) % names.length); }
      });
      tabs.append(tab);
    });
    const shown = el('div', null, 'od-room-section');
    shown.append(sections[selected]);
    panel.append(shown);
    limitTables(panel, room.id + '|' + names[selected]);
    if (rest.length && selected === 0) panel.append(...rest);
    if (names.length > 8) body.classList.add('od-side-index');
    body.append(tabs, panel);
  }
  function open(id, trigger, itemId, syncHash = true) {
    if (!canOpen(id)) return;
    if (!active) roomScroll = window.scrollY;
    opener = trigger;
    if (syncHash) {
      const params = new URLSearchParams({room: id});
      if (itemId != null) params.set('item', itemId);
      history.pushState(null, '', '#' + params);
    }
    active = id;
    const back = el('button', 'Back to Desk now');
    back.type = 'button';
    back.addEventListener('click', () => close());
    const bar = el('div', null, 'od-detail-bar');
    bar.append(back, el('h2', rooms.get(id).title));
    chosenTab.delete(id);
    detail.replaceChildren(bar, el('div', null, 'od-detail-scroll'));
    floor.hidden = seats.hidden = pnl.hidden = records.hidden = true;
    detail.hidden = false;
    renderDetail();
    window.scrollTo(0, 0);
    back.focus({preventScroll: true});
    if (id === 'control') refreshToolRequests();
    if (itemId != null) {
      const target = [...detail.querySelectorAll('[data-item-id]')].find(node => node.dataset.itemId === String(itemId));
      target?.scrollIntoView({block: 'nearest'});
    }
  }
  function close(syncHash = true) {
    if (!active) return;
    if (syncHash) history.pushState(null, '', location.pathname + location.search);
    const id = active;
    active = null;
    detail.hidden = true;
    floor.hidden = seats.hidden = pnl.hidden = records.hidden = false;
    renderPnl();
    renderSeats();
    renderGrid();
    renderFeed();
    (opener?.isConnected ? opener : grid.querySelector(`[data-room="${CSS.escape(id)}"]`))?.focus({preventScroll: true});
    window.scrollTo(0, roomScroll);
    opener = null;
  }
  function followHash() {
    const params = new URLSearchParams(location.hash.slice(1));
    const room = params.get('room');
    if (canOpen(room)) open(room, null, params.get('item'), false);
    else close(false);
  }
  function render() {
    renderHeader();
    if (active && !canOpen(active)) close(false);
    if (active) {
      renderFeed();
      const view = detail.querySelector('.od-detail-scroll');
      const scroll = view.scrollTop;
      const selection = document.activeElement;
      if (!view.contains(selection) || selection.closest('.od-tabs')) {
        const tabFocus = selection.closest?.('.od-tabs') ? true : false;
        const pageScroll = window.scrollY;
        renderDetail();
        view.scrollTop = scroll;
        window.scrollTo(0, pageScroll);
        if (tabFocus) detail.querySelector('.od-tabs [aria-selected="true"]')?.focus({preventScroll: true});
        const item = new URLSearchParams(location.hash.slice(1)).get('item');
        if (item != null && scroll === 0) {
          [...view.querySelectorAll('[data-item-id]')].find(node => node.dataset.itemId === item)?.scrollIntoView({block: 'nearest'});
        }
      }
    } else {
      renderPnl();
      renderSeats();
      renderGrid();
      renderFeed();
    }
  }
  async function adaptDesk(response) {
    const raw = await response.json();
    if (!raw.desk_now && raw.seats) {
      // The running server may still be on the previous API during a rolling UI update.
      const layout = await fetch('/rooms.json', {cache: 'no-store'});
      if (layout.ok) {
        raw.rooms = (await layout.json()).rooms;
        const paused = raw.rooms?.control ?? [];
        const roles = new Set(Object.values(raw.rooms ?? {}).flat().filter(role => !paused.includes(role)));
        raw.desk_now = {as_of: raw.as_of, state: 'limited', paused, seats: [...roles].map(role => ({
          role, status: raw.seats[role]?.status, sleep_until: raw.seats[role]?.sleep_until, parked_reason: raw.seats[role]?.parked_reason,
          current_tool: raw.seats[role]?.status === 'working' ? raw.seats[role]?.last_tool : null,
        }))};
      }
    }
    return window.OfficeAdapter.adapt(raw);
  }
  async function refresh() {
    if (busy) return;
    busy = true;
    const firstRead = !seen;
    try {
      const response = await fetch('/api/office/newsroom', {cache: 'no-store'});
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      data = await adaptDesk(response);
      for (const room of rooms.values()) room.seats = data.rooms?.[room.id] ?? [];
      try {
        const privateResponse = await fetch(chatPath, {cache: 'no-store', credentials: 'same-origin',
          signal: AbortSignal.timeout(2500)});
        operatorChat = privateResponse.ok ? await privateResponse.json()
          : privateResponse.status >= 500 ? {status: 'unavailable'} : null;
      } catch {
        operatorChat = null;
      }
      const events = Array.isArray(data.activity) ? data.activity : [];
      incoming = new Set();
      changedRooms = new Set();
      if (seen) {
        for (const event of events) {
          const key = eventKey(event);
          if (seen.has(key)) continue;
          incoming.add(key);
          changedRooms.add(event.room);
        }
      } else {
        incoming = new Set(events.slice(0, 5).map(eventKey));
      }
      seen = new Set(events.map(eventKey));
      failed = false;
    } catch {
      failed = true;
    } finally {
      busy = false;
      render();
      if (firstRead && !active && location.hash) followHash();
      if (active === 'control') refreshToolRequests();
      incoming.clear();
      changedRooms.clear();
    }
  }
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && active) close();
  });
  window.addEventListener('hashchange', followHash);
  new ResizeObserver(() => window.OfficeFloor.fitBoards(grid)).observe(grid);
  new ResizeObserver(fitActions).observe(seats);
  document.fonts.ready.then(() => { window.OfficeFloor.fitBoards(grid); fitActions(); });
  render();
  followHash();
  refresh();
  setInterval(refresh, 15000);
})();
