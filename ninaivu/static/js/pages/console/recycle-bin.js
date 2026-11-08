/**
 * The recycle bin, a section of the console's Folders page: what was deleted,
 * brought back or let go of for good. Loaded with the page (admin.js,
 * openPage).
 */

import { hideSecret } from '../../accounts.js';
import { admin } from '../../api.js';
import * as i18n from '../../i18n.js';
import {
  $, $$, bytesShort, consoleToast as toast, el, plural, whenShort,
} from '../../ui.js';

const adminApi = {
  recycleBin: () => admin.getRecycle(),
  restoreDeleted: (ids) => admin.postRecycleRestore({ body: { ids } }),
  purgeDeleted: (ids, password) => admin.postRecyclePurge({ body: { ids, password } }),
  rescan: (full) => admin.postScan({ body: { full } }),
};

/* --- the recycle bin ---------------------------------------------------- */
/*
 * Deleting already moves the file rather than erasing it, but a bin nobody
 * can look into is only half a promise. This is the other half: what went,
 * when, who by, and one button to put it back.
 */

const binState = { items: [], chosen: new Set(), folders: [] };

async function loadRecycleBin() {
  let data;
  try {
    data = await adminApi.recycleBin();
  } catch (exc) {
    // A library that has never had a deletion has no bin, and that is not an
    // error worth a red toast on every visit to the tab.
    binState.items = [];
    renderRecycleBin();
    if (exc.status && exc.status !== 404) toast(exc.message, true);
    return;
  }
  binState.items = data.items || [];
  binState.folders = data.folders || [];
  binState.summary = data.summary || {};
  binState.eraseAfter = data.erase_after_days || 0;
  // Drop anything that has gone since the last look, so "Put back" can never
  // be enabled for a row that is no longer there.
  const live = new Set(binState.items.map((row) => row.id));
  binState.chosen = new Set([...binState.chosen].filter((id) => live.has(id)));
  renderRecycleBin();
}

function renderRecycleBin() {
  const list = $('#bin-list');
  const rows = binState.items;
  list.innerHTML = '';

  const pill = $('#bin-count');
  pill.hidden = rows.length === 0;
  pill.textContent = String(rows.length);

  $('#bin-empty').hidden = rows.length > 0;
  $('#bin-policy').textContent = binState.eraseAfter
    ? ` ${i18n.t('Anything left here longer than {days} days is erased automatically when Ninaivu starts.',
      { days: binState.eraseAfter })}`
    : '';
  $('#bin-foot').hidden = rows.length === 0;
  $('#bin-note').hidden = rows.length === 0;

  for (const row of rows) {
    const line = el('label', 'bin-row');
    if (!row.present) line.classList.add('gone');

    const box = document.createElement('input');
    box.type = 'checkbox';
    box.checked = binState.chosen.has(row.id);
    box.disabled = !row.present;
    box.onchange = () => {
      if (box.checked) binState.chosen.add(row.id);
      else binState.chosen.delete(row.id);
      renderBinFoot();
    };
    line.appendChild(box);

    const preview = document.createElement('img');
    preview.className = 'bin-thumb';
    preview.alt = '';
    preview.loading = 'lazy';
    if (row.thumbnail) {
      preview.src = row.thumbnail;
      preview.onerror = () => preview.classList.add('missing');
    } else {
      preview.classList.add('missing');
    }
    line.appendChild(preview);

    const text = el('div', 'bin-what');
    text.appendChild(el('strong', '', row.filename));
    text.appendChild(el('span', 'path', row.rel_path));
    line.appendChild(text);

    line.appendChild(el('span', 'bin-when', whenShort(row.deleted_at)));
    line.appendChild(el('span', 'bin-size', bytesShort(row.size)));
    line.appendChild(el('span', row.present ? 'bin-state' : 'bin-state missing',
      row.present ? i18n.t('In the bin') : i18n.t('Moved or erased')));
    list.appendChild(line);
  }
  renderBinFoot();
}

function renderBinFoot() {
  const n = binState.chosen.size;
  const held = binState.summary || {};
  $('#bin-selected').textContent = n
    ? plural(n, i18n.key('1 file chosen'), i18n.key('{count} files chosen'))
    : held.items
      ? i18n.t('{files}, {size} in the bin', {
        files: plural(held.items, i18n.key('1 file'), i18n.key('{count} files')),
        size: bytesShort(held.bytes || 0) })
      : i18n.t('Nothing chosen');
  $('#bin-restore').disabled = n === 0;
  $('#bin-delete').disabled = n === 0;
}

