/**
 * One status card for work that runs on its own: the second copy, the
 * originals on their way to the Secondary, the Secondary's update.
 *
 * Each of those used to say what it was doing as one paragraph of sentences
 * joined together, so whether it was busy, done, waiting or stuck was a
 * matter of reading all of it. A card says that first, in one word with a
 * colour (``tone``), then one line of what it means, then a bar for how far
 * it has got, the steps when there are some, and the rest as a short list,
 * each fact on its own line, the ones that need someone marked.
 *
 *   renderStatus(host, {
 *     tone: 'ok' | 'busy' | 'wait' | 'warn' | 'off',
 *     state: 'Copying', headline: '1,200 of 5,000 files are on the copy.',
 *     bar: { value: 1200, max: 5000, label: '24%' },       // or null
 *     steps: [{ label: 'Send', at: 'done' | 'now' | 'todo' | 'bad' }],
 *     facts: ['plain line', { text: 'needs you', tone: 'warn' }],
 *   });
 *
 * Every word is given already in the page's language: this only lays out.
 */

const TONES = new Set(['ok', 'busy', 'wait', 'warn', 'off']);
const AT = new Set(['done', 'now', 'todo', 'bad']);

/** How far *value* is of *max*, as a whole percent from 0 to 100. */
export function percent(value, max) {
  const top = Number(max) || 0;
  if (top <= 0) return 0;
  const share = Math.max(0, Math.min(1, (Number(value) || 0) / top));
  // 99.6% is not "100%": the last file is still to go.
  return share >= 1 ? 100 : Math.min(99, Math.floor(share * 100));
}

export function renderStatus(host, card) {
  if (!host) return;
  if (!card) { host.replaceChildren(); host.hidden = true; return; }
  host.hidden = false;
  const tone = TONES.has(card.tone) ? card.tone : 'off';
  const box = document.createElement('div');
  box.className = 'jst-card';
  box.dataset.tone = tone;

  const head = document.createElement('div');
  head.className = 'jst-head';
  const dot = document.createElement('span');
  dot.className = 'jst-dot';
  dot.setAttribute('aria-hidden', 'true');
  const state = document.createElement('strong');
  state.className = 'jst-state';
  state.textContent = card.state || '';
  head.append(dot, state);
  if (card.headline) {
    const line = document.createElement('span');
    line.className = 'jst-headline';
    line.textContent = card.headline;
    head.append(line);
  }
  box.append(head);

  if (card.bar && Number(card.bar.max) > 0) {
    const value = percent(card.bar.value, card.bar.max);
    const bar = document.createElement('div');
    bar.className = 'jst-bar';
    bar.setAttribute('role', 'progressbar');
    bar.setAttribute('aria-valuemin', '0');
    bar.setAttribute('aria-valuemax', '100');
    bar.setAttribute('aria-valuenow', String(value));
    if (card.bar.label) bar.setAttribute('aria-valuetext', card.bar.label);
    const fill = document.createElement('span');
    fill.style.width = `${value}%`;
    bar.append(fill);
    box.append(bar);
    if (card.bar.label) {
      const label = document.createElement('p');
      label.className = 'jst-bar-label';
      label.textContent = card.bar.label;
      box.append(label);
    }
  }

  if (card.steps?.length) {
    const steps = document.createElement('ol');
    steps.className = 'jst-steps';
    for (const step of card.steps) {
      const item = document.createElement('li');
      item.dataset.at = AT.has(step.at) ? step.at : 'todo';
      item.textContent = step.label;
      if (step.at === 'now') item.setAttribute('aria-current', 'step');
      steps.append(item);
    }
    box.append(steps);
  }

  const facts = (card.facts || []).filter(Boolean)
    .map((fact) => (typeof fact === 'string' ? { text: fact } : fact))
    .filter((fact) => fact.text);
  if (facts.length) {
    const list = document.createElement('ul');
    list.className = 'jst-facts';
    for (const fact of facts) {
      const item = document.createElement('li');
      if (fact.tone === 'warn') item.className = 'jst-warn';
      if (fact.tone === 'quiet') item.className = 'jst-quiet';
      item.textContent = fact.text;
      list.append(item);
    }
    box.append(list);
  }
  host.replaceChildren(box);
}
