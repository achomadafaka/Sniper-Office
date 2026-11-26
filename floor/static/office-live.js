(() => {
  'use strict';
  // The newer rooms of the floor: the markets plug-in (newsroom, situation, sports, strategy lab, paper labs, explorer, memes, coach, video lab, control), the J7 copy lane and its 1 s ticks,
  // the books with live PnL, and the animations (J7 ping, routed-story cards, mail between desks). Every value comes from the host's read-only routes; nothing is invented, and
  // whatever a source does not hold is said to be unknown. All text from feeds and seats goes in with textContent (the helpers below), never as markup.
  const D = window.OfficeDetail, {table, section, block, line, time, text} = D;
  const N = window.OfficeNames;
  const fin = v => typeof v === 'number' && Number.isFinite(v);
  const el = (tag, content, cls) => { const n = document.createElement(tag); if (content != null) n.textContent = content; if (cls) n.className = cls; return n; };
  const short = (value, n = 80) => { const c = N.scrub(String(value ?? '')).replace(/\s+/g, ' ').trim(); return c.length > n ? c.slice(0, n - 1).trimEnd() + '…' : c; };
  const usd = (n, d = 2) => fin(n) ? (n < 0 ? '-$' : '+$') + Math.abs(n).toFixed(d) : null;
  const money = (n, d = 2) => fin(n) ? '$' + n.toFixed(d) : null;
  const pct = (n, d = 1) => fin(n) ? (n > 0 ? '+' : '') + n.toFixed(d) + '%' : null;
  const cents = n => fin(n) ? (100 * n).toFixed(1) + '¢' : null;
  const px = n => !fin(n) ? null : n >= 1000 ? n.toFixed(1) : n >= 1 ? n.toFixed(3) : n.toPrecision(4);
  const tone = n => !fin(n) || n === 0 ? '' : n > 0 ? 'ol-pos' : 'ol-neg';
  const human = v => String(v ?? '').replaceAll('_', ' ');
  const ageText = at => {
    if (!fin(at)) return null;
    const s = Math.max(0, Date.now() / 1000 - at);
    return s < 60 ? Math.floor(s) + ' s ago' : s < 3600 ? Math.floor(s / 60) + ' min ago' : s < 86400 ? Math.floor(s / 3600) + ' h ago' : Math.floor(s / 86400) + ' d ago';
  };
  const ageOf = s => fin(s) ? (s < 60 ? Math.floor(s) + ' s' : s < 3600 ? Math.floor(s / 60) + ' min' : Math.floor(s / 3600) + ' h') : null;
  const nameOf = seat => window.OfficeFloor.seatName(seat);
  const pts = x => (x >= 0 ? '+' : '') + x.toFixed(1);
  const mark = m => !m ? null : m.state === 'pending' ? 'waiting' : m.state === 'censored' ? 'no price' : fin(m.call_pp) ? pts(m.call_pp) + ' pp' + (fin(m.control_pp) ? ' (control ' + pts(m.control_pp) + ')' : '') : m.state;
  const CALL_COLS = [['When', r => when(r.at)], ['By', r => nameOf(r.role)], ['Market', r => short(r.question, 90)], ['Side', r => r.side], ['Entry ask', r => cents(r.entry_ask)], ['Expected', r => fin(r.expected_move_cents) ? '+' + r.expected_move_cents + '¢ in ' + r.horizon : r.horizon], ['15 min', r => mark(r.marks?.['15m'])], ['1 h', r => mark(r.marks?.['1h'])], ['6 h', r => mark(r.marks?.['6h'])], ['24 h', r => mark(r.marks?.['24h'])], ['Resolved', r => fin(r.resolved_pp) ? r.resolved_pp.toFixed(1) + ' pp' : null]];
  const colored = (value, n) => { const s = el('span', value ?? 'unknown'); if (tone(n)) s.className = tone(n); return s; };
  const getJson = async url => { try { const r = await fetch(url, {cache: 'no-store'}); return r.ok ? await r.json() : null; } catch { return null; } };
  const reduced = () => matchMedia('(prefers-reduced-motion: reduce)').matches;

  // ---- live state: the copy lane, polled about once a second
  const S = {d: null, as_of: null, books: null, positions: null, signals: null, resolution: null, ticks: new Map(), since: null, seen: null, ok: null, lastOk: null, cells: new Map()};
  const sigKey = s => s.chain + ':' + s.token;
  const posKey = p => p.chain + ':' + p.mint;
  let busy = false;
  async function poll() {
    if (document.hidden || busy) return;
    busy = true;
    try {
      const doc = await getJson('/api/office/live' + (S.since ? '?since=' + S.since : ''));
      if (!doc) throw new Error('live read failed');
      S.ok = true;
      S.lastOk = Date.now() / 1000;
      S.books = doc.books; S.positions = doc.positions; S.signals = doc.signals; S.resolution = doc.resolution ?? S.resolution; S.as_of = doc.as_of; S.state = doc.state; S.tickAt = doc.ticks_last_at ?? null;
      let newest = S.since ?? 0, full = false;
      for (const [key, rows] of Object.entries(doc.ticks ?? {})) {
        const have = S.since ? S.ticks.get(key) : null;
        const merged = have ? have.concat(rows.filter(r => r[0] > (have.at(-1)?.[0] ?? 0))) : rows;
        S.ticks.set(key, merged.slice(-1500));
        if (rows.length) newest = Math.max(newest, rows.at(-1)[0]);
      }
      const wanted = [...(S.positions ?? []).map(posKey), ...(S.signals ?? []).filter(s => s.side === 'buy').slice(0, 10).map(sigKey)];
      full = wanted.some(k => !S.ticks.has(k)) && !S.since;
      S.since = newest ? newest - 1 : null;
      if (wanted.some(k => !S.ticks.has(k))) S.since = null;       // a coin we have no history for: the next read is a full one
      await N.ensure([...(S.positions ?? []).map(p => [p.chain, p.mint]), ...(S.signals ?? []).map(s => [s.chain, s.token])], 400);
      announce();
      paintLive();
    } catch {
      S.ok = false;
      for (const n of document.querySelectorAll('.ol-livestate')) n.textContent = 'live read failing';
    } finally {
      busy = false;
    }
  }
  // New signals: ping the copy desk, and put them at the top of the live feed.
  function announce() {
    const list = S.signals ?? [];
    if (!S.seen) {
      S.seen = new Set(list.map(s => s.signal_id));
      for (const s of list.filter(x => x.side === 'buy' && S.as_of - x.recv_at < 20).reverse()) ping(s);
      return;
    }
    for (const s of [...list].reverse()) {
      if (S.seen.has(s.signal_id)) continue;
      S.seen.add(s.signal_id);
      if (s.side === 'buy') { ping(s); feedItem(s); }      // a buy is the signal the copy desk is asked about; a sell is only stored
    }
  }
  const coinText = s => N.text(s.chain, s.token, s.symbol);
  const sigLine = s => [s.handle ?? 'trader', s.chain, coinText(s), money(s.usd, 0), s.side === 'buy' ? 'buy' : s.side].filter(Boolean).join(' · ');
  function ping(s) {
    for (const station of document.querySelectorAll('[data-station="copy_desk"]')) {
      station.classList.add('ol-ping');
      for (const old of station.querySelectorAll('.ol-j7-bubble')) old.remove();
      const bubble = el('span', sigLine(s), 'vo-seat-bubble ol-j7-bubble');
      bubble.title = sigLine(s);
      station.append(bubble);
      setTimeout(() => { bubble.remove(); station.classList.remove('ol-ping'); }, 6500);
    }
  }
  function feedItem(s) {
    const feed = document.querySelector('.od-feed');
    if (!feed) return;
    const item = el('button', null, 'od-feed-item od-new-feed');
    item.type = 'button';
    const meta = el('span', null, 'od-feed-meta');
    meta.append(el('strong', 'J7', 'od-feed-type type-j7'), el('time', 'just now'));
    item.append(meta, el('span', sigLine(s)));
    item.addEventListener('click', () => { location.hash = 'room=trading'; });
    feed.querySelector('h2')?.after(item);
    for (const extra of [...feed.querySelectorAll('.od-feed-item')].slice(60)) extra.remove();
  }

  const hasSeat = (d, role) => Array.isArray(d.desk_now?.seats)
    ? d.desk_now.seats.some(seat => seat.role === role) : Boolean(d.seats?.[role]);
  const traderRole = {polymarket: 'pm_trader', perps: 'perps_trader', stocks: 'stocks_trader'};
  const staffedLane = (d, lane) => hasSeat(d, traderRole[lane]);
  const staffedBooks = d => (d.mk?.paper_labs?.books ?? []).filter(book => staffedLane(d, book.lane));

  // ---- totals across the books (the header strip and the Books table)
  function bookRows(d) {
    const rows = [];
    for (const b of hasSeat(d, 'copy_desk') ? S.books ?? [] : []) rows.push({key: 'book:' + b.book_id, book: b.book_id, lane: 'memecoins (J7 copy)', seat: 'copy_desk', equity: b.equity_usd, realized: b.realized_usd, unrealized: b.unrealized_usd, open: b.open_n, unpriced: b.unpriced_n, live: true});
    for (const [, book] of (hasSeat(d, 'judge') || hasSeat(d, 'judge_gpt') ? d.floor?.book_list ?? [] : []).filter(([key]) => key.startsWith('book:'))) {
      const sum = d['book:' + book]?.summary;
      if (sum && !rows.some(r => r.book === book)) rows.push({key: 'ledger:' + book, book, lane: 'memecoins (judges)', realized: sum.net_usd, unrealized: null, open: sum.open, closed: sum.scored, wins: sum.wins, judge: true});
    }
    for (const b of staffedBooks(d)) rows.push({key: 'mk:' + b.book_id, book: b.book_id, lane: b.lane, equity: b.equity, cash: b.cash, realized: b.realized_total, unrealized: b.unrealized_total, open: (b.open ?? []).length, closed: b.closed_n, wins: b.wins, halted: b.halted, start: b.start_cash});
    return rows;
  }
  function totals(d) {
    const rows = bookRows(d);
    const known = rows.filter(r => fin(r.realized) || fin(r.unrealized));
    // a book with open positions it could not price has no unrealized figure: the total says so rather than counting it as zero
    const unpriced = rows.filter(r => (r.open > 0 && !fin(r.unrealized)) || r.unpriced > 0).length;
    const total = known.reduce((n, r) => n + (r.realized ?? 0) + (r.unrealized ?? 0), 0);
    return {rows, total: known.length ? total : null, unpriced, n: known.length};
  }
  const pnlText = d => { const t = totals(d); return t.total == null ? 'unknown' : usd(t.total) + (t.unpriced ? ' (' + t.unpriced + ' book' + (t.unpriced > 1 ? 's' : '') + ' unpriced)' : ''); };
  const tickText = () => !S.ok || !fin(S.tickAt) || !fin(S.as_of) ? 'unknown' : 'newest ' + Math.max(0, S.as_of - S.tickAt).toFixed(1) + ' s old';
  function j7Hour() {
    const list = (S.signals ?? []).filter(s => !fin(S.as_of) || s.recv_at > S.as_of - 3600);
    return S.signals == null ? 'unknown' : list.filter(s => s.side === 'buy').length + ' buys / ' + list.length + ' stored';
  }
  const ROOM_OF = {polymarket: 'debate', memes: 'memes', sports: 'sports', stocks: 'trading', crypto: 'trading', perps: 'trading', other: 'newsroom'};
  const roomLabel = {debate: 'Debate', memes: 'Memes', sports: 'Sports', trading: 'Trading', newsroom: 'Newsroom'};
  function routed(d) {
    const wire = d.mk?.newsroom?.wire;
    return Array.isArray(wire) ? wire.filter(w => w.routes?.length && (w.age_s ?? 0) < 86400).length : null;
  }
  function header(d) {
    S.d = d;
    const g = d.svc?.guard, guard = !g ? 'unknown' : g.state === 'ready' ? 'ready' : g.state === 'paused' ? 'paused' + (g.reason ? ': ' + short(g.reason, 40) : '') : g.state;
    const r = routed(d);
    return [['PnL, all books', pnlText(d), 'hdr:pnl'], ['J7 signals, last hour', j7Hour(), 'hdr:j7'], ['J7 ticks', tickText(), 'hdr:tick'], ['Stories routed, 24 h', r == null ? 'unknown' : String(r)], ['Host guard', guard]];
  }
  // Rewrites every live value in place (header, tables, charts) from the newest 1 s read.
  function liveValue(spec) {
    const [kind, id, field] = spec.split(':');
    if (spec === 'hdr:pnl') return S.d ? pnlText(S.d) : null;
    if (spec === 'hdr:j7') return j7Hour();
    if (spec === 'hdr:tick') return tickText();
    const row = kind === 'book' ? (S.books ?? []).find(b => b.book_id === id) : kind === 'pos' ? (S.positions ?? []).find(p => p.order_id === id) : null;
    if (!row) return null;
    const v = row[field];
    return /pct/.test(field) ? pct(v) : /price/.test(field) ? px(v) : /_n$/.test(field) ? (fin(v) ? String(v) : null) : /usd/.test(field) && /unrealized|total|realized/.test(field) ? usd(v) : money(v);
  }
  function paintLive() {
    for (const node of document.querySelectorAll('[data-live]')) {
      const v = liveValue(node.dataset.live);
      if (v != null && node.textContent !== v) node.textContent = v;
      const row = node.dataset.live.split(':');
      const n = row[0] === 'hdr' ? null : (row[0] === 'book' ? (S.books ?? []).find(b => b.book_id === row[1]) : (S.positions ?? []).find(p => p.order_id === row[1]))?.[row[2]];
      if (fin(n) && /usd|pct/.test(row[2]) && !/equity|cost|value|price|qty/.test(row[2])) node.classList.toggle('ol-pos', n > 0), node.classList.toggle('ol-neg', n < 0);
    }
    for (const card of document.querySelectorAll('.ol-chart')) drawChart(card);
    for (const n of document.querySelectorAll('.ol-livestate')) {
      const r = S.resolution;
      n.textContent = S.ok === false ? 'live read failing' : 'polled each second' + (r ? ' · poll gap ' + (fin(r.poll_gap_median_s) ? r.poll_gap_median_s.toFixed(2) : '?') + ' s · provider price changes about every ' + (fin(r.price_gap_median_s) ? Math.round(r.price_gap_median_s) : '?') + ' s' : '');
    }
  }

  // ---- the live price chart: an SVG line from the 1 s ticks, redrawn in place on every read
  const SVG = 'http://www.w3.org/2000/svg';
  const svgEl = (tag, attrs = {}) => { const n = document.createElementNS(SVG, tag); for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v); return n; };
  const W = 640, H = 150, PAD = 6;
  function chart({key, title, address, entry, sl, tp, signalAt, windowS = 600, note}) {
    const card = el('div', null, 'ol-chart');
    Object.assign(card.dataset, {key, window: windowS});
    if (fin(entry)) card.dataset.entry = entry;
    if (fin(sl)) card.dataset.sl = sl;
    if (fin(tp)) card.dataset.tp = tp;
    if (fin(signalAt)) card.dataset.signal = signalAt;
    const head = el('div', null, 'ol-chart-head');
    head.append(title, ' ', el('span', null, 'ol-chart-last'));
    const svg = svgEl('svg', {viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': 'Live price from one-second ticks', preserveAspectRatio: 'none'});
    for (const cls of ['ol-sig', 'ol-tp', 'ol-sl', 'ol-entry']) svg.append(svgEl('line', {class: cls, x1: 0, x2: 0, y1: 0, y2: 0, visibility: 'hidden'}));
    svg.append(svgEl('polyline', {class: 'ol-line', fill: 'none', points: ''}), svgEl('g', {class: 'ol-moves'}), svgEl('circle', {class: 'ol-dot', r: 3.5, cx: -10, cy: -10}));
    const labels = el('div', null, 'ol-chart-labels');
    card.append(head, svg, labels, el('p', note ?? '', 'ol-chart-note'));
    drawChart(card);
    return card;
  }
  function drawChart(card) {
    const rows = S.ticks.get(card.dataset.key) ?? [];
    const svg = card.querySelector('svg'), line = svg.querySelector('.ol-line'), dot = svg.querySelector('.ol-dot'), labels = card.querySelector('.ol-chart-labels');
    const win = Number(card.dataset.window) || 600;
    if (rows.length < 2) {
      line.setAttribute('points', ''); dot.setAttribute('cx', -10);
      card.querySelector('.ol-chart-last').textContent = rows.length ? '' : 'no ticks stored for this coin yet';
      labels.textContent = '';
      return;
    }
    const t1 = rows.at(-1)[0], sigAt = Number(card.dataset.signal);
    const t0 = fin(sigAt) && t1 - sigAt < win ? sigAt - 20 : t1 - win;
    const shown = rows.filter(r => r[0] >= t0);
    const prices = shown.map(r => r[1]);
    let lo = Math.min(...prices), hi = Math.max(...prices);      // the range is the prices; entry, tp and sl are drawn only when they fall inside it, else named in the caption
    if (hi === lo) { hi *= 1.001; lo *= 0.999; }
    const pad = (hi - lo) * 0.08;
    lo -= pad; hi += pad;
    const x = t => PAD + (W - 2 * PAD) * (t - t0) / Math.max(1, t1 - t0);
    const y = p => PAD + (H - 2 * PAD) * (1 - (p - lo) / (hi - lo));
    line.setAttribute('points', shown.map(r => x(r[0]).toFixed(1) + ',' + y(r[1]).toFixed(1)).join(' '));
    const last = shown.at(-1);
    dot.setAttribute('cx', x(last[0]).toFixed(1)); dot.setAttribute('cy', y(last[1]).toFixed(1));
    const moves = svg.querySelector('.ol-moves');
    moves.replaceChildren(...shown.filter(r => r[2]).map(r => svgEl('circle', {class: 'ol-move', r: 2, cx: x(r[0]).toFixed(1), cy: y(r[1]).toFixed(1)})));
    const notes = [];
    for (const [cls, k, name] of [['ol-entry', 'entry', 'entry'], ['ol-tp', 'tp', 'tp'], ['ol-sl', 'sl', 'sl']]) {
      const level = Number(card.dataset[k]), l = svg.querySelector('.' + cls);
      if (!fin(level) || card.dataset[k] === '') { l.setAttribute('visibility', 'hidden'); continue; }
      if (level >= lo && level <= hi) {
        l.setAttribute('visibility', 'visible'); l.setAttribute('x1', PAD); l.setAttribute('x2', W - PAD); l.setAttribute('y1', y(level).toFixed(1)); l.setAttribute('y2', y(level).toFixed(1));
        notes.push(name + ' ' + px(level));
      } else { l.setAttribute('visibility', 'hidden'); notes.push(name + ' ' + px(level) + (level > hi ? ' (above the chart)' : ' (below the chart)')); }
    }
    const sig = svg.querySelector('.ol-sig');
    if (fin(sigAt) && sigAt >= t0) { sig.setAttribute('visibility', 'visible'); sig.setAttribute('x1', x(sigAt)); sig.setAttribute('x2', x(sigAt)); sig.setAttribute('y1', 0); sig.setAttribute('y2', H); notes.push('signal'); } else sig.setAttribute('visibility', 'hidden');
    card.querySelector('.ol-chart-last').textContent = `${px(last[1])} · ${shown.length} ticks · last ${Math.max(0, (Date.now() / 1000 - last[0])).toFixed(0)} s ago`;
    labels.textContent = [px(hi) + ' high', px(lo) + ' low', ...notes].join(' · ');
  }

  // ---- table helpers: unknown vs. empty is always said
  // a feed that fails with a network error on a host that cannot reach it is a known external block, not a fault of ours
  const EXTERNAL = {polymarket: 'Polymarket unreachable from this host (external block)'};
  const feedState = f => f.ok ? 'ok' : EXTERNAL[f.feed] && /timed out|URLError|unreachable|refused|unreachable/i.test(f.detail ?? '') ? 'external' : 'down';
  const feedLabel = f => feedState(f) === 'external' ? EXTERNAL[f.feed] : null;
  function T(title, rows, cols, empty, id, why) {
    if (rows == null) return section(title, el('p', 'Unknown: ' + (why ?? 'this source could not be read.')));
    if (!rows.length) return section(title, el('p', empty ?? 'Nothing recorded yet.'));
    return table(title, cols.map(c => c[0]), rows, r => cols.map(c => c[1](r)), id ?? undefined);
  }
  const cell = v => v == null || v === '' ? null : v;
  const when = at => fin(at) ? time(at) : null;
  const json = v => { try { return typeof v === 'string' ? JSON.parse(v) : v; } catch { return null; } };
  const flat = v => Array.isArray(v) ? v.filter(item => typeof item === 'string').join(', ')
    : typeof v === 'number' || typeof v === 'string' ? v : null;
  function eventDetail(row) {
    const detail = json(row.detail_json);
    if (typeof detail?.reason === 'string') return short(human(detail.reason), 140);
    if (row.kind === 'strategy_retired') return 'Automatic retirement after ' + (detail?.n ?? 'an unknown number of') + ' closes';
    return typeof detail?.instrument === 'string' ? detail.instrument : null;
  }

  // ---- rooms
  const noMarkets = (d, key) => section('Markets', el('p', 'Unknown: ' + (d.mkUnknown?.[key] ?? d.mk?.[key]?.reason
    ?? (d.mk ? 'this room source could not be read.' : 'the markets database is not readable.'))));
  function newsroom(d) {
    const n = d.mk?.newsroom;
    if (n?.state === 'unknown' || !Array.isArray(n?.wire)) return [noMarkets(d, 'newsroom')];
    const titleOf = id => n.wire.find(w => w.news_id === id)?.title ?? id;
    const debated = Object.entries(n.debates ?? {});
    return [
      T('Stories', n.wire, [['When', r => when(r.fetched_at)], ['Source', r => r.source], ['Story', r => short(r.title, 200)], ['Routed to', r => r.routes.map(x => x.room + ' ' + Math.round(x.score)).join(', ') || 'not routed'], ['Also from', r => r.also_from?.length ? r.also_from.join(', ') : null]],
        'No stories fetched yet.', r => r.news_id),
      section('Debates', ...(debated.length ? debated.map(([id, turns]) => { const box = block(el('h4', short(titleOf(id), 120))); for (const t of turns) box.append(line(nameOf(t.role), t.stance, short(t.text, 400), ageText(t.at))); return box; }) : [el('p', 'No story debates recorded yet.')])),
      T('News calls (forced picks with a control arm)', n.news_calls, CALL_COLS, 'No news calls yet.'),
      T('Calls on stories (open positions naming a story)', n.calls, [['Market', r => r.label ?? r.instrument], ['Side', r => r.side], ['Entry', r => px(r.entry_price)], ['Live', r => r.live?.state === 'unknown' ? 'unpriced' : money(r.live?.value)], ['Unrealized', r => colored(usd(r.live?.unrealized), r.live?.unrealized)], ['Lane', r => r.lane]], 'No open position names a story.'),
      Array.isArray(n.sources) ? T('News sources', n.sources, [['Source', r => r.source], ['Newest story', r => ageOf(r.age_s)], ['Stories, 24 h', r => r.n_24h]], 'No source has a story.') : null,
      T('Feeds', n.feeds, [['Feed', r => r.feed], ['Age', r => ageOf(r.age_s)], ['OK', r => feedState(r) === 'ok' ? 'yes' : feedState(r) === 'external' ? 'external block' : 'NO'], ['Detail', r => feedLabel(r) ?? short(r.detail, 100)]], 'No feed has reported.'),
    ];
  }
  function debate(d) {
    const n = d.mk?.newsroom;
    if (n?.state === 'unknown' || !Array.isArray(n?.wire)) return [noMarkets(d, 'newsroom')];
    const debated = Object.entries(n.debates ?? {});
    return [section('Story debates (newsdesk, trader, critic)', ...(debated.length ? debated.slice(0, 12).map(([id, turns]) => { const box = block(el('h4', short(n.wire.find(w => w.news_id === id)?.title ?? id, 120))); for (const t of turns) box.append(line(nameOf(t.role), t.stance, short(t.text, 400), ageText(t.at))); return box; }) : [el('p', 'No story debates recorded yet.')]))];
  }
  function sports(d) {
    const s = d.mk?.sports;
    if (!s) return [noMarkets(d, 'sports')];
    const prob = value => fin(value) ? (100 * value).toFixed(1) + '%' : null;
    const points = value => fin(value) ? value.toFixed(1) + ' pp' : null;
    const empty = s.edges_status?.state === 'unknown'
      ? 'Bookmaker lines are unavailable (' + human(s.edges_status.reason) + ').'
      : 'No fresh pregame matches with eligible Polymarket quotes.';
    const comparisons = (s.edges ?? []).filter(r => r.kind === 'model_comparison');
    const pairs = (s.edges ?? []).filter(r => r.kind === 'book_arb');
    return [
      T('Games', s.games, [['Start', r => when(r.start_ts)], ['League', r => r.league], ['Away', r => r.away], ['Home', r => r.home],
        ['State', r => r.state], ['Score', r => fin(r.home_score) && fin(r.away_score) ? r.away_score + ' - ' + r.home_score : null],
        ['Home implied (first book, with vig)', r => prob(r.home_imp)], ['Away implied (first book, with vig)', r => prob(r.away_imp)],
        ['Fair home', r => prob(r.fair_home)], ['Fair draw', r => prob(r.fair_draw)], ['Fair away', r => prob(r.fair_away)],
        ['Bookmaker', r => r.odds_provider]], 'No games fetched in the last hour.'),
      T('Pregame bookmaker comparison', comparisons, [['Market', r => short(r.question, 105)], ['Team', r => r.team], ['Token side', r => r.side],
        ['Fair', r => prob(r.fair)], ['Ask', r => cents(r.ask)], ['Spread', r => points(100 * r.spread)], ['Entry fee', r => cents(r.fee)],
        ['Hold gap after entry fee', r => colored(points(r.gap_pp), r.gap_pp)], ['Fair minus ask', r => points(r.gross_gap_pp)],
        ['3× round-trip cost hurdle', r => points(r.cost_hurdle_pp)], ['Quote age', r => ageOf(r.quote_age_s)],
        ['Line polled', r => ageOf(r.line_age_s)], ['Line unchanged', r => ageOf(r.line_unchanged_s)], ['PM midpoint move', r => points(r.pm_move_pp) ?? 'Unknown'], ['Bookmakers', r => r.provider],
        ['Screen', r => [r.qualifies ? 'Pass' : r.bookmaker_n < r.min_bookmakers ? 'Too few bookmakers' : 'Below screen', ...r.low_trust_reasons.map(human)].join(' · ')]], empty),
      pairs.length ? T('Paired Polymarket quotes', pairs, [['Market', r => short(r.question, 105)], ['Both asks + fees', r => cents(r.ask)],
        ['Payout gap', r => points(r.gap_pp)], ['Top size', r => r.ask_size], ['Oldest quote', r => ageOf(r.quote_age_s)]]) : null,
      T('Feed', s.feeds, [['Feed', r => r.feed], ['Age', r => ageOf(r.age_s)], ['OK', r => feedState(r) === 'ok' ? 'yes' : feedState(r) === 'external' ? 'external block' : 'NO'], ['Detail', r => feedLabel(r) ?? short(r.detail, 100)]]),
    ].filter(Boolean);
  }
  const strategyKey = r => r.book + ':' + r.strategy_id;
  function strategies(d) {
    const s = d.mk?.strategies;
    if (s?.state === 'unknown' || !Array.isArray(s?.strategies)) return [noMarkets(d, 'strategies')];
    const lanes = s.totals_by_lane ? Object.entries(s.totals_by_lane) : null;
    return [
      T('Strategies', s.strategies, [['Name', r => r.name ?? r.strategy_id], ['Lane', r => r.book],
        ['Owner', r => r.owner ? nameOf(r.owner) : null],
        ['Status', r => r.status === 'testing' && r.settled_n === 0 && r.open_n === 0 ? 'Registered, no entries' : r.status ?? (r.name ? 'Catalog' : null)],
        ['Settled', r => r.settled_n], ['Wins', r => r.wins], ['Mean net return (% of margin)', r => pct(r.settled_mean_pct, 2)],
        ['Open', r => r.open_n], ['Verdict', r => r.verdict], ['Thesis', r => r.thesis], ['Entry rule', r => r.entry],
        ['Exit rule', r => r.exit], ['Fails when', r => r.fails_when], ['Automatic retirement', r => {
          const conditions = [];
          if (fin(r.kill_mean_lt)) conditions.push('mean realized USD/trade < ' + r.kill_mean_lt);
          if (fin(r.kill_winrate_lt)) conditions.push('win rate < ' + pct(100 * r.kill_winrate_lt));
          return conditions.length ? 'After ' + r.min_n + ' closes: ' + conditions.join(' or ') : 'None configured';
        }]], 'No strategies in this ledger.', strategyKey),
      T('By lane', lanes, [['Lane', ([k]) => k], ['Settled', ([, v]) => v.settled_n], ['Wins', ([, v]) => v.wins],
        ['Open', ([, v]) => v.open_n], ['Sum of net returns (% of margin)', ([, v]) => v.settled_n ? pct(v.settled_sum_pct, 2) : '—']], 'No lane totals yet.'),
    ];
  }
  const bookTable = (title, books) => T(title, books, [['Book', r => r.book_id], ['Lane', r => r.lane], ['Cash', r => money(r.cash)], ['Equity', r => r.equity == null ? 'unknown (position unpriced)' : money(r.equity)], ['Realized', r => colored(usd(r.realized_total), r.realized_total)], ['Unrealized', r => r.unrealized_total == null ? 'unknown' : colored(usd(r.unrealized_total), r.unrealized_total)],
    ['Open', r => (r.open ?? []).length], ['Closed', r => r.closed_n], ['Wins', r => r.wins], ['Halted', r => r.halted ? 'YES' : 'no']], 'No book exists yet.');
  function paperLabs(d) {
    const p = d.mk?.paper_labs;
    const registry = d.mk?.strategies;
    const registered = registry?.state !== 'unknown' && Array.isArray(registry?.strategies)
      ? registry.strategies.filter(r => r.owner) : null;
    const experiments = T('Registered experiments', registered, [['Name', r => r.name ?? r.strategy_id], ['Lane', r => r.book],
      ['Owner', r => nameOf(r.owner)], ['Status', r => r.status], ['Entry rule', r => r.entry]], 'No experiments registered.', strategyKey, d.mkUnknown?.strategies ?? registry?.reason);
    if (!p || p.state === 'unknown') return [noMarkets(d, 'paper_labs'), experiments];
    return [
      T('News-call lab', p.news_calls, CALL_COLS, 'No news call has been made yet.'),
      bookTable('Books (live)', p.books),
      T('Strategy totals by lane', Object.entries(p.strategies ?? {}), [['Lane', ([k]) => k], ['Settled', ([, v]) => v?.settled_n], ['Wins', ([, v]) => v?.wins], ['Win rate', ([, v]) => v?.settled_n ? Math.round(100 * v.wins / v.settled_n) + '%' : '—'], ['Sum %', ([, v]) => v?.settled_n ? pct(v.settled_sum_pct, 2) : '—'], ['Open', ([, v]) => v?.open_n]], 'No strategy results yet.'),
      T('Post-count trackers', p.rule_markets?.trackers, [['Market window', r => r.title], ['Account', r => r.handle],
        ['Count so far', r => r.total], ['Pace at close', r => r.pace], ['Closes', r => when(r.end_ts)]], 'No active tracker windows.'),
      experiments,
      p.not_ported ? section('Not ported', el('p', p.not_ported)) : null,
    ];
  }
  const walletRows = d => d.sources?.wallet_findings?.state === 'known' ? d.sources.wallet_findings.rows : null;
  function explorer(d) {
    const e = d.mk?.explorer, c = d.svc?.creator;
    const launches = c?.state === 'known' ? c.launches : null;
    return [
      e ? T('Briefs', e.notes, [['When', r => when(r.at)], ['Seat', r => nameOf(r.role)], ['Topic', r => r.topic], ['Why this', r => short(r.why_this, 160)], ['Notebook', r => short(r.notebook, 400)]], 'No explorer brief yet.', r => 'note-' + r.id) : noMarkets(d, 'explorer'),
      e ? T('Leads (scored at resolution)', e.leads, [['Question', r => short(r.question, 120)], ['Side', r => r.side], ['Price at note', r => cents(r.price_at)], ['Bid now', r => cents(r.bid_now)], ['Resolved', r => fin(r.resolved_pp) ? r.resolved_pp.toFixed(1) + ' pp' : null]], 'No lead recorded yet.') : null,
      T('Wallet findings', walletRows(d), [['When', r => when(r.created_at)], ['Wallet', r => r.wallet], ['Finding', r => human(r.finding_type)], ['Severity', r => r.severity], ['Message', r => short(r.message, 240)]], 'No wallet finding recorded.', null, 'no wallet analyzer database (start office-ui with --wallet-db).'),
      T('Creator launches (BSC collector)', launches, [['Token', r => N.node('bsc', r.token)], ['Transaction', r => short(r.tx_hash, 20)]], c?.status ? 'The collector has seen no launch yet (' + c.status + ').' : 'No launch seen.', null, c?.reason ?? 'no creator checkpoint'),
    ].filter(Boolean);
  }
  const rowsOf = (d, name) => d.sources?.[name]?.state === 'known' ? d.sources[name].rows : null;
  const xhandle = a => a ? '@' + String(a).replace(/^@/, '').slice(0, 30) : null;
  function memeWhy(row) {
    if (!row.why_full || row.why_full === row.why) return row.why;
    const details = el('details');
    details.append(el('summary', row.why), el('p', row.why_full));
    return details;
  }
  function memes(d) {
    const m = d.mk?.memes;
    const guesses = rowsOf(d, 'narrative_guesses');
    return [
      m ? T('Top 5 meme candidates', m, [['When', r => ageOf(r.age_s)], ['Name', r => r.name], ['Ticker', r => r.ticker ? '$' + r.ticker : null], ['Score', r => r.score], ['Story', r => short(r.title, 140)], ['Why', memeWhy], ['Basis', r => r.proposed_by?.startsWith('seat:') ? 'Seat reviewed' : 'Headline rule']], 'No story has been proposed as a meme.', r => r.news_id) : noMarkets(d, 'memes'),
      hasSeat(d, 'narrative') ? T('Narrative guesses', guesses, [['Known', r => when(r.known_at)], ['Cohort', r => r.cohort], ['Source', r => r.source], ['Theme', r => r.theme_id], ['Confidence', r => r.confidence], ['Tickers', r => (json(r.ticker_candidates_json) ?? []).slice(0, 5).join(', ')], ['Names', r => (json(r.name_candidates_json) ?? []).slice(0, 4).join(', ')], ['Rationale', r => short(r.rationale, 200)]], 'No narrative guess recorded.', null, 'no narrative database.') : null,
      hasSeat(d, 'narrative') ? T('Launch matches', rowsOf(d, 'narrative_matches'), [['Matched', r => when(r.matched_at)], ['Coin', r => N.node(r.network_id, r.mint, r.symbol)], ['Kind', r => r.match_kind], ['Source', r => r.source], ['Guess', r => short(r.guess_id, 14)]], 'No launch has been matched against a guess.', null, 'no narrative database.') : null,
      T('X mentions', rowsOf(d, 'x_mentions'), [['Seen', r => when(r.seen_at)], ['Author', r => xhandle(r.author)], ['Coin', r => r.mint ? N.node(r.network_id, r.mint, r.ticker) : r.ticker ? '$' + r.ticker : null], ['How', r => r.method], ['Post', r => short(r.text, 260)]], 'No X mention recorded.', null, 'no X database (start office-ui with --x-db).'),
    ];
  }
  function coach(d) {
    const c = d.mk?.coach;
    if (!c) return [noMarkets(d, 'coach')];
    const names = [...new Set(c.skills.map(s => s.skill))];
    return [
      T('Lessons and loss reviews', c.lessons, [['When', r => when(r.at)], ['For', r => nameOf(r.role)], ['By', r => nameOf(r.by_role)], ['Position', r => r.pos_id], ['Cause', r => short(r.cause, 300)], ['Change', r => short(r.change, 300)], ['Lesson', r => short(r.lesson, 400)]], 'No lesson recorded yet.'),
      T('Coin desk loss reviews', rowsOf(d, 'order_reviews'), [['When', r => when(r.at)], ['By', r => nameOf(r.role)], ['Order', r => short(r.order_id, 12)], ['Cause', r => short(r.cause, 300)], ['Change', r => short(r.change, 300)]], 'No coin desk loss reviewed yet.'),
      T('Strategies retired by their kill rule', c.retired ?? [], [['When', r => when(r.retired_at)], ['Lane', r => r.lane], ['Strategy', r => r.strategy_id], ['Closes', r => json(r.stats_json)?.n], ['Mean net per close (USD)', r => usd(json(r.stats_json)?.mean_realized)],
        ['Win rate', r => { const rate = json(r.stats_json)?.winrate; return fin(rate) ? pct(100 * rate) : null; }]], 'No strategy retired yet.'),
      T('Halts and retirements', c.halts ?? [], [['When', r => when(r.at)], ['Book', r => r.book_id], ['Event', r => r.kind], ['Detail', eventDetail]], 'No halt or retirement yet.'),
      T('Skills (every version, newest first)', c.skills, [['When', r => when(r.at)], ['Skill', r => r.skill], ['By', r => nameOf(r.by_role)], ['Steps', r => short(r.steps, 400)], ['Why', r => short(r.why, 200)]], 'No skill saved yet.', r => 'skill-' + r.id),
      T('Reviews', c.reviews, [['When', r => when(r.at)], ['By', r => nameOf(r.by_role)], ['Review', r => short(r.review, 500)], ['Priorities', r => short(r.priorities, 300)]], 'No coach review yet.'),
      section('Skills registered', el('p', names.length ? names.join(' · ') : 'none')),
    ];
  }
  function lab(d) {
    const l = d.mk?.lab;
    if (!l) return [noMarkets(d, 'lab')];
    return [T('Video rules under test', l.rules, [['Added', r => when(r.at)], ['Video', r => short(r.title, 80)], ['Rule', r => short(r.rule_text, 300)], ['Instrument', r => r.instrument], ['Status', r => r.status], ['Back-test', r => r.backtest_json ? flat(json(r.backtest_json)) : 'not run (the back-tester needs stored bars)']], 'No video rule recorded yet.', r => r.rule_id)];
  }

  // ---- Control: services, health, halts (token gated), operator notes
  let token = '', writable = null;
  getJson('/api/overview').then(o => { writable = o?.writable ?? null; });
  async function post(path, body) {
    try {
      const r = await fetch(path, {method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json', 'X-Office-Token': token}, body: JSON.stringify(body)});
      const out = await r.json();
      const label = r.ok ? 'done: ' + (out.state ?? 'ok') : 'refused (' + r.status + '): ' + (out.error ?? out.reason ?? 'unknown');
      return {ok: r.ok, label, out};
    } catch { return {ok: false, label: 'Request status unknown'}; }
  }
  function haltPanel(d) {
    const books = d.mk?.control?.books;
    if (!books) return section('Halt a book', el('p', 'Unknown: ' + (reasonOf(d) ?? 'no control data.')));
    const box = block();
    const status = el('p', writable === false ? 'Actions are off: start office-ui with --token-file to enable halting.' : 'Halting stops new entries in one book; exits and rails keep running. Needs the operator token.', 'ol-status');
    status.setAttribute('role', 'status');
    const input = el('input');
    input.type = 'password'; input.autocomplete = 'off'; input.placeholder = 'operator token'; input.setAttribute('aria-label', 'operator token'); input.value = token;
    input.addEventListener('input', () => { token = input.value; });
    box.append(input);
    if (!books.length) box.append(el('p', 'No markets paper books in this ledger.'));
    for (const b of books) {
      const row = el('div', null, 'ol-halt');
      const state = el('span', (b.halted ? 'HALTED' : 'running') + ' · cash ' + (money(b.cash) ?? 'unknown'), b.halted ? 'ol-neg' : '');
      const go = el('button', b.halted ? 'Resume' : 'Halt');
      go.type = 'button';
      go.disabled = writable === false;
      go.addEventListener('click', async () => { status.textContent = 'sending…'; status.textContent = (await post('/api/markets/halt', {book_id: b.book_id, halted: !b.halted, reason: 'operator from the floor'})).label; });
      row.append(el('strong', b.book_id), ' ', state, ' ', go);
      box.append(row);
    }
    const say = el('form', null, 'ol-say');
    const to = el('input'); to.placeholder = 'seat role or all'; to.setAttribute('aria-label', 'recipient role or all seats'); to.maxLength = 40; to.required = true;
    const msg = el('input'); msg.placeholder = 'operator note'; msg.setAttribute('aria-label', 'message'); msg.maxLength = 500;
    const send = el('button', 'Queue note'); send.type = 'submit'; send.disabled = writable === false;
    say.append(to, msg, send);
    say.addEventListener('submit', async e => {
      e.preventDefault();
      const role = to.value.trim(), text = msg.value;
      if (!role || !text.trim() || send.disabled) return;
      send.disabled = true;
      status.textContent = 'Queueing note…';
      const result = await post('/api/markets/say', {to_role: role, text});
      send.disabled = writable === false;
      if (result.ok && result.out.state === 'recorded') {
        const count = result.out.recipient_n;
        status.textContent = 'Queued for ' + count + (count === 1 ? ' seat' : ' seats');
        if (msg.value === text) msg.value = '';
      } else status.textContent = result.label;
    });
    box.append(say, status);
    return section('Halt a book, operator note', box);
  }
  function unitTable(d) {
    const svc = d.svc;
    if (!svc) return section('Services', el('p', 'Unknown: the services read failed.'));
    const units = svc.units ? Object.entries(svc.units) : null;
    return T('Services (enabled systemd user units' + (fin(svc.units_not_enabled) && svc.units_not_enabled ? '; ' + svc.units_not_enabled + ' disabled, not listed' : '') + ')', units, [['Unit', ([k]) => k], ['State', ([, v]) => colored(v.active + (v.sub ? ' (' + v.sub + ')' : '') + (v.note ? ' · ' + v.note : ''), v.ok ? 1 : -1)]], 'No enabled unit is installed.', null, 'systemctl could not be read.');
  }
  function serviceFacts(d) {
    const s = d.svc;
    if (!s) return section('Pipeline', el('p', 'Unknown: the services read failed.'));
    const g = s.guard, r = S.resolution, crew = s.crew;
    const fact = (label, value) => value == null || value === '' ? null : line(label, value);
    return section('Pipeline state', block(
      fact('Host watchdog guard', g?.state === 'unknown' ? 'unknown: ' + g.reason : [g?.state, g?.reason, g?.latched ? 'latched' : null, fin(g?.age_s) ? 'file ' + Math.round(g.age_s) + ' s old' : null].filter(Boolean).join(' · ')),
      fact('J7 listener', s.j7?.state === 'known' ? s.j7.signals_total + ' signals stored, ' + s.j7.signals_1h + ' in the last hour, newest ' + (ageText(s.j7.last_recv_at) ?? 'never') + ' (' + s.j7.note + ')' : 'unknown: no J7 database'),
      fact('Tick recorder', s.ticks?.state === 'known' ? 'newest tick ' + (ageText(s.ticks.last_at) ?? 'never') + ', ' + s.ticks.coins_60s + ' coins in the last minute' : 'unknown: no ticks table'),
      fact('Ticks resolution', r ? 'poll gap median ' + (fin(r.poll_gap_median_s) ? r.poll_gap_median_s.toFixed(2) : 'unknown') + ' s; price change gap median ' + (fin(r.price_gap_median_s) ? r.price_gap_median_s.toFixed(1) : 'unknown') + ' s (last ' + r.window_s + ' s, ' + r.coins + ' coins)' : 'unknown until ticks arrive'),
      fact('Crew bridge', crew?.state === 'known' ? Object.entries(crew.cursors).map(([k, v]) => k + ' ' + v).join(' · ') || 'cursor table empty' : 'unknown: no crew cursor in the office store'),
      fact('Creator collector', s.creator?.state === 'known' ? [s.creator.status, s.creator.calls != null ? s.creator.calls + ' calls' : null, s.creator.launches.length + ' launches', s.creator.venue_discoveries != null ? s.creator.venue_discoveries + ' venue discovery files' : null, s.creator.last_error].filter(Boolean).join(' · ') : 'unknown: ' + (s.creator?.reason ?? 'no checkpoint'))));
  }
  function control(d) {
    const c = d.mk?.control;
    return [
      serviceFacts(d), unitTable(d),
      c ? T('Markets feeds health', c.feeds, [['Feed', r => r.feed], ['Age', r => ageOf(r.age_s)], ['OK', r => feedState(r) === 'external' ? el('span', 'external block') : colored(r.ok ? 'yes' : 'NO', r.ok ? 1 : -1)], ['Detail', r => feedLabel(r) ?? short(r.detail, 120)]], 'No feed has reported.') : noMarkets(d, 'control'),
      c ? T('Timed jobs (markets)', c.jobs, [['Job', r => r.name], ['Every', r => r.every_s + ' s']], 'No jobs.') : null,
      c ? section('Universe', line(Object.entries(c.universe ?? {}).map(([k, v]) => human(k) + ' ' + v).join(' · '))) : null,
      haltPanel(d),
      c ? T('Book events', c.events, [['When', r => when(r.at)], ['Book', r => r.book_id], ['Kind', r => r.kind], ['Detail', eventDetail]], 'No event yet.') : null,
      c ? T('Operator notes', c.operator_chat, [['When', r => when(r.at)], ['From', r => r.sender],
        ['To', r => r.to_role ?? 'All seats'], ['Text', r => short(r.text, 300)],
        ['Delivery', r => r.delivery === 'delivered' ? 'Delivered (' + r.delivered_n + '/' + r.recipient_n + ')'
          : r.delivery === 'pending' ? 'Pending (' + r.delivered_n + '/' + r.recipient_n + ' delivered)' : 'No receipt tracked']], 'No note recorded.') : null,
    ].filter(Boolean);
  }

  // ---- Trading: every book, with live PnL, positions, tp/sl, fills and live charts
  const sigDecision = (d, s) => {
    const reqs = (d.floor?.paper_requests ?? []).filter(r => s.ticket_id && r.ticket_id === s.ticket_id);
    if (!reqs.length) return s.side !== 'buy' ? 'sell (stored, no ticket)' : s.ticket_id ? 'no order requested (yet)' : 'no ticket';
    const out = (d.floor?.outcomes ?? []);
    return reqs.map(r => { const o = out.find(x => x.request_id === r.request_id); return r.kind + (o ? ' ' + o.outcome + (o.reason ? ' (' + o.reason + ')' : '') : ' queued'); }).join(', ');
  };
  function copyBook(d) {
    const books = S.books, positions = S.positions;
    const box = block();
    const bk = books?.[0];
    const head = el('div', null, 'ol-bookhead');
    head.append(el('strong', 'Copy desk book'), ' ');
    if (bk) {
      for (const [label, field] of [['total', 'total_usd'], ['realized', 'realized_usd'], ['unrealized', 'unrealized_usd'], ['equity', 'equity_usd']]) {
        const v = el('span', null, 'ol-fig'); const num = el('b', liveValue('book:' + bk.book_id + ':' + field) ?? 'unknown'); num.dataset.live = 'book:' + bk.book_id + ':' + field; v.append(el('i', label + ' '), num); head.append(v);
      }
      head.append(el('span', bk.open_n + ' open' + (bk.unpriced_n ? ' · ' + bk.unpriced_n + ' unpriced' : ''), 'ol-fig'));
    } else head.append(el('span', 'Unknown: no live PnL rows (desk_live_pnl) yet.'));
    box.append(head, el('p', '', 'ol-livestate'));
    const open = (positions ?? []).slice().sort((a, b) => (b.at ?? 0) - (a.at ?? 0));
    box.append(positions == null ? el('p', 'Unknown: no live position rows.') : !open.length ? el('p', 'No open position right now.') : block(...open.map(p => {
      const card = el('div', null, 'ol-pos-card');
      const info = el('div', null, 'ol-pos-info');
      const entry = p.entry_price_usd;
      const slPrice = fin(entry) && fin(p.sl_pct) ? entry * (1 - p.sl_pct / 100) : null, tpPrice = fin(entry) && fin(p.tp_pct) ? entry * (1 + p.tp_pct / 100) : null;
      const val = (field, label) => { const s = el('span', null, 'ol-fig'); const b = el('b', liveValue('pos:' + p.order_id + ':' + field) ?? 'unknown'); b.dataset.live = 'pos:' + p.order_id + ':' + field; s.append(el('i', label + ' '), b); return s; };
      info.append(N.node(p.chain, p.mint), ' ', val('price_usd', 'price'), val('unrealized_usd', 'unrealized'), val('unrealized_pct', 'unrealized %'), val('value_usd', 'value'), el('span', 'cost ' + (money(p.cost_usd) ?? 'unknown') + ' · entry ' + (px(entry) ?? 'unknown') + ' · tp ' + (fin(p.tp_pct) ? '+' + p.tp_pct + '%' : 'unset') + ' · sl ' + (fin(p.sl_pct) ? '-' + p.sl_pct + '%' : 'unset') + (fin(p.trail_pct) ? ' · trail ' + p.trail_pct + '%' : ''), 'ol-fig'));
      card.append(info, chart({key: posKey(p), title: N.node(p.chain, p.mint), entry, sl: slPrice, tp: tpPrice, windowS: 600,
        note: 'tp and sl rails act on the net exit value (after modelled slippage and fees); the lines are drawn from the entry price, so they are approximate.'}));
      return card;
    })));
    return section('Copy desk · J7', box);
  }
  function j7Signals(d) {
    const list = S.signals;
    if (list == null) return section('J7 signals', el('p', 'Unknown: no J7 database or no signals table.'));
    const box = block(el('p', '', 'ol-livestate'));
    box.append(T('Signals, last hour (newest first)', list, [['Received', r => when(r.recv_at)], ['Trader', r => r.handle ?? r.trader_id?.slice(0, 8)], ['Chain', r => r.chain], ['Coin', r => N.node(r.chain, r.token, r.symbol)], ['Side', r => r.side], ['Size', r => money(r.usd, 0)], ['Mcap', r => fin(r.mcap_usd) ? '$' + Math.round(r.mcap_usd).toLocaleString() : null], ['Copy desk decision', r => sigDecision(d, r)], ['Window result', r => r.outcome?.ret_pct != null ? pct(r.outcome.ret_pct, 2) + ' (' + r.outcome.n_ticks + ' ticks)' : null]], 'No signal in the last hour.', r => r.signal_id));
    const recent = list.filter((s, i, all) => s.side === 'buy' && all.findIndex(o => o.side === 'buy' && sigKey(o) === sigKey(s)) === i).slice(0, 4);      // one chart per coin, from its newest buy
    if (recent.length) box.append(section('Price since each recent buy signal', ...recent.map(s => { const c = block(el('p', sigLine(s))); c.append(chart({key: sigKey(s), title: N.node(s.chain, s.token, s.symbol), signalAt: s.recv_at, entry: s.price_usd, windowS: 900, note: 'ticks the recorder stored from our receive time; the line before the signal is whatever it was already watching.'})); return c; })));
    return section('J7 signals and the copy desk\'s decisions', box);
  }
  function booksSection(d) {
    const t = totals(d);
    const box = block(el('p', 'Total across books: ' + pnlText(d) + '. Unknown parts are left out, never counted as zero.'));
    const rows = t.rows;
    box.append(T('Books', rows, [['Book', r => r.book], ['Lane', r => r.lane], ['Equity', r => { if (r.live) { const b = el('b', money(r.equity) ?? 'unknown'); b.dataset.live = r.key + ':equity_usd'; return b; } return r.equity == null ? (r.judge ? 'not tracked' : 'unknown (position unpriced)') : money(r.equity); }],
      ['Realized', r => { if (r.live) { const b = el('b', usd(r.realized) ?? 'unknown'); b.dataset.live = r.key + ':realized_usd'; return b; } return colored(usd(r.realized) ?? 'unknown', r.realized); }],
      ['Unrealized', r => { if (r.live) { const b = el('b', usd(r.unrealized) ?? 'unknown'); b.dataset.live = r.key + ':unrealized_usd'; return b; } return r.unrealized == null ? 'unknown' : colored(usd(r.unrealized), r.unrealized); }],
      ['Open', r => r.open], ['Closed', r => r.closed], ['Wins', r => r.wins], ['Halted', r => r.halted ? 'YES' : null]], 'No book yet.'));
    return section('All books', box);
  }
  const posCols = [['Market', r => r.label ?? r.instrument], ['Side', r => r.side], ['Qty', r => px(r.qty)], ['Entry', r => px(r.entry_price)], ['Live', r => r.live?.state === 'unknown' ? 'unpriced' : px(r.live?.price ?? r.live?.mid ?? r.live?.bid)], ['Unrealized', r => r.live?.state === 'unknown' ? 'unknown' : colored(usd(r.live?.unrealized), r.live?.unrealized)],
    ['tp', r => px(r.tp_price)], ['sl', r => px(r.sl_price)], ['Liq', r => px(r.liq_price)], ['Lev', r => r.leverage > 1 ? r.leverage + 'x' : null], ['Opened', r => when(r.entry_at)], ['Thesis', r => short(r.thesis, 100)]];
  const closedCols = [['Closed', r => when(r.exit_at)], ['Market', r => r.label ?? r.instrument], ['Side', r => r.side], ['Entry', r => px(r.entry_price)], ['Exit (fill)', r => px(r.exit_price)], ['Realized', r => colored(usd(r.realized), r.realized)], ['Why', r => human(r.exit_reason)]];
  const laneSection = (d, lane, title) => {
    const allBooks = d.mk?.paper_labs?.books;
    if (!Array.isArray(allBooks)) return noMarkets(d, 'paper_labs');
    const books = allBooks.filter(b => b.lane === lane);
    if (!books.length) return section(title, el('p', 'No ' + lane + ' order filled in this ledger yet.'));
    return section(title, ...books.flatMap(b => [line(b.book_id, 'cash ' + (money(b.cash) ?? 'unknown'), 'equity ' + (b.equity == null ? 'unknown (position unpriced)' : money(b.equity)), 'realized ' + usd(b.realized_total), 'unrealized ' + (b.unrealized_total == null ? 'unknown' : usd(b.unrealized_total)), b.halted ? 'HALTED' : null),
      T('Open positions', b.open, posCols, 'No open position.'), T('Fills (closed, newest last)', b.closed_recent, closedCols, 'Nothing closed yet.')]));
  };
  const copyFills = d => {
    const rows = (d.floor?.orders ?? []).filter(o => /copy/.test(o.book_id ?? '')).sort((a, b) => b.requested_at - a.requested_at);
    return T('Copy desk orders and fills', d.floor?.orders ? rows : null, [['Requested', r => when(r.requested_at)], ['Coin', r => N.node(r.network_id, r.mint, r.ticker)], ['State', r => r.state], ['Filled', r => when(r.filled_at)], ['Entry', r => px(r.entry_price_usd)], ['Spent', r => money(r.spend_usd)], ['Closed', r => when(r.closed_at)], ['Exit value', r => money(r.exit_usd)],
      ['Net', r => r.state === 'closed' && fin(r.exit_usd) ? colored(usd(r.exit_usd - r.spend_usd - (r.costs_usd ?? 0)), r.exit_usd - r.spend_usd - (r.costs_usd ?? 0)) : null], ['Why', r => human(r.exit_reason ?? r.reason)]], 'The copy desk has placed no order yet.', r => r.order_id);
  };
  function trading(d) {
    return [booksSection(d),
      ...Object.keys(traderRole).filter(lane => staffedLane(d, lane))
        .map(lane => laneSection(d, lane, {polymarket: 'Polymarket', perps: 'Perps', stocks: 'Stocks'}[lane])),
      ...(hasSeat(d, 'copy_desk') ? [copyBook(d), j7Signals(d), section('Copy desk · orders', copyFills(d))] : [])];
  }

  function ledgerArchive(id, d) {
    const boundary = d.ledgerBoundary;
    if (!boundary || !['control', 'trading', 'coach', 'strategies', 'explorer', 'paper-labs'].includes(id)) return [];
    const history = boundary.history;
    const date = fin(boundary.started_at) ? new Date(boundary.started_at * 1000) : null;
    const sameDay = date?.toDateString() === new Date().toDateString();
    const timeOptions = {hour: 'numeric', minute: '2-digit'};
    const stamp = date?.toLocaleString(undefined, sameDay ? timeOptions : {...timeOptions, month: 'short', day: 'numeric'});
    const text = stamp ? 'Paper trading records restarted at ' + stamp + (sameDay ? ' today.' : '.')
      : 'The paper trading reset time is unavailable.';
    const hasHistory = history && ['trading', 'coach', 'explorer', 'paper-labs'].includes(id);
    const note = hasHistory ? ' Earlier records are marked “Before reset”.'
      : boundary.archive_path && !history ? ' Earlier records are unavailable.' : '';
    const sections = [section('Paper trading reset', el('p', text + note))];
    if (!history) return sections;
    const archive = ' · Before reset';
    if (id === 'trading') sections.push(T('Closed positions' + archive, history.closes?.filter(row => staffedLane(d, row.lane)),
      [['Closed', r => when(r.exit_at)], ['Lane', r => r.lane], ['Instrument', r => r.label ?? r.instrument],
        ['Net', r => usd(r.realized)]], 'No archived closes.', r => r.pos_id));
    if (id === 'coach') sections.push(
      T('Coach reviews' + archive, history.reviews, [['When', r => when(r.at)], ['By', r => nameOf(r.by_role)],
        ['Review', r => short(r.review, 500)], ['Priorities', r => short(r.priorities, 300)]], 'No archived reviews.', r => r.id),
      T('Skills' + archive, history.skills, [['When', r => when(r.at)], ['By', r => nameOf(r.by_role)],
        ['Skill', r => short(r.skill, 80)], ['Steps', r => short(r.steps, 400)]], 'No archived skill versions.', r => r.id));
    if (id === 'explorer') sections.push(T('Explorer briefs' + archive, history.explorer,
      [['When', r => when(r.at)], ['By', r => nameOf(r.role)], ['Topic', r => r.topic],
        ['Notebook', r => short(r.notebook, 500)]], 'No archived briefs.', r => r.id));
    if (id === 'paper-labs') sections.push(T('Cashless news calls' + archive, history.calls,
      [['When', r => when(r.at)], ['Market', r => r.question], ['Side', r => r.side],
        ['Entry ask', r => cents(r.entry_ask)]], 'No archived calls.', r => r.call_id));
    return sections;
  }

  // ---- wiring into the rooms: our sections come first, the room's own after them
  const add = {newsroom, debate, sports, strategies, 'paper-labs': paperLabs, explorer, memes, coach, lab, control, trading};
  function wire() {
    for (const [id, build] of Object.entries(add)) {
      const base = window.OfficeRooms.get(id);
      if (!base || base.live) continue;
      window.OfficeRooms.register({...base, live: true, detail(d) {
        let mine = [];
        try { mine = build(d).filter(n => n?.textContent.trim()); } catch (error) { console.error('Office live room failed', id, error); }
        const original = base.detail(d);
        const rest = original?.tagName === 'DIV' ? [...original.children].filter(n => !(id === 'trading' && /^(Stocks|Perps)$/.test(n.querySelector?.('h3')?.textContent ?? ''))) : [];
        const box = block();
        box.append(...ledgerArchive(id, d), ...mine, ...rest);
        return box;
      }});
    }
  }

  // ---- the wall: boards in the room tiles
  const F = window.OfficeFloor.furniture;
  const figure = (value, label, n) => value == null ? null : {value, label, tone: !fin(n) || n === 0 ? '' : n > 0 ? 'positive' : 'negative'};
  const laneLine = (k, v) => [k, fin(v?.settled_n) ? v.settled_n + ' settled' : null, fin(v?.wins) ? v.wins + ' won' : null, fin(v?.open_n) ? v.open_n + ' open' : null].filter(Boolean).join(' · ');
  const lines = (d, make) => d.mk ? make(d.mk) : [];
  Object.assign(F, {
    trading: d => [
      ['All books', 'screen', () => { const t = totals(d); return {figure: figure(t.total == null ? null : usd(t.total), 'PnL, realized and unrealized', t.total), lines: t.rows.slice(0, 4).map(r => [r.book, usd((r.realized ?? 0) + (r.unrealized ?? 0)), r.open != null ? r.open + ' open' : null].filter(Boolean).join(' · '))}; }],
      ...(hasSeat(d, 'copy_desk') ? [['Copy desk', 'screen', () => {
        const b = S.books?.[0];
        return b ? {figure: figure(usd(b.total_usd), 'total, ' + b.open_n + ' open', b.total_usd),
          lines: (S.signals ?? []).filter(s => s.side === 'buy').slice(0, 2).map(sigLine)} : [];
      }]] : []),
      ['Markets books', 'paper', () => staffedBooks(d).map(b => [b.lane, usd((b.realized_total ?? 0) + (b.unrealized_total ?? 0)), (b.open ?? []).length + ' open'].join(' · '))],
    ],
    newsroom: [
      ['Latest stories', 'paper', d => (Array.isArray(d.mk?.newsroom?.wire) ? d.mk.newsroom.wire.slice(0, 4) : []).map(w => short(w.title, 52) + ' · ' + (ageOf(w.age_s) ?? ''))],
      ['Routed, 24 h', 'screen', d => { const c = d.mk?.newsroom?.room_counts; return c ? {figure: figure(String(Object.values(c).reduce((a, b) => a + b, 0)), 'routes to rooms'), lines: Object.entries(c).slice(0, 4).map(([k, v]) => k + ' ' + v)} : []; }],
      F.newsroom?.[0],
    ].filter(Boolean),
    strategies: [['Strategies', 'gold', d => lines(d, mk => (mk.strategies?.strategies ?? []).slice(0, 4).map(s => [short(s.name ?? s.strategy_id, 26), s.status, s.settled_n + ' settled'].join(' · ')))],
      ['By lane', 'screen', d => lines(d, mk => Object.entries(mk.strategies?.totals_by_lane ?? {}).slice(0, 4).map(([k, v]) => laneLine(k, v)))]],
    'paper-labs': [['Rule markets', 'screen', d => lines(d, mk => { const r = mk.paper_labs?.rule_markets; return r ? {figure: figure(String(r.temperature + r.posts), 'temperature and post-count markets'), lines: (r.trackers ?? []).filter(t => t.total).slice(0, 3).map(t => short(t.title, 40) + ' · ' + t.total + ' so far, pace ' + t.pace)} : []; })],
      ['News calls', 'screen', d => lines(d, mk => { const c = mk.paper_labs?.news_calls ?? []; return {figure: figure(String(c.length), 'forced paper calls'), lines: c.slice(0, 3).map(x => [x.side, cents(x.entry_ask), x.horizon].filter(Boolean).join(' · '))}; })],
      ['Books', 'paper', d => lines(d, mk => (mk.paper_labs?.books ?? []).map(b => [b.lane, usd((b.realized_total ?? 0) + (b.unrealized_total ?? 0)), (b.open ?? []).length + ' open'].join(' · ')))]],
    sports: [['Games', 'screen', d => lines(d, mk => { const g = mk.sports?.games ?? []; return {figure: figure(String(g.length), 'games, last hour'), lines: g.slice(0, 3).map(x => x.away + ' at ' + x.home + (fin(x.fair_home) ? ' · fair ' + Math.round(100 * x.fair_home) + '%' : ''))}; })],
      ['Edges', 'gold', d => lines(d, mk => (mk.sports?.edges ?? []).slice(0, 3).map(e => [e.side ?? e.team, fin(e.gap_pp) ? e.gap_pp.toFixed(1) + ' pp' : null].filter(Boolean).join(' · ')))]],
    explorer: [['Newest brief', 'notebook', d => lines(d, mk => (mk.explorer?.notes ?? []).slice(0, 1).flatMap(n => [n.topic, ageText(n.at)]))], ['Wallet findings', 'paper', d => (walletRows(d) ?? []).slice(0, 3).map(w => human(w.finding_type) + ' · ' + w.severity)]],
    memes: [['Candidates', 'screen', d => lines(d, mk => { const m = mk.memes ?? []; return {figure: figure(String(m.length), 'meme candidates'), lines: m.slice(0, 3).map(x => [x.name, x.ticker ? '$' + x.ticker : null].filter(Boolean).join(' '))}; })],
      ['Narrative matches', 'paper', d => (rowsOf(d, 'narrative_matches') ?? []).slice(0, 3).map(m => [m.match_kind, m.symbol ?? N.text(m.network_id, m.mint)].join(' · '))],
      ['X mentions', 'cabinet', d => (rowsOf(d, 'x_mentions') ?? []).slice(0, 3).map(m => [xhandle(m.author), m.ticker ? '$' + m.ticker : null].filter(Boolean).join(' · '))]],
    coach: [['Lessons', 'notebook', d => lines(d, mk => { const l = mk.coach?.lessons ?? []; return {figure: figure(String(l.filter(x => x.pos_id).length), 'losses reviewed'), lines: l.slice(0, 2).map(x => short(x.change || x.lesson, 70)).concat((mk.coach?.retired ?? []).length ? [(mk.coach.retired.length) + ' strategies retired'] : [])}; })], ['Skills', 'gold', d => lines(d, mk => [...new Set((mk.coach?.skills ?? []).map(s => s.skill))].slice(0, 3))]],
    lab: [['Video rules', 'cabinet', d => lines(d, mk => (mk.lab?.rules ?? []).slice(0, 3).map(r => short(r.title ?? r.rule_text, 50) + ' · ' + r.status))]],
    control: [['Problems', 'screen', d => { const feeds = d.mk?.control?.feeds ?? [], bad = feeds.filter(f => feedState(f) === 'down'), external = feeds.filter(f => feedState(f) === 'external'), units = Object.entries(d.svc?.units ?? {}).filter(([, v]) => !v.ok); const g = d.svc?.guard;
        const count = bad.length + units.length + (g?.state === 'paused' ? 1 : 0);
        return {figure: figure(d.mk || d.svc ? String(count) : null, 'feeds or units down', count ? -1 : 0), lines: [...bad.map(f => f.feed + ' failing'), ...units.slice(0, 2).map(([k, v]) => k + ' ' + v.active), ...external.map(feedLabel), g ? 'guard ' + g.state : null].filter(Boolean)}; }],
      ['Wakes', 'screen', d => (d.floor?.wakes ?? []).slice(0, 3).map(w => nameOf(w.role) + ' ' + (w.outcome ?? '') + ' ' + (ageText(w.at) ?? ''))]],
  });

  // ---- situation: the map reads stories in the shape the original page used
  function situation(s, debated) {
    if (s?.state === 'unknown' || !Array.isArray(s?.stories)) return {state: 'unknown', reason: s?.reason, as_of: s?.as_of};
    const stories = s.stories.map((x, i) => ({id: x.news_id, name: x.title, state: debated?.has(x.news_id) ? 'debating' : (x.rooms ?? []).includes('polymarket') ? 'watching' : 'headlines_only', age_s: x.age_s, newest_at: Date.now() / 1000 - (x.age_s ?? 0), rank: i,
      places: x.places.map(p => ({name: p.name, lat: p.lat, lon: p.lon})), counts: {headlines: 1}, headlines: [{title: x.title + ' (' + x.source + ')', age_s: x.age_s}], geography_status: 'named'}));
    return {stories, as_of: s.as_of, note: s.note};
  }

  // ---- animations driven by the 15 s read
  const feedKinds = {story: 'Story', j7: 'J7'};
  function events(mk, now, mail) {
    const out = [], once = new Set();
    const wire = mk?.newsroom?.wire;
    for (const w of Array.isArray(wire) ? wire.slice(0, 10) : []) for (const r of w.routes ?? []) {
      const room = ROOM_OF[r.room] ?? 'newsroom';
      if (once.has(w.news_id + ':' + room)) continue;      // stocks and perps both land in Trading: one card
      once.add(w.news_id + ':' + room);
      out.push({kind: 'story', type: 'story', room, from_room: 'newsroom', item_id: w.news_id + ':' + room, at: w.fetched_at, seat: null, title: 'Story to ' + (roomLabel[room] ?? room) + ': ' + short(w.title, 90)});
    }
    for (const s of (S.signals ?? []).filter(x => x.side === 'buy').slice(0, 20)) out.push({kind: 'j7', type: 'j7', room: 'trading', item_id: s.signal_id, at: s.recv_at, seat: 'copy_desk', title: 'J7 signal: ' + sigLine(s), signal: s.signal_id});
    return out;
  }
  // replay hook for virtual.js: returns true when the event animated here
  function replay(floor, event) {
    if (reduced()) return false;
    const box = id => floor.querySelector(`[data-tile="${id}"]`)?.getBoundingClientRect();
    const origin = floor.getBoundingClientRect();
    const fly = (glyph, label, a, b, cls) => {
      if (!a || !b) return;
      const chip = el('span', null, 'ol-fly ' + cls);
      chip.setAttribute('aria-hidden', 'true');
      chip.append(el('b', glyph), label ? el('span', label) : '');
      floor.append(chip);
      const at = r => `translate(${r.left - origin.left + r.width / 2}px, ${r.top - origin.top + r.height / 2}px)`;
      const move = chip.animate([{transform: at(a), opacity: 0}, {transform: at(a), opacity: 1, offset: .12}, {transform: at(b), opacity: 1, offset: .88}, {transform: at(b), opacity: 0}], {duration: 2000, easing: 'ease-in-out'});
      move.onfinish = () => chip.remove();
    };
    if (event.kind === 'story') {
      fly('▤', short(event.title.replace(/^Story to \w+: /, ''), 40), box(event.from_room ?? 'newsroom'), box(event.room), 'ol-card');
      const tile = floor.querySelector(`[data-tile="${event.room}"]`);
      if (tile) { tile.classList.add('ol-arrive'); setTimeout(() => tile.classList.remove('ol-arrive'), 2200); }
      return false;
    }
    if (event.kind === 'mail' && event.from && event.to) {
      const st = id => floor.querySelector(`[data-station="${CSS.escape(id)}"]`)?.getBoundingClientRect() ?? (id.startsWith('crew') || id === 'operator_cli' || id === 'operator' ? box('control') : null);
      fly('✉', null, st(event.from), st(event.to), 'ol-mail');
      return false;
    }
    if (event.kind === 'j7') return 'done';   // the live poll already pinged the desk
    return false;
  }
  window.OfficeLive = {header, events, replay, situation, ping, state: () => ({books: S.books, positions: S.positions, signals: S.signals}), totals};

  wire();
  setInterval(poll, 1000);
  poll();
})();
