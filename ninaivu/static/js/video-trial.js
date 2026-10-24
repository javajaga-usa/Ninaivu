/**
 * Large files → Smaller videos: a trial (media/video_trial.py).
 *
 * Start, Pause and Clear, what the trial has found (how much smaller the
 * HEVC copies are, how close their picture is, what that would save across
 * the library), and each tried video with its original beside its copy to
 * play, and "Looks the same" or "Looks worse" for a person to say. Nothing
 * here changes the library; the server only makes and measures copies in
 * its own folder.
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

const score = (n) => (n == null ? '' : Number(n).toFixed(3));

/** How many tried videos one page shows. */
const PAGE = 24;

export class VideoTrial {
  constructor({ toast }) {
    this.toast = toast || (() => {});
    this.timer = null;
    this.visible = false;
    this.status = null;
    this.shown = PAGE;
    i18n.onChange(() => { if (this.status) this.render(this.status); });
  }

  wire() {
    if (!$('#vt-block')) return;
    $('#vt-start').onclick = () => this.act(() => json('/api/admin/video-trial/start',
      { method: 'POST', body: {} }), i18n.t('The trial has started'));
    $('#vt-pause').onclick = () => this.act(() => json('/api/admin/video-trial/pause',
      { method: 'POST' }));
    $('#vt-clear').onclick = () => {
      if (!window.confirm(i18n.t('Remove every trial copy? Your videos are not touched.'))) return;
      this.act(() => json('/api/admin/video-trial/clear', { method: 'POST' }));
    };
    $('#vt-more').onclick = () => { this.shown += PAGE; this.refresh(); };
  }

  show() { this.visible = true; this.refresh(); }

  hide() { this.visible = false; clearTimeout(this.timer); this.timer = null; }

  async refresh() {
    clearTimeout(this.timer);
    try {
      this.render(await json(`/api/admin/video-trial?limit=${this.shown}`));
    } catch (exc) {
      if (exc.status === 404 || exc.status === 403) { $('#vt-block').hidden = true; return; }
      if (exc.status !== 401 && exc.status !== 423) this.toast(exc.message, true);
    }
    this.schedule();
  }

  schedule() {
    clearTimeout(this.timer);
    if (!this.visible || !this.status) return;
    this.timer = setTimeout(() => this.refresh(), this.status.running ? 8000 : 60000);
  }

