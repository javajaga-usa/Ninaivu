/**
 * Photo books: "Make a book" from an album, an occasion or a person, and the
 * list of books made (ninaivu/media/books.py does the work).
 *
 * Three steps in one dialog, because each depends on the one before: the
 * settings, then the photographs Ninaivu picked — every one ticked, so
 * leaving one out is a tap — then the build, which runs on the server while
 * this polls it, and ends in a Download button.
 *
 * The dialog is built here rather than written into index.html: it is only
 * ever opened from a button this module is asked to make, and keeping its
 * markup beside its behaviour keeps the template untouched by a feature
 * that most visits never open. It reuses the export dialog's classes so it
 * looks like the rest of the app, and `.modal` already takes the iPhone's
 * real height (`--app-h`).
 */

import { api, thumbUrl } from './api.js';
import * as i18n from './i18n.js';

const TEMPLATES = [
  ['plain', i18n.key('Plain')],
  ['wedding', i18n.key('Wedding')],
  ['pongal', i18n.key('Pongal')],
  ['deepavali', i18n.key('Deepavali')],
  ['birthday', i18n.key('Birthday')],
  ['holiday', i18n.key('Holiday')],
];
const SIZES = [
  ['a4-portrait', i18n.key('A4 portrait')],
  ['a4-landscape', i18n.key('A4 landscape')],
  ['square-20', i18n.key('Square, 20 × 20 cm')],
  ['square-30', i18n.key('Square, 30 × 30 cm')],
];

let modal = null;
let source = null;
let picked = [];
let poll = null;
let titleEdited = false;
let subtitleEdited = false;
let toastFn = () => {};

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

function closeButton(onClick) {
  const button = el('button', { class: 'icon-btn', type: 'button', 'aria-label': i18n.t('Close') });
  button.innerHTML = '<svg viewBox="0 0 24 24"><path d="M18 6 6 18M6 6l12 12"/></svg>';
  button.onclick = onClick;
  return button;
}

function field(label, control) {
  return el('label', { class: 'export-field' }, el('span', { text: label }), control);
}

function select(options, value) {
  const box = el('select', { class: 'input' });
  for (const [code, name] of options) box.add(new Option(i18n.t(name), code));
  box.value = value;
  return box;
}

/** A title the template suggests, in the reader's language. */
function suggestedTitle(template) {
  const year = source?.started ? new Date(source.started * 1000).getUTCFullYear() : '';
  const name = source?.name || '';
  switch (template) {
    case 'wedding': return i18n.t('Our wedding');
    case 'pongal': return i18n.t('Pongal {year}', { year });
    case 'deepavali': return i18n.t('Deepavali {year}', { year });
    case 'birthday': return name ? i18n.t('Happy birthday, {name}', { name }) : i18n.t('Happy birthday');
    case 'holiday': return source?.place ? i18n.t('Holiday in {place}', { place: source.place })
      : i18n.t('Our holiday');
    default: return name || subtitleFor() || i18n.t('Photo book');
  }
}

function subtitleFor() {
  if (!source?.started) return source?.place || '';
  const dates = i18n.dateRange(source.started, source.ended || source.started);
  return [dates, source.place].filter(Boolean).join(' · ');
}

/** The twelve months as the reader writes them, for the dates under photographs. */
function months() {
  try {
    const format = new Intl.DateTimeFormat(i18n.locale(), { month: 'long', timeZone: 'UTC' });
    return Array.from({ length: 12 }, (_, m) => format.format(new Date(Date.UTC(2020, m, 15))));
  } catch {
    return undefined;
  }
}

function build() {
  if (modal) return modal;
  modal = el('div', { class: 'modal', id: 'book-modal', hidden: true, role: 'dialog',
                      'aria-modal': 'true', 'aria-labelledby': 'book-title' });
  const card = el('form', { class: 'modal-card narrow export-form book-card', id: 'book-form' });
  const head = el('div', { class: 'export-head' },
                  el('h2', { id: 'book-title', text: i18n.t('Make a book') }), closeButton(close));
  modal.append(card);
  card.append(head);
  modal.addEventListener('mousedown', (event) => { if (event.target === modal) close(); });
  modal.addEventListener('keydown', (event) => { if (event.key === 'Escape') close(); });
  document.body.appendChild(modal);
  return modal;
}

function step(...nodes) {
  const card = build().querySelector('form');
  while (card.children.length > 1) card.lastChild.remove();
  card.append(...nodes);
  card.querySelector('h2').textContent = i18n.t('Make a book');
  return card;
}

function close() {
  clearTimeout(poll);
  poll = null;
  if (modal) modal.hidden = true;
}

/**
 * Open "Make a book" for a source: `{ kind: 'album'|'occasion'|'person'|'smart',
 * id, name, place, started, ended }`.
 */
export function openBookSheet(from, { toast } = {}) {
  source = { ...from };
  if (toast) toastFn = toast;
  titleEdited = false;
  subtitleEdited = false;
  settings();
  build().hidden = false;
}

