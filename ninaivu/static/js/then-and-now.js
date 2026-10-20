/**
 * Then and now: the same place and the same person, years apart, with a
 * slider between the two (ninaivu/media/then_and_now.py finds the pairs,
 * ninaivu/api/api_then_and_now.py serves them; shown only while
 * `then_and_now` is switched on).
 *
 * Full screen over the gallery, like Our Year: the pairs as cards under three
 * headings (places, people, parent and child at the same age), and a pair
 * opened large with the newer photograph drawn over the older one up to a
 * handle that is dragged across (or the arrow keys), or the two side by side.
 * A face is held by the eyes in both, so a child and the grown man sit in the
 * same place and only the years change. Chosen pairs become a memory film
 * (static/js/films.js) when films are on. The family says the years people
 * were born here, which the same-age pairs need.
 *
 * `showThenNowStrip` fills the strip above the gallery with the pairs whose
 * older photograph was taken this week, years ago.
 */

import { thumbUrl } from './api.js';
import * as i18n from './i18n.js';
import { showFilmProgress } from './films.js';

let root = null;
let body = null;
let tabs = null;
let footer = null;
let found = null;
let tab = 'places';
let helpers = { toast: () => {}, openItems: () => Promise.resolve() };
const chosen = new Map();
let compare = null;

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

function yearOf(side) {
  return String(side?.date || '').slice(0, 4);
}

function keyOf(pair) {
  return `${pair.then.id}-${pair.now.id}`;
}

/** What a pair is of, in a few words: the place, the person, or both people. */
function caption(pair) {
  if (pair.kind === 'same_age') {
    return i18n.t('{parent} and {child} at {age}',
      { parent: pair.parent.name, child: pair.child.name, age: num(pair.age) });
  }
  if (pair.kind === 'person') return pair.person.name;
  return pair.place || (pair.kind === 'view' ? i18n.t('The same view') : i18n.t('The same place'));
}

function span(pair) {
  return i18n.t('{then} and {now}, {n} years apart',
    { then: yearOf(pair.then), now: yearOf(pair.now), n: num(pair.years) });
}

/** Whose face each side of a pair is held by, for a film. */
function faces(pair) {
  if (pair.kind === 'person') return [pair.person.id, pair.person.id];
  if (pair.kind === 'same_age') return [pair.parent.id, pair.child.id];
  return [null, null];
}

/* -- a pair as a small card ----------------------------------------------- */

function thumbs(pair, size = 640) {
  return el('div', { class: 'tn-thumbs' },
    el('img', { src: thumbUrl(pair.then.id, size, pair.then.thumb_v), alt: '', loading: 'lazy', draggable: false }),
    el('img', { src: thumbUrl(pair.now.id, size, pair.now.thumb_v), alt: '', loading: 'lazy', draggable: false }),
    el('span', { class: 'tn-year then', text: yearOf(pair.then) }),
    el('span', { class: 'tn-year now', text: yearOf(pair.now) }));
}

function pairCard(pair, list, index) {
  const open = el('button', { type: 'button', class: 'tn-card-open', 'aria-label': `${caption(pair)}: ${span(pair)}` },
    thumbs(pair),
    el('span', { class: 'tn-card-text' },
      el('strong', { text: caption(pair) }), el('span', { text: span(pair) })));
  open.onclick = () => openCompare(list, index);
  const card = el('article', { class: 'tn-card' }, open);
  if (found?.films) {
    const box = el('input', { type: 'checkbox', checked: chosen.has(keyOf(pair)),
                              'aria-label': i18n.t('Put in the film') });
    box.onchange = () => {
      if (box.checked) chosen.set(keyOf(pair), pair);
      else chosen.delete(keyOf(pair));
      syncFooter();
    };
    card.append(el('label', { class: 'tn-pick', title: i18n.t('Put in the film') }, box));
  }
  return card;
}

/* -- the three headings ---------------------------------------------------- */

function lists() {
  return {
    places: found.places || [],
    people: (found.people || []).flatMap((p) => p.pairs),
    same_age: (found.same_age || []).flatMap((f) => f.pairs),
  };
}

