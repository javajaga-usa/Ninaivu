/**
 * Folders → Second copy: what is on the other disk (storage/mirror_place.py).
 *
 * Shown only while the second copy is on. Its folders and files as they are
 * there, each file with what it is: Ninaivu's copy of a library file, one
 * found there already with the same bytes, one a check found changed, or a
 * file Ninaivu knows nothing about. A file opens read-only in a new tab, and
 * an admin can delete chosen files after their password; the copy itself
 * never deletes anything.
 */

import { consoleJson as json, thumbUrl } from './api.js';
import * as i18n from './i18n.js';
import { $$, askPasswordToDelete, bytesShort, consoleToast as toast, el } from './ui.js';

const state = { path: '', data: null, chosen: new Set(), on: false, here: '', folder: '' };

/** Folders page hooks (folders.js): told when the second copy is on or off,
 *  and asked to open a folder of the library. */
let hooks = { onState: () => {}, openLibrary: () => {} };

//: Translated where the list is drawn, not here: see i18n.key().
const IS = {
  copied: i18n.key('Copy of a library file'),
  found: i18n.key('Was there already, same bytes'),
  bad: i18n.key('Changed since copied; copied again next run'),
  other: i18n.key('Not from the library'),
};

const when = (seconds) => (seconds
  ? new Date(seconds * 1000).toLocaleDateString(i18n.locale(), { dateStyle: 'medium' })
  : '');

/** Whether the second copy is on, so the page offers it. */
export async function check() {
  try {
    const status = await json('/api/mirror/status');
    state.on = Boolean(status.enabled && status.folder);
    state.folder = status.folder || '';
  } catch {
    state.on = false;                   // not an admin, or no second copy here
  }
  hooks.onState(state.on);
  return state.on;
}

export const isOn = () => state.on;

async function load(path = state.path, { here = '' } = {}) {
  if (!$$('#scf-block')) return;
  state.here = here;
  if (!await check()) return;
  if (path !== state.path) state.chosen.clear();
  $$('#scf-where').textContent = i18n.t('On {folder}.', { folder: state.folder });
  try {
    state.data = await json(`/api/mirror/browse?path=${encodeURIComponent(path || '')}`);
    state.path = state.data.path;
  } catch (exc) {
    state.data = null;
    state.chosen.clear();
    renderAway(exc.message);
    return;
  }
  state.chosen = new Set([...state.chosen].filter(
    (p) => state.data.files.some((file) => file.path === p)));
  render();
}

function renderAway(message) {
  $$('#scf-trail').textContent = '';
  $$('#scf-dirs').textContent = '';
  $$('#scf-files').textContent = '';
  $$('#scf-pickbar').hidden = true;
  const empty = $$('#scf-empty');
  empty.textContent = message;
  empty.hidden = false;
}

