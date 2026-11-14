/**
 * The console's Large files page, loaded when it is first opened (admin.js,
 * openPage).
 */

import { admin, thumbUrl } from '../../api.js';
import * as i18n from '../../i18n.js';
import {
  $, $$, askPasswordToDelete, bytesShort, consoleToast as toast, el, lfDescribe, plural, span,
} from '../../ui.js';
import { revealItem } from './common.js';

const adminApi = {
  largeFiles: (minMb) => admin.getAdminLargeFiles({ query: { min_mb: minMb } }),
  keepLargeFiles: (ids) => admin.postAdminLargeFilesKeep({ body: { ids } }),
};

/* --- Large files ---------------------------------------------------------
 *
 * A worklist, not a filter: everything here still shows up everywhere else
 * in the library exactly as before. "Keep" only ever means "stop asking" —
 * see db.mark_large_files_reviewed — and "Delete" is the ordinary
 * password-gated /api/delete, the same one the gallery itself uses, so a
 * deleted file lands in the same recoverable _deleted folder either way.
 */

/* One set of buttons above the list acts on the rows that are ticked, rather
 * than buttons on every row. What is ticked survives a reload of the list,
 * so long as the file is still on it. */
const lfPick = { items: new Map(), chosen: new Set() };

function lfDuration(seconds) {
  if (!seconds) return '';
  const s = Math.round(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = String(Math.floor(s % 60)).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${sec}` : `${m}:${sec}`;
}

async function loadLargeFiles() {
  const list = $('#lf-list');
  const summary = $('#lf-summary');
  const empty = $('#lf-empty');
  const minMb = $('#lf-floor')?.value || 500;
  summary.textContent = i18n.t('Looking…');
  let data;
  try {
    data = await adminApi.largeFiles(minMb);
  } catch (exc) {
    toast(exc.message, true);
    summary.textContent = '';
    return;
  }
  list.innerHTML = '';
  lfPick.items = new Map(data.items.map((item) => [item.id, item]));
  lfPick.chosen = new Set([...lfPick.chosen].filter((id) => lfPick.items.has(id)));
  $('#lf-bar').hidden = !data.items.length;
  if (!data.items.length) {
    summary.textContent = '';
    empty.textContent = i18n.t('Nothing over {size} MB is waiting to be looked at.', { size: data.floor_mb });
    empty.hidden = false;
    lfRenderBar();
    return;
  }
  empty.hidden = true;
  summary.textContent =
    i18n.t('{files} over {size} MB, largest first.', {
      files: plural(data.items.length, i18n.key('1 file'), i18n.key('{count} files')), size: data.floor_mb });
  for (const item of data.items) list.appendChild(lfRow(item));
  lfRenderBar();
}

/** The ticked rows, in the order the list shows them (largest first). */
function lfChosen() {
  return [...lfPick.items.values()].filter((item) => lfPick.chosen.has(item.id));
}

function lfRenderBar() {
  const chosen = lfChosen();
  const n = chosen.length;
  const total = lfPick.items.size;
  $('#lf-selected').textContent = n
    ? plural(n, i18n.key('1 file chosen'), i18n.key('{count} files chosen'))
    : i18n.t('Nothing chosen');
  const all = $('#lf-all');
  all.checked = total > 0 && n === total;
  all.indeterminate = n > 0 && n < total;

  $('#lf-keep').disabled = n === 0;
  $('#lf-keep').title = i18n.t('Stop asking about these files');
  $('#lf-delete').disabled = n === 0;

  for (const row of document.querySelectorAll('.lf-row')) {
    const on = lfPick.chosen.has(Number(row.dataset.id));
    row.classList.toggle('chosen', on);
    const box = row.querySelector('.lf-check');
    if (box) box.checked = on;
  }
}

function lfChoose(id, on) {
  if (on) lfPick.chosen.add(id);
  else lfPick.chosen.delete(id);
  lfRenderBar();
}

function lfChooseAll() {
  const every = lfPick.items.size > 0 && lfPick.chosen.size === lfPick.items.size;
  lfPick.chosen = every ? new Set() : new Set(lfPick.items.keys());
  lfRenderBar();
}

async function lfKeepChosen() {
  const items = lfChosen();
  if (!items.length) return;
  const keep = $('#lf-keep');
  keep.disabled = true;
  try {
    await adminApi.keepLargeFiles(items.map((item) => item.id));
    for (const item of items) lfPick.chosen.delete(item.id);
    toast(items.length === 1
      ? i18n.t('Won’t ask about “{name}” again.', { name: items[0].filename })
      : i18n.t('Won’t ask about these {count} files again.', { count: items.length }));
    await loadLargeFiles();
  } catch (exc) {
    toast(exc.message, true);
    lfRenderBar();
  }
}

function lfRow(item) {
  const row = el('div', 'lf-row');
  row.dataset.id = item.id;

  const box = document.createElement('input');
  box.type = 'checkbox';
  box.className = 'lf-check';
  box.checked = lfPick.chosen.has(item.id);
  box.setAttribute('aria-label', item.filename);
  box.onchange = () => lfChoose(item.id, box.checked);
  row.appendChild(box);
  row.classList.toggle('chosen', box.checked);

  if (item.thumb) {
    const img = document.createElement('img');
    img.className = 'lf-thumb';
    img.src = thumbUrl(item.id, 160, item.thumb_v);
    img.alt = '';
    img.loading = 'lazy';
    img.title = i18n.t('Show this file where it lives');
    img.onclick = () => revealItem(item);
    row.appendChild(img);
  } else {
    const placeholder = el('div', 'lf-thumb lf-thumb-empty', item.kind === 'video' ? '▶' : '');
    row.appendChild(placeholder);
  }

  const info = el('div', 'lf-info');
  info.appendChild(el('div', 'lf-name', item.filename));
  const meta = el('div', 'lf-meta');
  meta.appendChild(span(bytesShort(item.size)));
  if (item.duration) meta.appendChild(span(lfDuration(item.duration)));
  meta.appendChild(span(item.folder || i18n.t('(library root)')));
  if (item.no_camera) meta.appendChild(el('span', 'tag warn', i18n.t('no camera info')));
  if (item.damaged) meta.appendChild(el('span', 'tag warn', i18n.t('damaged')));
  info.appendChild(meta);
  if (item.damaged) {
    info.appendChild(el('div', 'lf-job bad', i18n.t('This video is damaged at its source: the file was cut short, so its index is missing and it cannot be played. Keep it or delete it; only an older copy from elsewhere can bring it back.')));
  }
  row.appendChild(info);

  // Anywhere on the row ticks it, except the picture (which opens the file).
  row.addEventListener('click', (event) => {
    if (event.target.closest('.lf-thumb, button, input, a')) return;
    lfChoose(item.id, !lfPick.chosen.has(item.id));
  });

  return row;
}

/** Delete every ticked file: one password, one request, all into the bin. */
function lfDeleteChosen() {
  const items = lfChosen();
  if (!items.length) return;
  askPasswordToDelete(items, async (result) => {
    for (const item of items) lfPick.chosen.delete(item.id);
    const failed = (result.failed || []).length;
    if (failed) {
      toast(i18n.t('{count} could not be moved to the bin.', { count: failed }), true);
    } else {
      toast(items.length === 1
        ? i18n.t('“{name}” moved to the bin.', { name: items[0].filename })
        : i18n.t('{count} files moved to the bin.', { count: items.length }));
    }
    await loadLargeFiles();
  });
}

/** Ask for the password, then move *items* to the bin; *done* gets the
 *  server's answer. Shared by Large files and Folders. With `password` off
 *  (the Folders screen) it only asks to confirm: the files go to the bin,
 *  where Recently deleted puts them back. */

/** Bind the page's controls: once, when it is first opened. */
export function wire() {
  $$('#lf-refresh').onclick = () => loadLargeFiles();
  $$('#lf-floor').onchange = () => loadLargeFiles();
  $$('#lf-all').onchange = () => lfChooseAll();
  $$('#lf-keep').onclick = () => lfKeepChosen();
  $$('#lf-delete').onclick = () => lfDeleteChosen();
}

/** The page has been opened, or the language changed while it is open. */
export function show() {
  return loadLargeFiles();
}
