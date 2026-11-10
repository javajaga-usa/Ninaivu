/**
 * The gallery's sidebar and filter bar (app.js): the chips of what is
 * filtered, people, occasions, albums, tags, years and folders, and the
 * buttons over a filtered gallery (a book, a film, a collage, a life story).
 * Always on screen, so imported with the gallery, not fetched later; a
 * module of its own so app.js is the gallery's wiring and not everything.
 *
 * What it needs of the gallery around it comes through connectSidebar().
 */

import { api, thumbUrl } from '../api.js';
import * as i18n from '../i18n.js';
import { state } from '../store.js';
import { $, toast } from '../ui.js';

/** The gallery around the sidebar (app.js), given once at start. */
let ctx;

/** Given by app.js before anything here is drawn. */
export function connectSidebar(context) {
  ctx = context;
}

const reload = (options) => ctx.reload(options);
const sidebarAsk = (ask) => ctx.sidebarAsk(ask);
const openItems = (ids, index) => ctx.openItems(ids, index);
const openPage = (name) => ctx.openPage(name);
const openAskFamily = (...args) => ctx.features.openAskFamily(...args);
const openFamilyTree = (...args) => ctx.features.openFamilyTree(...args);
const openBookSheet = (...args) => ctx.features.openBookSheet(...args);
const openBooksList = (...args) => ctx.features.openBooksList(...args);
const filmsAvailable = (...args) => ctx.features.filmsAvailable(...args);
const openFilmSheet = (...args) => ctx.features.openFilmSheet(...args);
const openLifeStory = (...args) => ctx.features.openLifeStory(...args);

/* -- filter bar --------------------------------------------------------- */

export function renderFilterBar() {
  const bar = $('#active-filters');
  bar.innerHTML = '';
  const chips = [];
  if (state.filters.q) chips.push([i18n.t('Search'), state.filters.q, () => {
    state.filters.q = '';
    $('#search').value = '';
    $('#clear-search').hidden = true;
  }]);
  if (state.filters.tag) chips.push([i18n.t('Tag'), state.filters.tag, () => (state.filters.tag = '')]);
  if (state.filters.folder) chips.push([i18n.t('Folder'), state.filters.folder, () => (state.filters.folder = '')]);
  if (state.filters.camera) chips.push([i18n.t('Camera'), state.filters.camera, () => (state.filters.camera = '')]);
  if (state.filters.person) {
    const person = (state.people || []).find((p) => p.id === state.filters.person);
    chips.push([i18n.t('Person'), person?.name || i18n.t('Someone'), () => (state.filters.person = 0)]);
  }
  if (state.filters.near) chips.push([i18n.t('Taken near'), state.nearLabel || i18n.t('this photo'), () => {
    state.filters.near = 0;
    state.nearLabel = '';
  }]);
  if (state.filters.from) chips.push([i18n.t('Year'), state.filters.from.slice(0, 4), () => {
    state.filters.from = '';
    state.filters.to = '';
  }]);
  if (state.filters.occasion) {
    const trip = (state.occasions || []).find((o) => o.id === state.filters.occasion);
    chips.push([i18n.t('Trip'), (trip && occasionTitle(trip)) || i18n.t('Trip'), () => (state.filters.occasion = 0)]);
  }
  if (state.filters.album) {
    const album = (state.albums || []).find((a) => a.id === state.filters.album);
    chips.push([i18n.t('Album'), album?.name || i18n.t('Album'), () => (state.filters.album = 0)]);
  }

  for (const [label, value, clear] of chips) {
    const pill = document.createElement('span');
    pill.className = 'pill';
    const b = document.createElement('b');
    b.textContent = `${label}: `;
    const text = document.createElement('span');
    text.textContent = value;
    const button = document.createElement('button');
    button.type = 'button';
    button.setAttribute('aria-label', i18n.t('Remove the {label} filter', { label }));
    button.innerHTML = '<svg viewBox="0 0 24 24"><path d="M18 6 6 18M6 6l12 12"/></svg>';
    button.onclick = () => {
      clear();
      syncChips();
      reload({ resetScroll: true });
    };
    pill.append(b, text, button);
    bar.appendChild(pill);
  }

  if (state.filters.album) {
    const album = (state.albums || []).find((a) => a.id === state.filters.album);
    if (album) {
      const shareBtn = document.createElement('button');
      shareBtn.className = 'btn ghost small';
      shareBtn.innerHTML = '<svg viewBox="0 0 24 24" style="width:13px;height:13px;margin-right:4px;vertical-align:-2px;"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/></svg>Share Album';
      shareBtn.onclick = () => openPage('sharing')
        .then((page) => page?.openShareModal({ type: 'album', id: album.id, name: album.name }));
      bar.appendChild(shareBtn);

      const delBtn = document.createElement('button');
      delBtn.className = 'btn ghost small';
      delBtn.style.color = 'var(--danger)';
      delBtn.textContent = i18n.t('Delete Album');
      delBtn.onclick = async () => {
        if (!confirm(i18n.t('Delete album "{name}"? Photographs inside will not be deleted.', { name: album.name }))) return;
        try {
          await api.deleteAlbum(album.id);
          toast(i18n.t('Album "{name}" deleted.', { name: album.name }));
          state.filters.album = 0;
          await loadAlbums();
          syncChips();
          reload({ resetScroll: true });
        } catch (err) {
          toast(i18n.t('Could not delete album: {reason}', { reason: err.message }), true);
        }
      };
      bar.appendChild(delBtn);
    }
  }

  bookButton(bar);
  $('#filter-bar').hidden = chips.length === 0;
}

