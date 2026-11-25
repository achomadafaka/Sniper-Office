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
  function metric(label, text) {
    const node = el('div', null, 'od-metric');
    node.append(el('span', label), el('strong', text));
    return node;
  }
  function scores(row) {
    return `${points(row?.sum_pp)} / ${value(row?.wins)} wins of ${value(row?.scored)}`;
  }
  const compact = n => Number.isFinite(n) ? new Intl.NumberFormat('en', {notation: 'compact', maximumFractionDigits: 1}).format(n) : unknown;
  const money = n => Number.isFinite(n) ? (n < 0 ? '-' : '+') + '$' + Math.abs(n).toFixed(2) : unknown;
  function renderHeader() {
    const now = new Date();
    const desk = data.desk ?? {};
    const floor = data.floor ?? {};
    const rows = Object.values(data.seats ?? {});
    const total = key => rows.length && rows.every(row => Number.isFinite(row[key])) ? rows.reduce((sum, row) => sum + row[key], 0) : null;
    const count = state => data.seats ? rows.filter(seat => seat.office_status === state).length : null;
    const identity = el('div', null, 'od-identity');
    identity.append(el('h1', 'Office'), el('span', 'Paper book and seats'));
    const status = metric('Office heartbeat', `${value(desk.state)} · ${age(desk.heartbeat_at)}`);
    if (!failed && desk.state === 'running' && Number.isFinite(desk.heartbeat_at)
        && now / 1000 - desk.heartbeat_at <= 900) status.classList.add('od-running');
    status.append(el('span', `${value(desk.epoch)}${failed || (Number.isFinite(desk.heartbeat_at) && now / 1000 - desk.heartbeat_at > 900) ? ' · Stale reading' : ''}`));
    const health = data.health?.summary;
    if (health?.text) {
      const summary = el('span', `Office health · ${health.text}`, 'od-health-summary');
      if (health.state) summary.dataset.health = health.state;
      status.append(summary);
    }
    const event = data.calendar?.find(item => !Number.isFinite(item.starts_at) || item.starts_at >= now / 1000) ?? data.calendar?.[0];
    const eventAt = event?.starts_at;
    const remaining = Number.isFinite(eventAt) ? Math.max(0, Math.ceil((eventAt - now / 1000) / 60)) : null;
    const activeSeats = data.seats ? rows.filter(seat => window.OfficeFloor.activeSeat(seat, now / 1000)).length : null;
    const totalSeats = data.seats ? rows.length : null;
    const book = floor.totals;
    const strip = el('div', null, 'od-strip');
    strip.append(status,
      metric('Paper book, closed orders', book ? `${money(book.net_usd)} / ${value(book.wins)} wins of ${value(book.scored)}` : unknown),
      metric('Open orders', value(data.open_call_count)),
      metric('Perps margin return', percent(data.crypto?.summary?.sum_net_pct)),
      metric('Stock margin return', percent(data.stocks?.summary?.sum_net_pct)),
      metric('Last hour', `${value(data.stats?.tool_calls_1h)} tool calls / ${value(data.stats?.wakes_1h)} wakes`),
      metric('Seats awake / sleeping / parked', data.seats ? `${count('working')} / ${count('sleeping')} / ${count('parked')}` : unknown),
      metric('Seats active in last 5 min', `${value(activeSeats)} / ${value(totalSeats)}`),
      metric('Wakes today (UTC)', value(total('wakes_today'))),
      metric('Tokens today', compact(total('tokens_today'))),
      metric('Unread mail', value(total('unread_mail'))),
      metric('Requests, latest per seat', value(data.stats?.requests)),
      metric('Next event', event ? `${value(event.date)} · ${value(event.what)}${remaining == null ? '' : (eventAt < now / 1000 ? ' · overdue' : ` · in ${Math.max(1, remaining)} min`)}` : unknown));
    const time = el('div', `${now.toISOString().slice(11, 19)} UTC / ${now.toLocaleTimeString()} local`, 'od-clock');
    const title = el('div', null, 'od-title');
    title.append(identity, time);
    header.replaceChildren(title, strip);
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

  function operatorChatPanel() {
    if (!operatorChat) return null;
    const panel = el('section');
    panel.dataset.itemId = 'operator-chat';
    panel.append(el('h3', 'Operator chat'));
    if (operatorChat.status === 'unavailable') {
      panel.append(el('p', 'Operator chat unavailable.'));
      return panel;
    }
    const asks = Array.isArray(operatorChat.asks) ? operatorChat.asks : [];
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
      const row = el('article');
      row.append(el('h4', 'Message to ' + message.seats.join(', ')),
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
    const lag = Date.now() / 1000 - data.desk?.heartbeat_at;
    const live = !failed && data.desk?.state === 'running' && lag >= 0 && lag <= 900;
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
        if (seat === 'quant' && room.id === 'debate') continue;
        if (window.OfficeFloor) {
          const station = window.OfficeFloor.station(seat, data.seats?.[seat], index,
            room.id === 'debate' ? data.cases?.[0]?.seats?.[seat]?.probability_yes : null, live, data.health?.seats?.[seat]);
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
      const replay = data.activity.filter(event => incoming.has(eventKey(event))).slice(0, 5).reverse();
      window.OfficeFloor.replay(grid, replay);
    }
    if (focused) grid.querySelector(`[data-room="${CSS.escape(focused)}"]`)?.focus({preventScroll: true});
  }
  const feedLabels = {
    headline: 'Headline', headline_matched: 'Headline', headline_unmatched: 'Headline',
    debate: 'Debate', debate_outcome: 'Debate', call_opened: 'Call', call_refused: 'Refusal',
    thesis_review: 'Review', review_raised: 'Review', trade_opened: 'Trade', trade_closed: 'Trade',
    skill_changed: 'Skill', seat_reset: 'Reset',
  };
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
      item.append(meta, el('span', value(event.title)));
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
      const live = (data.desk?.heartbeat_at ?? 0) > Date.now() / 1000 - 900;
      person.append(window.OfficeFloor.station(seat, reading, index, null, live, data.health?.seats?.[seat]));
      const said = el('p', null, 'od-scene-said');
      said.append(el('strong', window.OfficeFloor.seatName(seat)), document.createTextNode(' ' + (reading?.last_action ? String(reading.last_action).replaceAll('_', ' ') : 'Nothing recorded yet')));
      person.append(said);
      const badge = askBadge([seat]);
      if (badge) said.append(' ', badge);
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
    if (active === 'control') {
      const chat = operatorChatPanel();
      if (chat) content.append(chat);
    }
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
      data = await window.OfficeAdapter.adapt(await response.json());
      for (const [id, seats] of Object.entries(data.rooms ?? {})) if (rooms.has(id)) rooms.get(id).seats = seats;
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
