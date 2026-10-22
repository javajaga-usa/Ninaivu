/**
 * A life in photographs: one person's life told decade by decade
 * (ninaivu/media/life_story.py tells it, ninaivu/api/api_life_story.py serves
 * it; shown only while `life_story` is switched on, and only to the family).
 *
 * Full screen over the gallery, like Then and now, and read like a book:
 * the person's best photograph across the top with their name, the years and
 * a few opening words, then a chapter for each decade with its own title and
 * sentences, the best photographs of those years as a mosaic, who joined the
 * person's photographs then (a husband, a daughter), who was beside them,
 * where they were, the trips, and the stories the family told, played where
 * they were recorded. A rail of the decades follows the reader down the page.
 *
 * The words are the on-device writing model's when it is there: the page
 * asks for them as soon as it opens, shows its own plain sentences from the
 * same facts while they are written, and swaps them in when they come. What
 * the model wrote is kept on the server, so the next visit opens with it.
 * Without the model the plain sentences are the story.
 *
 * It ends with a book and a film of the person (static/js/books.js,
 * static/js/films.js), which already make one of a person.
 */

import { thumbUrl } from './api.js';
import * as i18n from './i18n.js';
import { openBookSheet } from './books.js';
import { filmsAvailable, openFilmSheet } from './films.js';

let root = null;
let body = null;
let rail = null;
let titleEl = null;
let back = null;
let found = null;
let written = null;
let watcher = null;
let asking = 0;
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

function day(iso) {
  const [y, m, d] = String(iso || '').split('-').map(Number);
  if (!y) return '';
  if (!m || !d) return String(y);
  try {
    return new Intl.DateTimeFormat(i18n.locale(), { day: 'numeric', month: 'long', year: 'numeric', timeZone: 'UTC' })
      .format(new Date(Date.UTC(y, m - 1, d)));
  } catch {
    return String(iso);
  }
}

function list(names) {
  try {
    return new Intl.ListFormat(i18n.locale(), { style: 'long', type: 'conjunction' }).format(names);
  } catch {
    return names.join(', ');
  }
}

function language() {
  return i18n.language() === 'ta' ? 'ta' : 'en';
}

function faceUrl(id) {
  return id ? `/api/faces/thumb/${id}` : '';
}

function avatar(person, note) {
  const initial = () => el('span', { class: 'ls-initial', text: Array.from(person.name || '?')[0] });
  let shown = initial();
  if (person.face) {
    shown = el('img', { src: faceUrl(person.face), alt: '', loading: 'lazy', draggable: false });
    shown.addEventListener('error', () => shown.replaceWith(initial()), { once: true });
  }
  return el('figure', { class: 'ls-person' }, shown,
    el('figcaption', {}, el('strong', { text: person.name }), note ? el('span', { text: note }) : null));
}

const RELATION = {
  spouse: i18n.key('husband or wife'),
  parent: i18n.key('parent'),
  child: i18n.key('son or daughter'),
  grandparent: i18n.key('grandparent'),
  grandchild: i18n.key('grandchild'),
  sibling: i18n.key('brother or sister'),
};

function relation(person) {
  return RELATION[person.relation] ? i18n.t(RELATION[person.relation]) : '';
}

function span(c) {
  return c.first_year === c.last_year ? String(c.first_year) : `${c.first_year} – ${c.last_year}`;
}

function decade(c) {
  const name = (year) => i18n.t('{year}s', { year });
  return c.end - c.start > 9 ? `${name(c.start)} – ${name(c.end - 9)}` : name(c.start);
}

/* -- the page's own words, for when there is no writing model -------------- */

function plainTitle(c) {
  const spouse = c.new.find((p) => p.relation === 'spouse');
  const place = c.places[0]?.place?.split(',')[0];
  if (spouse && place) return i18n.t('{place}, and {name}', { place, name: spouse.name });
  if (spouse) return i18n.t('With {name}', { name: spouse.name });
  const born = c.born[0];
  if (born) return i18n.t('{name} arrives', { name: born.name });
  if (c.places.length > 1) {
    return list(c.places.slice(0, 2).map((p) => p.place.split(',')[0]));
  }
  if (place) return place;
  return decade(c);
}

function plainText(c) {
  const parts = [i18n.t('{n} photos from {span}.', { n: num(c.items), span: span(c) })];
  if (c.ages) parts.push(i18n.t('Aged {from} to {to}.', { from: num(c.ages[0]), to: num(c.ages[1]) }));
  for (const p of c.new.slice(0, 3)) {
    parts.push(i18n.t('{name} first appears beside them in {year}.', { name: p.name, year: p.first.slice(0, 4) }));
  }
  for (const p of c.born.slice(0, 3)) {
    parts.push(i18n.t('{name} was born in {year}.', { name: p.name, year: p.year }));
  }
  const often = c.with.filter((p) => !c.new.some((n) => n.id === p.id)).slice(0, 3);
  if (often.length) parts.push(i18n.t('Most often with {names}.', { names: list(often.map((p) => p.name)) }));
  if (c.trips.length) {
    const longest = [...c.trips].sort((a, b) => b.days - a.days)[0];
    const where = longest.place || longest.title;
    parts.push(where
      ? i18n.t('Away from home {n} times, the longest to {place}.', { n: num(c.trips.length), place: where })
      : i18n.t('Away from home {n} times.', { n: num(c.trips.length) }));
  }
  return parts.join(' ');
}

