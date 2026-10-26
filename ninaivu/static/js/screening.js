/**
 * The Screening tab — what family members sent for an administrator to look at.
 *
 * The front end for api/api_screening.py. A family member flags a photograph
 * from the viewer or a selection in the family app; it waits here, with who
 * sent it and why, until an administrator makes one of four decisions on one
 * item or many at once:
 *
 *   Ignore        nothing is wrong with it; it stays as it is.
 *   Hide          documents and screenshots: only admins see it now.
 *   Mark explicit adult content only, set by hand so no later pass undoes it.
 *   Delete        into the bin, which Recently deleted on Folders puts back.
 *
 * A module of its own, as Straighten is: its own queue, its own choosing, and
 * a viewer whose buttons are these four decisions rather than the Folders
 * screen's.
 */

import { reportUnauthorized, thumbUrl } from './api.js';
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
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  if (response.status === 401) reportUnauthorized();
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(i18n.fromServer(data, i18n.t('Request failed ({status})', { status: response.status })));
  return data;
}

/** "Maya: Bank statement", or just "Maya", for each person who asked. */
function askLines(item) {
  return item.requests.map((ask) => {
    const who = ask.name || i18n.t('Someone no longer in the family list');
    return ask.note ? `${who}: ${ask.note}` : who;
  });
}

export class ScreeningPanel {
  constructor({ toast, onChange } = {}) {
    this.toast = toast || (() => {});
    this.onChange = onChange || (() => {});
    this.items = [];
    this.chosen = new Set();
    this.viewing = -1;
    this.busy = false;
  }

