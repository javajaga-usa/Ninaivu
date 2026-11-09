/**
 * The console's Library page: the folders that are indexed, and the
 * photographs without a location that can be given one. Loaded when the
 * page is first opened (admin.js, openPage). The rescan and "Change" buttons
 * and the folder picker stay in admin.js: the command bar presses them, and
 * other pages use the picker too.
 */

import { admin, thumbUrl } from '../../api.js';
import * as i18n from '../../i18n.js';
import { consoleState as state } from '../../store.js';
import { $, consoleToast as toast, el } from '../../ui.js';

let shell = null;

/** `?path=…`, with `force=1` and `let_go=1` only when they are meant. */
function libraryQuery(path, force, letGo) {
  return { path, force: force ? '1' : undefined, let_go: letGo ? '1' : undefined };
}

/* -- the folders ----------------------------------------------------------- */

export function renderLibraryFolders() {
  const list = $('#library-list');
  if (!list) return;
  const folders = state.overview?.library?.folders || [];
  list.innerHTML = '';

  if (!folders.length) {
    list.appendChild(el('p', 'hint', i18n.t('No folders yet — add one to get started.')));
    return;
  }

  for (const folder of folders) {
    const row = el('div', `library-item${folder.exists ? '' : ' missing'}`);

    const main = el('div', 'li-main');
    const title = el('div', 'li-name');
    title.appendChild(el('strong', null, folder.name));
    if (folder.active) title.appendChild(el('span', 'tag family', i18n.t('default')));
    if (!folder.exists) title.appendChild(el('span', 'tag hidden', i18n.t('missing')));
    main.appendChild(title);
    main.appendChild(el('code', 'li-path', folder.path));
    const bits = [i18n.items(folder.count)];
    if (folder.assigned) {
      bits.push(folder.assigned === 1 ? i18n.t('1 person assigned')
        : i18n.t('{count} people assigned', { count: folder.assigned }));
    }
    main.appendChild(el('div', 'li-meta', bits.join(' · ')));
    row.appendChild(main);

    const actions = el('div', 'li-actions');
    if (!folder.active) {
      const makeDefault = el('button', 'btn small ghost', i18n.t('Make default'));
      makeDefault.type = 'button';
      makeDefault.title = i18n.t('The folder the Visibility tab works on');
      makeDefault.onclick = async () => {
        try {
          await admin.postAdminLibrariesActive({ body: { path: folder.path } });
          await shell.refresh();
        } catch (exc) { toast(exc.message, true); }
      };
      actions.appendChild(makeDefault);
    }
    const remove = el('button', 'btn small ghost', i18n.t('Remove'));
    remove.type = 'button';
    remove.title = i18n.t('Stop indexing this folder. Your files are not touched.');
    remove.onclick = () => removeLibrary(folder);
    actions.appendChild(remove);
    row.appendChild(actions);

    list.appendChild(row);
  }
}

async function removeLibrary(folder, letGo = false) {
  try {
    await admin.deleteAdminLibraries({ query: libraryQuery(folder.path, false, letGo) });
    toast(i18n.t('{name} is no longer indexed. Your files were not touched.', { name: folder.name }));
    await shell.refresh();
  } catch (exc) {
    if (exc.status === 409 && exc.data?.let_go && !letGo) {
      // Originals of it are kept only on the Secondary: asked on its own,
      // so agreeing to it never also agrees to what is asked next.
      if (confirm(`${exc.message}\n\n${i18n.t('Remove it anyway?')}`)) await removeLibrary(folder, true);
      return;
    }
    if (exc.status === 409) {
      // Someone is assigned to it, or its photographs carry stories, albums
      // and names that go with it: say which, and let the admin decide.
      if (!confirm(`${exc.message}\n\n${i18n.t('Remove it anyway?')}`)) return;
      try {
        await admin.deleteAdminLibraries({ query: libraryQuery(folder.path, true, letGo) });
        toast(i18n.t('{name} is no longer indexed. Your files were not touched.', { name: folder.name }));
        await shell.refresh();
      } catch (inner) { toast(inner.message, true); }
      return;
    }
    toast(exc.message, true);
  }
}

/* -- Locations filled in ------------------------------------------------- */
/* Photographs without a location, taken close in time to one with; given its
   place in the index only (storage/geo.py), and all of it undoable. */

const locations = { data: null, shown: 30 };

/* "India" for IN, from the browser, in the console's language; the code
   itself if it cannot say. */
