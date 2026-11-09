/**
 * The console's Uploads page — family uploads waiting for approval, phone
 * backups and phone keys — and the TV album on the Server page. Loaded when
 * either page is first opened (admin.js, openPage). The uploads queue is
 * asked for at sign-in and every ten seconds in admin.js
 * (loadPendingUploads), which also says when something new arrives; this
 * module draws it.
 */

import { admin } from '../../api.js';
import * as i18n from '../../i18n.js';
import { $, $$, bytesShort, consoleToast as toast, el } from '../../ui.js';
import { labelled } from './common.js';

let shell = null;
// What the list last drew, so a poll that brings nothing new draws nothing.
let uploadSignature = '';

/** The uploads waiting for approval, as admin.js last fetched them. */
export function render(data) {
  if (!data) return;
  const signature = JSON.stringify(data.items);
  if (signature === uploadSignature) return;
  uploadSignature = signature;
  const list = $('#pending-uploads');
  const drafts = new Map([...list.querySelectorAll('input[data-upload]')].map(input => [input.dataset.upload, input.value]));
  list.replaceChildren();
  if (!data.items.length) list.appendChild(el('p', 'hint', i18n.t('No uploads awaiting approval.')));
  for (const item of data.items) {
    const card = el('div', 'pending-upload');
    if (item.has_thumb) {
      const img = el('img');
      img.src = `/api/admin/uploads/${item.id}/preview?thumb=1`;
      img.alt = item.filename;
      img.loading = 'lazy';
      card.appendChild(img);
    }
    // An edit saved from the Playground is not a new photograph arriving, and
    // reviewing it is a different judgement: say so before the filename, which
    // is generated and tells an administrator nothing on its own.
    if (item.edited_from) {
      const badge = el('span', 'pending-kind', i18n.t('Edited copy'));
      card.appendChild(badge);
      card.appendChild(el('strong', null, i18n.t('Edit of {name}', { name: item.edited_from })));
      card.appendChild(el('p', 'hint', i18n.t('Edited by {who} · {size} · saved as {file}',
        { who: item.uploader, size: bytesShort(item.size), file: item.filename })));
    } else {
      card.appendChild(el('strong', null, item.filename));
      card.appendChild(el('p', 'hint', i18n.t('Uploaded by {who} · {size}',
        { who: item.uploader, size: bytesShort(item.size) })));
    }
    const preview = el('a', 'btn small ghost', i18n.t('Preview file'));
    preview.href = `/api/admin/uploads/${item.id}/preview`;
    preview.target = '_blank';
    preview.rel = 'noopener';
    card.appendChild(preview);
    const input = el('input', 'input');
    input.type = 'date';
    // Only a by-date filing needs one. A derivative goes beside its source
    // whatever the date says, so demanding one would be asking for a decision
    // that changes nothing.
    input.required = item.files_by_date;
    input.dataset.upload = String(item.id);
    input.value = drafts.get(String(item.id)) || item.creation_date || '';
    card.appendChild(labelled(item.files_by_date ? i18n.t('Creation date') : i18n.t('Creation date (optional)'), input));
    card.appendChild(el('p', 'hint', item.files_by_date
      ? i18n.t('Date source: {source}. Destination: {destination}.',
        { source: item.date_source, destination: item.destination })
      : i18n.t('Keeps the original’s date and who may see it. Destination: {destination}, beside its source.',
        { destination: item.destination })));
    const approve = el('button', 'btn', i18n.t('Approve and file'));
    approve.type = 'button';
    approve.onclick = async () => {
      if (!input.reportValidity() || shell.uploads.busy) return;
      shell.uploads.busy = true;
      approve.disabled = true;
      try {
        // An empty date is not a date: send the key only when there is one,
        // or the server rejects the blank a derivative is allowed to leave.
        const result = await admin.postAdminUploadsByUploadIdApprove(item.id, {
          body: input.value ? { creation_date: input.value } : {},
        });
        toast(item.edited_from
          ? i18n.t('Approved the edit of {name}. Filed in {folder}.', { name: item.edited_from, folder: result.item.folder })
          : i18n.t('Approved {name}. Filed in {folder}.', { name: item.filename, folder: result.item.folder }));
      } catch (exc) { toast(exc.message, true); }
      finally {
        shell.uploads.busy = false;
        approve.disabled = false;
        await shell.loadPendingUploads();
      }
    };
    // Not every upload belongs in the library. Refusing one deletes the
    // uploaded file; the queue keeps no copy, and nothing is filed.
    const reject = el('button', 'btn ghost danger', i18n.t('Delete'));
    reject.type = 'button';
    reject.onclick = async () => {
      if (shell.uploads.busy) return;
      const what = item.edited_from
        ? i18n.t('the edit of {name}', { name: item.edited_from }) : item.filename;
      if (!window.confirm(`${i18n.t('Delete {what}?', { what })}\n\n`
        + i18n.t('It will not be added to the library, and the uploaded file is removed from this computer. This cannot be undone.'))) return;
      shell.uploads.busy = true;
      reject.disabled = true;
      try {
        await admin.postAdminUploadsByUploadIdReject(item.id, { body: {} });
        toast(i18n.t('Deleted {what}. It was not added to the library.', { what }));
      } catch (exc) { toast(exc.message, true); }
      finally {
        shell.uploads.busy = false;
        reject.disabled = false;
        await shell.loadPendingUploads();
      }
    };
    const actions = el('div', 'pending-upload-actions');
    actions.append(approve, reject);
    card.appendChild(actions);
    list.appendChild(card);
  }
}