function showTab(name) {
  tab = name;
  [...tabs.children].forEach((b) => b.setAttribute('aria-selected', String(b.dataset.tab === name)));
  const all = lists();
  const grid = el('div', { class: 'tn-grid' });
  if (name === 'people') {
    const groups = el('div', { class: 'tn-groups' });
    let at = 0;
    for (const person of found.people || []) {
      const start = at;
      groups.append(el('section', { class: 'tn-group' },
        el('h3', { text: person.person.name }),
        el('div', { class: 'tn-grid' }, ...person.pairs.map((p, i) => pairCard(p, all.people, start + i)))));
      at += person.pairs.length;
    }
    body.replaceChildren(all.people.length ? groups
      : el('p', { class: 'tn-empty', text: i18n.t('No one has photos far enough apart yet. Name people on the People page.') }));
    return;
  }
  if (name === 'same_age') {
    const parts = [];
    if (found.ask_born?.length) parts.push(bornForm(found.ask_born));
    for (const family of found.same_age || []) {
      const start = all.same_age.indexOf(family.pairs[0]);
      parts.push(el('section', { class: 'tn-group' },
        el('h3', { text: i18n.t('{parent} and {child}', { parent: family.parent.name, child: family.child.name }) }),
        el('div', { class: 'tn-grid' }, ...family.pairs.map((p, i) => pairCard(p, all.same_age, start + i)))));
    }
    if (!parts.length) {
      parts.push(el('p', { class: 'tn-empty',
        text: i18n.t('Add parents and children to the family tree to see them at the same age.') }));
    }
    body.replaceChildren(el('div', { class: 'tn-groups' }, ...parts));
    return;
  }
  grid.append(...all.places.map((p, i) => pairCard(p, all.places, i)));
  body.replaceChildren(all.places.length ? grid
    : el('p', { class: 'tn-empty', text: i18n.t('No place has photos far enough apart yet.') }));
}

/** The years people were born, which "at the same age" needs. */
function bornForm(people) {
  const form = el('form', { class: 'tn-born' },
    el('h3', { text: i18n.t('When were they born?') }),
    el('p', { class: 'tn-hint', text: i18n.t('The year is enough. It is kept on this computer and used only to match ages.') }));
  const now = new Date().getFullYear();
  const inputs = people.map((person) => {
    const input = el('input', { type: 'number', min: 1850, max: now, inputMode: 'numeric',
                                placeholder: i18n.t('Year'), 'aria-label': i18n.t('The year {name} was born', { name: person.name }) });
    input.dataset.person = person.id;
    form.append(el('label', { class: 'tn-born-row' },
      person.face ? el('img', { src: `/api/faces/thumb/${person.face}`, alt: '', loading: 'lazy' }) : null,
      el('span', { text: person.name }), input));
    return input;
  });
  const save = el('button', { type: 'submit', class: 'btn primary small', text: i18n.t('Save') });
  form.append(el('div', { class: 'tn-born-actions' }, save));
  form.onsubmit = async (event) => {
    event.preventDefault();
    const given = inputs.filter((i) => i.value.trim());
    if (!given.length) return;
    save.disabled = true;
    try {
      for (const input of given) {
        await call(`/api/then-and-now/born/${input.dataset.person}`, { method: 'PUT',
          body: JSON.stringify({ year: Number(input.value) }) });
      }
      await load('same_age');
    } catch (error) {
      helpers.toast(error.message, true);
      save.disabled = false;
    }
  };
  return form;
}

function syncFooter() {
  if (!footer) return;
  footer.hidden = !found?.films;
  const make = footer.querySelector('button');
  const n = chosen.size;
  make.disabled = !n;
  make.textContent = n ? i18n.t('Make a film of {n} pairs', { n: num(n) }) : i18n.t('Choose pairs for a film');
}

