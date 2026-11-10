/**
 * What is done with the photographs chosen in the gallery (app.js): the
 * selection bar's buttons — favourite, download, export to another format,
 * add to or take out of an album, turn the files, delete. Always on screen
 * with the gallery, so imported with it; a module of its own so app.js is
 * the gallery's wiring and not everything.
 *
 * What it needs of the gallery around it comes through connectSelection().
 */

import { hideSecret, upgradeSecret } from '../accounts.js';
import { api } from '../api.js';
import * as i18n from '../i18n.js';
import { saveFile } from '../save-file.js';
import { state } from '../store.js';
import { $, toast } from '../ui.js';
import { loadAlbums, openAlbumModal } from './sidebar.js';

/** The gallery around the selection (app.js), given once at start. */
let ctx;

/** Given by app.js before anything here is wired. */
export function connectSelection(context) {
  ctx = context;
}

const reload = (options) => ctx.reload(options);
const refreshStatus = () => ctx.refreshStatus();

export function wireSelection() {
  $('#sel-cancel').onclick = () => ctx.grid().clearSelection();
  $('#sel-all').onclick = () => ctx.grid().selectAll();
  $('#sel-fav').onclick = () => bulk({ favorite: true });
  $('#sel-unfav').onclick = () => bulk({ favorite: false });
  $('#sel-download').onclick = () => {
    // One selection, one file. This used to fire up to twenty separate
    // downloads 320 ms apart, which browsers block after the first few and
    // which silently dropped everything past the twentieth anyway.
    const ids = [...ctx.grid().selection];
    if (!ids.length) return;

    if (ids.length === 1) {
      // On an iPhone this goes through the share sheet, so the app stays put.
      saveFile(`/api/download/${ids[0]}`, { toast });
      return;
    }

    const capped = ids.slice(0, 2000);
    if (capped.length < ids.length) {
      toast(i18n.t('Downloading the first {count} of {total}.', { count: capped.length.toLocaleString(i18n.locale()), total: ids.length.toLocaleString(i18n.locale()) }));
    } else {
      toast(i18n.t('Preparing {items}…', { items: i18n.items(capped.length) }));
    }
    // The server streams the archive as it reads, so the browser starts
    // saving straight away rather than waiting on a spinner.
    // On an iPhone it opens in a new tab rather than over the app.
    saveFile(`/api/download/zip?ids=${capped.join(',')}`, { archive: true });
  };
  wireExport();
  $('#sel-delete').onclick = () => deleteSelected();
  upgradeSecret($('#delete-password'));
  $('#sel-rot-left').onclick = () => rotateSelected(270);
  $('#sel-rot-right').onclick = () => rotateSelected(90);
  $('#sel-album').onclick = () => {
    const ids = [...ctx.grid().selection];
    if (!ids.length) return;
    openAlbumModal(ids);
  };
  $('#sel-album-remove').onclick = async () => {
    const ids = [...ctx.grid().selection];
    if (!ids.length || !state.filters.album) return;
    try {
      await api.albumRemove(state.filters.album, ids);
      toast(i18n.t('Removed {items} from the album.', { items: i18n.items(ids.length) }));
      ctx.grid().clearSelection();
      await loadAlbums();
      reload();
    } catch (err) {
      toast(i18n.t('Could not remove items: {reason}', { reason: err.message }), true);
    }
  };
}

/**
 * Export the selection converted to JPEG, PNG or WebP, optionally smaller.
 *
 * "Download" hands over the files as they are; this is for the photograph
 * that has to be a JPEG to be sent, or is a raw file nobody else can open.
 * The server converts as it streams the archive, so the browser saves it the
 * same way it saves a plain download.
 */
