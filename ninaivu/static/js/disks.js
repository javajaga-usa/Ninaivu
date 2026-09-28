/**
 * Drives — the Activity page's account of each drive's health.
 *
 * What Windows has logged about each drive in the last week, filed under the
 * drive it happened on (ninaivu/storage/disk_health.py), worst first, with one
 * line on what to do about it. The drives Ninaivu depends on — the library,
 * the archive, its own records — say so, because a failing backup drive in a
 * drawer and a failing library drive are not the same emergency.
 */

import { reportUnauthorized } from './api.js';

const $ = (sel) => document.querySelector(sel);

async function json(url, options = {}) {
  const response = await fetch(url, { headers: { Accept: 'application/json' }, ...options });
  const text = await response.text();
  const data = text ? JSON.parse(text) : null;
  if (!response.ok) {
    if (response.status === 401) reportUnauthorized(url);
    throw Object.assign(new Error(data?.error || response.statusText),
      { status: response.status, data });
  }
  return data;
}

const bytes = (n) => {
  let value = Number(n) || 0;
  for (const unit of ['B', 'KB', 'MB', 'GB', 'TB']) {
    if (value < 1024 || unit === 'TB') return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${unit}`;
    value /= 1024;
  }
  return '';
};

const day = (stamp) => new Date(stamp * 1000).toLocaleDateString(undefined,
  { day: 'numeric', month: 'short' });

const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
};

const STATE = { critical: 'may be failing', warning: 'worth watching', ok: 'healthy' };

export class DiskPanel {
  constructor({ toast }) {
    this.toast = toast || (() => {});
    this.timer = null;
    this.visible = false;
  }

  wire() {
    const button = $('#dh-check');
    if (!button) return;
    button.onclick = async () => {
      button.disabled = true;
      button.textContent = 'Checking…';
      try {
        this.render(await json('/api/admin/disks/check', { method: 'POST' }));
      } catch (exc) {
        this.toast(exc.message, true);
      } finally {
        button.disabled = false;
        button.textContent = 'Check now';
      }
    };
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
    try {
      this.render(await json('/api/admin/disks'));
    } catch (exc) {
      // A server from before drive checks has no such endpoint.
      if (exc.status === 404) { $('#dh-block').hidden = true; return; }
      if (exc.status !== 401 && exc.status !== 423) this.toast(exc.message, true);
    }
    if (this.visible) this.timer = setTimeout(() => this.refresh(), 60000);
  }

  render(report) {
    if (!report) return;
    const box = $('#dh-drives');
    box.replaceChildren();
    if (!report.supported) {
      box.append(el('p', 'hint', 'Drive health comes from Windows, and this '
        + 'server is not running on Windows, so there is nothing to show.'));
      $('#dh-when').textContent = '';
      return;
    }
    if (report.error) box.append(el('p', 'hint', `The last check failed: ${report.error}`));
    for (const drive of report.drives || []) box.append(this.card(drive, report.window_days));
    $('#dh-when').textContent = report.checked_at
      ? `Checked ${new Date(report.checked_at * 1000).toLocaleString()}.`
      : 'Not checked yet — the first check runs a minute after Ninaivu starts.';
  }

  card(drive, days) {
    const card = el('div', `dh-drive ${drive.status}`);
    const head = el('div', 'dh-head');
    head.append(el('span', 'dh-name', drive.name),
      el('span', `dh-state ${drive.status}`, STATE[drive.status] || drive.status));
    const where = drive.letters.map((l) => `${l}:`).join(', ');
    const meta = [
      drive.connected ? where || 'no drive letter' : (where ? `not connected (was ${where})` : 'not connected'),
      drive.bus,
      drive.used_for.length ? `Ninaivu uses it for ${drive.used_for.join(', ')}` : '',
      ...drive.volumes.filter((v) => v.size).map((v) => `${v.letter}: ${bytes(v.free)} free of ${bytes(v.size)}`),
    ].filter(Boolean).join(' · ');
    card.append(head, el('span', 'dh-meta', meta));

    if (drive.problems.length) {
      const list = el('ul', 'dh-problems');
      for (const problem of drive.problems) {
        const when = problem.first && problem.kind !== 'low_space' && problem.kind !== 'unhealthy'
          ? ` (${day(problem.first)}${day(problem.first) !== day(problem.last) ? `–${day(problem.last)}` : ''})`
          : '';
        const count = problem.count > 1 ? `${problem.count.toLocaleString()} ` : '';
        list.append(el('li', '', `${count}${problem.words}${when}`));
      }
      card.append(list);
      const advice = drive.problems.find((p) => p.advice)?.advice;
      if (advice) card.append(el('p', 'dh-advice', advice));
    } else {
      card.append(el('span', 'dh-meta', `Nothing logged in the last ${days} days.`));
    }
    return card;
  }
}