/** "Make a book" beside an album, a trip or a person (static/js/books.js).
 *  Not for guests, who may look but not take copies away. */
function bookButton(bar) {
  if (!state.user?.role || state.user.role === 'guest') return;
  const f = state.filters;
  let from = null;
  if (f.album) {
    const album = (state.albums || []).find((a) => a.id === f.album);
    from = { kind: 'album', id: f.album, name: album?.name || '' };
  } else if (f.occasion) {
    const trip = (state.occasions || []).find((o) => o.id === f.occasion);
    from = { kind: 'occasion', id: f.occasion, name: trip?.place || '', place: trip?.place || '',
             started: trip?.started_at, ended: trip?.ended_at };
  } else if (f.person) {
    const person = (state.people || []).find((p) => p.id === f.person);
    from = { kind: 'person', id: f.person, name: person?.name || '' };
  }
  if (!from) return;
  // A person's life, decade by decade (static/js/life-story.js), for the
  // family while life_story is on.
  if (from.kind === 'person' && state.status?.moments?.life && !state.user?.anonymous
      && ['family', 'admin'].includes(state.user?.role)) {
    const life = document.createElement('button');
    life.type = 'button';
    life.className = 'btn ghost small';
    life.textContent = i18n.t('Life story');
    life.onclick = () => openLifeStory({ toast, openItems, person: from.id });
    bar.appendChild(life);
  }
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'btn ghost small';
  button.textContent = i18n.t('Make a book');
  button.onclick = () => openBookSheet(from, { toast });
  bar.appendChild(button);
  if (state.user?.can?.download) {
    const collage = document.createElement('button');
    collage.type = 'button';
    collage.className = 'btn ghost small';
    collage.textContent = i18n.t('Make a collage');
    collage.onclick = () => openStudioFor([], { filters: { [from.kind]: from.id }, returnFocus: collage });
    bar.appendChild(collage);
  }
  filmsAvailable().then((on) => {
    if (!on || !button.isConnected) return;
    const film = document.createElement('button');
    film.type = 'button';
    film.className = 'btn ghost small';
    film.textContent = i18n.t('Make a film');
    film.onclick = () => openFilmSheet(from, { toast });
    button.after(film);
  });
}

