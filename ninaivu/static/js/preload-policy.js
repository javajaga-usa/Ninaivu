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

  if (lean) {
    return {
      lean: true, touch, lowMemory,
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
    lean: false, touch, lowMemory,
    aheadScreens, behindScreens, concurrency,
  };
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
    if (protocol === 'h2' || protocol === 'h3') return Math.min(6, Math.max(base, base + 2));
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
