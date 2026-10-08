/** Thin fetch layer with in-flight de-duplication and abortable searches. */

const inflight = new Map();

/* -- expired sessions ----------------------------------------------------
 *
 * A session can end while a tab is sitting open: it is reset, signed out from
 * elsewhere, or it simply reaches its thirty days. Every request after that
 * answers 401, and without this the page just showed failures until somebody
 * thought to reload. Instead the first 401 raises the sign-in screen the app
 * already has.
 *
 * Signing in is itself a 401 when the password is wrong, so anything under
 * /api/auth/ is exempt — otherwise one typo would be reported as an expired
 * session.
 */

const unauthorizedHandlers = new Set();
let sessionAlreadyLost = false;

/* -- guest browsing --------------------------------------------------------
 *
 * Bug: "Continue as a guest" signed itself out immediately. The guest picker
 * tile signs in as nobody on purpose (id 0, the anonymous viewer) so that
 * anything family-or-above-only — the People row, favouriting, and so on —
 * answers 401 "Sign in to do that." That is the server being honest, and the
 * calling code already expects it and degrades gracefully (loadPeople(), for
 * one, just shows an empty row). But every 401 anywhere also used to raise
 * the sign-in screen unconditionally, which is right for someone who really
 * was signed in and got logged out from under them, and wrong for someone
 * who was never signed in at all — a guest hits this on the very first
 * family-gated call the page happens to make, and got bounced straight back
 * to the picker they'd just left, looking exactly like an instant sign-out.
 *
 * The fix isn't to silence 401s (the server's distinction between "you're
 * not signed in" and "you don't have permission" is worth keeping); it's to
 * only treat one as a lost session when there was a session to lose.
 */
let viewerIsAnonymous = false;

/** Call whenever who's-viewing changes — real sign-in, guest, or sign-out. */
export function setAnonymousViewer(isAnonymous) {
  viewerIsAnonymous = !!isAnonymous;
}

export function onUnauthorized(handler) {
  unauthorizedHandlers.add(handler);
  return () => unauthorizedHandlers.delete(handler);
}

/** Call from any fetch helper that sees a 401. Safe to call repeatedly. */
export function reportUnauthorized(url = '') {
  if (String(url).includes('/api/auth/')) return false;
  if (viewerIsAnonymous) return false;      // expected for a guest; not a lost session
  if (sessionAlreadyLost) return true;      // one sign-in screen, not twelve
  sessionAlreadyLost = true;
  for (const handler of unauthorizedHandlers) {
    try {
      handler();
    } catch {
      /* a broken listener must not swallow the original error */
    }
  }
  return true;
}

/** Called once the viewer is back in, so a later expiry is noticed again. */
export function sessionRestored() {
  sessionAlreadyLost = false;
}

async function request(url, options = {}) {
  const response = await fetch(url, {
    headers: { Accept: 'application/json' },
    ...options,
  });
  const text = await response.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    throw new Error(`Unexpected response from ${url}`);
  }
  if (!response.ok) {
    // A 401 carrying `needs_password` is this request asking you to confirm
    // who you are before it does something irreversible — not a session that
    // has ended. Raising the sign-in screen for it put the gate on top of the
    // dialog doing the asking, and signed somebody out of their own gallery
    // for mistyping a password into a delete box.
    if (response.status === 401 && !data?.needs_password) reportUnauthorized(url);
    throw Object.assign(new Error(data?.error || response.statusText), {
      status: response.status,
      data,
    });
  }
  return data;
}

function post(url, body) {
  return request(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify(body ?? {}),
  });
}

/** Collapses duplicate concurrent GETs to the same URL. */
function get(url, { signal } = {}) {
  if (signal) return request(url, { signal });
  if (inflight.has(url)) return inflight.get(url);
  const promise = request(url).finally(() => inflight.delete(url));
  inflight.set(url, promise);
  return promise;
}

export function buildQuery(filters) {
  const params = new URLSearchParams();
  if (filters.q) params.set('q', filters.q);
  for (const kind of filters.kinds || []) params.append('kind', kind);
  if (filters.favorites) params.set('favorites', '1');
  if (filters.duplicates) params.set('duplicates', '1');
  if (filters.is_live) params.set('is_live', '1');
  if (filters.tag) params.set('tag', filters.tag);
  if (filters.folder) params.set('folder', filters.folder);
  if (filters.camera) params.set('camera', filters.camera);
  if (filters.person) params.set('person', String(filters.person));
  if (filters.occasion) params.set('occasion', String(filters.occasion));
  if (filters.album) params.set('album', String(filters.album));
  // Photographs taken near this one (the viewer's "Photos taken nearby").
  if (filters.near) params.set('near', String(filters.near));
  if (filters.from) params.set('from', filters.from);
  if (filters.to) params.set('to', filters.to);
  if (filters.minRating) params.set('min_rating', String(filters.minRating));
  if (filters.visibility) params.set('visibility', filters.visibility);
  if (filters.sort) params.set('sort', filters.sort);
  return params;
}

