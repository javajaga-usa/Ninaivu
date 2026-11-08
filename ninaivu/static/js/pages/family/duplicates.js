/**
 * The family app's duplicates review: the banner over the Duplicates view and
 * the dialog that keeps the best of each group. Loaded when the view is first
 * opened (app.js, openPage).
 */

import { api, thumbUrl } from '../../api.js';
import * as i18n from '../../i18n.js';
import { state } from '../../store.js';
import { $, humanBytes } from '../../ui.js';

/* -- duplicates review ---------------------------------------------------
 *
 * `/api/duplicates` already groups near-identical shots (a burst, or the same
 * photo saved twice) by perceptual hash and picks the sharpest, highest-
 * resolution one as `best` — never overriding a favourite or a rating someone
 * already gave it (see `db.best_of`). Nothing consumed that pick before this:
 * the sidebar's Duplicates view just filtered the ordinary grid to items that
 * have a `dup_group`, flat and unranked. This turns that existing judgment
 * into something a person can act on, without changing what the plain filter
 * view does for anyone who liked it as it was.
 */

/** The gallery around the review (app.js): its viewer, and its delete. */
let shell;

let duplicateGroupsCache = null;

/** Read the groups and say how many there are (the Duplicates view is open). */
export async function load() {
  try {
    const data = await api.duplicates();
    duplicateGroupsCache = data.groups || [];
  } catch {
    duplicateGroupsCache = [];
  }
  renderDuplicatesSummary();
}

function renderDuplicatesSummary() {
  const banner = $('#duplicates-banner');
  if (!banner) return;
  const groups = duplicateGroupsCache || [];
  if (state.view !== 'duplicates' || !groups.length) {
    banner.hidden = true;
    return;
  }
  const wasted = groups.reduce((sum, g) => sum + (g.wasted || 0), 0);
  $('#duplicates-headline').textContent =
    `${groups.length} ${groups.length === 1 ? 'group' : 'groups'} of similar shots`
    + (wasted ? ` · ${humanBytes(wasted)} could be freed` : '');
  banner.hidden = false;
}

function renderDuplicateGroups() {
  const box = $('#duplicates-groups');
  if (!box) return;
  const groups = duplicateGroupsCache || [];
  box.replaceChildren();
  if (!groups.length) {
    const empty = document.createElement('p');
    empty.className = 'empty-note';
    empty.textContent = i18n.t('No duplicate groups right now.');
    box.appendChild(empty);
    return;
  }
  for (const group of groups) {
    const wrap = document.createElement('div');
    wrap.className = 'dup-group';

    const head = document.createElement('div');
    head.className = 'dup-group-head';
    const info = document.createElement('span');
    info.className = 'hint';
    const count = group.items.length;
    info.innerHTML = `<b>${count}</b> versions of this shot`
      + (group.wasted_h ? ` &middot; <b>${group.wasted_h}</b> to free` : '');
    const keepBtn = document.createElement('button');
    keepBtn.className = 'btn ghost small';
    keepBtn.type = 'button';
    keepBtn.textContent = i18n.t('Keep only the best');
    keepBtn.onclick = () => {
      const rest = group.items.filter((i) => i.id !== group.best).map((i) => i.id);
      if (!rest.length) return;
      shell.deleteSelected(rest, () => {
        duplicateGroupsCache = (duplicateGroupsCache || [])
          .filter((g) => g.group !== group.group);
        renderDuplicateGroups();
        renderDuplicatesSummary();
      });
    };
    head.append(info, keepBtn);

    const thumbs = document.createElement('div');
    thumbs.className = 'dup-thumbs';
    group.items.forEach((item, index) => {
      const cell = document.createElement('div');
      cell.className = 'dup-item' + (item.id === group.best ? ' best' : '');
      const img = document.createElement('img');
      img.src = thumbUrl(item.id, 220, item.thumb_v);
      img.loading = 'lazy';
      img.alt = item.name || '';
      cell.appendChild(img);
      if (item.id === group.best) {
        const badge = document.createElement('span');
        badge.className = 'dup-badge';
        badge.textContent = i18n.t('Best');
        cell.appendChild(badge);
      }
      const meta = document.createElement('span');
      meta.className = 'dup-item-meta';
      const dims = item.width && item.height ? `${item.width}×${item.height} · ` : '';
      meta.textContent = `${dims}${item.size_h || ''}`;
      cell.appendChild(meta);
      cell.title = item.name || '';
      cell.onclick = () => {
        $('#duplicates-modal').hidden = true;
        shell.viewer.open(group.items.map((i) => i.id), index);
      };
      thumbs.appendChild(cell);
    });

    wrap.append(head, thumbs);
    box.appendChild(wrap);
  }
}

/** Bind the review: once, when it is first opened. */
export function wire(context) {
  shell = context;
  const banner = $('#duplicates-banner');
  if (!banner) return;

  $('#duplicates-review-btn')?.addEventListener('click', () => {
    renderDuplicateGroups();
    $('#duplicates-modal').hidden = false;
  });
  $('#duplicates-close')?.addEventListener('click', () => {
    $('#duplicates-modal').hidden = true;
  });
  $('#duplicates-close-x')?.addEventListener('click', () => {
    $('#duplicates-modal').hidden = true;
  });
  $('#duplicates-keep-all-best')?.addEventListener('click', () => {
    const groups = duplicateGroupsCache || [];
    const rest = groups.flatMap(
      (g) => g.items.filter((i) => i.id !== g.best).map((i) => i.id),
    );
    if (!rest.length) return;
    shell.deleteSelected(rest, () => {
      duplicateGroupsCache = [];
      renderDuplicateGroups();
      renderDuplicatesSummary();
      $('#duplicates-modal').hidden = true;
    });
  });
}
