/**
 * The family app's map of where the photographs were taken, loaded when first opened (app.js, openPage).
 */

import { api, thumbUrl } from '../../api.js';
import * as i18n from '../../i18n.js';
import { state } from '../../store.js';
import { $, toast } from '../../ui.js';

/** The gallery around the map (app.js): its viewer. */
let shell;

/* -- Geo Map ------------------------------------------------------------ */
let leafletMap = null;
let leafletLoading = null;

/**
 * Fetch Leaflet the first time a map is actually opened.
 *
 * It is 147 KB of the app's JavaScript and a stylesheet besides, and it was
 * loading on every visit to the gallery whether or not anybody went near the
 * map — including on phones, on the household's own broadband, before a
 * single photograph appeared.
 */
function loadLeaflet() {
  if (window.L) return Promise.resolve();
  if (leafletLoading) return leafletLoading;
  leafletLoading = new Promise((resolve, reject) => {
    const css = document.createElement('link');
    css.rel = 'stylesheet';
    css.href = '/static/css/leaflet.css';
    document.head.appendChild(css);
    const script = document.createElement('script');
    script.src = '/static/js/leaflet.js';
    script.onload = () => resolve();
    script.onerror = () => reject(new Error(i18n.t('The map could not be loaded.')));
    document.head.appendChild(script);
  });
  return leafletLoading;
}
let worldOutline = null;

/* Whether the household has asked for street tiles.
 *
 * Off is the default, and it is the only setting in Ninaivu that decides
 * whether anything about the library leaves the machine. A tile server is
 * told which square of the world is on screen, one request per tile — open
 * the map on last summer's holiday and somebody else's logs know where the
 * family spent it. So the map is drawn from an outline that ships with
 * Ninaivu instead, and the tiles are something a household turns on knowing
 * what it is trading.
 */
function wantsTiles() {
  return document.body.dataset.mapTiles === '1';
}

/* The world, from a file in this repository.
 *
 * Natural Earth's 1:110m coastlines and borders, public domain, about 160 KB,
 * fetched once and kept. It draws which country and which coast, which is
 * what a map of a family library is usually being asked. It has no streets,
 * and past roughly a city's width there is nothing left in it to draw — the
 * pins are then their own map, which for photographs taken in one town is
 * what you were looking at anyway.
 */
async function loadWorld() {
  if (worldOutline) return worldOutline;
  const answer = await fetch('/static/data/world.geojson');
  if (!answer.ok) throw new Error(i18n.t('The world outline could not be loaded.'));
  worldOutline = await answer.json();
  return worldOutline;
}

async function drawBasemap(map) {
  if (wantsTiles()) {
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 18,
      attribution: '\u00a9 OpenStreetMap contributors',
    }).addTo(map);
    return;
  }
  try {
    L.geoJSON(await loadWorld(), {
      interactive: false,
      // Colours live in the stylesheet so the map follows the theme rather
      // than staying daylight-coloured in a dark room.
      style: () => ({ className: 'world-shape' }),
      attribution: i18n.t('Outline: Natural Earth'),
    }).addTo(map);
  } catch (err) {
    // A map of pins on an empty ground is still a map. Failing to draw the
    // sea is not a reason to show the household nothing.
    console.warn(err);
  }
}

/* The map's state. It asked for up to 1,500 photographs and drew one pin
   each, and read `gps_lat` from answers that said `lat`, so it drew none.
   Now the server groups what is on screen (storage/geo.py): every photograph
   with a location is on the map, a busy place is one circle with a count,
   and a circle opens to all of its photographs. */
const mapView = {
  year: '', person: '', trip: false,
  groups: null, route: null, here: null,
  places: null, openCountries: new Set(),
  seq: 0, timer: null, returning: false,
};

function mapQuery(extra = {}) {
  const query = { ...extra };
  if (mapView.year) query.year = mapView.year;
  if (mapView.person) query.person = mapView.person;
  return query;
}

/* A country's name from its code, in the page's language, by the browser. */
function countryName(code) {
  if (!code) return '';
  try {
    return new Intl.DisplayNames([i18n.language(), 'en'], { type: 'region' }).of(code) || code;
  } catch {
    return code;
  }
}

function placeLabel(city, country) {
  return [city, countryName(country)].filter(Boolean).join(', ') || i18n.t('Unnamed places');
}

function shortCount(n) {
  if (n < 1000) return String(n);
  return `${(n / 1000).toFixed(n < 10000 ? 1 : 0)}k`;
}

function dateRange(first, last) {
  return first && last && first !== last ? `${first} – ${last}` : (first || last || '');
}

/** Frame [south, north, west, east] on the map. */
function fitTo(bounds, maxZoom = 14) {
  if (!leafletMap || !bounds) return;
  const [south, north, west, east] = bounds;
  leafletMap.fitBounds([[south, west], [north, east]], { padding: [40, 40], maxZoom });
}

