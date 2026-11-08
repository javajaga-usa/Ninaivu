/**
 * The family app's upload dialog, loaded when first opened (app.js, openPage).
 */

import * as i18n from '../../i18n.js';
import { $, toast } from '../../ui.js';

/** The gallery around the dialog (app.js): to show what came in. */
let shell;

/* -- Local Upload ------------------------------------------------------- */
/** Bind the upload dialog: once, when it is first opened. */
export function wire(context) {
  shell = context;
  const modal = $('#upload-modal');
  const dropzone = $('#upload-dropzone');
  const fileInput = $('#upload-file-input');
  const closeBtn = $('#upload-close');

  closeBtn?.addEventListener('click', () => {
    modal.hidden = true;
    shell.reload();
  });

  dropzone?.addEventListener('click', () => fileInput?.click());
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
  if (!files.length) return;
  const progress = $('#upload-progress');
  const fill = $('#upload-fill');
  const list = $('#upload-list');
  progress.hidden = false;
  fill.style.width = '20%';

  const formData = new FormData();
  files.forEach((f) => formData.append('files', f));

  try {
    fill.style.width = '60%';
    // X-Requested-With: multipart is what a cross-origin form could also
    // send, so the server takes one only from a script that says so. See
    // _refuse_cross_origin_writes in ninaivu/__init__.py.
    const response = await fetch('/api/upload', {
      method: 'POST',
      headers: { 'X-Requested-With': 'fetch' },
      body: formData,
    });
    const result = await response.json();
    fill.style.width = '100%';

    if (!response.ok) throw new Error(i18n.fromServer(result, i18n.t('Upload failed')));

    const count = result.total ?? result.uploaded?.length ?? 0;
    const failed = files.length - count;
    const sent = result.pending_approval
      ? i18n.t('Uploaded {items} for an administrator to approve.', { items: i18n.items(count) })
      : i18n.t('Uploaded {items}.', { items: i18n.items(count) });
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
    setTimeout(() => { progress.hidden = true; }, 1200);
  } catch (err) {
    toast(i18n.t('Upload failed: {reason}', { reason: err.message }), true);
    progress.hidden = true;
  }
}

/** Open the upload dialog (the top bar's Upload button). */
export function open() {
  $('#upload-modal').hidden = false;
}
