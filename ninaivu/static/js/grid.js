/*
 * The gallery grid: grid-base.js draws and recycles the tiles; this layer
 * decides what to fetch ahead and in what order (preload-policy.js).
 *
 *  - While scrolling: the next stretch the way it is going, further the slower
 *    it goes; nothing queued during a fling, and what is on its way may land.
 *  - On stopping: the nearest 300 thumbnails each way (PREFETCH_COUNT), two
 *    ahead for every one behind; the same around the date rail and Jump to.
 *  - Fetches can be called off when the scroll really turns round, go two to
 *    three at a time (more over HTTP/2) and only while on-screen tiles have
 *    their pictures.
 *  - Sharpening after a stop starts with the tiles in the middle of the screen.
 *  - Every tile has a blurred preview of its photograph before the photograph
 *    itself (placeholder.js), asked for a few hundred at a time and further
 *    ahead the faster the scroll: on a slow link a fling outruns the pictures,
 *    but never the previews, so no tile goes by empty.
 */

import {
  Grid as BaseGrid, mergeSegments,
  OVERSCAN, TRAILING, SETTLE_MS, PREFETCH_COUNT, MAX_AHEAD_SCREENS,
} from './grid-base.js';
import {
  AdaptivePreloader,
  connectionProfile,
  interleaveDirectional,
  predictionMs,
  FLING_PX_MS,
} from './preload-policy.js';
import { visibleRange } from './layout.js';
import { Placeholders } from './placeholder.js';

const WARM_EVERY_MS = 160;
// A turn-round counts once it is this fast (px/ms) or has lasted two frames;
// one frame of trackpad wobble no longer throws away pictures on their way.
const REVERSE_PX_MS = 0.5;
/** How often, at most, the previews wanted are worked out again. */
const PREVIEW_EVERY_MS = 100;
/** Previews are asked for this far ahead, in time: two round trips on a slow
 *  link and the drawing, so a fling meets them already here… */
const PREVIEW_AHEAD_MS = 2500;
/** …and at least this many screens ahead, and never more than this. */
const PREVIEW_MIN_SCREENS = 4;
const PREVIEW_MAX_SCREENS = 40;
/** Once a scroll stops: this many screens on, and this many back. */
const PREVIEW_REST_AHEAD = 12;
const PREVIEW_REST_BEHIND = 6;

function now() {
  return performance.now();
}

export class Grid extends BaseGrid {
  constructor(scroller, container) {
    super(scroller, container);
    this.turning = 0;
    this.sharpenQueue = new Map();
    this.sharpenFrame = null;
    this.prefetcher = new AdaptivePreloader({
      idle: () => !this.tilesLoading(),
      profile: () => connectionProfile(),
    });
    this.previewAt = 0;
    this.previewCells = new Map();      // id -> cell, for the ids asked about
    this.previews = new Placeholders({
      onReady: (ids) => this.showPreviews(ids),
      onConfirmed: (ids) => this.confirmThumbs(ids),
    });
  }

  profile() {
    return connectionProfile();
  }

  measureSpeed() {
    const at = now();
    const top = this.scroller.scrollTop;
    const elapsed = at - this.lastTime;
    let instant = 0;
    if (this.lastTime && elapsed > 0 && elapsed < 250) {
      instant = (top - this.lastTop) / elapsed;
      // The base grid's weighting: quick to notice a speed-up.
      this.velocity = this.velocity * 0.4 + instant * 0.6;
    } else {
      this.velocity = 0;
    }

    const way = instant < -0.12 ? -1 : instant > 0.12 ? 1 : this.direction;
    let reversed = false;
    if (way !== this.direction) {
      this.turning += 1;
      if (Math.abs(instant) > REVERSE_PX_MS || this.turning >= 2) {
        this.direction = way;
        this.turning = 0;
        reversed = true;
        // Drop work aimed the old way; requests near their end still land.
        this.prefetcher.want([], { cancel: true, priority: 'warm' });
      }
    } else {
      this.turning = 0;
    }
    this.lastTop = top;
    this.lastTime = at;
    this.fast = Math.abs(this.velocity) > FLING_PX_MS;

    if (!this.scrolling) {
      this.scrolling = true;
      this.container.classList.add('scrolling', 'no-hover');
    }
    clearTimeout(this.settleTimer);
    this.settleTimer = setTimeout(() => this.settle(), SETTLE_MS);
    this.warmAhead(at, reversed);
  }