function plainOpening(life) {
  const p = life.person;
  const decades = life.chapters.length;
  return i18n.t('{n} photos of {name}, from {first} to {last}, told in {chapters} chapters.',
    { n: num(p.photos), name: p.name, first: p.first_year, last: p.last_year, chapters: num(decades) });
}

/* -- the page --------------------------------------------------------------- */

function photo(item, list, cls) {
  const img = el('img', { src: thumbUrl(item.id, cls === 'ls-hero-img' ? 1280 : 512, item.thumb_v), alt: '',
                          loading: 'lazy', draggable: false });
  const button = el('button', { type: 'button', class: cls || 'ls-tile',
                                'aria-label': i18n.t('Open the photo from {date}', { date: day(item.date) }) }, img);
  if (item.width && item.height && cls !== 'ls-hero-img') {
    button.style.setProperty('--ar', `${item.width} / ${item.height}`);
  }
  button.onclick = () => openPhotos(list.map((i) => i.id), item.id);
  return button;
}

/** The viewer, in place of the story until it is closed: it sits below
 *  full-screen pages, and Escape there must close it, not the story. */
async function openPhotos(ids, id) {
  const top = body.scrollTop;
  root.hidden = true;
  root.querySelectorAll('audio').forEach((a) => a.pause());
  try {
    await helpers.openItems(ids, Math.max(0, ids.indexOf(id)));
  } finally {
    if (found) {
      root.hidden = false;
      body.scrollTop = top;
      root.focus({ preventScroll: true });
    }
  }
}

function story(s) {
  const audio = el('audio', { preload: 'none', src: `/api/stories/${s.id}/audio` });
  const play = el('button', { type: 'button', class: 'ls-play', 'aria-label': i18n.t('Play the story') });
  play.innerHTML = '<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>';
  play.onclick = () => {
    if (audio.paused) {
      root.querySelectorAll('.ls-story audio').forEach((a) => { if (a !== audio) a.pause(); });
      audio.play().catch(() => helpers.toast(i18n.t('This story could not be played.')));
    } else {
      audio.pause();
    }
  };
  audio.addEventListener('play', () => play.classList.add('playing'));
  audio.addEventListener('pause', () => play.classList.remove('playing'));
  const said = s.text || s.english;
  return el('figure', { class: 'ls-story' }, play,
    el('blockquote', {}, said ? el('p', { text: said }) : el('p', { class: 'ls-quiet', text: i18n.t('A recorded story') }),
      s.english && s.text && s.english !== s.text ? el('p', { class: 'ls-english', text: s.english }) : null),
    s.speaker ? el('figcaption', { text: i18n.t('Told by {name}', { name: s.speaker }) }) : null,
    audio);
}

function chapter(c, index) {
  const words = written?.chapters?.[index];
  const pics = [...c.photos, ...c.story_photos];
  const facts = [i18n.t('{n} photos', { n: num(c.items) })];
  if (c.ages) facts.unshift(i18n.t('aged {from} to {to}', { from: num(c.ages[0]), to: num(c.ages[1]) }));
  const head = el('header', { class: 'ls-chapter-head' },
    el('p', { class: 'ls-decade', text: decade(c) }),
    el('h2', { class: 'ls-title', text: words?.title || plainTitle(c) }),
    el('p', { class: 'ls-meta', text: [span(c), ...facts].join(' · ') }));
  const text = el('p', { class: `ls-text${words ? ' ls-written' : ''}`, text: words?.text || plainText(c) });
  const mosaic = el('div', { class: `ls-mosaic ls-n${Math.min(c.photos.length, 9)}` },
    ...c.photos.map((p) => photo(p, pics)));
  const aside = [];
  if (c.new.length || c.born.length) {
    aside.push(el('section', { class: 'ls-block' },
      el('h3', { text: i18n.t('Joined their photographs') }),
      el('div', { class: 'ls-people' },
        ...c.new.map((p) => avatar(p, [relation(p), day(p.first)].filter(Boolean).join(' · '))),
        ...c.born.filter((b) => !c.new.some((p) => p.id === b.id))
          .map((b) => avatar(b, i18n.t('born {year}', { year: b.year }))))));
  }
  const often = c.with.filter((p) => !c.new.some((n) => n.id === p.id));
  if (often.length) {
    aside.push(el('section', { class: 'ls-block' },
      el('h3', { text: i18n.t('Often beside them') }),
      el('div', { class: 'ls-people' },
        ...often.map((p) => avatar(p, [relation(p), i18n.t('{n} photos', { n: num(p.photos) })].filter(Boolean).join(' · '))))));
  }
  if (c.places.length || c.trips.length) {
    aside.push(el('section', { class: 'ls-block' },
      el('h3', { text: i18n.t('Where they were') }),
      el('ul', { class: 'ls-chips' },
        ...c.places.map((p) => el('li', { text: p.place })),
        ...c.trips.map((t) => el('li', { class: 'ls-trip',
          text: i18n.t('{place}, {days} days', { place: t.place || t.title, days: num(t.days) }) })))));
  }
  const stories = c.stories.length ? el('section', { class: 'ls-stories' },
    el('h3', { text: c.stories_count > c.stories.length
      ? i18n.t('Stories told ({n})', { n: num(c.stories_count) }) : i18n.t('Stories told') }),
    ...c.stories.map(story)) : null;
  const node = el('article', { class: 'ls-chapter', id: `ls-ch-${index}`, 'data-index': String(index) },
    head, text, mosaic, stories, aside.length ? el('div', { class: 'ls-aside' }, ...aside) : null);
  return node;
}

