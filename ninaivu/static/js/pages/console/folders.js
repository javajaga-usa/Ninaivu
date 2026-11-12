/**
 * The console's Folders page: the library as its folders, each one's rule
 * and photographs, the viewer, and choosing, hiding, flagging and deleting
 * from there. Loaded when the page is first opened (admin.js, openPage).
 * Folder visibility's ask-twice and undo stay the Visibility page's, reached
 * through the shell.
 */

import { consoleJson as json, thumbUrl } from '../../api.js';
import * as i18n from '../../i18n.js';
import { keepInApp } from '../../save-file.js';
import {
  $, $$, askPasswordToDelete, bytesShort, consoleToast as toast, el, plural, span,
} from '../../ui.js';
import { labelled, revealItem, VIS_NAMES_BY_VALUE, visibilityWord } from './common.js';

const folderApi = {
  folder: (path, show = 'all', deep = false, offset = 0) =>
    json(`/api/admin/folder?folder=${encodeURIComponent(path || '')}`
      + `&show=${encodeURIComponent(show)}${deep ? '&deep=1' : ''}`
      + `${offset ? `&offset=${offset}` : ''}`),
  clearFolderVisibility: (folder, confirm) =>
    json(`/api/visibility/folder?folder=${encodeURIComponent(folder)}${confirm ? '&confirm=1' : ''}`,
      { method: 'DELETE' }),
  setItemsVisibility: (ids, visibility) =>
    json('/api/visibility', { method: 'POST', body: { ids, visibility } }),
  setExplicit: (ids, nsfw) =>
    json('/api/assets/bulk', { method: 'POST', body: { ids, nsfw } }),
  revealFolder: (folder) =>
    json('/api/admin/reveal-folder', { method: 'POST', body: { folder } }),
};

/** The console around the page (admin.js). */
let shell;

/* --- the folder screen -------------------------------------------------- */

/* The Folders screen shows everything in the library, hidden and explicit
 * included, because it is where an admin goes to check those and act on
 * them: open a file full size, hide it or show it to the family again, flag
 * it explicit or clear the flag, move it to the bin, and hide a whole folder
 * or stop hiding it. Nothing here needs the computer Ninaivu runs on — the
 * file manager was the only way to see a file larger than its tile, and that
 * opened on the Mac, not in front of whoever was looking. */
const foldState = {
  path: '', show: 'all', deep: false, data: null, items: [],
  chosen: new Set(), viewing: -1,
};

// The kinds the Folders screen narrows to, in the server's words. The counts
// on each are for the open folder and everything beneath it.
// Each is [kind, the chip's label, how many of them, what an empty folder says],
// marked here and translated where they are shown.
const FOLD_FILTERS = [
  ['all', i18n.key('All'), i18n.key('{count} files'), i18n.key('This folder has nothing indexed in it.')],
  ['picture', i18n.key('Photos'), i18n.key('{count} photos'), i18n.key('No photos in here.')],
  ['video', i18n.key('Videos'), i18n.key('{count} videos'), i18n.key('No videos in here.')],
  ['audio', i18n.key('Audio'), i18n.key('{count} audio files'), i18n.key('No audio in here.')],
  ['screen', i18n.key('Screenshots & documents'), i18n.key('{count} screenshots and documents'),
    i18n.key('No screenshots or documents in here.')],
  ['hidden', i18n.key('Hidden'), i18n.key('{count} hidden files'), i18n.key('Nothing in here is hidden.')],
  ['explicit', i18n.key('Explicit'), i18n.key('{count} explicit files'),
    i18n.key('Nothing in here is flagged as explicit.')],
];

const FOLDER_ICON =
  '<svg viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/></svg>';

/** Open *path*. With `keep`, read the same folder again as far down as it
 *  had been shown, so an action on page three does not snap back to page one. */
