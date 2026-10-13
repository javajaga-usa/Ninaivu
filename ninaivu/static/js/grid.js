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

const WARM_EVERY_MS = 160;
// A turn-round counts once it is this fast (px/ms) or has lasted two frames;
// one frame of trackpad wobble no longer throws away pictures on their way.
const REVERSE_PX_MS = 0.5;

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
  }

  prefetchAround(y) {
    const profile = this.profile();
    if (this.scrolling || profile.lean) return;
    const view = this.scroller.clientHeight;
    const top = Math.max(0, Math.min(y, Math.max(0, (this.layout.height || 0) - view)));
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
    return super.paintCell(node, cell);
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
    // Base destroy calls prefetcher.want(); keep a harmless compatible object
    // for that final cleanup path after the adaptive controller is gone.
    this.prefetcher = { want() {}, pump() {} };
    super.destroy();
  }
}

export { mergeSegments };
