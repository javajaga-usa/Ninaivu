/**
 * Scan old prints — the family app's side of ninaivu/media/print_scan.py.
 *
 * Take or choose a photograph of some prints; the server finds each print in
 * it and sends back a small picture of each, ticked. The person unticks what
 * is not a print, chooses the clean-up, says when the prints are from (with
 * the decade the picture-search model guessed, where it is installed), and
 * saves. Saving runs on the server as a background job that this polls.
 *
 * Nothing here decides anything the server does not check again: which
 * prints, which options and which date are all validated there.
 */

import * as i18n from './i18n.js';

const $ = (sel) => document.querySelector(sel);
const sleep = (ms) => new Promise((resolve) => { setTimeout(resolve, ms); });

async function call(url, options = {}) {
  const response = await fetch(url, { headers: { Accept: 'application/json' }, ...options });
  let data = null;
  try { data = await response.json(); } catch { /* an HTML error page */ }
  if (!response.ok) {
    const error = new Error(data?.error || response.statusText);
    error.status = response.status;
    throw error;
  }
  return data;
}

export class PrintScan {
  constructor({ toast, openFolder }) {
    this.toast = toast || (() => {});
    this.openFolder = openFolder || (() => {});
    this.batch = null;
    this.busy = false;
    this.folder = '';
  }

