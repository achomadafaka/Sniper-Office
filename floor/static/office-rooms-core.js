(() => {
  'use strict';
  const unknown = 'unknown';
  const text = value => value == null || value === '' ? unknown : String(value);
  const num = (value, digits = 2) => Number.isFinite(value) ? value.toFixed(digits) : unknown;
  const pp = value => Number.isFinite(value) ? `${value >= 0 ? '+' : ''}${value.toFixed(1)} pp` : unknown;
  const pct = value => Number.isFinite(value) ? `${value.toFixed(2)}%` : unknown;
  const clock = value => Number.isFinite(value) ? new Date(value * 1000).toISOString().slice(0, 16).replace('T', ' ') + ' UTC' : unknown;
  const missing = value => value == null || (typeof value === 'object' && !(value instanceof Node))
    || /^(?:unknown|unreviewed|none recorded|—)?$/i.test(String(value).trim());
  const addressPattern = /\b(?:0x[a-f0-9]{40,64}|[1-9A-HJ-NP-Za-km-z]{44})\b/gi;
  const dateFor = value => clock(Number(value) / (value.length === 13 ? 1000 : 1));
  const plain = value => String(value)
    .replace(addressPattern, id => id.slice(0, 6) + '…' + id.slice(-4))
    .replace(/\b(?:1[5-9]\d{8}|2[0-4]\d{8})(?:\d{3})?\b/g, dateFor)
    .replace(/[{}\[\]]/g, '')
    .replace(/\b\w+(?:_\w+)+\b/g, key => key.replaceAll('_', ' '));
  function readable(node, value) {
    if (missing(value)) return;
    const source = String(value);
    let end = 0;
    for (const match of source.matchAll(addressPattern)) {
      const id = match[0];
      node.append(plain(source.slice(end, match.index)), window.OfficeNames ? window.OfficeNames.scrub(id) : plain(id));
      if (node.tagName !== 'BUTTON') {
        const copy = document.createElement('button');
        copy.type = 'button';
        copy.textContent = 'Copy';
        copy.setAttribute('aria-label', 'Copy address');
        copy.addEventListener('click', async () => {
          try {
            await navigator.clipboard.writeText(id);
            copy.textContent = 'Copied';
          } catch {
            copy.textContent = 'Copy unavailable';
          }
        });
        node.append(' ', copy);
      }
      end = match.index + id.length;
    }
    node.append(plain(source.slice(end)));
  }
  window.OfficeDetail = {missing, readable};
  const bookList = data => data.floor?.book_list ?? [['stocks', 'Stocks'], ['crypto', 'Perps']];
  const age = value => {
    if (!Number.isFinite(value) || value <= 0) return null;
    const seconds = Math.max(0, Date.now() / 1000 - value);
    const [scale, unit] = seconds >= 86400 ? [86400, 'd'] : seconds >= 3600 ? [3600, 'h'] : seconds >= 60 ? [60, 'min'] : [1, 's'];
    return Math.floor(seconds / scale) + ' ' + unit + ' ago';
  };
  const price = (value, asset = 'USD') => Number.isFinite(value) ? (asset === 'USD' ? '$' + num(value, value < 1 ? 6 : 2) : num(value, 4) + ' ' + asset) : null;
  const dollars = value => Number.isFinite(value)
    ? new Intl.NumberFormat('en-US', {style: 'currency', currency: 'USD', notation: 'compact', maximumFractionDigits: 1}).format(value)
    : null;
  function el(tag, value) {
    const node = document.createElement(tag);
    if (value != null) readable(node, value);
    return node;
  }
  function block(...children) {
    const node = el('div');
    node.append(...children.filter(Boolean));
    return node;
  }
  function section(title, ...children) {
    const node = el('section');
    const visible = children.filter(child => child?.textContent.trim() || child?.querySelector('canvas'));
    if (visible.length) node.append(el('h3', title), ...visible);
    return node;
  }
  function rows(headers, items, cells, id) {
    if (!Array.isArray(items) || !items.length) return el('div');
    const values = items.map(cells);
    const columns = headers.map((_, index) => index).filter(index => values.some(row =>
      row[index] instanceof Node ? row[index].textContent.trim() : !missing(row[index])));
    if (!columns.length) return el('div');
    const table = el('table');
    const header = el('tr');
    let sortColumn = -1, descending = false;
    const collator = new Intl.Collator(undefined, {numeric: true, sensitivity: 'base'});
    for (const [index, original] of columns.entries()) {
      const heading = headers[original];
      const th = el('th');
      const button = el('button', heading);
      button.type = 'button';
      button.addEventListener('click', () => {
        descending = sortColumn === index && !descending;
        sortColumn = index;
        const entries = [...body.rows];
        const sortValue = row => row.cells[index].textContent;
        entries.sort((a, b) => {
          const x = sortValue(a), y = sortValue(b);
          if (missing(x) || missing(y)) return Number(missing(x)) - Number(missing(y));
          const nx = Number.parseFloat(x), ny = Number.parseFloat(y);
          const order = Number.isFinite(nx) && Number.isFinite(ny) && !x.includes(' UTC') && !y.includes(' UTC')
            ? nx - ny : collator.compare(x, y);
          return descending ? -order : order;
        });
        for (const item of header.cells) item.removeAttribute('aria-sort');
        th.setAttribute('aria-sort', descending ? 'descending' : 'ascending');
        body.append(...entries);
      });
      th.append(button);
      th.scope = 'col';
      header.append(th);
    }
    const head = el('thead');
    head.append(header);
    const body = el('tbody');
    for (const [i, item] of items.entries()) {
      const row = el('tr');
      const key = id?.(item);
      if (key != null) row.dataset.itemId = key;
      for (const index of columns) {
        const cell = values[i][index];
        const td = el('td');
        td.dataset.label = headers[index];
        if (cell instanceof Node) td.append(cell);
        else if (missing(cell)) {
          td.textContent = '—';
          td.title = 'No value was recorded for this row';
        } else readable(td, cell);
        row.append(td);
      }
      body.append(row);
    }
    table.append(head, body);
    const wrap = block(table);
    wrap.className = 'od-table';
    return wrap;
  }
  function details(title, fields, id) {
    const data = fields.filter(([, value]) => !missing(value));
    const node = el('details');
    if (!data.length) return node;
    if (id != null) node.dataset.itemId = id;
    const list = el('dl');
    for (const [label, value] of data) list.append(el('dt', label), el('dd', value));
    node.append(el('summary', title), list);
    return node;
  }
  const replyFields = {
    decision: 'Decision', assessment: 'Assessment', why: 'Reason', reason: 'Reason', case: 'Case',
    lean: 'Lean', story: 'Market story', read: 'Read', priced_story: 'What the price implies',
    is_new_information: 'New information', overreaction_risk: 'Overreaction risk',
    finding: 'Finding', purpose: 'Purpose', numbers: 'Numbers', key_facts: 'Key facts', sources: 'Sources',
    new_in_48h: 'Recent evidence', scheduled_next: 'Coming up', possibly_mispriced: 'Possible mispricing',
    catalyst: 'Catalyst', rule_clause: 'Relevant rule', skill_steps: 'Skill steps', strategy_reason: 'Strategy reason',
    expected_move_pp: 'Expected move', confidence: 'Confidence',
  };
  function seatReply(reply) {
    return details('Read reply', Object.entries(replyFields).flatMap(([key, label]) => {
      const value = reply?.[key];
      if (Array.isArray(value)) return value.filter(v => typeof v === 'string').map(v => [label, v]);
      if (key === 'expected_move_pp') return [[label, pp(value)]];
      if (key === 'confidence') return [[label, probability({probability_yes: value})]];
      return [[label, typeof value === 'boolean' ? (value ? 'Yes' : 'No') : value]];
    }));
  }
  function facts(fact) {
    return fact && !missing(fact.value) ? [fact.field, fact.value, fact.source].filter(v => !missing(v)).join(' · ') : null;
  }
  const figure = (label, value, signed) => ({label, value, tone: value == null || value === unknown ? 'unknown'
    : signed > 0 ? 'positive' : signed < 0 ? 'negative' : ''});
  const first = (list, n) => Array.isArray(list) ? list.slice(0, n) : null;
  const laneName = value => value === 'experiment' ? 'Experiment' : value === 'main' ? 'Main' : null;
  const openHeaders = ['Market', 'Side', 'Entry', 'Provisional mark', 'Strategy', 'Age', 'Days to resolution', 'Review', 'Lane'];
  function openCalls(data) {
    return data.open_calls?.map(call => {
      const row = data.board?.find(r => r.condition_id === call.market_id && r.decision_at === call.called_at);
      const strategy = data.strategies?.strategies?.find(s => s.records.some(r => r.call_id === row?.call_id && r.call_id != null));
      const review = data.pending_reviews?.find(r => r.market_id === call.market_id && r.called_at === call.called_at);
      return {...call, ...row, strategy: strategy?.name, review: review ? 'Close or hold requested' : null};
    });
  }
  const openRows = c => [c.question ?? c.market, c.side, Number.isFinite(c.entry ?? c.entry_ask) ? num((c.entry ?? c.entry_ask) * 100, 1) + '¢' : null, pp(c.latest_net_pp),
    c.strategy, age(c.called_at),
    Number.isFinite(c.days_to_resolution) ? c.days_to_resolution
      : Number.isFinite(Date.parse(c.market_ends_utc)) ? Math.max(0, Math.ceil((Date.parse(c.market_ends_utc) - Date.now()) / 86400000))
      : unknown, c.thesis_invalidated ? 'Thesis invalidated' : c.review, laneName(c.book_lane)];
  const refused = data => data.cases?.filter(c => c.refused_call);
  const caseTitle = c => text(c.question ?? c.text);
  function compact(items, presenter) {
    if (!Array.isArray(items) || !items.length) return el('div');
    const list = el('ul');
    list.className = 'od-compact';
    for (const item of items) list.append(el('li', presenter(item)));
    return list;
  }

  const studyHorizons = ['15m', '1h', '6h', '24h', 'resolution'];
  const studyPoints = value => Number.isFinite(value) ? pp(value) : 'Not scored';
  const studyControl = control => {
    const count = control?.pairs;
    const score = studyPoints(control?.difference_pp);
    return Number.isInteger(count) ? score + ' · ' + count + (count === 1 ? ' pair' : ' pairs') : score;
  };
  const studyMoney = value => Number.isFinite(value) ? '$' + num(value) : 'Not scored';
  const studyState = data => !data ? 'No ledger published.'
    : data.state === 'reader_pending' ? 'Score reader pending.'
    : data.state === 'unavailable' ? 'Score unavailable.'
    : null;
  const scanReasons = {outside_horizon: 'closes more than 48 hours away', no_deciding_fact: 'no number the desk can check', edge_under_cost: 'gain smaller than the cost',
    payoff_shape: 'risks too much for the gain', quote_or_fee_unknown: 'price or fee unknown'};
  function scannerPanel(data) {
    const scan = data.scanner_scan, scores = data.scanner_scores;
    const live = (data.scanner_candidates ?? []).filter(c => c.end_at > Date.now() / 1000 && Date.now() / 1000 - c.read_at < 900);
    return section('Scanner',
      el('p', 'Looks for short-dated markets whose answer can be checked against a price feed or a post counter.'),
      scan ? el('p', `Last look ${clock(scan.finished_at)}: ${scan.scanned} markets, ${scan.accepted} worth a bet.`) : el('p', 'No scan recorded yet.'),
      scan ? rows(['Why markets were left out', 'Markets'], Object.entries(scan.refused ?? {}).sort((a, b) => b[1] - a[1]), ([why, n]) => [scanReasons[why] ?? why.replaceAll('_', ' '), n]) : null,
      live.length ? rows(['Worth a bet now', 'Side', 'Price', 'Gain after costs'], live, c => [c.question, c.side, num(c.indicative_ask * 100, 0) + '¢', num(c.edge_after_cost_pp, 1) + '¢'])
        : el('p', 'Nothing worth a bet right now.'),
      scores ? el('p', `Past signals that have settled: checked facts right ${scores.proof.correct} of ${scores.proof.resolved}, forecasts right ${scores.forecast.correct} of ${scores.forecast.resolved}.`) : null);
  }
  function livePositions(book, only) {
    const forced = only === 'Forced traders';
    const title = forced ? 'Forced trader positions' : 'Live positions';
    const keep = name => forced ? name === 'Forced perps' || name === 'Forced stocks' : !only || name === only;
    if (!book?.positions) return forced ? null : section(title, el('p', 'Live positions unavailable.'));
    const shown = Object.entries(book.books).filter(([name]) => keep(name));
    if (forced && !shown.length) return null;
    const unreadable = shown.filter(([, b]) => b.state === 'unreadable').map(([name]) => name);
    const total = shown.map(([name, b]) => [name, b.open, b.marked, Number.isFinite(b.net_usd) ? '$' + num(b.net_usd) : null]);
    const price = value => Number.isFinite(value) ? forced ? '$' + num(value) : num(value * 100, 1) + '¢' : null;
    return section(title, el('p', 'Priced ' + clock(book.at) + (forced
      ? '. Paper estimates at the latest finalized minute close.' : '. What each bet would sell for now, updated about every minute.')),
      unreadable.length ? el('p', 'Ledger unreadable: ' + unreadable.join(', ')) : null,
      rows(['Book', 'Open', 'Priced', 'Net dollars'], total, r => r),
      rows(['Book', 'Agent', 'Market', 'Side', 'Opened', 'Entry', forced ? 'Minute mark' : 'Bid now', 'Price age', 'Net', 'State'],
        book.positions.filter(p => keep(p.book)),
        p => [p.book, p.seat, p.question ?? p.market_id, p.side, clock(p.opened_at), price(p.entry), price(forced ? p.mark : p.bid),
          Number.isFinite(p.age_s) ? Math.round(p.age_s) + ' s' : null,
          Number.isFinite(p.net_usd) ? '$' + num(p.net_usd) : Number.isFinite(p.net_pp) && p.entry > 0 ? '$' + num(p.net_pp / 10 / p.entry, 2) + ' per $10' : null, p.status]));
  }
  const horizonSeconds = {'15m': 900, '1h': 3600, '6h': 21600, '24h': 86400};
  const newsNow = (r, live) => {
    const now = live?.[r.position_id];
    if (now && Number.isFinite(now.net_usd)) return '$' + num(now.net_usd) + ' now (bid ' + num(now.bid * 100, 1) + '¢)';
    if (now) return now.status;
    const hit = ['resolution', '24h', '6h', '1h', '15m'].find(h => Number.isFinite(r.net_pp?.[h]));
    return hit ? pp(r.net_pp[hit]) + ' at ' + hit : r.filled === false ? 'No trade' : 'First price at 15 min';
  };
  const newsFinal = r => {
    if (Number.isFinite(r.net_pp?.[r.horizon])) return pp(r.net_pp[r.horizon]);
    if (r.filled === false) return 'No trade';
    const due = Number.isFinite(r.at) && horizonSeconds[r.horizon] ? r.at + horizonSeconds[r.horizon] : null;
    if (due == null) return r.horizon === 'resolution' ? 'When the market settles' : 'Not scored';
    return due > Date.now() / 1000 ? 'Due ' + clock(due) : 'Waiting for price';
  };
  function newsCallLab(report, book) {
    const live = Object.fromEntries((book?.positions ?? []).filter(p => p.position_id).map(p => [p.position_id, p]));
    const state = studyState(report);
    if (state) return section('News calls', el('p', state));
    const latest = first(report.latest, 20);
    const seats = Object.entries(report.by_seat ?? {}).flatMap(([seat, scores]) => scores?.blocked
      ? [[seat, null, null, scores.blocked]]
      : studyHorizons.filter(horizon => scores?.[horizon]?.scored > 0).map(horizon => [seat, horizon, scores[horizon], null]));
    return section('News calls',
      Number.isFinite(report.at) ? el('p', 'Scored ' + clock(report.at)) : null,
      latest?.length ? rows(['Time', 'Seat', 'Headline', 'Market', 'Side', 'Horizon', 'Ask', 'Paper fill', 'Net so far', 'Final at horizon'],
        latest, r => [clock(r.at), r.seat, r.headline ?? r.headline_id, r.question ?? r.market_id, r.side,
          r.horizon, r.ask, r.filled === true ? 'Filled' : r.filled === false ? 'Unfilled' : 'Unknown',
          newsNow(r, live), newsFinal(r)], r => r.position_id) : el('p', 'No calls recorded.'),
      seats.length ? el('p', 'Descriptive results for each seat’s first 100 filled calls.') : null,
      rows(['Seat', 'Horizon', 'Scored', 'Net dollars', 'Mean net', 'vs Primary', 'vs Opposite', 'vs Matched random',
        'vs Random catalog', 'Status'], seats, ([seat, horizon, score, blocked]) => score
        ? [seat, horizon, score.scored, studyMoney(score.net_usd), studyPoints(score.net_pp),
          studyControl(score.versus_primary), studyControl(score.versus_opposite),
          studyControl(score.versus_random_match), studyControl(score.versus_random_catalog), null]
        : [seat, horizon, null, null, null, null, null, null, null, 'Not scored: ' + blocked.replaceAll('_', ' ')]));
  }
  function seatPickLab(report) {
    const state = studyState(report);
    if (state) return section('Forced picks', el('p', state));
    const picks = Object.entries(report.by_seat_kind ?? {}).flatMap(([seat, kinds]) =>
      kinds?.blocked ? [[seat, null, null, null, kinds.blocked]]
        : Object.entries(kinds ?? {}).flatMap(([kind, horizons]) =>
          Object.entries(horizons ?? {}).map(([horizon, score]) => [seat, kind, horizon, score, null])));
    const due = Object.entries(report.flow ?? {});
    return section('Forced picks',
      el('p', 'Descriptive paper results. Each story has equal weight in mean net points.'),
      Number.isFinite(report.at) ? el('p', 'Scored ' + clock(report.at)) : null,
      picks.length ? rows(['Seat', 'Market kind', 'Horizon', 'Stories scored', 'Net dollars so far', 'Mean net so far'],
        picks, ([seat, kind, horizon, score, blocked]) => [seat, kind, horizon, score?.stories,
          blocked ? 'Not scored: ' + String(blocked).replaceAll('_', ' ') : studyMoney(score?.net_usd),
          blocked ? null : studyPoints(score?.net_pp)]) : el('p', 'No scores published.'),
      rows(['Seat', 'Next due (UTC)'], due, ([seat, flow]) => [seat,
        Number.isFinite(flow?.next_due_at) ? clock(flow.next_due_at) : 'No due time reported']));
  }

  function algorithmLab(report) {
    const state = studyState(report);
    if (state) return section('Algorithms', el('p', state));
    const phases = first(report.algorithms, 25)?.flatMap(algorithm =>
      [['Backtest', algorithm.replay], ['Forward paper', algorithm.paper]].map(([phase, metrics]) =>
        [algorithm, phase, metrics]));
    return section('Algorithms',
      ...(report.limits ?? []).map(text => el('p', text)),
      phases?.length ? rows(['Name', 'Idea', 'Phase', 'Price evidence', 'Status', 'Decisions', 'Fills', 'Closed', 'Open', 'Missing quotes',
        'Realized net', 'Open cost', 'Last executable open value', 'Oldest open mark (UTC)'],
        phases, ([a, phase, m]) => [a.title, a.rationale, phase,
          m?.price_source === 'mid_only' ? 'Mid-only proxy, 1.5¢ half-spread plus fees' :
            m?.price_source === 'real_book' ? 'Real book' : null,
          m?.status ?? a.status,
          m?.decisions, m?.fills, m?.closed, m?.open, m?.missing_quotes, studyMoney(m?.net_usd),
          studyMoney(m?.open_cost_usd), studyMoney(m?.open_value_usd),
          Number.isFinite(m?.oldest_open_mark_at) ? clock(m.oldest_open_mark_at) + ' (' + age(m.oldest_open_mark_at) + ')' : 'Not scored']) : el('p', 'No algorithms recorded.'));
  }

  function muskLab(report) {
    const state = studyState(report);
    if (state) return section('Musk paper lane', el('p', state));
    const arms = ['musk-v1', 'musk-v2'].filter(name => report.arms?.[name]);
    const lines = arms.flatMap(name => {
      const arm = report.arms[name];
      return [[name, 'Strategy', arm], [name, 'Opposite side', arm.controls?.opposite],
        [name, 'Same-window leg', arm.controls?.same_window]];
    });
    return section('Musk paper lane',
      report.status && report.status !== 'ok' ? el('p', report.status) : null,
      lines.length ? rows(['Arm', 'Lane', 'Entries', 'Open cost', 'Executable open value',
        'Valued open', 'Unvalued open', 'Unvalued cost', 'Settled entries', 'Settled net', 'Windows',
        'Settled windows', 'Void entries', 'Void cost', 'Unknown controls'],
      lines, ([name, lane, metrics]) => {
        const value = studyMoney(metrics?.executable_open_value_usd);
        return [name, lane, metrics?.entries, studyMoney(metrics?.open_cost_usd),
          metrics?.unvalued_open > 0 ? value + ' (partial)' : value,
          metrics?.valued_open, metrics?.unvalued_open, studyMoney(metrics?.unvalued_cost_usd),
          metrics?.settled_entries, studyMoney(metrics?.settled_net_usd), metrics?.windows,
          metrics?.settled_windows, metrics?.void_entries, studyMoney(metrics?.void_cost_usd),
          lane === 'Strategy' ? null : metrics?.unknown];
      }) : el('p', 'No forward paper entries recorded.'));
  }

  OfficeRooms.register({
    id: 'paper-labs', title: 'Paper Labs', order: 14, seats: [],
    figures(data) {
      return [figure('News calls', data.news_call_lab?.calls),
        figure('Algorithms', data.algorithm_lab?.algorithms?.length)];
    },
    summary(data) {
      const call = data.news_call_lab?.latest?.[0];
      const algorithm = data.algorithm_lab?.algorithms?.[0];
      return block(call ? el('p', call.headline ?? call.question ?? call.market_id) : null,
        algorithm ? el('p', algorithm.title) : null);
    },
    detail(data) {
      return block(data.live_book && livePositions(data.live_book, 'News calls'),
        data.live_book && livePositions(data.live_book, 'Forced traders'),
        data.seat_pick_lab && seatPickLab(data.seat_pick_lab),
        data.news_call_lab && newsCallLab(data.news_call_lab, data.live_book),
        data.algorithm_lab && algorithmLab(data.algorithm_lab), data.musk_lab && muskLab(data.musk_lab));
    },
  });

  OfficeRooms.register({
    id: 'decision', title: 'Decision', order: 10, seats: [],
    figures(data) {
      const resolved = data.scorecard?.totals?.markets?.resolved?.sum_pp;
      return [figure('Resolved points', pp(resolved), resolved), figure('Open calls', data.open_call_count)];
    },
    summary(data) {
      return block(el('p', [[data.pending_reviews, 'reviews'], [data.scanner_candidates, 'candidates']]
        .filter(([items]) => Array.isArray(items)).map(([items, label]) => items.length + ' ' + label).join(' · ')),
        compact(first(openCalls(data), 2), c => openRows(c).slice(0, 4).filter(v => !missing(v)).join(' · ')));
    },
    detail(data) {
      if (!data.live_book && !data.open_calls && !data.board) return null;
      return block(
        livePositions(data.live_book, 'Main book'),
        section('Open calls', rows(openHeaders, openCalls(data), openRows, c => c.call_id),
          data.open_calls?.some(call => laneName(call.book_lane)) ? el('p', 'Exposure caps apply across both lanes.') : null),
        section('Close or hold reviews', rows(['Market', 'Review', 'Side', 'Horizon', 'Horizon quote at', 'Horizon exit price',
          'Horizon net P&L', 'Review at', 'Review bid', 'Review fair', 'Hold edge before costs', 'Previous evidence', 'New evidence'],
          data.call_reviews ?? data.pending_reviews, r => [r.question ?? r.market_id, r.outcome ?? r.status, r.side,
            Number.isFinite(r.original_horizon?.seconds) ? r.original_horizon.seconds / 60 + ' min' : null,
            clock(r.original_horizon?.at), num(r.original_horizon?.exit_price),
            r.original_horizon?.net_pp == null && r.original_horizon?.due_at > Date.now() / 1000
              ? 'Not reached until ' + clock(r.original_horizon.due_at) : pp(r.original_horizon?.net_pp),
            clock(r.at), num(r.close_bid), num(r.fair_price), pp(r.hold_edge_before_costs_pp), facts(r.old_fact), facts(r.new_fact)], r => r.item_id)),
        scannerPanel(data),
        section('Resolved calls', rows(['Market', 'Side', 'Net points', 'Playbook'],
          data.board?.filter(c => Number.isFinite(c.resolved_pp)),
          c => [c.question ?? c.condition_id, c.side, pp(c.resolved_pp), c.primary_playbook], c => c.call_id)),
        section('Previously opened calls', rows(['Market', 'Side', 'Entry', 'Status', 'Exit', 'Net after exit', 'Net if held to result'], data.board,
          c => [c.question ?? c.condition_id, c.side, Number.isFinite(c.entry) ? num(c.entry * 100, 1) + '¢' : null,
            c.paper_exit?.state === 'unknown' ? 'Exit record unreadable' : c.paper_exit?.state === 'closed' ? 'Closed early' : c.paper_exit?.state === 'swapped' ? 'Swapped' : Number.isFinite(c.resolved_pp) ? 'Resolved' : 'Open',
            Number.isFinite(c.paper_exit?.exit_bid) ? num(c.paper_exit.exit_bid * 100, 1) + '¢' : null, pp(c.paper_exit_net_pp),
            Number.isFinite(c.resolved_pp) ? pp(c.resolved_pp) : 'Not resolved'], c => c.call_id)),
        section('Refused calls', rows(['Market', 'Stated edge', 'Required edge', 'Reason', 'Evidence'], refused(data),
          c => [caseTitle(c), pp(c.refused_call.stated_edge_pp ?? c.refused_call.expected_move_pp),
            pp(c.refused_call.minimum_edge_pp ?? c.refused_call.required_edge_pp),
            c.refused_call.reason ?? c.outcome, details('Cost breakdown', [['Spread', pp(c.refused_call.spread_pp)], ['Fees', pp(c.refused_call.fee_pp)]])], c => c.item_id))
      );
    },
  });

  OfficeRooms.register({
    id: 'strategies', title: 'Strategy Lab', order: 12, seats: [],
    summary() { return null; },
    detail() { return null; }, // rendered from the markets ledger by office-live.js
  });

  const probability = reply => Number.isFinite(reply?.probability_yes) ? `${Math.round(100 * reply.probability_yes)}%` : unknown;
  const duration = ms => Number.isFinite(ms) ? `${(ms / 1000).toFixed(1)} s` : unknown;
  OfficeRooms.register({
    id: 'debate', title: 'Debate', order: 20, seats: [],
    figures(data) {
      return [figure('Debates in the last hour', data.stats?.debates_1h),
        figure('Latest debate time', duration(data.cases?.[0]?.total_ms))];
    },
    summary(data) {
      const c = data.cases?.[0];
      if (!c) return el('p', unknown);
      return block(el('p', caseTitle(c)), el('p', [c.outcome, duration(c.total_ms)].filter(v => !missing(v)).join(' · ')),
        el('p', Object.entries(c.seats ?? {}).filter(([, reply]) => Number.isFinite(reply?.probability_yes))
          .map(([seat, reply]) => `${seat} ${probability(reply)}`).join(' · ')));
    },
    detail(data) {
      if (!Array.isArray(data.cases) || !data.cases.length) return null;
      const narrative = data.narrative_hour;
      const measured = narrative && section('Narrative one-hour score · last 24 h',
        el('p', `${narrative.scored} of ${narrative.classified} briefs scored · ${narrative.recorded} with a starting quote`),
        narrative.scored ? el('p', `${narrative.correct} correct vs ${narrative.always_no_move_correct} for always no move · ${narrative.caught} of ${narrative.moved} moves caught`) : null,
        el('p', 'A move is an absolute midpoint change of at least the starting spread.'));
      return block(measured, ...data.cases.map(c => {
        const node = section(caseTitle(c), el('p', [age(c.at), c.outcome, duration(c.total_ms)].filter(v => !missing(v)).join(' · ')),
          rows(['Seat', 'YES probability', 'Turn time', 'Reply and skill steps'],
            Object.entries(c.seats ?? {}), ([seat, reply]) => [seat, probability(reply),
              duration(c.seat_timings?.[seat] ?? c.seat_ms?.[seat]), seatReply(reply)]));
        node.dataset.itemId = c.item_id ?? '';
        return node;
      }));
    },
  });

  const trades = book => book?.calls?.filter(c => !c.outcome);
  const tradeCells = (c, asset) => [c.symbol, c.side, price(c.entry, asset), pct(c.stop_pct), pct(c.target_pct), price(c.mark, asset), laneName(c.book_lane)];
  const tradeHeaders = ['Symbol', 'Side', 'Entry', 'Stop %', 'Target %', 'Mark', 'Lane'];
  const tradeId = (book, c) => `${book}:${c.symbol}:${c.at}`;
  function charts(series) {
    if (!series?.length) return el('div');
    return block(...series.map(chart => {
      const bars = (chart.bars ?? []).filter(bar => Number.isFinite(bar.close));
      const item = section(`${text(chart.symbol)} · ${text(chart.source)}`,
        el('p', [age(chart.observed_at), Number.isFinite(chart.interval_s) ? `${Math.round(chart.interval_s)} s bars` : null].filter(Boolean).join(' · ')));
      if (bars.length < 2) {
        item.append(el('p', missing(chart.reason) ? 'Fewer than two closing prices were saved.' : chart.reason));
        return item;
      }
      const canvas = el('canvas');
      const closes = bars.map(bar => bar.close);
      const lo = Math.min(...closes), hi = Math.max(...closes);
      canvas.width = 560;
      canvas.height = 100;
      canvas.setAttribute('role', 'img');
      canvas.setAttribute('aria-label', `${chart.symbol} recorded close prices from ${num(lo)} to ${num(hi)}`);
      const ctx = canvas.getContext('2d');
      ctx.strokeStyle = '#8be2d0';
      ctx.lineWidth = 2;
      ctx.beginPath();
      closes.forEach((price, i) => {
        const x = 4 + 552 * i / (closes.length - 1);
        const y = hi === lo ? 50 : 94 - 88 * (price - lo) / (hi - lo);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.stroke();
      item.append(canvas, el('p', `${price(lo, chart.quote_asset)}–${price(hi, chart.quote_asset)} · latest ${price(closes.at(-1), chart.quote_asset)}`));
      return item;
    }));
  }
  function tradeSummary(book) {
    return [Number.isFinite(book?.summary?.open) ? `${book.summary.open} open` : null,
      Number.isFinite(book?.today_net_pct) ? `Main today ${pct(book.today_net_pct)} net` : null,
      Number.isFinite(book?.summary?.net_usd) ? `Closed net ${book.summary.net_usd < 0 ? '-' : '+'}$${Math.abs(book.summary.net_usd).toFixed(2)} · ${book.summary.wins} of ${book.summary.scored} won` : null,
      !Number.isFinite(book?.summary?.net_usd) && Number.isFinite(book?.summary?.sum_net_pct) ? `Main total ${pct(book.summary.sum_net_pct)} net` : null].filter(Boolean).join(' · ');
  }
  OfficeRooms.register({
    id: 'trading', title: 'Trading', order: 30, seats: [],
    figures(data) {
      return bookList(data).slice(0, 2).map(([key, label]) => {
        const net = data[key]?.summary?.sum_net_pct;
        return figure(`${label} margin return`, pct(net), net);
      });
    },
    summary(data) {
      return block(...bookList(data).map(([key, label]) => {
        const c = trades(data[key])?.[0];
        return el('p', [label, Number.isFinite(data[key]?.summary?.open) ? data[key].summary.open + ' open' : null,
          ...(c ? tradeCells(c, key === 'crypto' ? 'USDT' : 'USD') : [])].filter(v => !missing(v)).join(' · '));
      }), el('p', data.trade_autopsies?.[0]?.autopsy?.verdict ? 'Latest autopsy · ' + data.trade_autopsies[0].autopsy.verdict : null));
    },
    detail(data) {
      return block(...bookList(data).map(([key, label]) => {
        const book = data[key], asset = key === 'crypto' ? 'USDT' : 'USD';
        return section(label, el('p', tradeSummary(book)), el('p', text(book?.read)),
          section('Open positions', rows(tradeHeaders, trades(book), c => tradeCells(c, asset), c => tradeId(key, c))),
          section('Charts', charts(book?.price_series)),
          section('Tickets and outcomes', rows(['Symbol', 'Side', 'Entry', 'Outcome', 'Net', 'Clock', 'Lane'], book?.calls,
            c => [c.symbol, c.side, price(c.entry, asset), c.outcome ?? 'Open', pct(c.net_pct), clock(c.exit_at ?? c.at), laneName(c.book_lane)], c => tradeId(key, c))),
          section('Queued entries', rows(['Symbol', 'Side', 'Trigger price', 'Order direction', 'Reason', 'Lane'], book?.pending,
            c => [c.symbol, c.side, price(c.trigger_price, asset), c.trigger_direction, c.thesis ?? c.reason, laneName(c.book_lane)])),
          section('Refusals', rows(['Symbol', 'Reason', 'Clock'], book?.refused, r => [r.symbol, r.why, clock(r.at)])),
          section('Autopsies', rows(['Symbol', 'Result', 'Verdict', 'After 1 h', 'After 4 h', 'Best move', 'Worst move'],
            data.trade_autopsies?.filter(a => a.book === key), a => [a.symbol, pct(a.net_pct), a.autopsy?.verdict ?? a.autopsy?.error ?? a.error,
              pct(a.autopsy?.direction_at_60m_pct), pct(a.autopsy?.direction_at_240m_pct),
              pct(a.autopsy?.best_move_in_window_pct), pct(a.autopsy?.worst_move_in_window_pct)])));
      }));
    },
  });

  const themes = data => data.memes?.passes?.flatMap(pass => (pass.narratives ?? []).map(theme => ({at: pass.at, ...theme})));
  const rates = data => data.memes?.by_conviction ? Object.entries(data.memes.by_conviction) : null;
  function memeCount(data, key) {
    const records = rates(data)?.map(([, row]) => row);
    return records?.every(row => Number.isFinite(row[key]))
      ? records.reduce((sum, row) => sum + row[key], 0) : null;
  }
  function rate(row) {
    const {hit, miss} = row;
    return Number.isFinite(hit) && Number.isFinite(miss) && hit + miss > 0
      ? `${Math.round(100 * hit / (hit + miss))}% of ${hit + miss}` : unknown;
  }
  const tape = data => data.memes?.launch_tape?.error ? null : data.memes?.launch_tape?.groups;

  function lookoutHeadline(item) {
    if (!item.source_headline) return null;
    try {
      const url = new URL(item.source_url);
      if (['http:', 'https:'].includes(url.protocol)) {
        const anchor = el('a', item.source_headline);
        anchor.href = url.href;
        anchor.target = '_blank';
        anchor.rel = 'noopener noreferrer';
        return anchor;
      }
    } catch {}
    return el('p', item.source_headline);
  }

  function lookout(data) {
    const watch = data.memes?.lookout;
    const stories = Array.isArray(watch?.stories) ? watch.stories : null;
    const notes = Array.isArray(watch?.notes) ? watch.notes : null;
    const errors = data.memes?.launch_watch_status?.errors ?? {};
    const stale = errors.watch_list_stale_s;
    const idle = Number.isFinite(stale) ? `Launch watch idle: story list ${Math.floor(stale / 60)} min old.`
      : errors.watch_list_error ? `Launch watch idle: story list ${errors.watch_list_error}.` : null;
    const capped = Object.keys(errors).filter(word => errors[word] === 'search capped');
    return block(section('Lookout stories',
      idle ? el('p', idle) : null,
      capped.length ? el('p', `Search capped for ${capped.join(', ')}. Launch coverage unknown.`) : null,
      watch?.watch_as_of ? el('p', `Watch read ${clock(watch.watch_as_of)}`) : null,
      !Array.isArray(stories) ? el('p', 'Watch list unavailable.') :
        stories.length ? null : el('p', 'No active lookout stories.'),
      ...(stories ?? []).map(story => {
        const chains = [...new Set((story.coins ?? []).map(coin => coin.chain))];
        return block(el('h4', story.keywords.join(' · ')),
          el('p', `${story.headline_count} distinct headlines · Latest ${clock(story.last_at)}`), lookoutHeadline(story),
          !Array.isArray(story.coins) ? el('p', 'Coin observations unavailable.') :
            chains.length ? null : el('p', 'No matching coins in the retained searches.'),
          ...chains.map(chain => block(el('h4', chain),
            rows(['Coin', 'Contract address', 'Observed', 'FDV', '24 h volume'], story.coins.filter(coin => coin.chain === chain),
              coin => [coin.symbol ? `${coin.name} (${coin.symbol})` : coin.name, coin.address,
                clock(coin.observed_at), dollars(coin.fdv), dollars(coin.volume_h24)]))));
      })),
    section('Notes sent',
      watch?.notes_since ? el('p', `Latest 50 delivery receipts · Recording started ${clock(watch.notes_since)}`) : null,
      !Array.isArray(notes) ? el('p', 'Delivery history unavailable.') :
        notes.length ? null : el('p', 'No confirmed sends recorded.'),
      rows(['Sent', 'Note', 'Story', 'Headlines', 'Source', 'Chain', 'Contract address'],
        [...(notes ?? [])].reverse(), note => [clock(note.at), note.kind === 'lookout' ? 'Lookout' : 'Coin live',
          note.keywords.join(', '), note.headline_count, lookoutHeadline(note), note.chain, note.address])));
  }

  const paperClock = ms => Number.isFinite(ms) ? new Date(ms).toISOString().slice(0, 19).replace('T', ' ') + ' UTC' : unknown;
  const paperValue = n => Number.isFinite(n) ? new Intl.NumberFormat('en-US', {maximumSignificantDigits: 8}).format(n) : unknown;
  let paperSelection;
  const armNames = {note_named: 'Note-named coin', busiest_fresh: 'Busiest fresh coin', first_observed: 'First observed launch'};
  function paperMarker(label, marker) {
    if (!marker) return el('p', label + ' unknown');
    return el('p', label + ' · ' + paperClock(marker.at_ms) + ' · Net USD ' + paperValue(marker.net_usd)
      + ' · Return ' + pct(marker.return_pct) + ' · ' + text(marker.source));
  }
  function drawPaperCandles(canvas, payload, marks) {
    const {from_ms: start, to_ms: end} = payload;
    const gaps = payload.gaps.filter(gap => Number.isFinite(gap.from_ms) && Number.isFinite(gap.to_ms)
      && gap.from_ms < end && gap.to_ms > start && gap.from_ms < gap.to_ms);
    const bars = payload.bars.filter(bar => Number.isFinite(bar.t_ms) && bar.t_ms >= start && bar.t_ms < end
      && [bar.open, bar.high, bar.low, bar.close].every(n => Number.isFinite(n) && n >= 0)
      && bar.high >= Math.max(bar.open, bar.close) && bar.low <= Math.min(bar.open, bar.close)
      && !gaps.some(gap => gap.from_ms <= bar.t_ms && bar.t_ms < gap.to_ms));
    const ctx = canvas.getContext('2d');
    const width = canvas.width = Math.max(260, canvas.clientWidth);
    const height = canvas.height = 210;
    const plotWidth = width - 58;
    const x = ms => 4 + plotWidth * (ms - start) / (end - start);
    ctx.fillStyle = '#eef2e8';
    ctx.fillRect(0, 0, width, height);
    ctx.fillStyle = '#dde3d7';
    for (const gap of gaps) {
      const left = x(Math.max(start, gap.from_ms));
      ctx.fillRect(left, 0, Math.max(1, x(Math.min(end, gap.to_ms)) - left), height - 24);
    }
    if (bars.length) {
      const low = Math.min(...bars.map(bar => bar.low));
      const high = Math.max(...bars.map(bar => bar.high));
      const pad = (high - low || high || 1) * .06;
      const y = n => 161 - 145 * (n - low + pad) / (high - low + 2 * pad);
      const candle = Math.max(1, Math.min(7, plotWidth * payload.interval_s * 700 / (end - start)));
      for (const bar of bars) {
        const center = x(bar.t_ms);
        ctx.fillStyle = bar.close >= bar.open ? '#287163' : '#a1483c';
        ctx.fillRect(center, y(bar.high), 1, Math.max(1, y(bar.low) - y(bar.high)));
        ctx.fillRect(center - candle / 2, Math.min(y(bar.open), y(bar.close)), candle,
          Math.max(1, Math.abs(y(bar.open) - y(bar.close))));
      }
      ctx.fillStyle = '#283c35';
      ctx.font = '11px monospace';
      ctx.fillText(payload.price_unit, width - 53, 9, 51);
      ctx.fillText(paperValue(high), width - 53, 20, 51);
      ctx.fillText(paperValue(low), width - 53, 160, 51);
    }
    for (const mark of marks) {
      if (!Number.isFinite(mark.at_ms) || mark.at_ms < start || mark.at_ms >= end) continue;
      ctx.fillStyle = mark.label === 'Note' ? '#42357e' : '#245979';
      const lane = {Note: 175, Entry: 180, High: 185, Low: 190}[mark.label];
      ctx.fillRect(x(mark.at_ms), lane, 2, 5);
    }
    ctx.fillStyle = '#283c35';
    ctx.font = '11px monospace';
    ctx.fillText(paperClock(start).slice(11, 19), 4, 207);
    ctx.fillText(paperClock(end).slice(11, 19), Math.max(4, plotWidth - 55), 207);
    return {bars, gaps};
  }
  function paperStudy(data) {
    const study = data.memes?.paper;
    if (study?.state === 'unavailable' || !Array.isArray(study?.items)) return section('Paper study', el('p', 'Paper chart data unavailable.'));
    const coins = study.items.filter(coin => coin?.chain && coin.address && Number.isSafeInteger(coin.window_start_ms)
      && Number.isSafeInteger(coin.window_end_ms) && coin.window_end_ms > coin.window_start_ms);
    const counts = study.counts || {};
    const status = el('p');
    status.setAttribute('role', 'status');
    const select = el('select');
    const source = el('select');
    const zoom = el('select');
    const prev = el('button', 'Earlier');
    const next = el('button', 'Later');
    const atNote = el('button', 'Capture start');
    const canvas = el('canvas');
    const details = el('div');
    const controls = block();
    controls.className = 'od-paper-controls';
    const panel = block(controls, status, canvas, details);
    panel.className = 'od-paper-study';
    canvas.setAttribute('role', 'img');
    canvas.setAttribute('aria-label', 'Observed candles with note and executable quote time markers');
    function label(name, control) {
      const wrapper = el('label', name);
      wrapper.append(control);
      return wrapper;
    }
    function option(value, name) {
      const item = el('option', name);
      item.value = value;
      return item;
    }
    [prev, next, atNote].forEach(button => {button.type = 'button';});
    controls.append(label('Coin', select), label('Source', source), label('Window', zoom), prev, next, atNote);
    for (const [i, coin] of coins.entries()) {
      select.append(option(String(i), `${coin.symbol || coin.address.slice(0, 8)} · ${coin.chain} · ${paperClock(coin.window_start_ms)}`));
    }
    for (const [value, name] of [[60, '1 minute'], [300, '5 minutes'], [3600, '1 hour']]) {
      zoom.append(option(value, name));
    }
    const keyOf = coin => coin.window_id || JSON.stringify([coin.chain, coin.address, coin.window_start_ms]);
    const previous = paperSelection;
    const selected = coins.findIndex(coin => keyOf(coin) === previous?.key);
    if (selected >= 0) select.value = String(selected);
    zoom.value = previous?.zoom || '300';
    let start = 0, pending, loaded, marks = [];
    const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(() => {
      if (!panel.isConnected) {observer.disconnect(); pending?.abort(); return;}
      if (loaded) drawPaperCandles(canvas, loaded, marks);
    }) : null;
    observer?.observe(canvas);
    async function load() {
      const coin = coins[Number(select.value)];
      pending?.abort();
      loaded = null;
      canvas.hidden = true;
      if (!coin) {status.textContent = 'No capture windows recorded.'; return;}
      const series = coin.series?.find(item => item.source === source.value);
      const span = Number(zoom.value) * 1000;
      const alignedStart = Math.ceil(start / 1000) * 1000;
      const end = Math.floor(Math.min(coin.window_end_ms, alignedStart + span) / 1000) * 1000;
      prev.disabled = alignedStart <= Math.ceil(coin.window_start_ms / 1000) * 1000;
      next.disabled = end >= Math.floor(coin.window_end_ms / 1000) * 1000;
      paperSelection = {key: keyOf(coin), start, source: source.value, zoom: zoom.value};
      if (!series) {status.textContent = coin.reason || 'Observed chart unavailable for this coin.'; return;}
      if (alignedStart >= end) {status.textContent = 'No complete seconds in this window.'; return;}
      status.textContent = 'Loading observed candles…';
      const params = new URLSearchParams({chain: coin.chain, address: coin.address, source: series.source,
        from_ms: alignedStart, to_ms: end});
      const viewStart = start;
      if (coin.window_id) params.set('window_id', coin.window_id);
      const request = pending = new AbortController();
      const timeout = setTimeout(() => request.abort(), 8000);
      try {
        const response = await fetch('/api/office/memes/paper/chart?' + params, {cache: 'no-store', signal: request.signal});
        if (!response.ok) throw new Error('Chart unavailable');
        const result = await response.json();
        if (request !== pending || !panel.isConnected) return;
        if (result.chain !== coin.chain || result.address !== coin.address || result.source !== series.source
          || result.from_ms !== Number(params.get('from_ms')) || result.to_ms !== end
          || !Number.isFinite(result.interval_s) || result.interval_s <= 0
          || !result.price_unit || result.price_unit !== series.price_unit
          || !Number.isFinite(result.effective_interval_s) || result.effective_interval_s <= 0
          || !result.commitment || !result.clock_basis
          || !Array.isArray(result.bars) || !Array.isArray(result.gaps) || result.bars.length > 3600) {
          throw new Error('Chart identity or window mismatch');
        }
        loaded = viewStart === alignedStart ? result : {...result, from_ms: viewStart, gaps: [
          {from_ms: viewStart, to_ms: alignedStart, reason: 'partial boundary second'}, ...result.gaps
        ]};
        canvas.hidden = false;
        const plotted = drawPaperCandles(canvas, loaded, marks);
        status.textContent = `${result.source} · ${result.price_unit} · ${result.effective_interval_s}s source cadence · ${plotted.bars.length} observed ${plotted.bars.length === 1 ? 'bar' : 'bars'}`;
        status.append(' · ', result.commitment, ' · ', result.clock_basis.replaceAll('_', ' '));
        if (result.basis) status.append(' · ', result.basis);
        if (!plotted.bars.length) status.append(' · No observed candles in this window.');
        const reasons = [...new Set(plotted.gaps.map(gap => gap.reason || 'coverage unknown'))];
        if (reasons.length) status.append(' · Gaps: ', reasons.join(', '));
      } catch {
        if (request === pending && panel.isConnected) status.textContent = 'Chart unavailable for this window.';
      } finally {
        clearTimeout(timeout);
      }
    }
    function choose(restore = false) {
      const coin = coins[Number(select.value)];
      if (!coin) return;
      source.replaceChildren(...(coin.series ?? []).map(item => option(item.source, item.source)));
      if (restore && [...source.options].some(item => item.value === previous.source)) source.value = previous.source;
      start = restore ? Math.min(coin.window_end_ms - 1, Math.max(coin.window_start_ms, previous.start)) : coin.window_start_ms;
      marks = (coin.notes ?? []).map(note => ({...note, label: 'Note'}));
      for (const arm of coin.arms ?? []) {
        for (const [key, name] of [['entry', 'Entry'], ['high', 'High'], ['low', 'Low']]) {
          if (arm[key]) marks.push({...arm[key], label: name});
        }
      }
      details.replaceChildren(el('p', `${coin.chain} · ${coin.address}`), el('p', 'Capture ' + text(coin.state)),
        el('p', 'Time markers: purple note; blue entry, observed high, and observed low on separate lanes.'),
        ...(coin.notes ?? []).map(note => el('p', `${note.kind === 'coin_live' ? 'Coin live' : 'Lookout'} · ${paperClock(note.at_ms)}`)),
        ...(coin.arms ?? []).map(arm => {
          const group = block(el('h4', armNames[arm.id] || text(arm.id)), el('p', text(arm.status) + (arm.reason ? ' · ' + arm.reason : '')),
            paperMarker('Entry quote', arm.entry), paperMarker('Observed executable high', arm.high), paperMarker('Observed executable low', arm.low));
          const coverage = arm.coverage || {};
          for (const [key, label] of [['eligible', 'Eligible'], ['scored', 'Scored'], ['quote_count', 'Quote receipts'],
            ['max_gap_s', 'Largest quote gap (s)'], ['capped', 'Capped'], ['evicted', 'Evicted'], ['no_route', 'No route']]) {
            if (Number.isFinite(coverage[key])) group.append(el('p', label + ' ' + coverage[key]));
          }
          if (coverage.reason) group.append(el('p', coverage.reason));
          if (arm.entry?.clip_usd != null) group.append(el('p', 'Entry clip USD ' + arm.entry.clip_usd));
          for (const rule of arm.exits ?? []) {
            const exit = el('details');
            exit.append(el('summary', `TP ${pct(rule.tp_pct)} · Stop ${pct(rule.stop_pct)} · ${text(rule.limit_s)} s · ${text(rule.status)}`),
              rule.reason ? el('p', rule.reason) : el('p'), paperMarker('Exit quote', rule.marker));
            group.append(exit);
          }
          return group;
        }));
      if (!coin.arms?.length) details.append(el('p', 'Executable paper results unavailable.'));
      load();
    }
    select.addEventListener('change', () => choose());
    source.addEventListener('change', load);
    zoom.addEventListener('change', load);
    const shift = direction => {
      const coin = coins[Number(select.value)];
      const shifted = Math.ceil(start / 1000) * 1000 + direction * Number(zoom.value) * 1000;
      start = shifted <= Math.ceil(coin.window_start_ms / 1000) * 1000 ? coin.window_start_ms : shifted;
      load();
    };
    prev.addEventListener('click', () => shift(-1));
    next.addEventListener('click', () => shift(1));
    atNote.addEventListener('click', () => {start = coins[Number(select.value)].window_start_ms; load();});
    if (coins.length) choose(selected >= 0);
    return section('Paper study', el('p', `Capped ${text(counts.capped)} · Evicted ${text(counts.evicted)} · No route ${text(counts.no_route)}`),
      coins.length ? panel : el('p', 'No capture windows recorded.'));
  }

  OfficeRooms.register({
    id: 'memes', title: 'Memes', order: 70, seats: ['memes'],
    figures(data) {
      return [figure('Hits', memeCount(data, 'hit')), figure('Misses', memeCount(data, 'miss'))];
    },
    summary(data) {
      return block(el('p', Number.isFinite(memeCount(data, 'unknown')) ? memeCount(data, 'unknown') + ' unscored' : null),
        el('p', (rates(data) ?? []).map(([conviction, row]) => `Conviction ${conviction}: ${row.hit} hit, ${row.miss} miss, ${row.unknown} unscored`).join(' · ') || unknown),
        compact(first(themes(data), 1), theme => `${text(theme.theme)} · ${text(theme.status)}`),
        compact(first(tape(data), 1), group => `${text(group.stem)} · ${text(group.tokens)} tokens`));
    },
    detail(data) {
      const ranked = (list) => rows(['Theme', 'Status', 'Conviction', 'Peak value', 'Recorded', 'Reason', 'Named chain evidence'], list,
        t => [t.theme, ({unknown: 'Outcome unscored', pending: 'Observation window open'})[t.status] ?? t.status,
          t.conviction, dollars(t.peak_fdv), age(t.at), t.outcome_reason,
          t.chain_status === 'confirmed' ? `Observed on ${t.predicted_chain}`
            : t.chain_status === 'other_chains_observed' ? `Observed on ${t.threshold_hit_chains.join(', ')}. Named ${t.predicted_chain} remains unscored.` : null]);
      const tokens = tape(data)?.flatMap(group => (group.examples ?? []).map(token => ({...token, stem: group.stem})));
      return block(paperStudy(data), lookout(data), el('p', Number.isFinite(data.memes?.fdv_floor) ? `Hit threshold: ${dollars(data.memes.fdv_floor)} fully diluted value` : null),
        section('Hit rate by conviction', rows(['Conviction', 'Hit rate', 'Hits', 'Misses', 'Window open', 'Unscored'], rates(data),
          ([c, r]) => [c, rate(r), r.hit, r.miss, r.pending, r.unknown])),
        data.memes?.chain_evidence ? section('Named chain evidence',
          el('p', `${data.memes.chain_evidence.confirmed} confirmed of ${data.memes.chain_evidence.named_calls} named calls · ${data.memes.chain_evidence.other_chains_observed} with a threshold hit observed only on other chains`)) : null,
        section('Themes', ranked(themes(data))),
        section('Launch tape',
          rows(['Token', 'Chain', 'Created', 'Peak value', '24 h volume', 'Market cap', 'Cap observed', 'Tags', 'Address'], tokens, t => [
            [t.name, t.symbol ? `(${t.symbol})` : null].filter(Boolean).join(' '), t.chain, age(t.pair_created_at),
            dollars(t.peak_fdv ?? t.fdv), dollars(t.peak_volume_h24 ?? t.volume_h24),
            dollars(t.market_cap_usd), age(t.market_cap_observed_at),
            (t.description_tags ?? []).filter(tag => typeof tag === 'string').join(', '), t.address
          ])),
        section('Best calls', ranked(data.memes?.best)), section('Worst calls', ranked(data.memes?.worst)));
    },
  });
})();