async function makeFilm() {
  const pairs = [...chosen.values()].map((pair) => {
    const [a, b] = faces(pair);
    return { then: { id: pair.then.id, person: a }, now: { id: pair.now.id, person: b } };
  });
  const make = footer.querySelector('button');
  make.disabled = true;
  try {
    const film = await call('/api/then-and-now/film', { method: 'POST',
      body: JSON.stringify({ pairs, title: i18n.t('Then and now') }) });
    chosen.clear();
    close();
    showFilmProgress(film, { toast: helpers.toast });
  } catch (error) {
    helpers.toast(i18n.t('Could not make the film: {reason}', { reason: error.message }), true);
    syncFooter();
  }
}

/* -- one pair, large, with the slider -------------------------------------- */

//: Where the eyes go in a pair of faces: their middle, as a share of the
//: stage, and how far apart they are, as a share of its shorter side.
const EYES_AT = [0.5, 0.4];
const EYES_APART = 0.16;

/** Place *img* so its eyes sit at EYES_AT, EYES_APART apart (over a
 *  blurred copy of itself where it does not reach), or whole. */
function place(img, eyes, stage) {
  const fit = () => {
    const W = stage.clientWidth;
    const H = stage.clientHeight;
    const aspect = img.naturalWidth / img.naturalHeight || 1;
    if (!eyes || !W || !H) {
      img.removeAttribute('style');
      img.classList.remove('held');
      return;
    }
    const [[ax, ay], [bx, by]] = eyes;
    const apart = Math.hypot(bx - ax, (by - ay) / aspect) || 0.1;
    // A small face in a big photograph is not blown up past four times the stage.
    const width = Math.min(4 * W, (EYES_APART * Math.min(W, H)) / apart);
    const height = width / aspect;
    img.classList.add('held');
    img.style.width = `${width}px`;
    img.style.height = `${height}px`;
    img.style.left = `${EYES_AT[0] * W - ((ax + bx) / 2) * width}px`;
    img.style.top = `${EYES_AT[1] * H - ((ay + by) / 2) * height}px`;
  };
  if (img.complete && img.naturalWidth) fit();
  else img.addEventListener('load', fit, { once: true });
  return fit;
}

