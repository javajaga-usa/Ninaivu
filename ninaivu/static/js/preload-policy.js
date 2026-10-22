/* Adaptive thumbnail preloading policy for Ninaivu.
 * Kept separate from the grid so the heuristics are deterministic and easy to test.
 */

const FLING_PX_MS = 5;

export function connectionProfile({
  connection = globalThis.navigator?.connection,
  deviceMemory = globalThis.navigator?.deviceMemory,
  touch = typeof globalThis.matchMedia === 'function'
    ? globalThis.matchMedia('(hover: none)').matches : false,
} = {}) {
  const effective = connection?.effectiveType || '';
  const lean = Boolean(connection?.saveData)
    || effective === '2g' || effective === 'slow-2g';
  const lowMemory = Number(deviceMemory || 4) <= 2;
  // Unknown (Safari and Firefox say neither) counts as an ordinary device.
  const memory = Number(deviceMemory) || 0;
  const downlink = Number(connection?.downlink) || 0;
  const slowLink = effective === '3g' || (downlink > 0 && downlink < 1.5);
  // Plenty of memory on a quick link (Chrome reports at most 8 GB and
  // 10 Mbit/s, so this is the top of what a browser says).
  const strong = memory >= 8 && !slowLink && (downlink === 0 || downlink >= 5);

  if (lean) {
    return {
      lean: true, touch, lowMemory, memory, slowLink, strong: false,
      aheadScreens: 1.5, behindScreens: 0,
      concurrency: 1,
    };
  }

  let aheadScreens = touch ? 5 : 8;
  let behindScreens = touch ? 2 : 3;
  let concurrency = touch ? 2 : 3;

  if (lowMemory) {
    aheadScreens *= 0.6;
    behindScreens *= 0.5;
    concurrency = Math.min(concurrency, 2);
  }

  return {
    lean: false, touch, lowMemory, memory, slowLink, strong,
    aheadScreens, behindScreens, concurrency,
  };
}

/**
 * Thumbnails to keep ready on each side of where a scroll stopped, sized to
 * the device: so many screens of its own tiles, within what its memory and
 * link can carry. A phone shows about 30 tiles a screen and gets 8 screens
 * (about 240 each way); a big desktop window shows 60 and gets 10 (600).
 * Small pictures, about 12 KB each: 600 either way is some 14 MB.
 *
 *   - 8 GB or more on a quick link: 12 screens on a phone (600 at most),
 *     15 on a computer (1,000 at most, some 24 MB either way; the app keeps
 *     4,000 thumbnails, so both sides stay kept).
 *   - 2 GB of memory or less: 150 at most; 4 GB: 250.
 *   - A 3G link, or one under 1.5 Mbit/s: half the usual most.
 *   - Never fewer than 60, so a long flick still lands on pictures.
 */
export function prefetchCount(profile, tilesPerScreen) {
  let screens = profile.touch ? 8 : 10;
  let most = profile.touch ? 400 : 600;
  if (profile.strong) {
    screens = profile.touch ? 12 : 15;
    most = profile.touch ? 600 : 1000;
  }
  if (profile.lowMemory || (profile.memory && profile.memory <= 2)) most = 150;
  else if (profile.memory && profile.memory <= 4) most = Math.min(most, 250);
  if (profile.slowLink) most = Math.round(most / 2);
  const tiles = Math.max(1, Number(tilesPerScreen) || 0);
  return Math.max(Math.min(60, most), Math.min(most, Math.round(screens * tiles)));
}

export function predictionMs(speed) {
  const value = Math.abs(speed);
  if (value >= FLING_PX_MS) return 0;
  if (value < 0.4) return 1400;
  if (value < 1.5) return 900;
  if (value < 3) return 600;
  return 450;
}

export function interleaveDirectional(ahead, behind, aheadRatio = 2) {
  const result = [];
  let i = 0;
  let j = 0;
  while (i < ahead.length || j < behind.length) {
    for (let n = 0; n < aheadRatio && i < ahead.length; n += 1) result.push(ahead[i++]);
    if (j < behind.length) result.push(behind[j++]);
  }
  return result;
}

export function nextHopConcurrency(base) {
  if (!globalThis.performance?.getEntriesByType) return base;
  const resources = globalThis.performance.getEntriesByType('resource');
  for (let i = resources.length - 1; i >= Math.max(0, resources.length - 50); i -= 1) {
    const protocol = resources[i]?.nextHopProtocol || '';
    // One connection carries them all (Tailscale Serve, a reverse proxy):
    // more at once hides the round trip, which away from home is the cost.
    if (protocol === 'h2' || protocol === 'h3') return Math.min(8, base + 4);
  }
  return base;
}

