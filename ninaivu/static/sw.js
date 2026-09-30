/**
 * Offline shell for the family app. Media always rechecks server access.
 *
 * WHAT THIS MAY CACHE, AND WHY THE LIST IS SHORT
 *
 * Only the app shell is stored. Thumbnails and originals recheck access on
 * every request, because file dates and role date ranges can change.
 *
 * Everything else goes to the network every time. In particular:
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
 * The caches are versioned. Changing CACHE_VERSION drops every old one on the
 * next activation, which is how a stale app shell gets cleaned up.
 */

const CACHE_VERSION = 'v10-faces';
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
self.addEventListener('message', (event) => {
  if (event.data === 'ninaivu:forget') {
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

/** Serve from cache, and refresh in the background for next time. */
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

async function staleWhileRevalidate(request, cacheName, limit) {
  const cache = await caches.open(cacheName);
  const hit = await cache.match(request);

  const network = fetch(request).then(async (response) => {
    // Only ever store a plain success. An opaque, partial or error response
    // cached here would be served back as though it were the real thing.
    if (response && response.status === 200 && response.type === 'basic') {
      await cache.put(request, response.clone());
      if (limit) trim(cacheName, limit);
    }
    return response;
  }).catch(() => null);

  if (hit) return hit;
  const fresh = await network;
  if (fresh) return fresh;
  return new Response('', { status: 504, statusText: 'Offline' });
}

async function networkFirstNavigation(request, cacheName) {
  const cache = await caches.open(cacheName);
  try {
    const networkResponse = await fetch(request);
    if (networkResponse && networkResponse.status === 200 && networkResponse.type === 'basic') {
      cache.put(request, networkResponse.clone());
    }
    return networkResponse;
  } catch {
    const hit = await cache.match(request) || await cache.match('/');
    if (hit) return hit;
    return new Response(
      '<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><title>Ninaivu — Offline</title><style>body{background:#12161c;color:#e6e8eb;font-family:system-ui,-apple-system,sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;padding:24px;box-sizing:border-box;text-align:center}.card{max-width:320px}h1{font-size:20px;margin-bottom:8px}p{color:#8b949e;font-size:14px;line-height:1.5}</style></head><body><div class="card"><h1>Ninaivu is Offline</h1><p>Check your Wi-Fi or network connection to reconnect to your library.</p></div></body></html>',
      { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8' } }
    );
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

  // Only the home page is an offline shell. Navigating to media, shares or
  // an API URL must never store that response or substitute the home page.
  if (url.pathname === '/' && request.mode === 'navigate') {
    event.respondWith(networkFirstNavigation(request, SHELL_CACHE));
    return;
  }

  if (url.pathname.startsWith('/static/')) {
    event.respondWith(staleWhileRevalidate(request, SHELL_CACHE, 0));
    return;
  }

  // Media must recheck access after an admin changes dates or date ranges.
  if (url.pathname.startsWith('/api/')) {
    event.respondWith(fetch(request, { cache: 'no-store' }));
  }
  // Everything else is left alone deliberately — see the note at the top.
});
