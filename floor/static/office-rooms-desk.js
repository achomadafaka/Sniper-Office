(() => {
  'use strict';

  const {register} = window.OfficeRooms;
  const {missing, readable} = window.OfficeDetail;
  const unknown = 'Unknown';
  const show = value => value === null || value === undefined || value === '' ? unknown : String(value);
  const human = value => show(value).replaceAll('_', ' ');
  const fmt = (value, suffix = '', digits = 1) => Number.isFinite(value) ? value.toFixed(digits) + suffix : unknown;
  const cents = value => Number.isFinite(value) ? fmt(value * 100, '¢') : unknown;
  const percent = value => Number.isFinite(value) ? fmt(value * 100, '%', 0) : unknown;
  const figure = (label, value) => ({
    label, value: show(value),
    tone: value == null || value === unknown ? 'unknown' : undefined
  });
  const points = value => Number.isFinite(value) ? (value > 0 ? '+' : '') + fmt(value, ' points') : unknown;
  const list = value => Array.isArray(value) ? value : [];
  const prose = value => Array.isArray(value) ? value.join(' ') : value;
  const text = (tag, value) => {
    const node = document.createElement(tag);
    if (value !== undefined) readable(node, value);
    return node;
  };
  const block = (...children) => {
    const node = text('div');
    node.append(...children.filter(Boolean));
    return node;
  };
  const line = (...values) => text('p', values.filter(value => !missing(value)).join(' · '));
  function time(at) {
    if (!Number.isFinite(at) || at <= 0) return text('span', unknown);
    const date = new Date(at * 1000);
    const seconds = Math.max(0, (Date.now() - date.getTime()) / 1000);
    const [scale, unit] = seconds >= 86400 ? [86400, 'd'] : seconds >= 3600 ? [3600, 'h']
      : seconds >= 60 ? [60, 'm'] : [1, 's'];
    const node = text('time', Math.floor(seconds / scale) + ' ' + unit + ' ago');
    node.dateTime = date.toISOString();
    node.title = date.toUTCString() + ' / ' + date.toLocaleString();
    return node;
  }
  const utc = at => Number.isFinite(at) && at > 0 ? new Date(at * 1000)
    .toISOString().slice(0, 16).replace('T', ' ') + ' UTC' : unknown;
  function withTime(value, at, extra) {
    const node = text('p');
    if (!missing(value)) node.append(text('span', value), ' · ');
    if (Number.isFinite(at)) node.append(time(at));
    if (extra) node.append(' · ', text('span', extra));
    return node;
  }
  function source(value) {
    try {
      const url = new URL(value);
      if (url.protocol === 'https:' || url.protocol === 'http:') {
        const link = text('a', url.hostname);
        link.href = url.href;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        link.style.color = "inherit";
        return link;
      }
    } catch { /* a recorded non-URL source stays plain text */ }
    return text('span', value ?? unknown);
  }
  function section(title, ...children) {
    const node = text('section');
    const visible = children.filter(child => child?.textContent.trim());
    if (visible.length) node.append(text('h3', title), ...visible);
    return node;
  }
  const tableSort = new Map();
  const collator = new Intl.Collator(undefined, {numeric: true, sensitivity: 'base'});
  function sortValue(cell) {
    const time = cell.querySelector('time');
    if (time) return Date.parse(time.dateTime);
    const value = cell.textContent.trim();
    if (missing(value)) return null;
    const number = value.match(/^([+-]?\d+(?:\.\d+)?)(?:%|¢| points)?$/);
    return number ? Number(number[1]) : value;
  }

  function table(title, headers, rows, cells, key = () => null) {
    if (!rows?.length) return text('div');
    const values = rows.map(cells);
    const columns = headers.map((_, index) => index).filter(index => values.some(row =>
      row[index] instanceof Node ? row[index].textContent.trim() : !missing(row[index])));
    if (!columns.length) return text('div');
    const node = text('table');
    const head = text('thead'), body = text('tbody'), names = text('tr');
    function applySort(index, descending) {
      const sorted = [...body.rows].map(row => ({row, value: sortValue(row.cells[index])}));
      sorted.sort((a, b) => {
        if (a.value === null || b.value === null) return Number(a.value === null) - Number(b.value === null);
        const order = typeof a.value === 'number' && typeof b.value === 'number'
          ? a.value - b.value : collator.compare(a.value, b.value);
        return descending ? -order : order;
      });
      for (const [i, column] of [...names.cells].entries()) {
        const indicator = column.querySelector('[aria-hidden]');
        if (i === index) {
          column.setAttribute('aria-sort', descending ? 'descending' : 'ascending');
          indicator.textContent = descending ? ' ↓' : ' ↑';
        } else {
          column.removeAttribute('aria-sort');
          indicator.textContent = '';
        }
      }
      body.append(...sorted.map(entry => entry.row));
    }
    for (const [index, original] of columns.entries()) {
      const label = headers[original];
      const column = text('th'), button = text('button', label), indicator = text('span');
      column.scope = 'col';
      button.type = 'button';
      indicator.setAttribute('aria-hidden', 'true');
      button.append(indicator);
      button.addEventListener('click', () => {
        const descending = column.getAttribute('aria-sort') === 'ascending';
        tableSort.set(title, [original, descending]);
        applySort(index, descending);
      });
      column.append(button);
      names.append(column);
    }
    head.append(names);
    for (const [i, row] of rows.entries()) {
      const tr = text('tr'), id = key(row);
      if (id !== null && id !== undefined) tr.dataset.itemId = String(id);
      for (const index of columns) {
        const value = values[i][index];
        const cell = text('td');
        cell.dataset.label = headers[index];
        if (value instanceof Node && value.textContent.trim()) cell.append(value);
        else if (!missing(value) && !(value instanceof Node)) readable(cell, value);
        else {
          cell.textContent = '—';
          cell.title = 'No value was recorded for this row';
        }
        tr.append(cell);
      }
      body.append(tr);
    }
    const savedSort = tableSort.get(title);
    if (savedSort && columns.includes(savedSort[0])) applySort(columns.indexOf(savedSort[0]), savedSort[1]);
    node.append(head, body);
    const scroll = block(node);
    scroll.style.overflowX = 'auto';
    return section(title, scroll);
  }
  const sources = values => values?.length ? block(...list(values).map(value => block(source(value)))) : unknown;
  const literal = value => missing(value) ? null : value;
  function headlineInstruments(entry) {
    if (!entry) return null;
    const node = block();
    for (const instrument of list(entry.instruments)) {
      const label = instrument.symbol && instrument.label && instrument.symbol !== instrument.label
        ? `${instrument.label} (${instrument.symbol})` : instrument.label ?? instrument.symbol;
      node.append(line(label, literal(instrument.kind), literal(instrument.relationship),
        literal(instrument.direction), literal(instrument.reason)));
    }
    return node;
  }
  function headlineAction(entry, seat) {
    const receipt = entry?.[seat];
    if (!receipt?.action) return null;
    return line(receipt.action[0].toUpperCase() + receipt.action.slice(1),
      list(receipt.symbols).join(', '), receipt.why);
  }
  function researchCard(row, fallbackQuestion, fallbackReason) {
    const note = row.note ?? {};
    const article = text('article');
    article.className = 'od-strategy';
    const heading = note.question ?? fallbackQuestion;
    if (!heading) return article;
    article.append(text('h4', heading),
      withTime([note.side, Number.isFinite(note.confidence) ? percent(note.confidence) + ' confidence' : null,
        typeof note.verified_market === 'boolean' ? (note.verified_market ? 'Market verified' : 'Market not verified') : null]
        .filter(Boolean).join(' · '), row.at),
      line(note.reason ?? fallbackReason));
    const links = sources(row.sources);
    if (links instanceof Node) article.append(links);
    return article;
  }
  const events = (d, room) => list(d.activity).filter(event => event.room === room);
  const eventRows = (d, room) => table(room === 'newsroom' ? 'Headline activity' : 'Seat activity', ['When', 'Event', 'Item'], d.activity ? events(d, room) : null,
    row => [time(row.at), human(row.kind), row.title], row => row.item_id);

  Object.assign(window.OfficeDetail, {table, section, block, line, time, text});

  register({
    id: 'newsroom', title: 'Newsroom', order: 30, seats: ['scout'],
    figures(d) {
      const hour = d.news_hour;
      return [figure('Headlines in 1 h', hour?.headlines), figure('Related market found', hour?.related),
        figure('Sent to debate', hour?.sent_to_debate)];
    },
    summary(d) {
      return block(...(d.wire?.length ? d.wire.slice(0, 3).map(row => withTime(row.title, row.received_at)) : [line(unknown)]));
    },
    detail(d) {
      const mapping = d.headline_instruments;
      return block(table('Wire', ['Headline', 'Source', 'Received', 'Disposition', 'Markets affected', 'Trader', 'Perps'], d.wire,
        row => {
          const entry = mapping?.[row.news_id];
          return [row.title, row.source, time(row.received_at), row.scout_disposition ?? human(row.disposition),
            headlineInstruments(entry), headlineAction(entry, 'trader'), headlineAction(entry, 'perps')];
        }, row => row.news_id),
      !d.wire?.length ? line('The wire has no saved headlines to show.') : null,
      !d.wire?.some(row => mapping?.[row.news_id]) ? line('No instrument links have been saved for these headlines.') : null,
      eventRows(d, 'newsroom'));
    }
  });

  register({
    id: 'research', title: 'Research', order: 40, seats: [],
    summary(d) {
      const last = list(d.mk?.explorer?.notes).find(row => row.role === 'researcher');
      return last ? withTime(last.topic, last.at) : null;
    },
    detail(d) {
      const explorer = d.mk?.explorer;
      if (explorer?.state === 'unknown' || !Array.isArray(explorer?.notes)) {
        return block(section('Research briefs', line('Unavailable', explorer?.reason ?? d.mkUnknown?.explorer)));
      }
      const notes = explorer.notes.filter(row => row.role === 'researcher');
      return block(notes.length ? table('Research briefs', ['Topic', 'Saved', 'Why this', 'Notebook'],
        notes, row => [row.topic, time(row.at), row.why_this, row.notebook], row => 'research-note-' + row.id)
        : section('Research briefs', line('No Researcher briefs in the recent Explorer records.')));
    }
  });

  register({
    id: 'explorer', title: 'Explorer', order: 60, seats: ['explorer'],
    summary(d) {
      const last = d.ideas?.briefs?.[0], lead = list(d.ideas?.leads).find(row => row.seat === 'explorer');
      const leadText = lead?.text ? 'Lead: ' + lead.text : null;
      return block(last ? withTime(last.topic, last.at, leadText) : line(leadText));
    },
    detail(d) {
      return block(
      table('Explorer lead outcomes', ['Lead cohort', 'Submitted', 'Routed', 'Approved', 'Scoreable'],
        d.ideas?.explorer_lead_scores, row => [row.cohort, row.submitted, row.routed, row.approved, row.scoreable]),
      section('Free-hour notebooks', ...(d.ideas?.briefs?.length ? d.ideas.briefs.map(row =>
        researchCard(row, row.topic, row.brief ?? row.why_this)) : [line('No free-hour notebook has been saved.')])),
      section('Research leads', ...list(d.ideas?.leads).map(row => {
        const card = researchCard(row, row.text, row.seat ? 'From ' + human(row.seat) : null);
        if (row.id) card.dataset.itemId = String(row.id);
        if (row.market_refusal) card.append(line('Unroutable', row.market_refusal));
        if (row.served_at) card.append(withTime('Taken by Research', row.served_at));
        return card;
      })));
    }
  });

  register({
    id: 'sports', title: 'Sports', order: 80, seats: [],
    figures(d) {
      const s = d.sports_clv, mean = s?.mean_closing_line_pp;
      const clv = figure('Mean CLV (n=' + show(s?.closing_line_n) + ')', points(mean));
      if (Number.isFinite(mean) && mean !== 0) clv.tone = mean > 0 ? 'positive' : 'negative';
      return [figure('Games tracked', d.sports_ledger?.games_tracked),
        Number.isFinite(mean) ? clv : figure('Paper bets', s?.bets_logged)];
    },
    summary(d) {
      const s = d.sports_clv;
      return block(line(s?.started_at ? 'Started ' + utc(s.started_at) : null),
        line(s?.report_due_at ? 'Report due ' + utc(s.report_due_at) : null),
        Number.isFinite(s?.mean_closing_line_pp)
          ? line(Number.isFinite(s.bets_logged) ? s.bets_logged + ' paper bets' : null,
            Number.isFinite(s.positive_closing_line_share) ? percent(s.positive_closing_line_share) + ' positive CLV' : null)
          : null);
    },
    detail(d) {
      const s = d.sports_clv;
      const games = new Map(list(d.sports_ledger?.games).map(game => [game.id, game]));
      const label = event => event?.outcomes ? show(event.outcomes.away?.team) + ' at ' + show(event.outcomes.home?.team) : unknown;
      const fair = (o, side) => percent(o.line?.fair?.[side]);
      const quote = (o, side) => cents(o.quotes?.[side]?.bid) + ' / ' + cents(o.quotes?.[side]?.ask);
      return block(section('48-hour paper study',
        !s ? line('The study report is unavailable.') : line(s.complete ? 'Complete' : 'Collecting'),
        s && !s.closing_line_n ? line('No paper bet has a qualifying closing line yet.') : null,
        s?.closing_line_n ? line(percent(s.positive_closing_line_share) + ' positive (n=' + s.closing_line_n + ')') : null,
        s?.settled_n ? line(points(s.settled_points) + ' settled (n=' + s.settled_n + ')') : null,
        line(s?.started_at ? 'Started ' + utc(s.started_at) : null, s?.report_due_at ? 'Report due ' + utc(s.report_due_at) : null),
        line(s?.closing_line_basis), line(s?.devig),
      ...list(s?.closing_books).map(row => line(row.book, show(row.n) + ' scored',
        fmt(row.min_age_s, ' s', 0) + ' to ' + fmt(row.max_age_s, ' s', 0) + ' before start'))),
      table('Paper bets', ['Game', 'Side', 'Entry', 'Book', 'Result', 'Net'], d.sports_ledger?.games?.filter(g => g.position),
        row => [label(row.event), row.event?.outcomes?.[row.position.side]?.team, cents(row.position.ask), row.position.book,
          row.invalidated ? 'Invalidated' : row.settlement?.winner, points(row.settlement?.settled_pp)], row => row.id),
      table('Observations', ['Game', 'Read', 'Book', 'Away fair', 'Home fair', 'Away bid / ask', 'Home bid / ask'],
        d.sports_ledger?.observations,
        row => [label(games.get(row.game_id)?.event), time(row.at), row.line?.book,
          fair(row, 'away'), fair(row, 'home'), quote(row, 'away'), quote(row, 'home')]));
    }
  });

  register({
    id: 'lab', title: 'Video rule lab', order: 90, seats: [],
    figures(d) {
      return [figure('Rules tested', d.lab?.rules_tested), figure('Holdout passes', d.lab?.rules_passed)];
    },
    summary(d) {
      const l = d.lab;
      return block(line(...[[l?.candidates_queued, 'queued'], [l?.rules_extracted, 'extracted'],
        [l?.rules_outside_dsl, 'outside the rule form']].filter(([value]) => Number.isFinite(value))
        .map(([value, label]) => value + ' ' + label)));
    },
    detail(d) {
      if (!d.lab) return line('No video rule trial ledger is available.');
      if (!d.lab.ledger?.length) return line('No video rules have reached the trial ledger.');
      return table('Trial ledger', ['Candidate', 'Stage', 'When', 'Outcome', 'Basis'], d.lab?.ledger,
        row => [row.candidate_id, human(row.event), time(row.at),
          row.field ?? row.reason ?? (typeof (row.result?.passed ?? row.passed) === 'boolean'
            ? ((row.result?.passed ?? row.passed) ? 'Pass' : 'Fail') : unknown),
          row.observation_basis ?? row.instrument], row => row.candidate_id);
    }
  });

  register({
    id: 'coach', title: 'Coach and skills', order: 100, seats: ['coach'],
    figures(d) {
      return [figure('Skill changes in 24 h', d.skills_changed_24h)];
    },
    summary(d) {
      const review = d.coach?.[0], authors = d.skill_authors_24h;
      const brier = d.seat_brier?.by_seat?.filter(row => row.seat !== 'bull').at(-1);
      const who = ['coach', 'seat', 'alpha', ...(authors?.other ? ['other'] : [])];
      return block(line(...who.filter(author => Number.isFinite(authors?.[author])).map(author => authors[author] + ' ' + author)),
        line(brier ? brier.seat + ' Brier ' + fmt(brier.brier, '', 3) + ' vs ' + fmt(brier.market_mid_brier, '', 3)
          + ' market (n=' + show(brier.forecasts) + ')' : null),
        line(review?.priorities?.[0] ?? review?.findings?.[0] ?? review?.review));
    },
    detail(d) {
      return block(!d.seat_brier?.by_seat?.length
        ? line('No resolved forecasts can yet be paired with a recorded market midpoint.') : null,
      table('What the desk has learned', ['When', 'Strategy', 'Lesson', 'Observed result', 'Evidence or limitation', 'Briefs carried'],
        d.brain_lessons, row => [time(row.at), human(row.strategy), row.name, row.outcome,
          row.failed_step, row.briefs_carried], row => row.id),
      table('Coach reviews', ['When', 'Review', 'Findings', 'Priorities'], d.coach,
        row => [time(row.at), row.review, prose(row.findings), prose(row.priorities)]),
      table('Skill versions', ['Skill', 'Version', 'Changed by', 'When', 'Reason', 'Previous text', 'New text'], d.skill_history,
        row => [row.name, row.version, row.by, time(row.at), row.why, row.replaced_text, row.retired ? 'Retired' : row.text],
        row => row.name + '@' + row.version),
      d.seat_brier?.bull_coverage?.cohorts?.length ? section('Bull calibration by model and prompt',
        line(`Brier shown from ${d.seat_brier.bull_coverage.minimum_pairs} paired forecasts per cohort.`),
        table('Bull forecast coverage', ['Method', 'Prompt', 'Model', 'Forecasts', 'Midpoints', 'Eligible', 'Paired', 'Bull Brier', 'Market Brier'],
          d.seat_brier.bull_coverage.cohorts, row => [row.cohort, row.prompt_version, row.model, row.forecast, row.midpoint,
            row.postmortem_eligible, row.paired, row.brier == null ? 'Sample below minimum' : fmt(row.brier, '', 3),
            row.market_brier == null ? 'Sample below minimum' : fmt(row.market_brier, '', 3)])) : null,
      table('Seat Brier against market midpoint', ['Week', 'Seat', 'Category', 'Model', 'Forecasts', 'Seat Brier', 'Market Brier'],
        d.seat_brier?.by_seat?.filter(row => row.seat !== 'bull').reverse(), row => [row.week, row.seat, row.category, row.model, row.forecasts,
          fmt(row.brier, '', 3), fmt(row.market_mid_brier, '', 3)]));
    }
  });

  const healthState = value => ({ok: 'OK', late: 'Late', failing: 'Failing', down: 'Down'})[value] ?? 'Awaiting evidence';
  const healthError = error => error?.at ? withTime(error.text, error.at) : error?.text;
  function healthInterval(seconds) {
    if (!Number.isFinite(seconds) || seconds <= 0) return null;
    const scale = seconds >= 86400 ? [86400, 'day'] : seconds >= 3600 ? [3600, 'hour'] : [60, 'minute'];
    const amount = seconds / scale[0];
    return 'Every ' + amount + ' ' + scale[1] + (amount === 1 ? '' : 's');
  }

  const joined = values => list(values).join(', ');
  function agentMap(map) {
    if (!map) return null;
    const routing = map.routing;
    const mapState = routing?.on === true ? 'Routing on' : routing?.on === false ? 'Routing off' : 'Routing status unknown';
    const fallback = Object.entries(routing?.fallbacks ?? {}).map(([reason, count]) => human(reason) + ' ' + count);
    const branches = Object.entries(map.branches ?? {}).map(([id, branch]) => {
      const tree = text('details');
      tree.dataset.recordKey = 'agent-map|' + id;
      tree.append(text('summary', human(id)), line(branch.deciding_fact),
        line('Settling fields', joined(branch.fields) || 'Unavailable'),
        line('Fair-value inputs', joined(branch.helps)),
        line('Skills', list(branch.skills).join(' → ')),
        line('When missing', branch.missing?.reader, branch.missing?.seat, branch.missing?.watch));
      for (const [key, label] of [['fair_value', 'Fair value'], ['cost', 'Cost'], ['break_even', 'Break-even']]) {
        if (branch.arithmetic?.[key]) tree.append(line(label, branch.arithmetic[key]));
      }
      if (branch.destinations?.length) tree.append(line('Destinations', joined(branch.destinations)));
      for (const lesson of list(branch.lessons)) {
        const evidence = typeof lesson === 'string' ? lesson : lesson.text ?? lesson.finding;
        if (evidence) tree.append(line('Settled lesson', evidence, lesson.count));
      }
      return tree;
    });
    const panel = section('Agent map',
      line(mapState, Number.isFinite(routing?.guides) ? 'Guides ' + routing.guides : null,
        fallback.length ? 'Fallbacks by reason · ' + fallback.join(', ')
          : routing?.fallbacks ? 'Fallbacks 0' : null),
      table('Sections', ['Section', 'Purpose', 'Agents', 'Inputs', 'Tools mapped', 'Skills', 'Files to', 'Next'],
        map.sections, row => [human(row.id), row.purpose, joined(row.seats), joined(row.gets),
          joined(row.tools), joined(row.skills), joined(row.files_to), joined(row.next)]),
      branches.length ? block(text('h4', 'Branch trees'), ...branches) : null);
    panel.dataset.itemId = 'agent-map';
    return panel;
  }

  register({
    id: 'control', title: 'Control', order: 110, seats: [],
    figures(d) {
      return [figure('Desk state', d.desk?.state && human(d.desk.state)), figure('Errors in 1 h', d.stats?.errors_1h)];
    },
    summary(d) {
      const next = d.calendar?.[0];
      return block(line(next ? 'Next: ' + show(next.what) + ' · ' + show(next.date) : null));
    },
    detail(d) {
      return block(agentMap(d.agent_map), section('Desk health', line(human(d.desk?.state), d.desk?.epoch),
        d.desk?.heartbeat_at ? withTime('Last heartbeat', d.desk.heartbeat_at)
          : line('The desk has not saved a heartbeat.'),
        d.desk?.seat_wall ? line('Seat wall', d.desk.seat_wall.state + (d.desk.seat_wall.web_search ? '' : ', web search off')) : null,
        d.desk?.halt_reason ? line('Halt reason', d.desk.halt_reason) : null),
      d.cleanup ? section('Daily cleanup',
        withTime('Last report', d.cleanup.at),
        d.cleanup.dry_run ? line('Dry run') : line('Deleted',
          d.cleanup.deleted_count + ' items · ' + (d.cleanup.deleted_bytes / 1000000).toFixed(2) + ' MB'),
        d.cleanup.error_count ? line('Errors', d.cleanup.error_count) : null,
        table('Ledger growth', ['Ledger', 'Size (MB)', 'Growth (MB/day)', 'Rate basis', 'Projected 2 GB (UTC)'],
          Object.entries(d.cleanup.ledgers ?? {}),
          ([name, row]) => [name, (row.bytes / 1000000).toFixed(1), (row.bytes_per_day / 1000000).toFixed(1),
            row.rate_basis, row.projected_2gb_utc])) : null,
      table('Timed jobs', ['Job', 'Health', 'Last completed run', 'Last error', 'Consecutive failures', 'Schedule'],
        d.health?.jobs, row => [row.label, healthState(row.state), time(row.last_success_at),
          healthError(row.last_error),
          row.consecutive_failures, row.complete ? 'Study complete' : row.schedule_note ?? healthInterval(row.expected_interval_s)],
        row => 'health:' + row.id),
      table('Seat sessions', ['Seat', 'Activity', 'Health', 'Last completed run', 'Last error', 'Consecutive failures', 'Schedule', 'Session turns', 'Resets today (UTC)', 'Failed handovers'],
        d.seats ? Object.entries(d.seats) : null,
        ([name, seat]) => {
          const health = d.health?.seats?.[name];
          return [name, human(seat.status), healthState(health?.state), time(health?.last_success_at),
            healthError(health?.last_error), health?.consecutive_failures,
            healthInterval(health?.expected_interval_s) ?? 'On demand',
            seat.turns, seat.resets_today, seat.failed_handovers];
        }),
      d.standing_budget ? section('Explorer standing budget',
        line('Turns in the last hour', d.standing_budget.turns_last_hour + ' / ' + d.standing_budget.turns_per_hour),
        line('Tokens today (UTC)', d.standing_budget.tokens_today == null ? 'Usage receipt missing'
          : d.standing_budget.tokens_today.toLocaleString() + ' / ' + d.standing_budget.daily_tokens.toLocaleString()),
        d.standing_budget.blocked ? line('Paused', human(d.standing_budget.blocked)) : null) : null,
      table('Event calendar', ['Date', 'Event', 'Seat', 'Source'], d.calendar,
        row => [row.date, row.what, row.seat, source(row.source)]),
      eventRows(d, 'control'));
    }
  });
})();