function closeMap() {
  mapView.returning = false;
  $('#map-modal').hidden = true;
}

export async function openMap({ focus = null } = {}) {
  const modal = $('#map-modal');
  try {
    await loadLeaflet();
  } catch (err) {
    toast(err.message, true);
    return;
  }
  modal.hidden = false;
  if (typeof L === 'undefined') return;

  if (!leafletMap) {
    leafletMap = L.map('leaflet-map', { worldCopyJump: true }).setView([20, 0], 2);
    await drawBasemap(leafletMap);
    mapView.groups = L.layerGroup().addTo(leafletMap);
    mapView.route = L.layerGroup().addTo(leafletMap);
    mapView.here = L.layerGroup().addTo(leafletMap);
    leafletMap.on('moveend', () => {
      clearTimeout(mapView.timer);
      mapView.timer = setTimeout(refreshGroups, 120);
    });
  }
  // Its size is only known once the dialog is showing.
  setTimeout(() => leafletMap?.invalidateSize(), 50);
  setTimeout(() => leafletMap?.invalidateSize(), 250);

  // Opened from a photograph, the map shows it: a year or a person chosen
  // earlier would have filtered out the very photograph it was opened on.
  if (focus) {
    mapView.year = '';
    mapView.person = '';
    if (mapView.trip) setTrip(false);
    const person = $('#map-person');
    if (person) person.value = '';
  }
  fillPeople();
  mapView.here.clearLayers();
  const places = await loadPlaces();
  if (focus) {
    leafletMap.setView([focus.lat, focus.lon], 15);
    mapView.here.addLayer(L.circleMarker([focus.lat, focus.lon],
      { radius: 34, className: 'map-here', interactive: false }));
  } else if (places?.bounds) {
    fitTo(places.bounds);
  } else if (places && !places.total) {
    toast(i18n.t('No photos with GPS coordinates found yet.'));
  }
  refreshGroups();
}

async function refreshMap() {
  await loadPlaces();
  refreshGroups();
  if (mapView.trip) loadRoute();
}

function fillPeople() {
  const select = $('#map-person');
  if (!select) return;
  const people = (state.people || []).filter((person) => person.name);
  select.hidden = !people.length;
  const signature = people.map((person) => person.id).join(',');
  if (select.dataset.signature === signature) return;
  select.dataset.signature = signature;
  select.length = 1;
  for (const person of people) select.add(new Option(person.name, String(person.id)));
  select.value = mapView.person;
}

function fillYears(years) {
  const select = $('#map-year');
  if (!select) return;
  const signature = (years || []).join(',');
  if (select.dataset.signature !== signature) {
    select.dataset.signature = signature;
    select.length = 1;
    for (const year of years || []) select.add(new Option(String(year), String(year)));
  }
  select.value = mapView.year;
}

async function loadPlaces() {
  let data;
  try {
    data = await api.geoPlaces(mapQuery());
  } catch (err) {
    toast(i18n.t('Could not load map points: {reason}', { reason: err.message }), true);
    return null;
  }
  mapView.places = data;
  fillYears(data.years);
  renderPlaces(data);
  return data;
}

/* The places, country by country, most photographed first. A country opens
   to its cities; a city frames the map, and its button opens its photos. */
function renderPlaces(data) {
  const list = $('#map-places-list');
  if (!list || !data) return;
  list.innerHTML = '';
  $('#map-places-summary').textContent = i18n.t('{count} photographs in {places} places',
    { count: data.total.toLocaleString(), places: data.places.length.toLocaleString() });

  const countries = new Map();
  for (const place of data.places) {
    const country = countries.get(place.country)
      || { code: place.country, count: 0, places: [], bounds: null };
    country.count += place.count;
    country.places.push(place);
    const [s, n, w, e] = place.bounds;
    country.bounds = country.bounds
      ? [Math.min(country.bounds[0], s), Math.max(country.bounds[1], n),
        Math.min(country.bounds[2], w), Math.max(country.bounds[3], e)]
      : [s, n, w, e];
    countries.set(place.country, country);
  }

  for (const country of [...countries.values()].sort((a, b) => b.count - a.count)) {
    const open = mapView.openCountries.has(country.code);
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'map-place map-country';
    row.setAttribute('aria-expanded', String(open));
    const toggle = document.createElement('span');
    toggle.className = 'map-place-toggle';
    toggle.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 6 6 6-6 6"/></svg>';
    const name = document.createElement('span');
    name.className = 'map-place-name';
    name.textContent = country.code ? countryName(country.code) : i18n.t('Unnamed places');
    const count = document.createElement('span');
    count.className = 'map-place-count';
    count.textContent = country.count.toLocaleString();
    row.append(toggle, name, count);
    row.onclick = () => {
      if (open) mapView.openCountries.delete(country.code);
      else mapView.openCountries.add(country.code);
      renderPlaces(mapView.places);
      if (!open) fitTo(country.bounds, 8);
    };
    list.appendChild(row);
    if (!open) continue;

    for (const place of country.places) {
      const city = document.createElement('div');
      city.className = 'map-place map-city';
      const go = document.createElement('button');
      go.type = 'button';
      go.className = 'map-place-name';
      go.textContent = place.city || i18n.t('Unnamed places');
      go.onclick = () => fitTo(place.bounds);
      const tally = document.createElement('span');
      tally.className = 'map-place-count';
      tally.textContent = place.count.toLocaleString();
      const view = document.createElement('button');
      view.type = 'button';
      view.className = 'icon-btn map-place-view';
      view.title = i18n.t('View photos');
      view.setAttribute('aria-label', `${i18n.t('View photos')}: ${go.textContent}`);
      view.innerHTML = '<svg viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="9" cy="10" r="1.6"/><path d="m21 16-5-5-8 8"/></svg>';
      view.onclick = () => openPlacePhotos({ country: place.country, city: place.city });
      city.append(go, tally, view);
      list.appendChild(city);
    }
  }
}