/** Creative Studio with photographs of the library: a selection, the photo in
 *  the viewer, or the first of an album, a trip or a person (`filters`).
 *  With none it opens at its own library picker. */
export async function openStudioFor(ids, { filters = null, returnFocus = document.activeElement, sourceId = null } = {}) {
  try {
    const { openCreativeStudio } = await import('./creative-studio.js');
    await openCreativeStudio({ ids, filters, returnFocus, sourceId, canSave: !!sourceId && !!state.user?.can?.rotate,
                               onSaved: () => reload() });
  } catch {
    toast(i18n.t('Creative Studio could not open. Please refresh and try again.'));
  }
}

export function clearFilters() {
  ctx.stopSearch();
  state.filters = { ...state.filters, q: '', tag: '', folder: '', camera: '', from: '', to: '', person: 0,
                    occasion: 0, album: 0, near: 0 };
  $('#suggestions').hidden = true;
  $('#search').value = '';
  $('#clear-search').hidden = true;
  syncChips();
  reload({ resetScroll: true });
}

export function syncChips() {
  document.querySelectorAll('#tag-cloud .chip').forEach((chip) => {
    chip.classList.toggle('active', chip.dataset.tag === state.filters.tag);
  });
  document.querySelectorAll('#year-list .chip').forEach((chip) => {
    chip.classList.toggle('active', chip.dataset.year === state.filters.from.slice(0, 4));
  });
  document.querySelectorAll('#folder-list button').forEach((button) => {
    button.classList.toggle('active', button.dataset.folder === state.filters.folder);
  });
  document.querySelectorAll('#people-row .person-chip').forEach((button) => {
    button.classList.toggle('active',
      Number(button.dataset.person) === state.filters.person);
  });
  document.querySelectorAll('#trip-list .trip-card').forEach((button) => {
    button.classList.toggle('active',
      Number(button.dataset.occasion) === state.filters.occasion);
  });
  document.querySelectorAll('#album-list .album-card').forEach((button) => {
    button.classList.toggle('active',
      Number(button.dataset.album) === state.filters.album);
  });
  const removeBtn = $('#sel-album-remove');
  if (removeBtn) {
    removeBtn.hidden = !state.filters.album;
  }
}

/* People.

   The list comes from the server already narrowed to this viewer: somebody
   who only appears in photographs they may not open is not in it at all. So
   there is nothing to filter here, and nothing that could be revealed by
   comparing what two people see. */
/** How many rows of faces show before "Show all". */
const PEOPLE_ROWS = 2;

function renderPeople() {
  const box = $('#people-row');
  const people = state.people || [];
  // Asking the family and the tree are for family members and administrators.
  const family = !state.user?.anonymous && ['family', 'admin'].includes(state.user?.role);
  $('#people-block').hidden = people.length === 0 && !family;
  const ask = $('#ask-family-btn');
  if (ask) {
    ask.hidden = !family;
    ask.onclick = () => openAskFamily({ toast });
  }
  const tree = $('#family-tree-btn');
  if (tree) {
    tree.hidden = !family || people.length === 0;
    tree.onclick = () => openFamilyTree({ toast, focus: state.filters.person, onOpen: showPerson });
  }
  if (!box) return;
  box.innerHTML = '';
  // Two full rows of faces, then "Show all": a household of forty would
  // otherwise push albums and trips off the bottom of the sidebar. Full rows
  // whatever the width, so it is counted rather than assumed: four columns in
  // the sidebar, fewer on a narrow screen or a zoomed page.
  const columns = peopleColumns(box);
  box.dataset.columns = String(columns);
  watchPeopleWidth(box);
  const room = columns * PEOPLE_ROWS;
  const folds = people.length > room;
  let shown = folds && !state.peopleExpanded ? people.slice(0, room) : people;
  // Whoever the gallery is filtered by stays in view, folded or not.
  const chosen = people.find((p) => p.id === state.filters.person);
  if (chosen && !shown.includes(chosen)) shown = [...shown.slice(0, room - 1), chosen];
  for (const person of shown) box.appendChild(personButton(person));
  if (folds) {
    const more = document.createElement('button');
    more.type = 'button';
    more.className = 'people-more';
    more.textContent = state.peopleExpanded
      ? i18n.t('Show fewer')
      : i18n.t('Show all ({count})', { count: people.length });
    more.onclick = () => {
      state.peopleExpanded = !state.peopleExpanded;
      renderPeople();
    };
    box.appendChild(more);
  }
  syncChips();
}

