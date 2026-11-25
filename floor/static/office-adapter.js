(() => {
  'use strict';
  // Turns the one read, /api/office/newsroom, into the shape the rooms were written for. What this office does not hold is left out, so the page says unknown.
  const fin = v => typeof v === 'number' && Number.isFinite(v);
  const clean = (text, n = 400) => String(text ?? '').replace(/\s+/g, ' ').trim().slice(0, n);
  const known = (raw, name) => raw.sources?.[name]?.state === 'known' ? raw.sources[name].rows : null;
  const median = list => { const s = [...list].sort((a, b) => a - b); return s.length ? s[s.length >> 1] : null; };
  const symbolOf = order => order.ticker || (order.mint ? order.mint.slice(0, 4) + '…' + order.mint.slice(-4) : 'unknown');
  const netUsd = o => o.state === 'closed' && fin(o.exit_usd) && fin(o.spend_usd) ? o.exit_usd - o.spend_usd - (o.costs_usd ?? 0) : null;
  const markCache = new Map();

  // The ledger's recorded marks for one order, as the line chart of the Trading room reads them. Fetched once per (order, mark count).
  async function series(order, source) {
    const key = order.marks_n + ':' + order.mark_at;
    const hit = markCache.get(order.order_id);
    if (hit?.key === key) return hit.value;
    const item = {symbol: symbolOf(order), source: 'ledger marks · ' + source, observed_at: order.mark_at, bars: [], quote_asset: 'USD'};
    try {
      const response = await fetch('/api/office/query?name=marks&order=' + encodeURIComponent(order.order_id), {cache: 'no-store'});
      if (!response.ok) throw new Error('HTTP ' + response.status);
      const rows = (await response.json()).rows;
      const usd = rows.some(r => fin(r.price_usd));
      item.quote_asset = usd ? 'USD' : 'SOL';
      item.bars = rows.map(r => ({at: r.at, close: usd ? r.price_usd : r.value_sol})).filter(b => fin(b.close));
      item.interval_s = median(item.bars.slice(1).map((b, i) => b.at - item.bars[i].at).filter(fin));
      if (!usd) item.reason = 'Older ledger: marks are the position value in SOL, with no USD price.';
    } catch {
      item.reason = 'The recorded marks could not be read.';
    }
    markCache.set(order.order_id, {key, value: item});
    return item;
  }

  async function adapt(raw) {
    const now = Date.now() / 1000;
    const seatMap = raw.seats ?? null;
    const roleOf = Object.fromEntries(Object.entries(seatMap ?? {}).map(([role, s]) => [s.actor_id, role]));
    const roleName = actor => roleOf[actor] ?? actor;
    const wakes = (known(raw, 'wakes') ?? []).map(w => ({role: roleName(w.actor_id), at: w.at, started_at: w.started_at, tokens: w.used, model: w.model, outcome: w.outcome, state: w.state}));
    const modelOf = {};
    for (const w of wakes) if (w.model && !modelOf[w.role]) modelOf[w.role] = w.model;
    const failedRun = role => { let n = 0; for (const w of wakes) if (w.role === role) { if (w.outcome === 'ok') break; n++; } return n; };

    const seats = seatMap && Object.fromEntries(Object.entries(seatMap).map(([role, s]) => [role, {
      model: modelOf[role] ?? null, panel: null, generation: s.generation, turns: s.wakes_today, errors: failedRun(role),
      in_session: s.status === 'working', office_status: s.status,
      status: s.status === 'working' ? 'working' : s.status ? 'idle' : null,
      last_action: s.last_action, last_item: null, last_reply: null, last_at: s.last_at, age_s: fin(s.last_at) ? Math.max(0, now - s.last_at) : null,
      resets: null, resets_today: null, failed_handovers: null, handovers: null, last_handover: null,
      wakes_today: s.wakes_today, tokens_today: s.tokens_today, unread_mail: s.unread_mail, last_tool: s.last_tool,
      last_thought: s.last_thought, last_thought_at: s.last_thought_at, sleep_until: s.sleep_until, parked_reason: s.parked_reason, actor_id: s.actor_id, requests: s.requests,
    }]));

    // The paper books: one per ledger file.
    const orders = known(raw, 'orders');
    const capital = known(raw, 'capital') ?? [];
    const names = [...new Set([...capital.map(c => c._source), ...(orders ?? []).map(o => o._source)])];
    const floor = {book_list: [], mail: raw.mail, requests: raw.requests, tasks: raw.tasks, wakes, orders: orders ?? [], capital,
      outcomes: known(raw, 'outcomes'), paper_requests: known(raw, 'requests'), halts: known(raw, 'halts'), candidates: known(raw, 'candidates'),
      filters: known(raw, 'feed_filters'), reads: known(raw, 'results_reads'), standups: (known(raw, 'standups') ?? []).map(r => ({...r, role: roleName(r.actor_id)})),
      visits: (known(raw, 'visits') ?? []).map(r => ({...r, role: roleName(r.actor_id)})), notebooks: Object.fromEntries((known(raw, 'notebooks') ?? []).map(r => [roleName(r.actor_id), r])),
      tickets: known(raw, 'tickets'), ticket_counts: known(raw, 'ticket_counts'),
      tools: (known(raw, 'tool_counts') ?? []).map(r => ({...r, role: roleName(r.actor_id)})), filler: known(raw, 'filler')?.[0] ?? null, totals: null};
    const out = {};
    const pending = [];
    let totals = orders ? {net_usd: 0, wins: 0, scored: 0, open: 0} : null;
    for (const name of names) {
      const key = 'book:' + name, mine = (orders ?? []).filter(o => o._source === name).sort((a, b) => b.requested_at - a.requested_at);
      const initial = capital.find(c => c._source === name && c.kind === 'initial');
      const calls = mine.filter(o => o.state === 'open' || o.state === 'closed').map(o => {
        const net = netUsd(o);
        return {symbol: symbolOf(o), side: 'long', at: o.filled_at ?? o.requested_at, entry: o.entry_price_usd, exit_at: o.closed_at, mark: o.mark_price,
          outcome: o.state === 'closed' ? (o.exit_reason ?? 'closed') : null, net_usd: net, net_pct: fin(net) && o.spend_usd > 0 ? 100 * net / o.spend_usd : null,
          mark_pct: o.state === 'open' && fin(o.mark_value) && o.spend_usd > 0 ? 100 * (o.mark_value - o.spend_usd) / o.spend_usd : null, order_id: o.order_id, address: o.mint, spend_usd: o.spend_usd};
      });
      const scored = calls.filter(c => fin(c.net_usd));
      const open = calls.filter(c => !c.outcome).length;
      out[key] = {
        read: [initial ? 'Paper capital $' + initial.usd + ' (' + initial.book_id + ')' : null, 'Modelled paper fills, no landing proof; a small sample shows no skill.'].filter(Boolean).join(' · '),
        summary: {open, scored: scored.length, wins: scored.filter(c => c.net_usd > 0).length, net_usd: scored.reduce((n, c) => n + c.net_usd, 0), sum_net_pct: scored.reduce((n, c) => n + (c.net_pct ?? 0), 0),
          censored: mine.filter(o => o.state === 'censored').length},
        calls, pending: mine.filter(o => o.state === 'pending').map(o => ({symbol: symbolOf(o), side: 'long', thesis: o.reason, book_lane: null, order_id: o.order_id})),
        refused: mine.filter(o => o.state === 'censored').map(o => ({symbol: symbolOf(o), why: o.reason ?? 'censored: no fill before the entry timeout', at: o.requested_at})),
        price_series: [], watch: [],
      };
      floor.book_list.push([key, name]);
      if (totals) {
        totals.net_usd += out[key].summary.net_usd; totals.wins += out[key].summary.wins; totals.scored += out[key].summary.scored; totals.open += open;
      }
      for (const order of mine.filter(o => o.marks_n >= 2).slice(0, 6)) pending.push(series(order, name).then(item => out[key].price_series.push(item)));
    }
    await Promise.all(pending);
    floor.book_list.push(['stocks', 'Stocks'], ['crypto', 'Perps']);   // this office holds no margin books: these two stay, and say unknown
    floor.totals = totals;

    const activity = (raw.activity ?? []).map(e => ({...e, type: e.kind}));
    const heartbeat = raw.office?.heartbeat_at;
    const halt = floor.halts?.find(h => h.action) ;
    const idle = seats ? Object.entries(seats) : [];
    const parked = idle.filter(([, s]) => s.office_status === 'parked');
    const state = fin(heartbeat) && now - heartbeat <= 900 ? 'running' : raw.office?.state === 'unknown' ? null : 'quiet';
    const filler = floor.filler;
    const fillerAt = fin(filler?.last_mark_at) || fin(filler?.last_outcome_at) ? Math.max(filler.last_mark_at ?? 0, filler.last_outcome_at ?? 0) : null;
    const requests = (raw.requests ?? []).map((r, i) => ({id: 'request-' + i, text: r.text, seat: r.seat, at: r.at, status: {state: 'open', note: 'A reply does not close a request here.'}}));
    const wire = floor.tickets?.map(t => ({news_id: t.ticket_id, title: [t.verdict, t.mint ? t.mint.slice(0, 4) + '…' + t.mint.slice(-4) : null, t.reason].filter(Boolean).join(' · '),
      source: t.seat ?? 'feed', received_at: Date.parse(t.ts) / 1000, disposition: t.verdict}));
    return {
      ...raw, floor, wire, activity, ...out, seats,
      desk: {state, epoch: raw.office?.epoch, heartbeat_at: heartbeat, halt_reason: halt?.action === 'halt' ? halt.reason : null, seat_wall: null},
      open_call_count: totals ? totals.open : null,
      stats: {...raw.stats, wakes_1h: raw.sources?.wakes?.state === 'known' ? wakes.filter(w => fin(w.at) && w.at > now - 3600).length : null},
      health: seats ? {
        summary: {text: parked.length ? parked.length + ' of ' + idle.length + ' seats parked' : idle.length + ' seats, none parked', state: parked.length ? 'late' : 'ok'},
        jobs: filler && fillerAt ? [{id: 'paper-filler', label: 'Paper filler (marks and outcomes)', state: fillerAt ? (now - fillerAt < 900 ? 'ok' : 'late') : null, last_success_at: fillerAt, last_error: null,
          consecutive_failures: null, schedule_note: 'Ticks while orders are open; idle with none'}] : [],
        seats: Object.fromEntries(idle.map(([role, s]) => [role, {seat: role, state: s.office_status === 'parked' ? 'down' : s.office_status ? 'ok' : null, last_success_at: s.last_at,
          last_error: s.office_status === 'parked' ? {text: 'Parked: ' + (s.parked_reason ?? 'reason unknown'), at: s.last_at} : null, consecutive_failures: s.errors ?? null}])),
      } : undefined,
      ideas: {proposal_counts: raw.requests ? {open: requests.length} : undefined, proposals_open: raw.requests ? requests.length : null, proposal_history: raw.requests ? requests : undefined, briefs: [], leads: []},
      calendar: [],
      situation: {stories: [], as_of: raw.as_of},
      memes: {paper: {items: [], counts: {}, state: 'chart_only'}},
    };
  }

  window.OfficeAdapter = {adapt};
})();