let choices = { template: 'plain', size: 'a4-portrait', count: 36, captions: true, bleed: false };

function settings(fonts) {
  const template = select(TEMPLATES, choices.template);
  const size = select(SIZES, choices.size);
  const title = el('input', { class: 'input', type: 'text', maxLength: 120 });
  const subtitle = el('input', { class: 'input', type: 'text', maxLength: 160 });
  title.value = choices.title && titleEdited ? choices.title : suggestedTitle(choices.template);
  subtitle.value = choices.subtitle ?? subtitleFor();
  title.oninput = () => { titleEdited = true; };
  subtitle.oninput = () => { subtitleEdited = true; };
  template.onchange = () => { if (!titleEdited) title.value = suggestedTitle(template.value); };
  const count = el('input', { type: 'range', min: 12, max: 120, step: 1, value: choices.count });
  const shown = el('output', { text: String(choices.count) });
  count.oninput = () => { shown.textContent = count.value; };
  const captions = el('input', { type: 'checkbox', checked: choices.captions });
  const bleed = el('input', { type: 'checkbox', checked: choices.bleed });
  const warning = el('p', { class: 'hint warn', hidden: true,
    text: i18n.t('This computer has no Tamil font, so Tamil words will be left out of the book. Installing one (for example the Noto Tamil fonts) fixes it.') });
  const showWarning = (report) => { warning.hidden = !report || report.tamil; };
  if (fonts) showWarning(fonts);
  else api.bookOptions().then((o) => showWarning(o.fonts)).catch(() => {});

  const next = el('button', { class: 'btn primary', type: 'submit', text: i18n.t('Choose photographs') });
  const cancel = el('button', { class: 'btn ghost', type: 'button', text: i18n.t('Cancel') });
  cancel.onclick = close;
  const card = step(
    el('p', { class: 'hint', text: i18n.t('Ninaivu picks the best photographs, leaves out blurred ones, screenshots and repeats, and lays them out on pages ready for any print shop.') }),
    field(i18n.t('Template'), template),
    field(i18n.t('Size'), size),
    field(i18n.t('Title'), title),
    field(i18n.t('Subtitle'), subtitle),
    el('label', { class: 'export-field' }, el('span', { text: i18n.t('Number of photographs') }),
       el('span', { class: 'export-range' }, count, shown)),
    el('label', { class: 'export-check' }, captions,
       el('span', { text: i18n.t('Captions: what was written, the date and who is in it') })),
    el('label', { class: 'export-check' }, bleed,
       el('span', { text: i18n.t('Add 3 mm bleed for the print shop') })),
    warning,
    el('div', { class: 'export-actions' }, cancel, next),
  );
  card.onsubmit = async (event) => {
    event.preventDefault();
    choices = { template: template.value, size: size.value, count: Number(count.value),
                captions: captions.checked, bleed: bleed.checked,
                title: title.value.trim(), subtitle: subtitle.value.trim() };
    next.disabled = true;
    try {
      const data = await api.bookPick({ kind: source.kind, id: source.id }, choices.count);
      picked = data.items || [];
      if (data.source) {
        source.place = source.place || data.source.place;
        source.started = source.started || data.source.started_at;
        source.ended = source.ended || data.source.ended_at;
      }
      // An album's dates are only known once its photographs are: written
      // in now, unless somebody typed their own.
      if (!choices.subtitle && !subtitleEdited) choices.subtitle = subtitleFor();
      preview(data.fonts);
    } catch (err) {
      toastFn(i18n.t('Could not choose photographs: {reason}', { reason: err.message }), true);
      next.disabled = false;
    }
  };
  setTimeout(() => template.focus(), 0);
}

function preview(fonts) {
  const grid = el('div', { class: 'book-picks' });
  const count = el('p', { class: 'hint' });
  const sync = () => {
    const n = grid.querySelectorAll('input:checked').length;
    count.textContent = i18n.t('{count} of {total} photographs will be in the book. Untick any you would rather leave out.',
                               { count: n, total: picked.length });
    buildButton.disabled = n === 0;
  };
  for (const item of picked) {
    const tick = el('input', { type: 'checkbox', checked: true, value: String(item.id),
                               'aria-label': item.date || String(item.id) });
    tick.onchange = sync;
    const img = el('img', { src: thumbUrl(item.id, 240, item.thumb_v), alt: '', loading: 'lazy' });
    grid.append(el('label', { class: 'book-pick' }, img, tick));
  }
  const back = el('button', { class: 'btn ghost', type: 'button', text: i18n.t('Back') });
  back.onclick = () => settings(fonts);
  const buildButton = el('button', { class: 'btn primary', type: 'submit', text: i18n.t('Build the book') });
  const card = step(
    picked.length ? count : el('p', { class: 'hint', text: i18n.t('There are no photographs here that can go in a book.') }),
    grid,
    el('div', { class: 'export-actions' }, back, buildButton),
  );
  sync();
  if (!picked.length) buildButton.disabled = true;
  card.onsubmit = async (event) => {
    event.preventDefault();
    const ids = [...grid.querySelectorAll('input:checked')].map((box) => Number(box.value));
    if (!ids.length) return;
    buildButton.disabled = true;
    try {
      const book = await api.bookBuild({
        source: { kind: source.kind, id: source.id }, ids,
        template: choices.template, size: choices.size, bleed: choices.bleed,
        captions: choices.captions, title: choices.title, subtitle: choices.subtitle,
        closing: i18n.t('Made with Ninaivu'), months: months(),
      });
      progress(book);
    } catch (err) {
      toastFn(i18n.t('Could not make the book: {reason}', { reason: err.message }), true);
      buildButton.disabled = false;
    }
  };
  setTimeout(() => buildButton.focus(), 0);
}