/** Columns the people grid has now: the sidebar's four, or fewer on a narrow
 *  or zoomed page. Not laid out (a closed sidebar on a phone), the browser
 *  answers with the rule itself, and four is what it will be once it opens. */
function peopleColumns(box) {
  const template = getComputedStyle(box).gridTemplateColumns;
  if (!template || template === 'none' || template.includes('repeat')) return 4;
  return Math.max(1, template.split(' ').length);
}

// Folded to two full rows, so a change in width that changes the columns
// folds it again rather than leaving a row half empty. Watched from the first
// time the list is drawn, when the element is certainly there.
let peopleResize = null;
function watchPeopleWidth(box) {
  if (peopleResize || typeof ResizeObserver === 'undefined') return;
  peopleResize = new ResizeObserver(() => {
    if (box.childElementCount && Number(box.dataset.columns) !== peopleColumns(box)) {
      renderPeople();
    }
  });
  peopleResize.observe(box);
}

/** One person: a face in a circle, the name centred under it. */
function personButton(person) {
  const button = document.createElement('button');
  button.className = 'person-chip';
  button.type = 'button';
  button.dataset.person = String(person.id);
  button.title = `${person.name} — ${i18n.t('{count} photographs',
    { count: Number(person.photo_count || 0).toLocaleString() })}`;
  button.appendChild(person.cover_face_id ? faceImage(person) : initialsFor(person.name));
  const label = document.createElement('span');
  label.textContent = person.name;
  button.appendChild(label);
  button.onclick = () => {
    const id = Number(person.id);
    if (state.filters.person === id) {
      state.filters.person = 0;                       // the same face again: let go of it
      syncChips();
      reload({ resetScroll: true });
    } else {
      showPerson(id);
    }
  };
  return button;
}

/** Somebody's photographs, from their face here or from the family tree. */
export function showPerson(id) {
  // Their photographs, not the overlap of them with whatever else was
  // switched on: a typed search, a folder, or a view of only videos would
  // mostly leave nothing to see. The sort order is the person's own to keep.
  ctx.stopSearch();
  state.filters = { ...state.filters, q: '', tag: '', folder: '', camera: '', from: '', to: '',
                    occasion: 0, album: 0, near: 0, person: Number(id) };
  state.view = 'all';
  document.querySelectorAll('[data-view]').forEach(
    (b) => b.classList.toggle('active', b.dataset.view === 'all'));
  $('#suggestions').hidden = true;
  $('#search').value = '';
  $('#clear-search').hidden = true;
  syncChips();
  reload({ resetScroll: true });
}

function faceImage(person) {
  const img = document.createElement('img');
  img.className = 'face';
  img.src = `/api/faces/thumb/${person.cover_face_id}`;
  img.alt = '';
  img.loading = 'lazy';
  img.decoding = 'async';
  // A crop that will not load becomes the initials, not a broken circle.
  img.onerror = () => img.replaceWith(initialsFor(person.name));
  return img;
}

/** Initials on a colour of their own, for somebody with no face to show. */
function initialsFor(name) {
  const face = document.createElement('div');
  face.className = 'face initials';
  const words = String(name || '?').trim().split(/\s+/).filter(Boolean);
  face.textContent = words.slice(0, 2).map((word) => Array.from(word)[0]).join('').toUpperCase() || '?';
  let hash = 0;
  for (const ch of String(name || '')) hash = (hash * 31 + ch.codePointAt(0)) >>> 0;
  face.style.backgroundColor = `hsl(${hash % 360} 45% 46%)`;
  return face;
}