/** The language changed: the next look draws the list again in it. */
export function relabel() {
  uploadSignature = '';
}

/* -- Phone backups (ninaivu/media/phone_backup.py) --------------------------- */

async function loadPhoneBackups() {
  const box = $('#pb-people');
  if (!box) return;
  let data;
  try {
    data = await admin.getAdminPhoneBackups();
  } catch (err) {
    // A server from before phone backup has no such endpoint.
    if (err.status === 404) { $('#pb-block').hidden = true; return; }
    toast(err.message, true);
    return;
  }
  $('#pb-trusted').checked = Boolean(data.trusted);
  box.replaceChildren();
  if (!data.people.length) {
    box.appendChild(el('p', 'hint subtle', i18n.t('Nobody has backed up a phone yet.')));
    return;
  }
  for (const person of data.people) {
    const row = el('div', 'pb-person');
    const when = person.last_backup
      ? i18n.t('last backup {when}', { when: new Date(person.last_backup * 1000).toLocaleString(i18n.locale()) })
      : i18n.t('nothing finished yet');
    const phones = person.phones === 1 ? i18n.t('1 phone')
      : i18n.t('{count} phones', { count: person.phones });
    row.appendChild(el('span', 'pb-who', person.name));
    row.appendChild(el('span', 'hint',
      `${i18n.t('{count} safe', { count: Number(person.safe || 0).toLocaleString() })} · ${phones} · ${when}`));
    if (person.waiting) {
      const approve = el('button', 'btn small',
        i18n.t('Approve all {count}', { count: person.waiting.toLocaleString() }));
      approve.type = 'button';
      approve.onclick = async () => {
        approve.disabled = true;
        try {
          const result = await admin.postAdminPhoneBackupsByUserIdApprove(person.user_id);
          const filed = { count: result.approved.toLocaleString(), name: person.name, failed: result.failed };
          toast(result.failed
            ? i18n.t('Filed {count} of {name}’s backups; {failed} could not be filed.', filed)
            : i18n.t('Filed {count} of {name}’s backups.', filed), result.failed > 0);
          shell.loadPendingUploads();
          loadPhoneBackups();
        } catch (err) {
          toast(err.message, true);
          approve.disabled = false;
        }
      };
      row.appendChild(approve);
    }
    box.appendChild(row);
  }
}

/* -- Phone keys (ninaivu/media/phone_keys.py) --------------------------------- */

