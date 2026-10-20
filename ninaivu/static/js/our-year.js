/**
 * Our Year: the family's year in photographs, as cards to tap through
 * (ninaivu/media/our_year.py counts it, ninaivu/api/api_our_year.py serves it;
 * shown only while `our_year` is switched on).
 *
 * Full screen over the gallery, like the photo frame: one card at a time with
 * the segments of the year along the top, the next card after a few seconds,
 * a tap on the right for the next and on the left for the one before, the
 * arrow keys and Escape on a computer, and a press held to stop and read.
 * The cards are written here, in the page's language, from the facts the
 * server sends; the opening words are the on-device writing model's when it
 * is there, and this page's own sentences when it is not. A card with nothing
 * to say (no trips, no stories, a guest's people) is simply not there.
 *
 * The last card offers a book and a film of the year (static/js/books.js,
 * static/js/films.js), which already make one of a year.
 */

import { thumbUrl } from './api.js';
import * as i18n from './i18n.js';
import { openBookSheet } from './books.js';
import { filmsAvailable, openFilmSheet } from './films.js';

//: How long a card stays before the next, in milliseconds.
const CARD_MS = 7000;

let root = null;
let deck = null;
let bars = null;
let picker = null;
let cards = [];
let at = 0;
let timer = null;
let started = 0;
let elapsed = 0;
let held = false;
let found = null;
let helpers = { toast: () => {}, openItems: () => Promise.resolve() };

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

async function call(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: { Accept: 'application/json', 'X-Requested-With': 'fetch',
               ...(options.body ? { 'Content-Type': 'application/json' } : {}),
               ...(options.headers || {}) },
  });
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

function monthName(month, style = 'long') {
  try {
    return new Intl.DateTimeFormat(i18n.locale(), { month: style, timeZone: 'UTC' })
      .format(new Date(Date.UTC(2020, month - 1, 15)));
  } catch {
    return String(month);
  }
}

function day(iso) {
  const [y, m, d] = String(iso || '').split('-').map(Number);
  if (!y || !m || !d) return String(iso || '');
  try {
    return new Intl.DateTimeFormat(i18n.locale(), { day: 'numeric', month: 'long', timeZone: 'UTC' })
      .format(new Date(Date.UTC(y, m - 1, d)));
  } catch {
    return String(iso);
  }
}

function picture(item, size = 640) {
  return item ? thumbUrl(item.id, size, item.thumb_v) : '';
}

function face(person) {
  return person.face ? `/api/faces/thumb/${person.face}` : '';
}

/** A photograph on a card that opens in the viewer when tapped. */
function photo(item, cls = 'year-photo', list = null) {
  const img = el('img', { class: cls, src: picture(item), alt: '', loading: 'lazy', draggable: false });
  img.addEventListener('click', (event) => {
    event.stopPropagation();
    const ids = (list || [item]).map((i) => i.id);
    pause(true);
    helpers.openItems(ids, Math.max(0, ids.indexOf(item.id))).then(() => pause(false));
  });
  return img;
}

function avatar(person) {
  const src = face(person);
  const initial = () => el('span', { class: 'year-initial', text: Array.from(person.name || '?')[0] });
  let shown = initial();
  if (src) {
    shown = el('img', { src, alt: '', loading: 'lazy' });
    shown.addEventListener('error', () => shown.replaceWith(initial()), { once: true });
  }
  return el('figure', { class: 'year-person' }, shown,
    el('figcaption', {}, el('strong', { text: person.name }),
      el('span', { text: i18n.t('{n} photos', { n: num(person.photos) }) })));
}

function card(kind, backdrop, ...children) {
  const node = el('section', { class: `year-card year-is-${kind}` });
  if (backdrop) node.style.setProperty('--year-backdrop', `url("${backdrop}")`);
  node.append(el('div', { class: 'year-body' }, ...children));
  return node;
}

function heading(text, sub) {
  return [el('h2', { class: 'year-head', text }), sub ? el('p', { class: 'year-sub', text: sub }) : null];
}

