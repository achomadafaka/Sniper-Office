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
  const percent = n => Number.isFinite(n) ? `${n >= 0 ? '+' : ''}${n.toFixed(2)}%` : unknown;
  const points = n => Number.isFinite(n) ? `${n >= 0 ? '+' : ''}${n.toFixed(1)} points` : unknown;
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
  const feed = el('aside', null, 'od-feed');
  const body = el('div', null, 'od-body');
  const detail = el('section', null, 'od-detail');
  body.append(grid, detail, feed);
  root.append(header, body);
  detail.hidden = true;
  grid.setAttribute('aria-label', 'Office rooms');
  let data = {}, failed = false, active = null, opener = null, busy = false, roomScroll = 0;
  let seen = null, incoming = new Set(), changedRooms = new Set();
  const eventKey = event => JSON.stringify([event.kind, event.room, event.item_id, event.at]);
  // The rooms this office has. Their seats come from the feed (data.rooms), so the seat-to-room mapping lives in one place, on the server.
  const placeholders = [['decision', 'Decision', 10], ['debate', 'Debate', 20], ['newsroom', 'Newsroom', 40], ['research', 'Research', 50], ['trading', 'Trading', 30], ['situation', 'Situation', 35]];
  for (const [id, title, order] of placeholders) {
    rooms.set(id, {id, title, order, seats: [], summary: () => el('p', unknown), detail: () => el('p', unknown)});
  }
  window.OfficeRooms = {
    register(room) {
      if (!/^[a-z][a-z0-9-]*$/.test(room.id) || !Number.isFinite(room.order)
          || typeof room.summary !== 'function' || typeof room.detail !== 'function') {
        throw new TypeError('Invalid Office room');
      }
      rooms.set(room.id, room);
      render();
    },
  };
  function metric(label, text) {
    const node = el('div', null, 'od-metric');
    node.append(el('span', label), el('strong', text));
    return node;
  }
  const sum = (rows, key) => rows.length && rows.every(row => Number.isFinite(row[key])) ? rows.reduce((total, row) => total + row[key], 0) : null;
  const compact = n => Number.isFinite(n) ? new Intl.NumberFormat('en', {notation: 'compact', maximumFractionDigits: 1}).format(n) : unknown;
  function renderHeader() {
    const now = new Date();
    const desk = data.office ?? {};
    const stats = data.stats ?? {};
    const rows = Object.values(data.seats ?? {});
    const identity = el('div', null, 'od-identity');
    identity.append(el('h1', 'Office'), el('span', 'Seat floor'));
    const status = metric('Office', failed ? 'unreadable' : `${value(desk.state)} · ${age(desk.heartbeat_at)}`);
    if (!failed && desk.state === 'running') status.classList.add('od-running');
    status.append(el('span', `${value(desk.epoch)}${failed ? ' · Stale reading' : ''}`));
    const count = state => data.seats ? rows.filter(seat => seat.status === state).length : null;
    const book = data.book?.books;
    const strip = el('div', null, 'od-strip');
    strip.append(status,
      metric('Seats awake / sleeping / parked', data.seats ? `${count('working')} / ${count('sleeping')} / ${count('parked')}` : unknown),
      metric('Seats active in last 5 min', `${data.seats ? rows.filter(seat => window.OfficeFloor.activeSeat(seat, now / 1000)).length : unknown} / ${data.seats ? rows.length : unknown}`),
      metric('Wakes today (UTC)', value(sum(rows, 'wakes_today'))),
      metric('Tokens today', compact(sum(rows, 'tokens_today'))),
      metric('Unread mail', value(sum(rows, 'unread_mail'))),
      metric('Requests, latest per seat', value(stats.requests)),
      metric('Tool calls, last hour', value(stats.tool_calls_1h)),
      metric('Paper book', Array.isArray(book) && book.length ? `${book.reduce((n, b) => n + b.open, 0)} open / ${book.reduce((n, b) => n + b.closed, 0)} closed` : unknown));
    const time = el('div', `${now.toISOString().slice(11, 19)} UTC / ${now.toLocaleTimeString()} local`, 'od-clock');
    const title = el('div', null, 'od-title');
    title.append(identity, time);
    header.replaceChildren(title, strip);
  }
  function seatChips(room) {
    const list = el('div', null, 'od-seats');
    for (const name of room.seats ?? []) {
      const seat = data.seats?.[name] ?? {};
      const recent = Number.isFinite(seat.last_at) && Date.now() / 1000 - seat.last_at < 60;
      const chip = el('div', null, recent ? 'od-seat od-seat-recent' : 'od-seat');
      const status = seat.status === 'failed' ? 'failed' : seat.status === 'working' || recent ? 'working' : seat.status ?? unknown;
      const label = window.OfficeFloor?.seatName(name) ?? name.replaceAll('_', ' ');
      const action = seat.last_action === 'head abstain'
        ? ['abstained', seat.last_item && `on ${seat.last_item}`].filter(Boolean).join(' ')
        : (seat.last_action ?? seat.last_item)?.replaceAll('_', ' ');
      const dot = el('span', null, `od-seat-dot od-seat-${status}`);
      dot.setAttribute('aria-label', status === unknown ? 'No recorded state' : status);
      chip.append(dot, el('strong', label));
      if (seat.model) chip.append(el('span', seat.model));
      if (action) chip.append(el('span', action));
      if (Number.isFinite(seat.last_at)) chip.append(clock(seat.last_at));
      chip.title = [label, seat.model, status === unknown ? null : status, action,
        Number.isFinite(seat.last_at) ? age(seat.last_at) : null].filter(Boolean).join(' · ');
      list.append(chip);
    }
    return list;
  }
  function roomContent(room, mode) {
    try {
      return room[mode](data);
    } catch (error) {
      console.error('Office room failed', room.id, error);
      return el('p', unknown);
    }
  }
  function renderGrid() {
    const focused = document.activeElement?.dataset.room;
    const live = !failed && data.office?.state === 'running';
    grid.replaceChildren(...[...rooms.values()].sort((a, b) => a.order - b.order).map(room => {
      const tile = el('article', null, 'od-tile od-floor-room');
      tile.dataset.tile = room.id;
      if (changedRooms.has(room.id)) tile.classList.add('od-new-event');
      const button = el('button', room.title, 'od-room-open');
      button.type = 'button';
      button.dataset.room = room.id;
      button.addEventListener('click', () => open(room.id, button));
      let figures = [];
      try {
        figures = (room.figures?.(data) ?? []).slice(0, 2)
          .filter(figure => figure.value != null && String(figure.value).toLowerCase() !== unknown);
      } catch (error) {
        console.error('Office figures failed', room.id, error);
      }
      const boards = window.OfficeFloor.boards(room.id, data);
      const stations = el('div', null, 'od-floor-stations');
      for (const [index, seat] of (room.seats ?? []).entries()) {
        if (window.OfficeFloor) {
          const station = window.OfficeFloor.station(seat, data.seats?.[seat], index,
            null, live, null);
          stations.append(station);
        }
      }
      if (room.id === 'situation') {
        const map = roomContent(room, 'summary');
        const board = el('div', null, 'od-floor-board vo-situation-wall');
        board.append(map);
        tile.append(button, board);
      } else tile.append(button, boards, stations);
      tile.addEventListener('click', event => {
        if (!event.target.closest('a, button, input, select, textarea, summary')) open(room.id, button);
      });
      return tile;
    }));
    window.OfficeFloor.fitBoards(grid);
    if (live && incoming.size) {
      const replay = data.activity.filter(event => incoming.has(eventKey(event))).slice(0, 5).reverse();
      window.OfficeFloor.replay(grid, replay);
    }
    if (focused) grid.querySelector(`[data-room="${CSS.escape(focused)}"]`)?.focus({preventScroll: true});
  }
  const feedLabels = {mail: 'Mail', request: 'Request', wake: 'Wake', tool: 'Tool'};
  function renderFeed() {
    const scroll = feed.scrollTop;
    feed.replaceChildren(el('h2', 'Live feed'));
    const events = Array.isArray(data.activity) ? [...data.activity].sort((a, b) => b.at - a.at) : [];
    if (!events.length) feed.append(el('p', Array.isArray(data.activity) ? 'No recorded events' : unknown));
    for (const event of events) {
      const label = feedLabels[event.kind] ?? value(event.type ?? event.kind).replaceAll('_', ' ');
      const item = el('button', null, 'od-feed-item');
      item.type = 'button';
      if (incoming.has(eventKey(event))) item.classList.add('od-new-feed');
      item.disabled = !rooms.has(event.room);
      const meta = el('span', null, 'od-feed-meta');
      meta.append(el('strong', label, `od-feed-type type-${label.toLowerCase()}`), clock(event.at));
      item.append(meta, el('span', event.seat ? `${window.OfficeFloor.seatName(event.seat)}: ${value(event.title)}` : value(event.title)));
      item.addEventListener('click', () => open(event.room, item, event.item_id));
      feed.append(item);
    }
    feed.scrollTop = scroll;
  }
  const chosenTab = new Map(), expandedTables = new Set();
  function scene(room) {
    const box = el('div', null, 'od-scene');
    box.setAttribute('role', 'region');
    box.setAttribute('aria-label', room.title + ' room');
    if (room.id === 'situation') {
      const board = el('div', null, 'od-floor-board vo-situation-wall');
      board.append(roomContent(room, 'summary'));
      box.append(board);
    } else if (window.OfficeFloor) box.append(window.OfficeFloor.boards(room.id, data));
    const people = el('div', null, 'od-scene-people');
    for (const [index, seat] of (room.seats ?? []).entries()) {
      if (!window.OfficeFloor) break;
      const reading = data.seats?.[seat], person = el('div', null, 'od-scene-person');
      const live = !failed && data.office?.state === 'running';
      person.append(window.OfficeFloor.station(seat, reading, index, null, live, null));
      const said = el('p', null, 'od-scene-said');
      said.append(el('strong', window.OfficeFloor.seatName(seat)), document.createTextNode(' ' + (reading?.last_action ? String(reading.last_action).replaceAll('_', ' ') : 'Nothing recorded yet')));
      person.append(said);
      people.append(person);
    }
    if (people.children.length) box.append(people);
    return box;
  }
  function limitTables(panel, key) {
    for (const [index, table] of [...panel.querySelectorAll('table')].entries()) {
      const count = table.querySelectorAll('tr').length - 1, id = key + '|' + index;
      const wrap = el('div', null, 'od-table-wrap');
      wrap.tabIndex = 0;
      wrap.setAttribute('role', 'region');
      wrap.setAttribute('aria-label', 'Table, scrolls sideways');
      table.replaceWith(wrap);
      wrap.append(table);
      if (count <= 12 || expandedTables.has(id)) continue;
      table.classList.add('od-short-table');
      const more = el('button', 'Show all ' + count, 'od-show-all');
      more.type = 'button';
      more.addEventListener('click', () => { expandedTables.add(id); table.classList.remove('od-short-table'); more.remove(); });
      wrap.after(more);
    }
  }
  function renderDetail() {
    const room = rooms.get(active);
    const view = detail.querySelector('.od-detail-scroll');
    const content = roomContent(room, 'detail');
    const sections = [...content.children].filter(node => node.tagName === 'SECTION' && node.querySelector(':scope > h3'));
    const body = el('div', null, 'od-room-body');
    view.replaceChildren(scene(room), body);
    const rest = [...content.children].filter(node => !sections.includes(node) && node.textContent.trim());
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
    if (!rooms.has(id)) return;
    if (!active) roomScroll = window.scrollY;
    opener = trigger;
    if (syncHash) {
      const params = new URLSearchParams({room: id});
      if (itemId != null) params.set('item', itemId);
      history.pushState(null, '', '#' + params);
    }
    active = id;
    const back = el('button', 'Back to rooms');
    back.type = 'button';
    back.addEventListener('click', () => close());
    const bar = el('div', null, 'od-detail-bar');
    bar.append(back, el('h2', rooms.get(id).title));
    chosenTab.delete(id);
    detail.replaceChildren(bar, el('div', null, 'od-detail-scroll'));
    grid.hidden = true;
    detail.hidden = false;
    renderDetail();
    window.scrollTo(0, 0);
    back.focus({preventScroll: true});
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
    grid.hidden = false;
    renderGrid();
    renderFeed();
    (opener?.isConnected ? opener : grid.querySelector(`[data-room="${CSS.escape(id)}"]`))?.focus({preventScroll: true});
    window.scrollTo(0, roomScroll);
    opener = null;
  }
  function followHash() {
    const params = new URLSearchParams(location.hash.slice(1));
    const room = params.get('room');
    if (rooms.has(room)) open(room, null, params.get('item'), false);
    else close(false);
  }
  function render() {
    renderHeader();
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
      renderGrid();
      renderFeed();
    }
  }
  async function refresh() {
    if (busy) return;
    busy = true;
    try {
      const response = await fetch('/api/office/newsroom', {cache: 'no-store'});
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      data = await response.json();
      for (const [id, seats] of Object.entries(data.rooms ?? {})) if (rooms.has(id)) rooms.get(id).seats = seats;
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
      incoming.clear();
      changedRooms.clear();
    }
  }
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && active) close();
  });
  window.addEventListener('hashchange', followHash);
  new ResizeObserver(() => window.OfficeFloor.fitBoards(grid)).observe(grid);
  document.fonts.ready.then(() => window.OfficeFloor.fitBoards(grid));
  render();
  followHash();
  refresh();
  setInterval(refresh, 15000);
})();
