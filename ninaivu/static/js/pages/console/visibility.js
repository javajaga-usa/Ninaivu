/**
 * The console's Visibility page: who sees each folder, as a tree, and
 * changing it with one question asked first when it would reveal anything,
 * and the way back from the last change. Loaded when the page is first
 * opened, or when the folder screen asks it to set a folder or undo
 * (admin.js, openPage).
 */

import { consoleJson as json } from '../../api.js';
import * as i18n from '../../i18n.js';
import { consoleState as state } from '../../store.js';
import { $, $$, consoleToast as toast, el, emphasised, plural, strong } from '../../ui.js';
import { VIS_NAMES_BY_VALUE, visibilityWord } from './common.js';

const visibilityApi = {
  folders: () => json('/api/admin/folders'),
  setFolderVisibility: (folder, visibility, confirm = false) =>
    json('/api/visibility/folder',
      { method: 'POST', body: { folder, visibility, confirm } }),
  undoVisibility: (batchId) =>
    json('/api/visibility/undo', { method: 'POST', body: { batch_id: batchId } }),
  visibilityHistory: () => json('/api/visibility/history'),
};

/** The console around the page (admin.js). */
let shell;

async function loadFolders() {
  try {
    const data = await visibilityApi.folders();
    state.folders = data.folders;
    state.rootRule = data.root_rule;
  } catch (exc) {
    toast(exc.message, true);
    return;
  }
  renderFolderTree();
}

/* The tree opens on its top folders (a dated library's years) with everything
   below folded: every folder at once was thousands of rows to scroll past to
   reach one. A folder opened stays open while the page is, so setting one
   does not fold the tree back up around it. */
const openFolders = new Set();

function parentOf(path) {
  const cut = path.lastIndexOf('/');
  return cut < 0 ? '' : path.slice(0, cut);
}

function shownInTree(path) {
  for (let above = parentOf(path); above; above = parentOf(above)) {
    if (!openFolders.has(above)) return false;
  }
  return true;
}

/* Tree order, a folder's name at a time. Sorted as text, "2019 trip" came
   between "2019" and "2019/07", so an opened 2019 showed its months under the
   wrong folder. */
function byTreeOrder(a, b) {
  const left = a.path.split('/');
  const right = b.path.split('/');
  for (let i = 0; i < Math.min(left.length, right.length); i += 1) {
    const order = left[i].localeCompare(right[i], undefined, { numeric: true });
    if (order) return order;
  }
  return left.length - right.length;
}

function renderFolderTree() {
  const tree = $('#folder-tree');
  tree.innerHTML = '';
  const total = state.overview?.stats?.count || 0;

  tree.appendChild(folderRow({
    path: '', name: i18n.t('Whole library'), depth: 0, count: total,
    rule: state.rootRule,
  }));
  const parents = new Set(state.folders.map((folder) => parentOf(folder.path)));
  for (const folder of [...state.folders].sort(byTreeOrder)) {
    if (shownInTree(folder.path)) {
      tree.appendChild(folderRow(folder, parents.has(folder.path)));
    }
  }
  if (!state.folders.length) {
    tree.appendChild(el('p', 'hint', i18n.t('No folders indexed yet.')));
  }
}

function folderToggle(folder, hasChildren) {
  if (!hasChildren) return el('span', 'folder-toggle');
  const open = openFolders.has(folder.path);
  const toggle = el('button', 'folder-toggle');
  toggle.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 6 6 6-6 6"/></svg>';
  toggle.type = 'button';
  toggle.setAttribute('aria-expanded', String(open));
  toggle.setAttribute('aria-label', open ? i18n.t('Collapse {folder}', { folder: folder.name || folder.path })
    : i18n.t('Expand {folder}', { folder: folder.name || folder.path }));
  toggle.onclick = () => {
    if (open) {
      // Folding a folder folds everything inside it, so it opens as it closed.
      for (const path of [...openFolders]) {
        if (path === folder.path || path.startsWith(`${folder.path}/`)) openFolders.delete(path);
      }
    } else {
      openFolders.add(folder.path);
    }
    renderFolderTree();
    $(`#folder-tree .folder-toggle[data-path="${CSS.escape(folder.path)}"]`)?.focus();
  };
  toggle.dataset.path = folder.path;
  return toggle;
}

