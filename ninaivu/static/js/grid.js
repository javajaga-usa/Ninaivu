/*
 * Adaptive preload facade.
 *
 * grid-base.js is the proven virtualised grid implementation from main.
 * This subclass changes only prediction, preloading and sharpening order so
 * the rest of gallery behaviour remains byte-for-byte compatible.
 */

import { Grid as BaseGrid, mergeSegments } from './grid-base.js';
import {
  AdaptivePreloader,
  connectionProfile,
  interleaveDirectional,
  predictionMs,
  FLING_PX_MS,
} from './preload-policy.js';

const SETTLE_MS = 140;
const WARM_EVERY_MS = 160;
const MAX_WARM_SCREENS = 3;
const OVERSCAN = 900;
const TRAILING = 300;

function now() {
  return performance.now();
}

export class Grid extends BaseGrid {
  constructor(scroller, container) {
    super(scroller, container);
    this.directionConfidence = 0;
    this.lastInstantDirection = this.direction || 1;
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

  getPreloadMetrics() {
    return this.prefetcher.snapshot();
  }

  measureSpeed() {
    const at = now();
    const top = this.scroller.scrollTop;
    const elapsed = at - this.lastTime;
    let instant = 0;
    if (this.lastTime && elapsed > 0 && elapsed < 250) {
      instant = (top - this.lastTop) / elapsed;
      this.velocity = this.velocity * 0.75 + instant * 0.25;
    } else {
      this.velocity = 0;
    }

    const instantDirection = instant < -0.12 ? -1 : instant > 0.12 ? 1 : this.direction;
    const reversed = instantDirection !== this.direction && Math.abs(instant) > 0.12;
    if (reversed) {
      this.direction = instantDirection;
      this.directionConfidence = 1;
      // Drop work aimed at the old direction immediately. The controller lets
      // requests that have probably reached their last mile finish.
      this.prefetcher.want([], { cancel: true, priority: 'warm' });
    } else {
      this.direction = instantDirection;
      this.directionConfidence = Math.min(1, this.directionConfidence + 0.15);
    }
    this.lastInstantDirection = instantDirection;
    this.lastTop = top;
    this.lastTime = at;
    this.fast = Math.abs(instant || this.velocity) > FLING_PX_MS;

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
    const ahead = Math.min(lead + speed * prediction, lead + view * MAX_WARM_SCREENS);
    const behind = this.fast ? TRAILING : OVERSCAN;
    const top = this.scroller.scrollTop;
    return this.direction >= 0
      ? [top - behind, top + view + ahead]
      : [top - ahead, top + view + behind];
  }

  warmAhead(at, reversed = false) {
    const profile = this.profile();
    if (this.fast || profile.lean) {
      this.prefetcher.want([], { cancel: this.fast, priority: 'warm' });
      return;
    }
    if (!reversed && at - this.lastWarm < WARM_EVERY_MS) return;
    this.lastWarm = at;

    const view = this.scroller.clientHeight;
    const predicted = Math.abs(this.velocity) * predictionMs(this.velocity);
    const reach = Math.min(view * MAX_WARM_SCREENS, Math.max(view, predicted));
    const [top, bottom] = this.band();
    const urls = this.direction >= 0
      ? this.unmountedUrls(bottom, bottom + reach)
      : this.unmountedUrls(top - reach, top).reverse();
    this.prefetcher.want(urls, { cancel: reversed, priority: 'warm' });
  }

  urlsAround(top, bottom, down = true, { aheadScreens, behindScreens } = this.profile()) {
    const view = Math.max(1, this.scroller.clientHeight);
    const height = this.layout.height || 0;
    const below = this.unmountedUrls(bottom, Math.min(height, bottom + view * aheadScreens));
    const above = this.unmountedUrls(Math.max(0, top - view * behindScreens), top).reverse();
    const [ahead, behind] = down ? [below, above] : [above, below];
    return interleaveDirectional(ahead, behind, 2);
  }

  // Keep the public helper used by the date rail / Jump To, but make it
  // viewport- and device-aware instead of a fixed 300 + 300 photographs.
  readyAround(top, bottom, down = true) {
    return this.urlsAround(top, bottom, down, this.profile());
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
    const urls = this.urlsAround(top, bottom, down, profile);
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
    // Pointer intent is strong, but still bounded by the device/network profile.
    const pointerPlan = {
      aheadScreens: Math.min(profile.aheadScreens, profile.touch ? 4 : 6),
      behindScreens: Math.min(profile.behindScreens, profile.touch ? 2 : 3),
    };
    this.prefetcher.want(this.urlsAround(top, top + view, true, pointerPlan), {
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
