/**
 * The family app's duplicates review: the banner over the Duplicates view and
 * the screen that shows each group's copies side by side. Loaded when the
 * view is first opened (app.js, openPage).
 *
 * `/api/duplicates/review` joins the two things that say two files are the
 * same photograph: the content fingerprint (exact copies, byte for byte) and
 * the look (near copies: a burst, a resized or re-sent one). Each group comes
 * with the frame worth keeping picked (`db.best_of`: never over a favourite
 * or stars somebody gave), drawn first.
 *
 * An administrator chooses which one to keep and then either hides the
 * others, merges them (each person's favourite and stars, albums and a
 * written caption carried onto the kept one, then hidden) or says they are
 * not duplicates. Nothing here deletes a file: every decision is a change to
 * the index that "Set aside" can put back. A family member may compare, and
 * only ever sees copies they are allowed to see.
 */

import { home, thumbUrl } from '../../api.js';
import * as i18n from '../../i18n.js';
import { state } from '../../store.js';
import { $, el, humanBytes, toast } from '../../ui.js';

/** The gallery around the review (app.js): its viewer and its reload. */
let shell;

const view = {
  kind: 'all',
  groups: [],        // the page of groups loaded
  offset: 0,         // where that page starts in the whole list
  total: 0,          // groups of this kind still to review
  index: 0,          // the group on screen, within `groups`
  keep: null,        // the id chosen to keep in it
  canDecide: false,
  busy: false,
  zoom: false,
  aside: { entries: [], total: 0 },
};

const PAGE = 20;

/* -- the banner ---------------------------------------------------------- */

/** Read how many groups there are and say so (the Duplicates view is open). */
export async function load() {
  await fetchGroups(0);
  renderBanner();
}

function renderBanner(data = view.summary) {
  const banner = $('#duplicates-banner');
  if (!banner) return;
  if (state.view !== 'duplicates' || !data || !(data.counts.exact + data.counts.near)) {
    banner.hidden = true;
    return;
  }
  const count = data.counts.exact + data.counts.near;
  $('#duplicates-headline').textContent = i18n.t(
    count === 1 ? '{count} group of copies to review' : '{count} groups of copies to review',
    { count: count.toLocaleString() })
    + (data.total_wasted ? ` · ${i18n.t('{size} could be freed', { size: humanBytes(data.total_wasted) })}` : '');
  banner.hidden = false;
}

/* -- reading ------------------------------------------------------------- */

async function fetchGroups(offset) {
  try {
    const data = await home.getDuplicatesReview({ query: { kind: view.kind, offset, limit: PAGE } });
    view.groups = data.groups || [];
    view.offset = data.offset || 0;
    view.total = data.total || 0;
    view.canDecide = !!data.can_decide;
    if (view.kind === 'all') view.summary = data;
    $('#dup-count-exact').textContent = data.counts?.exact ? String(data.counts.exact) : '';
    $('#dup-count-near').textContent = data.counts?.near ? String(data.counts.near) : '';
    return data;
  } catch (err) {
    view.groups = [];
    view.total = 0;
    toast(err.message, true);
    return null;
  }
}

/** Show group *at* of the whole list, fetching its page when needed. */
async function go(at) {
  if (view.total && (at < 0 || at >= view.total)) return;
  if (at < view.offset || at >= view.offset + view.groups.length) {
    await fetchGroups(Math.max(0, Math.floor(at / PAGE) * PAGE));
  }
  view.index = Math.max(0, Math.min(at - view.offset, view.groups.length - 1));
  const group = view.groups[view.index];
  view.keep = group ? group.best : null;
  renderGroup();
}

/* -- one group, side by side -------------------------------------------- */

function current() { return view.groups[view.index] || null; }

function kindLabel(kind) {
  return kind === 'exact' ? i18n.t('Exact copies') : i18n.t('Look alike');
}

function when(item) {
  if (item.taken) {
    return new Date(item.taken * 1000).toLocaleString(i18n.locale(),
      { dateStyle: 'medium', timeStyle: 'short' });
  }
  return item.date || '';
}

