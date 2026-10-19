/**
 * Time capsules: photographs and a letter sealed for somebody in the family
 * until a date (ninaivu/storage/capsules.py, ninaivu/api/api_capsules.py).
 *
 * Three things on screen. The list, from the envelope beside Albums: every
 * capsule for you or sealed by you, each a closed envelope saying who it is
 * from, who it is for and when it opens. The seal sheet, from "Seal in a time
 * capsule" on a selection (or from the list, for a letter alone). And the
 * opening: on the day, the envelope's seal breaks, the flap lifts and the
 * letter rises out of it, with the photographs under it.
 *
 * Before the day the server sends nothing of what is inside, to anybody; this
 * page cannot show what it is never given.
 */

import { thumbUrl } from './api.js';
import * as i18n from './i18n.js';

let modal = null;
let toastFn = () => {};
let openItemsFn = null;

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

async function ask(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: { Accept: 'application/json', 'X-Requested-With': 'fetch', ...(options.headers || {}) },
  });
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    throw Object.assign(new Error(i18n.fromServer(data, response.statusText)),
      { status: response.status, data });
  }
  return data;
}

function send(url, body, method = 'POST') {
  return ask(url, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
}

function closeButton(onClick) {
  const button = el('button', { class: 'icon-btn', type: 'button', 'aria-label': i18n.t('Close') });
  button.innerHTML = '<svg viewBox="0 0 24 24"><path d="M18 6 6 18M6 6l12 12"/></svg>';
  button.onclick = onClick;
  return button;
}

/** The day a capsule opens, as the reader writes dates. */
function day(iso) {
  const [y, m, d] = String(iso).split('-').map(Number);
  try {
    return new Intl.DateTimeFormat(i18n.locale(), { day: 'numeric', month: 'long', year: 'numeric', timeZone: 'UTC' })
      .format(new Date(Date.UTC(y, m - 1, d)));
  } catch {
    return String(iso);
  }
}

function daysUntil(iso) {
  const [y, m, d] = String(iso).split('-').map(Number);
  const now = new Date();
  const today = Date.UTC(now.getFullYear(), now.getMonth(), now.getDate());
  return Math.round((Date.UTC(y, m - 1, d) - today) / 86400000);
}

function untilText(iso) {
  const days = daysUntil(iso);
  if (days <= 0) return i18n.t('Ready to open');
  if (days === 1) return i18n.t('Opens tomorrow');
  if (days < 60) return i18n.t('Opens in {days} days', { days });
  const years = Math.floor(days / 365.25);
  if (years >= 2) return i18n.t('Opens in {years} years', { years });
  if (days >= 330) return i18n.t('Opens in a year');
  const months = Math.max(2, Math.round(days / 30.44));
  return i18n.t('Opens in {months} months', { months });
}

function whoText(capsule) {
  const to = capsule.for_everybody ? i18n.t('For everybody in the family')
    : i18n.t('For {name}', { name: capsule.for || '' });
  const from = capsule.from ? i18n.t('From {name}', { name: capsule.from }) : '';
  return [from, to].filter(Boolean).join(' · ');
}

/* -- the dialog -------------------------------------------------------- */

function build() {
  if (modal) return modal;
  modal = el('div', { class: 'modal capsule-modal', hidden: true, role: 'dialog',
                      'aria-modal': 'true', 'aria-labelledby': 'capsule-heading' });
  const card = el('div', { class: 'modal-card capsule-card' });
  card.append(el('div', { class: 'export-head' },
                 el('h2', { id: 'capsule-heading', text: i18n.t('Time capsules') }), closeButton(close)));
  card.append(el('div', { class: 'capsule-body' }));
  modal.append(card);
  modal.addEventListener('mousedown', (event) => { if (event.target === modal) close(); });
  modal.addEventListener('keydown', (event) => { if (event.key === 'Escape') close(); });
  document.body.appendChild(modal);
  return modal;
}

function body(heading, ...nodes) {
  build();
  modal.querySelector('#capsule-heading').textContent = heading;
  const box = modal.querySelector('.capsule-body');
  box.replaceChildren(...nodes);
  modal.hidden = false;
  return box;
}

function close() {
  if (modal) modal.hidden = true;
}

function envelope(capsule, { large = false } = {}) {
  const sealed = capsule.sealed;
  return el('div', { class: `capsule-envelope${sealed ? ' sealed' : ' ready'}${large ? ' large' : ''}`, 'aria-hidden': 'true' },
    el('div', { class: 'capsule-flap' }),
    el('div', { class: 'capsule-seal' }, el('span', { text: '✦' })));
}

/** The list of capsules. */
export async function openCapsules({ toast, openItems } = {}) {
  if (toast) toastFn = toast;
  if (openItems) openItemsFn = openItems;
  const box = body(i18n.t('Time capsules'), el('p', { class: 'capsule-muted', text: i18n.t('Loading…') }));
  let listed;
  try {
    listed = await ask('/api/capsules');
  } catch (error) {
    box.replaceChildren(el('p', { class: 'capsule-muted', text: error.message }));
    return;
  }
  const list = el('div', { class: 'capsule-list' });
  for (const capsule of listed.capsules) {
    const row = el('button', { type: 'button', class: `capsule-row${capsule.waiting ? ' waiting' : ''}` },
      envelope(capsule),
      el('span', { class: 'capsule-row-text' },
        el('strong', { text: capsule.title }),
        el('span', { text: whoText(capsule) }),
        el('span', { class: 'capsule-when', text: capsule.sealed
          ? `${untilText(capsule.opens_on)} · ${day(capsule.opens_on)}`
          : (capsule.waiting ? i18n.t('Ready to open') : i18n.t('Opened {date}', { date: day(capsule.opens_on) })) })));
    row.onclick = () => openCapsule(capsule.id);
    list.append(row);
  }
  const empty = el('div', { class: 'capsule-empty' },
    el('p', { text: i18n.t('Seal photographs and a letter for somebody, to be opened on a day you choose: a birthday, a wedding, a year from now.') }),
    el('p', { class: 'capsule-muted', text: i18n.t('Select photographs in the gallery and choose “Seal in a time capsule”, or write a letter on its own.') }));
  const write = el('button', { type: 'button', class: 'btn primary', text: i18n.t('Write a letter on its own') });
  write.onclick = () => openSealSheet([], { toast: toastFn });
  box.replaceChildren(listed.capsules.length ? list : empty, el('div', { class: 'capsule-actions' }, write));
}

/** One capsule: the envelope while sealed, the ceremony when it is ready. */
async function openCapsule(id) {
  let opened;
  try {
    opened = await ask(`/api/capsules/${id}`);
  } catch (error) {
    if (error.status === 423 && error.data?.capsule) {
      showSealed(error.data.capsule);
      return;
    }
    toastFn(error.message, true);
    return;
  }
  showOpened(opened);
}

function showSealed(capsule) {
  const remove = capsule.mine ? el('button', { type: 'button', class: 'btn ghost small danger-text', text: i18n.t('Take it back') }) : null;
  if (remove) remove.onclick = () => takeBack(capsule);
  const back = el('button', { type: 'button', class: 'btn ghost small', text: i18n.t('All time capsules') });
  back.onclick = () => openCapsules();
  body(capsule.title,
    el('div', { class: 'capsule-stage' }, envelope(capsule, { large: true })),
    el('p', { class: 'capsule-who', text: whoText(capsule) }),
    el('p', { class: 'capsule-countdown', text: untilText(capsule.opens_on) }),
    el('p', { class: 'capsule-muted', text: i18n.t('Sealed until {date}. Nobody can see inside until then, not even whoever sealed it.', { date: day(capsule.opens_on) }) }),
    el('div', { class: 'capsule-actions' }, back, remove));
}

async function takeBack(capsule) {
  if (!confirm(i18n.t('Take back “{title}”? The letter is deleted; the photographs are not touched.', { title: capsule.title }))) return;
  try {
    await ask(`/api/capsules/${capsule.id}`, { method: 'DELETE' });
    toastFn(i18n.t('The time capsule was taken back.'));
    openCapsules();
  } catch (error) {
    toastFn(error.message, true);
  }
}

function showOpened(opened) {
  const { capsule } = opened;
  const stage = el('div', { class: 'capsule-stage' }, envelope(capsule, { large: true }));
  const letter = el('div', { class: 'capsule-letter' },
    el('p', { class: 'capsule-who', text: whoText(capsule) }),
    opened.letter ? el('div', { class: 'capsule-words', text: opened.letter }) : null);
  const photos = el('div', { class: 'capsule-photos' });
  opened.ids.forEach((assetId, index) => {
    const tile = el('button', { type: 'button', class: 'capsule-photo', 'aria-label': i18n.t('Open photograph {number}', { number: index + 1 }) },
      el('img', { src: thumbUrl(assetId, 256), alt: '', loading: 'lazy' }));
    tile.style.setProperty('--i', String(index));
    tile.onclick = async () => {
      if (!openItemsFn) return;
      modal.hidden = true;
      await openItemsFn(opened.ids, index);
      modal.hidden = false;
    };
    photos.append(tile);
  });
  const notShown = opened.not_shown
    ? el('p', { class: 'capsule-muted', text: i18n.t('{count} more were sealed in it that you cannot see now.', { count: opened.not_shown }) })
    : null;
  const back = el('button', { type: 'button', class: 'btn ghost small', text: i18n.t('All time capsules') });
  back.onclick = () => openCapsules();
  const remove = capsule.mine ? el('button', { type: 'button', class: 'btn ghost small danger-text', text: i18n.t('Delete') }) : null;
  if (remove) remove.onclick = () => takeBack(capsule);
  const reveal = el('div', { class: 'capsule-reveal', hidden: true }, letter,
    opened.ids.length ? photos : null, notShown, el('div', { class: 'capsule-actions' }, back, remove));
  const open = el('button', { type: 'button', class: 'btn primary capsule-open-btn', text: i18n.t('Open it') });
  const ceremony = () => {
    open.hidden = true;
    stage.querySelector('.capsule-envelope').classList.add('opening');
    const motion = !matchMedia('(prefers-reduced-motion: reduce)').matches;
    setTimeout(() => {
      stage.classList.add('done');
      reveal.hidden = false;
    }, motion ? 1500 : 0);
  };
  open.onclick = ceremony;
  body(capsule.title, stage, open, reveal);
  // Opened before: straight to what is inside.
  if (!capsule.waiting) ceremony();
  else open.focus();
}

/* -- sealing one ------------------------------------------------------- */

function tomorrow() {
  const now = new Date();
  const next = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1);
  return `${next.getFullYear()}-${String(next.getMonth() + 1).padStart(2, '0')}-${String(next.getDate()).padStart(2, '0')}`;
}

