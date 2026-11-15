/**
 * Blurred previews for the gallery's tiles: so that no tile is ever empty,
 * however fast the scroll.
 *
 * A thumbnail is a request of its own, a round trip each; on a phone far from
 * home (a third of a second a round trip, six requests at a time) a flick ran
 * past a hundred tiles a second and most of them were still their one colour
 * when they went by. A preview is a couple of dozen bytes, and the previews
 * of a few hundred tiles come in one request (/api/placeholders), asked for
 * well ahead of the scroll: by the time a tile is made, its preview is here,
 * and it shows the photograph's own shapes and colours, blurred, until the
 * picture itself lands over it.
 *
 * Fetched and drawn in a worker (placeholder-worker.js), so a block of a few
 * hundred costs the page nothing; on the page itself, a few at a time when
 * idle, where there is no worker.
 */
import { readAnswer } from './placeholder-codec.js';

/** Ids asked for in one request. */
export const BLOCK = 256;
/** Requests on their way at once: they share the six connections the
 *  thumbnails use, which must not wait behind them. */
const IN_FLIGHT = 2;
/** Previews kept drawn (about 1.3 KB each). The rest are asked for again,
 *  cheaply, if the scroll comes back to them. */
export const KEEP = 6000;

export class Placeholders {
  /**
   * *onReady(ids)* is told which ids have just got their previews.
   * *onConfirmed(ids)* which the server has just said this viewer may see.
   */
  constructor({ onReady = () => {}, onConfirmed = () => {} } = {}) {
    this.onReady = onReady;
    this.onConfirmed = onConfirmed;
    this.urls = new Map();          // id -> data: URL, or '' for none
    this.pending = new Set();       // ids on their way
    this.queue = [];
    this.active = 0;
    this.seq = 0;
    this.waiting = new Map();       // seq -> ids
    this.worker = undefined;
    this.failures = 0;
    this.restUntil = 0;
  }

  /** The preview of *id*: a data: URL, '' when it has none, undefined when
   *  it has not been asked for yet. */
  get(id) {
    return this.urls.get(id);
  }

  /**
   * The ids wanted next, nearest first. Replaces what was queued: a scroll
   * that turns round wants the other way first. What is on its way lands.
   */
  want(ids) {
    if (this.restUntil && performance.now() < this.restUntil) return;
    this.queue = [];
    for (const id of ids) {
      if (!this.urls.has(id) && !this.pending.has(id)) this.queue.push(id);
    }
    this.pump();
  }

  pump() {
    while (this.active < IN_FLIGHT && this.queue.length) {
      const ids = [];
      while (ids.length < BLOCK && this.queue.length) {
        const id = this.queue.shift();
        if (this.urls.has(id) || this.pending.has(id)) continue;
        ids.push(id);
      }
      if (!ids.length) return;
      ids.forEach((id) => this.pending.add(id));
      this.active += 1;
      this.request(ids);
    }
  }

  request(ids) {
    const worker = this.ensureWorker();
    if (worker) {
      const seq = ++this.seq;
      this.waiting.set(seq, ids);
      worker.postMessage({ seq, ids });
      return;
    }
    fetch(`/api/placeholders?ids=${ids.join(',')}`, { credentials: 'same-origin', priority: 'high' })
      .then((answer) => {
        if (!answer.ok) throw new Error(String(answer.status));
        return answer.arrayBuffer();
      })
      .then((buffer) => new Promise((resolve) => {
        // Drawn a few at a time when the page has a moment: a whole block at
        // once was a dropped frame or two on a phone.
        const idle = typeof requestIdleCallback === 'function'
          ? (fn) => requestIdleCallback(fn, { timeout: 200 }) : (fn) => setTimeout(fn, 16);
        const bytes = new Uint8Array(buffer);
        const out = [];
        let at = 0;
        let i = 0;
        const step = () => {
          const until = performance.now() + 4;
          while (i < ids.length && at < bytes.length && performance.now() < until) {
            const length = bytes[at];
            out.push(readAnswer([ids[i]], bytes.slice(at, at + 1 + length))[0]);
            at += 1 + length;
            i += 1;
          }
          if (i < ids.length && at < bytes.length) idle(step);
          else resolve(out);
        };
        step();
      }))
      .then((urls) => this.land(ids, urls), () => this.land(ids, null));
  }

  ensureWorker() {
    if (this.worker !== undefined) return this.worker;
    this.worker = null;
    if (typeof Worker !== 'function') return null;
    try {
      const worker = new Worker(new URL('./placeholder-worker.js', import.meta.url), { type: 'module' });
      worker.addEventListener('message', (event) => {
        const { seq, urls } = event.data || {};
        const ids = this.waiting.get(seq);
        if (!ids) return;
        this.waiting.delete(seq);
        this.land(ids, urls || null);
      });
      worker.addEventListener('error', () => {
        // A worker that will not start (an old browser, a blocked script):
        // the page draws them itself from here on.
        this.worker = null;
        const stranded = [...this.waiting.values()];
        this.waiting.clear();
        worker.terminate();
        // Each keeps the place it already holds among those on their way.
        for (const ids of stranded) this.request(ids);
      });
      this.worker = worker;
    } catch {
      this.worker = null;
    }
    return this.worker;
  }

  land(ids, urls) {
    this.active -= 1;
    ids.forEach((id) => this.pending.delete(id));
    if (!urls) {
      // Not answered (offline, signed out, an older server without the
      // route): the tiles keep their colour, and nothing more is asked for a
      // little while rather than on every frame.
      this.failures += 1;
      if (this.failures >= 3) {
        this.restUntil = performance.now() + 30000;
        this.queue = [];
        this.failures = 0;
      }
      this.pump();
      return;
    }
    this.failures = 0;
    const ready = [];
    const confirmed = [];
    ids.forEach((id, i) => {
      const url = urls[i] || '';
      this.urls.delete(id);
      this.urls.set(id, url);
      if (url) {
        ready.push(id);
        confirmed.push(id);
      }
    });
    if (this.urls.size > KEEP) {
      let extra = this.urls.size - KEEP;
      for (const id of this.urls.keys()) {
        if (extra-- <= 0) break;
        this.urls.delete(id);
      }
    }
    if (ready.length) this.onReady(ready);
    if (confirmed.length) this.onConfirmed(confirmed);
    this.pump();
  }

  /** Everything forgotten: a different viewer, a new library. */
  reset() {
    this.urls.clear();
    this.queue = [];
    this.failures = 0;
    this.restUntil = 0;
  }

  destroy() {
    this.queue = [];
    this.worker?.terminate?.();
    this.worker = null;
    this.waiting.clear();
  }
}
