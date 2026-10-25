/**
 * "Background jobs" — the Activity page's line per long job over the whole
 * library, saying how far it has got whether or not it is running now:
 * Complete, so many left, Running, Off, or waiting for its model.
 *
 * The activity strip shows a job only while it runs, so one that finished
 * overnight left no sign that it had. See ninaivu/server/job_status.py.
 *
 * Counting reads the whole index, so this asks every thirty seconds while
 * the page is open rather than every ten, and the server keeps its answer
 * for twenty.
 */

import { reportUnauthorized } from './api.js';
import * as i18n from './i18n.js';

const $ = (sel) => document.querySelector(sel);

const REFRESH = 30000;

const STATES = {
  running: i18n.key('Running'),
  waiting: i18n.key('Left to do'),
  complete: i18n.key('Complete'),
  off: i18n.key('Off'),
  unavailable: i18n.key('Not available'),
};

const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
};

async function fetchJobs(fresh) {
  const url = `/api/admin/jobs${fresh ? '?fresh=1' : ''}`;
  const response = await fetch(url, { headers: { Accept: 'application/json' } });
  const text = await response.text();
  const data = text ? JSON.parse(text) : null;
  if (!response.ok) {
    if (response.status === 401) reportUnauthorized(url);
    throw Object.assign(new Error(i18n.fromServer(data, response.statusText)), { status: response.status });
  }
  return data;
}

const n = (value) => Number(value || 0).toLocaleString(i18n.locale());

/** The words beside a job's state: how many, and anything it is waiting for. */
function detailOf(job) {
  const parts = [];
  if (job.state === 'complete') {
    if (job.total) parts.push(i18n.t('All {total} done', { total: n(job.total) }));
    else if (job.total === 0) parts.push(i18n.t('Nothing to do in this library'));
  } else if (job.state === 'running' || job.state === 'waiting') {
    if (job.total) {
      parts.push(i18n.t('{done} of {total} done, {left} left',
        { done: n(job.done), total: n(job.total), left: n(job.left) }));
    } else if (job.left != null) {
      parts.push(i18n.t('{left} left', { left: n(job.left) }));
    }
  }
  if (job.failed) parts.push(i18n.t('{count} could not be done', { count: n(job.failed) }));
  if (job.why) parts.push(i18n.t(job.why));
  if (job.finished_at && job.state !== 'running') {
    const then = new Date(job.finished_at * 1000);
    parts.push(i18n.t('last pass ended {when}', {
      when: then.toLocaleString(i18n.locale(), {
        day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }),
    }));
  }
  return parts.join(' · ');
}

export class JobStatusPanel {
  constructor({ toast }) {
    this.toast = toast || (() => {});
    this.timer = null;
    this.visible = false;
    this.data = null;
  }

  wire() {
    if (!$('#jobs-block')) return;
    $('#jobs-refresh').onclick = () => this.refresh(true);
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

  async refresh(fresh = false) {
    clearTimeout(this.timer);
    const button = $('#jobs-refresh');
    if (button) button.disabled = true;
    try {
      this.render(await fetchJobs(fresh));
    } catch (exc) {
      // A server from before this list has no endpoint for it: no block.
      if (exc.status === 404) { $('#jobs-block').hidden = true; return; }
      $('#jobs-rows').replaceChildren(el('p', 'hint', exc.message));
    } finally {
      if (button) button.disabled = false;
    }
    if (this.visible) this.timer = setTimeout(() => this.refresh(), REFRESH);
  }

  render(data) {
    this.data = data;
    const box = $('#jobs-rows');
    if (!box) return;
    const rows = (data?.jobs || []).map((job) => {
      const row = el('div', `job-status-row is-${job.state}`);
      row.dataset.job = job.id;
      row.appendChild(el('span', 'job-status-name', i18n.t(job.title)));
      row.appendChild(el('span', `job-status-state is-${job.state}`,
        i18n.t(STATES[job.state] || job.state)));
      row.appendChild(el('span', 'job-status-detail', detailOf(job)));
      return row;
    });
    box.replaceChildren(...(rows.length ? rows : [el('p', 'hint', i18n.t('Nothing yet.'))]));
    const counted = $('#jobs-counted');
    if (counted && data?.counted_at) {
      counted.textContent = i18n.t('Counted at {time}', {
        time: new Date(data.counted_at * 1000).toLocaleTimeString(
          i18n.locale(), { hour: '2-digit', minute: '2-digit' }),
      });
    }
  }
}
