/**
 * The Cloud tab — one-way backup to Google Drive.
 *
 * Self-contained, like the Archive panel: the console hands it a toast
 * function and it owns everything under `[data-panel="cloud"]`.
 *
 * Two things drive how this is written.
 *
 * **It polls, and only while it is on screen.** An upload of a family library
 * runs for days. A stream held open by a tab nobody is looking at is a thread
 * and a database reader spent on nothing, so the panel starts a two-second
 * poll when it is shown and stops it when it is not — and slows to a crawl
 * when there is nothing running.
 *
 * **It says what is true, not what is comforting.** "Uploading" with no file
 * name and no bytes moving is exactly the screen that makes somebody restart
 * a server that was working. So the status line carries the file, the
 * position within it, and how much this run has actually sent.
 */

import { reportUnauthorized } from './api.js';
import { RestoreWizard } from './restore.js';
import { IndexCopyStatus, RestoreTests } from './restore-tests.js';

const $ = (sel) => document.querySelector(sel);

const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
};

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
  status: () => json('/api/cloud/status'),
  settings: (body) => json('/api/cloud/settings', { method: 'POST', body }),
  client: (body) => json('/api/cloud/client', { method: 'POST', body }),
  connect: () => json('/api/cloud/connect', { method: 'POST' }),
  disconnect: () => json('/api/cloud/disconnect', { method: 'POST' }),
  start: () => json('/api/cloud/start', { method: 'POST' }),
  pause: () => json('/api/cloud/pause', { method: 'POST' }),
  retry: () => json('/api/cloud/retry', { method: 'POST' }),
  queue: () => json('/api/cloud/queue', { method: 'POST' }),
  createKey: (body) => json('/api/cloud/encryption/key', { method: 'POST', body }),
  recovery: () => json('/api/cloud/encryption/recovery'),
};

/** Two seconds while something is moving; twenty when nothing is. */
const FAST = 2000;
const SLOW = 20000;