function render() {
  const data = state.data;
  if (!data) return;
  const trail = $$('#scf-trail');
  trail.textContent = '';
  const steps = [{ name: i18n.t('Second copy'), path: '' }];
  let walked = '';
  for (const part of (data.path ? data.path.split('/') : [])) {
    walked = walked ? `${walked}/${part}` : part;
    steps.push({ name: part, path: walked });
  }
  steps.forEach((step, index) => {
    if (index) trail.append(el('span', 'sep', '/'));
    const button = el('button', index === steps.length - 1 ? 'here' : '', step.name);
    button.type = 'button';
    button.onclick = () => load(step.path);
    trail.appendChild(button);
  });

  // The copy's own folders, as the library's are drawn (folders.js): a
  // picture from inside, how many of the library's files are there and
  // their size, from the copy's record.
  const dirs = $$('#scf-dirs');
  dirs.textContent = '';
  for (const dir of data.dirs) dirs.appendChild(dirCard(dir));
  dirs.hidden = !data.dirs.length;

  const files = data.files;
  const summary = $$('#scf-summary');
  summary.textContent = '';
  const inDirs = data.dirs.reduce((sum, dir) => sum + (dir.n || 0), 0);
  const kinds = { copied: 0, found: 0, bad: 0, other: 0 };
  for (const file of files) kinds[file.is in kinds ? file.is : 'other'] += 1;
  const piece = (text, cls = '') => summary.appendChild(el('span', cls, text));
  piece(i18n.t('{count} folders', { count: number(data.dirs.length) }));
  piece(i18n.t('{count} files', { count: number(files.length + inDirs) }));
  const bytes = files.reduce((sum, file) => sum + (file.size || 0), 0)
    + data.dirs.reduce((sum, dir) => sum + (dir.bytes || 0), 0);
  if (bytes) piece(bytesShort(bytes));
  if (kinds.found) piece(i18n.t('{count} were there already', { count: number(kinds.found) }));
  if (kinds.bad) piece(i18n.t('{count} changed since copied', { count: number(kinds.bad) }), 'hidden-n');
  if (kinds.other) piece(i18n.t('{count} not from the library', { count: number(kinds.other) }), 'hidden-n');
  summary.hidden = !(data.dirs.length || files.length);

  const head = $$('#scf-files-head');
  head.hidden = !files.length;
  $$('#scf-files-title').textContent = files.length === 1 ? i18n.t('1 file here')
    : i18n.t('{count} files here', { count: number(files.length) });
  $$('#scf-files-note').textContent = data.more
    ? i18n.t('{count} more are not listed here.', { count: number(data.more) }) : '';

  const list = $$('#scf-files');
  list.textContent = '';
  for (const file of files) list.appendChild(fileTile(file, data));
  const toLib = $$('#scf-to-lib');
  toLib.hidden = data.library_folder === null || data.library_folder === undefined;
  toLib.onclick = () => hooks.openLibrary(data.library_folder || '');
  if (state.here) {
    list.querySelector('.fold-item.here')?.scrollIntoView({ block: 'center' });
  }
  const empty = $$('#scf-empty');
  empty.hidden = Boolean(data.dirs.length || data.files.length);
  empty.textContent = i18n.t('Nothing in this folder of the second copy.');
  renderPick();
}

const number = (n) => (Number(n) || 0).toLocaleString(i18n.locale());

const FOLDER_ICON =
  '<svg viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/></svg>';

/** The library's thumbnail of *id* in *holder*, over the plain mark already
 * there; one the library no longer has leaves the mark showing. */
function picture(id, holder) {
  const img = document.createElement('img');
  img.src = thumbUrl(id, 256);
  img.alt = '';
  img.loading = 'lazy';
  img.className = 'scf-thumb';
  img.onerror = () => { img.hidden = true; };
  holder.appendChild(img);
}

function dirCard(dir) {
  const card = el('button', 'fold-card scf-dir');
  card.type = 'button';
  card.title = dir.path;
  card.onclick = () => load(dir.path);
  const cover = el('div', 'fold-cover');
  cover.innerHTML = FOLDER_ICON;
  if (dir.cover) picture(dir.cover, cover);
  card.appendChild(cover);
  const body = el('div', 'fold-body');
  body.appendChild(el('div', 'fold-name', dir.name));
  const meta = el('div', 'fold-meta');
  if (dir.n) {
    meta.appendChild(el('span', '', i18n.t('{count} files', { count: number(dir.n) })));
    if (dir.bytes) meta.appendChild(el('span', '', bytesShort(dir.bytes)));
  } else {
    meta.appendChild(el('span', '', i18n.t('No library files')));
  }
  body.appendChild(meta);
  card.appendChild(body);
  return card;
}

//: The mark on a tile that is not simply Ninaivu's copy of a library file.
const BADGE = {
  found: i18n.key('There already'),
  bad: i18n.key('Changed'),
  other: i18n.key('Not from the library'),
};