async function loadFolderScreen(path = '', { keep = false } = {}) {
  const same = keep && path === foldState.path;
  const wanted = same ? Math.max(foldState.items.length, 1) : 1;
  let data;
  const items = [];
  try {
    do {
      data = await folderApi.folder(path || '', foldState.show, foldState.deep, items.length);
      items.push(...(data.items || []));
    } while (items.length < wanted && items.length < (data.items_total || 0)
             && (data.items || []).length);
  } catch (exc) {
    toast(exc.message, true);
    return;
  }
  foldState.path = path || '';
  foldState.data = data;
  foldState.items = items;
  const ids = new Set(items.map((item) => item.id));
  foldState.chosen = new Set(same ? [...foldState.chosen].filter((id) => ids.has(id)) : []);
  renderFolderScreen();
}

async function loadMoreFiles() {
  const button = $('#fold-more');
  button.disabled = true;
  try {
    const data = await folderApi.folder(foldState.path, foldState.show, foldState.deep,
      foldState.items.length);
    foldState.items.push(...(data.items || []));
    foldState.data = { ...data, items: foldState.items };
    renderFolderScreen();
  } catch (exc) {
    toast(exc.message, true);
  } finally {
    button.disabled = false;
  }
}

function renderFolderScreen() {
  const data = foldState.data;
  if (!data) return;

  /* breadcrumb */
  const trail = $('#fold-trail');
  trail.innerHTML = '';
  data.trail.forEach((step, index) => {
    if (index) trail.append(el('span', 'sep', '/'));
    const button = el('button', index === data.trail.length - 1 ? 'here' : '', step.name);
    button.type = 'button';
    button.onclick = () => loadFolderScreen(step.path);
    trail.appendChild(button);
  });

  renderFolderFilters(data);
  renderFolderRule(data);
  const filter = FOLD_FILTERS.find(([key]) => key === (data.show || 'all')) || FOLD_FILTERS[0];

  /* what is in here, with the hidden and explicit counts called out */
  const counts = data.counts || {};
  const total = (data.children || []).reduce((sum, c) => sum + c.n, 0) + (counts.n || 0);
  const hidden = data.total_hidden || 0;
  const explicit = data.total_explicit || 0;
  const summary = $('#fold-summary');
  summary.innerHTML = '';
  summary.append(
    span(i18n.t('{count} folders', { count: (data.children || []).length.toLocaleString() })),
    span(i18n.t(filter[2], { count: total.toLocaleString() })),
  );
  if (hidden) {
    const mark = el('span', 'hidden-n');
    mark.append(el('span', 'n', hidden.toLocaleString()),
                document.createTextNode(` ${i18n.t('only admins can see')}`));
    summary.appendChild(mark);
  } else {
    summary.append(span(i18n.t('nothing hidden in here')));
  }
  if (explicit) {
    const mark = el('span', 'explicit-n');
    mark.append(el('span', 'n', explicit.toLocaleString()),
                document.createTextNode(` ${i18n.t('flagged explicit')}`));
    summary.appendChild(mark);
  }

  /* subfolders */
  const grid = $('#fold-children');
  grid.innerHTML = '';
  const children = data.children || [];
  for (const child of children) grid.appendChild(folderCard(child));

  /* the files: directly in this folder, or everything beneath it */
  const items = foldState.items;
  const shown = items.length;
  const totalItems = data.items_total ?? shown;
  const head = $('#fold-items-head');
  head.hidden = shown === 0;
  if (shown) {
    const where = data.deep ? i18n.t('in here and every folder inside')
      : '';
    $('#fold-items-title').textContent = totalItems === 1 ? i18n.t('1 file here')
      : i18n.t('{count} files here', { count: totalItems.toLocaleString() });
    const hiddenHere = items.filter((i) => i.visibility_name === 'hidden').length;
    const notes = [];
    if (where) notes.push(where);
    if (shown < totalItems) {
      notes.push(i18n.t('showing {shown} of {total}',
        { shown: shown.toLocaleString(), total: totalItems.toLocaleString() }));
    }
    if (hiddenHere) {
      notes.push(i18n.t('{count} of them are admin-only', { count: hiddenHere.toLocaleString() }));
    }
    $('#fold-items-note').textContent = notes.join(' · ');
  }
  const list = $('#fold-items');
  list.innerHTML = '';
  items.forEach((item, index) => list.appendChild(folderItem(item, index)));
  const more = $('#fold-more');
  more.hidden = shown >= totalItems;
  more.textContent = i18n.t('Show {count} more', {
    count: Math.min(data.page || 200, totalItems - shown).toLocaleString() });

  const empty = $('#fold-empty');
  if (!children.length && !items.length) {
    empty.textContent = i18n.t(filter[3]);
    empty.hidden = false;
  } else {
    empty.hidden = true;
  }
  renderFoldPick();
}