function hero(life) {
  const p = life.person;
  const cover = life.cover || life.first;
  const years = p.first_year === p.last_year ? String(p.first_year) : `${p.first_year} – ${p.last_year}`;
  const lines = [p.born ? i18n.t('Born {year}', { year: p.born }) : '', years,
                 i18n.t('{n} photos', { n: num(p.photos) })].filter(Boolean);
  const opening = el('p', { class: `ls-opening${written?.opening ? ' ls-written' : ''}`,
                            text: written?.opening || plainOpening(life) });
  const node = el('section', { class: 'ls-hero' },
    cover ? photo(cover, [cover], 'ls-hero-img') : null,
    el('div', { class: 'ls-hero-text' },
      el('p', { class: 'ls-kicker', text: i18n.t('A life in photographs') }),
      el('h1', { class: 'ls-name', text: p.name }),
      el('p', { class: 'ls-years', text: lines.join(' · ') }),
      opening,
      p.born ? null : bornForm(p)));
  return node;
}

function bornForm(person) {
  const input = el('input', { type: 'number', inputmode: 'numeric', min: '1850',
                              max: String(new Date().getFullYear()), placeholder: '1950',
                              'aria-label': i18n.t('Year born') });
  const save = el('button', { type: 'button', class: 'btn small', text: i18n.t('Save') });
  save.onclick = async () => {
    const year = Number(input.value);
    if (!Number.isInteger(year)) return;
    save.disabled = true;
    try {
      await call(`/api/life/${person.id}/born`, { method: 'PUT', body: JSON.stringify({ year }) });
      await show(person.id);
    } catch (error) {
      helpers.toast(error.message, true);
      save.disabled = false;
    }
  };
  return el('div', { class: 'ls-born' },
    el('label', { text: i18n.t('When was {name} born? Each chapter then says how old they were.', { name: person.name }) }),
    el('div', { class: 'ls-born-row' }, input, save));
}

function footer(life) {
  const from = { kind: 'person', id: life.person.id, name: life.person.name };
  const book = el('button', { type: 'button', class: 'btn primary', text: i18n.t('Make a book') });
  book.onclick = () => openBookSheet(from, { toast: helpers.toast });
  const node = el('footer', { class: 'ls-end' },
    el('p', { text: i18n.t("Keep {name}'s life as a book, or watch it as a film.", { name: life.person.name }) }),
    el('div', { class: 'ls-end-actions' }, book));
  filmsAvailable().then((on) => {
    if (!on) return;
    const film = el('button', { type: 'button', class: 'btn', text: i18n.t('Make a film') });
    film.onclick = () => openFilmSheet(from, { toast: helpers.toast });
    node.querySelector('.ls-end-actions').append(film);
  });
  return node;
}

