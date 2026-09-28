/**
 * "Sharing this computer" — the Activity page's choice of Balanced, Quiet or
 * Overnight, and a line per background job saying what it is doing about the
 * household right now. See ninaivu/server/workload.py for the rules.
 *
 * It refreshes every ten seconds while the page is open, because the answer
 * changes as people start and stop using the app, and a setting whose effect
 * cannot be seen is one nobody trusts.
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

const REFRESH = 10000;

export class WorkloadPanel {
  constructor({ toast }) {
    this.toast = toast || (() => {});
    this.timer = null;
    this.visible = false;
  }

  wire() {
    if (!$('#wl-block')) return;
    document.querySelectorAll('input[name="wl-mode"]').forEach((input) => {
      input.onchange = () => this.save({ mode: input.value });
    });
    $('#wl-save-night').onclick = () => this.save({
      night_start: $('#wl-night-start').value,
      night_end: $('#wl-night-end').value,
    });
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
      this.render(await json('/api/admin/workload'));
    } catch (exc) {
      // A server from before this setting has no endpoint for it; the block
      // stays out of the way rather than showing an error on every visit.
      if (exc.status === 404) { $('#wl-block').hidden = true; return; }
      if (exc.status !== 401) this.toast(exc.message, true);
    }
    if (this.visible) this.timer = setTimeout(() => this.refresh(), REFRESH);
  }

  async save(body) {
    try {
      const data = await json('/api/admin/workload', { method: 'POST', body });
      this.render(data);
      const names = { balanced: 'Balanced', quiet: 'Quiet', overnight: 'Overnight' };
      this.toast(body.mode ? `${names[data.mode]} from now on`
        : `Night hours ${data.night_label}`);
    } catch (exc) {
      this.toast(exc.message, true);
      this.refresh();
    }
  }

  render(data) {
    if (!data) return;
    $('#wl-block').hidden = false;
    const radio = document.querySelector(`input[name="wl-mode"][value="${data.mode}"]`);
    if (radio) radio.checked = true;
    const start = $('#wl-night-start');
    const end = $('#wl-night-end');
    if (document.activeElement !== start) start.value = data.night_start || '';
    if (document.activeElement !== end) end.value = data.night_end || '';
    $('#wl-night-row').hidden = data.mode !== 'overnight';

    const who = data.watching ? 'someone is watching a video'
      : data.browsing ? 'someone is using Ninaivu' : 'nobody is using Ninaivu';
    $('#wl-now').textContent = data.mode === 'overnight'
      ? `${who} · ${data.is_night ? 'night' : 'daytime'}` : who;

    const list = $('#wl-jobs');
    list.replaceChildren(...(data.jobs || []).map((job) => {
      const item = document.createElement('li');
      if (job.holding) {
        item.className = 'wl-held';
        item.textContent = `${job.name}: waits — ${job.holding}`;
      } else {
        item.textContent = `${job.name}: may run${job.boost ? ', at full speed' : ''}`;
      }
      return item;
    }));
  }
}