function openCompare(list, index) {
  closeCompare();
  let at = index;
  let split = 50;
  let sideBySide = false;
  const stage = el('div', { class: 'tn-stage' });
  const handle = el('input', { type: 'range', class: 'tn-handle', min: 0, max: 100, value: split,
                               'aria-label': i18n.t('Slide between then and now') });
  const title = el('h2', { class: 'tn-compare-title' });
  const sub = el('p', { class: 'tn-compare-sub' });
  const toggle = el('button', { type: 'button', class: 'btn ghost small' });
  const prev = el('button', { type: 'button', class: 'icon-btn tn-step', 'aria-label': i18n.t('Previous') });
  prev.innerHTML = '<svg viewBox="0 0 24 24"><path d="m15 18-6-6 6-6"/></svg>';
  const next = el('button', { type: 'button', class: 'icon-btn tn-step', 'aria-label': i18n.t('Next') });
  next.innerHTML = '<svg viewBox="0 0 24 24"><path d="m9 18 6-6-6-6"/></svg>';
  const back = el('button', { type: 'button', class: 'icon-btn tn-close', 'aria-label': i18n.t('Back') });
  back.innerHTML = '<svg viewBox="0 0 24 24"><path d="M18 6 6 18M6 6l12 12"/></svg>';
  const openThen = el('button', { type: 'button', class: 'btn ghost small' });
  const openNow = el('button', { type: 'button', class: 'btn ghost small' });
  let fits = [];

  const draw = () => {
    const pair = list[at];
    title.textContent = caption(pair);
    sub.textContent = span(pair);
    openThen.textContent = i18n.t('Open {year}', { year: yearOf(pair.then) });
    openNow.textContent = i18n.t('Open {year}', { year: yearOf(pair.now) });
    toggle.textContent = sideBySide ? i18n.t('Slider') : i18n.t('Side by side');
    const then = el('img', { class: 'tn-then', src: thumbUrl(pair.then.id, 640, pair.then.thumb_v), alt: yearOf(pair.then), draggable: false });
    const now = el('img', { class: 'tn-now', src: thumbUrl(pair.now.id, 640, pair.now.thumb_v), alt: yearOf(pair.now), draggable: false });
    const behind = (side) => (side.eyes
      ? el('img', { class: 'tn-behind', src: thumbUrl(side.id, 256, side.thumb_v), alt: '', draggable: false }) : null);
    const thenBox = el('div', { class: 'tn-layer then' }, behind(pair.then), then,
      el('span', { class: 'tn-year then', text: yearOf(pair.then) }));
    const nowBox = el('div', { class: 'tn-layer now' }, behind(pair.now), now,
      el('span', { class: 'tn-year now', text: yearOf(pair.now) }));
    stage.classList.toggle('side', sideBySide);
    stage.replaceChildren(thenBox, nowBox, sideBySide ? null : el('span', { class: 'tn-line' }));
    fits = [place(then, pair.then.eyes, thenBox), place(now, pair.now.eyes, nowBox)];
    stage.style.setProperty('--split', `${split}%`);
    handle.hidden = sideBySide;
    prev.disabled = at <= 0;
    next.disabled = at >= list.length - 1;
  };
  const move = (value) => {
    split = Math.max(0, Math.min(100, value));
    handle.value = String(split);
    stage.style.setProperty('--split', `${split}%`);
  };
  handle.oninput = () => move(Number(handle.value));
  const fromPointer = (event) => {
    const box = stage.getBoundingClientRect();
    move(((event.clientX - box.left) / box.width) * 100);
  };
  stage.addEventListener('pointerdown', (event) => {
    if (sideBySide) return;
    stage.setPointerCapture(event.pointerId);
    fromPointer(event);
  });
  stage.addEventListener('pointermove', (event) => {
    if (!sideBySide && stage.hasPointerCapture(event.pointerId)) fromPointer(event);
  });
  toggle.onclick = () => { sideBySide = !sideBySide; draw(); };
  prev.onclick = () => { if (at > 0) { at -= 1; draw(); } };
  next.onclick = () => { if (at < list.length - 1) { at += 1; draw(); } };
  back.onclick = closeCompare;
  const ids = () => [list[at].then.id, list[at].now.id];
  openThen.onclick = () => helpers.openItems(ids(), 0);
  openNow.onclick = () => helpers.openItems(ids(), 1);

  compare = el('div', { class: 'tn-compare', role: 'dialog', 'aria-modal': 'true',
                        'aria-label': i18n.t('Then and now'), tabindex: '-1' },
    el('div', { class: 'tn-compare-top' }, el('div', {}, title, sub), back),
    el('div', { class: 'tn-compare-main' }, prev, stage, next),
    handle,
    el('div', { class: 'tn-compare-tools' }, openThen, toggle, openNow));
  compare.addEventListener('keydown', (event) => {
    if (event.target === handle && (event.key === 'ArrowLeft' || event.key === 'ArrowRight')) return;
    if (event.key === 'Escape') { event.stopPropagation(); closeCompare(); }
    else if (event.key === 'ArrowLeft') move(split - 5);
    else if (event.key === 'ArrowRight') move(split + 5);
    else if (event.key === 'PageUp') prev.click();
    else if (event.key === 'PageDown') next.click();
  });
  compare.resize = () => fits.forEach((fit) => fit());
  window.addEventListener('resize', compare.resize);
  (root && !root.hidden ? root : document.body).appendChild(compare);
  draw();
  compare.focus();
}

function closeCompare() {
  if (!compare) return;
  window.removeEventListener('resize', compare.resize);
  compare.remove();
  compare = null;
  root?.focus();
}

/* -- the screen -------------------------------------------------------------- */

