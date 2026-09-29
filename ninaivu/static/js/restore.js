/**
 * Restore from Google Drive — the Cloud page's way back.
 *
 * Three questions and a check before anything starts: what to bring back,
 * where to put it, and (for an encrypted backup) the key. "Check what this
 * would restore" asks the server for the same list the restore would work
 * from, so the numbers on screen are the numbers that will be restored, and
 * the Start button stays off until they have been seen.
 *
 * While a restore runs this polls its own status, once a second while the
 * panel is on screen, and shows every file that failed a check with the
 * reason — a restore that quietly skipped files would be worse than none.
 */

import { reportUnauthorized } from './api.js';
import * as i18n from './i18n.js';

const $ = (sel) => document.querySelector(sel);

async function json(url, options = {}) {
  const response = await fetch(url, {
    headers: {
      Accept: 'application/json',
      ...(options.body ? { 'Content-Type': 'application/json' } : {}),
    },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const text = await response.text();
  const data = text ? JSON.parse(text) : null;
  if (!response.ok) {
    if (response.status === 401) reportUnauthorized(url);
    throw Object.assign(new Error(data?.error || response.statusText),
      { status: response.status, data });
  }
  return data;
}

const api = {
  preview: (body) => json('/api/cloud/restore/preview', { method: 'POST', body }),
  start: (body) => json('/api/cloud/restore/start', { method: 'POST', body }),
  status: () => json('/api/cloud/restore/status'),
  stop: () => json('/api/cloud/restore/stop', { method: 'POST' }),
};

const bytes = (n) => {
  const value = Number(n) || 0;
  if (value < 1024) return `${value} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let size = value / 1024;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
  return `${size < 10 ? size.toFixed(1) : Math.round(size)} ${units[unit]}`;
};

export class RestoreWizard {
  constructor({ toast }) {
    this.toast = toast || (() => {});
    this.visible = false;
    this.timer = null;
    this.recovery = null;
    this.checked = null;       // the request the preview on screen answers
    this.preview = null;       // what the preview on screen was drawn from
    this.state = null;         // the last restore status drawn
    i18n.onChange(() => {
      if (!$('#rs-block')) return;
      this.formChanged();
      if (this.preview && !$('#rs-preview').hidden) this.renderPreview(...this.preview);
      if (this.state) this.render(this.state);
    });
  }

  wire() {
    if (!$('#rs-block')) return;
    document.querySelectorAll('input[name="rs-what"], input[name="rs-where"]')
      .forEach((input) => { input.onchange = () => this.formChanged(); });
    ['#rs-folder', '#rs-dest', '#rs-passphrase'].forEach((sel) => {
      $(sel).oninput = () => this.formChanged();
    });
    $('#rs-from-drive').onchange = () => this.formChanged();
    $('#rs-recovery').onchange = (event) => this.readRecovery(event.target.files?.[0]);
    $('#rs-check').onclick = () => this.check();
    $('#rs-start').onclick = () => this.start();
    $('#rs-stop').onclick = () => this.stop();
    $('#rs-again').onclick = () => this.showForm();
    this.formChanged();
  }

  show() {
    this.visible = true;
    this.poll();
  }

  hide() {
    this.visible = false;
    clearTimeout(this.timer);
    this.timer = null;
  }

  /* -- the form -------------------------------------------------------- */

  request() {
    const what = document.querySelector('input[name="rs-what"]:checked')?.value;
    const where = document.querySelector('input[name="rs-where"]:checked')?.value;
    const fromDrive = $('#rs-from-drive').checked;
    const scope = { source: fromDrive ? 'drive' : 'record' };
    if (what === 'folder') scope.folder = $('#rs-folder').value.trim();
    const body = { scope };
    // Drive alone does not know where a file came from, so a restore read
    // from it always needs somewhere to go.
    if (where === 'folder' || fromDrive) body.destination = $('#rs-dest').value.trim();
    return body;
  }

  formChanged() {
    const what = document.querySelector('input[name="rs-what"]:checked')?.value;
    const fromDrive = $('#rs-from-drive').checked;
    $('#rs-folder-row').hidden = what !== 'folder';
    const inPlace = document.querySelector('input[name="rs-where"][value="original"]');
    if (fromDrive && inPlace.checked) {
      document.querySelector('input[name="rs-where"][value="folder"]').checked = true;
    }
    inPlace.disabled = fromDrive;
    $('#rs-in-place-label').title = fromDrive
      ? i18n.t('Files read from Drive do not say which library folder they came from')
      : '';
    const where = document.querySelector('input[name="rs-where"]:checked')?.value;
    $('#rs-dest-row').hidden = where !== 'folder';
    // Anything changed since the check means the check no longer describes
    // what Start would do.
    if (this.checked !== JSON.stringify(this.request())) {
      $('#rs-start').disabled = true;
      if (this.checked) $('#rs-preview').hidden = true;
    }
  }

  async readRecovery(file) {
    this.recovery = null;
    $('#rs-recovery-name').textContent = '';
    if (!file) return;
    try {
      const parsed = JSON.parse(await file.text());
      if (!parsed || typeof parsed.key !== 'string') throw new Error(i18n.t('no key in it'));
      this.recovery = parsed;
      $('#rs-recovery-name').textContent = i18n.t('{file} · key {id}', { file: file.name, id: parsed.key_id || '' });
    } catch (exc) {
      this.toast(i18n.t('That is not a Ninaivu recovery file ({reason})', { reason: exc.message }), true);
    }
  }

  async check() {
    const body = this.request();
    if (body.scope.folder === '') {
      this.toast(i18n.t('Type the folder to restore, or choose everything'), true);
      return;
    }
    if (body.destination === '') {
      this.toast(i18n.t('Type the folder to restore into'), true);
      return;
    }
    const button = $('#rs-check');
    button.disabled = true;
    button.textContent = body.scope.source === 'drive'
      ? i18n.t('Reading the Drive folder…') : i18n.t('Checking…');
    // A key given already travels with the check: an encrypted copy of the
    // index has to be decrypted before it can say what it holds.
    const ask = { scope: body.scope };
    if (this.recovery) ask.recovery = this.recovery;
    if ($('#rs-passphrase').value) ask.passphrase = $('#rs-passphrase').value;
    try {
      const data = await api.preview(ask);
      this.renderPreview(data, body);
    } catch (exc) {
      if (exc.data?.needs_key) {
        $('#rs-key').hidden = false;
        $('#rs-key-note').textContent = exc.message;
        $('#rs-preview').hidden = true;
      } else {
        this.toast(exc.message, true);
      }
    } finally {
      button.disabled = false;
      button.textContent = i18n.t('Check what this would restore');
    }
  }

  renderPreview(data, body) {
    this.preview = [data, body];
    const box = $('#rs-preview');
    box.replaceChildren();
    box.hidden = false;
    const head = document.createElement('strong');
    if (!data.files) {
      head.textContent = i18n.t('Nothing in the backup matches that.');
      box.append(head);
      $('#rs-start').disabled = true;
      return;
    }
    head.textContent = data.files === 1
      ? i18n.t('1 file, {size}', { size: bytes(data.bytes) })
      : i18n.t('{count} files, {size}', { count: Number(data.files).toLocaleString(), size: bytes(data.bytes) });
    box.append(head);
    if (data.from_index_copy) {
      const copy = document.createElement('p');
      const when = data.from_index_copy.modified
        ? new Date(data.from_index_copy.modified).toLocaleString(i18n.locale()) : i18n.t('recently');
      copy.textContent = i18n.t('Read from the copy of the index kept in Drive (sent {when}): every folder comes back as it was, and every file is checked against the checksum recorded when it was sent. Albums, faces and names can be put back afterwards — the restore says how.', { when });
      box.append(copy);
    }
    const list = document.createElement('ul');
    const add = (text) => {
      const item = document.createElement('li');
      item.textContent = text;
      list.append(item);
    };
    for (const folder of (data.folders || []).slice(0, 8)) {
      const name = folder.folder || i18n.t('(the top of the library)');
      add(folder.files === 1 ? i18n.t('{folder} — 1 file', { folder: name })
        : i18n.t('{folder} — {count} files', { folder: name, count: Number(folder.files).toLocaleString() }));
    }
    if ((data.folders || []).length > 8) add(i18n.t('and {count} more folders', { count: data.folders.length - 8 }));
    add(body.destination
      ? i18n.t('Into {folder}', { folder: body.destination })
      : i18n.t('Back into the library folders they came from'));
    box.append(list);

    const keyBox = $('#rs-key');
    keyBox.hidden = !data.needs_key;
    if (data.needs_key) {
      const one = data.encrypted === 1;
      const encrypted = { count: Number(data.encrypted).toLocaleString() };
      if (data.key_on_this_machine) {
        $('#rs-key-note').textContent = one
          ? i18n.t('1 file is encrypted. This computer has its key; give the recovery file or passphrase only if it was made with a different one.')
          : i18n.t('{count} files are encrypted. This computer has its key; give the recovery file or passphrase only if these were made with a different one.', encrypted);
      } else {
        $('#rs-key-note').textContent = one
          ? i18n.t('1 file is encrypted, and this computer does not have the key. Choose the recovery file, or type the passphrase.')
          : i18n.t('{count} files are encrypted, and this computer does not have the key. Choose the recovery file, or type the passphrase.', encrypted);
      }
    }
    this.checked = JSON.stringify(body);
    $('#rs-start').disabled = false;
  }

  async start() {
    const body = this.request();
    if (this.recovery) body.recovery = this.recovery;
    const passphrase = $('#rs-passphrase').value;
    if (passphrase) body.passphrase = passphrase;
    $('#rs-start').disabled = true;
    try {
      const data = await api.start(body);
      $('#rs-passphrase').value = '';
      this.toast(i18n.t('Restore started'));
      this.render(data.restore);
      this.poll();
    } catch (exc) {
      this.toast(exc.message, true);
      $('#rs-start').disabled = false;
    }
  }

  async stop() {
    try {
      this.render((await api.stop()).restore);
      this.toast(i18n.t('Stopping after the file in hand'));
    } catch (exc) {
      this.toast(exc.message, true);
    }
  }

  showForm() {
    $('#rs-run').hidden = true;
    $('#rs-form').hidden = false;
    this.state = null;
    $('#rs-state').textContent = i18n.t('nothing running');
  }

  /* -- a running restore ----------------------------------------------- */

  async poll() {
    clearTimeout(this.timer);
    if (!this.visible) return;
    let state = null;
    try {
      state = await api.status();
      this.render(state);
    } catch (exc) {
      if (exc.status !== 401 && exc.status !== 404) this.toast(exc.message, true);
    }
    if (state?.running) this.timer = setTimeout(() => this.poll(), 1000);
  }

  render(state) {
    if (!state || (!state.running && !state.ended_at)) return;
    this.state = state;
    $('#rs-form').hidden = Boolean(state.running);
    $('#rs-run').hidden = false;
    $('#rs-stop').hidden = !state.running;
    $('#rs-again').hidden = Boolean(state.running);
    $('#rs-fill').style.width = `${state.percent || 0}%`;
    $('#rs-state').textContent = state.running
      ? (state.stopping ? i18n.t('stopping…') : i18n.t('restoring · {percent}%', { percent: state.percent || 0 }))
      : (state.finished ? i18n.t('finished') : i18n.t('stopped'));
    $('#rs-current').textContent = state.current || '';
    const parts = [
      i18n.t('{done} of {total}', {
        done: Number(state.processed || 0).toLocaleString(), total: Number(state.total || 0).toLocaleString() }),
      i18n.t('{done} of {total}', { done: bytes(state.bytes_done), total: bytes(state.total_bytes) }),
    ];
    if (state.already_there) parts.push(i18n.t('{count} already there', { count: state.already_there.toLocaleString() }));
    if (state.beside) parts.push(i18n.t('{count} put beside a different file', { count: state.beside.toLocaleString() }));
    if (state.failed) parts.push(i18n.t('{count} failed', { count: state.failed.toLocaleString() }));
    $('#rs-counts').textContent = parts.join(' · ');
    $('#rs-message').textContent = state.running ? '' : (state.message || '');

    const problems = state.problems || [];
    $('#rs-problems-wrap').hidden = !problems.length;
    const body = $('#rs-problems');
    body.replaceChildren(...problems.map((problem) => {
      const row = document.createElement('tr');
      const file = document.createElement('td');
      file.textContent = problem.file;
      const why = document.createElement('td');
      why.className = 'cl-detail';
      why.textContent = problem.why;
      row.append(file, why);
      return row;
    }));
  }
}