function renderFolderFilters(data) {
  const box = $('#fold-filters');
  if (!box) return;
  box.innerHTML = '';
  const kinds = data.kinds || {};
  const current = data.show || 'all';
  for (const [key, label] of FOLD_FILTERS) {
    // Explicit is only worth a chip when something was flagged.
    if (key === 'explicit' && !kinds.explicit && current !== 'explicit') continue;
    const chip = el('button', `chip${key === current ? ' active' : ''}`);
    if (key === 'hidden' || key === 'explicit') chip.classList.add(`chip-${key}`);
    chip.type = 'button';
    chip.setAttribute('aria-pressed', String(key === current));
    chip.append(document.createTextNode(i18n.t(label)),
      el('span', 'n', (kinds[key] || 0).toLocaleString()));
    chip.onclick = () => {
      if (foldState.show === key) return;
      foldState.show = key;
      // Hidden and explicit files are found across a whole year at a time,
      // not one day's folder after another.
      if ((key === 'hidden' || key === 'explicit') && !foldState.deep) {
        foldState.deep = true;
        $('#fold-deep').checked = true;
      }
      loadFolderScreen(foldState.path);
    };
    box.appendChild(chip);
  }
}

/* Who can see this folder, and the one button that changes it: hide it, or
 * stop hiding it. A folder hidden because a folder above it is says which,
 * and opens it, since that is where the rule can be taken off. */
function renderFolderRule(data) {
  const bar = $('#fold-rule');
  bar.replaceChildren();
  bar.className = 'fold-rule';
  const rule = data.rule;
  const here = Boolean(rule) && rule.at === data.folder;
  const hidden = Boolean(rule) && rule.visibility === 2;
  if (hidden) bar.classList.add('is-hidden');
  const text = el('div', 'fold-rule-text');
  if (!data.folder) {
    text.append(el('strong', '', i18n.t('The whole library')),
      el('span', 'hint', i18n.t('Open a folder to hide it, or choose files below.')));
    bar.appendChild(text);
    if (data.local) bar.appendChild(el('div', 'fold-rule-actions')).appendChild(revealFolderButton(data));
    return;
  }
  const level = rule ? visibilityWord(VIS_NAMES_BY_VALUE[rule.visibility]) : '';
  if (!rule) {
    text.append(el('strong', '', i18n.t('Not hidden')),
      el('span', 'hint', i18n.t('This folder follows the family rules. Hiding it keeps everything in it, and anything added later, for admins only.')));
  } else if (here) {
    text.append(el('strong', '', hidden ? i18n.t('Hidden folder')
      : i18n.t('Folder rule: {level}', { level })),
    el('span', 'hint', hidden
      ? i18n.t('Only admins see what is in here, including anything added later.')
      : i18n.t('Set on this folder.')));
  } else {
    text.append(el('strong', '', hidden ? i18n.t('Hidden by “{folder}”', { folder: rule.at })
      : i18n.t('Folder rule: {level} (from “{folder}”)', { level, folder: rule.at })),
    el('span', 'hint', i18n.t('The rule is set on a folder above this one.')));
  }
  bar.appendChild(text);

  const actions = el('div', 'fold-rule-actions');
  if (data.local) actions.appendChild(revealFolderButton(data));
  if (rule && !here) {
    const open = el('button', 'btn ghost small', i18n.t('Open “{folder}”', { folder: rule.at.split('/').pop() }));
    open.type = 'button';
    open.onclick = () => loadFolderScreen(rule.at);
    actions.appendChild(open);
  }
  if (here) {
    const clear = el('button', 'btn small', hidden ? i18n.t('Stop hiding this folder')
      : i18n.t('Remove this folder rule'));
    clear.type = 'button';
    clear.id = 'fold-unhide-folder';
    clear.onclick = () => clearFolderRule(data.folder);
    actions.appendChild(clear);
  }
  if (!hidden) {
    const hide = el('button', 'btn danger small', i18n.t('Hide this folder'));
    hide.type = 'button';
    hide.id = 'fold-hide-folder';
    hide.onclick = async () => {
      await shell.applyFolderVisibility(data.folder, 'hidden');
      await loadFolderScreen(foldState.path, { keep: true });
    };
    actions.appendChild(hide);
  }
  bar.appendChild(actions);
}

