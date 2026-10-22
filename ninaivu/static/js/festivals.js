/**
 * Festival albums: every Deepavali, every Pongal, side by side
 * (ninaivu/media/festivals.py finds them, ninaivu/api/api_festivals.py serves
 * them; shown only while `festival_albums` is switched on).
 *
 * Full screen over the gallery, like A life in photographs. It opens on the
 * festivals, each a card with its latest photograph, the years found and
 * the festival's next day; a festival opens on its years side by side, the
 * oldest on the left, each a column with that year's day and its best
 * photographs, so the doorstep lamps of 1994 sit beside last year's. A chip
 * for each year jumps along the row. A tap opens that year's photographs in
 * the viewer. It ends with a book of every year (static/js/books.js).
 *
 * Nobody tagged any of it: the days come from the Tamil calendar and the
 * picture search keeps only the days that look like the festival.
 */

import { thumbUrl } from './api.js';
import * as i18n from './i18n.js';
import { openBookSheet } from './books.js';

let root = null;
let body = null;
let rail = null;
let titleEl = null;
let back = null;
let showing = null;
let asking = 0;
let helpers = { toast: () => {}, openItems: () => Promise.resolve(), books: false };

/** The festivals' names, for the translations. */
const NAMES = {
  pongal: i18n.key('Pongal'),
  puthandu: i18n.key('Tamil New Year'),
  krishna: i18n.key('Krishna Jayanthi'),
  vinayagar: i18n.key('Vinayagar Chathurthi'),
  navaratri: i18n.key('Navaratri'),
  deepavali: i18n.key('Deepavali'),
  karthigai: i18n.key('Karthigai Deepam'),
  christmas: i18n.key('Christmas'),
};

/** A line under each name: what the picture search looked for. */
const LOOKS = {
  pongal: i18n.key('The pot boiling over, sugarcane and kolam'),
  puthandu: i18n.key('New clothes, the fruit tray and the feast'),
  krishna: i18n.key('Little footprints and baby Krishna'),
  vinayagar: i18n.key('Pillaiyar at home, with flowers and kozhukattai'),
  navaratri: i18n.key('Golu dolls on their steps'),
  deepavali: i18n.key('Lamps, sparklers and new clothes'),
  karthigai: i18n.key('Rows of lamps on the doorstep'),
  christmas: i18n.key('The tree, the star and the cake'),
};

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k in node) node[k] = v;
    else node.setAttribute(k, v);
  }
  node.append(...children.filter(Boolean));
  return node;
}

async function call(url) {
  const response = await fetch(url, { headers: { Accept: 'application/json', 'X-Requested-With': 'fetch' } });
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    throw Object.assign(new Error(i18n.fromServer(data, response.statusText)),
      { status: response.status, data });
  }
  return data;
}

function num(n) {
  return Number(n || 0).toLocaleString(i18n.locale());
}

function day(iso, withYear = false) {
  const [y, m, d] = String(iso || '').split('-').map(Number);
  if (!y || !m || !d) return '';
  const options = { day: 'numeric', month: 'long', timeZone: 'UTC' };
  if (withYear) options.year = 'numeric';
  try {
    return new Intl.DateTimeFormat(i18n.locale(), options).format(new Date(Date.UTC(y, m - 1, d)));
  } catch {
    return String(iso);
  }
}

function name(festival) {
  return NAMES[festival.key] ? i18n.t(NAMES[festival.key]) : festival.name;
}

function span(festival) {
  if (!festival.first) return '';
  return festival.first === festival.last ? String(festival.first) : `${festival.first} – ${festival.last}`;
}

/** The viewer, in place of the albums until it is closed: it sits below
 *  full-screen pages, and Escape there must close it, not the albums. */
async function openPhotos(ids, id) {
  const row = body.querySelector('.fv-years');
  const top = body.scrollTop;
  const left = row ? row.scrollLeft : 0;
  root.hidden = true;
  try {
    await helpers.openItems(ids, Math.max(0, ids.indexOf(id)));
  } finally {
    if (showing !== null) {
      root.hidden = false;
      body.scrollTop = top;
      const again = body.querySelector('.fv-years');
      if (again) again.scrollLeft = left;
      root.focus({ preventScroll: true });
    }
  }
}

function tile(item, ids, cls, size = 512) {
  const img = el('img', { src: thumbUrl(item.id, size, item.thumb_v), alt: '', loading: 'lazy', draggable: false });
  const button = el('button', { type: 'button', class: cls,
                                'aria-label': i18n.t('Open the photo from {date}', { date: day(item.date, true) }) }, img);
  button.onclick = () => openPhotos(ids, item.id);
  return button;
}

