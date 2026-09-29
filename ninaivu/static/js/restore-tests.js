/**
 * Test restores — the Cloud page's proof that the backup restores.
 *
 * Shows the last test and a short history, lets the administrator run one now
 * and choose how often they run by themselves. The work is in
 * ninaivu/cloud/restore_test.py; this only asks and tells.
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

const when = (stamp) => new Date(stamp * 1000).toLocaleString(i18n.locale(),
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

/** A test's outcome, as the history table names it. */
const OUTCOMES = {
  passed: i18n.key('passed'),
  failed: i18n.key('failed'),
  skipped: i18n.key('skipped'),
};

/** The copy of the index kept in Drive (ninaivu/cloud/index_copy.py). */
export class IndexCopyStatus {
  constructor({ toast }) {
    this.toast = toast || (() => {});
    this.timer = null;
    this.visible = false;
    this.status = null;
    i18n.onChange(() => { if (this.status) this.render(this.status); });
  }

  wire() {
    if (!$('#ic-block')) return;
    $('#ic-run').onclick = async () => {
      $('#ic-run').disabled = true;
      try {
        this.render(await json('/api/cloud/index-copy/run', { method: 'POST' }));
        this.toast(i18n.t('Sending a copy of the index to Drive'));
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
    this.status = status;
    $('#ic-block').hidden = false;
    $('#ic-run').disabled = Boolean(status.running);
    const last = status.last;
    $('#ic-state').textContent = status.running ? i18n.t('sending…')
      : last ? i18n.t('sent {when}', { when: when(last.at) }) : i18n.t('not sent yet');
    const parts = [];
    if (last) {
      // The folder's name is its name in Drive, in every language.
      const sent = { size: size(last.size), folder: 'Ninaivu index', name: last.name };
      parts.push(last.encrypted
        ? i18n.t('{size}, encrypted, in Drive’s “{folder}” folder as {name}.', sent)
        : i18n.t('{size}, in Drive’s “{folder}” folder as {name}.', sent));
    }
    if (status.error) parts.push(i18n.t('The last attempt failed: {reason}', { reason: status.error }));
    if (!status.every_hours) parts.push(i18n.t('Sending by itself is switched off.'));
    $('#ic-detail').textContent = parts.join(' ');
  }
}

export class RestoreTests {
  constructor({ toast }) {
    this.toast = toast || (() => {});
    this.timer = null;
    this.visible = false;
    this.status = null;
    i18n.onChange(() => { if (this.status) this.render(this.status); });
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
      this.toast(i18n.t('Testing a restore from Google Drive'));
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
      this.toast(!days ? i18n.t('Test restores off')
        : days === 7 ? i18n.t('Test restores every week')
          : i18n.t('Test restores every {count} days', { count: days }));
    } catch (exc) {
      this.toast(exc.message, true);
    }
  }

  render(status) {
    if (!status) return;
    this.status = status;
    $('#rt-block').hidden = false;
    const select = $('#rt-every');
    const known = [...select.options].find((o) => Number(o.value) === status.every_days);
    const every = i18n.t('Every {count} days', { count: status.every_days });
    // One this page added itself is relabelled; the template's own are not ours.
    if (known?.dataset.added) known.textContent = every;
    else if (!known) {
      const option = new Option(every, String(status.every_days));
      option.dataset.added = '1';
      select.append(option);
    }
    select.value = String(status.every_days);
    $('#rt-run').disabled = Boolean(status.running);

    const last = status.last;
    const state = $('#rt-state');
    if (status.running) {
      const p = status.progress;
      state.textContent = p?.total
        ? i18n.t('testing · {done} of {total}', { done: p.processed, total: p.total }) : i18n.t('testing…');
    } else if (last) {
      const at = { when: when(last.started_at) };
      state.textContent = last.status === 'passed' ? i18n.t('passed {when}', at)
        : last.status === 'failed' ? i18n.t('failed {when}', at) : i18n.t('skipped {when}', at);
    } else {
      state.textContent = i18n.t('not tested yet');
    }

    const note = $('#rt-last');
    note.hidden = !last;
    if (last) {
      note.className = `ar-note ${last.status === 'failed' ? 'ar-note-bad'
        : last.status === 'passed' ? 'ar-note-info' : 'ar-note-fix'}`;
      note.replaceChildren(el('strong', '', last.status === 'passed'
        ? i18n.t('The backup restored') : last.status === 'failed'
          ? i18n.t('The last test restore failed') : i18n.t('The last test did not run')),
      el('p', '', last.summary));
      const problems = (last.detail || []).slice(0, 5);
      if (problems.length) {
        const list = el('ul');
        for (const problem of problems) list.append(el('li', '', `${problem.file}: ${problem.why}`));
        note.append(list);
      }
      if (status.next_due && status.every_days) {
        note.append(el('p', '', i18n.t('Next test {when}.', { when: when(status.next_due) })));
      }
    }

    const history = status.history || [];
    $('#rt-history-wrap').hidden = history.length < 2;
    $('#rt-history').replaceChildren(...history.map((test) => {
      const row = el('tr');
      const result = el('td');
      result.append(el('span', `cl-state cl-${test.status === 'passed' ? 'done'
        : test.status === 'failed' ? 'failed' : 'skipped'}`,
      OUTCOMES[test.status] ? i18n.t(OUTCOMES[test.status]) : test.status),
      document.createTextNode(' '), el('span', 'cl-detail', test.summary));
      row.append(el('td', '', when(test.started_at)), result);
      return row;
    }));
  }
}
