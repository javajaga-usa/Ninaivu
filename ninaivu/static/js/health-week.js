/**
 * The week's health (ninaivu/server/health_week.py): damaged files, free
 * space, failed jobs and drives over the last seven days, for administrators.
 *
 * Two places show it. The console's Overview has the whole of it, under "Is
 * everything safe?", with the weeks kept before it folded underneath. The
 * family app puts a short card at the top for an administrator who is signed
 * in there, which can be put away until the next week. Anybody else is
 * refused by the server and sees nothing.
 */

import * as i18n from './i18n.js';

const $ = (sel) => document.querySelector(sel);

const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
};

const URL = '/api/health/week';

/** A sentence from the server, in the reader's language. The names of a
 *  part or a disk are translated too where the locales carry them. */
function sentence(line) {
  const params = { ...(line.params || {}) };
  for (const name of ['name', 'title']) {
    if (typeof params[name] === 'string') params[name] = i18n.t(params[name]);
  }
  return i18n.t(line.key, params);
}

function headline(week) {
  return week.headline_key ? i18n.t(week.headline_key, week.headline_params || {})
    : i18n.t(week.headline || '');
}

function span(since, until) {
  const fmt = (t) => new Date(t * 1000).toLocaleDateString(i18n.language(),
    { day: 'numeric', month: 'short' });
  return `${fmt(since)} – ${fmt(until)}`;
}

async function ask(fresh) {
  const response = await fetch(`${URL}${fresh ? '?fresh=1' : ''}`,
    { headers: { Accept: 'application/json' }, credentials: 'same-origin' });
  if (!response.ok) return null;
  const data = await response.json();
  return data && data.current && Array.isArray(data.current.parts)
    && Array.isArray(data.weeks) ? data : null;
}

//: The console pages a part's button opens.
const PAGES = { health: i18n.key('Health'), activity: i18n.key('Activity') };

/* ========================================================================
   The console's Overview
   ======================================================================== */

export class WeekPanel {
  constructor({ toast, openPage } = {}) {
    this.toast = toast || (() => {});
    this.openPage = openPage || (() => {});
  }

  wire() {
    const button = $('#hw-refresh');
    if (!button) return;
    button.onclick = () => this.load(true);
    i18n.onChange(() => { if (this.data) this.render(this.data); });
  }

  async load(fresh = false) {
    if (!$('#hw-block')) return;
    try {
      const data = await ask(fresh);
      if (!data) { $('#hw-block').hidden = true; return; }
      this.render(data);
    } catch (exc) {
      this.toast(exc.message, true);
    }
  }

  render(data) {
    this.data = data;
    const week = data.current;
    $('#hw-block').hidden = false;
    const head = $('#hw-headline');
    head.textContent = `${headline(week)} · ${span(week.since, week.until)}`;
    head.className = `sf-headline sf-${week.verdict}`;
    $('#hw-parts').replaceChildren(...week.parts.map((part) => this.row(part)));
    const earlier = $('#hw-earlier');
    earlier.replaceChildren();
    earlier.hidden = !data.weeks.length;
    if (data.weeks.length) {
      const details = el('details');
      details.append(el('summary', null, data.weeks.length === 1
        ? i18n.t('1 earlier week') : i18n.t('{count} earlier weeks', { count: data.weeks.length })));
      const list = el('ul', 'sf-checks');
      for (const kept of data.weeks) {
        const item = el('li', 'sf-check');
        const text = el('div', 'sf-text');
        text.append(el('span', 'sf-title', span(kept.since, kept.until)),
          el('span', 'sf-summary', headline(kept)));
        const bad = (kept.parts || []).filter((p) => p.status !== 'ok');
        if (bad.length) {
          text.append(el('span', 'sf-detail', bad.map((p) => `${i18n.t(p.title)}: `
            + (p.lines || []).map(sentence).join(' ')).join(' · ')));
        }
        item.append(el('span', `sf-dot sf-${kept.verdict}`), text, el('span'));
        list.append(item);
      }
      details.append(list);
      earlier.append(details);
    }
  }

  row(part) {
    const row = el('li', 'sf-check');
    const text = el('div', 'sf-text');
    text.append(el('span', 'sf-title', i18n.t(part.title)),
      el('span', 'sf-summary', part.lines.map(sentence).join(' ')));
    const dot = el('span', `sf-dot sf-${part.status}`);
    dot.title = part.status;
    row.append(dot, text);
    if (part.status !== 'ok' && PAGES[part.page]) {
      const go = el('button', 'btn ghost small', i18n.t('Open {page}', { page: i18n.t(PAGES[part.page]) }));
      go.type = 'button';
      go.onclick = () => this.openPage(part.page);
      row.append(go);
    } else {
      row.append(el('span'));
    }
    return row;
  }
}

/* ========================================================================
   The family app, for an administrator
   ======================================================================== */

const PUT_AWAY = 'ninaivu.health-week.away';

/** The Monday the week shown began, so "until next week" means just that. */
function weekOf(until) {
  const day = new Date(until * 1000);
  day.setHours(0, 0, 0, 0);
  day.setDate(day.getDate() - ((day.getDay() + 6) % 7));
  return day.toISOString().slice(0, 10);
}

function putAway() {
  try { return window.localStorage.getItem(PUT_AWAY) || ''; } catch { return ''; }
}

/**
 * A card at the top of *mount* with the week's four lines, for an
 * administrator. *consoleUrl* is where "Open the console" goes.
 */
export function weekCard(mount, { consoleUrl } = {}) {
  if (!mount) return;
  const card = el('section', 'week-card');
  card.setAttribute('aria-label', i18n.t('This week'));
  card.hidden = true;
  // Under a waiting handover's banner, which matters more.
  const banner = mount.querySelector(':scope > .handover-banner');
  if (banner) banner.after(card); else mount.prepend(card);
  let data = null;

  const draw = () => {
    card.replaceChildren();
    if (!data) { card.hidden = true; return; }
    const week = data.current;
    if (putAway() === weekOf(week.until)) { card.hidden = true; return; }
    card.hidden = false;
    card.dataset.verdict = week.verdict;
    const head = el('div', 'week-card-head');
    const title = el('div', 'week-card-title');
    title.append(el('strong', null, i18n.t('This week')), el('span', `week-card-headline sf-${week.verdict}`, headline(week)));
    head.append(title);
    const actions = el('div', 'week-card-actions');
    if (consoleUrl) {
      const open = el('a', 'btn ghost small', i18n.t('Open the console'));
      open.href = consoleUrl;
      open.target = '_blank';
      open.rel = 'noopener';
      actions.append(open);
    }
    const hide = el('button', 'btn ghost small', i18n.t('Hide until next week'));
    hide.type = 'button';
    hide.onclick = () => {
      try { window.localStorage.setItem(PUT_AWAY, weekOf(week.until)); } catch { /* kept for this page only */ }
      card.hidden = true;
    };
    actions.append(hide);
    head.append(actions);
    const list = el('ul', 'week-card-parts');
    for (const part of week.parts) {
      const item = el('li', `week-card-part sf-${part.status}`);
      item.append(el('span', `sf-dot sf-${part.status}`), el('span', 'week-card-name', i18n.t(part.title)),
        el('span', 'week-card-line', sentence(part.lines[0] || { key: '' })));
      list.append(item);
    }
    card.append(head, list);
  };

  const check = async () => {
    try { data = await ask(false); } catch { data = null; }
    draw();
  };
  check();
  i18n.onChange(draw);
  setInterval(check, 60 * 60 * 1000);
}
