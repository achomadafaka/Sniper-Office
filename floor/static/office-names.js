(() => {
  'use strict';
  // Token names for the page. A coin is shown as "Name ($SYMBOL)"; the contract address is only a small secondary detail with a copy button, and the whole label falls back to the short address while the name is unknown.
  // Names are provider text (untrusted): they only ever reach the page through textContent, capped here again.
  const known = new Map(), wanted = new Set(), tried = new Map();
  const key = (chain, mint) => (chain || 'solana') + ':' + mint;
  const shortCa = mint => String(mint).length > 12 ? String(mint).slice(0, 4) + '…' + String(mint).slice(-4) : String(mint);
  const cap = (value, n) => String(value ?? '').replace(/[\p{Cc}\p{Cf}]/gu, '').replace(/\s+/g, ' ').trim().slice(0, n);
  const label = (chain, mint, ticker) => {
    const hit = known.get(key(chain, mint)), name = cap(hit?.name, 40), symbol = cap(hit?.symbol || ticker, 16).replace(/^\$/, '');
    return name && symbol ? `${name} ($${symbol})` : name || (symbol ? '$' + symbol : null);
  };
  function text(chain, mint, ticker) {
    if (!mint) return ticker ? '$' + String(ticker).replace(/^\$/, '') : 'unknown';
    wanted.add(key(chain, mint));
    return label(chain, mint, ticker) ?? shortCa(mint);
  }
  function paint(box) {
    const {chain, mint, ticker} = box.dataset, name = label(chain, mint, ticker);
    const main = document.createElement('span');
    main.className = 'tok-name';
    main.textContent = name ?? shortCa(mint);
    const ca = document.createElement('code');
    ca.className = 'tok-ca';
    ca.textContent = name ? shortCa(mint) : '';
    ca.title = mint;
    const copy = document.createElement('button');
    copy.type = 'button';
    copy.className = 'tok-copy';
    copy.textContent = 'Copy';
    copy.setAttribute('aria-label', 'Copy contract address');
    copy.title = mint;
    copy.addEventListener('click', async event => {
      event.stopPropagation();
      try { await navigator.clipboard.writeText(mint); copy.textContent = 'Copied'; } catch { copy.textContent = 'Copy unavailable'; }
    });
    box.replaceChildren(main, ...(name ? [' ', ca] : []), ' ', copy);
    box.title = mint;
  }
  function node(chain, mint, ticker) {
    const box = document.createElement('span');
    box.className = 'tok';
    if (!mint) { box.textContent = text(chain, mint, ticker); return box; }
    box.dataset.chain = chain || 'solana';
    box.dataset.mint = mint;
    box.dataset.ticker = ticker ?? '';
    wanted.add(key(chain, mint));
    paint(box);
    return box;
  }
  // Free text (seat words, headlines, feed lines) may carry a raw address: show the name when it is known, else the short form.
  const SOL = /\b[1-9A-HJ-NP-Za-km-z]{43,44}\b/g, EVM = /\b0x[a-fA-F0-9]{40}\b/g;
  const scrub = value => String(value ?? '').replace(EVM, a => shortCa(a)).replace(SOL, a => { wanted.add(key('solana', a)); return label('solana', a) ?? shortCa(a); });
  const valid = id => /^[a-z0-9][a-z0-9-]{1,23}:[A-Za-z0-9]{20,66}$/.test(id);
  async function fetchNames(ids) {
    const unknown = ids.filter(id => valid(id) && !known.has(id) && (tried.get(id) ?? 0) < 40);
    for (let i = 0; i < unknown.length; i += 40) {
      const part = unknown.slice(i, i + 40);
      part.forEach(id => tried.set(id, (tried.get(id) ?? 0) + 1));
      try {
        const response = await fetch('/api/office/token-names?ids=' + encodeURIComponent(part.join(',')), {cache: 'no-store'});
        if (!response.ok) continue;
        for (const [id, hit] of Object.entries((await response.json()).names ?? {})) if (hit && (hit.name || hit.symbol)) known.set(id, {name: cap(hit.name, 40), symbol: cap(hit.symbol, 16)});
      } catch { /* the label stays the short address */ }
    }
    for (const box of document.querySelectorAll('.tok[data-mint]')) paint(box);
  }
  window.OfficeNames = {
    text, node, label, shortCa, scrub,
    // Ask for these coins and wait at most `ms`, so the first paint already has the names that are local to the host.
    ensure: (pairs, ms = 1200) => {
      const ids = [...new Set(pairs.filter(([, mint]) => mint).map(([chain, mint]) => key(chain, mint)))];
      ids.forEach(id => wanted.add(id));
      return Promise.race([fetchNames(ids), new Promise(resolve => setTimeout(resolve, ms))]);
    },
  };
  // Names the host is still looking up arrive a few seconds later: ask again for the unresolved ones.
  setInterval(() => { if (!document.hidden) fetchNames([...wanted].filter(id => !known.has(id))); }, 4000);
})();
