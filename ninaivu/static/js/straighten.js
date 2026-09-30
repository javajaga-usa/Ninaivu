/**
 * The Straighten tab — putting a library that was indexed sideways back up.
 *
 * The front end for the straightening endpoints in `ninaivu/api.py`. It is a
 * module of its own for the same reason the Archive and Faces tabs are: it
 * owns a long-running job, its own polling, and a review grid nothing else
 * shares.
 *
 * The shape of the screen is the shape of the promise. A survey proposes and
 * changes nothing; a separate, deliberate press applies. So the grid shows
 * every proposal as a before-and-after pair, turned with a CSS transform
 * rather than a re-rendered file — nothing on disk has moved yet, and the
 * preview must not pretend otherwise.
 */

import { reportUnauthorized } from './api.js';
import * as i18n from './i18n.js';

const $ = (sel) => document.querySelector(sel);
const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
};

async function json(url, options = {}) {
  const response = await fetch(url, {
    headers: {
      Accept: 'application/json',
      ...(options.body ? { 'Content-Type': 'application/json' } : {}),
    },
    ...options,
  });
  if (response.status === 401) reportUnauthorized();
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || i18n.t('Request failed ({status})', { status: response.status }));
  return data;
}

/** "12 of 40 will be straightened", and how many more there are. */
function countLine(chosen, shown, total) {
  const line = i18n.t('{chosen} of {shown} will be straightened', { chosen, shown });
  return total > shown
    ? `${line} · ${i18n.t('showing the {shown} clearest of {total}', { shown, total })}`
    : line;
}

export class StraightenPanel {
  constructor({ toast } = {}) {
    this.toast = toast || (() => {});
    this.visible = false;
    this.timer = null;
    this.state = { status: null, items: [], chosen: new Set() };
    this._lastSurveyRunning = false;
    this._proposalsLoaded = false;
  }

  wire() {
    // One button that knows what to do next, rather than a row of them that
    // each need a decision first. It downloads the model if that is what is
    // missing, looks for photographs if it is not, and stops the run if one
    // is going.
    $('#st-go')?.addEventListener('click', () => this.go());
    $('#st-apply')?.addEventListener('click', () => this.apply());
    $('#st-undo')?.addEventListener('click', () => this.undo());
    $('#st-resurvey')?.addEventListener('click', () => this.survey(true));
    $('#st-download')?.addEventListener('click', () => this.download());
    $('#st-auto')?.addEventListener('change', (event) => this.setAuto(event.target.checked));
    $('#st-auto-apply')?.addEventListener('change', (event) => this.setAutoApply(event.target.checked));
    // Drawn here from the last status, so a change of language redraws it.
    i18n.onChange(() => {
      if (!this.state.status) return;
      this.renderStatus();
      this.renderGrid();
    });
  }

  /** The switch: run by itself after every scan, or only when asked. */
  async setAuto(on) {
    try {
      await json('/api/admin/settings', { method: 'POST', body: JSON.stringify({ straighten_auto: !!on }) });
      if (this.state.status) this.state.status.auto = !!on;
      this.toast(on ? i18n.t('Sideways photographs will be looked for after every scan.')
        : i18n.t('Only when you press the button.'));
    } catch (exc) {
      const box = $('#st-auto');
      if (box) box.checked = !on;
      this.toast(exc.message, true);
    }
  }

  /** The second switch: turn what is found at once, or keep it for review. */
  async setAutoApply(on) {
    try {
      await json('/api/admin/settings', { method: 'POST', body: JSON.stringify({ straighten_auto_apply: !!on }) });
      if (this.state.status) this.state.status.auto_apply = !!on;
      this.toast(on ? i18n.t('Sideways photographs will be turned as soon as they are found.')
        : i18n.t('Found photographs will wait here for you to approve.'));
    } catch (exc) {
      const box = $('#st-auto-apply');
      if (box) box.checked = !on;
      this.toast(exc.message, true);
    }
  }

  /** Whatever the obvious next step is. */
  go() {
    const status = this.state.status || {};
    if (status.progress?.running) return this.stop();
    if (!status.model?.present) return this.download();
    return this.survey(false);
  }

  show() { this.visible = true; this.refresh(); }

  hide() {
    this.visible = false;
    if (this.timer) { clearTimeout(this.timer); this.timer = null; }
  }

  /* -- reading ----------------------------------------------------------- */

  async refresh() {
    try {
      const status = await json('/api/straighten/status');
      this.state.status = status;
      this.renderStatus();
      const running = !!status.progress?.running;
      if (status.model?.ready) {
        const justFinished = this._lastSurveyRunning && !running;
        if (!this._proposalsLoaded || justFinished) {
          await this.loadProposals();
          this._proposalsLoaded = true;
        }
      }
      this._lastSurveyRunning = running;
      this.renderGrid();
      if (this.visible && running) {
        this.timer = setTimeout(() => this.refresh(), 1200);
      }
    } catch (err) {
      this.toast(err.message, true);
    }
  }