/** The plain opening, for when there is no writing model. */
function plainWords(y) {
  const parts = [y.videos
    ? i18n.t('{photos} photos and {videos} videos, on {days} days.',
      { photos: num(y.pictures), videos: num(y.videos), days: num(y.days) })
    : i18n.t('{photos} photos, on {days} days.', { photos: num(y.pictures), days: num(y.days) })];
  const top = y.people?.top || [];
  if (top.length) parts.push(i18n.t('Mostly with {names}.', { names: top.slice(0, 3).map((p) => p.name).join(', ') }));
  if (y.trip_count) parts.push(i18n.t('{n} trips, {days} days away.', { n: num(y.trip_count), days: num(y.trip_days) }));
  return parts.join(' ');
}

function buildCards(y) {
  const out = [];
  const cover = picture(y.cover, 640);
  const words = el('p', { class: 'year-words', text: plainWords(y) });
  out.push(card('intro', cover,
    el('p', { class: 'year-kicker', text: y.finished ? i18n.t('Our year') : i18n.t('Our year so far') }),
    el('h1', { class: 'year-title', text: String(y.year) }), words));

  const more = y.previous_year ? Math.round(((y.pictures - y.previous_year) / y.previous_year) * 100) : null;
  out.push(card('numbers', null,
    ...heading(i18n.t('{n} photos', { n: num(y.pictures) }),
      more === null ? '' : more >= 0
        ? i18n.t('{pct}% more than in {year}', { pct: num(more), year: y.year - 1 })
        : i18n.t('{pct}% fewer than in {year}', { pct: num(-more), year: y.year - 1 })),
    el('ul', { class: 'year-facts' },
      y.videos ? el('li', { text: i18n.t('{n} videos, {minutes} minutes in all', { n: num(y.videos), minutes: num(y.video_minutes) }) }) : null,
      y.live ? el('li', { text: i18n.t('{n} Live Photos', { n: num(y.live) }) }) : null,
      el('li', { text: i18n.t('Photos on {n} days of the year', { n: num(y.days) }) }),
      y.camera ? el('li', { text: i18n.t('Most taken with {camera}', { camera: y.camera.name }) }) : null)));

  if (y.busiest_month) {
    const peak = Math.max(...y.months, 1);
    const chart = el('div', { class: 'year-months', role: 'img',
                              'aria-label': i18n.t('Photos in each month') });
    y.months.forEach((n, i) => {
      chart.append(el('div', { class: `year-month${i + 1 === y.busiest_month ? ' peak' : ''}` },
        el('span', { class: 'year-bar', style: `--h:${Math.max(3, Math.round((n / peak) * 100))}%` }),
        el('span', { class: 'year-mlabel', text: monthName(i + 1, 'narrow') })));
    });
    const busy = y.busiest_day;
    out.push(card('months', null,
      ...heading(i18n.t('{month} was the busiest month', { month: monthName(y.busiest_month) }),
        busy ? i18n.t('The busiest day was {date}, with {n} photos and videos.', { date: day(busy.date), n: num(busy.items) }) : ''),
      chart));
  }

  const people = y.people;
  if (people?.top?.length) {
    out.push(card('people', null,
      ...heading(i18n.t('In the most photos'), ''),
      el('div', { class: 'year-people' }, ...people.top.map(avatar))));
  }
  if (people?.new?.length) {
    const first = people.new[0];
    out.push(card('new', face(first),
      ...heading(people.new.length === 1
        ? i18n.t('Welcome, {name}', { name: first.name })
        : i18n.t('New faces this year'),
        people.new.length === 1
          ? i18n.t('First seen in our photos on {date}.', { date: day(first.first) })
          : ''),
      el('div', { class: 'year-people' }, ...people.new.map(avatar))));
  }
  if (people?.together) {
    const [a, b] = people.together.people;
    out.push(card('together', null,
      ...heading(i18n.t('Always together'),
        i18n.t('{a} and {b}, in {n} photos together.', { a: a.name, b: b.name, n: num(people.together.photos) })),
      el('div', { class: 'year-people pair' }, avatar(a), avatar(b))));
  }

  const places = y.places;
  if (places?.top?.length) {
    out.push(card('places', null,
      ...heading(places.count === 1
        ? i18n.t('One place')
        : i18n.t('{n} places', { n: num(places.count) }),
        places.furthest ? i18n.t('The furthest from home: {place}, {km} km away.',
          { place: places.furthest.place, km: num(places.furthest.km) }) : ''),
      el('ol', { class: 'year-list' }, ...places.top.map((p) =>
        el('li', {}, el('span', { text: p.place }), el('span', { class: 'year-count', text: num(p.items) }))))));
  }
  if (y.trips?.length) {
    out.push(card('trips', null,
      ...heading(i18n.t('{n} trips', { n: num(y.trip_count) }),
        i18n.t('{days} days away from home.', { days: num(y.trip_days) })),
      el('div', { class: 'year-trips' }, ...y.trips.map((t) => el('figure', {},
        t.cover ? photo({ id: t.cover, thumb_v: '' }, 'year-trip') : null,
        el('figcaption', {}, el('strong', { text: t.place || t.title }),
          el('span', { text: i18n.t('{n} days', { n: num(t.days) }) })))))));
  }

  const loved = y.favourites?.loved;
  if (loved) {
    out.push(card('loved', picture(loved),
      ...heading(i18n.t('The family’s favourite'),
        loved.hearts > 1 ? i18n.t('Favourited by {n} of you.', { n: num(loved.hearts) }) : ''),
      photo(loved, 'year-photo big'),
      y.favourites.mine ? el('p', { class: 'year-sub', text: i18n.t('You favourited {n} from this year.', { n: num(y.favourites.mine) }) }) : null));
  }
  const stories = y.stories;
  if (stories?.count) {
    const ex = stories.example;
    out.push(card('stories', null,
      ...heading(i18n.t('{n} stories told', { n: num(stories.count) }),
        stories.speakers.length ? i18n.t('By {names}.', { names: stories.speakers.join(', ') }) : ''),
      ex ? photo({ id: ex.id, thumb_v: '' }, 'year-photo') : null,
      ex?.text ? el('blockquote', { class: 'year-quote' }, el('p', { text: ex.text }),
        ex.speaker ? el('cite', { text: ex.speaker }) : null) : null));
  }
  if (y.month_photos?.length) {
    out.push(card('every-month', null,
      ...heading(i18n.t('A photo for every month'), ''),
      el('div', { class: 'year-grid' }, ...y.month_photos.map((m) => el('figure', {},
        photo(m, 'year-thumb', y.month_photos), el('figcaption', { text: monthName(m.month, 'short') }))))));
  }

  const actions = el('div', { class: 'year-actions' });
  const from = { kind: 'year', id: y.year, name: String(y.year) };
  const book = el('button', { type: 'button', class: 'btn primary', text: i18n.t('Make a book of this year') });
  book.onclick = (event) => { event.stopPropagation(); close(); openBookSheet(from, { toast: helpers.toast }); };
  actions.append(book);
  filmsAvailable().then((on) => {
    if (!on) return;
    const film = el('button', { type: 'button', class: 'btn', text: i18n.t('Make a film of this year') });
    film.onclick = (event) => { event.stopPropagation(); close(); openFilmSheet(from, { toast: helpers.toast }); };
    actions.append(film);
  });
  const again = el('button', { type: 'button', class: 'btn ghost', text: i18n.t('See it again') });
  again.onclick = (event) => { event.stopPropagation(); show(0); };
  actions.append(again);
  out.push(card('end', cover,
    ...heading(i18n.t('That was {year}', { year: y.year }), i18n.t('Made on this computer, from our own photos.')),
    actions));
  return { cards: out, words };
}

