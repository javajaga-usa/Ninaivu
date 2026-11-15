/**
 * Suggested albums, for an administrator in the family app's sidebar
 * (ninaivu/features/event_albums): trips and festivals found from dates and
 * places. "Make album" makes an ordinary album of one, which the family then
 * sees as any album, each person the photographs in it they may open; "No
 * thanks" turns it down for good. Nobody but an administrator is asked, or
 * sees the list.
 */

import { home, thumbUrl } from './api.js';
import * as i18n from './i18n.js';
import { state } from './store.js';
import { $, toast } from './ui.js';

/** What to call when an album was made, so the album list shows it. */
let albumsChanged = () => {};

function card(item) {
  const row = document.createElement('div');
  row.className = 'event-album';
  row.dataset.key = item.key;
  const covers = document.createElement('div');
  covers.className = 'event-album-covers';
  for (const id of (item.covers || []).slice(0, 3)) {
    const img = document.createElement('img');
    img.src = thumbUrl(id, 80);
    img.alt = '';
    img.loading = 'lazy';
    covers.appendChild(img);
  }
  const text = document.createElement('div');
  text.className = 'trip-text';
  const title = document.createElement('b');
  title.textContent = item.title;
  title.title = item.title;
  const n = document.createElement('span');
  n.className = 'n';
  n.textContent = item.kind === 'festival'
    ? `${i18n.t('Festival')} · ${i18n.items(item.count)}`
    : `${i18n.t('Trip')} · ${i18n.items(item.count)}`;
  text.append(title, n);
  const actions = document.createElement('div');
  actions.className = 'event-album-actions';
  const make = document.createElement('button');
  make.type = 'button';
  make.className = 'btn small primary';
  make.textContent = i18n.t('Make album');
  make.onclick = () => answer(item, 'approve', [make, skip]);
  const skip = document.createElement('button');
  skip.type = 'button';
  skip.className = 'btn small ghost';
  skip.textContent = i18n.t('No thanks');
  skip.onclick = () => answer(item, 'decline', [make, skip]);
  actions.append(make, skip);
  row.append(covers, text, actions);
  return row;
}

async function answer(item, how, buttons) {
  for (const b of buttons) b.disabled = true;
  try {
    if (how === 'approve') {
      const done = await home.postEventAlbumsByKeyApprove(item.key, { body: {} });
      toast(i18n.t('Made the album “{name}” with {count}.', { name: item.title, count: i18n.items(done.count) }));
      albumsChanged();
    } else {
      await home.postEventAlbumsByKeyDecline(item.key, { body: {} });
      toast(i18n.t('It will not be suggested again.'));
    }
  } catch (exc) {
    toast(exc.message, true);
  }
  loadEventAlbums();
}

/** Ask for the suggestions and draw them; hidden for anybody but an
 *  administrator, or with event albums switched off. */
export async function loadEventAlbums({ onAlbumMade } = {}) {
  if (onAlbumMade) albumsChanged = onAlbumMade;
  const block = $('#event-albums-block');
  const box = $('#event-album-list');
  if (!block || !box) return;
  if (state.user?.role !== 'admin' || state.user?.anonymous || !state.status?.moments?.event_albums) {
    block.hidden = true;
    return;
  }
  let data;
  try {
    data = await home.getEventAlbums();
  } catch {
    block.hidden = true;             // a convenience, never a blocker
    return;
  }
  const items = data?.suggestions || [];
  block.hidden = items.length === 0;
  box.replaceChildren(...items.map(card));
}
