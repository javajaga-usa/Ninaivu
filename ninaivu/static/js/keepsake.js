/**
 * The family archive on a drive: the library on a USB drive, with a page any
 * browser opens, so the photographs outlive Ninaivu (storage/keepsake.py).
 *
 * One block on the Handover page. It keeps its own element across the
 * page's re-renders, so a letter being typed is not lost when the plan above
 * it changes, and polls only while an archive is being made.
 */

import { reportUnauthorized } from './api.js';
import * as i18n from './i18n.js';

const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
};

async function json(url, body) {
  const response = await fetch(url, {
    method: body === undefined ? 'GET' : 'POST',
    headers: { Accept: 'application/json', ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401) reportUnauthorized(url);
    throw Object.assign(new Error(data?.error || response.statusText), { status: response.status });
  }
  return data;
}

const status = () => json('/api/admin/keepsake');
const post = (body) => json('/api/admin/keepsake', body);

const day = (seconds) => (seconds
  ? new Date(seconds * 1000).toLocaleDateString(i18n.locale(), { dateStyle: 'long' }) : '');

export class KeepsakeCard {
  constructor({ toast, pickFolder } = {}) {
    this.toast = toast || (() => {});
    this.pickFolder = pickFolder || null;
    this.block = el('div', 'block keepsake');
    this.state = null;
    this.folder = null;
    this.letter = null;
    this.everything = false;
    this.small = false;
    this.timer = 0;
  }

  /** The block, loading what the server says the first time. */
  element() {
    if (!this.state) this.refresh();
    else this.render();
    return this.block;
  }

  async refresh() {
    try {
      this.state = await status();
    } catch (exc) {
      this.block.replaceChildren(el('p', 'hint', exc.message));
      return;
    }
    if (this.folder == null) this.folder = this.state.last_folder || '';
    if (this.letter == null) this.letter = this.state.letter || '';
    this.render();
    clearTimeout(this.timer);
    if (this.state.running && this.block.isConnected) this.timer = setTimeout(() => this.refresh(), 1500);
  }

  stop() { clearTimeout(this.timer); }

  render() {
    const s = this.state;
    const block = this.block;
    block.replaceChildren();
    block.appendChild(el('h2', null, i18n.t('Family photo archive on a drive')));
    block.appendChild(el('p', 'hint', i18n.t(
      'Copies the photographs to a USB drive with a page that opens in any web browser: by month, with the people, albums and places, and no Ninaivu needed. Made again on the same drive, it copies only what is new.')));
    block.appendChild(el('p', null, s.last_made
      ? i18n.t('Last made on {date}.', { date: day(s.last_made) })
      : i18n.t('Not made yet.')));

    const where = el('div', 'setting-field');
    const label = el('label', null, i18n.t('Drive or folder'));
    label.htmlFor = 'keepsake-folder';
    const row = el('div', 'row');
    const input = el('input', 'input');
    input.id = 'keepsake-folder';
    input.type = 'text';
    input.value = this.folder;
    input.placeholder = '/Volumes/USB';
    input.oninput = () => { this.folder = input.value; };
    row.appendChild(input);
    if (this.pickFolder) {
      const browse = el('button', 'btn ghost', i18n.t('Browse…'));
      browse.type = 'button';
      browse.onclick = () => this.pickFolder({
        title: i18n.t('Choose the drive for the family archive'),
        cta: i18n.t('Use this folder'),
        anyFolder: true,
        start: this.folder,
        pick: (path) => { this.folder = path; this.render(); },
      });
      row.appendChild(browse);
    }
    where.append(label, row);
    block.appendChild(where);

    for (const [key, text, hint] of [
      ['everything', i18n.t('Include hidden photographs'),
        i18n.t('Off, only what the family sees goes in. Flagged items, sound recordings and the bin never go.')],
      ['small', i18n.t('Smaller copies of the photographs'),
        i18n.t('For a small drive: photographs at 2048 pixels as JPEG. Videos are copied as they are.')],
    ]) {
      const toggle = el('label', 'toggle');
      const box = el('input');
      box.type = 'checkbox';
      box.checked = this[key];
      box.onchange = () => { this[key] = box.checked; };
      toggle.append(box, el('span', null, text));
      block.append(toggle, el('p', 'hint', hint));
    }

    const letter = el('div', 'setting-field');
    const letterLabel = el('label', null, i18n.t('A letter to whoever opens it'));
    letterLabel.htmlFor = 'keepsake-letter';
    const area = el('textarea', 'input handover-text');
    area.id = 'keepsake-letter';
    area.rows = 5;
    area.maxLength = 20000;
    area.value = this.letter;
    area.placeholder = i18n.t('Who to ask, where the rest is kept, what matters. Never a password or a key.');
    area.oninput = () => { this.letter = area.value; };
    letter.append(letterLabel, area);
    block.appendChild(letter);

    if (s.running) {
      const line = el('p', 'hint');
      line.setAttribute('aria-live', 'polite');
      line.textContent = `${s.phase || ''} ${s.total ? `${s.done.toLocaleString()} / ${s.total.toLocaleString()}` : ''}`.trim();
      const bar = el('progress');
      bar.max = Math.max(1, s.total);
      bar.value = s.done;
      block.append(line, bar);
    } else if (s.error) {
      const note = el('div', 'ar-note ar-note-bad');
      note.appendChild(el('p', null, s.error));
      block.appendChild(note);
    } else if (s.message) {
      block.appendChild(el('p', 'hint', s.message));
    }
    if (s.problems?.length && !s.running) {
      const list = el('ul', 'hint');
      for (const p of s.problems.slice(0, 10)) list.appendChild(el('li', null, `${p.file}: ${p.why}`));
      block.append(el('p', 'hint', i18n.t('Some files could not be copied:')), list);
    }

    const actions = el('div', 'row');
    if (s.running) {
      const stop = el('button', 'btn ghost', i18n.t('Stop'));
      stop.type = 'button';
      stop.onclick = () => this.act({ stop: true });
      actions.appendChild(stop);
    } else {
      const make = el('button', 'btn primary', s.last_made ? i18n.t('Bring the archive up to date') : i18n.t('Make the archive'));
      make.type = 'button';
      make.onclick = () => this.act({
        folder: this.folder.trim(), everything: this.everything, small: this.small, letter: this.letter,
      });
      const save = el('button', 'btn ghost', i18n.t('Save the letter'));
      save.type = 'button';
      save.onclick = async () => {
        await this.act({ letter: this.letter });
        this.toast(i18n.t('Saved'));
      };
      actions.append(make, save);
    }
    block.appendChild(actions);
  }

  async act(body) {
    try {
      this.state = await post(body);
    } catch (exc) {
      this.toast(exc.message, true);
      return;
    }
    this.render();
    clearTimeout(this.timer);
    if (this.state.running) this.timer = setTimeout(() => this.refresh(), 1500);
  }
}