/* -- every festival ---------------------------------------------------------- */

function card(festival) {
  const found = festival.years.length;
  const cover = festival.cover
    ? el('img', { src: thumbUrl(festival.cover.id, 512, festival.cover.thumb_v), alt: '', loading: 'lazy', draggable: false })
    : el('span', { class: 'fv-glyph', 'aria-hidden': 'true' });
  const lines = found
    ? [i18n.t('{n} years', { n: num(found) }), span(festival), i18n.t('{n} photos', { n: num(festival.items) })]
    : [i18n.t('None found yet')];
  const node = el('button', { type: 'button', class: `fv-card fv-${festival.key}${found ? '' : ' fv-none'}`,
                              disabled: !found },
    el('span', { class: 'fv-card-cover' }, cover),
    el('span', { class: 'fv-card-text' },
      el('strong', { class: 'fv-card-name', text: name(festival) }),
      el('span', { class: 'fv-card-meta', text: lines.filter(Boolean).join(' · ') }),
      el('span', { class: 'fv-card-next', text: i18n.t('Next: {date}', { date: day(festival.next, true) }) })));
  if (found) node.onclick = () => show(festival.key);
  return node;
}

async function home() {
  const ticket = ++asking;
  showing = null;
  root.className = 'festivals';
  back.hidden = true;
  rail.hidden = true;
  titleEl.textContent = i18n.t('Festival albums');
  body.replaceChildren(el('p', { class: 'fv-empty', text: i18n.t('Lighting the lamps…') }));
  let data;
  try {
    data = await call('/api/festivals');
  } catch (error) {
    if (ticket === asking) body.replaceChildren(el('p', { class: 'fv-empty', text: error.message }));
    return;
  }
  if (ticket !== asking) return;
  const festivals = [...data.festivals].sort((a, b) => (b.years.length > 0) - (a.years.length > 0));
  const anyByDate = data.festivals.some((f) => f.years.some((y) => !y.checked));
  body.replaceChildren(
    el('p', { class: 'fv-intro', text: i18n.t('Every festival, year after year: the days worked out from the Tamil calendar, the photos found without anybody tagging them.') }),
    el('div', { class: 'fv-cards' }, ...festivals.map(card)),
    anyByDate ? el('p', { class: 'fv-note', text: i18n.t('Some years are found from their dates alone. Once the picture search has read those photos, only the days that look like the festival are kept.') }) : null);
}

/* -- one festival, its years side by side ------------------------------------ */

function column(entry, index) {
  const [first, ...rest] = entry.photos;
  const facts = [i18n.t('{n} photos', { n: num(entry.pictures) })];
  if (entry.videos) facts.push(i18n.t('{n} videos', { n: num(entry.videos) }));
  const open = el('button', { type: 'button', class: 'btn ghost small fv-open', text: i18n.t('Open all') });
  open.onclick = () => openPhotos(entry.ids, entry.ids[0]);
  return el('article', { class: 'fv-year', id: `fv-y-${index}`, 'data-index': String(index) },
    el('header', { class: 'fv-year-head' },
      el('h2', { class: 'fv-year-num', text: String(entry.year) }),
      el('p', { class: 'fv-year-day', text: day(entry.day) })),
    first ? tile(first, entry.ids, 'fv-lead', 1024) : el('div', { class: 'fv-lead fv-lead-empty' }),
    rest.length ? el('div', { class: `fv-more fv-n${Math.min(rest.length, 5)}` },
      ...rest.map((p) => tile(p, entry.ids, 'fv-tile'))) : null,
    el('footer', { class: 'fv-year-foot' },
      el('span', { class: 'fv-year-meta', text: facts.join(' · ') }),
      entry.checked ? null : el('span', { class: 'fv-by-date', title: i18n.t('Found from the date alone'), text: i18n.t('by date') }),
      open));
}

function railFor(festival, row) {
  rail.replaceChildren(...festival.years.map((entry, index) => {
    const b = el('button', { type: 'button', class: 'fv-rail-step', text: String(entry.year) });
    b.onclick = () => row.querySelector(`#fv-y-${index}`)?.scrollIntoView({ behavior: 'smooth', inline: 'start', block: 'nearest' });
    return b;
  }));
  rail.hidden = festival.years.length < 3;
}

