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

  const SEAT_NAMES = {judge_gpt: 'Judge GPT'};
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
  // Each board answers one question of the operator. Rooms add theirs in office-rooms.js; a reader returns lines, or {figure, lines}; it never invents a number.
  const FURNITURE = {};
  const activeSeat = (reading, at = Date.now() / 1000) => finite(reading?.last_at) && at - reading.last_at >= 0 && at - reading.last_at <= 300;
  window.OfficeFloor = {
    activeSeat,
    furniture: FURNITURE,
    seatName: seat => SEAT_NAMES[seat] || seat.charAt(0).toUpperCase() + seat.slice(1),
    boards(id, data) {
      const wall = make('div', null, 'vo-board-wall');
      const definitions = FURNITURE[id] ?? [];
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
        for (const value of (Array.isArray(reading) ? reading : reading.lines).filter(Boolean)) scroll.append(make('p', value));
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
      const state = reading?.status === 'failed' ? 'Failed' : reading?.status === 'parked' ? 'Parked' : reading?.status === 'sleeping' ? 'Sleeping'
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
      const action = (reading?.last_action || '').replaceAll('_', ' ');
      const subject = reading?.last_item;
      const sentence = [action, subject && subject !== action ? subject : ''].filter(Boolean).join(' · ');
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
