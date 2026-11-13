/**
 * The family app's upload dialog, loaded when first opened (app.js, openPage).
 */

import * as i18n from '../../i18n.js';
import { $, toast } from '../../ui.js';
import { reportUnauthorized } from '../../http.js';
import { registerDialog } from '../../dialogs.js';

/** The gallery around the dialog (app.js): to show what came in. */
let shell;
let uploading = false;
let progressTimer;
let dialog;

/* -- Local Upload ------------------------------------------------------- */
/** Bind the upload dialog: once, when it is first opened. */
export function wire(context) {
  shell = context;
  const modal = $('#upload-modal');
  const dropzone = $('#upload-dropzone');
  const fileInput = $('#upload-file-input');
  const closeBtn = $('#upload-close');

  dialog = registerDialog(modal, { escape: true, backdrop: true, initialFocus: '#upload-dropzone',
    onClose: () => shell.reload() });
  closeBtn?.addEventListener('click', () => dialog.close());

  dropzone?.addEventListener('click', () => { if (!uploading) fileInput?.click(); });
  dropzone?.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      if (!uploading) fileInput?.click();
    }
  });
  fileInput?.addEventListener('change', (e) => {
    if (e.target.files?.length) handleUpload(Array.from(e.target.files));
  });

  dropzone?.addEventListener('dragover', (e) => {
    e.preventDefault();
    dropzone.classList.add('dragover');
  });
  dropzone?.addEventListener('dragleave', () => dropzone.classList.remove('dragover'));
  dropzone?.addEventListener('drop', (e) => {
    e.preventDefault();
    dropzone.classList.remove('dragover');
    if (e.dataTransfer.files?.length) {
      handleUpload(Array.from(e.dataTransfer.files));
    }
  });
}

async function handleUpload(files) {
  if (!files.length || uploading) return;
  uploading = true;
  clearTimeout(progressTimer);
  const dropzone = $('#upload-dropzone');
  const fileInput = $('#upload-file-input');
  fileInput.disabled = true;
  dropzone.setAttribute('aria-disabled', 'true');
  dropzone.setAttribute('aria-busy', 'true');
  const progress = $('#upload-progress');
  const fill = $('#upload-fill');
  const list = $('#upload-list');
  const status = $('#upload-status');
  status.hidden = false;
  status.textContent = i18n.t('Uploading…');
  progress.hidden = false;
  progress.setAttribute('aria-valuenow', '0');
  fill.style.width = '0%';

  const formData = new FormData();
  files.forEach((f) => formData.append('files', f));

  try {
    // X-Requested-With: multipart is what a cross-origin form could also
    // send, so the server takes one only from a script that says so. See
    // _refuse_cross_origin_writes in ninaivu/__init__.py.
    const result = await new Promise((resolve, reject) => {
      const request = new XMLHttpRequest();
      request.open('POST', '/api/upload');
      request.setRequestHeader('X-Requested-With', 'fetch');
      request.responseType = 'json';
      request.upload.onprogress = (event) => {
        if (!event.lengthComputable) return;
        const percent = Math.round(event.loaded / event.total * 100);
        fill.style.width = `${percent}%`;
        progress.setAttribute('aria-valuenow', String(percent));
      };
      request.upload.onload = () => {
        // Bytes have arrived; the server is still validating and filing them.
        progress.removeAttribute('aria-valuenow');
        status.textContent = i18n.t('Finishing up');
      };
      request.onload = () => {
        if (request.status === 401) reportUnauthorized('/api/upload');
        if (request.status < 200 || request.status >= 300 || !request.response) {
          reject(new Error(i18n.fromServer(request.response || {}, i18n.t('Upload failed'))));
        } else resolve(request.response);
      };
      request.onerror = request.onabort = () => reject(new Error(i18n.t('Upload failed')));
      request.send(formData);
    });
    fill.style.width = '100%';
    progress.setAttribute('aria-valuenow', '100');

    const count = result.total ?? result.uploaded?.length ?? 0;
    const failed = files.length - count;
    const sent = result.pending_approval
      ? i18n.t('Uploaded {items} for an administrator to approve.', { items: i18n.items(count) })
      : i18n.t('Uploaded {items}.', { items: i18n.items(count) });
    status.textContent = failed > 0
      ? `${sent} ${i18n.t('{count} could not be uploaded.', { count: failed.toLocaleString(i18n.locale()) })}`
      : sent;
    toast(failed > 0
      ? `${sent} ${i18n.t('{count} could not be uploaded.', { count: failed.toLocaleString(i18n.locale()) })}`
      : sent, failed > 0);
    for (const error of result.errors || []) toast(`${error.filename}: ${error.error}`, true);
    if (count && !result.pending_approval) shell.reload();
    $('#upload-file-input').value = '';
    if (list) {
      (result.uploaded || []).forEach((u) => {
        const item = document.createElement('div');
        item.className = 'upload-item';
        // Built as nodes, not markup: a filename is whatever the person
        // uploading chose to call the file, and every other line in this
        // codebase treats such text as text.
        const tick = document.createElement('span');
        tick.append('✓ ');
        const name = document.createElement('strong');
        name.textContent = u.filename;
        tick.appendChild(name);
        const where = document.createElement('span');
        where.className = 'hint';
        where.textContent = result.pending_approval ? i18n.t('Awaiting admin approval') : u.rel_path;
        item.replaceChildren(tick, where);
        list.prepend(item);
      });
    }
    progressTimer = setTimeout(() => { progress.hidden = true; }, 1200);
  } catch (err) {
    status.textContent = i18n.t('Upload failed: {reason}', { reason: err.message });
    toast(i18n.t('Upload failed: {reason}', { reason: err.message }), true);
    progress.hidden = true;
  } finally {
    uploading = false;
    fileInput.disabled = false;
    // Selecting the same file again must fire change after a failed attempt.
    fileInput.value = '';
    dropzone.removeAttribute('aria-disabled');
    dropzone.removeAttribute('aria-busy');
  }
}

/** Open the upload dialog (the top bar's Upload button). */
export function open() {
  dialog.open();
}