export async function loadPeople() {
  // Faces are an addition to the gallery, never a requirement for it. If the
  // endpoint is unavailable the row simply does not appear.
  const data = await sidebarAsk(() => api.people());
  if (data === undefined) return;
  state.people = data?.people || [];
  renderPeople();
}

/* Trips — the auto-albums the scanner already groups the library into (see
   ninaivu/media/occasions.py and db.rebuild_occasions). Read-only and
   derived, so there is nothing to manage here: just a way in. */
/** An occasion's title in the reader's language: its place, and its dates
 *  written by the page rather than the server's English. */
export function occasionTitle(trip) {
  if (!trip.started_at) return trip.title || '';
  const dates = i18n.dateRange(trip.started_at, trip.ended_at || trip.started_at);
  if (!dates) return trip.title || '';
  return trip.place ? `${trip.place}, ${dates}` : dates;
}

export function renderOccasions() {
  const box = $('#trip-list');
  const trips = state.occasions || [];
  $('#trips-block').hidden = trips.length === 0;
  if (!box) return;
  box.innerHTML = '';
  for (const trip of trips.slice(0, 40)) {
    const button = document.createElement('button');
    button.className = 'trip-card';
    button.type = 'button';
    button.dataset.occasion = String(trip.id);
    const tripTitle = occasionTitle(trip);
    button.title = `${tripTitle} — ${i18n.items(trip.count)}`;

    if (trip.cover_id) {
      const img = document.createElement('img');
      img.src = thumbUrl(trip.cover_id, 80);
      img.alt = '';
      img.loading = 'lazy';
      button.appendChild(img);
    }
    const text = document.createElement('span');
    text.className = 'trip-text';
    const title = document.createElement('b');
    title.textContent = tripTitle;
    const n = document.createElement('span');
    n.className = 'n';
    n.textContent = i18n.items(trip.count);
    text.append(title, n);
    button.appendChild(text);

    button.onclick = () => {
      state.filters.occasion = state.filters.occasion === trip.id ? 0 : trip.id;
      syncChips();
      reload({ resetScroll: true });
    };
    box.appendChild(button);
  }
  syncChips();
}

export async function loadOccasions() {
  // Same fallback as people: an auto-album feed that fails to load must
  // never take the rest of the gallery down with it.
  const data = await sidebarAsk(() => api.occasions());
  if (data === undefined) return;
  state.occasions = data?.occasions || [];
  renderOccasions();
}

let albumTargetIds = [];

export function renderAlbums() {
  const box = $('#album-list');
  const albums = state.albums || [];
  const block = $('#albums-block');
  if (block) block.hidden = false;
  if (!box) return;
  box.innerHTML = '';
  if (albums.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'hint';
    empty.style.padding = '6px 8px';
    empty.style.fontSize = '12px';
    empty.textContent = i18n.t('No albums yet. Click + to create one.');
    box.appendChild(empty);
    return;
  }
  for (const album of albums) {
    const button = document.createElement('button');
    button.className = 'album-card';
    button.type = 'button';
    button.dataset.album = String(album.id);
    button.title = `${album.name} — ${i18n.items(album.n)}`;

    if (album.cover_id) {
      const img = document.createElement('img');
      img.src = thumbUrl(album.cover_id, 80);
      img.alt = '';
      img.loading = 'lazy';
      button.appendChild(img);
    } else {
      const ph = document.createElement('div');
      ph.className = 'album-icon-ph';
      ph.innerHTML = '<svg viewBox="0 0 24 24"><rect width="18" height="18" x="3" y="3" rx="2"/><path d="M3 9h18M9 21V9"/></svg>';
      button.appendChild(ph);
    }

    const text = document.createElement('span');
    text.className = 'album-text';
    const title = document.createElement('b');
    title.textContent = album.name;
    const n = document.createElement('span');
    n.className = 'n';
    n.textContent = i18n.items(album.n);
    text.append(title, n);
    button.appendChild(text);

    button.onclick = () => {
      state.filters.album = state.filters.album === album.id ? 0 : album.id;
      syncChips();
      reload({ resetScroll: true });
    };
    box.appendChild(button);
  }
  syncChips();
}

