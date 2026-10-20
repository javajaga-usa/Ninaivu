/**
 * Colour for black-and-white photographs, in the viewer (`colour_photos`).
 *
 * On a photograph with no colour of its own (grey, or one tint all over, as
 * a sepia print), the viewer's menu offers Add colour. The home computer
 * colours it (ninaivu/api/api_colour.py, DDColor) and the result is laid
 * over the original with a line to drag between them: the original on the
 * left, the colour on the right. Keep makes it a copy beside the original,
 * through the photo editor's own save, so the original is never written to;
 * a family member's copy waits for an administrator, as an edit does.
 *
 * On a colour copy the same button is Before and after, any time later; on
 * an original that has one, Show the colour copy.
 */

import { reportUnauthorized } from './api.js';
import * as i18n from './i18n.js';

async function getJson(url) {
  const response = await fetch(url, { credentials: 'same-origin' });
  if (response.status === 401) reportUnauthorized(url);
  if (!response.ok) throw new Error(String(response.status));
  return response.json();
}

async function failure(response) {
  const data = await response.json().catch(() => ({}));
  return new Error(data.error || data.message || '');
}

export class ColourPanel {
  constructor(viewer) {
    this.viewer = viewer;
    this.button = viewer.root.querySelector('#v-colour');
    this.label = this.button?.querySelector('.viewer-tools-more-label');
    this.status = null;
    this.item = null;
    this.overlay = null;
    this.asked = 0;
    if (this.button) this.button.onclick = () => this.act();
    this.onKey = (event) => this.key(event);
  }

  /** Called for every item the viewer shows. */
  async show(item) {
    this.close();
    this.item = item;
    this.status = null;
    if (!this.button) return;
    this.button.hidden = true;
    if (!this.viewer.colourOn || item?.kind !== 'picture') return;
    const asked = ++this.asked;
    let status;
    try {
      status = await getJson(`/api/asset/${item.id}/colour`);
    } catch { return; }
    if (asked !== this.asked || this.viewer.item?.id !== item.id) return;
    this.status = status;
    const [text, shown] = status.source ? [i18n.t('Before and after'), true]
      : status.mono ? [i18n.t('Add colour'), true]
        : status.copies?.length ? [i18n.t('Show the colour copy'), true] : ['', false];
    this.button.hidden = !shown;
    this.button.title = text;
    this.button.setAttribute('aria-label', text);
    if (this.label) this.label.textContent = text;
  }

  act() {
    const status = this.status;
    const item = this.item;
    if (!status || !item) return;
    this.viewer.closeMoreMenu?.();
    this.viewer.stopSlideshow?.();
    if (status.source) {
      this.compare({ before: status.before, after: item.view || item.src });
    } else if (status.mono) {
      this.make(item);
    } else if (status.copies?.length) {
      this.viewer.open([status.copies[0]], 0);
    }
  }

  async make(item) {
    if (!this.status.ready) {
      this.viewer.toast?.(i18n.t(this.status.why || 'The colouring model is not downloaded (AI models page)'), true);
      return;
    }
    const overlay = this.open();
    overlay.classList.add('busy');
    overlay.querySelector('.cc-note').textContent = i18n.t('Adding colour on this computer…');
    let blob;
    try {
      const response = await fetch(`/api/asset/${item.id}/colour`, { method: 'POST', credentials: 'same-origin' });
      if (response.status === 401) reportUnauthorized('/api/asset/colour');
      if (!response.ok) throw await failure(response);
      blob = await response.blob();
    } catch (error) {
      if (this.overlay === overlay) this.close();
      this.viewer.toast?.(i18n.t(error.message || 'This photograph could not be coloured.'), true);
      return;
    }
    if (this.overlay !== overlay || this.item !== item) return;      // closed meanwhile
    const after = URL.createObjectURL(blob);
    this.compare({ before: `/api/asset/${item.id}/colour/upright`, after, keep: { item, blob, url: after } });
  }