  async loadProposals() {
    const data = await json('/api/straighten/proposals?limit=200');
    this.state.items = data.items || [];
    this.state.total = data.total || 0;
    // Everything is chosen to begin with: the common case is agreeing with a
    // survey and applying it, and making somebody tick two hundred boxes to
    // do that would be the same chore this feature exists to remove.
    this.state.chosen = new Set(this.state.items.map((i) => i.id));
  }

  /* -- rendering --------------------------------------------------------- */

  renderStatus() {
    const s = this.state.status || {};
    const model = s.model || {};
    const box = $('#st-status');
    if (!box) return;
    box.innerHTML = '';
    const auto = $('#st-auto');
    if (auto && 'auto' in s && auto !== document.activeElement) auto.checked = !!s.auto;
    const turn = $('#st-auto-apply');
    if (turn && 'auto_apply' in s && turn !== document.activeElement) turn.checked = !!s.auto_apply;

    if (!model.opencv?.available) {
      box.appendChild(el('p', 'warn',
        i18n.t('Orientation needs OpenCV, which this install does not have: {reason}.',
          { reason: model.opencv?.reason || i18n.t('unavailable') })));
      return;
    }
    const go = $('#st-go');
    if (!model.present) {
      box.appendChild(el('p', 'hint',
        i18n.t('One-time setup: Ninaivu needs a 77 MB model to tell which way up a photograph goes. It is downloaded once and kept with the rest of Ninaivu’s data.')));
      if (go) go.textContent = i18n.t('Download the model and start');
      return;
    }

    const p = s.progress || {};
    const counts = s.counts || {};
    if (go) {
      go.textContent = p.running ? i18n.t('Stop') : i18n.t('Find sideways photos now');
      go.classList.toggle('ghost', !!p.running);
      go.classList.toggle('primary', !p.running);
    }
    if (p.running) {
      const bar = el('div', 'progress');
      const fill = el('div', 'progress-fill');
      fill.style.width = `${p.percent || 0}%`;
      bar.appendChild(fill);
      box.appendChild(el('p', 'hint', p.proposed
        ? i18n.t('Looking at your photographs — {done} of {total}, {found} sideways so far',
          { done: p.processed || 0, total: p.total || 0, found: p.proposed })
        : i18n.t('Looking at your photographs — {done} of {total}',
          { done: p.processed || 0, total: p.total || 0 })));
      box.appendChild(bar);
    } else {
      const parts = [];
      if (counts.pending) parts.push(i18n.t('{count} ready to straighten', { count: counts.pending }));
      if (counts.applied) parts.push(i18n.t('{count} straightened', { count: counts.applied }));
      if (counts.dismissed) parts.push(i18n.t('{count} skipped', { count: counts.dismissed }));
      box.appendChild(el('p', 'hint', parts.length ? parts.join(' · ')
        : i18n.t('Nothing found yet. A search you start yourself changes nothing — it only shows you what it would put right.')));
      if (p.status === 'done' && !counts.pending && p.total) {
        box.appendChild(el('p', 'hint',
          i18n.t('Looked at {count} photographs and found nothing sideways.', { count: p.total })
          + (p.no_person ? ` ${i18n.t('{count} were sideways but had nobody in them, so they were left alone.', { count: p.no_person })}` : '')));
      }
      if (p.status === 'error' && p.message) {
        box.appendChild(el('p', 'warn', p.message));
      }
    }
    $('#st-undo').hidden = !s.last_batch;
  }

