/**
 * Young faces, older names: who an unnamed child in an old photograph may be
 * (ninaivu/media/young_faces.py finds them, ninaivu/api/api_young_faces.py
 * serves them; shown only while `young_faces` is switched on, and only to the
 * family).
 *
 * Full screen over the gallery, like A life in photographs. One section for
 * each person somebody may be: their name, the year of their earliest named
 * photograph, and the unnamed faces from before it, each beside that earliest
 * face so the eye can compare. Under each, why it was suggested (the year,
 * how old they would have been, a mother or brother named in the same
 * photograph) and three answers: yes, no, or ask a relative with a "Who is
 * this?" link, which shows the faces and never the name Ninaivu thought of.
 *
 * Nothing is named until somebody says yes. A no is kept, and the face is not
 * suggested as that person again.
 */

import { thumbUrl } from './api.js';
import * as i18n from './i18n.js';

const MAX_ASK = 20;

let root = null;
let body = null;
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

const faceUrl = (id) => (id ? `/api/faces/thumb/${id}` : '');

const RELATION = {
  spouse: i18n.key('husband or wife'),
  parent: i18n.key('parent'),
  grandparent: i18n.key('grandparent'),
  sibling: i18n.key('brother or sister'),
};

function face(id, cls, alt = '') {
  const img = el('img', { src: faceUrl(id), alt, loading: 'lazy', draggable: false, class: cls });
  img.addEventListener('error', () => img.classList.add('yf-missing'), { once: true });
  return img;
}

function avatar(person) {
  if (!person.face) return el('span', { class: 'yf-avatar yf-initial', text: Array.from(person.name || '?')[0] });
  return face(person.face, 'yf-avatar');
}

/** The viewer, in place of this page until it is closed (it sits below
 *  full-screen pages, and Escape there must close it, not this). */
async function openPhoto(id) {
  const top = body.scrollTop;
  root.hidden = true;
  try {
    await helpers.openItems([id], 0);
  } finally {
    if (found) {
      root.hidden = false;
      body.scrollTop = top;
      root.focus({ preventScroll: true });
    }
  }
}

function linkFor(url) {
  return new URL(url, window.location.origin).href;
}

/** A "Who is this?" link for *faces*, shown in *box* to copy or send. */
async function ask(faces, box, button) {
  button.disabled = true;
  try {
    const made = await call('/api/ask-family/questions', {
      method: 'POST', body: JSON.stringify({ face_ids: faces.slice(0, MAX_ASK) }),
    });
    const url = linkFor(made.url);
    const input = el('input', { class: 'input yf-link', readOnly: true, value: url,
                                'aria-label': i18n.t('Link to send') });
    const copy = el('button', { type: 'button', class: 'btn small', text: i18n.t('Copy') });
    copy.onclick = async () => {
      try { await navigator.clipboard.writeText(url); helpers.toast(i18n.t('Link copied.')); } catch { input.select(); }
    };
    const message = i18n.t('Do you know who these people are? Please tell us here: {url}', { url });
    const whatsapp = el('a', { class: 'btn ghost small', text: i18n.t('Send by WhatsApp'),
                               href: `https://wa.me/?text=${encodeURIComponent(message)}`,
                               target: '_blank', rel: 'noopener noreferrer' });
    box.replaceChildren(
      el('p', { class: 'yf-hint', text: i18n.t('The link shows the faces, not the name Ninaivu thought of. Answers come back under Ask the family.') }),
      el('div', { class: 'yf-link-row' }, input, copy, whatsapp));
    box.hidden = false;
  } catch (error) {
    helpers.toast(i18n.t('Could not make the link: {reason}', { reason: error.message }), true);
    button.disabled = false;
  }
}

function reasons(item, person) {
  const out = [];
  out.push(item.year ? i18n.t('Taken in {year}', { year: item.year }) : i18n.t('Year not known'));
  if (item.age != null) {
    out.push(item.age < 1 ? i18n.t('the year {name} was born', { name: person.name })
      : i18n.t('{name} would have been about {age}', { name: person.name, age: num(item.age) }));
  }
  if (item.gap) {
    out.push(i18n.t('{n} years before the earliest named photo', { n: num(item.gap) }));
  }
  for (const f of item.family) {
    const what = RELATION[f.relation] ? i18n.t(RELATION[f.relation]) : '';
    out.push(what ? i18n.t('{name} ({relation}) is in this photo', { name: f.name, relation: what })
      : i18n.t('{name} is in this photo', { name: f.name }));
  }
  return out;
}