async function refreshGroups() {
  if (!leafletMap || $('#map-modal').hidden) return;
  const seq = ++mapView.seq;
  const bounds = leafletMap.getBounds();
  let data;
  try {
    data = await api.geoClusters(mapQuery({
      south: bounds.getSouth(), north: bounds.getNorth(),
      west: bounds.getWest(), east: bounds.getEast(), zoom: leafletMap.getZoom(),
    }));
  } catch (err) {
    if (seq === mapView.seq) {
      toast(i18n.t('Could not load map points: {reason}', { reason: err.message }), true);
    }
    return;
  }
  if (seq !== mapView.seq) return;           // the map moved on while this was asked
  mapView.groups.clearLayers();
  for (const group of mergeOverlapping(data.clusters || [])) {
    mapView.groups.addLayer(groupMarker(group));
  }
}

/* The server groups by a grid, and a town on a grid line came back as two
   circles on top of each other. Circles nearer than this on screen become
   one; it keeps each part's own bounds, so opening it opens exactly its
   photographs and none from between them. */
const MERGE_PX = 44;

function mergeOverlapping(groups) {
  const merged = [];
  for (const group of [...groups].sort((a, b) => b.count - a.count)) {
    const at = leafletMap.latLngToLayerPoint([group.lat, group.lon]);
    const near = merged.find((other) => other.at.distanceTo(at) < MERGE_PX);
    if (!near) {
      merged.push({ ...group, at, parts: [group.bounds] });
      continue;
    }
    const total = near.count + group.count;
    near.lat = (near.lat * near.count + group.lat * group.count) / total;
    near.lon = (near.lon * near.count + group.lon * group.count) / total;
    near.count = total;
    near.parts.push(group.bounds);
    const [s, n, w, e] = group.bounds;
    near.bounds = [Math.min(near.bounds[0], s), Math.max(near.bounds[1], n),
      Math.min(near.bounds[2], w), Math.max(near.bounds[3], e)];
  }
  return merged;
}

function groupMarker(group) {
  const many = group.count > 1;
  const html = `<div class="map-cluster"><img src="${thumbUrl(group.cover, 160)}" alt="" loading="lazy">`
    + (many ? `<span class="map-count">${shortCount(group.count)}</span>` : '') + '</div>';
  const icon = L.divIcon({ className: 'map-cluster-icon', html, iconSize: [52, 52], iconAnchor: [26, 26] });
  const marker = L.marker([group.lat, group.lon], { icon, title: placeLabel(group.city, group.country) });
  if (many) {
    // Bound once, drawn when opened. Bound in the click itself, Leaflet's own
    // toggle ran in the same click and closed the popup it had just opened.
    marker.bindPopup(() => groupPopup(group, marker), { minWidth: 200 });
  } else {
    marker.on('click', () => openMapPhotos([group.cover]));
  }
  return marker;
}

function groupPopup(group, marker) {
  const box = document.createElement('div');
  box.className = 'map-popup';
  const img = document.createElement('img');
  img.src = thumbUrl(group.cover, 320);
  img.alt = '';
  const title = document.createElement('strong');
  title.textContent = placeLabel(group.city, group.country);
  const count = document.createElement('span');
  count.className = 'hint';
  count.textContent = i18n.t('{count} photographs', { count: group.count.toLocaleString() });
  const actions = document.createElement('div');
  actions.className = 'map-popup-actions';
  const view = document.createElement('button');
  view.type = 'button';
  view.className = 'btn primary small';
  view.textContent = i18n.t('View photos');
  view.onclick = () => {
    marker.closePopup();
    openGroupPhotos(group.parts || [group.bounds]);
  };
  const zoom = document.createElement('button');
  zoom.type = 'button';
  zoom.className = 'btn ghost small';
  zoom.textContent = i18n.t('Zoom in');
  zoom.onclick = () => {
    marker.closePopup();
    fitTo(group.bounds, Math.min(18, leafletMap.getZoom() + 4));
  };
  actions.append(view, zoom);
  box.append(img, title, count, actions);
  return box;
}

