/**
 * Ask Ninaivu: a question about the family's photographs, in Tamil or
 * English, answered on this computer (ninaivu/api/api_ask_ninaivu.py).
 *
 * One sheet: a box to type in, a few questions to start from, and under it
 * the answer — what was understood (the same chips the search box shows),
 * the answer itself, and the photographs it comes from, which open in the
 * viewer. The answer is written by the on-device writing model when it is
 * there; otherwise it is a line or two of facts, put in the page's own
 * language here from the sentences the server sends.
 *
 * Nothing typed here is sent anywhere but this household's own server.
 */

import { thumbUrl } from './api.js';
import * as i18n from './i18n.js';

let modal = null;
let openItemsFn = null;
let asking = false;

//: Translated where they are drawn: see i18n.key().
const STARTERS = [
  i18n.key('When did we last go to the beach?'),
  i18n.key('How many photos from last year?'),
  i18n.key("What's new this week?"),
  i18n.key('Who is in the most photos from 2019?'),
];

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
    headers: { Accept: 'application/json', 'X-Requested-With': 'fetch', ...(options.headers || {}) },
  });
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    throw Object.assign(new Error(i18n.fromServer(data, response.statusText)),
      { status: response.status, data });
  }
  return data;
}

/** A date the server sent as YYYY-MM-DD, as the reader writes dates. */
function day(iso) {
  const [y, m, d] = String(iso || '').split('-').map(Number);
  if (!y || !m || !d) return String(iso || '');
  try {
    return new Intl.DateTimeFormat(i18n.locale(), { day: 'numeric', month: 'long', year: 'numeric', timeZone: 'UTC' })
      .format(new Date(Date.UTC(y, m - 1, d)));
  } catch {
    return String(iso);
  }
}

/** One of the server's plain sentences, in this page's language. */
function line({ key, params = {} }) {
  const vars = { ...params };
  for (const name of ['date', 'first', 'last']) if (name in vars) vars[name] = day(vars[name]);
  if (typeof vars.n === 'number') vars.n = vars.n.toLocaleString(i18n.locale());
  return i18n.t(key, vars);
}

function chip(item) {
  const label = item.type === 'kind' || item.type === 'favorites' ? i18n.t(item.label) : item.label;
  return el('span', { class: `ask-chip ask-chip-${item.type}`, text: label });
}