function detail(label, value, differs) {
  const row = el('div', 'dup-detail' + (differs ? ' differs' : ''));
  row.append(el('dt', '', label), el('dd', '', value));
  return row;
}

function renderGroup() {
  const box = $('#dup-compare');
  const group = current();
  const at = view.offset + view.index;
  box.replaceChildren();
  $('#dup-prev').disabled = at <= 0;
  $('#dup-next').disabled = at >= view.total - 1;
  $('#dup-actions').hidden = !view.canDecide || !group;
  $('#dup-note').textContent = view.canDecide
    ? i18n.t('Nothing is deleted. Hidden copies stay on disk and can be put back.')
    : i18n.t('Only an administrator can hide copies. You can compare them here.');
  if (!group) {
    $('#dup-where').textContent = '';
    $('#dup-group-line').textContent = '';
    box.append(el('p', 'empty-note', i18n.t('No duplicates left to review.')));
    return;
  }
  $('#dup-where').textContent = i18n.t('{at} of {total}',
    { at: (at + 1).toLocaleString(), total: view.total.toLocaleString() });
  const more = group.count > group.items.length
    ? ` · ${i18n.t('{count} more not shown', { count: group.count - group.items.length })}` : '';
  $('#dup-group-line').textContent = `${kindLabel(group.kind)} · `
    + i18n.t('{count} copies', { count: group.count })
    + (group.wasted ? ` · ${i18n.t('{size} to free', { size: group.wasted_h })}` : '') + more;

  // What differs between the copies is what the eye should go to.
  const differs = (key) => new Set(group.items.map((i) => String(i[key] ?? ''))).size > 1;
  const columns = el('div', 'dup-columns');
  columns.style.setProperty('--dup-cols', String(Math.min(group.items.length, 4)));
  group.items.forEach((item, n) => {
    const col = el('article', 'dup-col' + (item.id === view.keep ? ' kept' : ''));
    col.dataset.id = String(item.id);

    const frame = el('div', 'dup-frame');
    const img = document.createElement('img');
    img.alt = item.name || '';
    img.loading = 'lazy';
    img.src = view.zoom && item.kind === 'picture'
      ? `/api/preview/${item.id}` : thumbUrl(item.id, 640, item.thumb_v);
    frame.append(img);
    const badges = el('div', 'dup-badges');
    if (item.id === group.best) badges.append(el('span', 'dup-badge', i18n.t('Best')));
    if (group.kind !== 'exact' && item.same_bytes && item.id !== group.best) {
      badges.append(el('span', 'dup-badge soft', i18n.t('Same bytes as best')));
    }
    if (item.visibility === 'hidden') badges.append(el('span', 'dup-badge warn', i18n.t('Hidden')));
    if (item.explicit) badges.append(el('span', 'dup-badge warn', i18n.t('Explicit')));
    frame.append(badges);
    frame.addEventListener('dblclick', () => {
      $('#duplicates-modal').hidden = true;
      shell.viewer.open(group.items.map((i) => i.id), n);
    });
    col.append(frame);

    const dl = el('dl', 'dup-details');
    dl.append(
      detail(i18n.t('Name'), item.name || '', differs('name')),
      detail(i18n.t('Folder'), item.folder || '/', differs('folder')),
      detail(i18n.t('Taken'), when(item), differs('taken')),
      detail(i18n.t('Size'), `${item.size_h} · ${(item.ext || '').toUpperCase()}`, differs('size')),
      detail(i18n.t('Pixels'), item.width && item.height ? `${item.width} × ${item.height}` : '—',
        differs('width') || differs('height')),
      detail(i18n.t('Sharpness'), item.sharpness == null ? '—' : String(item.sharpness), differs('sharpness')),
    );
    if (item.camera) dl.append(detail(i18n.t('Camera'), item.camera, differs('camera')));
    const marks = [item.favorite ? '♥' : '', item.rating ? '★'.repeat(item.rating) : '',
      item.albums ? i18n.t('{count} albums', { count: item.albums }) : ''].filter(Boolean).join('  ');
    if (marks) dl.append(detail(i18n.t('Yours'), marks, false));
    col.append(dl);

    if (view.canDecide) {
      const pick = el('button', 'btn small dup-keep' + (item.id === view.keep ? ' primary' : ' ghost'),
        item.id === view.keep ? i18n.t('Keeping this') : i18n.t('Keep this'));
      pick.type = 'button';
      pick.setAttribute('aria-pressed', String(item.id === view.keep));
      pick.title = i18n.t('Key {n}', { n: n + 1 });
      pick.onclick = () => choose(item.id);
      col.append(pick);
    }
    columns.append(col);
  });
  box.append(columns);
  if (view.zoom) syncScroll(columns);
}