  async act(call, done) {
    const buttons = [...document.querySelectorAll('#vt-block .ar-actions button')];
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

  render(status) {
    this.status = status;
    $('#vt-block').hidden = false;
    const tried = (status.made || 0) + (status.larger || 0) + (status.failed || 0);
    const says = [];
    if (status.unavailable) {
      says.push(i18n.t('This computer cannot make HEVC copies, so the trial cannot run here.'));
    } else if (!status.sampled) {
      says.push(i18n.t('No trial yet.'));
    } else {
      says.push(i18n.t('{tried} of {sampled} videos tried.', { tried: count(tried), sampled: count(status.sampled) }));
      if (status.saving != null) {
        says.push(i18n.t('The HEVC copies are {percent}% smaller ({before} as {after}).', {
          percent: Math.round(status.saving * 100),
          before: size(status.original_bytes),
          after: size(status.copy_bytes),
        }));
      }
      if (status.library_saving_bytes) {
        says.push(i18n.t('At that rate the library’s {videos} of videos would take {saving} less.', {
          videos: size(status.library_video_bytes), saving: size(status.library_saving_bytes),
        }));
      }
      if (status.running && status.doing && status.doing.path) {
        says.push(i18n.t('Now: {file}', { file: status.doing.path }));
      }
    }
    $('#vt-says').textContent = says.join(' ');
    const picture = [];
    if (status.ssim_median != null) {
      picture.push(i18n.t('Picture likeness: {median} typical, {lowest} at the lowest (1.000 is the same picture).',
        { median: score(status.ssim_median), lowest: score(status.ssim_lowest) }));
    }
    const verdicts = status.verdicts || {};
    if (verdicts.same || verdicts.worse) {
      picture.push(i18n.t('You looked at {n}: {same} look the same, {worse} look worse.', {
        n: count((verdicts.same || 0) + (verdicts.worse || 0)),
        same: count(verdicts.same), worse: count(verdicts.worse),
      }));
    }
    if (status.tags_lost || status.rotation_lost) {
      picture.push(i18n.t('{n} copies did not keep every tag or the rotation.',
        { n: count(Math.max(status.tags_lost || 0, status.rotation_lost || 0)) }));
    }
    if (status.failed) picture.push(i18n.t('{n} could not be made.', { n: count(status.failed) }));
    $('#vt-picture').textContent = picture.join(' ');

    const left = (status.waiting || 0) > 0;
    $('#vt-start').hidden = Boolean(status.unavailable) || status.running || (status.sampled && !left);
    $('#vt-start').textContent = status.sampled ? i18n.t('Carry on') : i18n.t('Start the trial');
    $('#vt-pause').hidden = !status.running;
    $('#vt-clear').hidden = !status.sampled;
    this.renderList(status.samples || [], tried);
  }

  renderList(rows, tried) {
    const list = $('#vt-list');
    const open = new Set([...list.querySelectorAll('details[open]')].map((d) => d.dataset.id));
    list.replaceChildren();
    for (const row of rows) {
      if (row.outcome === 'waiting') continue;
      const item = document.createElement('li');
      const name = row.rel_path.split('/').pop();
      const line = document.createElement('span');
      if (row.outcome === 'made') {
        line.textContent = i18n.t('{name}: {before} as {after}, likeness {score}', {
          name, before: size(row.size), after: size(row.copy_size), score: score(row.ssim),
        });
      } else if (row.outcome === 'larger') {
        line.textContent = i18n.t('{name}: no smaller in HEVC, left as it is', { name });
      } else {
        line.textContent = i18n.t('{name}: could not be made', { name });
      }
      item.append(line);
      if (row.verdict) {
        const said = document.createElement('span');
        said.className = 'vt-said';
        said.textContent = row.verdict === 'same' ? i18n.t('Looks the same') : i18n.t('Looks worse');
        item.append(said);
      }
      if (row.outcome === 'made') item.append(this.compare(row, open.has(String(row.asset_id))));
      list.append(item);
    }
    $('#vt-more-row').hidden = rows.length < this.shown || this.shown >= tried;
  }

  compare(row, open) {
    const details = document.createElement('details');
    details.dataset.id = String(row.asset_id);
    details.open = open;
    const summary = document.createElement('summary');
    summary.textContent = i18n.t('Compare');
    details.append(summary);
    const pair = document.createElement('div');
    pair.className = 'vt-pair';
    const video = (which, label) => {
      const figure = document.createElement('figure');
      const player = document.createElement('video');
      player.controls = true;
      player.preload = 'none';
      player.src = `/api/admin/video-trial/${row.asset_id}/${which}`;
      const caption = document.createElement('figcaption');
      caption.textContent = label;
      figure.append(player, caption);
      return figure;
    };
    pair.append(video('original', i18n.t('Original')), video('copy', i18n.t('HEVC copy')));
    const actions = document.createElement('div');
    actions.className = 'ar-actions';
    for (const [verdict, label] of [['same', i18n.t('Looks the same')], ['worse', i18n.t('Looks worse')]]) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'btn ghost small';
      button.textContent = label;
      button.onclick = async () => {
        try {
          await json(`/api/admin/video-trial/${row.asset_id}/verdict`,
            { method: 'POST', body: { verdict: row.verdict === verdict ? null : verdict } });
          this.refresh();
        } catch (exc) {
          this.toast(exc.message, true);
        }
      };
      actions.append(button);
    }
    details.append(pair, actions);
    return details;
  }
}