/** At the Ninaivu computer: open this folder in Finder or Explorer, where
 *  the files really are. Elsewhere the button is not shown at all. */
function revealFolderButton(data) {
  const button = el('button', 'btn ghost small', i18n.t('Show in Finder or Explorer'));
  button.type = 'button';
  button.id = 'fold-reveal-folder';
  button.title = data.path || '';
  button.onclick = async () => {
    try {
      await folderApi.revealFolder(data.folder);
    } catch (exc) { toast(exc.message, true); }
  };
  return button;
}

/** "Stop hiding": the rule comes off and its files follow whatever covers
 *  them without it. Showing anything to more people is asked about first. */
async function clearFolderRule(path) {
  try {
    let result;
    try {
      result = await folderApi.clearFolderVisibility(path, false);
    } catch (exc) {
      if (!(exc.status === 409 && exc.data?.needs_confirmation)) throw exc;
      if (!await shell.confirmExposure(exc.data.impact, path, null)) return;
      result = await folderApi.clearFolderVisibility(path, true);
    }
    toast(i18n.t('“{folder}” is no longer hidden. {items} changed.',
      { folder: path, items: i18n.items(result.updated) }));
    await shell.overviewChanged();
    await shell.visibilityChanged();
    await loadFolderScreen(foldState.path, { keep: true });
  } catch (exc) { toast(exc.message, true); }
}

function folderCard(child) {
  const card = el('button', 'fold-card');
  card.type = 'button';
  // Entirely private is worth seeing before opening it.
  if (child.n && child.hidden === child.n) card.classList.add('all-hidden');
  card.onclick = () => loadFolderScreen(child.path);

  const cover = el('div', 'fold-cover');
  if (child.cover) {
    const img = document.createElement('img');
    img.src = thumbUrl(child.cover, 256, child.cover_v);
    img.alt = '';
    img.loading = 'lazy';
    cover.appendChild(img);
  } else {
    cover.innerHTML = FOLDER_ICON;
  }
  if (child.rule === 'hidden') cover.appendChild(el('span', 'fold-flag', i18n.t('Hidden folder')));
  card.appendChild(cover);

  const body = el('div', 'fold-body');
  body.appendChild(el('div', 'fold-name', child.name));

  const bar = el('div', 'fold-bar');
  for (const [key, cls] of [['public', 'b-public'], ['family', 'b-family'],
                            ['hidden', 'b-hidden']]) {
    if (!child[key]) continue;
    const piece = el('i', cls);
    piece.style.width = `${(child[key] / child.n) * 100}%`;
    bar.appendChild(piece);
  }
  body.appendChild(bar);

  const meta = el('div', 'fold-meta');
  meta.appendChild(span(i18n.t('{count} files', { count: child.n.toLocaleString() })));
  if (child.bytes) meta.appendChild(span(bytesShort(child.bytes)));
  if (child.hidden) {
    meta.appendChild(el('span', 'hid', i18n.t('{count} hidden', { count: child.hidden.toLocaleString() })));
  }
  if (child.explicit) {
    meta.appendChild(el('span', 'xp', i18n.t('{count} explicit', { count: child.explicit.toLocaleString() })));
  }
  body.appendChild(meta);

  card.appendChild(body);
  card.title = child.hidden
    ? `${child.path} — ${i18n.t('{hidden} of {total} visible to admins only', { hidden: child.hidden, total: child.n })}`
    : child.path;
  return card;
}