function wireExport() {
  const modal = $('#export-modal');
  if (!modal) return;
  const format = $('#export-format'), quality = $('#export-quality');
  const close = () => { modal.hidden = true; };
  const sync = () => {
    $('#export-quality-field').hidden = format.value === 'png';
    $('#export-quality-out').textContent = quality.value;
  };
  format.onchange = quality.oninput = sync;
  $('#sel-export').onclick = () => {
    const count = ctx.grid().selection.size;
    if (!count) return;
    $('#export-count').textContent = i18n.t(
      'Convert {items} to another format, and shrink them if you like.',
      { items: i18n.items(count) });
    sync();
    modal.hidden = false;
    format.focus();
  };
  $('#export-close').onclick = $('#export-cancel').onclick = close;
  modal.addEventListener('mousedown', (event) => { if (event.target === modal) close(); });
  $('#export-form').onsubmit = (event) => {
    event.preventDefault();
    const ids = [...ctx.grid().selection].slice(0, 2000);
    if (!ids.length) return close();
    const query = new URLSearchParams({
      ids: ids.join(','), format: format.value, edge: $('#export-edge').value,
      quality: quality.value,
    });
    if ($('#export-metadata').checked) query.set('metadata', 'keep');
    toast(i18n.t('Preparing your export…'));
    close();
    // Streamed by the server as each photograph is converted.
    saveFile(`/api/download/zip?${query}`, { archive: true });
  };
}

/**
 * Turn the selected photographs and videos — in the files themselves.
 *
 * The viewer's rotate button writes a number into the index and leaves the
 * file alone, which is right for correcting one photograph. This is the other
 * job: a box of scanned prints that all came out sideways, where the point is
 * that they are still the right way up after they leave Ninaivu.
 *
 * It asks first, because it writes to the library. It asks once, not once per
 * file, and it says plainly what it is about to do. And it asks for the
 * password, exactly as deleting does and for the same reason: the gallery is
 * the window left open on the sofa, and this rewrites the files.
 */
function rotatePasswordField(modal) {
  // The same field the delete dialog has, in the same place in the card. Built
  // here rather than a second copy in the page, so the two cannot drift apart.
  let input = $('#rotate-password');
  if (input) return input;
  const field = document.createElement('label');
  field.className = 'field';
  const label = document.createElement('span');
  label.textContent = i18n.t('Your password');
  input = document.createElement('input');
  input.type = 'password';
  input.id = 'rotate-password';
  input.className = 'input';
  input.autocomplete = 'current-password';
  field.append(label, input);
  const error = document.createElement('p');
  error.className = 'gate-error';
  error.id = 'rotate-error';
  error.setAttribute('role', 'alert');
  error.hidden = true;
  modal.querySelector('.modal-foot').before(field, error);
  upgradeSecret(input);
  return input;
}

async function rotateSelected(turn) {
  const ids = [...ctx.grid().selection];
  if (!ids.length) return;

  const modal = $('#rotate-modal');
  const input = rotatePasswordField(modal);
  const error = $('#rotate-error');
  const count = ids.length;
  $('#rotate-what').textContent =
    `${count.toLocaleString()} ${count === 1 ? 'file' : 'files'} will be turned a `
    + `quarter turn ${turn === 90 ? 'clockwise' : 'anticlockwise'}.`;
  error.hidden = true;
  input.value = '';
  hideSecret(input);
  modal.hidden = false;
  input.focus();

  const close = () => {
    modal.hidden = true;
    input.value = '';
    input.onkeydown = null;
    $('#rotate-confirm').onclick = null;
    $('#rotate-cancel').onclick = null;
    document.onkeydown = null;
  };

  $('#rotate-cancel').onclick = close;
  document.onkeydown = (event) => { if (event.key === 'Escape') close(); };
  const attempt = async () => {
    if (!input.value) { input.focus(); return; }
    const password = input.value;
    try {
      const result = await api.rotateFiles(ids, turn, password);
      close();
      const done = result.rotated || 0;
      if (done) {
        toast(i18n.t('{items} turned.', { items: i18n.items(done) }));
      }
      // What could not be turned is named rather than quietly dropped: an
      // admin who selected forty and saw "38 turned" deserves to know which
      // two did not move, and why.
      const skipped = result.skipped || [];
      if (skipped.length) {
        const first = skipped[0];
        toast(skipped.length === 1
          ? `${first.name} was left alone — ${first.why}`
          : `${skipped.length} were left alone, starting with ${first.name} — ${first.why}`,
        true);
      }
      ctx.grid().clearSelection();
      await reload();
    } catch (exc) {
      if (exc.status === 401 && exc.data?.needs_password) {
        error.textContent = i18n.fromServer(exc.data, exc.message);
        error.hidden = false;
        input.value = '';
        input.focus();
        return;
      }
      close();
      toast(exc.message, true);
    }
  };

  $('#rotate-confirm').onclick = attempt;
  input.onkeydown = (event) => {
    if (event.key === 'Enter') { event.preventDefault(); attempt(); }
    if (event.key === 'Escape') close();
  };
}