/** The model's words, with each [#id] it points at made a small picture. */
function written(text, items, ids) {
  const box = el('p', { class: 'ask-written', lang: '' });
  const parts = String(text).split(/(\[#\d+\])/);
  for (const part of parts) {
    const match = /^\[#(\d+)\]$/.exec(part);
    if (!match) { box.append(part); continue; }
    const id = Number(match[1]);
    const item = items.find((i) => i.id === id);
    if (!item) continue;
    const button = el('button', { class: 'ask-cite', type: 'button', title: i18n.t('Open this photo') });
    button.append(el('img', { src: thumbUrl(id, 160, item.thumb_v), alt: '' }));
    button.onclick = () => openAt(ids, ids.indexOf(id));
    box.append(button);
  }
  return box;
}

function openAt(ids, index) {
  if (!openItemsFn || !ids.length) return;
  close();
  openItemsFn(ids, Math.max(0, index));
}

function answerView(data) {
  const box = el('div', { class: 'ask-answer' });
  if (data.understood?.length) {
    box.append(el('div', { class: 'ask-understood' }, ...data.understood.map(chip)));
  }
  const ids = (data.items || []).map((i) => i.id);
  if (data.written) {
    box.append(written(data.written, data.items || [], ids));
    box.append(el('p', { class: 'ask-plain ask-plain-small' },
      data.lines.map(line).join(' ')));
  } else {
    box.append(el('p', { class: 'ask-plain' }, data.lines.map(line).join(' ')));
  }
  if (ids.length) {
    const strip = el('div', { class: 'ask-photos' });
    (data.items || []).forEach((item, index) => {
      const button = el('button', { class: 'ask-photo', type: 'button',
                                    'aria-label': item.name || i18n.t('Photo') });
      button.append(el('img', { src: thumbUrl(item.id, 320, item.thumb_v), alt: '', loading: 'lazy' }));
      if (item.kind === 'video') button.append(el('span', { class: 'ask-photo-video', text: '▶' }));
      button.onclick = () => openAt(ids, index);
      strip.append(button);
    });
    box.append(strip);
    if ((data.total || 0) > ids.length && data.ids?.length > ids.length) {
      const more = el('button', { class: 'btn ghost small', type: 'button',
                                  text: i18n.t('See all {n}', { n: data.total.toLocaleString(i18n.locale()) }) });
      more.onclick = () => openAt(data.ids, 0);
      box.append(more);
    }
  }
  const by = el('p', { class: 'ask-by' },
    data.model ? i18n.t('Written on this computer by the writing model.')
      : i18n.t('Answered from the library on this computer.'));
  box.append(by);
  return box;
}

function build() {
  if (modal) return modal;
  modal = el('div', { class: 'modal ask-modal', hidden: true, role: 'dialog',
                      'aria-modal': 'true', 'aria-labelledby': 'ask-heading' });
  const card = el('div', { class: 'modal-card ask-card' });
  const closer = el('button', { class: 'icon-btn', type: 'button', 'aria-label': i18n.t('Close') });
  closer.innerHTML = '<svg viewBox="0 0 24 24"><path d="M18 6 6 18M6 6l12 12"/></svg>';
  closer.onclick = close;
  card.append(el('div', { class: 'export-head' },
                 el('h2', { id: 'ask-heading', text: i18n.t('Ask Ninaivu') }), closer));
  const form = el('form', { class: 'ask-form' });
  const input = el('textarea', { class: 'ask-input', rows: 2, maxLength: 500,
                                 placeholder: i18n.t('Ask about your photos, in Tamil or English'),
                                 'aria-label': i18n.t('Your question') });
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); form.requestSubmit(); }
  });
  const go = el('button', { class: 'btn primary', type: 'submit', text: i18n.t('Ask') });
  form.append(input, go);
  form.onsubmit = (event) => { event.preventDefault(); submit(input.value); };
  const starters = el('div', { class: 'ask-starters' },
    ...STARTERS.map((text) => {
      const b = el('button', { class: 'ask-starter', type: 'button', text: i18n.t(text) });
      b.onclick = () => { input.value = b.textContent; submit(input.value); };
      return b;
    }));
  card.append(form, starters, el('div', { class: 'ask-result', 'aria-live': 'polite' }),
              el('p', { class: 'ask-note' }));
  modal.append(card);
  modal.addEventListener('mousedown', (event) => { if (event.target === modal) close(); });
  modal.addEventListener('keydown', (event) => { if (event.key === 'Escape') close(); });
  document.body.appendChild(modal);
  return modal;
}

async function submit(text) {
  const question = String(text || '').trim();
  if (!question || asking) return;
  const result = modal.querySelector('.ask-result');
  asking = true;
  modal.querySelector('.ask-form button').disabled = true;
  result.replaceChildren(el('p', { class: 'ask-thinking', text: i18n.t('Looking through the photos…') }));
  try {
    const data = await call('/api/ask-ninaivu', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question }),
    });
    result.replaceChildren(answerView(data));
  } catch (error) {
    result.replaceChildren(el('p', { class: 'ask-error', text: error.message }));
  } finally {
    asking = false;
    modal.querySelector('.ask-form button').disabled = false;
  }
}

function close() {
  if (modal) modal.hidden = true;
}

/** Open the sheet, with *question* in the box (and asked) when there is one. */
export async function openAsk({ openItems, question = '' } = {}) {
  openItemsFn = openItems || openItemsFn;
  build();
  modal.hidden = false;
  const input = modal.querySelector('.ask-input');
  const note = modal.querySelector('.ask-note');
  note.textContent = '';
  call('/api/ask-ninaivu').then((status) => {
    note.textContent = status.writer ? ''
      : i18n.t('Answers are short facts until the writing model is downloaded (AI models page) and the on-device AI engine is on.');
  }).catch(() => {});
  if (question) {
    input.value = question;
    submit(question);
  } else {
    input.focus();
  }
}