const VIS_WHY = {
  hidden: i18n.key('from disk'),
  folder: i18n.key('folder rule'),
  item: i18n.key('set here'),
  screen: i18n.key('screenshot or document'),
  private: i18n.key('only to its owner'),
  screening: i18n.key('held while an admin screens it'),
  kind: i18n.key('audio stays admin-only'),
  default: '',
};

function folderItem(item, index) {
  const tile = el('div', 'fold-item');
  tile.dataset.id = item.id;
  const isHidden = item.visibility_name === 'hidden';
  if (isHidden) tile.classList.add('is-hidden');
  if (item.explicit) tile.classList.add('is-explicit');
  if (foldState.chosen.has(item.id)) tile.classList.add('chosen');

  const open = el('button', 'fold-open');
  open.type = 'button';
  open.setAttribute('aria-label', i18n.t('Open {name}', { name: item.filename }));
  if (item.thumb) {
    const img = document.createElement('img');
    img.src = thumbUrl(item.id, 256, item.thumb_v);
    img.alt = '';
    img.loading = 'lazy';
    open.appendChild(img);
  } else {
    open.appendChild(el('span', 'fold-nothumb', (item.kind || '').toUpperCase()));
  }
  if (item.kind === 'video') open.appendChild(el('span', 'fold-play', '▶'));
  open.onclick = () => openFoldViewer(index);
  tile.appendChild(open);

  const pick = document.createElement('input');
  pick.type = 'checkbox';
  pick.className = 'fold-pick';
  pick.checked = foldState.chosen.has(item.id);
  pick.setAttribute('aria-label', i18n.t('Choose {name}', { name: item.filename }));
  pick.onchange = () => {
    if (pick.checked) foldState.chosen.add(item.id); else foldState.chosen.delete(item.id);
    tile.classList.toggle('chosen', pick.checked);
    renderFoldPick();
  };
  tile.appendChild(pick);

  const badges = el('div', 'fold-badges');
  if (isHidden) badges.appendChild(el('span', 'badge', i18n.t('Hidden')));
  if (item.explicit) badges.appendChild(el('span', 'badge xp', i18n.t('Explicit')));
  if (badges.childElementCount) tile.appendChild(badges);

  const caption = el('div', 'name', item.filename);
  tile.appendChild(caption);
  if (foldState.deep && item.folder !== foldState.path) {
    tile.appendChild(el('div', 'where', item.folder || i18n.t('the library')));
  }
  const why = isHidden ? VIS_WHY[item.vis_source] : '';
  if (why) tile.appendChild(el('div', 'why', i18n.t(why)));
  tile.title = `${item.filename} — ${visibilityWord(item.visibility_name)}`;
  return tile;
}

/* -- choosing files, and what can be done with them ------------------------ */

function foldChosenItems() {
  return foldState.items.filter((item) => foldState.chosen.has(item.id));
}

function renderFoldPick() {
  const bar = $('#fold-pickbar');
  const chosen = foldChosenItems();
  bar.hidden = !foldState.items.length;
  $('#fold-picked').textContent = chosen.length
    ? plural(chosen.length, i18n.key('1 file chosen'), i18n.key('{count} files chosen'))
    : i18n.t('Nothing chosen');
  for (const id of ['#fold-act-hide', '#fold-act-show', '#fold-act-explicit',
    '#fold-act-clean', '#fold-act-delete', '#fold-act-none']) {
    $(id).disabled = !chosen.length;
  }
  const all = $('#fold-act-all');
  all.textContent = chosen.length === foldState.items.length && chosen.length
    ? i18n.t('Choose none') : i18n.t('Choose all');
}

function foldChooseAll() {
  if (foldState.chosen.size === foldState.items.length) foldState.chosen.clear();
  else foldState.chosen = new Set(foldState.items.map((item) => item.id));
  renderFolderScreen();
}

