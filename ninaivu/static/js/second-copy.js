/**
 * Mugil → Second copy on another disk (storage/mirror.py).
 *
 * The folder, whether the copy is kept and how often, and the copy itself:
 * Copy now, Check the copy, Stop, and what it has done. Before a folder is
 * used, "Will it fit?" asks what that kind of disk could not keep as it is
 * (storage/fit.py): names it refuses, two names it would take for one,
 * files too large for it, each with a name that would work. Choosing a new
 * folder asks the same by itself. The server does the checking; this only
 * says what it answers.
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

const PROBLEM = {
  too_large: () => i18n.t('too large for this disk'),
  forbidden_character: () => i18n.t('a character this disk does not take'),
  trailing_dot_or_space: () => i18n.t('ends in a dot or a space'),
  reserved_name: () => i18n.t('a name this disk keeps for itself'),
  name_too_long: () => i18n.t('a name too long for this disk'),
  same_name_different_case: () => i18n.t('this disk sees it as the same name as another file'),
  same_name_different_spelling: () => i18n.t('this disk sees it as the same name as another file'),
};

/** How many problems are listed on the page; the counts are always whole. */
const LISTED = 20;

export class SecondCopy {
  constructor({ toast }) {
    this.toast = toast || (() => {});
    this.timer = null;
    this.visible = false;
    this.status = null;
    this.fit = null;
    i18n.onChange(() => {
      if (this.status) this.render(this.status);
      if (this.fit) this.renderFit(this.fit);
    });
  }

  wire() {
    if (!$('#sc-block')) return;
    $('#sc-check').onclick = () => this.check();
    $('#sc-save').onclick = () => this.save();
    $('#sc-start').onclick = () => this.act(() => json('/api/mirror/start', { method: 'POST' }),
      i18n.t('Copying to the other disk'));
    $('#sc-verify').onclick = () => this.act(() => json('/api/mirror/verify', { method: 'POST' }),
      i18n.t('Checking every file on the copy'));
    $('#sc-stop').onclick = () => this.act(() => json('/api/mirror/stop', { method: 'POST' }));
  }

  show() { this.visible = true; this.refresh(); }

  hide() { this.visible = false; clearTimeout(this.timer); this.timer = null; }

  async refresh() {
    clearTimeout(this.timer);
    try {
      this.render(await json('/api/mirror/status'));
    } catch (exc) {
      if (exc.status === 404 || exc.status === 403) { $('#sc-block').hidden = true; return; }
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
    const buttons = [...document.querySelectorAll('#sc-block button')];
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

  async check() {
    const folder = $('#sc-folder').value.trim();
    if (!folder) {
      this.toast(i18n.t('Choose a folder to put the files in.'), true);
      return;
    }
    try {
      this.renderFit(await json(`/api/admin/fit?limit=${LISTED}&target=${encodeURIComponent(folder)}`));
    } catch (exc) {
      this.toast(exc.message, true);
    }
  }

  async save() {
    const folder = $('#sc-folder').value.trim();
    const moved = folder !== (this.status?.folder || '');
    await this.act(() => json('/api/mirror/settings', {
      method: 'POST',
      body: { folder, enabled: $('#sc-enabled').checked, every_hours: Number($('#sc-every').value) },
    }), i18n.t('Saved'));
    // A new folder: say at once what that disk could not keep.
    if (moved && folder) await this.check();
  }

  renderFit(fit) {
    this.fit = fit;
    $('#sc-fit').hidden = false;
    const kind = fit.filesystem ? fit.filesystem.toUpperCase() : i18n.t('an unknown kind');
    const problems = Object.values(fit.problems || {}).reduce((a, b) => a + b, 0);
    $('#sc-fit-says').textContent = fit.fits
      ? i18n.t('That disk ({kind}) can keep all {files} files as they are.', {
        kind, files: count(fit.files) })
      : i18n.t('That disk ({kind}) could not keep {count} of the {files} files as they are. The first ones:', {
        kind, count: count(fit.problem_files || problems), files: count(fit.files) });
    const list = $('#sc-problems');
    list.textContent = '';
    for (const item of (fit.listed || []).slice(0, LISTED)) {
      const line = document.createElement('li');
      const why = (PROBLEM[item.problem] || (() => item.problem))();
      line.textContent = item.suggestion
        ? i18n.t('{path}: {why}. It could be called {suggestion}.', { path: item.path, why, suggestion: item.suggestion })
        : i18n.t('{path}: {why}.', { path: item.path, why });
      list.append(line);
    }
    if (!fit.fits) {
      const line = document.createElement('li');
      line.textContent = i18n.t('The second copy writes these under the names shown and lists them in NINAIVU-NAMES.txt at the top of the copy. Files too large for the disk are left out.');
      list.append(line);
    }
    if (fit.coarse_times) {
      const line = document.createElement('li');
      line.textContent = i18n.t('This kind of disk keeps file times to two seconds, so a copy\'s time can differ a little from the original\'s.');
      list.append(line);
    }
  }

  render(status) {
    if (!status) return;
    this.status = status;
    $('#sc-block').hidden = false;
    if (document.activeElement !== $('#sc-folder')) $('#sc-folder').value = status.folder || '';
    $('#sc-enabled').checked = Boolean(status.enabled);
    const every = String(Math.round(status.every_hours || 0));
    $('#sc-every').value = ['24', '168', '0'].includes(every) ? every : '24';
    $('#sc-state').textContent = !status.enabled ? i18n.t('Off')
      : status.running ? i18n.t('Copying')
        : status.there ? i18n.t('On') : i18n.t('The disk is not plugged in');
    $('#sc-run-row').hidden = !status.enabled;
    $('#sc-start').hidden = Boolean(status.running);
    $('#sc-verify').hidden = Boolean(status.running);
    $('#sc-stop').hidden = !status.running;

    const lines = [];
    if (status.problem) lines.push(i18n.fromServer(status.problem));
    if (status.enabled || status.copied) {
      lines.push(i18n.t('{copied} of {files} files are on the copy ({size}); {owed} still to copy.', {
        copied: count(status.copied), files: count(status.files),
        size: size(status.copied_bytes), owed: count(status.owed),
      }));
    }
    if (status.renamed) {
      lines.push(i18n.t('{count} files are kept under names this disk takes; NINAIVU-NAMES.txt on the copy says which.', {
        count: count(status.renamed) }));
    }
    if (status.too_large) {
      lines.push(i18n.t('{count} files are too large for this disk and were left out (for example {path}). A disk formatted as exFAT takes them.', {
        count: count(status.too_large), path: (status.too_large_examples || [])[0] || '' }));
    }
    if (status.running && status.current) lines.push(i18n.t('Now: {path}', { path: status.current }));
    if (status.waiting) lines.push(i18n.t('Waiting: {reason}', { reason: i18n.fromServer(status.waiting) }));
    if (status.free_bytes !== null && status.free_bytes !== undefined) {
      lines.push(i18n.t('{free} free on that disk.', { free: size(status.free_bytes) }));
    }
    if (status.last_finished) lines.push(i18n.t('Last whole copy {when}.', { when: when(status.last_finished) }));
    if (status.last_verified) {
      lines.push(status.verify_bad || status.verify_missing
        ? i18n.t('Last checked {when}: {bad} no longer matched and {missing} were missing; they are copied again.', {
          when: when(status.last_verified), bad: count(status.verify_bad), missing: count(status.verify_missing) })
        : i18n.t('Last checked {when}: every file matched.', { when: when(status.last_verified) }));
    }
    if (status.error) lines.push(i18n.fromServer(status.error));
    $('#sc-progress').textContent = lines.join(' ');
  }
}
