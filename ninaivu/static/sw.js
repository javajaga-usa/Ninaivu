/**
 * Offline shell for the family app. Media always rechecks server access.
 *
 * WHAT THIS MAY CACHE, AND WHY THE LIST IS SHORT
 *
 * Only the app shell is stored. Thumbnails and originals recheck access on
 * every request, because file dates and role date ranges can change.
 *
 * Everything else goes to the network every time (asking with the copy the
 * browser has, so an unchanged answer is a 304). In particular:
 *
 *   /api/file, /api/download, /api/proxy   originals, which are large and
 *                                          private and belong on disk once
 *                                          only, where the user put them.
 *   /api/segments, /api/assets, /api/me    listings differ per viewer. Cached,
 *                                          the next person to pick up the
 *                                          tablet would be handed the last
 *                                          person's gallery.
 *   anything on the console                management is never offline.
 *
 * THE CONSOLE'S COPY
 *
 * The admin console registers this same file as /sw.js?console=1. That copy
 * stores nothing at all: it only answers a failed visit to the console's home
 * page with the same "Ninaivu is Offline" page the family app shows, so an
 * admin who opens the console while the server is down is told so in the same
 * words instead of getting the browser's own error.
 *
 * The caches are versioned. Changing CACHE_VERSION drops every old one on the
 * next activation, which is how a stale app shell gets cleaned up.
 */

const CACHE_VERSION = 'v14-new-icon';

/** True for the console's copy, which caches nothing (see the note above). */
const CONSOLE = new URLSearchParams(self.location.search || '').has('console');
/**
 * Thumbnails keep the version they were cached under. They are the expensive
 * thing to fetch again — thousands of them for a library scrolled through — and
 * a change to the app's scripts does not change a single one. The shell's version
 * moves when the scripts change, so that nobody is left with the old editor
 * asking a new worker to do something it no longer knows how to.
 */
const THUMB_VERSION = 'v9-date-access';
const SHELL_CACHE = `ninaivu-shell-${CACHE_VERSION}`;
const THUMB_CACHE = `ninaivu-thumbs-${THUMB_VERSION}`;

/**
 * Thumbnails are small (a 256px WEBP is tens of kilobytes) and a library is
 * scrolled through in long runs, so the cache has to be worth having: at 600
 * entries, and two sizes per picture, it held about three hundred photographs
 * and evicted the beginning of a scroll before the end of it.
 */
const MAX_THUMBS = 4000;

self.addEventListener('install', (event) => {
  // Nothing is pre-fetched. The shell is cached as it is actually used, so a
  // first visit costs one round trip per file and never downloads something
  // this install will not open.
  event.waitUntil(self.skipWaiting());
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(names
      .filter((name) => name.startsWith('ninaivu-')
        && name !== SHELL_CACHE && name !== THUMB_CACHE)
      .map((name) => caches.delete(name)));
    await self.clients.claim();
  })());
});

/** Signing out clears everything: the next person gets a clean device. */
/**
 * Thumbnails the server confirmed a little while ago, and when. Scrolling back
 * to them asked the server again for every tile, one round trip each before
 * the picture could show (222 of them going back up 40 screens): on a phone
 * over Wi-Fi, the "reload" of photographs that had just been on screen. One
 * the server answered for in the last RECHECK_MS is shown from the browser's
 * own copy; after that it is asked about again, so a change of access still
 * reaches it within minutes, and signing out forgets them all at once. Held in
 * memory only: a helper the browser stops and starts again asks afresh.
 */
const RECHECK_MS = 10 * 60 * 1000;
const checked = new Map();          // thumbnail URL -> when the server last answered
const MAX_CHECKED = 20000;

function thumbnail(request) {
  const at = checked.get(request.url);
  if (at && Date.now() - at < RECHECK_MS) {
    // The copy the browser has: no request leaves for the server. If the
    // browser has let it go, this fetches it, which is a check in itself.
    return fetch(request, { cache: 'force-cache' });
  }
  return fetch(request, { cache: 'no-cache' }).then((response) => {
    if (response.ok || response.status === 304) {
      if (checked.size > MAX_CHECKED) checked.clear();
      checked.set(request.url, Date.now());
    } else {
      checked.delete(request.url);
    }
    return response;
  });
}

self.addEventListener('message', (event) => {
  if (event.data === 'ninaivu:forget') {
    checked.clear();
    event.waitUntil((async () => {
      const names = await caches.keys();
      await Promise.all(names.filter((n) => n.startsWith('ninaivu-'))
        .map((n) => caches.delete(n)));
    })());
  }
});

async function trim(cacheName, limit) {
  const cache = await caches.open(cacheName);
  const keys = await cache.keys();
  // Oldest first: Cache Storage preserves insertion order.
  for (let i = 0; i < keys.length - limit; i += 1) {
    await cache.delete(keys[i]);
  }
}

/** For content addressed by a version in its URL: ask the network once, ever. */
async function cacheFirst(request, cacheName, limit) {
  const cache = await caches.open(cacheName);
  const hit = await cache.match(request);
  if (hit) return hit;

  const response = await fetch(request).catch(() => null);
  // Only ever store a plain success. An opaque, partial or error response
  // cached here would be served back as though it were the real thing.
  if (response && response.status === 200 && response.type === 'basic') {
    await cache.put(request, response.clone());
    if (limit) trim(cacheName, limit);
  }
  return response || new Response('', { status: 504, statusText: 'Offline' });
}

