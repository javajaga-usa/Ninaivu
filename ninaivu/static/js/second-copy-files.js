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

import { consoleJson as json } from './api.js';
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
  $$('#scf-where').textContent = i18n.t('On {folder}', { folder: state.folder });
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

  const dirs = $$('#scf-dirs');
  dirs.textContent = '';
  for (const dir of data.dirs) {
    const card = el('button', 'fold-card scf-dir');
    card.type = 'button';
    const body = el('div', 'fold-body');
    body.appendChild(el('div', 'fold-name', dir.name));
    card.title = dir.path;
    card.appendChild(body);
    card.onclick = () => load(dir.path);
    dirs.appendChild(card);
  }

  const list = $$('#scf-files');
  list.textContent = '';
  for (const file of data.files) {
    const row = el('label', `scf-file is-${file.is}`);
    const tick = el('input');
    tick.type = 'checkbox';
    tick.checked = state.chosen.has(file.path);
    tick.onchange = () => {
      if (tick.checked) state.chosen.add(file.path); else state.chosen.delete(file.path);
      renderPick();
    };
    const name = el('span', 'scf-name', file.name);
    name.title = file.library_path
      ? i18n.t('In the library: {path}', { path: file.library_path }) : file.path;
    if (file.path === state.here) row.classList.add('here');
    const open = el('a', 'scf-open', i18n.t('Open'));
    open.href = `/api/mirror/file?path=${encodeURIComponent(file.path)}`;
    open.target = '_blank';
    open.rel = 'noopener';
    open.onclick = (event) => event.stopPropagation();
    row.append(tick, name,
      el('span', 'scf-size', bytesShort(file.size)),
      el('span', 'scf-when', when(file.mtime)),
      el('span', 'scf-is', i18n.t(IS[file.is] || IS.other)),
      open);
    if (file.library_path && file.library_root === data.active_root) {
      const back = el('button', 'scf-tolib', i18n.t('Show in library'));
      back.type = 'button';
      back.onclick = (event) => {
        event.preventDefault();
        event.stopPropagation();
        const folder = file.library_path.includes('/')
          ? file.library_path.slice(0, file.library_path.lastIndexOf('/')) : '';
        hooks.openLibrary(folder);
      };
      row.appendChild(back);
    } else {
      row.appendChild(el('span'));
    }
    list.appendChild(row);
  }
  const toLib = $$('#scf-to-lib');
  toLib.hidden = data.library_folder === null || data.library_folder === undefined;
  toLib.onclick = () => hooks.openLibrary(data.library_folder || '');
  if (state.here) {
    list.querySelector('.scf-file.here')?.scrollIntoView({ block: 'center' });
  }
  const empty = $$('#scf-empty');
  empty.hidden = Boolean(data.dirs.length || data.files.length);
  empty.textContent = i18n.t('Nothing in this folder of the second copy.');
  if (data.more) {
    list.appendChild(el('p', 'hint subtle', i18n.t('{count} more are not listed here.', {
      count: data.more.toLocaleString(i18n.locale()) })));
  }
  renderPick();
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
