/* A read-only building composed from the newsroom snapshot. */
(function () {
  const make = (tag, text, cls) => {
    const node = document.createElement(tag);
    if (text != null) node.textContent = text;
    if (cls) node.className = cls;
    return node;
  };
  const finite = (value) => typeof value === 'number' && Number.isFinite(value);
  const number = (value) => finite(value) ? value.toLocaleString(undefined,{maximumSignificantDigits:10,useGrouping:false}) : 'unknown';
  const percent = (value, unit = '%') => finite(value) ? (value > 0 ? '+' : '') + value.toFixed(2) + ' ' + unit : 'unknown';
  const time = (value) => finite(value) ? new Date(value * 1000).toLocaleString() : 'time unknown';
  const COLORS = ['#72b9ca','#d7a56c','#9abf9d','#72adb8','#c39dc5','#9eadd8','#d99e9e','#d4b274','#8ebcb6','#b2a9d4','#a5bf84','#79bbae','#dda97c','#91a9c7'];
  function stationSprite(seat, label, index, tag = 'div') {
    const station = make('div', null, 'vo-station');
    station.dataset.station = seat;
    const desk = make('div', null, 'vo-furniture');
    desk.setAttribute('aria-hidden', 'true');
    desk.append(make('div', null, 'vo-screen'), make('div', null, 'vo-keyboard'));
    const agent = make(tag, null, 'vo-agent');
    agent.dataset.seat = seat;
    agent.style.setProperty('--vo-color', COLORS[index % COLORS.length]);
    const person = make('span', null, 'vo-person');
    person.setAttribute('aria-hidden', 'true');
    for (const part of ['shadow', 'legs', 'body', 'head']) person.append(make('i', null, 'vo-' + part));
    agent.append(person, make('span', label, 'vo-agent-name'));
    station.append(desk, agent);
    return {station, agent};
  }

  const SEAT_NAMES = {judge_gpt: 'Judge GPT', head: 'Head (Hermes)'};
  const field = value => typeof value === 'string' && value.trim() ? value.trim().replaceAll('_', ' ') : null;
  const line = (...parts) => parts.filter(part => part != null && part !== '').join(' · ');
  const signed = (n, unit) => finite(n) ? (n >= 0 ? '+' : '') + n.toFixed(2) + unit : null;
  const quote = value => finite(value) ? (100 * value).toFixed(1) + '¢' : null;
  const age = at => {
    if (!finite(at)) return null;
    const seconds = Math.max(0, (Date.now() / 1000 - at) | 0);
    return seconds < 60 ? seconds + 's ago' : seconds < 3600 ? (seconds / 60 | 0) + 'm ago' : (seconds / 3600 | 0) + 'h ago';
  };
  const short = (text, n = 44) => {
    const clean = String(text ?? '').replace(/^Will (the )?/, '').replace(/\?$/, '').replace(/ - [A-Z][\w .]+$/, '').trim();
    return clean.length > n ? clean.slice(0, n - 1).trimEnd() + '…' : clean;
  };
  const figure = (value, label, n) => value == null ? null : {value, label, tone: !finite(n) || n === 0 ? '' : n > 0 ? 'positive' : 'negative'};
  const points = n => finite(n) ? (n > 0 ? '+' : '') + n.toFixed(1) + ' pp' : null;
  const dollars = n => finite(n) ? (n < 0 ? '-$' : '+$') + Math.abs(n).toFixed(2) : null;
  const count = (n, one, many = one + 's') => finite(n) ? n + ' ' + (n === 1 ? one : many) : null;
  const ten = (pointsNet, entry) => finite(pointsNet) && finite(entry) && entry > 0 ? pointsNet / 10 / entry : null;
  const day = at => finite(at) ? new Date(at * 1000).toLocaleDateString(undefined, {month: 'short', day: 'numeric'}) : '?';
  const cents = p => finite(p) ? Math.round(p * 100) + '¢' : '?';
  const finished = d => (d.board ?? []).map(c => ({call: c, usd: ten(['closed', 'swapped'].includes(c.paper_exit?.state) ? c.paper_exit_net_pp : c.resolved_pp, c.entry)})).filter(r => r.usd != null);
  const bookLine = (d, name) => {
    const b = d.live_book?.books?.[name];
    return b ? line(dollars(b.net_usd) ?? points(b.net_pp) ?? 'no price', b.marked + ' of ' + b.open + ' priced') : null;
  };
  // Each board answers one question of the operator. A reader returns lines, or {figure, lines}; it never invents a number.
  const FURNITURE = {
    newsroom: [
      ['News to trades', 'screen', d => ({figure: figure(finite(d.news_hour?.matched) ? d.news_hour.matched + ' of ' + d.news_hour.headlines : null, 'headlines matched, last hour'),
        lines: [line(finite(d.news_hour?.related) ? d.news_hour.related + ' related' : null, finite(d.news_hour?.sent_to_debate) ? d.news_hour.sent_to_debate + ' sent to debate' : null)]})],
      ['Latest headlines', 'paper', d => (d.wire ?? []).slice(0, 4).map(row => line(short(row.title, 52), age(row.received_at)))],
    ],
    research: [
      ['Last finding', 'paper', d => (d.briefs ?? []).slice(0, 1).flatMap(r => [short(r.question, 60), field(r.possibly_mispriced)])],
      ['Seat requests', 'gold', d => ({figure: figure(finite(d.ideas?.proposal_counts?.open) ? String(d.ideas.proposal_counts.open) : null, 'open'),
        lines: [finite(d.ideas?.proposal_counts?.built) ? d.ideas.proposal_counts.built + ' built' : null]})],
    ],
    debate: [
      ['Debates', 'paper', d => ({figure: figure(finite(d.stats?.argued_1h) ? String(d.stats.argued_1h) : null, 'full debates, last hour'),
        lines: [finite(d.stats?.debates_1h) ? line(d.stats.debates_1h + ' items read', count(d.stats.errors_1h, 'error')) : null]})],
      ['Latest verdicts', 'cabinet', d => (d.cases ?? []).filter(c => c.question).slice(0, 4).map(c => line(short(c.question), field(c.outcome), age(c.at)))],
    ],
    decision: [
      ['Open now', 'paper', d => {
        const held = (d.live_book?.positions ?? []).filter(p => p.book === 'Main book').map(p => ({p, usd: ten(p.net_pp, p.entry)}));
        const priced = held.filter(h => h.usd != null), total = priced.reduce((sum, h) => sum + h.usd, 0);
        const opened = held.map(h => h.p.opened_at).filter(finite);
        return {figure: figure(priced.length ? dollars(total) : null, `if $10 each. ${held.length} bets the Head approved` + (opened.length ? `, ${day(Math.min(...opened))} to ${day(Math.max(...opened))}` : ''), total),
          lines: held.sort((x, y) => (y.usd ?? -Infinity) - (x.usd ?? -Infinity)).map(({p, usd}) =>
            line(dollars(usd) ?? p.status, p.side + ' ' + short(p.question, 26), `${cents(p.entry)} to ${cents(p.bid)}`))};
      }],
      ['Finished', 'gold', d => {
        const done = finished(d), total = done.reduce((sum, r) => sum + r.usd, 0);
        return {figure: figure(done.length ? dollars(total) : null, 'at $10 each', total), lines: [`${done.filter(r => r.usd > 0).length} of ${done.length} made money`]};
      }],
      ['What would make it trade', 'notebook', d => (d.strategy_lab?.seats ?? []).filter(s => s.last_no_trade && s.book === 'Prediction').sort((a, b) => b.last_no_trade.at - a.last_no_trade.at).slice(0, 1)
        .flatMap(s => [String(s.last_no_trade.would_trade_if ?? '').split('. ')[0], age(s.last_no_trade.at)])],
    ],
    trading: [
      ['Perps', 'screen', d => tradingBook(d.crypto)],
      ['Stocks', 'screen', d => tradingBook(d.stocks)],
      ['Last trade', 'notebook', d => (d.trade_autopsies ?? []).slice(0, 1).flatMap(t => [line(t.symbol, signed(t.net_pct, '%'), age(t.exit_at)), t.autopsy?.verdict])],
    ],
    'paper-labs': [
      ['News calls', 'screen', d => {
        const lab = d.news_call_lab, rows = Object.entries(lab?.by_seat ?? {});
        const result = rows.map(([seat, tables]) => ['24h', '6h', '1h', '15m'].map(h => [h, tables?.[h]]).find(([, t]) => t?.scored > 0)).map((hit, i) => hit
          ? line(field(rows[i][0]), hit[0], dollars(hit[1].net_usd), count(hit[1].scored, 'trade')) : line(field(rows[i][0]), 'no result yet'));
        return {figure: figure(finite(lab?.calls) ? String(lab.calls) : null, 'forced paper calls'),
          lines: [bookLine(d, 'News calls') ? 'Live ' + bookLine(d, 'News calls') : null].concat(result,(lab?.latest ?? []).filter((c, i, all) => all.findIndex(o => o.question === c.question) === i).slice(0, 3).map(c => line(c.side + ' ' + short(c.question, 34), quote(c.ask))))};
      }],
      ['Algorithms', 'gold', d => (d.algorithm_lab?.algorithms ?? []).slice(-3).reverse().flatMap(a => [a.title,
        a.replay ? (a.replay.closed ? line(count(a.replay.closed, 'closed trade'), dollars(a.replay.net_usd)) : count(a.replay.fills, 'fill') + ', untested') : null])],
      ['$10 lane', 'paper', d => ({figure: figure(finite(d.value_lane?.entered) ? String(d.value_lane.entered) : null, 'entered'),
        lines: [finite(d.value_lane?.open) ? d.value_lane.open + ' open' : null, ...(d.musk_lab?.arms ? Object.entries(d.musk_lab.arms).map(([arm, r]) => line(arm, count(r.entries, 'entry', 'entries'), dollars(r.settled_net_usd))) : [])]})],
    ],
    explorer: [['Newest idea', 'notebook', d => (d.ideas?.briefs ?? []).slice(0, 1).flatMap(n => [n.topic, line(finite(n.market?.volume_24h) ? '$' + Math.round(n.market.volume_24h).toLocaleString() + ' traded in 24 h' : null, age(n.at))])]],
    memes: [
      ['Stories to watch', 'screen', d => (d.memes?.passes ?? []).flatMap(pass => pass.narratives ?? []).filter(n => n.status === 'pending').slice(0, 3).map(n => line(n.theme, age(d.memes.scored_at)))],
      ['Hit rate', 'paper', d => {
        const all = Object.values(d.memes?.by_conviction ?? {}).reduce((sum, r) => ({hit: sum.hit + r.hit, miss: sum.miss + r.miss, unknown: sum.unknown + r.unknown, pending: sum.pending + r.pending}), {hit: 0, miss: 0, unknown: 0, pending: 0});
        return {figure: figure(`${all.hit}`, 'hits'), lines: [line(all.miss + ' missed', all.unknown + ' unknown'), all.pending + ' pending']};
      }],
    ],
    strategies: [
      ['Auto-fire (enters by itself)', 'screen', d => {
        const b = d.live_book?.books?.['Auto-fire'];
        return {figure: figure(b ? dollars(b.net_usd) ?? String(b.open ?? 0) : null, b ? 'live, ' + b.open + ' open' : 'no trade yet'), lines: b ? [] : ['Looking every minute for crypto price and post-count bets']};
      }],
      ['Old bets by hand, by idea', 'gold', d => (d.strategies?.strategies ?? []).filter(s => s.settled_n > 0 && s.unit === 'points')
        .sort((a, b) => b.settled_sum - a.settled_sum).map(s => line(short(s.name, 26), `${s.wins} of ${s.settled_n} won`, 'last ' + day(Math.max(...(s.records ?? []).map(r => r.at).filter(finite))))) ],
    ],
    sports: [['Sports', 'screen', d => ({figure: figure(finite(d.sports_clv?.bets_logged) ? String(d.sports_clv.bets_logged) : null, 'paper bets'),
      lines: [finite(d.sports_ledger?.games_tracked) ? d.sports_ledger.games_tracked + ' NBA games tracked' : null, finite(d.sports_clv?.margin_pp) ? `Bets only when the price beats the bookmaker by ${d.sports_clv.margin_pp} pp` : null,
        age(d.sports_clv?.report_at) ? 'Checked ' + age(d.sports_clv.report_at) : null]})]],
    lab: [['Video rules', 'cabinet', d => (d.lab?.ledger ?? []).slice(0, 2).map(r => line(field(r.event), r.video_id))]],
    coach: [['Coach wants fixed', 'notebook', d => (d.coach?.[0]?.priorities ?? []).slice(0, 2)],
      ['Skills changed', 'gold', d => (d.skill_history ?? []).slice(0, 2).map(s => line(s.name?.replaceAll('-', ' '), s.by, age(s.at)))]],
    control: [['Problems', 'screen', d => {
      const bad = (d.health?.jobs ?? []).filter(j => j.state !== 'ok'), seats = Object.values(d.health?.seats ?? {}).filter(s => s.state !== 'ok');
      return {figure: figure(d.health?.jobs ? String(bad.length + seats.length) : null, 'down or late', bad.length + seats.length ? -1 : 0),
        lines: [...bad.map(j => line(j.label, j.state)), ...seats.map(s => line(field(s.seat), field(s.state) ?? s.last_error?.text)), line(field(d.desk?.state), age(d.desk?.heartbeat_at))]};
    }], ['Next events', 'paper', d => (d.calendar ?? []).slice(0, 3).map(event => line(event.date?.slice(5), short(event.what, 60)))]],
  };

  function tradingBook(book) {
    if (!book) return [];
    const open = (book.calls ?? []).filter(c => !c.outcome), sum = book.summary ?? {};
    return {figure: figure(signed(sum.sum_net_pct, '%'), 'total on margin', sum.sum_net_pct),
      lines: [...open.map(c => line(c.symbol, c.side, signed(c.mark_pct, '%'))), finite(sum.scored) ? `${sum.wins} of ${sum.scored} won` : null,
        open.length ? null : 'Flat now', ...(book.pending ?? []).slice(0, 2).map(t => line('Queued', t.symbol, t.side, t.trigger_price))]};
  }

  function checked(book, reading) {
    const opened = reading?.last_reply?.opened;
    if (!Array.isArray(opened)) return 'Checked ' + book;
    return opened.length ? 'Opened ' + opened.join(', ') : 'Checked ' + book + ', no new trade';
  }
  const activeSeat = (reading, at = Date.now() / 1000) => finite(reading?.last_at) && at - reading.last_at >= 0 && at - reading.last_at <= 300;
  window.OfficeFloor = {
    furniture: FURNITURE,
    activeSeat,
    seatName: seat => SEAT_NAMES[seat] || (seat.replace(/^pm_/, 'PM_').replaceAll('_', ' ').replace(/^./, c => c.toUpperCase())),
    boards(id, data) {
      const wall = make('div', null, 'vo-board-wall');
      const defined = FURNITURE[id];     // a room's boards are a list, or a function of the page's data (this office's books vary)
      const definitions = typeof defined === 'function' ? defined(data) : defined ?? [];
      for (const [name, material, read] of definitions) {
        const panel = make('section', null, 'od-floor-board vo-board-' + material);
        panel.dataset.furniture = name;
        panel.append(make('h3', name));
        const reading = read(data) ?? [];
        const shown = Array.isArray(reading) || !reading.figure ? [] : [reading.figure];
        if (shown.length) {
          const row = make('div', null, 'od-figures');
          for (const figure of shown) {
            const node = make('div', null, 'od-figure ' + (figure.tone ?? ''));
            node.append(make('strong', figure.value), make('span', figure.label));
            row.append(node);
          }
          panel.append(row);
        }
        const scroll = make('div', null, 'vo-board-lines');
        for (const value of (Array.isArray(reading) ? reading : reading.lines).filter(Boolean)) scroll.append(make('p', window.OfficeNames ? window.OfficeNames.scrub(value) : value));
        if (!scroll.children.length && !shown.length) scroll.append(make('p', 'Nothing recorded yet'));
        if (scroll.children.length) panel.append(scroll);
        wall.append(panel);
      }
      return wall;
    },
    fitBoards(floor) {
      for (const bubble of floor.querySelectorAll('.vo-seat-bubble')) {
        bubble.style.margin = '0px';
        const box = bubble.getBoundingClientRect();
        const room = bubble.closest('[data-tile]').getBoundingClientRect();
        bubble.style.marginLeft = Math.max(room.left + 5 - box.left, Math.min(0, room.right - 5 - box.right)) + 'px';
        bubble.style.marginBottom = -Math.max(0, room.top + 5 - box.top) + 'px';
      }
      for (const badge of floor.querySelectorAll('.vo-room-more')) badge.remove();
      const hiddenByRoom = new Map();
      for (const box of floor.querySelectorAll('.vo-board-lines')) {
        const lines = [...box.querySelectorAll('p')];
        for (const line of lines) {
          line.dataset.fullText ??= line.textContent;
          line.textContent = line.dataset.fullText;
          line.hidden = false;
        }
        const fits = () => box.scrollHeight <= box.clientHeight + 1 && box.scrollWidth <= box.clientWidth + 1;
        let count = 0;
        for (const line of [...lines].reverse()) {
          if (fits()) break;
          line.hidden = true;
          count++;
        }
        if (!count) continue;
        const next = lines.find(line => line.hidden);
        if (next && count === lines.length) {
          const words = next.dataset.fullText.split(/\s+/);
          next.hidden = false;
          let low = 1, high = words.length - 1, fit = 0;
          while (low <= high) {
            const mid = Math.floor((low + high) / 2);
            next.textContent = words.slice(0, mid).join(' ') + '…';
            if (fits()) {
              fit = mid;
              low = mid + 1;
            } else high = mid - 1;
          }
          next.hidden = fit === 0;
          if (fit) next.textContent = words.slice(0, fit).join(' ') + '…';
        }
        const room = box.closest('[data-tile]');
        hiddenByRoom.set(room, (hiddenByRoom.get(room) ?? 0) + count);
      }
      for (const [room, count] of hiddenByRoom) {
        const badge = make('span', '+' + count + ' more', 'vo-room-more');
        const opener = room.querySelector('.od-room-open');
        opener.setAttribute('aria-label', opener.firstChild.textContent);
        opener.append(badge);
      }
    },
    station(seat, reading, index, probability, live, health) {
      const name = this.seatName(seat);
      const {station, agent} = stationSprite(seat, name, index);
      const recent = activeSeat(reading);
      const state = reading?.status === 'failed' ? 'Failed'
        : live && reading?.status === 'working' ? 'Working' : recent ? 'Active' : 'Idle';
      agent.dataset.active = String(recent || state === 'Working');
      const healthState = field(health?.state);
      if (healthState) station.dataset.health = health.state;
      const elapsed = state === 'Working' ? null : age(reading?.last_at);
      const elapsedLabel = elapsed?.replace(/(\d+)m ago$/, '$1 min ago').replace(/(\d+)h ago$/, '$1 h ago').replace(/(\d+)s ago$/, '$1 s ago');
      const status = make('span', line(state, elapsed?.replace(' ago', '')), 'vo-agent-status');
      agent.append(status);
      const fullStatus = line(state, elapsedLabel, finite(probability) ? Math.round(100 * probability) + '% YES' : null);
      status.setAttribute('aria-label', fullStatus);
      const raw = reading?.last_action || '';
      const action = raw === 'head abstain' ? 'Abstained'
        : raw === 'free hour' ? 'Explored'
        : raw === 'desk review' ? 'Reviewed the desk'
        : raw === 'perp snapshot' ? checked('perps', reading)
        : raw === 'stock screener' ? checked('stocks', reading)
        : raw.replaceAll('_', ' ');
      const subject = ['perp snapshot', 'stock screener'].includes(raw) ? null : reading?.last_item;
      const sentence = [action, subject && subject !== raw ? subject : ''].filter(Boolean).join(' · ');
      if (healthState && !['healthy', 'ok', 'unknown'].includes(healthState)) {
        agent.append(make('span', healthState, 'vo-agent-health'));
      }
      station.title = [name, fullStatus, reading?.model, sentence, healthState, health?.last_error?.text].filter(Boolean).join(' · ');
      if (live && state !== 'Failed' && (recent || state === 'Working')) {
        station.classList.add('vo-seat-recent');
        // Operator 2026-11-20: bubbles were whole paragraphs held for five minutes and hid the boards. One short line, only while the
        // action is under 90 seconds old or still running; the full sentence stays on the seat's hover title.
        const fresh = state === 'Working' || (finite(reading?.last_at) && Date.now() / 1000 - reading.last_at < 90);
        const words = action.length > 22 ? action.slice(0, 21).trimEnd() + '…' : action;
        if (words && fresh) station.append(make('span', words, 'vo-seat-bubble'));
      }
      return station;
    },
    replay(floor, events) {
      const first = floor.firstElementChild;
      events.forEach((event, index) => setTimeout(() => {
        if (floor.firstElementChild === first && !floor.hidden && !document.hidden) this.replayEvent(floor, event);
      }, index * 2300));
    },
    replayEvent(floor, event) {
      if (matchMedia('(prefers-reduced-motion: reduce)').matches) return;
      if (window.OfficeLive?.replay(floor, event) === 'done') return;      // story cards, mail between desks
      const room = id => floor.querySelector(`[data-tile="${id}"]`);
      const target = room(event.room);
      if (target) {
        const bubble = make('span', event.title, 'vo-replay-bubble');
        target.append(bubble);
        setTimeout(() => bubble.remove(), 1800);
      }
      const board = event.book === 'stocks' ? 'Stock terminal' : event.book === 'crypto' ? 'Perps terminal' : null;
      const light = board ? target?.querySelector(`[data-furniture="${board}"]`)
        : event.kind === 'skill_changed' ? target?.querySelector('.od-floor-board') : null;
      if (light) {
        light.classList.add('vo-recorded-light');
        setTimeout(() => light.classList.remove('vo-recorded-light'), 1500);
      }
      if (event.kind !== 'debate_outcome') return;
      const origin = floor.getBoundingClientRect();
      const route = ['newsroom', 'debate', 'decision'].map(id => room(id)?.getBoundingClientRect());
      if (route.some(box => !box)) return;
      const file = make('span', '▤', 'vo-recorded-file');
      file.setAttribute('aria-hidden', 'true');
      floor.append(file);
      const travel = file.animate(route.map(box => ({
        transform: `translate(${box.left - origin.left + box.width / 2}px, ${box.top - origin.top + box.height / 2}px)`,
      })), {duration: 1900, easing: 'ease-in-out', fill: 'forwards'});
      travel.onfinish = () => file.remove();
    },
  };
})();
