(() => {
  'use strict';
  const text = (tag, content, cls) => {
    const node = document.createElement(tag);
    if (content != null) node.textContent = content;
    if (cls) node.className = cls;
    return node;
  };
  const STATES = {holding: 'Holding a paper call', debating: 'Debating', watching: 'Watching', review_pending: 'Review pending', headlines_only: 'Headlines only'};
  const LABELS = {headlines: 'Headlines', markets: 'Debated markets', open_calls: 'Open calls',
    pending_reviews: 'Pending reviews', scanner_candidates: 'Scanner candidates'};
  let selected;
  const reading = data => data.situation ?? {};
  const recorded = data => [...(reading(data).stories ?? []), ...(reading(data).unplaced ?? []), ...(reading(data).no_geography ?? [])];
  const own = value => typeof value === 'string' && value.trim() ? value.trim().replaceAll('_', ' ') : '';
  const number = value => Number.isFinite(value) ? value : null;
  function age(seconds) {
    return number(seconds) === null ? '' : seconds < 60 ? 'Just now'
      : seconds < 3600 ? `${Math.floor(seconds / 60)} min ago` : `${Math.floor(seconds / 3600)} h ago`;
  }
  function facts(story) {
    const section = text('section');
    section.dataset.itemId = story.id;
    section.append(text('h3', story.name), text('p', [STATES[story.state], age(story.age_s),
      story.places?.map(place => place.name).join(', ') || 'No recorded place'].filter(Boolean).join(' · ')));
    for (const [key, title] of Object.entries(LABELS)) {
      const rows = story[key] ?? [];
      if (!rows.length) continue;
      section.append(text('h4', title));
      const list = text('ul');
      for (const row of rows) {
        const label = [row.title ?? row.question ?? row.market, own(row.side), own(row.status ?? row.outcome), key === "headlines" ? age(row.age_s) : null]
          .filter(Boolean).join(' · ');
        if (!label) continue;
        const item = text('li', label);
        for (const [seat, reply] of Object.entries(row.seats ?? {})) {
          const excerpt = own(reply?.assessment ?? reply?.story ?? reply?.reason ?? reply?.read ?? reply?.case ?? reply?.why);
          const probability = number(reply?.probability_yes);
          if (excerpt || probability !== null) {
            const name = window.OfficeFloor.seatName(seat);
            item.append(text('p', [name, probability === null ? null : `${Math.round(100 * probability)}% YES`, excerpt]
              .filter(Boolean).join(' · ')));
          }
        }
        list.append(item);
      }
      if (list.children.length) section.append(list);
    }
    return section;
  }
  function map(data, onSelect, compact) {
    const box = text('div', null, 'office-situation-map');
    const highlight = typeof WorldMap !== 'undefined'
      ? WorldMap.storyMap(box, reading(data).stories ?? [], onSelect, compact, reading(data).as_of)
      : () => {};
    if (!compact && typeof WorldMap !== 'undefined') {
      const legend = text('div', null, 'situation-legend');
      for (const {label, color} of Object.values(WorldMap.storyStates)) {
        const item = text('span', label);
        item.style.setProperty('--situation-color', color);
        legend.append(item);
      }
      box.append(legend);
    }
    return {box, highlight};
  }
  window.OfficeRooms.register({
    id: 'situation', title: 'Situation', order: 35, seats: [],
    figures(data) {
      return reading(data).stories ? [{label:'Placed stories', value:reading(data).stories.length}] : [];
    },
    summary(data) {
      const wall = text('div', null, 'office-situation-wall');
      wall.append(map(data, null, true).box);
      const top = (reading(data).stories ?? [])[0];
      if (top) wall.append(text('p', 'Top story: ' + top.name));
      else wall.append(text('p', 'No stories recorded here'));
      return wall;
    },
    detail(data) {
      const result = text('div', null, 'office-situation');
      if (!reading(data).stories) {
        result.append(text('p', 'This office records no stories or places, so there is nothing to place on the map.'));
        return result;
      }
      const stories = typeof WorldMap !== 'undefined' ? WorldMap.storyOrder(recorded(data), reading(data).as_of) : recorded(data);
      const byId = new Map(stories.map(story => [story.id, story]));
      const header = text('div', null, 'office-situation-stats');
      const recent = stories.reduce((youngest, story) => number(story.age_s) === null ? youngest : Math.min(youngest, story.age_s), Infinity);
      for (const [label, value] of [
        ['Stories tracked', stories.length],
        ['Review queue', stories.filter(story => story.state === 'review_pending').length],
        ['Latest activity', age(recent) || 'No recorded activity'],
      ]) {
        const card = text('div');
        card.append(text('strong', value), text('span', label));
        header.append(card);
      }
      const records = text('div', null, 'office-situation-records');
      const overview = text('div', null, 'office-situation-overview');
      const side = text('aside', null, 'office-situation-side');
      const nearby = text('div', null, 'office-situation-nearby');
      const tabs = text('div', null, 'office-situation-stories');
      function select(id, show = true) {
        const story = byId.get(id);
        if (!story) return;
        selected = id;
        const detail = facts(story);
        records.replaceChildren(detail);
        if (show) {
          detail.tabIndex = -1;
          detail.focus({preventScroll: true});
          detail.scrollIntoView({block: 'nearest'});
        }
        highlight(id);
        for (const button of side.querySelectorAll('[data-story]')) button.setAttribute('aria-pressed', String(button.dataset.story === id));
      }
      const {box, highlight} = map(data, ids => {
        nearby.replaceChildren();
        if (ids.length > 1) nearby.append(text('h3', `${ids.length} nearby stories`));
        for (const id of ids) nearby.append(storyButton(byId.get(id), ids.length === 1));
        select(ids.includes(selected) ? selected : ids[0], false);
      }, false);
      function storyButton(story, includeFacts) {
        const button = text('button', null, 'situation-story-row');
        button.type = 'button';
        button.dataset.story = story.id;
        const appearance = typeof WorldMap !== 'undefined' ? WorldMap.storyStates[story.state] : null;
        if (appearance) button.style.setProperty('--situation-color', appearance.color);
        button.append(text('strong', story.name));
        if (includeFacts) {
          const place = story.places?.map(place => place.name).join(', ') || 'No recorded place';
          const state = STATES[story.state];
          const counts = [['headlines', 'headline'], ['markets', 'market'], ['open_calls', 'call']]
            .map(([key, label]) => {
              const n = story.counts?.[key];
              return Number.isFinite(n) ? `${n} ${label}${n === 1 ? '' : 's'}` : null;
            });
          button.append(text('span', [state, place, ...counts, age(story.age_s)].filter(Boolean).join(' · ')));
        }
        button.addEventListener('click', () => select(story.id, false));
        return button;
      }
      for (const [label, items] of [
        ['Placed', stories.filter(story => story.places?.length)],
        ['Named geography without a map anchor', stories.filter(story => story.geography_status === 'named')],
        ['Geography unread', stories.filter(story => !story.places?.length && !['named', 'none'].includes(story.geography_status))],
        ['No geography', stories.filter(story => story.geography_status === 'none')],
      ]) {
        if (!items.length) continue;
        tabs.append(text('h3', label));
        for (const story of items) tabs.append(storyButton(story, true));
      }
      side.append(nearby, tabs);
      overview.append(box, side);
      result.append(header, overview, records);
      if (stories.length) select(byId.has(selected) ? selected : stories[0].id, false);
      return result;
    },
  });
})();
