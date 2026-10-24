/**
 * Mugil → Copy out of the house (cloud/offsite.py).
 *
 * Where the encrypted off-site copy goes (Backblaze B2 or another S3
 * service, or a folder), whether it is kept and how often, Test, Send now
 * and Stop, and what it has done. On a two-computer Secondary the server
 * sends it from the originals it keeps, and the page says so. Without the
 * household's backup key it offers to bring the key from its recovery file,
 * which is how a second computer gets the same key. The secret is sent only
 * when typed and never shown again.
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

const FIELDS = ['endpoint', 'region', 'bucket', 'prefix', 'access_key', 'folder'];
const INPUT = { endpoint: '#os-endpoint', region: '#os-region', bucket: '#os-bucket',
  prefix: '#os-prefix', access_key: '#os-access', folder: '#os-folder' };

export class Offsite {
  constructor({ toast }) {
    this.toast = toast || (() => {});
    this.timer = null;
    this.visible = false;
    this.status = null;
    this.filled = false;
    i18n.onChange(() => { if (this.status) this.render(this.status); });
  }

  wire() {
    if (!$('#os-block')) return;
    $('#os-kind').onchange = () => this.showKind();
    $('#os-save').onclick = () => this.save();
    $('#os-test').onclick = () => this.act(() => json('/api/offsite/test', { method: 'POST' }),
      i18n.t('The destination answered'));
    $('#os-start').onclick = () => this.act(() => json('/api/offsite/start',
      { method: 'POST', body: {} }), i18n.t('Sending the off-site copy'));
    $('#os-stop').onclick = () => this.act(() => json('/api/offsite/stop', { method: 'POST' }));
    $('#os-recovery').onchange = (event) => this.bringKey(event.target);
  }

  show() { this.visible = true; this.refresh(); }

  hide() { this.visible = false; clearTimeout(this.timer); this.timer = null; }

  async refresh() {
    clearTimeout(this.timer);
    try {
      this.render(await json('/api/offsite'));
    } catch (exc) {
      if (exc.status === 404 || exc.status === 403) { $('#os-block').hidden = true; return; }
      if (exc.status !== 401 && exc.status !== 423) this.toast(exc.message, true);
    }
    this.schedule();
  }

  schedule() {
    clearTimeout(this.timer);
    if (!this.visible || !this.status) return;
    this.timer = setTimeout(() => this.refresh(), this.status.running ? 4000 : 60000);
  }

  async act(call, done) {
    const buttons = [...document.querySelectorAll('#os-block button')];
    buttons.forEach((b) => { b.disabled = true; });
    try {
      await call();
      if (done) this.toast(done);
    } catch (exc) {
      this.toast(exc.message, true);
    } finally {
      buttons.forEach((b) => { b.disabled = false; });
    }
    this.refresh();
  }

  showKind() {
    const s3 = $('#os-kind').value === 's3';
    $('#os-s3').hidden = !s3;
    $('#os-folder-row').hidden = s3;
  }

  async save() {
    const body = { kind: $('#os-kind').value, enabled: $('#os-enabled').checked,
      every_hours: Number($('#os-every').value) || 24 };
    for (const name of FIELDS) body[name] = $(INPUT[name]).value;
    const secret = $('#os-secret').value;
    if (secret.trim()) body.secret_key = secret;
    await this.act(async () => {
      await json('/api/offsite', { method: 'POST', body });
      $('#os-secret').value = '';
    }, i18n.t('Saved'));
  }

  async bringKey(input) {
    const file = input.files && input.files[0];
    if (!file) return;
    let recovery;
    try {
      recovery = JSON.parse(await file.text());
    } catch {
      this.toast(i18n.t('That is not a Ninaivu recovery file.'), true);
      input.value = '';
      return;
    }
    await this.act(() => json('/api/cloud/encryption/import',
      { method: 'POST', body: { recovery } }), i18n.t('The backup key is here now'));
    input.value = '';
  }

  render(status) {
    this.status = status;
    $('#os-block').hidden = false;
    $('#os-state').textContent = status.running ? i18n.t('Sending')
      : status.enabled ? i18n.t('On') : i18n.t('Off');
    $('#os-source').hidden = !status.from_the_originals;
    const noKey = /backup key/.test(String(status.problem || ''));
    $('#os-key-row').hidden = !noKey;
    if (!this.filled) {
      // Filled once: a refresh must not write over what is being typed.
      const form = status.form || {};
      $('#os-kind').value = form.kind === 'folder' ? 'folder' : 's3';
      for (const name of FIELDS) $(INPUT[name]).value = form[name] || '';
      $('#os-every').value = Number(form.every_hours) >= 168 ? '168' : '24';
      $('#os-enabled').checked = Boolean(status.enabled);
      $('#os-secret').placeholder = status.secret_saved ? i18n.t('Saved; type to change it')
        : i18n.t('Application key');
      this.filled = true;
      this.showKind();
    }
    $('#os-run-row').hidden = !status.enabled;
    $('#os-start').hidden = Boolean(status.running);
    $('#os-stop').hidden = !status.running;
    const problem = status.error || status.problem;
    $('#os-problem').hidden = !problem;
    $('#os-problem').textContent = problem ? i18n.fromServer({ error: problem }, problem) : '';
    const says = [];
    if (status.files) {
      says.push(i18n.t('{kept} of {files} files are in the copy ({bytes}).', {
        kept: count(status.kept), files: count(status.files), bytes: size(status.kept_bytes) }));
    }
    if (status.running && status.current) says.push(i18n.t('Now: {file}', { file: status.current }));
    if (status.last_run) says.push(i18n.t('Last finished {when}.', { when: when(status.last_run) }));
    if (status.failed) says.push(i18n.t('{n} could not be sent.', { n: count(status.failed) }));
    if (status.message && !status.running) says.push(i18n.fromServer({ error: status.message }, status.message));
    $('#os-progress').textContent = says.join(' ');
  }
}