function card(item, person, section) {
  const node = el('article', { class: 'yf-card' });
  const unknown = el('button', { type: 'button', class: 'yf-face-btn',
                                 'aria-label': i18n.t('Open the photo') }, face(item.face_id, 'yf-face', ''));
  unknown.onclick = () => openPhoto(item.photo.id);
  const known = el('figure', { class: 'yf-known' }, face(person.earliest_face, 'yf-face yf-face-known', person.name),
    el('figcaption', { text: i18n.t('{name} in {year}', { name: person.name, year: person.earliest }) }));
  const pair = el('div', { class: 'yf-pair' },
    el('figure', { class: 'yf-unknown' }, unknown,
      el('figcaption', { text: item.year ? String(item.year) : i18n.t('Year not known') })),
    el('span', { class: 'yf-arrow', 'aria-hidden': 'true', text: '→' }),
    known);
  const photo = el('button', { type: 'button', class: 'yf-photo', 'aria-label': i18n.t('Open the photo') },
    el('img', { src: thumbUrl(item.photo.id, 256, item.photo.thumb_v), alt: '', loading: 'lazy', draggable: false }));
  photo.onclick = () => openPhoto(item.photo.id);
  const question = el('p', { class: 'yf-question', text: item.also
    ? i18n.t('Could this be {name}, or {other}?', { name: person.name, other: item.also.name })
    : i18n.t('Could this be {name}?', { name: person.name }) });
  const why = el('ul', { class: 'yf-why' }, ...reasons(item, person).map((r) => el('li', { text: r })));
  const box = el('div', { class: 'yf-ask', hidden: true });
  const done = (text) => {
    node.classList.add('yf-done');
    node.replaceChildren(el('p', { class: 'yf-said', role: 'status', text }));
    section.count -= 1;
    section.update();
  };
  const answer = async (who, yes, button) => {
    button.disabled = true;
    try {
      await call(`/api/young-faces/${item.face_id}/${yes ? 'yes' : 'no'}`,
        { method: 'POST', body: JSON.stringify({ person_id: who.id }) });
      done(yes ? i18n.t('Named {name}.', { name: who.name })
        : i18n.t('Not {name}. It will not be suggested as them again.', { name: who.name }));
    } catch (error) {
      helpers.toast(error.message, true);
      button.disabled = false;
    }
  };
  const yes = el('button', { type: 'button', class: 'btn primary small', text: i18n.t('Yes, {name}', { name: person.name }) });
  yes.onclick = () => answer(person, true, yes);
  const actions = el('div', { class: 'yf-actions' }, yes);
  if (item.also) {
    const other = el('button', { type: 'button', class: 'btn small', text: i18n.t('Yes, {name}', { name: item.also.name }) });
    other.onclick = () => answer(item.also, true, other);
    actions.append(other);
  }
  const no = el('button', { type: 'button', class: 'btn ghost small', text: i18n.t('No') });
  no.onclick = () => answer(person, false, no);
  const unsure = el('button', { type: 'button', class: 'btn ghost small', text: i18n.t('Ask a relative') });
  unsure.onclick = () => ask([item.face_id], box, unsure);
  actions.append(no, unsure);
  node.append(el('div', { class: 'yf-top' }, pair, photo), question, why, actions, box);
  return node;
}

function bornForm(person, section) {
  const input = el('input', { type: 'number', inputmode: 'numeric', min: '1850',
                              max: String(new Date().getFullYear()), placeholder: '1950',
                              'aria-label': i18n.t('Year born') });
  const save = el('button', { type: 'button', class: 'btn small', text: i18n.t('Save') });
  save.onclick = async () => {
    const year = Number(input.value);
    if (!Number.isInteger(year)) return;
    save.disabled = true;
    try {
      await call(`/api/young-faces/born/${person.id}`, { method: 'PUT', body: JSON.stringify({ year }) });
      await load();
    } catch (error) {
      helpers.toast(error.message, true);
      save.disabled = false;
    }
  };
  return el('div', { class: 'yf-born' },
    el('label', { text: i18n.t('When was {name} born? Photos from before are then left out, and each says how old they were.', { name: person.name }) }),
    el('div', { class: 'yf-born-row' }, input, save));
}