function build() {
  if (root) return root;
  root = el('div', { class: 'then-now', id: 'then-now', hidden: true, role: 'dialog', 'aria-modal': 'true',
                     'aria-label': i18n.t('Then and now'), tabindex: '-1' });
  const closer = el('button', { type: 'button', class: 'icon-btn tn-close', 'aria-label': i18n.t('Close') });
  closer.innerHTML = '<svg viewBox="0 0 24 24"><path d="M18 6 6 18M6 6l12 12"/></svg>';
  closer.onclick = close;
  tabs = el('div', { class: 'tn-tabs', role: 'tablist' });
  for (const [name, label] of [['places', i18n.t('Places')], ['people', i18n.t('People')],
    ['same_age', i18n.t('At the same age')]]) {
    const b = el('button', { type: 'button', role: 'tab', class: 'tn-tab', text: label });
    b.dataset.tab = name;
    b.onclick = () => showTab(name);
    tabs.append(b);
  }
  body = el('div', { class: 'tn-body' });
  const make = el('button', { type: 'button', class: 'btn primary' });
  make.onclick = makeFilm;
  footer = el('div', { class: 'tn-footer', hidden: true }, make);
  root.append(
    el('header', { class: 'tn-head' },
      el('div', {}, el('h1', { text: i18n.t('Then and now') }),
        el('p', { class: 'tn-hint', text: i18n.t('The same places and faces, years apart. Found on this computer.') })),
      closer),
    tabs, body, footer);
  root.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !compare) close();
  });
  document.body.appendChild(root);
  return root;
}

async function load(prefer) {
  body.replaceChildren(el('p', { class: 'tn-empty', text: i18n.t('Looking through the years…') }));
  try {
    found = await call('/api/then-and-now');
  } catch (error) {
    body.replaceChildren(el('p', { class: 'tn-empty', text: error.message }));
    return;
  }
  const all = lists();
  const people = found.people?.length || found.same_age?.length || found.ask_born?.length;
  tabs.querySelector('[data-tab="people"]').hidden = !found.people?.length;
  tabs.querySelector('[data-tab="same_age"]').hidden = !(found.same_age?.length || found.ask_born?.length);
  tabs.hidden = !people;
  const first = prefer || (all.places.length ? 'places' : all.people.length ? 'people'
    : all.same_age.length || found.ask_born?.length ? 'same_age' : 'places');
  showTab(first);
  syncFooter();
}

/** Open Then and now, on *pair* (from the strip) when given. */
export async function openThenAndNow({ toast, openItems, pair } = {}) {
  helpers = { toast: toast || helpers.toast, openItems: openItems || helpers.openItems };
  build();
  root.hidden = false;
  document.body.style.overflow = 'hidden';
  root.focus();
  await load();
  if (pair) {
    const all = [...lists().places, ...lists().people, ...lists().same_age];
    const index = all.findIndex((p) => keyOf(p) === keyOf(pair));
    if (index >= 0) openCompare(all, index);
  }
}

function close() {
  if (!root || root.hidden) return;
  closeCompare();
  root.hidden = true;
  document.body.style.overflow = '';
  body.replaceChildren();
}

/* -- the strip above the gallery --------------------------------------------- */

/** Fill #then-now-banner with this week's pairs, years ago; false when none. */
export async function showThenNowStrip({ toast, openItems } = {}) {
  const banner = document.getElementById('then-now-banner');
  const carousel = document.getElementById('then-now-carousel');
  if (!banner || !carousel) return false;
  let data;
  try {
    data = await call('/api/then-and-now/today');
  } catch {
    banner.hidden = true;
    return false;
  }
  const pairs = data?.pairs || [];
  carousel.replaceChildren(...pairs.map((pair) => {
    const card = el('button', { type: 'button', class: 'memory-card tn-strip-card',
                                'aria-label': `${caption(pair)}: ${span(pair)}` },
      thumbs(pair, 256),
      el('span', { class: 'memory-badge', text: i18n.t('{n} years apart', { n: num(pair.years) }) }));
    card.onclick = () => openThenAndNow({ toast, openItems, pair });
    return card;
  }));
  banner.hidden = !pairs.length;
  return pairs.length > 0;
}

export function hideThenNowStrip() {
  const banner = document.getElementById('then-now-banner');
  if (banner) banner.hidden = true;
}