async function loadPhoneKeys() {
  const box = $('#pk-list');
  if (!box) return;
  let data;
  try {
    data = await admin.getAdminPhoneKeys();
  } catch (err) {
    if (err.status !== 404) toast(err.message, true);
    return;
  }
  box.replaceChildren();
  if (!data.keys.length) {
    box.appendChild(el('p', 'hint subtle', i18n.t('No phone keys yet.')));
    return;
  }
  for (const item of data.keys) {
    const row = el('div', 'pb-person');
    row.appendChild(el('span', 'pb-who', `${item.person} · ${item.name}`));
    const used = item.last_used_at
      ? i18n.t('{count} sent · last used {when}', {
        count: Number(item.files || 0).toLocaleString(),
        when: new Date(item.last_used_at * 1000).toLocaleString(i18n.locale()),
      })
      : i18n.t('Not used yet');
    row.appendChild(el('span', 'hint', `…${item.hint} · ${used}`));
    const revoke = el('button', 'btn small danger', i18n.t('Revoke'));
    revoke.type = 'button';
    revoke.onclick = async () => {
      if (!window.confirm(i18n.t('Revoke the key for {name}? The app on that phone will stop backing up until it has a new key.', { name: item.name }))) return;
      revoke.disabled = true;
      try {
        await admin.deleteAdminPhoneKeysByKeyId(item.id);
        toast(i18n.t('Key revoked'));
      } catch (err) {
        toast(err.message, true);
      }
      loadPhoneKeys();
    };
    row.appendChild(revoke);
    box.appendChild(row);
  }
}

/* -- The TV album (ninaivu/server/tv_album.py) --------------------------------- */

function showTvStatus(status) {
  $('#tv-port').value = status.port || '';
  const select = $('#tv-album-select');
  if (select) select.value = String(status.album_id || 0);
  let line;
  if (!status.album_id) line = i18n.t('Off. Choose an album to show it on the TVs at home.');
  else if (status.running) {
    line = i18n.t('Showing “{name}” ({count} photos and videos) to the TVs at home.',
      { name: status.album_name || '', count: Number(status.items || 0).toLocaleString() });
  } else {
    line = i18n.t('Not running: {reason}', { reason: status.problem || i18n.t('starting…') });
  }
  $('#tv-status').textContent = line;
  const list = $('#tv-addresses');
  list.replaceChildren();
  for (const address of status.addresses || []) list.appendChild(el('li', '', address));
  $('#tv-secret').disabled = !status.album_id;
}

export async function loadTvAlbum() {
  if (!$('#tv-block')) return;
  let status;
  let albums;
  try {
    [status, albums] = await Promise.all([admin.getAdminTvAlbum(), admin.getAlbums()]);
  } catch (err) {
    if (err.status === 404) { $('#tv-block').hidden = true; return; }
    toast(err.message, true);
    return;
  }
  const select = $('#tv-album-select');
  select.replaceChildren(el('option', '', i18n.t('Off')));
  select.firstChild.value = '0';
  for (const album of albums.albums || []) {
    const option = el('option', '', album.name);         // family-written, as text
    option.value = String(album.id);
    select.appendChild(option);
  }
  showTvStatus(status);
}

async function saveTvAlbum() {
  const button = $('#tv-save');
  button.disabled = true;
  try {
    const body = { album_id: Number($('#tv-album-select').value) || 0 };
    const port = Number($('#tv-port').value);
    if (port) body.port = port;
    showTvStatus(await admin.putAdminTvAlbum({ body }));
    toast(body.album_id ? i18n.t('TV album saved') : i18n.t('TV album turned off'));
  } catch (err) {
    toast(err.message, true);
  } finally {
    button.disabled = false;
  }
}

async function newTvAddresses() {
  if (!window.confirm(i18n.t('Give the TV album new addresses? TVs find it again by themselves; any address typed in by hand stops working.'))) return;
  try {
    showTvStatus(await admin.postAdminTvAlbumSecret());
  } catch (err) {
    toast(err.message, true);
  }
}

async function savePhoneBackupTrust(trusted) {
  try {
    await admin.postAdminPhoneBackupsSettings({ body: { trusted } });
    toast(trusted ? i18n.t('Family phone backups are filed without asking')
      : i18n.t('Family phone backups wait for approval'));
  } catch (err) {
    toast(err.message, true);
  }
  loadPhoneBackups();
}

/** Bind the pages' controls, and draw what is held: once, when first opened. */
export function wire(context) {
  shell = context;
  $$('#tv-save').onclick = () => saveTvAlbum();
  $$('#tv-secret').onclick = () => newTvAddresses();
  $$('#pb-trusted').onchange = (event) => savePhoneBackupTrust(event.target.checked);
  render(shell.uploads.last);
}

/** The Uploads page has been opened, or refreshed. */
export function show() {
  loadPhoneBackups();
  loadPhoneKeys();
}