const bytes = (n) => {
  const value = Number(n) || 0;
  if (value < 1024) return `${value} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let size = value / 1024;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
  return `${size < 10 ? size.toFixed(1) : Math.round(size)} ${units[unit]}`;
};

/** A wall-clock stamp from the server as the time of day it is here. */
const clock = (stamp) => {
  const when = Number(stamp) || 0;
  if (!when) return '';
  return new Date(when * 1000).toLocaleTimeString([],
    { hour: '2-digit', minute: '2-digit' });
};

const STATE_WORDS = {
  done: 'Uploaded',
  pending: 'Waiting',
  uploading: 'Uploading',
  failed: 'Failed',
  skipped: 'Kept back',
};

export class CloudPanel {
  constructor({ toast }) {
    this.toast = toast || (() => {});
    this.status = null;
    this.view = 'recent';
    this.timer = null;
    this.visible = false;
    this.restore = new RestoreWizard({ toast: this.toast });
    this.tests = new RestoreTests({ toast: this.toast });
    this.indexCopy = new IndexCopyStatus({ toast: this.toast });
  }

  /* -- wiring ---------------------------------------------------------- */

  wire() {
    $('#cl-save-client').onclick = () => this.saveClient();
    $('#cl-connect').onclick = () => this.connect();
    $('#cl-disconnect').onclick = () => this.disconnect();
    $('#cl-start').onclick = () => this.run(api.start(), 'Upload started');
    $('#cl-pause').onclick = () => this.run(api.pause(), 'Paused');
    $('#cl-retry').onclick = () => this.run(api.retry(), 'Failures queued again');
    $('#cl-queue').onclick = () => this.checkForNew();
    $('#cl-save-folder').onclick = () => this.saveFolder();
    $('#cl-save-rate').onclick = () => this.saveRate();
    $('#cl-save-window').onclick = () => this.saveWindow();
    // Guarded: markup cached from before this control existed must not stop
    // everything wired after it.
    const saveHidden = $('#cl-save-hidden');
    if (saveHidden) saveHidden.onclick = () => this.saveHidden();
    const saveParallel = $('#cl-save-parallel');
    if (saveParallel) saveParallel.onclick = () => this.saveParallel();
    const fullSpeed = $('#cl-full-speed');
    if (fullSpeed) fullSpeed.onchange = () => this.saveFullSpeed();
    $('#cl-copy-uri').onclick = () => this.copyRedirect();
    $('#cl-enc-create').onclick = () => this.createKey();
    $('#cl-enc-recovery').onclick = () => this.downloadRecovery();
    $('#cl-encrypt').onchange = () => this.saveEncrypt();

    ['enabled', 'autostart'].forEach((key) => {
      const box = $(`#cl-${key}`);
      if (box) box.onchange = () => this.saveSwitches();
    });

    document.querySelectorAll('#cl-filters button').forEach((button) => {
      button.onclick = () => {
        this.view = button.dataset.view;
        document.querySelectorAll('#cl-filters button').forEach(
          (other) => other.classList.toggle('active', other === button));
        this.renderTable();
      };
    });

    this.restore.wire();
    this.tests.wire();
    this.indexCopy.wire();

    // Coming back from Google's consent screen lands on `#cloud?connected=1`.
    this.readReturn();
  }

  readReturn() {
    const hash = location.hash || '';
    if (!hash.startsWith('#cloud')) return;
    const query = new URLSearchParams(hash.split('?')[1] || '');
    if (query.get('connected')) this.toast('Google account connected');
    if (query.get('error')) {
      this.toast(`Google sign-in failed: ${decodeURIComponent(query.get('error'))}`,
        true);
    }
    if (query.get('connected') || query.get('error')) {
      history.replaceState(null, '', '#cloud');
    }
  }

  /* -- showing and hiding ---------------------------------------------- */

  show() {
    this.visible = true;
    this.refresh();
    this.tests.show();
    this.indexCopy.show();
  }

  hide() {
    this.visible = false;
    this.tests.hide();
    this.indexCopy.hide();
    clearTimeout(this.timer);
    this.timer = null;
  }

  // Restore has a page of its own (Backup & health → Restore): the careful,
  // rare task apart from the everyday backup page. Same wizard, shown there.
  showRestore() { this.restore.show(); }
  hideRestore() { this.restore.hide(); }

  schedule() {
    clearTimeout(this.timer);
    if (!this.visible) return;
    const busy = this.status?.running;
    this.timer = setTimeout(() => this.refresh(), busy ? FAST : SLOW);
  }

  async refresh() {
    try {
      this.status = await api.status();
      this.render();
    } catch (exc) {
      if (exc.status !== 401) this.toast(exc.message, true);
    }
    this.schedule();
  }

  /* -- actions ---------------------------------------------------------- */

  async run(promise, message) {
    try {
      this.status = await promise;
      this.render();
      if (message) this.toast(message);
    } catch (exc) {
      this.toast(exc.message, true);
    }
    this.schedule();
  }

  async checkForNew() {
    try {
      const data = await api.queue();
      this.status = data;
      this.render();
      this.toast(data.queued
        ? `${data.queued} new ${data.queued === 1 ? 'file' : 'files'} queued`
        : 'Nothing new — everything is already uploaded or waiting');
    } catch (exc) {
      this.toast(exc.message, true);
    }
  }

  async saveClient() {
    const clientId = $('#cl-client-id').value.trim();
    const secret = $('#cl-client-secret').value.trim();
    if (!clientId || !secret) {
      this.toast('Both the client ID and the secret are needed', true);
      return;
    }
    try {
      this.status = await api.client({ client_id: clientId, client_secret: secret });
      // The secret is not shown again, and is not kept in the page.
      $('#cl-client-secret').value = '';
      this.render();
      this.toast('Saved. Now connect the account.');
    } catch (exc) {
      this.toast(exc.message, true);
    }
  }

  async saveSwitches() {
    await this.run(api.settings({
      enabled: $('#cl-enabled').checked,
      autostart: $('#cl-autostart').checked,
    }), '');
  }

  async createKey() {
    const passphrase = $('#cl-enc-pass').value;
    const confirm = $('#cl-enc-confirm').value;
    const button = $('#cl-enc-create');
    button.disabled = true;
    try {
      const data = await api.createKey({ passphrase, confirm });
      $('#cl-enc-pass').value = '';
      $('#cl-enc-confirm').value = '';
      this.saveRecoveryFile(data.recovery);
      this.status = data;
      this.render();
      this.toast(this.status?.encryption?.enabled
        ? 'Encryption key made. Keep the recovery file safe; every upload is encrypted with it.'
        : 'Encryption key made. Keep the recovery file safe, then switch encryption on.');
    } catch (exc) {
      this.toast(exc.message, true);
    } finally {
      button.disabled = false;
    }
  }

  async downloadRecovery() {
    try {
      this.saveRecoveryFile(await api.recovery());
    } catch (exc) {
      this.toast(exc.message, true);
    }
  }

  saveRecoveryFile(documentBody) {
    const blob = new Blob([JSON.stringify(documentBody, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `ninaivu-recovery-${documentBody.key_id}.json`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  async saveEncrypt() {
    const box = $('#cl-encrypt');
    const wanted = box.checked;
    await this.run(api.settings({ encrypt: wanted }),
      wanted ? 'New uploads will be encrypted'
        : 'Encryption off — new uploads, and the copy of the index with everybody’s names and faces, go up as they are');
  }

  async saveFolder() {
    const name = $('#cl-folder').value.trim();
    if (!name) return;
    await this.run(api.settings({ folder_name: name }),
      `Uploads will go to “${name}”`);
  }

  async saveHidden() {
    const mode = $('#cl-hidden').value;
    await this.run(api.settings({ hidden: mode }), {
      never: 'Hidden things are left out of the backup',
      encrypted: 'Hidden things go while the backup is encrypted',
      always: 'Hidden things are backed up too',
    }[mode] || 'Saved');
  }

  async saveParallel() {
    const count = Math.max(1, Math.round(Number($('#cl-parallel').value) || 1));
    await this.run(api.settings({ parallel: count }),
      count > 1 ? `${count} files go up at once` : 'One file at a time');
  }

  async saveFullSpeed() {
    const on = $('#cl-full-speed').checked;
    await this.run(api.settings({ full_speed: on }), on
      ? 'The backup keeps going while people use Ninaivu'
      : 'The backup makes way for the household again');
  }

  async saveRate() {
    const rate = Math.max(0, Math.round(Number($('#cl-rate').value) || 0));
    await this.run(api.settings({ rate_kbps: rate }),
      rate ? `Uploads limited to ${rate} KB/s` : 'Speed limit removed');
  }

  async saveWindow() {
    // The message has to come from the answer rather than the fields: the
    // server normalises the times, and an empty pair means something quite
    // different from a pair of hours.
    try {
      this.status = await api.settings({
        window_start: $('#cl-window-start').value,
        window_end: $('#cl-window-end').value,
      });
      this.render();
      this.toast(this.status.window_label
        ? `Uploads run ${this.status.window_label}`
        : 'Uploads may run at any hour');
    } catch (exc) {
      this.toast(exc.message, true);
    }
    this.schedule();
  }

  async connect() {
    try {
      const { url } = await api.connect();
      // Same tab: Google sends the browser back to the console, and the
      // callback puts it on this panel again.
      location.href = url;
    } catch (exc) {
      this.toast(exc.message, true);
    }
  }

  async disconnect() {
    const account = this.status?.account?.account || 'this account';
    if (!window.confirm(
      `Disconnect ${account}?\n\nNothing is deleted from Drive, and Ninaivu `
      + 'keeps its record of what has already been uploaded — so reconnecting '
      + 'will not send the whole library again.')) return;
    await this.run(api.disconnect(), 'Disconnected');
  }

  async copyRedirect() {
    const uri = this.status?.redirect_uri || '';
    try {
      await navigator.clipboard.writeText(uri);
      this.toast('Redirect URI copied');
    } catch {
      this.toast(uri);
    }
  }

  /* -- rendering -------------------------------------------------------- */

  render() {
    const data = this.status;
    if (!data) return;
    this.renderAccount(data);
    this.renderSettings(data);
    this.renderRun(data);
    this.renderTable();
  }

  renderAccount(data) {
    const account = data.account || {};
    const uri = $('#cl-redirect');
    if (uri) uri.textContent = data.redirect_uri || '';

    // Google checks its own rules before it checks the client's list, so an
    // address it will not accept fails as a mismatch however carefully it was
    // pasted in. Say which, and give the address that does work.
    const warning = $('#cl-redirect-warning');
    if (warning) {
      const problem = data.redirect_uri_problem;
      warning.hidden = !problem;
      if (problem) {
        const here = (data.redirect_uri_loopback || '').replace('/api/cloud/callback', '');
        warning.textContent = `${problem} Open this console on the Ninaivu computer`
          + (here ? ` at ${here}` : '')
          + ' and connect from there — that address is one Google accepts.';
      }
    }

    const tag = $('#cl-account');
    if (account.connected) {
      tag.textContent = account.account || 'Connected';
    } else if (account.configured) {
      tag.textContent = 'Not connected';
    } else {
      tag.textContent = 'Needs setup';
    }

    $('#cl-setup').hidden = Boolean(account.connected);
    $('#cl-disconnect').hidden = !account.connected;
    $('#cl-connect').textContent = account.connected
      ? 'Connect a different account' : 'Connect Google account';
    $('#cl-connect').disabled = !account.configured;

    const idField = $('#cl-client-id');
    if (account.client_id_hint && !idField.value) {
      idField.placeholder = account.client_id_hint;
    }
  }

  renderEncryption(data) {
    const enc = data.encryption;
    if (!enc) return;
    $('#cl-enc-setup').hidden = enc.key_exists;
    $('#cl-enc-ready').hidden = !enc.key_exists;
    $('#cl-encrypt').checked = Boolean(enc.enabled);
    $('#cl-enc-state').textContent = !enc.key_exists
      ? (enc.enabled ? 'no key yet — nothing is uploaded until there is one' : 'no key')
      : enc.enabled ? 'on' : 'key made · off';
    if (enc.key_exists) {
      const made = enc.created_at ? new Date(enc.created_at * 1000).toLocaleDateString() : '';
      $('#cl-enc-summary').textContent =
        `Key ${enc.key_id}${made ? `, made ${made}` : ''}. `
        + `${enc.encrypted_uploads.toLocaleString()} files uploaded encrypted, `
        + `${enc.unencrypted_uploads.toLocaleString()} uploaded before encryption.`
        + (enc.params_in_drive ? ' The key settings file is in the Drive folder.' : '');
    }
  }

  renderSettings(data) {
    this.renderEncryption(data);
    $('#cl-enabled').checked = Boolean(data.enabled);
    $('#cl-autostart').checked = Boolean(data.autostart);
    const folder = $('#cl-folder');
    if (document.activeElement !== folder) folder.value = data.folder_name || 'Ninaivu';

    // Not while somebody is mid-edit: a two-second poll that overwrites the
    // box being typed into is the most irritating bug a settings panel has.
    const set = (selector, value) => {
      const field = $(selector);
      if (field && document.activeElement !== field) field.value = value;
    };
    set('#cl-rate', data.rate_kbps || 0);
    set('#cl-parallel', String(data.parallel || 1));
    const fullSpeedBox = $('#cl-full-speed');
    if (fullSpeedBox) fullSpeedBox.checked = Boolean(data.full_speed);
    set('#cl-hidden', data.hidden || 'never');
    const hiddenState = $('#cl-hidden-state');
    if (hiddenState) {
      hiddenState.textContent = data.hidden_now ? 'being backed up'
        : data.hidden === 'encrypted' ? 'waiting for encryption to be on'
          : 'not backed up';
    }
    set('#cl-window-start', data.window_start || '');
    set('#cl-window-end', data.window_end || '');

    const limits = $('#cl-limits');
    if (limits) {
      limits.textContent = [
        data.full_speed ? 'full speed' : null,
        data.parallel > 1 ? `${data.parallel} at once` : 'one at a time',
        data.rate_kbps ? `${data.rate_kbps} KB/s` : 'no limit',
        data.window_label || 'any time',
      ].filter(Boolean).join(' · ');
    }
  }

  renderRun(data) {
    const state = data.state || {};
    const queue = data.queue || {};
    const status = $('#cl-status');
    const text = $('#cl-status-text');

    // Holding for the window is alive but not moving, and the pulse means
    // "bytes are going". Claiming otherwise is how somebody ends up
    // restarting a server that was doing exactly what it was told.
    status.classList.toggle('busy',
      Boolean(data.running) && !state.waiting_for_window && !state.held_for);
    status.classList.toggle('bad', Boolean(state.needs_reconnect));
    // Library folders the run could not reach. Their files stay pending, so
    // the "done" tick below is already withheld; what was missing was a line
    // saying why an upload with work left in it had stopped.
    const away = state.offline_roots || [];
    status.classList.toggle('done',
      !data.running && !state.needs_reconnect && queue.total > 0
      && (queue.pending || 0) + (queue.uploading || 0) === 0);

    if (state.needs_reconnect) {
      text.textContent = 'Stopped — the Google permission needs renewing';
    } else if (!data.running && away.length) {
      // Not an error and not a disconnection: the drive is unplugged. Said
      // here because the run has ended and the queue still has work in it,
      // which without this reads as an upload that stopped for no reason.
      text.textContent = away.length === 1
        ? `Waiting for ${away[0]} — it is not connected`
        : `Waiting for ${away.length} library folders that are not connected`;
    } else if (state.queueing) {
      // Going through the index to see what still has to go up. On a large
      // library this takes a moment, and saying nothing looks exactly like an
      // upload that never started.
      const so_far = Number(state.queued_so_far || 0);
      text.textContent = so_far
        ? `Going through the library — ${so_far.toLocaleString()} queued so far`
        : 'Going through the library…';
    } else if (data.running && state.waiting_for_window) {
      const at = clock(state.window_opens_at);
      text.textContent = at
        ? `Waiting until ${at} — uploads run ${data.window_label}`
        : `Waiting for the upload hours — ${data.window_label}`;
    } else if (data.running && state.held_for) {
      // Making way for the household (the workload mode, or somebody using
      // Ninaivu from outside the house). Without this it read as running, with
      // nothing moving and no reason given.
      text.textContent = `Waiting — ${state.held_for}`;
    } else if (data.running && state.current) {
      text.textContent = `Uploading ${state.current}`;
    } else if (data.running) {
      text.textContent = 'Working…';
    } else if (!data.enabled) {
      text.textContent = 'Cloud backup is off';
    } else if ((queue.pending || 0) + (queue.uploading || 0) > 0) {
      // "Paused" is only true of something that was going. A queue that has
      // never been started is ready, and saying "paused" would have somebody
      // hunting for what stopped it.
      const waiting = queue.pending + queue.uploading;
      const hours = data.window_open ? '' : ` · outside ${data.window_label}`;
      text.textContent = state.started_at
        ? `Paused — ${waiting} waiting${hours}`
        : `Ready — ${waiting} waiting to go up${hours}`;
    } else if (queue.done) {
      text.textContent = `Up to date — ${queue.done} uploaded`;
    } else {
      text.textContent = 'Nothing queued yet';
    }

    const percent = $('#cl-percent');
    percent.hidden = !queue.total;
    percent.textContent = `${queue.percent || 0}% of the library`;

    const progress = $('#cl-progress');
    progress.hidden = !(data.running && state.current_total);
    if (!progress.hidden) {
      $('#cl-fill').style.width = `${state.percent_of_file || 0}%`;
      $('#cl-current').textContent = state.current || '';
      $('#cl-current-size').textContent =
        `${bytes(state.current_sent)} of ${bytes(state.current_total)}`;
      $('#cl-run').textContent = [
        `${state.uploaded_this_run || 0} sent this run`,
        bytes(state.bytes_this_run),
        data.rate_kbps ? `capped at ${data.rate_kbps} KB/s` : '',
      ].filter(Boolean).join(' · ');
    }

    $('#cl-reconnect').hidden = !state.needs_reconnect;
    if (state.needs_reconnect && state.last_error) {
      $('#cl-reconnect-text').textContent = state.last_error;
    }

    $('#cl-done').textContent = queue.done || 0;
    $('#cl-done-sub').textContent = queue.bytes_sent
      ? `${bytes(queue.bytes_sent)} in Drive` : 'never sent twice';
    $('#cl-pending').textContent = (queue.pending || 0) + (queue.uploading || 0);
    // The queue sends photographs before video. That is worth saying here
    // rather than leaving somebody to wonder why a folder of films is not
    // moving — and it is the one place the claim can be checked against the
    // numbers it is made about.
    const owed = [];
    if (queue.bytes_waiting) owed.push(`${bytes(queue.bytes_waiting)} to go`);
    if (queue.waiting_pictures && queue.waiting_videos) {
      owed.push(`${queue.waiting_pictures.toLocaleString()} photographs first, `
        + `then ${queue.waiting_videos.toLocaleString()} videos`);
    }
    $('#cl-pending-sub').textContent = owed.join(' · ') || 'still to go';
    $('#cl-skipped').textContent = queue.skipped || 0;
    $('#cl-failed').textContent = queue.failed || 0;
    $('#cl-failed-card').classList.toggle('bad', Boolean(queue.failed));

    $('#cl-start').disabled = Boolean(data.running) || !data.enabled
      || !data.account?.connected;
    $('#cl-pause').disabled = !data.running;
    $('#cl-retry').disabled = !queue.failed;
  }

  renderTable() {
    const data = this.status;
    const body = $('#cl-tbody');
    if (!body) return;
    const rows = (this.view === 'failed' ? data?.failures
      : this.view === 'skipped' ? data?.skipped
        : data?.recent) || [];

    body.innerHTML = '';
    if (!rows.length) {
      const empty = el('tr');
      const cell = el('td', 'ar-empty', 'Nothing yet');
      cell.colSpan = 3;
      empty.appendChild(cell);
      body.appendChild(empty);
      return;
    }

    rows.forEach((row) => {
      const tr = el('tr');
      const name = el('td');
      name.appendChild(el('span', '', row.filename || row.rel_path));
      name.title = row.rel_path || '';
      tr.appendChild(name);
      tr.appendChild(el('td', `cl-state cl-${row.state}`,
        STATE_WORDS[row.state] || row.state));
      const detail = row.state === 'done'
        ? `${bytes(row.size)}${row.remote_folder ? ` · ${row.remote_folder}` : ''}`
        : (row.error || '');
      tr.appendChild(el('td', 'cl-detail', detail));
      body.appendChild(tr);
    });
  }
}