/**
 * A fingerprinted file (/static/js/app.js?v=…) never changes under its URL, so
 * the copy kept is the answer; the copies of its earlier versions are let go
 * as the new one is stored, or every update would add a full set.
 *
 * A bare one (./grid.js, as app.js imports it) is asked of the server every
 * time, and the copy kept is only for when there is no server. Answered from
 * the cache first, the first visit after an update ran the new app.js against
 * last week's modules. The server sends these as `no-cache` with an ETag, so
 * an unchanged file costs a 304, not a download.
 */
async function staticFile(request, url) {
  if (url.searchParams.has('v')) {
    const cache = await caches.open(SHELL_CACHE);
    const hit = await cache.match(request);
    if (hit) return hit;
    const response = await fetch(request).catch(() => null);
    if (response && response.status === 200 && response.type === 'basic') {
      const older = await cache.keys(request, { ignoreSearch: true });
      await Promise.all(older.map((key) => cache.delete(key)));
      await cache.put(request, response.clone());
    }
    return response || new Response('', { status: 504, statusText: 'Offline' });
  }
  return networkFirst(request, SHELL_CACHE);
}

/**
 * Whether the home page just came from the cache because the server could not
 * be reached. The shell is then running offline, and its modules are taken
 * from the cache at once rather than each waiting out the same unreachable
 * server. Forgotten with the worker; it then starts out asking the server.
 */
let serverAway = false;

async function networkFirst(request, cacheName) {
  const cache = await caches.open(cacheName);
  if (serverAway) {
    const hit = await cache.match(request);
    if (hit) return hit;
  }
  try {
    const response = await fetch(request);
    if (response && response.status === 200 && response.type === 'basic') {
      await cache.put(request, response.clone());
    }
    return response;
  } catch {
    return (await cache.match(request)) || new Response('', { status: 504, statusText: 'Offline' });
  }
}

/** What a visit to the home page gets when the server cannot be reached. */
function offlinePage() {
  return new Response(
    '<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><title>Ninaivu — Offline</title><style>body{background:#141210;color:#efe9e0;font-family:system-ui,-apple-system,sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;padding:24px;box-sizing:border-box;text-align:center}.card{max-width:320px}h1{font-size:20px;margin-bottom:8px}p{color:#b1a79b;font-size:14px;line-height:1.5}</style></head><body><div class="card"><h1>Ninaivu is Offline</h1><p>Check your Wi-Fi or network connection to reconnect to your library.</p></div>' +
    // Asks the server itself every few seconds and comes back the moment it
    // answers. The worker is let go first: when it is the worker that cannot
    // reach the server (a certificate the browser does not trust, which a page
    // the person clicked through can still reach), it would show this page
    // again on every visit while Ninaivu is up.
    '<script>(function(){function ask(){fetch("/healthz",{cache:"no-store"}).then(function(){' +
    'var sw=navigator.serviceWorker;(sw?sw.getRegistrations():Promise.resolve([])).then(function(all){' +
    'return Promise.all(all.map(function(r){return r.unregister();}));}).catch(function(){}).then(function(){location.reload();});' +
    '}).catch(function(){setTimeout(ask,3000);});}ask();})();</script></body></html>',
    { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8' } }
  );
}

async function networkFirstNavigation(request, cacheName) {
  const cache = await caches.open(cacheName);
  try {
    const networkResponse = await fetch(request);
    serverAway = false;
    if (networkResponse && networkResponse.status === 200 && networkResponse.type === 'basic') {
      cache.put(request, networkResponse.clone());
    }
    return networkResponse;
  } catch {
    serverAway = true;
    const hit = await cache.match(request) || await cache.match('/');
    if (hit) return hit;
    return offlinePage();
  }
}

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;

  let url;
  try {
    url = new URL(request.url);
  } catch {
    return;
  }
  if (url.origin !== self.location.origin) return;

  // Range requests are how video seeking works; a cache must not answer one.
  if (request.headers.has('range')) return;

  // The console: the network every time, and the offline page when there is
  // none. Nothing is stored, so nothing from management outlives the visit.
  if (CONSOLE) {
    if (url.pathname === '/' && request.mode === 'navigate') {
      event.respondWith(fetch(request).catch(() => offlinePage()));
    }
    return;
  }

  // Only the home page is an offline shell. Navigating to media, shares or
  // an API URL must never store that response or substitute the home page.
  if (url.pathname === '/' && request.mode === 'navigate') {
    event.respondWith(networkFirstNavigation(request, SHELL_CACHE));
    return;
  }

  if (url.pathname.startsWith('/static/')) {
    event.respondWith(staticFile(request, url));
    return;
  }

  // Media must recheck access after an admin changes dates or date ranges.
  // A thumbnail is revalidated rather than fetched again: the server checks
  // access before it answers 304, and the picture is not sent twice — and
  // not again within minutes of the last answer (see thumbnail()). The
  // rest of /api/ is never stored at all.
  if (url.pathname.startsWith('/api/thumb/')) {
    event.respondWith(thumbnail(request));
    return;
  }
  // Originals are never stored. The rest is asked about every time, but asked
  // with what the browser already has: a gallery layout or a viewing copy
  // that has not changed comes back as a 304 rather than in full. `no-store`
  // here sent the whole layout of the library on every visit — nothing at
  // home, seconds through Tailscale. The server still decides: what it marks
  // no-store is not kept, and a listing's tag names who it was for, so the
  // next person on the tablet is never answered from the last one's.
  if (url.pathname.startsWith('/api/')) {
    const original = /^\/api\/(file|download|proxy)\//.test(url.pathname);
    event.respondWith(fetch(request, { cache: original ? 'no-store' : 'no-cache' }));
  }
  // Everything else is left alone deliberately — see the note at the top.
});
