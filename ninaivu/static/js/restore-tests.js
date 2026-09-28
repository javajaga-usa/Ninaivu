/**
 * Test restores — the Cloud page's proof that the backup restores.
 *
 * Shows the last test and a short history, lets the administrator run one now
 * and choose how often they run by themselves. The work is in
 * ninaivu/cloud/restore_test.py; this only asks and tells.
 */

import { reportUnauthorized } from './api.js';

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

const when = (stamp) => new Date(stamp * 1000).toLocaleString(undefined,
  { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });

const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
};

const size = (n) => {
  let value = Number(n) || 0;
  for (const unit of ['B', 'KB', 'MB', 'GB']) {
    if (value < 1024 || unit === 'GB') return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${unit}`;
    value /= 1024;
  }
  return '';
};

/** The copy of the index kept in Drive (ninaivu/cloud/index_copy.py). */
export class IndexCopyStatus {
  constructor({ toast }) {
    this.toast = toast || (() => {});
    this.timer = null;
    this.visible = false;
  }

  wire() {
    if (!$('#ic-block')) return;
    $('#ic-run').onclick = async () => {
      $('#ic-run').disabled = true;
      try {
        this.render(await json('/api/cloud/index-copy/run', { method: 'POST' }));
        this.toast('Sending a copy of the index to Drive');
      } catch (exc) {
        this.toast(exc.message, true);
      }
      this.refresh();
    };
  }

  show() { this.visible = true; this.refresh(); }

  hide() { this.visible = false; clearTimeout(this.timer); this.timer = null; }

  async refresh() {
    clearTimeout(this.timer);
    let status = null;
    try {
      status = await json('/api/cloud/index-copy');
      this.render(status);
    } catch (exc) {
      if (exc.status === 404) { $('#ic-block').hidden = true; return; }
      if (exc.status !== 401 && exc.status !== 423) this.toast(exc.message, true);
    }
    if (this.visible) this.timer = setTimeout(() => this.refresh(), status?.running ? 3000 : 60000);
  }

  render(status) {
    if (!status) return;
    $('#ic-block').hidden = false;
    $('#ic-run').disabled = Boolean(status.running);
    const last = status.last;
    $('#ic-state').textContent = status.running ? 'sending…'
      : last ? `sent ${when(last.at)}` : 'not sent yet';
    const parts = [];
    if (last) {
      parts.push(`${size(last.size)}${last.encrypted ? ', encrypted' : ''}, in Drive's `
        + `“Ninaivu index” folder as ${last.name}.`);
    }
    if (status.error) parts.push(`The last attempt failed: ${status.error}`);
    if (!status.every_hours) parts.push('Sending by itself is switched off.');
    $('#ic-detail').textContent = parts.join(' ');
  }
}

export class RestoreTests {
  constructor({ toast }) {
    this.toast = toast || (() => {});
    this.timer = null;
    this.visible = false;
  }

  wire() {
    if (!$('#rt-block')) return;
    $('#rt-run').onclick = () => this.runNow();
    $('#rt-every').onchange = (event) => this.save(Number(event.target.value));
  }

  show() {
    this.visible = true;
    this.refresh();
  }

  hide() {
    this.visible = false;
    clearTimeout(this.timer);
    this.timer = null;
  }

  async refresh() {
    clearTimeout(this.timer);
    let status = null;
    try {
      status = await json('/api/cloud/restore-tests');
      this.render(status);
    } catch (exc) {
      // A server from before test restores has no such endpoint.
      if (exc.status === 404) { $('#rt-block').hidden = true; return; }
      if (exc.status !== 401 && exc.status !== 423) this.toast(exc.message, true);
    }
    if (this.visible) this.timer = setTimeout(() => this.refresh(), status?.running ? 3000 : 60000);
  }

  async runNow() {
    const button = $('#rt-run');
    button.disabled = true;
    try {
      this.render(await json('/api/cloud/restore-tests/run', { method: 'POST' }));
      this.toast('Testing a restore from Google Drive');
    } catch (exc) {
      this.toast(exc.message, true);
    } finally {
      button.disabled = false;
    }
    this.refresh();
  }

  async save(days) {
    try {
      this.render(await json('/api/cloud/restore-tests/settings',
        { method: 'POST', body: { every_days: days } }));
      this.toast(days ? `Test restores every ${days === 7 ? 'week' : `${days} days`}`
        : 'Test restores off');
    } catch (exc) {
      this.toast(exc.message, true);
    }
  }

  render(status) {
    if (!status) return;
    $('#rt-block').hidden = false;
    const select = $('#rt-every');
    const known = [...select.options].some((o) => Number(o.value) === status.every_days);
    if (!known) select.append(new Option(`Every ${status.every_days} days`, String(status.every_days)));
    select.value = String(status.every_days);
    $('#rt-run').disabled = Boolean(status.running);

    const last = status.last;
    const state = $('#rt-state');
    if (status.running) {
      const p = status.progress;
      state.textContent = p?.total ? `testing · ${p.processed} of ${p.total}` : 'testing…';
    } else if (last) {
      state.textContent = last.status === 'passed' ? `passed ${when(last.started_at)}`
        : last.status === 'failed' ? `failed ${when(last.started_at)}` : `skipped ${when(last.started_at)}`;
    } else {
      state.textContent = 'not tested yet';
    }

    const note = $('#rt-last');
    note.hidden = !last;
    if (last) {
      note.className = `ar-note ${last.status === 'failed' ? 'ar-note-bad'
        : last.status === 'passed' ? 'ar-note-info' : 'ar-note-fix'}`;
      note.replaceChildren(el('strong', '', last.status === 'passed'
        ? 'The backup restored' : last.status === 'failed'
          ? 'The last test restore failed' : 'The last test did not run'),
      el('p', '', last.summary));
      const problems = (last.detail || []).slice(0, 5);
      if (problems.length) {
        const list = el('ul');
        for (const problem of problems) list.append(el('li', '', `${problem.file}: ${problem.why}`));
        note.append(list);
      }
      if (status.next_due && status.every_days) {
        note.append(el('p', '', `Next test ${when(status.next_due)}.`));
      }
    }

    const history = status.history || [];
    $('#rt-history-wrap').hidden = history.length < 2;
    $('#rt-history').replaceChildren(...history.map((test) => {
      const row = el('tr');
      const result = el('td');
      result.append(el('span', `cl-state cl-${test.status === 'passed' ? 'done'
        : test.status === 'failed' ? 'failed' : 'skipped'}`, test.status),
      document.createTextNode(' '), el('span', 'cl-detail', test.summary));
      row.append(el('td', '', when(test.started_at)), result);
      return row;
    }));
  }
}