  /** The colour over the original, with a line to drag between them. */
  compare({ before, after, keep = null }) {
    const overlay = this.overlay || this.open();
    overlay.classList.remove('busy');
    this.keep = keep;
    const frame = overlay.querySelector('.cc-frame');
    overlay.querySelector('.cc-before').src = before;
    overlay.querySelector('.cc-after').src = after;
    overlay.querySelector('.cc-note').textContent = keep
      ? i18n.t('Drag the line to compare. The original is not changed.')
      : i18n.t('Drag the line to compare with the original.');
    overlay.querySelector('.cc-keep').hidden = !keep;
    const slider = overlay.querySelector('.cc-slider');
    const place = (percent) => {
      const value = Math.max(0, Math.min(100, percent));
      slider.value = String(value);
      frame.style.setProperty('--cc-split', `${value}%`);
    };
    this.place = place;
    slider.oninput = () => place(Number(slider.value));
    frame.onpointerdown = (event) => {
      frame.setPointerCapture?.(event.pointerId);
      const move = (e) => {
        const box = frame.getBoundingClientRect();
        place(((e.clientX - box.left) / box.width) * 100);
      };
      move(event);
      frame.onpointermove = move;
      frame.onpointerup = frame.onpointercancel = () => { frame.onpointermove = null; };
    };
    place(50);
    slider.focus({ preventScroll: true });
  }

  open() {
    const overlay = document.createElement('div');
    overlay.className = 'colour-compare';
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-label', i18n.t('Before and after'));
    overlay.innerHTML = `
      <div class="cc-frame">
        <img class="cc-before" alt="">
        <img class="cc-after" alt="">
        <span class="cc-line" aria-hidden="true"></span>
        <span class="cc-tag cc-tag-before"></span><span class="cc-tag cc-tag-after"></span>
        <span class="cc-spinner" aria-hidden="true"></span>
      </div>
      <input class="cc-slider" type="range" min="0" max="100" value="50">
      <div class="cc-bar">
        <p class="cc-note" role="status"></p>
        <button type="button" class="btn ghost small cc-close"></button>
        <button type="button" class="btn primary small cc-keep" hidden></button>
      </div>`;
    overlay.querySelector('.cc-before').alt = i18n.t('Before');
    overlay.querySelector('.cc-after').alt = i18n.t('After');
    overlay.querySelector('.cc-tag-before').textContent = i18n.t('Before');
    overlay.querySelector('.cc-tag-after').textContent = i18n.t('After');
    overlay.querySelector('.cc-slider').setAttribute('aria-label', i18n.t('Before and after'));
    overlay.querySelector('.cc-close').textContent = i18n.t('Close');
    overlay.querySelector('.cc-keep').textContent = i18n.t('Keep the colour copy');
    overlay.querySelector('.cc-close').onclick = () => this.close();
    overlay.querySelector('.cc-keep').onclick = () => this.save();
    this.viewer.root.appendChild(overlay);
    this.overlay = overlay;
    document.addEventListener('keydown', this.onKey, true);
    return overlay;
  }

  key(event) {
    if (!this.overlay) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      this.close();
    } else if ((event.key === 'ArrowLeft' || event.key === 'ArrowRight') && this.place) {
      // The arrows move the line here, not to the next photograph.
      event.preventDefault();
      event.stopPropagation();
      const slider = this.overlay.querySelector('.cc-slider');
      this.place(Number(slider.value) + (event.key === 'ArrowLeft' ? -5 : 5));
    }
  }

  async save() {
    const keep = this.keep;
    if (!keep) return;
    const button = this.overlay.querySelector('.cc-keep');
    button.disabled = true;
    try {
      const response = await fetch(`/api/asset/${keep.item.id}/edited-copy?made=colour`, {
        method: 'POST', body: keep.blob, credentials: 'same-origin',
        headers: { 'Content-Type': 'image/jpeg' },
      });
      if (response.status === 401) reportUnauthorized('/api/asset/edited-copy');
      if (response.status === 202) {
        // A family member's copy waits for an administrator, as an edit does.
        this.close();
        this.viewer.toast?.(i18n.t('Saved for an administrator to approve. It joins the library once they do.'));
        return;
      }
      if (!response.ok) throw await failure(response);
      const copy = await response.json();
      this.close();
      this.viewer.toast?.(i18n.t('Colour copy saved — original kept'));
      this.viewer.onChange?.(copy);
      this.viewer.open([copy.id], 0);
    } catch (error) {
      button.disabled = false;
      this.viewer.toast?.(i18n.t(error.message || 'Could not save the copy.'), true);
    }
  }

  close() {
    document.removeEventListener('keydown', this.onKey, true);
    if (this.keep?.url) URL.revokeObjectURL(this.keep.url);
    this.keep = null;
    this.place = null;
    this.overlay?.remove();
    this.overlay = null;
  }
}
