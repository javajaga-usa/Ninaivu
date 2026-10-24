/**
 * Mugil → Two computers: which part this computer plays, and pairing it.
 *
 * A single computer sees one choice and nothing else. The Secondary chooses
 * the folder on its big disk and shows a pairing code; the Primary is given
 * the Secondary's address and that code, then sends its originals and shows
 * how far it has got. The server does the checking (api/mugil_api.py): this
 * only says what it answers.
 *
 * Owned by the Mugil panel, shown and hidden with it; it polls only while a
 * send is running and the page is on screen.
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
    throw Object.assign(new Error(i18n.fromServer(data, response.statusText)),
      { status: response.status, data });
  }
  return data;
}

const size = (n) => {
  const value = Number(n) || 0;
  if (value < 1024) return `${value} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let amount = value / 1024;
  let unit = 0;
  while (amount >= 1024 && unit < units.length - 1) { amount /= 1024; unit += 1; }
  return `${amount < 10 ? amount.toFixed(1) : Math.round(amount)} ${units[unit]}`;
};

const count = (n) => (Number(n) || 0).toLocaleString(i18n.locale());

const when = (seconds) => (seconds
  ? new Date(seconds * 1000).toLocaleString(i18n.locale(),
    { dateStyle: 'medium', timeStyle: 'short' })
  : '');

const ROLE_STATE = {
  single: () => i18n.t('One computer'),
  primary: () => i18n.t('Primary'),
  secondary: () => i18n.t('Secondary'),
};

export class TwoComputers {
  constructor({ toast }) {
    this.toast = toast || (() => {});
    this.timer = null;
    this.visible = false;
    this.status = null;
    i18n.onChange(() => { if (this.status) this.render(this.status); });
  }

  wire() {
    if (!$('#tc-block')) return;
    $('#tc-save-role').onclick = () => this.act(
      () => json('/api/admin/mugil/role', { method: 'POST', body: { role: $('#tc-role').value } }),
      i18n.t('Saved'));
    $('#tc-save-folder').onclick = () => this.act(
      () => json('/api/admin/mugil/role', {
        method: 'POST', body: { role: 'secondary', originals_dir: $('#tc-folder').value.trim() },
      }),
      i18n.t('The originals will be kept in that folder'));
    $('#tc-code').onclick = () => this.showCode();
    $('#tc-pair').onclick = () => this.act(
      () => json('/api/admin/mugil/pair', {
        method: 'POST',
        body: { url: $('#tc-url').value.trim(), code: $('#tc-pair-code').value.trim() },
      }),
      i18n.t('The two computers are paired'));
    $('#tc-every').onchange = (event) => this.act(
      () => json('/api/admin/mugil/role', {
        method: 'POST', body: { role: 'primary', copy_every_hours: Number(event.target.value) },
      }),
      i18n.t('Saved'));
    $('#tc-send').onclick = () => this.act(
      () => json('/api/admin/mugil/send', { method: 'POST' }),
      i18n.t('Sending the originals to the Secondary'));
    $('#tc-stop').onclick = () => this.act(() => json('/api/admin/mugil/stop', { method: 'POST' }));
    $('#tc-unpair').onclick = () => {
      // Pairing again needs a fresh code from the other computer.
      if (!window.confirm(i18n.t('Unpair the two computers? To pair them again you will need a new code from the Secondary.'))) return;
      this.act(() => json('/api/admin/mugil/unpair', { method: 'POST' }),
        i18n.t('Unpaired. Unpair the other computer too.'));
    };
  }

  show() { this.visible = true; this.refresh(); }

  hide() { this.visible = false; clearTimeout(this.timer); this.timer = null; }

  async act(call, done) {
    const buttons = [...document.querySelectorAll('#tc-block button')];
    buttons.forEach((b) => { b.disabled = true; });
    try {
      this.render(await call());
      if (done) this.toast(done);
    } catch (exc) {
      this.toast(exc.message, true);
    } finally {
      buttons.forEach((b) => { b.disabled = false; });
    }
    this.schedule();
  }

  async showCode() {
    try {
      const answer = await json('/api/admin/mugil/code', { method: 'POST' });
      const out = $('#tc-code-out');
      out.textContent = answer.code;
      const note = document.createElement('small');
      note.textContent = i18n.t('Type this on the Primary within {minutes} minutes. It works once.',
        { minutes: Math.round((answer.expires_in || 600) / 60) });
      out.append(note);
      out.hidden = false;
      clearTimeout(this.codeTimer);
      this.codeTimer = setTimeout(() => { out.hidden = true; this.refresh(); },
        (answer.expires_in || 600) * 1000);
    } catch (exc) {
      this.toast(exc.message, true);
    }
  }

  async refresh() {
    clearTimeout(this.timer);
    try {
      this.render(await json('/api/admin/mugil'));
    } catch (exc) {
      if (exc.status === 404) { $('#tc-block').hidden = true; return; }
      if (exc.status !== 401 && exc.status !== 423) this.toast(exc.message, true);
    }
    this.schedule();
  }

  schedule() {
    clearTimeout(this.timer);
    if (!this.visible) return;
    const running = Boolean(this.status?.sending?.running);
    this.timer = setTimeout(() => this.refresh(), running ? 3000 : 60000);
  }

  render(status) {
    if (!status) return;
    this.status = status;
    const part = status.role || 'single';
    $('#tc-block').hidden = false;
    $('#tc-role').value = part;
    $('#tc-state').textContent = (ROLE_STATE[part] || ROLE_STATE.single)();
    $('#tc-secondary').hidden = part !== 'secondary';
    $('#tc-primary').hidden = part !== 'primary';

    const pairing = status.pairing || {};
    $('#tc-paired').hidden = !status.paired;
    $('#tc-paired-with').textContent = status.paired
      ? i18n.t('Paired with {name} since {when}.', {
        name: pairing.partner_name || pairing.partner_url || pairing.partner_address || '—',
        when: when(pairing.paired_at),
      })
      : '';
    // A computer's part is changed only while it is not paired.
    $('#tc-role').disabled = Boolean(status.paired);
    $('#tc-save-role').disabled = Boolean(status.paired);

    if (part === 'secondary') this.renderSecondary(status);
    if (part === 'primary') this.renderPrimary(status);
  }

  renderSecondary(status) {
    const folder = $('#tc-folder');
    if (document.activeElement !== folder) folder.value = status.originals_dir || '';
    const keeping = status.keeping || {};
    $('#tc-keeping').textContent = !status.originals_dir
      ? i18n.t('Choose the folder on the big disk the originals are kept in.')
      : i18n.t('{files} originals kept, {size}; {free} free on that disk.', {
        files: count(keeping.files), size: size(keeping.bytes),
        free: keeping.free_bytes == null ? '—' : size(keeping.free_bytes),
      });
    $('#tc-code-row').hidden = !status.originals_dir;
  }

  renderPrimary(status) {
    $('#tc-pair-row').hidden = Boolean(status.paired);
    $('#tc-send-row').hidden = !status.paired;
    const url = $('#tc-url');
    if (document.activeElement !== url && status.partner_url) url.value = status.partner_url;
    const every = String(Math.round(status.copy_every_hours || 0));
    const select = $('#tc-every');
    if ([...select.options].some((o) => o.value === every)) select.value = every;

    const sending = status.sending || {};
    const running = Boolean(sending.running);
    $('#tc-send').hidden = running;
    $('#tc-stop').hidden = !running;

    const lines = [];
    const progress = sending.progress || {};
    if (running) {
      lines.push(i18n.t('Sending: {done} of {files} files looked at, {sent} sent ({size}), {already} already there.', {
        done: count(progress.done), files: count(progress.files), sent: count(progress.sent),
        size: size(progress.bytes), already: count(progress.already),
      }));
      if (progress.waiting) lines.push(i18n.t('Waiting: {reason}', { reason: progress.waiting }));
    } else if (sending.last && sending.last.finished) {
      const last = sending.last;
      lines.push(i18n.t('Last run {when}: {sent} sent ({size}), {already} already there.', {
        when: when(last.finished), sent: count(last.sent), size: size(last.bytes),
        already: count(last.already),
      }));
      if (last.changed) lines.push(i18n.t('{count} changed while being sent; they go next time.', { count: count(last.changed) }));
      if (last.failed) lines.push(i18n.t('{count} could not be sent; the log says why.', { count: count(last.failed) }));
      if (last.error) lines.push(i18n.t('It stopped: {reason}', { reason: last.error }));
    }
    const confirmed = Object.values(status.confirmed || {});
    if (confirmed.length) {
      const files = confirmed.reduce((sum, c) => sum + (c.files || 0), 0);
      const bytes = confirmed.reduce((sum, c) => sum + (c.bytes || 0), 0);
      lines.push(i18n.t('The Secondary has confirmed {files} originals, {size}.', {
        files: count(files), size: size(bytes),
      }));
    }
    $('#tc-progress').textContent = lines.join(' ');
  }
}