/** Hide (admins only) or show to the family again. */
async function foldSetVisibility(items, level) {
  if (!items.length) return;
  try {
    const result = await folderApi.setItemsVisibility(items.map((item) => item.id), level);
    const still = items.filter((item) => item.private).length;
    let message = level === 'hidden'
      ? i18n.t('{items} hidden. Only admins can see them now.', { items: i18n.items(items.length) })
      : i18n.t('{items} shown to the family again.', { items: i18n.items(items.length) });
    if (result.note) message += ` ${i18n.fromServer({ error: result.note }, result.note)}`;
    if (level !== 'hidden' && still) {
      message += ` ${i18n.t('Files somebody keeps “Only to me” stay hidden until they let them go.')}`;
    }
    toast(message);
  } catch (exc) {
    toast(exc.message, true);
    return;
  }
  await afterFoldChange();
}

/** Flag as explicit, or clear the flag. A flag set by hand is never put back
 *  by the model on a later pass. */
async function foldSetExplicit(items, explicit) {
  if (!items.length) return;
  try {
    await folderApi.setExplicit(items.map((item) => item.id), explicit);
    toast(explicit
      ? i18n.t('{items} flagged explicit. Only admins can see them.', { items: i18n.items(items.length) })
      : i18n.t('{items} no longer flagged explicit.', { items: i18n.items(items.length) }));
  } catch (exc) {
    toast(exc.message, true);
    return;
  }
  await afterFoldChange();
}

async function afterFoldChange() {
  await loadFolderScreen(foldState.path, { keep: true });
  if (foldState.viewing >= 0) showFoldViewerItem();
  shell.refreshUndo();
}

/* -- the viewer: a file full size, and the same actions on it -------------- */

function openFoldViewer(index) {
  foldState.viewing = index;
  const modal = $('#fold-viewer');
  modal.hidden = false;
  showFoldViewerItem();
  $('#fv-close').focus();
}

function closeFoldViewer() {
  const modal = $('#fold-viewer');
  modal.hidden = true;
  foldState.viewing = -1;
  $('#fv-stage').replaceChildren();          // stops a video that was playing
}

function stepFoldViewer(by) {
  const next = foldState.viewing + by;
  if (next < 0 || next >= foldState.items.length) return;
  foldState.viewing = next;
  showFoldViewerItem();
}

function showFoldViewerItem() {
  const item = foldState.items[foldState.viewing];
  if (!item) { closeFoldViewer(); return; }
  const stage = $('#fv-stage');
  stage.replaceChildren();
  if (item.kind === 'video' || item.kind === 'audio') {
    foldPlay(stage, item);
  } else {
    const img = document.createElement('img');
    img.alt = item.filename;
    // The thumbnail first, so there is something to look at at once.
    if (item.thumb) img.src = thumbUrl(item.id, 1024, item.thumb_v);
    const full = new Image();
    full.onload = () => { if (foldState.items[foldState.viewing] === item) img.src = full.src; };
    full.src = `/api/preview/${item.id}`;
    if (item.rotation) img.style.transform = `rotate(${item.rotation}deg)`;
    stage.appendChild(img);
  }

  $('#fv-name').textContent = item.filename;
  // At the Ninaivu computer, where the file really is; elsewhere, its folder.
  const where = foldState.data?.local && item.path ? item.path : (item.folder || i18n.t('the library'));
  const facts = [where, visibilityWord(item.visibility_name)];
  if (item.size) facts.push(bytesShort(item.size));
  if (item.date_key) facts.push(item.date_key);
  $('#fv-facts').textContent = facts.join(' · ');
  $('#fv-count').textContent = `${foldState.viewing + 1} / ${foldState.items.length}`;
  $('#fv-prev').disabled = foldState.viewing <= 0;
  $('#fv-next').disabled = foldState.viewing >= foldState.items.length - 1;

  const badges = $('#fv-badges');
  badges.replaceChildren();
  if (item.visibility_name === 'hidden') {
    badges.appendChild(el('span', 'badge', i18n.t('Hidden')));
    const why = VIS_WHY[item.vis_source];
    if (why) badges.appendChild(el('span', 'hint', i18n.t(why)));
  }
  if (item.explicit) badges.appendChild(el('span', 'badge xp', i18n.t('Explicit')));

  const isHidden = item.visibility_name === 'hidden';
  const vis = $('#fv-visibility');
  vis.textContent = isHidden ? i18n.t('Show to family') : i18n.t('Hide');
  vis.className = isHidden ? 'btn small' : 'btn danger small';
  vis.onclick = () => foldSetVisibility([item], isHidden ? 'family' : 'hidden');
  const flag = $('#fv-explicit');
  flag.textContent = item.explicit ? i18n.t('Not explicit') : i18n.t('Mark explicit');
  flag.onclick = () => foldSetExplicit([item], !item.explicit);
  $('#fv-delete').onclick = () => foldDelete([item]);
  $('#fv-download').href = `/api/download/${item.id}`;
  $('#fv-download').dataset.size = item.size || 0;
  const reveal = $('#fv-reveal');
  reveal.hidden = !foldState.data?.local;
  reveal.onclick = () => revealItem(item);
  $('#fv-date').replaceChildren(creationDateControl(item));
}

