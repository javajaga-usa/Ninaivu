/**
 * Albums for when there is no signal.
 *
 * Each album a family member or administrator opens is handed to the service
 * worker (static/sw.js, "ALBUMS KEPT FOR OFFLINE"), which keeps its name, its
 * order and its thumbnails for the last six albums opened, for that person
 * only. When the server cannot be reached, the offline banner offers
 * **Saved albums**: those albums, a grid of each, and a photograph at a time,
 * all from what was kept. Nothing is kept for a guest, nor a photograph that
 * is hidden or marked explicit (an administrator's included), nor an
 * original. Signing out, or somebody else signing in on the device, wipes it.
 */

import * as i18n from './i18n.js';
import { $, el } from './ui.js';

/** Segment flags (api.py segments): explicit, and hidden. */
const FLAG_EXPLICIT = 4;
const FLAG_HIDDEN = 128;
/** Item fields by position in a segment (api.py segments). */
const ID = 0;
const ASPECT = 1;
const FLAGS = 3;
const VERSION = 5;

function worker() {
  try {
    return navigator.serviceWorker?.controller || null;
  } catch {
    return null;
  }
}

/** Whether the person viewing may keep albums on this device. */
function keeper(user) {
  return user && !user.anonymous && ['family', 'admin'].includes(user.role) ? user.id : null;
}

/** Say who is viewing: anybody else's kept albums are wiped. */
export function tellViewer(user, on = true) {
  worker()?.postMessage({ type: 'ninaivu:offline-viewer', user: on ? keeper(user) : null });
}

/** Keep the album just opened, from the segments the server answered with. */
export function keepAlbum({ user, album, segments, sizes }) {
  const who = keeper(user);
  const target = worker();
  if (who == null || !target || !album) return;
  const items = [];
  for (const segment of segments || []) {
    for (const item of segment.items || []) {
      const flags = Number(item[FLAGS]) || 0;
      if (flags & (FLAG_EXPLICIT | FLAG_HIDDEN)) continue;
      if ((flags & 2) === 0) continue;            // no thumbnail to keep
      items.push({ id: item[ID], a: item[ASPECT], v: item[VERSION] || '' });
    }
  }
  // The larger of the sizes the server makes, up to 640: sharp enough to
  // look at a photograph full screen on a phone, small enough to keep many.
  const size = Math.max(...(sizes || [256]).filter((s) => s <= 640), 256);
  target.postMessage({ type: 'ninaivu:offline-keep', user: who, album: { id: album.id, name: album.name },
    items, size });
}

/** The albums kept on this device, or none when there is no worker. */
export function savedAlbums(timeout = 1500) {
  const target = worker();
  if (!target) return Promise.resolve([]);
  return new Promise((resolve) => {
    const channel = new MessageChannel();
    const timer = setTimeout(() => resolve([]), timeout);
    channel.port1.onmessage = (event) => {
      clearTimeout(timer);
      resolve(Array.isArray(event.data?.albums) ? event.data.albums : []);
    };
    try {
      target.postMessage({ type: 'ninaivu:offline-list' }, [channel.port2]);
    } catch {
      clearTimeout(timer);
      resolve([]);
    }
  });
}

/* --- the Saved albums screen --------------------------------------------- */

let albums = [];

function close() {
  const sheet = $('#offline-albums');
  if (sheet) sheet.hidden = true;
}

function showPhoto(album, index) {
  const view = $('#offline-photo');
  const img = $('#offline-photo-img');
  if (!view || !img) return;
  const item = album.items[index];
  if (!item) return;
  img.src = item.url;
  view.hidden = false;
  view.dataset.index = String(index);
  $('#offline-photo-prev').onclick = () => showPhoto(album, Math.max(0, index - 1));
  $('#offline-photo-next').onclick = () => showPhoto(album, Math.min(album.items.length - 1, index + 1));
  $('#offline-photo-close').onclick = () => { view.hidden = true; img.removeAttribute('src'); };
}

function showAlbum(album) {
  $('#offline-albums-title').textContent = album.name;
  $('#offline-albums-back').hidden = false;
  const box = $('#offline-albums-body');
  const grid = el('div', 'offline-grid');
  album.items.forEach((item, index) => {
    const tile = el('button', 'offline-tile');
    tile.type = 'button';
    const img = el('img');
    img.src = item.url;
    img.alt = '';
    img.loading = 'lazy';
    tile.style.aspectRatio = `${Math.max(30, Math.min(400, item.a)) / 100}`;
    tile.appendChild(img);
    tile.onclick = () => showPhoto(album, index);
    grid.appendChild(tile);
  });
  box.replaceChildren(grid);
}

function showList() {
  $('#offline-albums-title').textContent = i18n.t('Saved albums');
  $('#offline-albums-back').hidden = true;
  const box = $('#offline-albums-body');
  if (!albums.length) {
    box.replaceChildren(el('p', 'hint', i18n.t('No album is saved on this device yet. Albums you open while connected are kept here for when there is no signal.')));
    return;
  }
  const list = el('div', 'offline-list');
  for (const album of albums) {
    const card = el('button', 'album-card offline-album');
    card.type = 'button';
    if (album.items[0]) {
      const img = el('img');
      img.src = album.items[0].url;
      img.alt = '';
      card.appendChild(img);
    }
    const text = el('span', 'trip-text');
    text.append(el('b', '', album.name), el('span', 'n', i18n.items(album.items.length)));
    card.appendChild(text);
    card.onclick = () => showAlbum(album);
    list.appendChild(card);
  }
  box.replaceChildren(list);
}

/** Open the Saved albums screen. */
export async function openSavedAlbums() {
  albums = await savedAlbums();
  const sheet = $('#offline-albums');
  if (!sheet) return;
  sheet.hidden = false;
  $('#offline-albums-close').onclick = close;
  $('#offline-albums-back').onclick = showList;
  showList();
}

/** Offer the Saved albums button on the offline banner when any are kept;
 *  *open* opens the screen at once (a visit that found no server). */
export async function offerSavedAlbums({ open = false } = {}) {
  const button = $('#offline-saved-btn');
  const kept = await savedAlbums();
  if (button) {
    button.hidden = kept.length === 0;
    button.onclick = openSavedAlbums;
  }
  if (open && kept.length) openSavedAlbums();
}

/** The server answers again: the screen goes, the gallery is the server's. */
export function closeSavedAlbums() {
  close();
  const button = $('#offline-saved-btn');
  if (button) button.hidden = true;
}