/**
 * Erase the chosen files, after asking for the password.
 *
 * The password is asked here as well as in the gallery, and for a different
 * reason: the gallery's delete can be taken back from this screen, and this
 * one cannot be taken back from anywhere. An open console says somebody is
 * administering the library; it does not say they meant to lose a photograph.
 */
function purgeChosen() {
  const ids = [...binState.chosen];
  if (!ids.length) return;

  const modal = $('#purge-modal');
  const input = $('#purge-password');
  const error = $('#purge-error');
  const confirm = $('#purge-confirm');

  $('#purge-what').textContent =
    plural(ids.length, i18n.key('1 file will be erased from the disk.'),
      i18n.key('{count} files will be erased from the disk.'));
  error.hidden = true;
  input.value = '';
  hideSecret(input);
  modal.hidden = false;
  input.focus();

  const attempt = async () => {
    if (!input.value) { input.focus(); return; }
    confirm.disabled = true;
    try {
      const result = await adminApi.purgeDeleted(ids, input.value);
      closePurge();
      toast(result.purged
        ? plural(result.purged, i18n.key('1 file is gone for good.'), i18n.key('{count} files are gone for good.'))
        : i18n.t('Nothing could be erased.'), !result.purged);
      for (const problem of result.failed || []) {
        toast(`${problem.name}: ${problem.why}`, true);
      }
      binState.chosen.clear();
      await loadRecycleBin();
    } catch (exc) {
      if (exc.status === 401 && exc.data?.needs_password) {
        error.textContent = i18n.fromServer(exc.data, exc.message);
        error.hidden = false;
        input.value = '';
        input.focus();
        return;
      }
      closePurge();
      toast(exc.message, true);
    } finally {
      confirm.disabled = false;
    }
  };

  confirm.onclick = attempt;
  $('#purge-cancel').onclick = closePurge;
  input.onkeydown = (event) => {
    if (event.key === 'Enter') { event.preventDefault(); attempt(); }
    if (event.key === 'Escape') closePurge();
  };
}

function closePurge() {
  $('#purge-modal').hidden = true;
  $('#purge-password').value = '';
  $('#purge-password').onkeydown = null;
  $('#purge-confirm').onclick = null;
  $('#purge-cancel').onclick = null;
}

async function restoreChosen() {
  const ids = [...binState.chosen];
  if (!ids.length) return;
  const button = $('#bin-restore');
  button.disabled = true;
  try {
    const result = await adminApi.restoreDeleted(ids);
    toast(result.restored
      ? `${plural(result.restored, i18n.key('1 file is back where it was.'),
        i18n.key('{count} files are back where they were.'))} ${
        i18n.t('They reappear in the library after the next scan.')}`
      : i18n.t('Nothing could be put back.'), !result.restored);
    for (const problem of result.failed || []) {
      toast(`${problem.name}: ${problem.why}`, true);
    }
    binState.chosen.clear();
    await loadRecycleBin();
    // Restoring puts files back on the disk but not into the index; a scan is
    // what makes them visible again, so start one rather than leaving the
    // household wondering where the photographs went.
    if (result.restored) await adminApi.rescan(false).catch(() => {});
  } catch (exc) {
    toast(exc.message, true);
  } finally {
    renderBinFoot();
  }
}

/** Bind the bin's controls: once, when the Folders page is first opened. */
export function wire() {
  $$('#bin-refresh').onclick = () => loadRecycleBin();
  $$('#bin-restore').onclick = () => restoreChosen();
  $$('#bin-delete').onclick = () => purgeChosen();
  $$('#purge-modal').addEventListener('click', (event) => {
    if (event.target === $('#purge-modal')) closePurge();
  });
  $$('#bin-all').onclick = () => {
    const present = binState.items.filter((row) => row.present);
    const all = present.length && present.every((row) => binState.chosen.has(row.id));
    binState.chosen = all ? new Set() : new Set(present.map((row) => row.id));
    renderRecycleBin();
  };
}

/** Read the bin again (the page was opened, or something was deleted). */
export function load() {
  return loadRecycleBin();
}

/** Draw it again as it was last read (the language changed). */
export function render() {
  renderRecycleBin();
}