/* A circle's photographs: each part it was made of, asked for on its own. */
async function openGroupPhotos(parts) {
  let answers;
  try {
    answers = await Promise.all(parts.map(([south, north, west, east]) =>
      api.geoPhotos(mapQuery({ south, north, west, east }))));
  } catch (err) {
    toast(err.message, true);
    return;
  }
  const ids = [...new Set(answers.flatMap((answer) => answer.ids || []))];
  const total = answers.reduce((sum, answer) => sum + (answer.total || 0), 0);
  if (!ids.length) return;
  if (total > ids.length) {
    toast(i18n.t('Showing the newest {shown} of {total}',
      { shown: ids.length.toLocaleString(), total: total.toLocaleString() }));
  }
  openMapPhotos(ids);
}

async function openPlacePhotos(query) {
  let data;
  try {
    data = await api.geoPhotos(mapQuery(query));
  } catch (err) {
    toast(err.message, true);
    return;
  }
  if (!data.ids?.length) return;
  if (data.total > data.ids.length) {
    toast(i18n.t('Showing the newest {shown} of {total}',
      { shown: data.ids.length.toLocaleString(), total: data.total.toLocaleString() }));
  }
  openMapPhotos(data.ids);
}

/* The viewer is drawn beneath dialogs, so the map steps aside while a
   photograph is open, and comes back when it closes. */
function openMapPhotos(ids) {
  mapView.returning = true;
  $('#map-modal').hidden = true;
  shell.viewer.open(ids, 0);
}

function setTrip(on) {
  if (on && !mapView.year) {
    toast(i18n.t('Choose a year to see its trips.'));
    return;
  }
  mapView.trip = on;
  const button = $('#map-trip');
  button?.setAttribute('aria-pressed', String(on));
  button?.classList.toggle('active', on);
  if (on) loadRoute();
  else mapView.route?.clearLayers();
}

/* A year's photographs in the order they were taken, as a route of stops. */
async function loadRoute() {
  mapView.route.clearLayers();
  if (!mapView.trip || !mapView.year) return;
  let data;
  try {
    data = await api.geoTrail(mapQuery());
  } catch (err) {
    toast(err.message, true);
    return;
  }
  const stops = data.stops || [];
  if (!stops.length) {
    toast(i18n.t('No photos with locations in {year}.', { year: mapView.year }));
    return;
  }
  const line = L.polyline(stops.map((stop) => [stop.lat, stop.lon]),
    { className: 'map-trail', interactive: false });
  mapView.route.addLayer(line);
  stops.forEach((stop, index) => {
    const dot = L.circleMarker([stop.lat, stop.lon], { radius: 6, className: 'map-stop' });
    const tip = document.createElement('span');
    tip.textContent = `${index + 1}. ${placeLabel(stop.city, stop.country)} · `
      + `${dateRange(stop.first, stop.last)} · ${i18n.t('{count} photographs', { count: stop.count })}`;
    dot.bindTooltip(tip);
    dot.on('click', () => openMapPhotos(stop.ids));
    mapView.route.addLayer(dot);
  });
  leafletMap.fitBounds(line.getBounds(), { padding: [40, 40], maxZoom: 12 });
}

/** Bind the map's controls: once, when it is first opened. */
export function wire(context) {
  shell = context;
  $('#map-close')?.addEventListener('click', closeMap);
  $('#map-year')?.addEventListener('change', (event) => {
    mapView.year = event.target.value;
    if (!mapView.year && mapView.trip) setTrip(false);
    refreshMap();
  });
  $('#map-person')?.addEventListener('change', (event) => {
    mapView.person = event.target.value;
    refreshMap();
  });
  $('#map-trip')?.addEventListener('click', () => setTrip(!mapView.trip));
  $('#map-places-toggle')?.addEventListener('click', () => {
    const open = $('#map-places').classList.toggle('open');
    $('#map-places-toggle').setAttribute('aria-expanded', String(open));
    setTimeout(() => leafletMap?.invalidateSize(), 50);
  });
  // A photograph opened from the map goes back to the map when it closes.
  shell.viewer.addEventListener('close', () => {
    if (!mapView.returning) return;
    mapView.returning = false;
    $('#map-modal').hidden = false;
    setTimeout(() => leafletMap?.invalidateSize(), 50);
  });
}