let scanProgressMissing = false;

export const api = {
  // Without the counts by default. They are the one part of the status that
  // grows with the library — 196,174 photographs and a cold cache made it
  // twenty-three seconds — and nothing on screen needs them to draw a
  // photograph. `counts()` fills them in afterwards.
  status: () => get('/api/status?stats=0'),
  counts: () => get('/api/status/stats'),
  // Everything running in the background, the scan among them. See
  // activity.js, which owns the fallback for a server older than this script.
  activity: () => get('/api/status/activity'),
  scanProgress: async () => {
    // Scripts update the moment they change on disk; the server only on a
    // restart. A page that has the new script and a server that does not yet
    // have this endpoint gets the scan from /api/status as before — the heavy
    // way, but a bar that moves rather than one that stops.
    if (!scanProgressMissing) {
      try {
        return await get('/api/status/scan');
      } catch (err) {
        if (err instanceof TypeError || (err.status && err.status !== 404)) throw err;
        scanProgressMissing = true;
      }
    }
    return (await get('/api/status'))?.scan ?? null;
  },
  people: () => get('/api/faces/people'),
  albums: () => get('/api/albums'),
  album: (id) => get(`/api/albums/${id}`),
  createAlbum: (name, ids = []) => post('/api/albums', { name, ids }),
  updateAlbum: (id, fields) => request(`/api/albums/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify(fields ?? {}),
  }),
  albumAdd: (id, ids) => post(`/api/albums/${id}/items`, { ids, remove: false }),
  albumRemove: (id, ids) => post(`/api/albums/${id}/items`, { ids, remove: true }),
  deleteAlbum: (id) => request(`/api/albums/${id}`, { method: 'DELETE' }),
  segments: (filters, signal, { limit, offset = 0 } = {}) => {
    const params = buildQuery(filters);
    if (limit) params.set('limit', String(limit));
    if (offset) params.set('offset', String(offset));
    return get(`/api/segments?${params}`, { signal });
  },
  assets: (filters, { limit = 200, offset = 0 } = {}) => {
    const params = buildQuery(filters);
    params.set('limit', String(limit));
    params.set('offset', String(offset));
    return get(`/api/assets?${params}`);
  },
  byIds: (ids) => get(`/api/assets?ids=${ids.join(',')}`),
  asset: (id) => get(`/api/asset/${id}`),
  // Voice stories (stories.js). The recording goes as multipart, so the
  // browser writes its own Content-Type.
  stories: (id) => request(`/api/asset/${id}/stories`),
  addStory: (id, form) => request(`/api/asset/${id}/stories`, { method: 'POST', body: form }),
  deleteStory: (id) => request(`/api/stories/${id}`, { method: 'DELETE' }),
  update: (id, fields) => post(`/api/asset/${id}`, fields),
  bulk: (ids, fields) => post('/api/assets/bulk', { ids, ...fields }),
  // The password goes with every call, never remembered between them.
  deleteItems: (ids, password) => post('/api/delete', { ids, password }),
  rotate: (id, rotation) => post(`/api/asset/${id}/rotate`, { rotation }),
  // Turns the files themselves, not just Ninaivu's view of them. Admin only.
  rotateFiles: (ids, rotation) => post('/api/rotate', { ids, rotation }),
  facets: () => get('/api/facets'),
  duplicates: () => get('/api/duplicates'),
  occasions: () => get('/api/occasions'),
  similar: (id) => get(`/api/similar/${id}`),
  suggest: (q) => get(`/api/suggest?q=${encodeURIComponent(q)}`),
  browse: (path) => get(`/api/library/browse?path=${encodeURIComponent(path || '')}`),
  setRoot: (path, full = false) => post('/api/library/root', { path, full }),
  rescan: (full = false) => post('/api/scan', { full }),
  settings: (fields) => post('/api/settings', fields),
  memories: (month, day) => {
    const params = new URLSearchParams();
    if (month) params.set('month', month);
    if (day) params.set('day', day);
    const qs = params.toString();
    return get(`/api/memories/on-this-day${qs ? '?' + qs : ''}`);
  },
  geoPoints: (bounds = {}) => {
    const qs = new URLSearchParams(bounds).toString();
    return get(`/api/geo/points${qs ? '?' + qs : ''}`);
  },
  // The map (storage/geo.py): groups on screen, the places, a group's or a
  // place's photographs, and a year's route.
  geoClusters: (query) => get(`/api/geo/clusters?${new URLSearchParams(query)}`),
  geoPlaces: (query = {}) => get(`/api/geo/places?${new URLSearchParams(query)}`),
  geoPhotos: (query) => get(`/api/geo/photos?${new URLSearchParams(query)}`),
  geoTrail: (query) => get(`/api/geo/trail?${new URLSearchParams(query)}`),
  shares: () => get('/api/shares'),
  createShare: (scope, targetId, expiresInDays, password) =>
    post('/api/shares', { scope, target_id: targetId, expires_in_days: expiresInDays, password }),
  deleteShare: (token) => request(`/api/shares/${token}`, { method: 'DELETE' }),
  peopleClusters: () => get('/api/faces/clusters'),
  savePersonCluster: (name, avatarAssetId) =>
    post('/api/faces/clusters', { name, avatar_asset_id: avatarAssetId }),
};

/**
 * A thumbnail URL, with the version that makes caching safe.
 *
 * Thumbnails come back `immutable` with a year's lifetime, so a browser that
 * has one will never ask again — which is right until a rotation rewrites the
 * file underneath it. Passing the asset's `thumb_v` means a rewritten
 * thumbnail is a different URL, and the old one can stay cached for ever
 * exactly as intended.
 */
export function thumbUrl(id, size, version) {
  const v = version ? `&v=${version}` : '';
  return `/api/thumb/${id}?s=${size}${v}`;
}

function safeParse(text) {
  try { return JSON.parse(text); } catch { return null; }
}

/** Subscribes to scan progress over SSE, falling back to polling. */
export function subscribeProgress(onMessage) {
  let source = null;
  let retry = null;
  let poll = null;

  // Not every browser has server-sent events, and a privacy extension or a
  // corporate proxy can take them away from one that does. Falling back to
  // polling costs a request every two seconds and keeps the progress bar
  // honest; throwing here used to take the whole page down with it, because
  // this runs inside the console's start-up and nothing above it caught.
  const startPolling = () => {
    if (poll) return;
    const ask = async () => {
      // Nobody is looking; the next tick after they come back catches up.
      if (typeof document !== 'undefined' && document.hidden) return;
      try {
        // The counters alone. /api/status counts the whole library, which
        // every two seconds during a scan is real work for the database.
        const scan = await api.scanProgress();
        if (scan) onMessage(scan);
      } catch { /* a dropped poll is not worth reporting */ }
    };
    ask();
    poll = setInterval(ask, 2000);
  };

  let failures = 0;

  const connect = () => {
    if (typeof EventSource === 'undefined') {
      startPolling();
      return;
    }
    source = new EventSource('/api/events');
    source.onmessage = (event) => {
      failures = 0;
      onMessage(safeParse(event.data));
    };
    source.onerror = () => {
      source.close();
      source = null;
      clearTimeout(retry);
      failures += 1;
      // A stream that keeps failing is not a network blip, it is a stream
      // that is not there. On the family port the progress endpoint is an
      // admin route and so genuinely 404s, which used to leave this retrying
      // every four seconds for ever while the progress bar never appeared at
      // all. Two attempts, then poll — which that port does allow.
      if (failures >= 2) startPolling();
      else retry = setTimeout(connect, 4000);
    };
  };

  connect();
  return () => {
    clearTimeout(retry);
    clearInterval(poll);
    poll = null;
    source?.close();
  };
}


// Scan phases measured in items (done, total) rather than files walked.
// When tag_total is present, these provide the human-readable progress line.
export const SCAN_COUNTS = {
  tagging: (done, total) => `Analysing ${done} / ${total}`,
  videos: (done, total) => `Describing ${done} / ${total} videos`,
  naming: (done, total) => `Naming places in ${done} / ${total}`,
  reading: (done, total) => `Reading text in ${done} / ${total}`,
  faces: (done, total) => `Looking for faces in ${done} / ${total}`,
  covers: (done, total) => `Drawing pictures for ${done} / ${total} sound files`,
};

// How long is left, in the words somebody would use out loud. Rounded hard on
// purpose: a pass measured over thousands of items is not accurate to the
// minute, and "about 14 hours" is the answer people are actually asking for —
// whether to leave the machine on tonight.
export function timeLeft(seconds) {
  if (!seconds || seconds < 0) return '';
  if (seconds < 90) return 'nearly done';
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `about ${minutes} minutes left`;
  const hours = seconds / 3600;
  if (hours < 2) return 'about an hour and a half left';
  if (hours < 36) return `about ${Math.round(hours)} hours left`;
  return `about ${Math.round(hours / 24)} days left`;
}