export async function loadAlbums() {
  const data = await sidebarAsk(() => api.albums());
  if (data === undefined) return;
  state.albums = data?.albums || [];
  renderAlbums();
}

export function openAlbumModal(ids = []) {
  albumTargetIds = ids;
  const modal = $('#album-modal');
  const title = $('#album-modal-title');
  const subtitle = $('#album-modal-subtitle');
  const input = $('#album-new-name');
  input.value = '';

  if (ids.length > 0) {
    title.textContent = i18n.t('Add {items} to an album', { items: i18n.items(ids.length) });
    subtitle.textContent = i18n.t('Choose an album or enter a name to create a new one:');
  } else {
    title.textContent = i18n.t('Create New Album');
    subtitle.textContent = i18n.t('Enter a name for the new album:');
  }

  const list = $('#album-modal-list');
  list.innerHTML = '';
  const albums = state.albums || [];
  if (albums.length === 0) {
    list.innerHTML = '<div class="hint" style="padding:8px;">No existing albums yet.</div>';
  } else {
    for (const album of albums) {
      const row = document.createElement('button');
      row.className = 'album-modal-item';
      row.type = 'button';

      if (album.cover_id) {
        const img = document.createElement('img');
        img.src = thumbUrl(album.cover_id, 64);
        img.alt = '';
        row.appendChild(img);
      } else {
        const ph = document.createElement('div');
        ph.className = 'album-icon-ph';
        ph.innerHTML = '<svg viewBox="0 0 24 24"><rect width="18" height="18" x="3" y="3" rx="2"/><path d="M3 9h18M9 21V9"/></svg>';
        row.appendChild(ph);
      }

      const text = document.createElement('div');
      text.className = 'album-text';
      const b = document.createElement('b');
      b.textContent = album.name;
      const n = document.createElement('span');
      n.className = 'n';
      n.textContent = i18n.items(album.n);
      text.append(b, n);
      row.appendChild(text);

      row.onclick = async () => {
        if (albumTargetIds.length > 0) {
          try {
            await api.albumAdd(album.id, albumTargetIds);
            toast(i18n.t('Added {items} to "{album}".', { items: i18n.items(albumTargetIds.length), album: album.name }));
            modal.hidden = true;
            ctx.grid().clearSelection();
            await loadAlbums();
            if (state.filters.album === album.id) reload();
          } catch (err) {
            toast(i18n.t('Could not add to album: {reason}', { reason: err.message }), true);
          }
        } else {
          modal.hidden = true;
          state.filters.album = album.id;
          syncChips();
          reload({ resetScroll: true });
        }
      };
      list.appendChild(row);
    }
  }

  modal.hidden = false;
  setTimeout(() => input.focus(), 50);
}

export function wireAlbums() {
  $('#books-btn')?.addEventListener('click', () => openBooksList({ toast }));
  $('#new-album-btn')?.addEventListener('click', () => {
    openAlbumModal([]);
  });

  $('#album-create-btn')?.addEventListener('click', async () => {
    const input = $('#album-new-name');
    const name = input.value.trim();
    if (!name) {
      toast(i18n.t('Please enter an album name.'), true);
      input.focus();
      return;
    }
    const button = $('#album-create-btn');
    button.disabled = true;
    try {
      const created = await api.createAlbum(name, albumTargetIds);
      toast(albumTargetIds.length
        ? i18n.t('Album "{name}" made, with {items}.', { name, items: i18n.items(albumTargetIds.length) })
        : i18n.t('Album "{name}" made.', { name }));
      $('#album-modal').hidden = true;
      if (albumTargetIds.length) ctx.grid().clearSelection();
      await loadAlbums();
      state.filters.album = created.id;
      syncChips();
      reload({ resetScroll: true });
    } catch (err) {
      toast(i18n.t('Could not create album: {reason}', { reason: err.message }), true);
    } finally {
      button.disabled = false;
    }
  });

  $('#album-new-name')?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      $('#album-create-btn')?.click();
    }
  });

  $('#album-modal-close')?.addEventListener('click', () => {
    $('#album-modal').hidden = true;
  });
  $('#album-modal-close-x')?.addEventListener('click', () => {
    $('#album-modal').hidden = true;
  });

  ctx.viewer().addEventListener('album', (e) => {
    if (e.detail?.item) {
      openAlbumModal([e.detail.item.id]);
    }
  });
}