function fileTile(file, data) {
  const tile = el('div', `fold-item scf-item is-${file.is}`);
  if (state.chosen.has(file.path)) tile.classList.add('chosen');
  if (file.path === state.here) tile.classList.add('here');
  tile.title = file.library_path
    ? i18n.t('In the library: {path}', { path: file.library_path }) : file.path;

  // Opens read-only in a new tab (the server says which kinds a browser may show).
  const open = el('a', 'fold-open');
  open.href = `/api/mirror/file?path=${encodeURIComponent(file.path)}`;
  open.target = '_blank';
  open.rel = 'noopener';
  open.setAttribute('aria-label', i18n.t('Open {name}', { name: file.name }));
  const ext = file.name.includes('.') ? file.name.slice(file.name.lastIndexOf('.') + 1) : '';
  open.appendChild(el('span', 'fold-nothumb', ext.toUpperCase().slice(0, 5)));
  if (file.id && (file.kind === 'picture' || file.kind === 'video')) picture(file.id, open);
  if (file.kind === 'video') open.appendChild(el('span', 'fold-play', '▶'));
  tile.appendChild(open);

  const pick = document.createElement('input');
  pick.type = 'checkbox';
  pick.className = 'fold-pick';
  pick.checked = state.chosen.has(file.path);
  pick.setAttribute('aria-label', i18n.t('Choose {name}', { name: file.name }));
  pick.onchange = () => {
    if (pick.checked) state.chosen.add(file.path); else state.chosen.delete(file.path);
    tile.classList.toggle('chosen', pick.checked);
    renderPick();
  };
  tile.appendChild(pick);

  if (BADGE[file.is]) {
    const badges = el('div', 'fold-badges');
    badges.appendChild(el('span', `badge scf-badge-${file.is}`, i18n.t(BADGE[file.is])));
    tile.appendChild(badges);
  }
  tile.appendChild(el('div', 'name', file.name));
  const meta = el('div', 'where', `${bytesShort(file.size)} · ${when(file.mtime)}`);
  meta.title = i18n.t(IS[file.is] || IS.other);
  tile.appendChild(meta);
  if (file.library_path && file.library_root === data.active_root) {
    const back = el('button', 'scf-tolib', i18n.t('Show in library'));
    back.type = 'button';
    back.onclick = () => {
      const folder = file.library_path.includes('/')
        ? file.library_path.slice(0, file.library_path.lastIndexOf('/')) : '';
      hooks.openLibrary(folder);
    };
    tile.appendChild(back);
  }
  return tile;
}

function renderPick() {
  const bar = $$('#scf-pickbar');
  const files = state.data?.files || [];
  bar.hidden = !files.length;
  const count = state.chosen.size;
  $$('#scf-picked').textContent = count
    ? i18n.t('{count} chosen', { count: count.toLocaleString(i18n.locale()) })
    : i18n.t('Nothing chosen');
  $$('#scf-delete').disabled = !count;
}

function deleteChosen() {
  const files = (state.data?.files || []).filter((file) => state.chosen.has(file.path));
  if (!files.length) return;
  const items = files.map((file) => ({ id: file.path, filename: file.name, size: file.size }));
  askPasswordToDelete(items, async (result) => {
    state.chosen.clear();
    const failed = (result.failed || []).length;
    const done = (result.deleted || []).length;
    if (failed) {
      toast(i18n.t('{count} could not be deleted from the second copy.', { count: failed }), true);
    }
    if (done) {
      toast(result.copied_again
        ? i18n.t('Deleted {count} from the second copy. {again} of them are still in the library and are copied again on the next run.', {
          count: done, again: result.copied_again })
        : i18n.t('Deleted {count} from the second copy.', { count: done }));
    }
    await load(state.path);
  }, {
    send: (ids, password) => json('/api/mirror/delete', {
      method: 'POST', body: { paths: ids, password } }),
    note: i18n.t('This deletes the file on the second copy\'s disk for good. The library is not touched. A file still in the library is copied again on the next run.'),
  });
}

/** Open the second copy where a library folder (or one file) is. */
export async function showLibrary({ folder = '', id = null } = {}) {
  const query = id !== null ? `id=${encodeURIComponent(id)}` : `folder=${encodeURIComponent(folder)}`;
  let place;
  try {
    place = await json(`/api/mirror/where?${query}`);
  } catch (exc) {
    toast(exc.message, true);
    return;
  }
  if (id !== null && !place.copied) {
    toast(i18n.t('This file is not on the second copy yet. It is copied on the next run.'));
  }
  await load(place.path, { here: place.file });
}

export function wire(given = {}) {
  hooks = { ...hooks, ...given };
  if (!$$('#scf-block')) return;
  $$('#scf-refresh').onclick = () => load(state.path);
  $$('#scf-delete').onclick = deleteChosen;
}

export { load };

export function redraw() {
  if (state.data) render();
}
