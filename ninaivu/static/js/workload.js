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
    // Redrawn from the last reading in the new language.
    i18n.onChange(() => { if (this.data) this.render(this.data); });
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
      const names = { balanced: i18n.t('Balanced'), quiet: i18n.t('Quiet'), overnight: i18n.t('Overnight') };
      this.toast(body.mode ? i18n.t('{mode} from now on', { mode: names[data.mode] || data.mode })
        : i18n.t('Night hours {hours}', { hours: data.night_label }));
    } catch (exc) {
      this.toast(exc.message, true);
      this.refresh();
    }
  }

  render(data) {
    if (!data) return;
    this.data = data;
    $('#wl-block').hidden = false;
    const radio = document.querySelector(`input[name="wl-mode"][value="${data.mode}"]`);
    if (radio) radio.checked = true;
    const start = $('#wl-night-start');
    const end = $('#wl-night-end');
    if (document.activeElement !== start) start.value = data.night_start || '';
    if (document.activeElement !== end) end.value = data.night_end || '';
    $('#wl-night-row').hidden = data.mode !== 'overnight';

    const who = data.watching ? i18n.t('someone is watching a video')
      : data.browsing ? i18n.t('someone is using Ninaivu') : i18n.t('nobody is using Ninaivu');
    $('#wl-now').textContent = data.mode === 'overnight'
      ? `${who} · ${data.is_night ? i18n.t('night') : i18n.t('daytime')}` : who;

    const list = $('#wl-jobs');
    list.replaceChildren(...(data.jobs || []).map((job) => {
      const item = document.createElement('li');
      if (job.holding) {
        item.className = 'wl-held';
        item.textContent = i18n.t('{job}: waits — {reason}', { job: job.name, reason: job.holding });
      } else {
        item.textContent = job.boost
          ? i18n.t('{job}: may run, at full speed', { job: job.name })
          : i18n.t('{job}: may run', { job: job.name });
      }
      return item;
    }));
  }
}