function build() {
  if (root) return root;
  root = el('div', { class: 'our-year', id: 'our-year', hidden: true, role: 'dialog', 'aria-modal': 'true',
                     'aria-label': i18n.t('Our year'), tabindex: '-1' });
  bars = el('div', { class: 'year-bars' });
  picker = el('select', { class: 'year-picker', 'aria-label': i18n.t('Year') });
  picker.addEventListener('click', (event) => event.stopPropagation());
  picker.onchange = () => load(Number(picker.value));
  const closer = el('button', { type: 'button', class: 'icon-btn year-close', 'aria-label': i18n.t('Close') });
  closer.innerHTML = '<svg viewBox="0 0 24 24"><path d="M18 6 6 18M6 6l12 12"/></svg>';
  closer.onclick = (event) => { event.stopPropagation(); close(); };
  deck = el('div', { class: 'year-deck', 'aria-live': 'polite' });
  root.append(deck, el('div', { class: 'year-top' }, bars, el('div', { class: 'year-tools' }, picker, closer)));
  root.addEventListener('click', (event) => {
    if (event.target.closest('button, a, select, img.year-photo, img.year-thumb, img.year-trip')) return;
    const box = root.getBoundingClientRect();
    show(event.clientX - box.left < box.width / 3 ? at - 1 : at + 1);
  });
  root.addEventListener('pointerdown', (event) => { if (!event.target.closest('button, select')) pause(true); });
  root.addEventListener('pointerup', () => pause(false));
  root.addEventListener('pointercancel', () => pause(false));
  root.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') close();
    else if (event.key === 'ArrowRight') show(at + 1);
    else if (event.key === 'ArrowLeft') show(at - 1);
    else if (event.key === ' ') { event.preventDefault(); pause(!held); }
  });
  document.body.appendChild(root);
  return root;
}