function section(group) {
  const p = group.person;
  const node = el('section', { class: 'yf-person' });
  const state = { count: group.faces.length };
  const meta = el('p', { class: 'yf-meta' });
  state.update = () => {
    meta.textContent = [
      p.born ? i18n.t('Born {year}', { year: p.born }) : '',
      i18n.t('Named in photos from {year}', { year: p.earliest }),
      state.count > 1 ? i18n.t('{n} faces that may be them', { n: num(state.count) })
        : state.count === 1 ? i18n.t('One face that may be them') : i18n.t('All answered'),
    ].filter(Boolean).join(' · ');
  };
  state.update();
  const box = el('div', { class: 'yf-ask', hidden: true });
  const askAll = el('button', { type: 'button', class: 'btn ghost small',
    text: i18n.t('Ask a relative about these') });
  askAll.onclick = () => ask(group.faces.map((f) => f.face_id), box, askAll);
  node.append(...[
    el('header', { class: 'yf-person-head' }, avatar(p),
      el('div', { class: 'yf-person-text' }, el('h2', { text: p.name }), meta),
      group.faces.length > 1 ? askAll : null),
    box,
    p.born ? null : bornForm(p, state),
    el('div', { class: 'yf-cards' }, ...group.faces.map((item) => card(item, p, state)))].filter(Boolean));
  return node;
}

function render() {
  if (!found.people.length) {
    body.replaceChildren(el('p', { class: 'yf-empty', text: found.faces
      ? i18n.t('No young faces look like anybody named yet. Name more people on the People page, or say when they were born, and look again.')
      : i18n.t('Name people in photos from later years on the People page, and Ninaivu will look for them in older photos.') }));
    return;
  }
  body.replaceChildren(
    el('p', { class: 'yf-intro', text: i18n.t('Faces nobody has named in older photos, beside the earliest photo where the family named each person. Say whether it is them, or send a relative a link to ask.') }),
    ...found.people.map(section));
}

async function load() {
  body.replaceChildren(el('p', { class: 'yf-empty', text: i18n.t('Looking through the faces…') }));
  try {
    found = await call('/api/young-faces');
  } catch (error) {
    body.replaceChildren(el('p', { class: 'yf-empty', text: error.message }));
    return;
  }
  const top = body.scrollTop;
  render();
  body.scrollTop = top;
}

function build() {
  if (root) return root;
  root = el('div', { class: 'young-faces', id: 'young-faces', hidden: true, role: 'dialog', 'aria-modal': 'true',
                     'aria-label': i18n.t('Young faces, older names'), tabindex: '-1' });
  const closer = el('button', { type: 'button', class: 'icon-btn yf-close', 'aria-label': i18n.t('Close') });
  closer.innerHTML = '<svg viewBox="0 0 24 24"><path d="M18 6 6 18M6 6l12 12"/></svg>';
  closer.onclick = close;
  body = el('div', { class: 'yf-body' });
  root.append(el('header', { class: 'yf-bar' },
    el('p', { class: 'yf-bar-title', text: i18n.t('Young faces, older names') }), closer), body);
  root.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') close();
  });
  document.body.appendChild(root);
  return root;
}

/** Open the page of suggestions. */
export async function openYoungFaces({ toast, openItems } = {}) {
  helpers = { toast: toast || helpers.toast, openItems: openItems || helpers.openItems };
  build();
  root.querySelector('.yf-bar-title').textContent = i18n.t('Young faces, older names');
  root.hidden = false;
  document.body.style.overflow = 'hidden';
  root.focus();
  await load();
}

function close() {
  if (!root || root.hidden) return;
  root.hidden = true;
  document.body.style.overflow = '';
  body.replaceChildren();
  found = null;
}