/**
 * Delete items, after asking for the password.
 *
 * The password is asked for every time, not once per session. Choosing what to
 * delete means looking at the photographs, so this lives in the gallery — and
 * the gallery is the window left open on the sofa.
 *
 * `explicitIds` lets another screen (the duplicates review, below) reuse this
 * same confirm-and-delete flow for a list that has nothing to do with the
 * main grid's selection. Left out — as every existing caller leaves it — this
 * deletes the current `ctx.grid().selection`, exactly as before. `onDone` is an
 * optional callback fired after a successful delete, so a caller with its own
 * list to update (the duplicates modal) can refresh itself without a second
 * network round trip.
 */
export async function deleteSelected(explicitIds, onDone) {
  const usingSelection = !Array.isArray(explicitIds);
  const ids = usingSelection ? [...ctx.grid().selection] : [...explicitIds];
  if (!ids.length) return;

  const modal = $('#delete-modal');
  const input = $('#delete-password');
  const error = $('#delete-error');
  const count = ids.length;

  $('#delete-what').textContent =
    `${count.toLocaleString()} ${count === 1 ? 'item' : 'items'} will be removed `
    + 'from the library.';
  error.hidden = true;
  input.value = '';
  hideSecret(input);
  modal.hidden = false;
  input.focus();

  const close = () => {
    modal.hidden = true;
    input.value = '';
    input.onkeydown = null;
    $('#delete-confirm').onclick = null;
    $('#delete-cancel').onclick = null;
  };

  const attempt = async () => {
    if (!input.value) { input.focus(); return; }
    try {
      const result = await api.deleteItems(ids, input.value);
      close();
      const moved = result.deleted;
      toast(i18n.t('{items} moved to the recycle bin.', { items: i18n.items(moved) }));
      if (result.failed?.length) {
        toast(i18n.t('{count} could not be moved: {reason}', {
          count: result.failed.length.toLocaleString(i18n.locale()), reason: result.failed[0].why,
        }), true);
      }
      if (usingSelection) ctx.grid().clearSelection();
      await reload();
      onDone?.(result);
    } catch (exc) {
      if (exc.status === 401 && exc.data?.needs_password) {
        error.textContent = i18n.fromServer(exc.data, exc.message);
        error.hidden = false;
        input.value = '';
        input.focus();
        return;
      }
      close();
      toast(exc.message, true);
    }
  };

  $('#delete-confirm').onclick = attempt;
  $('#delete-cancel').onclick = close;
  input.onkeydown = (event) => {
    if (event.key === 'Enter') { event.preventDefault(); attempt(); }
    if (event.key === 'Escape') close();
  };
}

async function bulk(fields) {
  const ids = [...ctx.grid().selection];
  if (!ids.length) return;
  try {
    await api.bulk(ids, fields);
    toast(i18n.t('Updated {items}.', { items: i18n.items(ids.length) }));
    ctx.grid().clearSelection();
    await refreshStatus();
    reload();
  } catch (error) {
    toast(error.message, true);
  }
}