export function renderTagCloud() {
  const box = $('#tag-cloud');
  const tags = state.facets?.tags || [];
  $('#tags-block').hidden = tags.length === 0;
  box.innerHTML = '';
  for (const tag of tags.slice(0, 28)) {
    const chip = document.createElement('button');
    chip.className = 'chip';
    chip.type = 'button';
    chip.dataset.tag = tag.name;
    chip.textContent = tag.name;
    const n = document.createElement('span');
    n.className = 'n';
    n.textContent = tag.count;
    chip.appendChild(n);
    chip.onclick = () => {
      state.filters.tag = state.filters.tag === tag.name ? '' : tag.name;
      syncChips();
      reload({ resetScroll: true });
    };
    box.appendChild(chip);
  }
  syncChips();
}

export function renderYears() {
  const box = $('#year-list');
  const years = state.facets?.years || [];
  $('#years-block').hidden = years.length < 2;
  box.innerHTML = '';
  for (const year of years) {
    const chip = document.createElement('button');
    chip.className = 'chip';
    chip.type = 'button';
    chip.dataset.year = year.year;
    chip.textContent = year.year;
    const n = document.createElement('span');
    n.className = 'n';
    n.textContent = year.count;
    chip.appendChild(n);
    chip.onclick = () => {
      const active = state.filters.from.slice(0, 4) === year.year;
      state.filters.from = active ? '' : `${year.year}-01-01`;
      state.filters.to = active ? '' : `${year.year}-12-31`;
      syncChips();
      reload({ resetScroll: true });
    };
    box.appendChild(chip);
  }
}

export function renderFolders() {
  const box = $('#folder-list');
  const folders = (state.facets?.folders || []).filter((f) => f.name);
  $('#folders-block').hidden = folders.length < 2;
  box.innerHTML = '';
  for (const folder of folders.slice(0, 40)) {
    const button = document.createElement('button');
    button.type = 'button';
    button.dataset.folder = folder.name;
    // The name shown is for reading; `dataset.folder` above, and the filter
    // below, keep the path exactly as the server gave it.
    const path = document.createElement('span');
    path.className = 'path';
    path.title = folder.name;
    const asDate = i18n.folderDate(folder.name);
    const cut = folder.name.replace(/\/+$/, '').lastIndexOf('/');
    if (asDate) {
      // "2025/05/11" is a day, and reads as one: "11 May 2025".
      path.textContent = asDate;
    } else if (cut > 0) {
      // A deep path: its last folder is the one that says what is in it, so
      // that is the part kept whole; the folders above it give way first.
      const parent = document.createElement('span');
      parent.className = 'parent';
      parent.textContent = folder.name.slice(0, cut + 1);
      const leaf = document.createElement('span');
      leaf.className = 'leaf';
      leaf.textContent = folder.name.slice(cut + 1).replace(/\/+$/, '');
      path.append(parent, leaf);
    } else {
      path.textContent = folder.name;
    }
    const n = document.createElement('span');
    n.className = 'n';
    n.textContent = folder.count;
    button.append(path, n);
    button.onclick = () => {
      state.filters.folder = state.filters.folder === folder.name ? '' : folder.name;
      syncChips();
      reload({ resetScroll: true });
    };
    box.appendChild(button);
  }
}