function choose(id) {
  view.keep = id;
  renderGroup();
}

/** While zoomed in, the frames move together: the same corner of each copy. */
function syncScroll(columns) {
  const frames = [...columns.querySelectorAll('.dup-frame')];
  let leading = null;
  for (const frame of frames) {
    frame.classList.add('zoomed');
    frame.addEventListener('scroll', () => {
      if (leading && leading !== frame) return;
      leading = frame;
      const x = frame.scrollLeft / Math.max(1, frame.scrollWidth - frame.clientWidth);
      const y = frame.scrollTop / Math.max(1, frame.scrollHeight - frame.clientHeight);
      for (const other of frames) {
        if (other === frame) continue;
        other.scrollLeft = x * (other.scrollWidth - other.clientWidth);
        other.scrollTop = y * (other.scrollHeight - other.clientHeight);
      }
      requestAnimationFrame(() => { leading = null; });
    }, { passive: true });
  }
}

/* -- deciding ------------------------------------------------------------ */

async function decide(action) {
  const group = current();
  if (!group || !view.canDecide || view.busy) return;
  const others = group.items.map((i) => i.id).filter((id) => id !== view.keep);
  if (!others.length) return;
  view.busy = true;
  try {
    const done = await home.postDuplicatesReviewDecide({ body: { keep: view.keep, others, action } });
    const carried = done.carried || {};
    const said = action === 'keep_all'
      ? i18n.t('Kept them all: not duplicates.')
      : action === 'merge'
        ? i18n.t('Merged into the kept one: {count} hidden.', { count: others.length })
          + (carried.albums ? ` ${i18n.t('{count} albums carried.', { count: carried.albums })}` : '')
        : i18n.t('{count} hidden. They stay on disk.', { count: others.length });
    toast(said);
    // The group is done: the list moves up under it.
    const at = view.offset + view.index;
    await fetchGroups(view.offset);
    if (view.kind !== 'all') {
      const all = await home.getDuplicatesReview({ query: { kind: 'all', limit: 1 } }).catch(() => null);
      if (all) view.summary = all;
    }
    renderBanner();
    await go(Math.min(at, Math.max(0, view.total - 1)));
    shell.reload?.();
  } catch (err) {
    toast(err.message, true);
  } finally {
    view.busy = false;
  }
}

/* -- set aside ----------------------------------------------------------- */

async function loadAside(offset = 0) {
  try {
    const data = await home.getDuplicatesReviewSetAside({ query: { offset, limit: 30 } });
    view.aside.entries = offset ? [...view.aside.entries, ...data.entries] : data.entries;
    view.aside.total = data.total;
  } catch (err) {
    toast(err.message, true);
  }
  renderAside();
}

function tile(item, kept) {
  const cell = el('figure', 'dup-aside-tile' + (kept ? ' kept' : ''));
  if (item) {
    const img = document.createElement('img');
    img.loading = 'lazy';
    img.alt = item.name || '';
    img.src = thumbUrl(item.id, 256, item.thumb_v);
    cell.append(img, el('figcaption', '', kept ? i18n.t('Kept') : item.size_h || ''));
    cell.title = `${item.folder || ''}/${item.name || ''}`;
  }
  return cell;
}

