/**
 * Photo stacks, in the viewer (`photo_stacks`).
 *
 * The timeline shows a burst, or a photograph and the copies edited from it,
 * as one tile: its top photograph, with how many there are on it
 * (ninaivu/storage/stacks.py). Opened, a bar above the filmstrip shows every
 * frame; pressing one opens it, and the frames join the viewer's sequence
 * right after the top one, so the arrows walk through them too. The
 * household can put another frame on top, or show the stack as separate
 * photographs (and stack it again). Nothing is moved or deleted.
 */

import { reportUnauthorized, thumbUrl } from './api.js';
import * as i18n from './i18n.js';

async function call(url, options = {}) {
  const response = await fetch(url, { credentials: 'same-origin', ...options });
  if (response.status === 401) reportUnauthorized(url);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || data.message || data.description || '');
  return data;
}

const TOP_ICON = '<svg viewBox="0 0 24 24"><path d="m12 3.5 2.6 5.3 5.9.8-4.3 4.1 1 5.8-5.2-2.8-5.2 2.8 1-5.8-4.3-4.1 5.9-.8Z"/></svg>';

export class StackPanel {
  constructor(viewer) {
    this.viewer = viewer;
    /** Set by the app while `photo_stacks` is on. */
    this.on = false;
    this.bar = viewer.root.querySelector('#v-stack');
    /** The stack shown: { apart, top, can_change, items }. */
    this.data = null;
    this.asked = 0;
    this.busy = false;
  }

  /** Called for every item the viewer shows. */
  show(item) {
    if (!this.bar) return;
    if (!this.on || !item || item.kind !== 'picture' || !item.stack || this.viewer.isKiosk) {
      this.hide();
      return;
    }
    // A frame of the stack already shown: the bar stays, its mark moves.
    if (this.data?.items.some((frame) => frame.id === item.id)) {
      this.render(item.id);
      return;
    }
    this.hide();
    this.load(item.id);
  }

  hide() {
    this.asked += 1;
    this.data = null;
    if (this.bar) {
      this.bar.hidden = true;
      this.bar.innerHTML = '';
    }
    this.viewer.root.classList.remove('has-stack');
  }

  async load(id) {
    const asked = ++this.asked;
    try {
      const data = await call(`/api/stacks/${id}`);
      if (asked !== this.asked || this.viewer.item?.id !== id) return;
      if (!data.items || data.items.length < 2) return;
      this.data = data;
      this.render(id);
    } catch {
      // In no stack after all (taken apart elsewhere, or loosened since):
      // nothing to show.
    }
  }

  render(currentId) {
    const { items, top, apart, can_change: canChange } = this.data;
    const bar = this.bar;
    bar.innerHTML = '';
    bar.hidden = false;
    this.viewer.root.classList.add('has-stack');

    const label = document.createElement('span');
    label.className = 'stack-label';
    label.textContent = apart
      ? i18n.t('Shown separately')
      : i18n.t('{n} photos in this stack', { n: items.length });
    bar.appendChild(label);

    const frames = document.createElement('div');
    frames.className = 'stack-frames';
    for (const frame of items) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'stack-frame';
      button.classList.toggle('current', frame.id === currentId);
      button.classList.toggle('top', !apart && frame.id === top);
      button.setAttribute('aria-label', frame.id === top && !apart
        ? i18n.t('Top photo of the stack')
        : i18n.t('Open this photo'));
      button.setAttribute('aria-current', frame.id === currentId ? 'true' : 'false');
      const img = document.createElement('img');
      img.alt = '';
      img.loading = 'lazy';
      img.decoding = 'async';
      img.src = thumbUrl(frame.id, 256, frame.thumb_v);
      button.appendChild(img);
      if (frame.id === top && !apart) {
        const mark = document.createElement('span');
        mark.className = 'stack-top-mark';
        mark.innerHTML = TOP_ICON;
        button.appendChild(mark);
      }
      button.onclick = () => this.open(frame.id);
      frames.appendChild(button);
    }
    bar.appendChild(frames);
    frames.querySelector('.current')?.scrollIntoView?.({ block: 'nearest', inline: 'center' });

    if (!canChange) return;
    const actions = document.createElement('div');
    actions.className = 'stack-actions';
    const action = (text, run) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'btn ghost small';
      button.textContent = text;
      button.onclick = () => this.change(run);
      actions.appendChild(button);
    };
    if (apart) {
      action(i18n.t('Stack again'), () => this.post(currentId, 'apart', { apart: false }));
    } else {
      if (currentId !== top) {
        action(i18n.t('Put on top'), () => this.post(currentId, 'top'));
      }
      action(i18n.t('Show separately'), () => this.post(currentId, 'apart', { apart: true }));
    }
    bar.appendChild(actions);
  }

  /** Open a frame, its stack joining the viewer's sequence after the top one. */
  open(id) {
    const viewer = this.viewer;
    let index = viewer.ids.indexOf(id);
    if (index < 0) {
      const frames = this.data.items.map((frame) => frame.id);
      const held = new Set(viewer.ids);
      const missing = frames.filter((frame) => !held.has(frame));
      const anchor = viewer.ids.findIndex((one) => frames.includes(one));
      const ids = [...viewer.ids];
      ids.splice(anchor < 0 ? 0 : anchor + 1, 0, ...missing);
      viewer.ids = ids;
      index = ids.indexOf(id);
    }
    if (index >= 0) viewer.goTo(index);
  }

  async post(id, what, body) {
    await call(`/api/stacks/${id}/${what}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
  }

  async change(run) {
    if (this.busy) return;
    this.busy = true;
    const current = this.viewer.item?.id;
    try {
      await run();
      // The timeline is drawn again when the viewer closes.
      this.viewer.dispatchEvent(new CustomEvent('stacks', { detail: { id: current } }));
      this.data = null;
      if (current) await this.load(current);
    } catch (error) {
      this.viewer.toast?.(i18n.t(error.message || i18n.key('The stack could not be changed.')), true);
    } finally {
      this.busy = false;
    }
  }
}