function countryLabel(code) {
  if (!code) return '';
  try {
    return new Intl.DisplayNames([i18n.locale()], { type: 'region' }).of(code) || code;
  } catch {
    return code;
  }
}

async function loadLocations() {
  const summary = $('#loc-summary');
  if (!summary) return;
  const hours = $('#loc-hours').value;
  summary.textContent = i18n.t('Looking…');
  try {
    locations.data = await admin.getAdminLocations({ query: { hours } });
  } catch (exc) {
    summary.textContent = exc.message;
    return;
  }
  locations.shown = 30;
  renderLocations();
}

function renderLocations() {
  const data = locations.data;
  if (!data) return;
  const span = $('#loc-hours').selectedOptions[0]?.textContent.toLowerCase() || '';
  const found = i18n.t('{count} photographs taken {span} of one with a location, on {groups} days and places.',
    { count: data.total.toLocaleString(), span, groups: data.groups.length.toLocaleString() });
  const already = i18n.t('{count} already have a filled-in location.',
    { count: (data.inferred || 0).toLocaleString() });
  $('#loc-summary').textContent = data.total
    ? (data.inferred ? `${found} ${already}` : found)
    : (data.inferred
      ? i18n.t('Nothing more to fill in. {count} photographs have a filled-in location.',
        { count: data.inferred.toLocaleString() })
      : i18n.t('Nothing to fill in: no photograph without a location was taken that close to one with.'));
  const all = $('#loc-apply-all');
  all.hidden = !data.total;
  all.textContent = i18n.t('Give all {count} their place', { count: data.total.toLocaleString() });
  $('#loc-undo').hidden = !data.inferred;

  const list = $('#loc-list');
  list.innerHTML = '';
  for (const group of data.groups.slice(0, locations.shown)) {
    const row = el('div', 'loc-row');
    const text = el('div', 'loc-text');
    const place = [group.city, countryLabel(group.country)].filter(Boolean).join(', ')
      || i18n.t('a place with no name');
    const photographs = group.count === 1 ? i18n.t('1 photograph')
      : i18n.t('{count} photographs', { count: group.count.toLocaleString() });
    text.append(el('strong', null, `${group.date || i18n.t('No date')} · ${place}`),
      el('span', 'hint', `${photographs} · ${i18n.t('up to {minutes} min from one with a location',
        { minutes: Math.round(group.max_minutes) })}`));
    const thumbs = el('div', 'loc-thumbs');
    for (const id of group.ids.slice(0, 5)) {
      const img = el('img');
      img.loading = 'lazy';
      img.alt = '';
      img.src = thumbUrl(id, 160);
      thumbs.appendChild(img);
    }
    const apply = el('button', 'btn ghost small', i18n.t('Give them this place'));
    apply.type = 'button';
    apply.onclick = () => applyLocations({ ids: group.ids }, apply);
    row.append(text, thumbs, apply);
    list.appendChild(row);
  }
  const more = $('#loc-more');
  more.hidden = data.groups.length <= locations.shown;
}

async function applyLocations(body, button) {
  if (button) button.disabled = true;
  try {
    const done = await admin.postAdminLocationsApply({ body: { hours: Number($('#loc-hours').value), ...body } });
    toast(i18n.t('{count} photographs given their place.', { count: done.applied.toLocaleString() }));
  } catch (exc) {
    toast(exc.message, true);
  }
  await loadLocations();
}

function wireLocations() {
  $('#loc-hours')?.addEventListener('change', loadLocations);
  $('#loc-apply-all')?.addEventListener('click', (event) => applyLocations({ all: true }, event.target));
  $('#loc-more')?.addEventListener('click', () => {
    locations.shown += 30;
    renderLocations();
  });
  $('#loc-undo')?.addEventListener('click', async () => {
    const count = locations.data?.inferred || 0;
    if (!window.confirm(i18n.t('Take back the filled-in location of {count} photographs?',
      { count: count.toLocaleString() }))) return;
    try {
      const done = await admin.postAdminLocationsUndo({ body: {} });
      toast(i18n.t('{count} filled-in locations taken back.', { count: done.undone.toLocaleString() }));
    } catch (exc) {
      toast(exc.message, true);
    }
    await loadLocations();
  });
}

/** Bind the page's controls: once, when it is first opened. */
export function wire(context) {
  shell = context;
  wireLocations();
}

/** The page has been opened. */
export function show() {
  renderLibraryFolders();
  loadLocations();
}

/** The language changed: draw what is held again in it. */
export function relabel() {
  renderLocations();
}