function folderRow(folder, hasChildren = false) {
  const row = el('div', 'folder-row');
  // The library root has no buttons. There is no decision anybody makes about
  // *every photograph in the house* that is worth a control, and it was the
  // one place where a slip cost everything — so it is a summary line now, and
  // the server refuses the change even if something asks for it.
  const isRoot = !folder.path;
  if (isRoot) row.classList.add('is-root');

  const name = el('div', 'folder-name');
  name.style.paddingLeft = `${folder.depth * 16}px`;
  if (!isRoot) name.appendChild(folderToggle(folder, hasChildren));
  name.appendChild(el('span', null, folder.name || folder.path || i18n.t('Whole library')));
  const count = el('span', 'hint', ` ${folder.count}`);
  name.appendChild(count);
  if (folder.rule) name.appendChild(el('span', `tag ${folder.rule}`, visibilityWord(folder.rule)));
  row.appendChild(name);

  const mix = el('div', 'folder-mix');
  for (const [key, value] of [['public', folder.public], ['family', folder.family],
    ['hidden', folder.hidden]]) {
    if (!value) continue;
    const chunk = el('i', `mix ${key}`);
    chunk.style.flex = String(value);
    chunk.title = `${value} ${visibilityWord(key)}`;
    mix.appendChild(chunk);
  }
  row.appendChild(mix);

  if (isRoot) {
    row.appendChild(el('div', 'folder-note',
      i18n.t('Set on a folder below, or on the photographs themselves.')));
    return row;
  }

  const picker = el('div', 'vis-picker');
  for (const [value, text] of [['public', i18n.t('Everyone')], ['family', i18n.t('Family')],
    ['hidden', i18n.t('Nobody')]]) {
    const button = el('button', null, text);
    button.type = 'button';
    button.dataset.vis = value;
    if (folder.rule === value) button.classList.add('active');
    button.onclick = () => applyFolderVisibility(folder.path, value);
    picker.appendChild(button);
  }
  row.appendChild(picker);
  return row;
}

/* --- bulk visibility: ask twice, and keep the way back ------------------ */

const VIS_WORDS = { public: i18n.key('everyone, guests included'),
                    family: i18n.key('family members and admins'),
                    hidden: i18n.key('admins only') };

/**
 * Apply a folder rule, stopping first if it would reveal anything.
 *
 * The server refuses a widening change that has not been confirmed, and says
 * how much it would expose. That refusal is the first question; this modal is
 * the second. One mis-click on a control whose three buttons sit a few pixels
 * apart should not be able to publish photographs somebody hid on purpose.
 */
async function applyFolderVisibility(path, value) {
  try {
    let result;
    let confirmed = false;
    // One question, asked only when it applies: a change that would reveal
    // something has to be confirmed after the reader has seen how much. The
    // whole-library password that used to live here went with the control it
    // guarded — the root is refused outright now, not asked about.
    for (let attempt = 0; attempt < 3; attempt += 1) {
      try {
        result = await visibilityApi.setFolderVisibility(path, value, confirmed);
        break;
      } catch (exc) {
        if (exc.status === 409 && exc.data?.needs_confirmation) {
          if (!await confirmExposure(exc.data.impact, path, value)) return;
          confirmed = true;
          continue;
        }
        throw exc;
      }
    }
    if (!result) return;
    toast(i18n.t('{items} → {level}.', { items: i18n.items(result.updated), level: visibilityWord(value) }));
    await shell.overviewChanged();
    await loadFolders();
    await refreshUndo();
    shell.renderPreview();
  } catch (exc) { toast(exc.message, true); }
}