/** Play a video or a recording in the viewer. What a browser opens (MP4,
 *  MOV, WebM, MP3…) plays from the file itself, with seeking. What it does
 *  not (3GP, MKV, AVI, WMV and the like) plays from a copy Ninaivu converts
 *  once and keeps, the same copy the family app's viewer uses; so does a
 *  file the browser gives up on, such as HEVC in a MOV outside Safari. */
function foldPlay(stage, item) {
  if (item.playable && !item.needs_proxy) {
    const media = document.createElement(item.kind === 'audio' ? 'audio' : 'video');
    media.src = `/api/file/${item.id}`;
    media.controls = true;
    media.preload = 'metadata';
    media.playsInline = true;
    if (item.kind === 'video' && item.thumb) media.poster = thumbUrl(item.id, 1024, item.thumb_v);
    media.onerror = () => {
      if (foldState.items[foldState.viewing] !== item) return;
      stage.replaceChildren();
      foldPlayConverted(stage, item);
    };
    stage.appendChild(media);
    return;
  }
  foldPlayConverted(stage, item);
}

async function foldPlayConverted(stage, item) {
  const url = `/api/proxy/${item.id}`;
  const panel = el('div', 'fv-wait');
  if (item.thumb) {
    const still = document.createElement('img');
    still.src = thumbUrl(item.id, 1024, item.thumb_v);
    still.alt = '';
    panel.appendChild(still);
  }
  const note = el('div', 'fv-wait-note');
  const heading = el('strong', '', i18n.t('Making a copy this browser can play'));
  const detail = el('span', 'hint',
    i18n.t('{format} files do not play in a browser, so Ninaivu converts a copy once. The original is not changed.',
      { format: (item.filename.split('.').pop() || '').toUpperCase() }));
  const bar = el('div', 'fv-wait-bar');
  const fill = document.createElement('i');
  bar.appendChild(fill);
  note.append(heading, detail, bar);
  panel.appendChild(note);
  stage.appendChild(panel);

  const gone = () => foldState.items[foldState.viewing] !== item || !panel.isConnected;
  for (let attempt = 0; attempt < 1800; attempt += 1) {
    if (gone()) return;
    let response;
    try {
      response = await fetch(url, { headers: { Accept: 'application/json' } });
    } catch {
      heading.textContent = i18n.t('Could not reach the server');
      bar.remove();
      return;
    }
    if (gone()) return;
    if (response.ok) {
      const media = document.createElement(item.kind === 'audio' ? 'audio' : 'video');
      media.src = url;
      media.controls = true;
      media.autoplay = true;
      media.playsInline = true;
      stage.replaceChildren(media);
      return;
    }
    let payload = {};
    try { payload = await response.json(); } catch { /* not JSON */ }
    if (response.status !== 202) {
      heading.textContent = i18n.t('This file cannot be played here');
      detail.textContent = `${i18n.fromServer(payload, payload.message || '')} ${
        i18n.t('Download it to check it on this computer.')}`.trim();
      bar.remove();
      return;
    }
    fill.style.width = `${Math.max(2, payload.percent || 0)}%`;
    await new Promise((resolve) => { setTimeout(resolve, 1000); });
  }
  bar.remove();
}