  band() {
    const view = this.scroller.clientHeight;
    const speed = Math.abs(this.velocity);
    const lead = Math.max(OVERSCAN, view);
    const prediction = predictionMs(speed);
    const ahead = Math.min(lead + speed * prediction, lead + view * MAX_AHEAD_SCREENS);
    const behind = this.fast ? TRAILING : OVERSCAN;
    const top = this.scroller.scrollTop;
    return this.direction >= 0
      ? [top - behind, top + view + ahead]
      : [top - ahead, top + view + behind];
  }

  warmAhead(at, reversed = false) {
    // Dragging the date rail: nothing ahead of a place it will not stay.
    if (this.scrubbing) return;
    const profile = this.profile();
    if (this.fast || profile.lean) {
      // Nothing queued is where a fling is going; what is on its way may land.
      this.prefetcher.want([], { priority: 'warm' });
      return;
    }
    if (!reversed && at - this.lastWarm < WARM_EVERY_MS) return;
    this.lastWarm = at;

    const view = this.scroller.clientHeight;
    const predicted = Math.abs(this.velocity) * predictionMs(this.velocity);
    const reach = Math.min(view * MAX_AHEAD_SCREENS, Math.max(view, predicted));
    const [top, bottom] = this.band();
    const urls = this.direction >= 0
      ? this.unmountedUrls(bottom, bottom + reach)
      : this.unmountedUrls(top - reach, top).reverse();
    this.prefetcher.want(urls, { cancel: reversed, priority: 'warm' });
  }

  /**
   * The thumbnails to keep ready around a stretch of the grid. Counted in
   * photographs, not screens: the nearest PREFETCH_COUNT (300) below and the
   * nearest 300 above, two the way it is going for every one behind, so big
   * tiles get as many as small ones. A lean link (Data Saver, 2G) gets the
   * profile's screen-sized plan: a screen and a half on, nothing behind.
   */
  readyAround(top, bottom, down = true, profile = this.profile()) {
    if (profile.lean) {
      const view = Math.max(1, this.scroller.clientHeight);
      return down
        ? this.unmountedUrls(bottom, bottom + view * profile.aheadScreens)
        : this.unmountedUrls(Math.max(0, top - view * profile.aheadScreens), top).reverse();
    }
    const count = profile.lowMemory ? PREFETCH_COUNT / 2 : PREFETCH_COUNT;
    const below = this.nearestUrls(bottom, 1, count);
    const above = this.nearestUrls(top, -1, count);
    const [ahead, behind] = down ? [below, above] : [above, below];
    return interleaveDirectional(ahead, behind, 2);
  }

  settle() {
    const profile = this.profile();
    this.velocity = 0;
    this.lastTime = 0;
    this.lastWarm = 0;
    this.fast = false;
    this.scrolling = false;
    this.container.classList.remove('scrolling');

    // render() calls sharpen(); our override batches those calls center-first.
    this.render();

    const [top, bottom] = this.band();
    const down = this.direction >= 0;
    const urls = this.readyAround(top, bottom, down, profile);
    this.prefetcher.want(urls, {
      cancel: true,
      idleOnly: !profile.lean,
      priority: 'speculative',
    });
    this.wantPreviews({ resting: true });
  }