  wire() {
    $('#scr-refresh')?.addEventListener('click', () => this.load());
    $('#scr-all')?.addEventListener('click', () => this.chooseAll());
    for (const action of ['ignore', 'hide', 'explicit', 'delete']) {
      $(`#scr-${action}`)?.addEventListener('click', () => this.decide(this.chosenIds(), action));
      $(`#sv-${action}`)?.addEventListener('click', () => {
        const item = this.items[this.viewing];
        if (item) this.decide([item.id], action);
      });
    }
    $('#sv-close')?.addEventListener('click', () => this.closeViewer());
    $('#sv-prev')?.addEventListener('click', () => this.step(-1));
    $('#sv-next')?.addEventListener('click', () => this.step(1));
    $('#scr-viewer')?.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') this.closeViewer();
      if (event.key === 'ArrowLeft') this.step(-1);
      if (event.key === 'ArrowRight') this.step(1);
    });
    i18n.onChange(() => this.render());
  }

  show() { this.load(); }

  hide() { this.closeViewer(); }

  async load() {
    try {
      const data = await json('/api/admin/screening');
      this.items = data.items || [];
    } catch (exc) {
      this.toast(exc.message, true);
      return;
    }
    const present = new Set(this.items.map((item) => item.id));
    this.chosen = new Set([...this.chosen].filter((id) => present.has(id)));
    this.render();
    if (this.viewing >= 0) {
      if (!this.items.length) this.closeViewer();
      else {
        this.viewing = Math.min(this.viewing, this.items.length - 1);
        this.showItem();
      }
    }
  }

  /** The tab's live count, also kept by the console's Needs you. */
  setCount(count) {
    const badge = $('#screening-count');
    if (!badge) return;
    badge.textContent = count ? String(count) : '';
    badge.hidden = !count;
  }

  chosenIds() {
    return this.items.filter((item) => this.chosen.has(item.id)).map((item) => item.id);
  }

  chooseAll() {
    if (this.chosen.size === this.items.length) this.chosen.clear();
    else this.chosen = new Set(this.items.map((item) => item.id));
    this.render();
  }

  render() {
    const list = $('#scr-items');
    if (!list) return;
    this.setCount(this.items.length);
    $('#scr-empty').hidden = this.items.length > 0;
    $('#scr-pickbar').hidden = !this.items.length;
    const count = this.chosenIds().length;
    $('#scr-picked').textContent = count
      ? (count === 1 ? i18n.t('1 file chosen') : i18n.t('{count} files chosen', { count }))
      : i18n.t('Nothing chosen');
    for (const action of ['ignore', 'hide', 'explicit', 'delete']) {
      $(`#scr-${action}`).disabled = !count || this.busy;
    }
    $('#scr-all').textContent = count && count === this.items.length
      ? i18n.t('Choose none') : i18n.t('Choose all');
    list.replaceChildren(...this.items.map((item, index) => this.tile(item, index)));
  }

  tile(item, index) {
    const tile = el('div', 'fold-item scr-item');
    tile.dataset.id = item.id;
    if (item.visibility_name === 'hidden') tile.classList.add('is-hidden');
    if (item.explicit) tile.classList.add('is-explicit');
    const chosen = this.chosen.has(item.id);
    tile.classList.toggle('chosen', chosen);

    const open = el('button', 'fold-open');
    open.type = 'button';
    open.setAttribute('aria-label', i18n.t('Open {name}', { name: item.filename }));
    if (item.thumb) {
      const img = document.createElement('img');
      img.src = thumbUrl(item.id, 256, item.thumb_v);
      img.alt = '';
      img.loading = 'lazy';
      open.appendChild(img);
    } else {
      open.appendChild(el('span', 'fold-nothumb', (item.kind || '').toUpperCase()));
    }
    if (item.kind === 'video') open.appendChild(el('span', 'fold-play', '▶'));
    open.onclick = () => this.openViewer(index);
    tile.appendChild(open);

    const pick = document.createElement('input');
    pick.type = 'checkbox';
    pick.className = 'fold-pick';
    pick.checked = chosen;
    pick.setAttribute('aria-label', i18n.t('Choose {name}', { name: item.filename }));
    pick.onchange = () => {
      if (pick.checked) this.chosen.add(item.id); else this.chosen.delete(item.id);
      this.render();
    };
    tile.appendChild(pick);

    const badges = el('div', 'fold-badges');
    if (item.requests.length > 1) {
      badges.appendChild(el('span', 'badge', i18n.t('{count} asked', { count: item.requests.length })));
    }
    if (item.visibility_name === 'hidden') badges.appendChild(el('span', 'badge', i18n.t('Hidden')));
    if (item.explicit) badges.appendChild(el('span', 'badge xp', i18n.t('Explicit')));
    if (badges.childElementCount) tile.appendChild(badges);

    tile.appendChild(el('div', 'name', item.filename));
    const lines = askLines(item);
    tile.appendChild(el('div', 'where', lines.join(' · ')));
    tile.title = `${item.filename} — ${lines.join('; ')}`;
    return tile;
  }

  /** One decision on these items; they leave the queue. */
  async decide(ids, action) {
    if (!ids.length || this.busy) return;
    this.busy = true;
    this.render();
    let result;
    try {
      result = await json('/api/admin/screening/decide', { method: 'POST', body: { ids, action } });
    } catch (exc) {
      this.toast(exc.message, true);
      this.busy = false;
      this.render();
      return;
    }
    this.busy = false;
    for (const id of ids) this.chosen.delete(id);
    const items = i18n.items(result.settled || 0);
    const failed = (result.failed || []).length;
    const messages = {
      ignore: i18n.t('{items} left as they are.', { items }),
      hide: i18n.t('{items} hidden. Only admins can see them now.', { items }),
      explicit: i18n.t('{items} flagged explicit. Only admins can see them.', { items }),
      delete: i18n.t('{items} moved to the bin. Recently deleted on the Folders page puts them back.', { items }),
    };
    this.toast(messages[action]);
    if (failed) this.toast(i18n.t('{count} could not be moved to the bin.', { count: failed }), true);
    await this.load();
    this.onChange();
  }

  openViewer(index) {
    this.viewing = index;
    $('#scr-viewer').hidden = false;
    this.showItem();
    $('#sv-close').focus();
  }

  closeViewer() {
    const modal = $('#scr-viewer');
    if (!modal || modal.hidden) return;
    modal.hidden = true;
    this.viewing = -1;
    $('#sv-stage').replaceChildren();
  }

  step(by) {
    const next = this.viewing + by;
    if (next < 0 || next >= this.items.length) return;
    this.viewing = next;
    this.showItem();
  }

  showItem() {
    const item = this.items[this.viewing];
    if (!item) { this.closeViewer(); return; }
    const stage = $('#sv-stage');
    stage.replaceChildren();
    if ((item.kind === 'video' || item.kind === 'audio') && item.playable && !item.needs_proxy) {
      const media = document.createElement(item.kind === 'audio' ? 'audio' : 'video');
      media.src = `/api/file/${item.id}`;
      media.controls = true;
      media.playsInline = true;
      stage.appendChild(media);
    } else if (item.kind === 'video' || item.kind === 'audio') {
      const media = document.createElement('video');
      media.src = `/api/proxy/${item.id}`;
      media.controls = true;
      media.playsInline = true;
      if (item.thumb) media.poster = thumbUrl(item.id, 1024, item.thumb_v);
      stage.appendChild(media);
    } else {
      const img = document.createElement('img');
      img.alt = item.filename;
      if (item.thumb) img.src = thumbUrl(item.id, 1024, item.thumb_v);
      const full = new Image();
      full.onload = () => { if (this.items[this.viewing] === item) img.src = full.src; };
      full.src = `/api/preview/${item.id}`;
      if (item.rotation) img.style.transform = `rotate(${item.rotation}deg)`;
      stage.appendChild(img);
    }
    $('#sv-name').textContent = item.filename;
    const facts = [item.folder || i18n.t('the library')];
    if (item.date_key) facts.push(item.date_key);
    $('#sv-facts').textContent = facts.join(' · ');
    $('#sv-count').textContent = `${this.viewing + 1} / ${this.items.length}`;
    $('#sv-prev').disabled = this.viewing <= 0;
    $('#sv-next').disabled = this.viewing >= this.items.length - 1;
    const asks = $('#sv-asks');
    asks.replaceChildren(el('span', 'hint', i18n.t('Sent by')),
      ...askLines(item).map((line) => el('div', 'scr-ask', line)));
  }
}