function downloadLink(book) {
  return el('a', { class: 'btn primary', href: book.download, download: '', text: i18n.t('Download') });
}

function progress(book) {
  const bar = el('progress', { class: 'book-progress', max: 1, value: 0 });
  const said = el('p', { class: 'hint', 'aria-live': 'polite' });
  const actions = el('div', { class: 'export-actions' });
  step(el('p', { text: book.title }), bar, said, actions);
  const show = (now) => {
    clearTimeout(poll);
    actions.replaceChildren();
    const { done, total } = now.progress || {};
    if (now.state === 'building') {
      bar.max = total || 1;
      bar.value = done || 0;
      said.textContent = total ? i18n.t('Laying out page {done} of {total}…', { done, total })
        : i18n.t('Getting the photographs ready…');
      const stop = el('button', { class: 'btn ghost', type: 'button', text: i18n.t('Stop') });
      stop.onclick = () => api.bookCancel(now.id).catch(() => {});
      actions.append(stop);
      poll = setTimeout(() => api.book(now.id).then(show).catch(() => {
        poll = setTimeout(() => show(now), 3000);
      }), 1000);
      return;
    }
    const closer = el('button', { class: 'btn ghost', type: 'button', text: i18n.t('Close') });
    closer.onclick = close;
    if (now.state === 'done') {
      bar.max = 1;
      bar.value = 1;
      said.textContent = i18n.t('Your book is ready: {pages} pages. It stays in Books until you delete it.',
                                { pages: now.pages });
      actions.append(closer, downloadLink(now));
      actions.lastChild.focus();
    } else {
      said.textContent = now.state === 'cancelled' ? i18n.t('Stopped. No book was made.')
        : i18n.t('The book could not be made.');
      if (now.error && now.state !== 'cancelled') said.title = now.error;
      actions.append(closer);
    }
  };
  show(book);
}

function sizeName(code) {
  const found = SIZES.find(([c]) => c === code);
  return found ? i18n.t(found[1]) : code;
}

/** Every book this person has made: download again, or delete. */
export async function openBooksList({ toast } = {}) {
  if (toast) toastFn = toast;
  const list = el('div', { class: 'book-list' });
  step(list);
  build().querySelector('h2').textContent = i18n.t('Books');
  build().hidden = false;
  let data;
  try {
    data = await api.books();
  } catch (err) {
    list.append(el('p', { class: 'hint', text: i18n.t('Could not load your books: {reason}', { reason: err.message }) }));
    return;
  }
  const books = data.books || [];
  if (!books.length) {
    list.append(el('p', { class: 'hint', text: i18n.t('No books yet. Open an album, a trip or a person and choose Make a book.') }));
    return;
  }
  for (const book of books) {
    const when = new Date((book.finished_at || book.created_at) * 1000).toLocaleDateString(i18n.locale());
    let state = '';
    if (book.state === 'building') state = i18n.t('Being made…');
    else if (book.state === 'done') state = i18n.t('{pages} pages', { pages: book.pages });
    else if (book.state === 'cancelled') state = i18n.t('Stopped');
    else state = i18n.t('Could not be made');
    const remove = el('button', { class: 'btn ghost small', type: 'button', text: i18n.t('Delete') });
    remove.onclick = async () => {
      if (!confirm(i18n.t('Delete the book “{title}”? The photographs are not touched.', { title: book.title }))) return;
      try {
        await api.deleteBook(book.id);
        row.remove();
      } catch (err) {
        toastFn(i18n.t('Could not delete the book: {reason}', { reason: err.message }), true);
      }
    };
    const open = book.state === 'building'
      ? el('button', { class: 'btn ghost small', type: 'button', text: i18n.t('Show progress') })
      : null;
    if (open) open.onclick = () => progress(book);
    const row = el('div', { class: 'book-row' },
      el('div', { class: 'book-row-text' }, el('b', { text: book.title }),
         el('span', { class: 'hint', text: [sizeName(book.size), state, when].join(' · ') })),
      el('div', { class: 'book-row-actions' },
         book.download ? Object.assign(downloadLink(book), { className: 'btn primary small' }) : open,
         remove));
    list.append(row);
  }
}