/** Move files to the library's bin: one confirmation, no password (they
 *  can be put back from Recently deleted, just below), one request. */
function foldDelete(items) {
  if (!items.length) return;
  askPasswordToDelete(items, async (result) => {
    for (const item of items) foldState.chosen.delete(item.id);
    const failed = (result.failed || []).length;
    if (failed) {
      toast(i18n.t('{count} could not be moved to the bin.', { count: failed }), true);
    } else {
      toast(items.length === 1
        ? i18n.t('“{name}” moved to the bin.', { name: items[0].filename })
        : i18n.t('{count} files moved to the bin.', { count: items.length }));
    }
    if (foldState.viewing >= 0 && foldState.viewing >= foldState.items.length - items.length) {
      foldState.viewing = Math.max(0, foldState.items.length - items.length - 1);
    }
    await loadFolderScreen(foldState.path, { keep: true });
    if (!foldState.items.length) closeFoldViewer();
    else if (foldState.viewing >= 0) showFoldViewerItem();
    shell.openPage('recycle-bin').then((page) => page?.load());
  }, { password: false });
}

function wireFolderScreen() {
  $$('#fold-deep').onchange = (event) => {
    foldState.deep = event.target.checked;
    loadFolderScreen(foldState.path);
  };
  $$('#fold-more').onclick = loadMoreFiles;
  $$('#fold-act-all').onclick = foldChooseAll;
  $$('#fold-act-none').onclick = () => { foldState.chosen.clear(); renderFolderScreen(); };
  $$('#fold-act-hide').onclick = () => foldSetVisibility(foldChosenItems(), 'hidden');
  $$('#fold-act-show').onclick = () => foldSetVisibility(foldChosenItems(), 'family');
  $$('#fold-act-explicit').onclick = () => foldSetExplicit(foldChosenItems(), true);
  $$('#fold-act-clean').onclick = () => foldSetExplicit(foldChosenItems(), false);
  $$('#fold-act-delete').onclick = () => foldDelete(foldChosenItems());
  $$('#fv-close').onclick = closeFoldViewer;
  // On an iPhone a plain download opened over the console with no way back.
  keepInApp($$('#fv-download'), { toast });
  $$('#fv-prev').onclick = () => stepFoldViewer(-1);
  $$('#fv-next').onclick = () => stepFoldViewer(1);
  $$('#fold-viewer').addEventListener('keydown', (event) => {
    if (event.target.closest('input, textarea, select')) return;
    if (event.key === 'Escape') closeFoldViewer();
    if (event.key === 'ArrowLeft') stepFoldViewer(-1);
    if (event.key === 'ArrowRight') stepFoldViewer(1);
  });
}

function creationDateControl(item) {
  const row = el('div', 'setting-field');
  row.onclick = (event) => event.stopPropagation();
  row.onkeydown = (event) => event.stopPropagation();
  const input = el('input', 'input');
  input.type = 'date';
  input.value = item.date_key || '';
  input.required = true;
  row.appendChild(labelled(i18n.t('Creation date'), input));
  const save = el('button', 'btn small', i18n.t('Save date and move'));
  save.type = 'button';
  save.onclick = async (event) => {
    event.stopPropagation();
    if (!input.reportValidity()) return;
    save.disabled = true;
    try {
      const result = await json(`/api/admin/assets/${item.id}/creation-date`, {
        method: 'PATCH', body: { creation_date: input.value },
      });
      toast(i18n.t('Saved. File is now in {folder}.', { folder: result.item.folder }));
      await loadFolderScreen(foldState.path);
    } catch (exc) { toast(exc.message, true); }
    finally { save.disabled = false; }
  };
  row.appendChild(save);
  row.appendChild(el('p', 'hint', i18n.t('Moves to year/month/day. Saved at midnight UTC; original embedded metadata is preserved.')));
  return row;
}

/** The page has been opened: wire its controls, once. */
export function wire(context) {
  shell = context;
  wireFolderScreen();
}

/** Show the folder last open, or the library's top. */
export function load() {
  return loadFolderScreen(foldState.path);
}

/** Draw again in the current language. */
export function render() {
  renderFolderScreen();
}