function show(index) {
  if (!cards.length) return;
  at = Math.max(0, Math.min(index, cards.length - 1));
  cards.forEach((c, i) => c.classList.toggle('shown', i === at));
  [...bars.children].forEach((bar, i) => {
    bar.classList.toggle('done', i < at);
    bar.classList.toggle('now', i === at);
  });
  started = Date.now();
  schedule(CARD_MS);
}

function schedule(ms) {
  clearTimeout(timer);
  timer = null;
  if (held || at >= cards.length - 1) return;
  timer = setTimeout(() => show(at + 1), ms);
}

function pause(on) {
  if (!root || root.hidden || on === held) return;
  held = on;
  root.classList.toggle('held', on);
  if (on) {
    clearTimeout(timer);
    elapsed = Date.now() - started;
  } else {
    started = Date.now() - elapsed;
    schedule(Math.max(1500, CARD_MS - elapsed));
  }
}

async function load(year) {
  deck.replaceChildren(el('p', { class: 'year-loading', text: i18n.t('Looking through the year…') }));
  bars.replaceChildren();
  cards = [];
  try {
    found = await call(`/api/our-year/${year}`);
  } catch (error) {
    deck.replaceChildren(el('p', { class: 'year-loading', text: error.message }));
    return;
  }
  const made = buildCards(found);
  cards = made.cards;
  deck.replaceChildren(...cards);
  bars.replaceChildren(...cards.map(() => el('span', { class: 'year-seg' }, el('i'))));
  root.style.setProperty('--year-card-ms', `${CARD_MS}ms`);
  show(0);
  if (root.dataset.writer === '1') {
    const asked = found.year;
    call(`/api/our-year/${asked}/words`, { method: 'POST',
      body: JSON.stringify({ language: i18n.language() === 'ta' ? 'ta' : 'en' }) })
      .then((said) => {
        if (said?.written && found?.year === asked) {
          made.words.textContent = said.written;
          made.words.classList.add('written');
          made.words.title = i18n.t('Written by Ninaivu on this computer');
        }
      })
      .catch(() => { /* the page's own words stay */ });
  }
}

/** Open Our Year on the newest year there is enough to tell, or *year*. */
export async function openOurYear({ toast, openItems, year } = {}) {
  helpers = { toast: toast || helpers.toast, openItems: openItems || helpers.openItems };
  build();
  let listed;
  try {
    listed = await call('/api/our-year');
  } catch (error) {
    helpers.toast(error.message);
    return;
  }
  if (!listed.years.length) {
    helpers.toast(i18n.t('There are not enough photos in any year yet.'));
    return;
  }
  root.dataset.writer = listed.writer ? '1' : '';
  picker.replaceChildren(...listed.years.map((y) => new Option(String(y.year), String(y.year))));
  // The year just finished, on the first weeks of a new one; else the newest.
  const now = new Date();
  const fallback = listed.years.find((y) => now.getMonth() === 0 && y.year === now.getFullYear() - 1)
    || listed.years[0];
  const chosen = listed.years.find((y) => y.year === year) || fallback;
  picker.value = String(chosen.year);
  root.hidden = false;
  document.body.style.overflow = 'hidden';
  held = false;
  root.classList.remove('held');
  root.focus();
  await load(chosen.year);
}

export function closeOurYear() {
  close();
}

function close() {
  if (!root || root.hidden) return;
  clearTimeout(timer);
  timer = null;
  root.hidden = true;
  document.body.style.overflow = '';
  deck.replaceChildren();
  cards = [];
}