function railFor(life) {
  rail.replaceChildren(...life.chapters.map((c, index) => {
    const b = el('button', { type: 'button', class: 'ls-rail-step', text: String(c.start) });
    b.onclick = () => body.querySelector(`#ls-ch-${index}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    return b;
  }));
  rail.hidden = life.chapters.length < 2;
  watcher?.disconnect();
  watcher = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      const at = Number(entry.target.dataset.index);
      rail.querySelectorAll('.ls-rail-step').forEach((b, i) => b.classList.toggle('on', i === at));
    }
  }, { root: body, rootMargin: '-35% 0px -60% 0px' });
  body.querySelectorAll('.ls-chapter').forEach((c) => watcher.observe(c));
}

function render() {
  const life = found;
  const writing = !written && life.writer
    ? el('p', { class: 'ls-writing', role: 'status', text: i18n.t('Writing the chapters on this computer…') }) : null;
  body.replaceChildren(hero(life), writing,
    el('div', { class: 'ls-chapters' }, ...life.chapters.map(chapter)), footer(life));
  railFor(life);
}

/** Ask the writing model for the chapters' words, once per opening. */
async function write(personId) {
  const ticket = ++asking;
  try {
    const got = await call(`/api/life/${personId}/words`, { method: 'POST', body: JSON.stringify({ language: language() }) });
    if (ticket !== asking || !found || found.person.id !== personId) return;
    written = got;
    const top = body.scrollTop;
    render();
    body.scrollTop = top;
  } catch {
    if (ticket !== asking) return;
    body.querySelector('.ls-writing')?.remove();
  }
}

async function show(personId) {
  asking += 1;
  back.hidden = false;
  titleEl.textContent = i18n.t('A life in photographs');
  body.replaceChildren(el('p', { class: 'ls-empty', text: i18n.t('Turning the pages…') }));
  rail.hidden = true;
  try {
    found = await call(`/api/life/${personId}?language=${language()}`);
  } catch (error) {
    body.replaceChildren(el('p', { class: 'ls-empty', text: error.status === 404
      ? i18n.t('There are not enough dated photos of this person yet to tell their life.') : error.message }));
    return;
  }
  written = found.written;
  titleEl.textContent = found.person.name;
  render();
  body.scrollTop = 0;
  if (!written && found.writer) write(personId);
}

async function picker() {
  asking += 1;
  found = null;
  back.hidden = true;
  rail.hidden = true;
  titleEl.textContent = i18n.t('A life in photographs');
  body.replaceChildren(el('p', { class: 'ls-empty', text: i18n.t('Turning the pages…') }));
  let data;
  try {
    data = await call('/api/life');
  } catch (error) {
    body.replaceChildren(el('p', { class: 'ls-empty', text: error.message }));
    return;
  }
  if (!data.people.length) {
    body.replaceChildren(el('p', { class: 'ls-empty',
      text: i18n.t('Name the people on the People page, and their lives can be told here.') }));
    return;
  }
  body.replaceChildren(
    el('p', { class: 'ls-intro', text: i18n.t("Choose somebody, and their life is told decade by decade from the family's photos.") }),
    el('div', { class: 'ls-pick' }, ...data.people.map((p) => {
      const years = p.first && p.last && p.first !== p.last ? `${p.first} – ${p.last}` : String(p.first || '');
      const b = el('button', { type: 'button', class: 'ls-pick-card' },
        avatar(p, [years, i18n.t('{n} photos', { n: num(p.photos) })].filter(Boolean).join(' · ')));
      b.onclick = () => show(p.id);
      return b;
    })));
}

function build() {
  if (root) return root;
  root = el('div', { class: 'life-story', id: 'life-story', hidden: true, role: 'dialog', 'aria-modal': 'true',
                     'aria-label': i18n.t('A life in photographs'), tabindex: '-1' });
  const closer = el('button', { type: 'button', class: 'icon-btn ls-close', 'aria-label': i18n.t('Close') });
  closer.innerHTML = '<svg viewBox="0 0 24 24"><path d="M18 6 6 18M6 6l12 12"/></svg>';
  closer.onclick = close;
  back = el('button', { type: 'button', class: 'icon-btn ls-back', 'aria-label': i18n.t('Everybody'), hidden: true });
  back.innerHTML = '<svg viewBox="0 0 24 24"><path d="m15 18-6-6 6-6"/></svg>';
  back.onclick = picker;
  titleEl = el('p', { class: 'ls-bar-title' });
  rail = el('nav', { class: 'ls-rail', 'aria-label': i18n.t('Decades'), hidden: true });
  body = el('div', { class: 'ls-body' });
  root.append(el('header', { class: 'ls-bar' }, back, titleEl, closer), rail, body);
  root.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') close();
  });
  document.body.appendChild(root);
  return root;
}

/** Open somebody's life (*person*, an id), or the list of everybody whose
 *  life there is enough to tell. */
export async function openLifeStory({ toast, openItems, person } = {}) {
  helpers = { toast: toast || helpers.toast, openItems: openItems || helpers.openItems };
  build();
  root.hidden = false;
  document.body.style.overflow = 'hidden';
  root.focus();
  if (person) await show(Number(person));
  else await picker();
}

function close() {
  if (!root || root.hidden) return;
  asking += 1;
  root.querySelectorAll('audio').forEach((a) => a.pause());
  watcher?.disconnect();
  root.hidden = true;
  document.body.style.overflow = '';
  body.replaceChildren();
  found = null;
  written = null;
}