async function show(key) {
  const ticket = ++asking;
  showing = key;
  root.className = `festivals fv-${key}`;
  back.hidden = false;
  titleEl.textContent = i18n.t('Festival albums');
  body.replaceChildren(el('p', { class: 'fv-empty', text: i18n.t('Lighting the lamps…') }));
  rail.hidden = true;
  let festival;
  try {
    festival = await call(`/api/festivals/${encodeURIComponent(key)}`);
  } catch (error) {
    if (ticket === asking) body.replaceChildren(el('p', { class: 'fv-empty', text: error.message }));
    return;
  }
  if (ticket !== asking) return;
  titleEl.textContent = name(festival);
  if (!festival.years.length) {
    body.replaceChildren(el('p', { class: 'fv-empty', text: i18n.t('No photos of {name} have been found yet.', { name: name(festival) }) }));
    return;
  }
  const cover = festival.cover;
  const all = festival.years.flatMap((y) => y.ids);
  const hero = el('section', { class: 'fv-hero' },
    cover ? tile(cover, all, 'fv-hero-img', 1280) : null,
    el('div', { class: 'fv-hero-text' },
      el('p', { class: 'fv-kicker', text: i18n.t(LOOKS[festival.key] || '') }),
      el('h1', { class: 'fv-name', text: i18n.t('Every {name}', { name: name(festival) }) }),
      el('p', { class: 'fv-span', text: [span(festival),
        i18n.t('{n} years', { n: num(festival.years.length) }),
        i18n.t('{n} photos', { n: num(festival.items) })].filter(Boolean).join(' · ') }),
      el('p', { class: 'fv-next', text: i18n.t('Next: {date}', { date: day(festival.next, true) }) })));
  const row = el('div', { class: 'fv-years', role: 'list', tabindex: '0',
                          'aria-label': i18n.t('Every {name}', { name: name(festival) }) },
    ...festival.years.map(column));
  const end = el('footer', { class: 'fv-end' });
  if (helpers.books) {
    const book = el('button', { type: 'button', class: 'btn primary', text: i18n.t('Make a book') });
    book.onclick = () => openBookSheet({ kind: 'festival', id: festival.index,
                                         name: i18n.t('Every {name}', { name: name(festival) }) },
                                       { toast: helpers.toast });
    end.append(el('p', { text: i18n.t('Keep every {name} in one book, year after year.', { name: name(festival) }) }),
      el('div', { class: 'fv-end-actions' }, book));
  }
  body.replaceChildren(hero, row, end.childElementCount ? end : null);
  railFor(festival, row);
  body.scrollTop = 0;
  // The newest year in view first; the older ones are a swipe to the left.
  requestAnimationFrame(() => { row.scrollLeft = row.scrollWidth; });
}

function build() {
  if (root) return root;
  root = el('div', { class: 'festivals', id: 'festivals', hidden: true, role: 'dialog', 'aria-modal': 'true',
                     'aria-label': i18n.t('Festival albums'), tabindex: '-1' });
  const closer = el('button', { type: 'button', class: 'icon-btn fv-close', 'aria-label': i18n.t('Close') });
  closer.innerHTML = '<svg viewBox="0 0 24 24"><path d="M18 6 6 18M6 6l12 12"/></svg>';
  closer.onclick = close;
  back = el('button', { type: 'button', class: 'icon-btn fv-back', 'aria-label': i18n.t('Every festival'), hidden: true });
  back.innerHTML = '<svg viewBox="0 0 24 24"><path d="m15 18-6-6 6-6"/></svg>';
  back.onclick = home;
  titleEl = el('p', { class: 'fv-bar-title' });
  rail = el('nav', { class: 'fv-rail', 'aria-label': i18n.t('Years'), hidden: true });
  body = el('div', { class: 'fv-body' });
  root.append(el('header', { class: 'fv-bar' }, back, titleEl, closer), rail, body);
  root.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') close();
  });
  document.body.appendChild(root);
  return root;
}

/** Open the festival albums: every festival, or the one named by *festival*
 *  (its key, such as "deepavali"). *books* offers a book of a festival. */
export async function openFestivals({ toast, openItems, books, festival } = {}) {
  helpers = { toast: toast || helpers.toast, openItems: openItems || helpers.openItems, books: !!books };
  build();
  root.hidden = false;
  document.body.style.overflow = 'hidden';
  root.focus();
  if (festival) await show(festival);
  else await home();
}

function close() {
  if (!root || root.hidden) return;
  asking += 1;
  root.hidden = true;
  document.body.style.overflow = '';
  body.replaceChildren();
  showing = null;
}