  prefetchAround(y) {
    const profile = this.profile();
    if (this.scrolling) return;
    const view = this.scroller.clientHeight;
    const top = Math.max(0, Math.min(y, Math.max(0, (this.layout.height || 0) - view)));
    // The previews first, even on a lean link: a few kilobytes, and the jump
    // then lands on the photographs' shapes rather than on bare colour.
    this.wantPreviews({ resting: true, at: top });
    if (profile.lean) return;
    // The date rail under the mouse, a month in Jump to: the same 300 each way.
    this.prefetcher.want(this.readyAround(top, top + view, true, profile), {
      cancel: true,
      idleOnly: true,
      priority: 'speculative',
    });
  }

  paintCell(node, cell) {
    const full = this.cellThumbUrl(cell);
    const small = this.cellThumbUrl(cell, true);
    this.prefetcher.markVisible([full, small]);
    const painted = super.paintCell(node, cell);
    this.applyPreview(node, cell);
    return painted;
  }

  render() {
    super.render();
    this.wantPreviews();
  }

  /**
   * The tile's blurred preview behind its <img>, until the picture covers it
   * (the picture fades in over it). None for a tile already holding its
   * picture, nor for one with no thumbnail, which shows its sign instead.
   */
  applyPreview(node, cell) {
    const img = node.firstElementChild;
    const url = (cell.flags & 2) !== 0 && img && !img.hidden && !img.classList.contains('ready')
      ? this.previews.get(cell.id) : '';
    const shows = url ? cell.id : null;
    if (node.previewOf === shows) return;
    node.previewOf = shows;
    node.style.backgroundImage = url ? `url("${url}")` : '';
  }

  /** Previews that have just arrived, onto the tiles already made for them. */
  showPreviews(ids) {
    const arrived = new Set(ids);
    const cells = this.layout.cells;
    for (const [n, node] of this.mounted) {
      const cell = cells[n];
      if (cell && arrived.has(cell.id) && node.dataset.id === String(cell.id)) this.applyPreview(node, cell);
    }
  }

  /**
   * The previews to have ready: the screen, then the stretch the scroll is
   * heading into, as far as two round trips at its speed reach (at least a
   * few screens), and a little behind. Once it has stopped, further both ways.
   * A preview is a couple of dozen bytes, so a fling, which fetches no
   * pictures ahead at all, still has them asked for well before it arrives.
   */
  wantPreviews({ resting = false, at: place = null } = {}) {
    const at = now();
    if (!resting && at - this.previewAt < PREVIEW_EVERY_MS) return;
    this.previewAt = at;
    const shown = this.shownView.height ? this.shownView : this.viewport();
    const view = shown.height;
    const top = place ?? shown.top;
    if (!view || !this.layout.cells.length) return;
    const profile = this.profile();
    let ahead;
    let behind;
    if (this.scrubbing) {
      // The date rail held: the screen under it and one either side.
      ahead = view;
      behind = view;
    } else if (resting) {
      ahead = view * (profile.lean ? 3 : PREVIEW_REST_AHEAD);
      behind = view * (profile.lean ? 1 : PREVIEW_REST_BEHIND);
    } else {
      ahead = Math.min(view * PREVIEW_MAX_SCREENS,
        Math.max(view * PREVIEW_MIN_SCREENS, Math.abs(this.velocity) * PREVIEW_AHEAD_MS));
      behind = view * 2;
    }
    const down = this.direction >= 0;
    const from = down ? top - behind : top - ahead;
    const to = down ? top + view + ahead : top + view + behind;
    const bottom = top + view;
    // Nearest first, the way it is going before the way it came.
    const distance = (cell) => {
      if (cell.y + cell.h > top && cell.y < bottom) return -1;
      const gap = cell.y >= bottom ? cell.y - bottom : top - (cell.y + cell.h);
      const behindIt = down ? cell.y < top : cell.y >= bottom;
      return behindIt ? gap * 3 : gap;
    };
    const cells = visibleRange(this.layout, from, to).cells
      .filter((cell) => (cell.flags & 2) !== 0 && this.previews.get(cell.id) === undefined)
      .map((cell) => [distance(cell), cell])
      .sort((a, b) => a[0] - b[0]);
    if (this.previewCells.size > 20000) this.previewCells.clear();
    const ids = [];
    for (const [, cell] of cells) {
      ids.push(cell.id);
      this.previewCells.set(cell.id, cell);
    }
    this.previews.want(ids);
  }