function confirmExposure(impact, path, value) {
  return new Promise((resolve) => {
    const where = path ? `“${path}”` : i18n.t('the whole library');
    $('#expose-what').replaceChildren(
      ...(value === null
        ? emphasised(i18n.key('Stopping hiding {where} lets the family see what is in it again.'),
          { where: strong(where) })
        : emphasised(i18n.key('Setting {where} to {level} makes it visible to {who}.'), {
          where: strong(where),
          level: strong(visibilityWord(value)),
          who: VIS_WORDS[value] ? i18n.t(VIS_WORDS[value]) : value,
        })),
      ' ',
      ...emphasised(impact.exposed === 1
        ? i18n.key('{count} file is currently more private than that, and would become visible.')
        : i18n.key('{count} files are currently more private than that, and would become visible.'),
      { count: strong(Number(impact.exposed || 0).toLocaleString()) }),
    );

    // Name *why* they are private, because "I hid those myself" and "the
    // filesystem hid those" are different decisions to be second-guessing.
    const notes = [];
    if (impact.decided_individually) {
      notes.push(plural(impact.decided_individually,
        i18n.key('1 file was set individually and would be overwritten'),
        i18n.key('{count} files were set individually and would be overwritten')));
    }
    if (impact.hidden_by_the_filesystem) {
      notes.push(plural(impact.hidden_by_the_filesystem,
        i18n.key('1 file is hidden because of where it sits on disk'),
        i18n.key('{count} files are hidden because of where they sit on disk')));
    }
    if (impact.child_rules) {
      notes.push(plural(impact.child_rules,
        i18n.key('1 folder rule inside it would be overridden'),
        i18n.key('{count} folder rules inside it would be overridden')));
    }
    $('#expose-detail').textContent = notes.length ? `${notes.join('; ')}.` : '';
    $('#expose-detail').hidden = !notes.length;

    const modal = $('#expose-modal');
    modal.hidden = false;
    const close = (answer) => {
      modal.hidden = true;
      $('#expose-confirm').onclick = null;
      $('#expose-cancel').onclick = null;
      resolve(answer);
    };
    $('#expose-confirm').onclick = () => close(true);
    $('#expose-cancel').onclick = () => close(false);
  });
}

async function refreshUndo() {
  const strip = $('#vis-undo');
  if (!strip) return;
  try {
    const { changes } = await visibilityApi.visibilityHistory();
    const last = (changes || []).find((c) => !c.undone_at && c.restorable > 0);
    if (!last) { strip.hidden = true; return; }
    const where = last.scope === 'folder'
      ? (last.folder ? `“${last.folder}”` : i18n.t('the whole library'))
      : i18n.items(last.affected);
    const level = VIS_NAMES_BY_VALUE[last.visibility];
    $('#vis-undo-what').textContent = i18n.t('Last change: {where} → {level}',
      { where, level: level ? visibilityWord(level) : last.visibility });
    $('#vis-undo-detail').textContent = last.exposed
      ? `${plural(last.exposed, i18n.key('1 file became visible to more people.'),
        i18n.key('{count} files became visible to more people.'))} ${
        i18n.t('Undo puts every file back exactly as it was.')}`
      : plural(last.restorable, i18n.key('1 file can be put back exactly as it was.'),
        i18n.key('{count} files can be put back exactly as they were.'));
    strip.hidden = false;
  } catch { strip.hidden = true; }
}

async function undoLastVisibility() {
  try {
    const result = await visibilityApi.undoVisibility(null);
    toast(plural(result.restored, i18n.key('Put 1 file back as it was.'),
      i18n.key('Put {count} files back as they were.')));
    await shell.overviewChanged();
    await loadFolders();
    await refreshUndo();
    shell.renderPreview();
  } catch (exc) { toast(exc.message, true); }
}

/** Loaded: wire the page's one button, once. */
export function wire(context) {
  shell = context;
  $$('#vis-undo-btn').onclick = undoLastVisibility;
}

/** The page has been opened: the tree and the way back, fresh. */
export async function show() {
  await loadFolders();
  await refreshUndo();
}

/** Draw again in the current language. */
export function render() {
  if (state.folders) renderFolderTree();
}

export { applyFolderVisibility, confirmExposure, loadFolders, refreshUndo };