function renderAside() {
  const box = $('#dup-aside-list');
  box.replaceChildren();
  if (!view.aside.entries.length) {
    box.append(el('p', 'empty-note', i18n.t('Nothing set aside.')));
  }
  for (const entry of view.aside.entries) {
    const row = el('div', 'dup-aside-row');
    const tiles = el('div', 'dup-aside-tiles');
    tiles.append(tile(entry.keeper_item, true), ...entry.items.map((i) => tile(i, false)));
    const info = el('div', 'dup-aside-info');
    info.append(
      el('span', 'hint', entry.action === 'merged' ? i18n.t('Merged') : i18n.t('Hidden')),
      el('span', 'hint', entry.decided_at
        ? new Date(entry.decided_at * 1000).toLocaleDateString() : ''),
    );
    const back = el('button', 'btn ghost small', i18n.t('Put back'));
    back.type = 'button';
    back.onclick = async () => {
      back.disabled = true;
      try {
        const done = await home.postDuplicatesReviewPutBack({ body: { batch: entry.batch } });
        toast(i18n.t('{count} shown again.', { count: done.shown }));
        await loadAside(0);
        await fetchGroups(0);
        renderBanner();
        shell.reload?.();
      } catch (err) {
        back.disabled = false;
        toast(err.message, true);
      }
    };
    row.append(tiles, info, back);
    box.append(row);
  }
  $('#dup-aside-more').hidden = view.aside.entries.length >= view.aside.total;
}

/* -- wiring -------------------------------------------------------------- */

function showTab(name) {
  for (const tab of document.querySelectorAll('[data-dup-tab]')) {
    const on = tab.dataset.dupTab === name;
    tab.classList.toggle('active', on);
    tab.setAttribute('aria-selected', String(on));
  }
  $('#dup-review-pane').hidden = name !== 'review';
  $('#dup-aside-pane').hidden = name !== 'aside';
  if (name === 'aside') loadAside(0);
}

async function open() {
  $('#duplicates-modal').hidden = false;
  showTab('review');
  await fetchGroups(0);
  $('[data-dup-tab="aside"]').hidden = !view.canDecide;
  await go(0);
}

function onKey(event) {
  const modal = $('#duplicates-modal');
  if (!modal || modal.hidden || $('#dup-review-pane').hidden) return;
  if (event.target.closest('input, textarea, select, [contenteditable="true"]')) return;
  if (event.metaKey || event.ctrlKey || event.altKey) return;
  const at = view.offset + view.index;
  const group = current();
  const key = event.key.toLowerCase();
  if (key === 'arrowright') go(at + 1);
  else if (key === 'arrowleft') go(at - 1);
  else if (/^[1-9]$/.test(key) && group?.items[Number(key) - 1] && view.canDecide) {
    choose(group.items[Number(key) - 1].id);
  } else if (key === 'm') decide('merge');
  else if (key === 'h') decide('hide');
  else if (key === 'n') decide('keep_all');
  else return;
  event.preventDefault();
  event.stopPropagation();
}

/** Bind the review: once, when it is first opened. */
export function wire(context) {
  shell = context;
  if (!$('#duplicates-banner')) return;

  $('#duplicates-review-btn')?.addEventListener('click', open);
  $('#duplicates-close-x')?.addEventListener('click', () => { $('#duplicates-modal').hidden = true; });
  for (const tab of document.querySelectorAll('[data-dup-tab]')) {
    tab.addEventListener('click', () => showTab(tab.dataset.dupTab));
  }
  for (const chip of document.querySelectorAll('[data-dup-kind]')) {
    chip.addEventListener('click', async () => {
      view.kind = chip.dataset.dupKind;
      for (const other of document.querySelectorAll('[data-dup-kind]')) {
        other.classList.toggle('active', other === chip);
      }
      await fetchGroups(0);
      await go(0);
    });
  }
  $('#dup-prev').addEventListener('click', () => go(view.offset + view.index - 1));
  $('#dup-next').addEventListener('click', () => go(view.offset + view.index + 1));
  $('#dup-zoom').addEventListener('change', (event) => {
    view.zoom = event.target.checked;
    renderGroup();
  });
  $('#dup-merge').addEventListener('click', () => decide('merge'));
  $('#dup-hide').addEventListener('click', () => decide('hide'));
  $('#dup-not').addEventListener('click', () => decide('keep_all'));
  $('#dup-aside-more').addEventListener('click', () => loadAside(view.aside.entries.length));
  document.addEventListener('keydown', onKey, true);
}