function nextYear() {
  const now = new Date();
  return `${now.getFullYear() + 1}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
}

/** Seal these photographs (or none, for a letter alone). */
export async function openSealSheet(ids, { toast, onSealed } = {}) {
  if (toast) toastFn = toast;
  let listed;
  try {
    listed = await ask('/api/capsules');
  } catch (error) {
    toastFn(error.message, true);
    return;
  }
  if (ids.length > listed.max_items) {
    toastFn(i18n.t('A time capsule holds at most {max_items} photographs.', { max_items: listed.max_items }), true);
    return;
  }
  const title = el('input', { class: 'input', type: 'text', maxLength: 120, required: true,
                              placeholder: i18n.t('For Arun, on his eighteenth birthday') });
  const reader = el('select', { class: 'input' });
  reader.add(new Option(i18n.t('Everybody in the family'), ''));
  for (const person of listed.readers) reader.add(new Option(person.name, String(person.id)));
  const opens = el('input', { class: 'input', type: 'date', min: tomorrow(), value: nextYear(), required: true });
  const letter = el('textarea', { class: 'input', rows: 7, maxLength: listed.max_letter,
                                  placeholder: i18n.t('Write what you want them to read on that day.') });
  const strip = el('div', { class: 'capsule-strip' });
  for (const id of ids.slice(0, 24)) strip.append(el('img', { src: thumbUrl(id, 256), alt: '', loading: 'lazy' }));
  if (ids.length > 24) strip.append(el('span', { class: 'capsule-more', text: `+${ids.length - 24}` }));
  const field = (label, control) => el('label', { class: 'export-field' }, el('span', { text: label }), control);
  const seal = el('button', { type: 'submit', class: 'btn primary', text: i18n.t('Seal it') });
  const form = el('form', { class: 'capsule-form' },
    ids.length ? el('p', { class: 'capsule-muted', text: ids.length === 1 ? i18n.t('One photograph goes in.')
      : i18n.t('{count} photographs go in.', { count: ids.length }) }) : null,
    ids.length ? strip : null,
    field(i18n.t('Name'), title),
    field(i18n.t('For'), reader),
    field(i18n.t('Opens on'), opens),
    field(i18n.t('Letter'), letter),
    el('p', { class: 'capsule-muted', text: i18n.t('Once sealed, nobody can open it before that day, not even you. You can take it back.') }),
    el('div', { class: 'capsule-actions' }, seal));
  form.onsubmit = async (event) => {
    event.preventDefault();
    seal.disabled = true;
    try {
      const made = await send('/api/capsules', {
        title: title.value, letter: letter.value, opens_on: opens.value,
        for: reader.value ? Number(reader.value) : null, ids,
      });
      toastFn(i18n.t('Sealed until {date}.', { date: day(made.capsule.opens_on) }));
      onSealed?.();
      showSealed(made.capsule);
    } catch (error) {
      toastFn(error.message, true);
      seal.disabled = false;
    }
  };
  body(i18n.t('Seal a time capsule'), form);
  setTimeout(() => title.focus(), 50);
}

/* -- the day it opens -------------------------------------------------- */

/** Say once a session when a capsule for this person is ready to open. */
export async function announceCapsules({ toast, openItems } = {}) {
  if (toast) toastFn = toast;
  if (openItems) openItemsFn = openItems;
  try {
    if (sessionStorage.getItem('ninaivu.capsules.told') === '1') return;
  } catch { /* private window: tell every time */ }
  let listed;
  try {
    listed = await ask('/api/capsules');
  } catch {
    return;
  }
  const waiting = listed.capsules.filter((c) => c.waiting);
  if (!waiting.length) return;
  try { sessionStorage.setItem('ninaivu.capsules.told', '1'); } catch { /* as above */ }
  const first = waiting[0];
  const text = first.from
    ? i18n.t('A time capsule from {name} is ready to open.', { name: first.from })
    : i18n.t('A time capsule is ready to open.');
  const open = el('button', { type: 'button', class: 'btn primary small', text: i18n.t('Open it') });
  const later = el('button', { type: 'button', class: 'icon-btn', 'aria-label': i18n.t('Close') });
  later.innerHTML = '<svg viewBox="0 0 24 24"><path d="M18 6 6 18M6 6l12 12"/></svg>';
  const note = el('div', { class: 'capsule-note', role: 'status' },
    envelope({ sealed: false }), el('span', { text }), open, later);
  open.onclick = () => { note.remove(); openCapsule(first.id); };
  later.onclick = () => note.remove();
  document.body.append(note);
}