  /**
   * The server has just said, in answering for these previews, that this
   * viewer may see these items: the service worker can show their thumbnails
   * from the browser's own copy without asking the server about each one
   * again (static/sw.js), which on a slow link was a round trip per tile even
   * for pictures it already had.
   */
  confirmThumbs(ids) {
    const worker = navigator.serviceWorker?.controller;
    if (!worker) return;
    const urls = [];
    for (const id of ids) {
      const cell = this.previewCells.get(id);
      if (!cell) continue;
      const full = this.cellThumbUrl(cell);
      const small = this.cellThumbUrl(cell, true);
      if (full) urls.push(new URL(full, location.href).href);
      if (small && small !== full) urls.push(new URL(small, location.href).href);
    }
    if (urls.length) worker.postMessage({ type: 'ninaivu:checked', urls });
  }

  swapIn(node, cell, url) {
    this.prefetcher.markVisible([url]);
    return super.swapIn(node, cell, url);
  }

  sharpen(node, cell) {
    if (this.scrolling) return;
    const img = node.querySelector('img');
    const full = this.cellThumbUrl(cell);
    if (!full || !img || img.hidden || img.getAttribute('src') === full) return;

    const { top, height } = this.viewport();
    if (cell.y + cell.h < top - height / 2 || cell.y > top + height * 1.5) return;
    this.sharpenQueue.set(cell.n, { node, cell, full });
    if (this.sharpenFrame != null) return;
    this.sharpenFrame = requestAnimationFrame(() => this.flushSharpen());
  }

  flushSharpen() {
    this.sharpenFrame = null;
    if (this.scrolling || !this.sharpenQueue.size) {
      this.sharpenQueue.clear();
      return;
    }

    const { top, height } = this.viewport();
    const center = top + height / 2;
    const jobs = [...this.sharpenQueue.values()];
    this.sharpenQueue.clear();
    jobs.sort((a, b) => {
      const aCenter = a.cell.y + a.cell.h / 2;
      const bCenter = b.cell.y + b.cell.h / 2;
      const aOn = a.cell.y + a.cell.h > top && a.cell.y < top + height;
      const bOn = b.cell.y + b.cell.h > top && b.cell.y < top + height;
      if (aOn !== bOn) return aOn ? -1 : 1;
      return Math.abs(aCenter - center) - Math.abs(bCenter - center);
    });

    const visible = jobs.filter(({ cell }) => cell.y + cell.h > top && cell.y < top + height);
    const nearby = jobs.filter(({ cell }) => !(cell.y + cell.h > top && cell.y < top + height));
    for (const { node, cell } of visible) BaseGrid.prototype.sharpen.call(this, node, cell);

    const finishNearby = () => {
      if (this.scrolling) return;
      for (const { node, cell } of nearby) BaseGrid.prototype.sharpen.call(this, node, cell);
    };
    if (nearby.length) {
      if (typeof requestIdleCallback === 'function') requestIdleCallback(finishNearby, { timeout: 500 });
      else setTimeout(finishNearby, 80);
    }
  }

  destroy() {
    if (this.sharpenFrame != null) cancelAnimationFrame(this.sharpenFrame);
    this.sharpenQueue.clear();
    this.prefetcher.destroy();
    this.previews.destroy();
    // Base destroy calls prefetcher.want(); keep a harmless compatible object
    // for that final cleanup path after the adaptive controller is gone.
    this.prefetcher = { want() {}, pump() {} };
    super.destroy();
  }
}

export { mergeSegments };