  wire() {
    const modal = $('#prints-modal');
    if (!modal) return;
    $('#prints-btn')?.addEventListener('click', () => this.open());
    $('#prints-close').addEventListener('click', () => this.close());
    modal.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') this.close();
    });
    $('#prints-camera').addEventListener('click', () => $('#prints-camera-input').click());
    $('#prints-choose').addEventListener('click', () => $('#prints-input').click());
    for (const id of ['#prints-camera-input', '#prints-input']) {
      $(id).addEventListener('change', (event) => {
        const files = Array.from(event.target.files || []);
        event.target.value = '';
        if (files.length) this.detect(files);
      });
    }
    $('#prints-use-guess').addEventListener('click', () => {
      if (this.batch?.guess) $('#prints-date').value = `${this.batch.guess.decade}s`;
    });
    $('#prints-save').addEventListener('click', () => this.save());
    $('#prints-again').addEventListener('click', () => this.reset());
    $('#prints-open').addEventListener('click', () => {
      this.close();
      this.openFolder(this.folder);
    });
  }

  async open() {
    $('#prints-modal').hidden = false;
    this.reset();
    $('#prints-camera').focus();
    try {
      this.capabilities(await call('/api/prints/capabilities'));
    } catch (err) {
      if (err.status !== 401) this.status(err.message);
    }
  }

  close() {
    if (this.busy) {
      this.toast(i18n.t('Keep this page open until the prints are saved.'));
      return;
    }
    $('#prints-modal').hidden = true;
  }

  reset() {
    this.batch = null;
    this.folder = '';
    $('#prints-pick').hidden = false;
    $('#prints-review').hidden = true;
    $('#prints-save').hidden = true;
    $('#prints-again').hidden = true;
    $('#prints-open').hidden = true;
    $('#prints-progress').hidden = true;
    $('#prints-grid').replaceChildren();
    $('#prints-date').value = '';
    this.status('');
  }

  status(text) {
    $('#prints-status').textContent = text;
  }

  progress(value, max) {
    $('#prints-progress').hidden = false;
    $('#prints-fill').style.width = `${max ? Math.round((value / max) * 100) : 10}%`;
  }

  /** Disable what this server cannot do, and say why once. */
  capabilities(caps) {
    if (!caps) return;
    this.caps = caps;
    const faces = $('#prints-faces');
    const bigger = $('#prints-upscale');
    faces.disabled = !caps.restore;
    if (!caps.restore) faces.checked = false;
    bigger.disabled = !caps.upscale;
    if (!caps.upscale) bigger.value = '0';
    $('#prints-missing').hidden = caps.restore && caps.upscale;
  }

  /* -- step one: find the prints ---------------------------------------- */

  async detect(files) {
    if (this.busy) return;
    const limit = this.caps?.max_photos || 10;
    if (files.length > limit) {
      this.toast(i18n.t('Up to {count} photos at a time.', { count: limit }), true);
      return;
    }
    this.busy = true;
    this.status(i18n.t('Looking for prints…'));
    this.progress(0, 0);
    const form = new FormData();
    files.forEach((file) => form.append('photos', file));
    try {
      const batch = await call('/api/prints/detect', { method: 'POST', body: form });
      this.batch = batch;
      this.capabilities(batch.capabilities);
      this.review(batch);
    } catch (err) {
      this.status(i18n.t('Could not read the photo: {reason}', { reason: err.message }));
    } finally {
      this.busy = false;
      $('#prints-progress').hidden = true;
    }
  }

  review(batch) {
    const grid = $('#prints-grid');
    grid.replaceChildren();
    let count = 0;
    let whole = false;
    batch.photos.forEach((photo, photoIndex) => {
      photo.prints.forEach((spot) => {
        count += 1;
        whole = whole || spot.whole;
        const label = document.createElement('label');
        label.className = 'prints-tile';
        const tick = document.createElement('input');
        tick.type = 'checkbox';
        tick.checked = true;
        tick.dataset.photo = String(photoIndex);
        tick.dataset.print = String(spot.index);
        const img = document.createElement('img');
        img.src = spot.thumb;
        img.alt = i18n.t('Print {count}', { count });
        label.append(tick, img);
        grid.appendChild(label);
      });
    });
    $('#prints-pick').hidden = true;
    $('#prints-review').hidden = false;
    $('#prints-save').hidden = false;
    $('#prints-again').hidden = false;
    this.status(whole
      ? i18n.t('No separate prints were found in some photos, so the whole photo is used.')
      : i18n.t('{count} prints found.', { count }));

    const guess = batch.guess;
    $('#prints-guess').hidden = !guess;
    $('#prints-use-guess').hidden = !guess;
    if (guess) {
      $('#prints-guess').textContent = i18n.t('These look like the {decade}s ({percent}% sure). A guess, not a fact.',
        { decade: guess.decade, percent: Math.round(guess.confidence * 100) });
      $('#prints-use-guess').textContent = i18n.t('Use the {decade}s', { decade: guess.decade });
    }
    grid.querySelector('input')?.focus();
  }

  /* -- step two: save them ----------------------------------------------- */

  async save() {
    if (this.busy || !this.batch) return;
    const picks = [...document.querySelectorAll('#prints-grid input:checked')]
      .map((tick) => [Number(tick.dataset.photo), Number(tick.dataset.print)]);
    if (!picks.length) {
      this.toast(i18n.t('Tick at least one print to save.'), true);
      return;
    }
    this.busy = true;
    $('#prints-save').disabled = true;
    this.status(i18n.t('Saving the prints…'));
    this.progress(0, 0);
    try {
      const started = await call('/api/prints/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({
          session: this.batch.session,
          picks,
          fix_colours: $('#prints-colours').checked,
          restore_faces: $('#prints-faces').checked,
          upscale: Number($('#prints-upscale').value) || 0,
          keep_original: $('#prints-keep').checked,
          date: $('#prints-date').value.trim(),
        }),
      });
      const result = await this.follow(started.id);
      this.done(result);
    } catch (err) {
      this.status(i18n.t('Could not save the prints: {reason}', { reason: err.message }));
      $('#prints-progress').hidden = true;
    } finally {
      this.busy = false;
      $('#prints-save').disabled = false;
    }
  }

  async follow(id) {
    for (;;) {
      const state = await call(`/api/prints/jobs/${encodeURIComponent(id)}`);
      if (state.state === 'error') throw new Error(state.error);
      if (state.state === 'done') return call(`/api/prints/jobs/${encodeURIComponent(id)}/result`);
      if (state.max) {
        this.progress(state.value, state.max);
        this.status(i18n.t('Saving {done} of {total}…', { done: state.value, total: state.max }));
      }
      await sleep(700);
    }
  }

  done(result) {
    this.progress(1, 1);
    this.batch = null;
    $('#prints-review').hidden = true;
    $('#prints-save').hidden = true;
    const count = (result.saved || []).length;
    const failed = (result.failed || []).length;
    let text = result.pending_approval
      ? i18n.t('{count} saved. They join the library once an admin approves them.', { count })
      : i18n.t('{count} saved in {folder}.', { count, folder: result.folder });
    if (failed) text += ` ${i18n.t('{count} could not be saved.', { count: failed })}`;
    this.status(text);
    this.folder = result.folder || '';
    const open = $('#prints-open');
    open.hidden = Boolean(result.pending_approval) || !count;
    if (!open.hidden) open.focus();
  }
}