function idleSchedule(callback) {
  if (typeof globalThis.requestIdleCallback === 'function') {
    return globalThis.requestIdleCallback(callback, { timeout: 800 });
  }
  return globalThis.setTimeout(callback, 150);
}

function idleCancel(handle) {
  if (typeof globalThis.cancelIdleCallback === 'function') globalThis.cancelIdleCallback(handle);
  else globalThis.clearTimeout(handle);
}

/** The most finished pictures remembered, however recent. */
export const DONE_LIMIT = 20000;

export class AdaptivePreloader {
  constructor({ idle = () => true, profile = () => connectionProfile() } = {}) {
    this.idle = idle;
    this.profile = profile;
    this.queue = [];
    this.active = new Map();
    this.done = new Map();
    this.idleHandle = null;
    this.metrics = {
      requested: 0,
      completed: 0,
      cancelled: 0,
      failed: 0,
      preloadHits: 0,
      visibleRequests: 0,
    };
  }

  has(url) {
    return Boolean(url) && this.done.has(url);
  }

  markVisible(urls) {
    this.metrics.visibleRequests += 1;
    if (urls.some((url) => url && this.done.has(url))) this.metrics.preloadHits += 1;
  }

  snapshot() {
    const { requested, completed, cancelled, failed, preloadHits, visibleRequests } = this.metrics;
    return {
      requested, completed, cancelled, failed, preloadHits, visibleRequests,
      active: this.active.size,
      queued: this.queue.length,
      hitRate: visibleRequests ? preloadHits / visibleRequests : 0,
      usefulRate: requested ? preloadHits / requested : 0,
    };
  }

  clearOldDone() {
    if (this.done.size <= 5000) return;
    const keepAfter = performance.now() - 10 * 60 * 1000;
    for (const [url, at] of this.done) {
      if (at < keepAfter) this.done.delete(url);
      if (this.done.size <= 3500) break;
    }
    // Fast scrolling through a large library finishes thousands of pictures
    // in ten minutes, none of them old enough to go above, and the list grew
    // for as long as the tab was open. Past a hard limit the oldest go
    // whatever their age; a Map keeps them in the order they finished.
    if (this.done.size <= DONE_LIMIT) return;
    for (const url of this.done.keys()) {
      this.done.delete(url);
      if (this.done.size <= 3500) break;
    }
  }

  want(urls, { cancel = false, idleOnly = false, priority = 'warm' } = {}) {
    const unique = [...new Set(urls.filter(Boolean))];
    const wanted = new Set(unique);

    if (cancel) {
      const now = performance.now();
      for (const [url, entry] of this.active) {
        if (wanted.has(url)) continue;
        // Let an older request finish instead of aborting it on the last mile.
        if (now - entry.startedAt > 300) continue;
        entry.controller.abort();
        this.active.delete(url);
        this.metrics.cancelled += 1;
      }
    }

    this.clearOldDone();
    this.queue = unique
      .filter((url) => !this.active.has(url) && !this.done.has(url))
      .map((url) => ({ url, priority }));

    if (this.idleHandle != null) {
      idleCancel(this.idleHandle);
      this.idleHandle = null;
    }
    if (idleOnly) {
      this.idleHandle = idleSchedule(() => {
        this.idleHandle = null;
        this.pump();
      });
    } else {
      this.pump();
    }
  }

  pump() {
    if (!this.idle()) return;
    const limit = nextHopConcurrency(this.profile().concurrency);
    while (this.active.size < limit && this.queue.length && this.idle()) {
      const { url, priority } = this.queue.shift();
      if (this.done.has(url) || this.active.has(url)) continue;
      this.start(url, priority);
    }
  }

  start(url, priority) {
    const controller = new AbortController();
    const entry = { controller, startedAt: performance.now(), priority };
    this.active.set(url, entry);
    this.metrics.requested += 1;

    fetch(url, {
      signal: controller.signal,
      credentials: 'same-origin',
      cache: 'force-cache',
      priority: 'low',
    }).then(async (response) => {
      if (!response.ok) throw new Error(`thumbnail preload ${response.status}`);
      // Consume the body so the HTTP cache has a complete reusable response.
      await response.blob();
      this.done.set(url, performance.now());
      this.metrics.completed += 1;
    }).catch((error) => {
      if (error?.name !== 'AbortError') this.metrics.failed += 1;
    }).finally(() => {
      const current = this.active.get(url);
      if (current === entry) this.active.delete(url);
      this.pump();
    });
  }

  destroy() {
    if (this.idleHandle != null) idleCancel(this.idleHandle);
    this.idleHandle = null;
    this.queue = [];
    for (const entry of this.active.values()) entry.controller.abort();
    this.active.clear();
  }
}

export { FLING_PX_MS };