  renderGrid() {
    const grid = $('#st-grid');
    if (!grid) return;
    grid.innerHTML = '';
    const items = this.state.items || [];
    const review = $('#st-review');
    if (!items.length) {
      if (review) review.hidden = true;
      return;
    }
    if (review) review.hidden = false;
    const chosen = this.state.chosen.size;
    $('#st-count').textContent = countLine(chosen, items.length, this.state.total);
    const apply = $('#st-apply');
    if (apply) {
      apply.disabled = chosen === 0;
      apply.textContent = chosen === items.length
        ? i18n.t('Straighten all {count}', { count: chosen }) : i18n.t('Straighten {count}', { count: chosen });
    }

    for (const item of items) {
      const card = el('div', 'st-card');
      card.dataset.id = item.id;
      if (this.state.chosen.has(item.id)) card.classList.add('chosen');

      const shot = el('div', 'st-shot');
      const img = document.createElement('img');
      img.src = `${item.thumb}&v=${item.thumb_v}`;
      img.alt = item.filename;
      img.loading = 'lazy';
      // The turn is a preview, so it is done here rather than on disk.
      img.style.transform = `rotate(${item.rotation}deg)`;
      shot.appendChild(img);
      card.appendChild(shot);

      const meta = el('div', 'st-meta');
      meta.appendChild(el('span', 'st-name', item.filename));
      meta.appendChild(el('span', 'st-conf', i18n.t('{angle}° · {percent}% sure',
        { angle: item.rotation, percent: Math.round(item.confidence * 100) })));
      card.appendChild(meta);

      // The card is the control. A row of buttons underneath a row of
      // photographs is two things to read where one will do.
      card.setAttribute('role', 'switch');
      card.tabIndex = 0;
      card.setAttribute('aria-checked', String(this.state.chosen.has(item.id)));
      card.setAttribute('aria-label',
        i18n.t('{name} — click to skip this one', { name: item.filename }));
      const flip = () => this.flip(item.id);
      card.onclick = flip;
      card.onkeydown = (event) => {
        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); flip(); }
      };
      card.appendChild(el('span', 'st-skip',
        this.state.chosen.has(item.id) ? '' : i18n.t('Skipped')));
      grid.appendChild(card);
    }
  }

  flip(id) {
    if (this.state.chosen.has(id)) this.state.chosen.delete(id);
    else this.state.chosen.add(id);
    const card = $('#st-grid')?.querySelector(`[data-id="${id}"]`);
    if (card) {
      const isChosen = this.state.chosen.has(id);
      card.classList.toggle('chosen', isChosen);
      card.setAttribute('aria-checked', String(isChosen));
      const skip = card.querySelector('.st-skip');
      if (skip) skip.textContent = isChosen ? '' : i18n.t('Skipped');
    }
    this._updateCounts();
  }

  _updateCounts() {
    const items = this.state.items || [];
    const chosen = this.state.chosen.size;
    const countEl = $('#st-count');
    if (countEl) {
      countEl.textContent = countLine(chosen, items.length, this.state.total);
    }
    const apply = $('#st-apply');
    if (apply) {
      apply.disabled = chosen === 0;
      apply.textContent = chosen === items.length
        ? i18n.t('Straighten all {count}', { count: chosen }) : i18n.t('Straighten {count}', { count: chosen });
    }
  }


  /* -- doing ------------------------------------------------------------- */

  async download() {
    const button = $('#st-go');
    button.disabled = true;
    button.textContent = i18n.t('Downloading… (77 MB)');
    try {
      await json('/api/straighten/model', { method: 'POST' });
      this.toast(i18n.t('Ready. Looking for sideways photographs…'));
      await this.survey(false);
    } catch (err) {
      this.toast(i18n.t('The model could not be downloaded: {reason}', { reason: err.message }), true);
    } finally {
      button.disabled = false;
      button.textContent = i18n.t('Find sideways photos');
    }
  }

  async survey(rescan) {
    try {
      await json('/api/straighten/survey', {
        method: 'POST', body: JSON.stringify({ rescan: !!rescan }),
      });
      this.toast(rescan ? i18n.t('Looking at every photograph again…') : i18n.t('Survey started.'));
      this.refresh();
    } catch (err) { this.toast(err.message, true); }
  }

  async stop() {
    try {
      await json('/api/straighten/stop', { method: 'POST' });
      this.toast(i18n.t('Stopping.'));
    } catch (err) { this.toast(err.message, true); }
  }

  async apply() {
    const ids = [...this.state.chosen];
    if (!ids.length) return;
    // Anything left unticked was looked at and rejected, so record that in the
    // same press rather than making it a second decision on a second button.
    const skipped = this.state.items
      .map((i) => i.id).filter((id) => !this.state.chosen.has(id));
    try {
      if (skipped.length) {
        await json('/api/straighten/dismiss', {
          method: 'POST', body: JSON.stringify({ ids: skipped }),
        });
      }
      await json('/api/straighten/apply', {
        method: 'POST', body: JSON.stringify({ ids }),
      });
      this.toast(ids.length === 1 ? i18n.t('Straightening 1 photograph.')
        : i18n.t('Straightening {count} photographs.', { count: ids.length }));
      this.refresh();
    } catch (err) { this.toast(err.message, true); }
  }


  async undo() {
    try {
      const out = await json('/api/straighten/undo', {
        method: 'POST', body: JSON.stringify({}),
      });
      this.toast(out.restored === 1 ? i18n.t('1 photograph put back.')
        : i18n.t('{count} photographs put back.', { count: out.restored }));
      this.refresh();
    } catch (err) { this.toast(err.message, true); }
  }
}
