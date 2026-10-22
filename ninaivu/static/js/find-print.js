/**
 * Find this print — the family app's side of ninaivu/media/find_print.py
 * (served by ninaivu/api/api_find_print.py; offered only while `find_print`
 * is switched on, and only to the family).
 *
 * Point the phone at a framed photograph on the wall, or one print in an
 * album, and the server says which photograph in the library it was printed
 * from: the sure ones first and marked, then any it might be. A tap opens
 * them in the viewer. When the library has no copy, or none of the likely
 * ones is it, "Take it in" hands the same photograph to Scan old prints
 * (static/js/print-scan.js), already found and flattened, to be filed as a
 * print of its own.
 *
 * Nothing here decides who may see what: the server only answers with
 * photographs this person may open.
 */

import { thumbUrl } from './api.js';
import * as i18n from './i18n.js';

const $ = (sel) => document.querySelector(sel);

async function call(url, options = {}) {
  // Multipart, which a cross-origin form could also send; X-Requested-With
  // is how a script says it is not one (ninaivu/__init__.py).
  const response = await fetch(url, {
    ...options,
    headers: { Accept: 'application/json', 'X-Requested-With': 'fetch', ...(options.headers || {}) },
  });
  let data = null;
  try { data = await response.json(); } catch { /* an HTML error page */ }
  if (!response.ok) {
    const error = new Error(i18n.fromServer(data, response.statusText));
    error.status = response.status;
    throw error;
  }
  return data;
}

export class FindPrint {
  constructor({ toast, openItems, takeIn }) {
    this.toast = toast || (() => {});
    this.openItems = openItems || (() => Promise.resolve());
    this.takeIn = takeIn || (() => {});
    this.answer = null;
    this.busy = false;
  }

  wire() {
    const modal = $('#find-print-modal');
    if (!modal) return;
    $('#find-print-btn')?.addEventListener('click', () => this.open());
    $('#find-print-close').addEventListener('click', () => this.close());
    modal.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') this.close();
    });
    $('#find-print-camera').addEventListener('click', () => $('#find-print-camera-input').click());
    $('#find-print-choose').addEventListener('click', () => $('#find-print-input').click());
    for (const id of ['#find-print-camera-input', '#find-print-input']) {
      $(id).addEventListener('change', (event) => {
        const file = (event.target.files || [])[0];
        event.target.value = '';
        if (file) this.find(file);
      });
    }
    $('#find-print-again').addEventListener('click', () => this.reset());
    $('#find-print-take').addEventListener('click', () => {
      if (!this.answer) return;
      const batch = this.answer;
      $('#find-print-modal').hidden = true;
      this.takeIn(batch);
    });
  }

  /** Shown on the Add photos menu while the switch is on, for the family. */
  show(on) {
    const button = $('#find-print-btn');
    if (button) button.hidden = !on;
  }

  open() {
    $('#find-print-modal').hidden = false;
    this.reset();
    $('#find-print-camera').focus();
  }

  close() {
    if (this.busy) return;
    $('#find-print-modal').hidden = true;
  }

  reset() {
    this.answer = null;
    $('#find-print-pick').hidden = false;
    $('#find-print-result').hidden = true;
    $('#find-print-again').hidden = true;
    $('#find-print-take').hidden = true;
    $('#find-print-progress').hidden = true;
    $('#find-print-grid').replaceChildren();
    this.status('');
  }

  status(text) {
    $('#find-print-status').textContent = text;
  }

  async find(file) {
    if (this.busy) return;
    this.busy = true;
    this.status(i18n.t('Looking through the library…'));
    $('#find-print-progress').hidden = false;
    $('#find-print-fill').style.width = '35%';
    const form = new FormData();
    form.append('photo', file);
    try {
      const answer = await call('/api/prints/find', { method: 'POST', body: form });
      this.answer = answer;
      this.render(answer);
    } catch (err) {
      this.status(i18n.t('Could not look for the print: {reason}', { reason: err.message }));
    } finally {
      this.busy = false;
      $('#find-print-progress').hidden = true;
    }
  }

  render(answer) {
    // The print as the server cut it out: only ever its own JPEG, never a
    // name or address that came from the page.
    const thumb = String(answer.photos?.[0]?.prints?.[0]?.thumb || '');
    const shot = $('#find-print-shot');
    shot.hidden = !thumb.startsWith('data:image/jpeg;base64,');
    if (!shot.hidden) shot.src = thumb;
    const sure = answer.matches.filter((m) => m.sure);
    let verdict;
    if (answer.verdict === 'sure') {
      verdict = sure.length === 1
        ? i18n.t('Found it: this is the photo it was printed from.')
        : i18n.t('Found it: {count} copies of this photo.', { count: sure.length });
    } else if (answer.verdict === 'maybe') {
      verdict = i18n.t('Not sure. It might be one of these.');
    } else {
      verdict = i18n.t('The library has no copy of this photo yet.');
    }
    $('#find-print-verdict').textContent = verdict;

    const grid = $('#find-print-grid');
    grid.replaceChildren();
    const ids = answer.matches.map((m) => m.id);
    answer.matches.forEach((match, index) => grid.appendChild(this.tile(match, ids, index)));
    grid.hidden = !answer.matches.length;

    $('#find-print-pick').hidden = true;
    $('#find-print-result').hidden = false;
    $('#find-print-again').hidden = false;
    // Taking it in is offered whenever nothing is certainly it: a likely
    // match may be another print from the same afternoon.
    const take = $('#find-print-take');
    take.hidden = answer.verdict === 'sure';
    take.textContent = answer.verdict === 'maybe'
      ? i18n.t('None of these: take it in') : i18n.t('Take it in');
    let note = '';
    if (answer.verdict !== 'sure' && !answer.search) {
      note = i18n.t('Only the picture fingerprint was compared. With the picture search on, a faded or glary print is found more often.');
    }
    this.status(note);
    (grid.querySelector('button') || (take.hidden ? $('#find-print-again') : take)).focus();
  }

  tile(match, ids, index) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `fp-tile${match.sure ? ' sure' : ''}`;
    const img = document.createElement('img');
    img.src = thumbUrl(match.id, 512, match.thumb_v);
    img.alt = '';
    img.loading = 'lazy';
    const meta = document.createElement('span');
    meta.className = 'fp-meta';
    const label = document.createElement('b');
    label.textContent = match.sure ? i18n.t('The same photo') : i18n.t('Might be this');
    const when = document.createElement('span');
    when.textContent = i18n.folderDate(match.date) || i18n.t('Date unknown');
    meta.append(label, when);
    button.append(img, meta);
    // Only an administrator is ever sent these, and is told so.
    const badge = match.explicit ? i18n.t('Explicit') : (match.hidden ? i18n.t('Hidden') : '');
    if (badge) {
      const mark = document.createElement('span');
      mark.className = 'fp-badge';
      mark.textContent = badge;
      button.appendChild(mark);
    }
    button.setAttribute('aria-label', `${label.textContent}, ${when.textContent}`);
    button.addEventListener('click', async () => {
      const modal = $('#find-print-modal');
      modal.hidden = true;
      await this.openItems(ids, index);
      modal.hidden = false;
      button.focus();
    });
    return button;
  }
}
