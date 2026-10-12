/**
 * Virtualised gallery grid.
 *
 * Only the cells intersecting the viewport (plus an overscan margin) exist in
 * the DOM. Nodes are recycled through a pool, positions are applied with
 * `transform`, and scroll handling is coalesced into a single rAF per frame,
 * so scrolling a 100k-item library costs the same as scrolling 200 items.
 */

import { computeLayout, visibleRange, sectionAt, targetSize } from './layout.js';
import { thumbUrl } from './api.js';
import * as i18n from './i18n.js';
import {
  LIVE_ICON, liveUrls, liveVideo, mayAutoplay, releaseVideo,
} from './live.js';

const OVERSCAN = 900;      // px rendered above and below the viewport
/** Tiles kept, with their pictures, after they leave the screen: scrolling
 *  back to them is instant, with nothing to fetch or decode. More on a
 *  computer; a phone or tablet has less memory to keep them in. */
const POOL_LIMIT = (typeof matchMedia === 'function' && matchMedia('(hover: none)').matches)
  ? 600 : 1200;
/** How long a thumbnail may take before the tile admits to loading. */
const SHIMMER_DELAY = 120;

// Scrolling fast. The band of tiles kept ready grows ahead of the scroll with
// its speed, so a flick finds its pictures already asked for, and shrinks
// behind it, where nothing is going to be looked at.
/** How far ahead, in time, the band reaches: speed × this. */
const LOOKAHEAD_MS = 450;
/** …but never more than this many screens. */
const MAX_AHEAD_SCREENS = 3;
/** The band left behind a fast scroll. */
const TRAILING = 300;
/** Faster than this (px/ms: 5,000 px a second) is flying past rather than
 *  looking: nothing is fetched ahead, and tiles left behind are let go. */
const FLING = 5;
/** A picture arriving within this of its tile being made is shown at once,
 *  with no fade: the tile was never seen empty, so there is nothing to ease
 *  from. A fade on every tile of a scroll, each at its own moment, read as
 *  flicker; the fade is kept for a tile that genuinely sat empty. */
const INSTANT_MS = 90;
/** No scroll for this long and it has stopped: sharpen, and look ahead. */
const SETTLE_MS = 140;
/** Thumbnails kept ready on each side of where the screen stopped, or of
 *  where the pointer is about to take it (the date rail, Jump to): counted in
 *  photographs, not screens, so small tiles get as many as large ones. Like a
 *  phone's own photo app, the next few hundred either way are already there.
 *  The small pictures, 256 px: 600 of them are a few megabytes. */
const PREFETCH_COUNT = 300;
/** While scrolling: pictures fetched this far ahead of the band, in time. */
const WARM_AHEAD_MS = 2000;
/** How often, while scrolling, what is fetched ahead is looked at again. */
const WARM_EVERY_MS = 200;
/** On a lean link, only this much is fetched ahead of where a scroll stopped. */
const LEAN_PREFETCH_SCREENS = 0.5;

const KIND_NAMES = ['picture', 'video', 'audio'];
/** Segment flags for a live photo and for a RAW+JPEG shot. */
const FLAG_LIVE = 256;
const FLAG_RAW = 512;
/** How long a mouse rests on a live photo before it plays. */
const LIVE_HOVER_MS = 280;

const ICON = {
  video: '<svg viewBox="0 0 24 24"><path d="m8 5 11 7-11 7Z" fill="currentColor" stroke="none"/></svg>',
  audio: '<svg viewBox="0 0 24 24"><path d="M9 18V6l11-2v12"/><circle cx="6.5" cy="18" r="2.5"/><circle cx="17.5" cy="16" r="2.5"/></svg>',
  // Only ever a glyph: a photograph with no picture to show, which is almost
  // always a damaged file.
  picture: '<svg viewBox="0 0 24 24"><rect x="3.5" y="4.5" width="17" height="15" rx="2"/><circle cx="9" cy="10" r="1.6"/><path d="m4 17 5-5 4 4 2.5-2.5L20 18"/></svg>',
  heart: '<svg viewBox="0 0 24 24"><path d="M12 20.5 4.5 13a4.6 4.6 0 0 1 6.5-6.5l1 1 1-1A4.6 4.6 0 0 1 19.5 13Z"/></svg>',
  globe: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8"/><path d="M4 12h16M12 4a14 14 0 0 1 0 16 14 14 0 0 1 0-16"/></svg>',
  eyeOff: '<svg viewBox="0 0 24 24"><path d="M10.6 5.2A9.7 9.7 0 0 1 12 5c5 0 9 4.5 9 7a11 11 0 0 1-2.2 3.4M6.3 6.6C3.9 8.2 3 10.6 3 12c0 2.5 4 7 9 7 1.6 0 3-.4 4.3-1.1M3 3l18 18"/></svg>',
  check: '<svg viewBox="0 0 24 24"><path d="m5 12 5 5 9-9"/></svg>',
};

/**
 * Thumbnails fetched ahead of the scroll into the browser's cache, a few at a
 * time and at low priority, so a tile mounted there later has nothing to wait
 * for. Only while `idle()` says the tiles already made have their pictures:
 * the server speaks HTTP/1.1, so a browser opens six connections to it, and a
 * look-ahead holding four of them through a scroll left the tiles coming on
 * screen queued behind it, each one arriving late and fading in. Changing what is wanted replaces what is queued; the few already on
 * their way are left to land unless `cancel` says they belong to a result set
 * that is gone — stopping one a moment before it arrived, only to ask for it
 * again when its tile was made, was what left a turned-round scroll waiting.
 */
class Prefetcher {
  constructor(limit = 3, idle = () => true) {
    this.limit = limit;
    this.idle = idle;
    this.queue = [];
    this.active = new Map();     // url -> Image in flight
    this.done = new Set();
  }

  want(urls, { cancel = false } = {}) {
    const wanted = new Set(urls);
    for (const [url, image] of this.active) {
      if (cancel && !wanted.has(url)) {
        this.active.delete(url);
        image.removeAttribute('src');      // stops the download
      }
    }
    if (this.done.size > 4000) this.done.clear();
    this.queue = urls.filter((url) => !this.active.has(url) && !this.done.has(url));
    this.pump();
  }

  pump() {
    while (this.active.size < this.limit && this.queue.length && this.idle()) {
      const url = this.queue.shift();
      const image = new Image();
      image.decoding = 'async';
      if ('fetchPriority' in image) image.fetchPriority = 'low';
      const finish = () => {
        if (this.active.get(url) !== image) return;
        this.active.delete(url);
        this.done.add(url);
        this.pump();
      };
      image.onload = finish;
      image.onerror = finish;
      this.active.set(url, image);
      image.src = url;
    }
  }
}

/**
 * Whether the connection asks for restraint: Data Saver turned on, or a link
 * the browser measures as 2G. Looking ahead spends someone's data on pictures
 * they may never scroll to — a fair trade at home, not on a phone's last few
 * hundred megabytes. Browsers that do not say (Safari, Firefox) are taken at
 * their word, which is none.
 */
function leanLink() {
  const link = navigator.connection;
  if (!link) return false;
  return Boolean(link.saveData) || link.effectiveType === '2g' || link.effectiveType === 'slow-2g';
}

/** The sign for an item's kind, standing in for a picture it does not have. */
function glyphFor(kind) {
  const glyph = document.createElement('div');
  glyph.className = 'glyph';
  glyph.innerHTML = ICON[kind] || ICON.audio;
  return glyph;
}

function formatDuration(seconds) {
  if (!seconds) return '';
  const s = Math.round(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = String(s % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${sec}` : `${m}:${sec}`;
}

function formatSection(key, count) {
  if (key === 'match') return { title: i18n.t('Best matches'), sub: i18n.t('{count} results', { count }) };
  if (key === 'unknown') return { title: i18n.t('Undated'), sub: i18n.items(count) };
  const date = new Date(`${key}T00:00:00`);
  if (Number.isNaN(date.getTime())) return { title: key, sub: i18n.items(count) };
  const now = new Date();
  const sameYear = date.getFullYear() === now.getFullYear();
  return {
    title: date.toLocaleDateString(i18n.locale(), {
      weekday: 'short',
      day: 'numeric',
      month: 'long',
      ...(sameYear ? {} : { year: 'numeric' }),
    }),
    sub: i18n.items(count),
  };
}

export class Grid extends EventTarget {
  constructor(scroller, container) {
    super();
    this.scroller = scroller;
    this.container = container;
    this.segments = [];
    this.layout = { cells: [], headers: [], height: 0, buckets: new Map(), bucketSize: 600 };
    this.mode = 'justified';
    this.zoom = 2;
    this.width = 0;
    this.mounted = new Map();     // cell index -> node
    this.mountedHeads = new Map();
    this.pool = [];
    this.parked = new Map();      // thumbnail URL -> parked node still showing it
    this.headPool = [];
    this.selection = new Set();
    this.selecting = false;
    this.showVisibility = false;   // admins only; set by the app shell
    this.cursor = -1;
    this.lastAnchor = -1;
    // What was selected before the current Shift run began. Held so that
    // shrinking a Shift+Arrow range gives back what the range swallowed,
    // instead of leaving it selected the way an add-only range would.
    // Null means no run is in progress.
    this.rangeBase = null;
    this.frame = null;
    this.renderView = null;       // the screen, while a render runs (viewport)
    // How fast, and which way, the scroll is going (px per ms; down is +).
    this.velocity = 0;
    this.direction = 1;
    this.lastTop = 0;
    this.lastTime = 0;
    this.fast = false;
    this.settleTimer = null;
    this.scrolling = false;
    this.lastWarm = 0;
    this.prefetcher = new Prefetcher(3, () => !this.tilesLoading());
    // The pointer, for telling a real move of the mouse from the one a
    // browser makes up when the page scrolls under it.
    this.pointerAt = null;

    this.onScroll = this.onScroll.bind(this);
    this.scroller.addEventListener('scroll', this.onScroll, { passive: true });

    this.resizeObserver = new ResizeObserver(() => this.measure());
    this.resizeObserver.observe(this.scroller);

    this.container.addEventListener('click', (event) => this.onClick(event));
    this.container.addEventListener('dblclick', (event) => this.onClick(event, true));

    // A live photo plays where it sits when a mouse rests on it, as it does
    // in Photos. Not for touch: a finger on a tile is the start of a scroll
    // or a tap to open it, and the viewer plays it there.
    this.liveTimer = null;
    this.container.addEventListener('pointerover', (event) => {
      // Not for anyone who asked their system for less motion either: a
      // picture that starts moving because the mouse crossed it is exactly
      // what they asked not to be shown.
      if (event.pointerType !== 'mouse' || !mayAutoplay()) return;
      const node = event.target.closest?.('.cell');
      if (!node || node.contains(event.relatedTarget)) return;
      clearTimeout(this.liveTimer);
      if (!node.classList.contains('is-live')) return;
      this.liveTimer = setTimeout(() => this.peekLive(node), LIVE_HOVER_MS);
    });
    // The hover look comes back when the mouse moves, not when the scroll
    // stops. Brought back at the stop, it came and went between every notch
    // of a mouse wheel: the tile under the pointer lit up, zoomed and showed
    // its tick, then dropped it again as the next notch scrolled — a flicker.
    this.container.addEventListener('pointermove', (event) => {
      if (event.pointerType !== 'mouse') return;
      const was = this.pointerAt;
      this.pointerAt = { x: event.clientX, y: event.clientY };
      if (!this.container.classList.contains('no-hover') || !was) return;
      if (Math.abs(event.clientX - was.x) + Math.abs(event.clientY - was.y) < 2) return;
      this.hoverBack(event.target.closest?.('.cell'));
    });
    this.container.addEventListener('pointerdown', (event) => {
      if (event.pointerType === 'mouse') this.hoverBack(event.target.closest?.('.cell'));
    });
    this.container.addEventListener('pointerout', (event) => {
      const node = event.target.closest?.('.cell');
      if (!node || node.contains(event.relatedTarget)) return;
      clearTimeout(this.liveTimer);
      this.stopLive(node);
    });
  }

  /** The mouse moved: tiles answer it again, and a live photo under it plays. */
  hoverBack(node) {
    if (this.scrolling || !this.container.classList.contains('no-hover')) return;
    this.container.classList.remove('no-hover');
    clearTimeout(this.liveTimer);
    if (node?.classList.contains('is-live') && mayAutoplay()) {
      this.liveTimer = setTimeout(() => this.peekLive(node), LIVE_HOVER_MS);
    }
  }

  /** Whether any tile made so far is still waiting for its picture. */
  tilesLoading() {
    for (const node of this.mounted.values()) {
      const img = node.firstElementChild;
      if (img && !img.hidden && img.getAttribute('src') && !img.complete) return true;
    }
    return false;
  }

  /** Play a live photo's clip over its tile, once, then show the photograph. */
  peekLive(node) {
    // Not while the grid scrolls: the tile only slid under a resting mouse.
    if (this.fast || this.scrolling || this.container.classList.contains('no-hover')
        || node.querySelector('.live-peek')
        || !node.matches(':hover')) return;
    const id = node.dataset.id;
    const video = liveVideo(liveUrls(id), {
      className: 'live-peek',
      wanted: () => node.isConnected && node.dataset.id === id && node.matches(':hover'),
    });
    video.dataset.playing = '1';
    const done = () => this.stopLive(node);
    video.addEventListener('ended', done);
    video.addEventListener('live-unavailable', done);
    video.addEventListener('playing', () => node.classList.add('live-playing'));
    node.appendChild(video);
    video.play().catch(() => {});
  }

  stopLive(node) {
    node.classList.remove('live-playing');
    releaseVideo(node.querySelector('.live-peek'));
  }

  /* ---------------------------------------------------------------- */

  /**
   * `resetScroll` distinguishes a genuinely new result set (a search, a
   * filter, a sort, a different view) from an in-place refresh of the one
   * already on screen (an edit, a background rescan finishing). The former
   * wants the viewport back at the top — nothing scrolled to is likely to
   * still be there once the query itself changed, so `relayout`'s scroll-
   * anchor preservation (below) just leaves the new results sitting above a
   * scroll position that means nothing for them, invisible until scrolled to
   * by hand. The latter wants exactly the opposite: don't move the person's
   * place in the grid just because a photo they are looking at got a new
   * favourite flag.
   */
  setData(segments, { resetScroll = false } = {}) {
    this.segments = segments || [];
    const hadSelection = this.selection.size > 0;
    this.selection.clear();
    this.cursor = -1;
    this.rangeBase = null;
    if (resetScroll) this.scroller.scrollTop = 0;
    // What was being fetched ahead belongs to the last result set.
    this.prefetcher.want([], { cancel: true });
    this.relayout(!resetScroll);
    // Say so, or the selection bar goes on offering to act on photos that are
    // no longer selected.
    if (hadSelection) this.emitSelection();
  }

  /**
   * Add the next piece of the same result set to the end.
   *
   * Unlike setData this keeps the selection, the keyboard cursor and the
   * scroll position: nothing already on screen moves, because every cell that
   * was laid out keeps its index and the new ones only ever follow it.
   */
  appendData(segments) {
    const added = mergeSegments(this.segments, segments);
    if (added) this.relayout(true);
    return added;
  }

  setMode(mode) {
    if (this.mode === mode) return;
    this.mode = mode;
    this.relayout(true);
  }

  setZoom(zoom) {
    const next = Math.max(0, Math.min(4, zoom));
    if (this.zoom === next) return;
    this.zoom = next;
    this.relayout(true);
  }

  /** Usable width = scroller content box minus its horizontal padding. */
  contentWidth() {
    const style = getComputedStyle(this.scroller);
    const pad = parseFloat(style.paddingLeft || 0) + parseFloat(style.paddingRight || 0);
    return Math.max(0, this.scroller.clientWidth - pad);
  }

  measure() {
    const width = this.contentWidth();
    if (Math.abs(width - this.width) < 2) return;
    this.width = width;
    this.relayout(true);
  }

  /** Keeps the item under the viewport top anchored across relayouts. */
  /** Rewrite the headings on screen in the language just chosen. */
  relabel() {
    for (const node of this.mountedHeads.values()) {
      const head = node.dataset.head;
      const seg = this.segments?.find((one) => one.key === head);
      const count = seg ? (seg.count ?? seg.items?.length) : undefined;
      const { title, sub } = formatSection(head, count ?? 0);
      node.querySelector('.title').textContent = title;
      if (node.classList.contains('packed')) node.querySelector('.title').title = title;
      if (count != null) node.querySelector('.sub').textContent = sub;
      const select = node.querySelector('[data-select-section]');
      if (select) select.textContent = i18n.t('Select all');
    }
  }

  relayout(preserveAnchor = false) {
    if (!this.width) this.width = this.contentWidth();
    if (!this.width) return;

    let anchorId = null;
    let anchorOffset = 0;
    if (preserveAnchor && this.layout.cells.length) {
      const top = this.scroller.scrollTop;
      const found = this.layout.cells.find((c) => c.y + c.h > top);
      if (found) {
        anchorId = found.id;
        anchorOffset = found.y - top;
      }
    }

    this.layout = computeLayout(this.segments, {
      width: this.width,
      mode: this.mode,
      zoom: this.zoom,
      gap: this.mode === 'film' ? 2 : 6,
    });
    this.container.style.height = `${this.layout.height}px`;

    this.unmountAll();

    if (anchorId != null) {
      const target = this.layout.cells.find((c) => c.id === anchorId);
      if (target) this.scroller.scrollTop = Math.max(0, target.y - anchorOffset);
    }
    this.render();
    this.trimPool();
    this.dispatchEvent(new CustomEvent('layout', { detail: this.layout }));
  }

  /** Back to the pool's size, letting the longest-parked nodes go. */
  trimPool() {
    if (this.pool.length <= POOL_LIMIT) return;
    const dropped = new Set(this.pool.splice(0, this.pool.length - POOL_LIMIT));
    for (const [url, node] of this.parked) if (dropped.has(node)) this.parked.delete(url);
  }

  onScroll() {
    if (this.frame) return;
    this.frame = requestAnimationFrame(() => {
      this.frame = null;
      this.measureSpeed();
      this.render();
      this.dispatchEvent(new CustomEvent('scroll', {
        detail: {
          top: this.scroller.scrollTop,
          section: sectionAt(this.layout, this.scroller.scrollTop + 40),
          ratio: this.scroller.scrollTop /
            Math.max(1, this.layout.height - this.scroller.clientHeight),
        },
      }));
    });
  }

  /* ---------------------------------------------------------------- */

  /** Speed and direction from this frame and the last, smoothed. */
  measureSpeed() {
    const now = performance.now();
    const top = this.scroller.scrollTop;
    const elapsed = now - this.lastTime;
    if (this.lastTime && elapsed > 0 && elapsed < 250) {
      const speed = (top - this.lastTop) / elapsed;
      this.velocity = this.velocity * 0.4 + speed * 0.6;
    } else {
      this.velocity = 0;
    }
    this.direction = this.velocity < 0 ? -1 : this.velocity > 0 ? 1 : this.direction;
    this.lastTop = top;
    this.lastTime = now;
    this.fast = Math.abs(this.velocity) > FLING;
    // Tiles slide under a pointer resting on the grid. With hover live, each
    // one zoomed its picture in and out again as it passed, which read as the
    // pictures being fitted into place; the hover waits for the scroll to stop.
    if (!this.scrolling) {
      this.scrolling = true;
      this.container.classList.add('scrolling', 'no-hover');
    }
    clearTimeout(this.settleTimer);
    this.settleTimer = setTimeout(() => this.settle(), SETTLE_MS);
    this.warmAhead(now);
  }

  /**
   * While the scroll goes on, fetch the next couple of seconds of it beyond
   * the band, so pictures are in the cache before their tiles are made. Not
   * while flying past: those tiles are never looked at. Not on a lean link
   * either (see leanLink): the band's own tiles are all it fetches.
   */
  warmAhead(now) {
    if (this.fast || leanLink()) {
      // Nothing queued is where a fling is going; what is on its way may land.
      this.prefetcher.want([]);
      return;
    }
    if (now - this.lastWarm < WARM_EVERY_MS) return;
    this.lastWarm = now;
    const view = this.scroller.clientHeight;
    const reach = Math.min(view * MAX_AHEAD_SCREENS,
      Math.max(view, Math.abs(this.velocity) * WARM_AHEAD_MS));
    const [top, bottom] = this.band();
    this.prefetcher.want(this.direction >= 0
      ? this.unmountedUrls(bottom, bottom + reach)
      : this.unmountedUrls(top - reach, top).reverse());
  }

  /** Thumbnails for the tiles between two offsets that are not mounted yet,
   *  top to bottom — the small ones, which is what a tile made while the
   *  scroll goes on will ask for (see paintCell). */
  unmountedUrls(from, to) {
    const cells = visibleRange(this.layout, from, to).cells
      .filter((cell) => !this.mounted.has(cell.n))
      .sort((a, b) => a.y - b.y || a.x - b.x);
    const urls = [];
    for (const cell of cells) {
      const url = this.cellThumbUrl(cell, true);
      if (url) urls.push(url);
    }
    return urls;
  }

  /** The band of tiles kept ready: longer ahead of a fast scroll, shorter behind. */
  band() {
    const view = this.scroller.clientHeight;
    const speed = Math.abs(this.velocity);
    // At least a screen ahead: on a tall window 900 px was less than one, and
    // a steady scroll met tiles made a moment before they came into view.
    const lead = Math.max(OVERSCAN, view);
    const ahead = Math.min(lead + speed * LOOKAHEAD_MS, lead + view * MAX_AHEAD_SCREENS);
    const behind = this.fast ? TRAILING : OVERSCAN;
    const top = this.scroller.scrollTop;
    return this.direction >= 0
      ? [top - behind, top + view + ahead]
      : [top - ahead, top + view + behind];
  }

  /**
   * The scroll has stopped. Tiles that took the small thumbnail while flying
   * past get the sharp one — drawn over the small one when it arrives, so
   * nothing flickers — and pictures are fetched both ways: two screens on in
   * the direction it was going, then one back the other way, so carrying on
   * or turning round both find them ready.
   */
  settle() {
    this.velocity = 0;
    this.lastTime = 0;
    this.lastWarm = 0;
    this.fast = false;
    this.scrolling = false;
    this.container.classList.remove('scrolling');
    this.render();
    const view = this.scroller.clientHeight;
    const [top, bottom] = this.band();
    const down = this.direction >= 0;
    if (leanLink()) {
      // Half a screen on, the way it was going, and nothing behind.
      const reach = view * LEAN_PREFETCH_SCREENS;
      this.prefetcher.want(down
        ? this.unmountedUrls(bottom, bottom + reach)
        : this.unmountedUrls(top - reach, top).reverse());
      return;
    }
    this.prefetcher.want(this.readyAround(top, bottom, down));
  }

  /**
   * The thumbnails to keep ready around a stretch of the grid: the nearest
   * PREFETCH_COUNT below it and the nearest PREFETCH_COUNT above, nearest
   * first, two on the way it is going for every one behind, so carrying on
   * and turning round both find them there.
   */
  readyAround(top, bottom, down = true) {
    const below = this.nearestUrls(bottom, 1, PREFETCH_COUNT);
    const above = this.nearestUrls(top, -1, PREFETCH_COUNT);
    const [ahead, behind] = down ? [below, above] : [above, below];
    const urls = [];
    for (let i = 0, j = 0; i < ahead.length || j < behind.length;) {
      for (let k = 0; k < 2 && i < ahead.length; k++) urls.push(ahead[i++]);
      if (j < behind.length) urls.push(behind[j++]);
    }
    return urls;
  }

  /** Up to *count* thumbnails of tiles not made yet, from *y* on in the
   *  direction *step* (1 down, -1 up), nearest first. The stretch looked at
   *  doubles until it holds enough or reaches the end of the grid. */
  nearestUrls(y, step, count) {
    const height = this.layout.height || 0;
    let span = Math.max(this.scroller.clientHeight, 400);
    for (;;) {
      const from = step > 0 ? y : Math.max(0, y - span);
      const to = step > 0 ? Math.min(height, y + span) : y;
      const urls = this.unmountedUrls(from, to);
      const reachedEnd = step > 0 ? to >= height : from <= 0;
      if (urls.length >= count || reachedEnd) {
        return step > 0 ? urls.slice(0, count) : urls.reverse().slice(0, count);
      }
      span *= 2;
    }
  }

  /**
   * Get the pictures ready around a place the pointer is about to take the
   * grid to: the date rail under the mouse, a month in Jump to. *y* is where
   * the top of the screen would be. Not on a lean link, and not while a
   * scroll is under way, which is fetching for itself.
   */
  prefetchAround(y) {
    if (this.scrolling || leanLink()) return;
    const view = this.scroller.clientHeight;
    const top = Math.max(0, Math.min(y, Math.max(0, (this.layout.height || 0) - view)));
    this.prefetcher.want(this.readyAround(top, top + view, true));
  }

  /**
   * Where the screen is. Read once per render and kept until it ends: asked
   * of the scroller after a tile has been placed, it made the browser lay the
   * whole grid out again there and then, once for every tile made — most of
   * the time a fast scroll spent, in frames that missed their turn.
   */
  viewport() {
    return this.renderView
      || { top: this.scroller.scrollTop, height: this.scroller.clientHeight };
  }

  onScreen(cell) {
    const { top, height } = this.viewport();
    return cell.y + cell.h > top && cell.y < top + height;
  }

  /**
   * The sharp picture for a tile that took the small one while the scroll
   * went on — once it has stopped, and only for tiles near the screen, so the
   * ones being looked at are not queued behind ones a screen further on.
   *
   * Fetched and decoded on the side first, and only then put in the tile.
   * Pointing a tile's <img> at a picture the browser does not have yet blanks
   * it in Safari — WebKit drops the old picture the moment the address
   * changes, where Chrome keeps it until the new one is ready — so after
   * every brisk scroll the whole screen blinked and came back sharp. A
   * picture already decoded is drawn in the same frame, in every browser.
   */
  sharpen(node, cell) {
    if (this.scrolling) return;
    const img = node.querySelector('img');
    const full = this.cellThumbUrl(cell);
    if (!full || !img || img.hidden || img.getAttribute('src') === full) return;
    const { top, height: view } = this.viewport();
    if (cell.y + cell.h < top - view / 2 || cell.y > top + view * 1.5) return;
    this.swapIn(node, cell, full);
  }

  /** Fetch and decode `url` on the side, then show it in the tile — which
   *  keeps the picture it has until then. See sharpen. */
  swapIn(node, cell, url) {
    if (node.pendingSrc === url) return;
    node.pendingSrc = url;
    const img = node.querySelector('img');
    const sharp = new Image();
    sharp.decoding = 'async';
    if ('fetchPriority' in sharp) sharp.fetchPriority = this.onScreen(cell) ? 'high' : 'auto';
    const swap = () => {
      if (node.pendingSrc !== url) return;            // recycled, or wanted elsewhere
      node.pendingSrc = null;
      if (img.hidden || node.dataset.id !== String(cell.id) || !this.mounted.has(cell.n)) return;
      img.src = url;
    };
    sharp.onload = () => {
      const decoded = sharp.decode ? sharp.decode() : Promise.resolve();
      decoded.then(swap, swap);
    };
    sharp.onerror = () => { if (node.pendingSrc === url) node.pendingSrc = null; };
    sharp.src = url;
  }

  render() {
    this.renderView = { top: this.scroller.scrollTop, height: this.scroller.clientHeight };
    try {
      this.renderBand();
    } finally {
      this.renderView = null;
    }
  }

  renderBand() {
    const [top, bottom] = this.band();
    const { cells, headers } = visibleRange(this.layout, top, bottom);

    const wanted = new Set();
    for (const cell of cells) {
      wanted.add(cell.n);
      let node = this.mounted.get(cell.n);
      if (!node) {
        node = this.takeCell(cell);
        this.mounted.set(cell.n, node);
        this.container.appendChild(node);
        this.paintCell(node, cell);
      } else {
        // Already mounted: reposition, and re-sync the state that can change
        // without the cell being remounted (selection, keyboard cursor).
        this.positionCell(node, cell);
        this.syncCellState(node, cell);
        this.sharpen(node, cell);
      }
    }
    for (const [index, node] of this.mounted) {
      if (!wanted.has(index)) {
        this.mounted.delete(index);
        this.releaseCell(node);
      }
    }

    const wantedHeads = new Set();
    for (const head of headers) {
      // Small days share a row (see layout.js), so a header is known by where
      // it sits across as well as down.
      const place = `${head.y}:${head.x || 0}`;
      wantedHeads.add(place);
      let node = this.mountedHeads.get(place);
      if (!node) {
        node = this.headPool.pop() || this.createHeader();
        this.mountedHeads.set(place, node);
        this.container.appendChild(node);
        const { title, sub } = formatSection(head.key, head.count);
        node.querySelector('.title').textContent = title;
        node.querySelector('.sub').textContent = sub;
        node.dataset.head = head.key;
        node.dataset.firstCell = String(head.firstCell);
        // A packed day's header is only as wide as its own photographs; a
        // pooled node may come back from one, so both ways are set.
        const packed = head.w != null;
        node.classList.toggle('packed', packed);
        node.style.width = packed ? `${head.w}px` : '';
        node.querySelector('.title').title = packed ? title : '';
        node.style.transform = `translate(${head.x || 0}px,${head.y}px)`;
      }
    }
    for (const [key, node] of this.mountedHeads) {
      if (!wantedHeads.has(key)) {
        this.mountedHeads.delete(key);
        node.remove();
        if (this.headPool.length < 40) this.headPool.push(node);
      }
    }
  }

  /* -- node lifecycle ------------------------------------------------ */

  createCell() {
    const node = document.createElement('div');
    node.className = 'cell';
    node.setAttribute('role', 'gridcell');
    node.tabIndex = -1;
    const img = document.createElement('img');
    img.decoding = 'async';
    img.draggable = false;
    img.alt = '';
    img.addEventListener('load', () => {
      clearTimeout(node.shimmerTimer);
      // Arrived almost at once: no fade (see INSTANT_MS).
      img.classList.toggle('instant',
        !!node.paintedAt && performance.now() - node.paintedAt < INSTANT_MS);
      img.classList.add('ready');
      node.classList.remove('loading');
      this.prefetcher.pump();
    });
    img.addEventListener('error', () => {
      clearTimeout(node.shimmerTimer);
      node.classList.remove('loading');
      this.prefetcher.pump();
      // A thumbnail that will not load shows the sign for its kind, as an item
      // with none does. An empty square reads as the page still loading, or
      // the file being broken, and neither is usually true.
      if (!img.getAttribute('src') || node.querySelector('.glyph')) return;
      img.hidden = true;
      node.classList.add('audio-cell');
      node.appendChild(glyphFor(node.dataset.kind));
    });
    node.appendChild(img);

    const pick = document.createElement('button');
    pick.className = 'pick';
    pick.type = 'button';
    pick.setAttribute('aria-label', i18n.t('Select'));
    pick.innerHTML = ICON.check;
    node.appendChild(pick);
    return node;
  }

  createHeader() {
    const node = document.createElement('div');
    node.className = 'section-head';
    node.innerHTML = '<span class="title"></span><span class="sub"></span>'
      + '<button type="button" data-select-section></button>';
    node.querySelector('[data-select-section]').textContent = i18n.t('Select all');
    return node;
  }

  /** The smallest derivative that still covers the size this cell is drawn at —
   *  or, with `small`, the smallest there is, for a tile only flying past. */
  cellThumbUrl(cell, small = false) {
    if ((cell.flags & 2) === 0) return null;
    const need = Math.max(cell.w, cell.h) * (window.devicePixelRatio || 1);
    return thumbUrl(cell.id, !small && need > 300 ? 640 : 256, cell.v);
  }

  /**
   * Prefer the parked node that is still holding this very picture.
   *
   * Recycling any node and pointing it at a new URL means the browser decodes
   * the image again, and the tile spends a frame or two on the placeholder
   * even when the bytes are in cache — which is the flicker you see scrolling
   * back up through a library. A node that never stopped holding the right
   * picture has nothing to decode and nothing to wait for.
   */
  takeCell(cell) {
    // The sharp picture if a parked node holds it, else the small one: flying
    // past it is all that is wanted, and otherwise it stays on screen while
    // the sharp one comes in on the side (see paintCell), where a tile with
    // neither sits on its colour until it does.
    const urls = [this.cellThumbUrl(cell), this.cellThumbUrl(cell, true)];
    for (const url of urls) {
      const kept = url && this.parked.get(url);
      if (kept) {
        this.parked.delete(url);
        const at = this.pool.indexOf(kept);
        if (at >= 0) this.pool.splice(at, 1);
        return kept;
      }
    }
    const node = this.pool.pop();
    if (!node) return this.createCell();
    const src = node.querySelector('img')?.getAttribute('src');
    if (src) this.parked.delete(src);
    return node;
  }

  releaseCell(node, keep = this.pool.length < POOL_LIMIT) {
    this.stopLive(node);
    node.remove();
    clearTimeout(node.shimmerTimer);
    node.pendingSrc = null;
    if (keep) {
      node.className = 'cell';
      const img = node.querySelector('img');
      if (img) {
        // `ready` goes, because this node may be handed to a different
        // picture next; the src stays, because it may just as easily be
        // handed back to this one.
        img.classList.remove('ready');
        const src = img.getAttribute('src');
        // Still downloading: stop it. On a fast scroll these are the tiles
        // already gone past, and the browser fetches only a few at a time —
        // left running, they hold up the ones now coming on screen.
        if (src && !img.complete) img.removeAttribute('src');
        else if (src) this.parked.set(src, node);
      }
      this.pool.push(node);
    }
  }

  unmountAll() {
    // Every node is kept, whatever the pool's size: a relayout mounts most
    // of them again at once, each with the picture it already holds, and a
    // node dropped here came back as a tile on its colour, fading its own
    // picture in again. The pool is trimmed after the render (trimPool).
    for (const node of this.mounted.values()) this.releaseCell(node, true);
    // Anything not parked above is gone; the map must not outlive the pool.
    for (const [url, node] of this.parked) {
      if (!this.pool.includes(node)) this.parked.delete(url);
    }
    this.mounted.clear();
    for (const node of this.mountedHeads.values()) {
      node.remove();
      if (this.headPool.length < 40) this.headPool.push(node);
    }
    this.mountedHeads.clear();
  }

  syncCellState(node, cell) {
    node.classList.toggle('selected', this.selection.has(cell.id));
    node.classList.toggle('cursor', this.cursor === cell.n);
    node.setAttribute('aria-selected', String(this.selection.has(cell.id)));
  }

  positionCell(node, cell) {
    // A plain translate, not translate3d: a tile does not move while the
    // grid scrolls (the scroller moves it), so a layer of its own bought
    // nothing, and hundreds of them — one GPU texture per tile — had an
    // iPhone dropping and redrawing tiles as it ran short of memory.
    node.style.transform = `translate(${cell.x}px,${cell.y}px)`;
    node.style.width = `${cell.w}px`;
    node.style.height = `${cell.h}px`;
  }

  paintCell(node, cell) {
    this.positionCell(node, cell);
    node.dataset.id = String(cell.id);
    node.dataset.n = String(cell.n);

    const kind = KIND_NAMES[cell.kind] || 'picture';
    node.dataset.kind = kind;
    const hasThumb = (cell.flags & 2) !== 0;
    const favorite = (cell.flags & 1) !== 0;

    this.syncCellState(node, cell);
    node.classList.toggle('audio-cell', !hasThumb);

    // Clear previous badges without touching the img/pick nodes.
    node.querySelectorAll('.badge, .glyph, .label').forEach((n) => n.remove());

    const img = node.querySelector('img');
    // Its own colour until the picture arrives: a fast scroll shows a mosaic
    // of the right colours rather than a wall of grey.
    node.style.backgroundColor = hasThumb && cell.swatch ? `#${cell.swatch}` : '';
    if (hasThumb) {
      img.hidden = false;
      // Already holding the sharp one: keep it. Made while the scroll goes
      // on: the small one, a quarter of the bytes and usually fetched ahead
      // already, sharpened once the scroll stops (see settle) — the way
      // Photos shows its small pictures while the finger is moving. On a
      // phone every tile wants the sharp one (a Retina screen makes a small
      // tile a large picture), so a scroll fetching sharp ones outran the
      // network at any pace and met its tiles still empty.
      const full = this.cellThumbUrl(cell);
      const current = img.getAttribute('src');
      const url = current === full || !this.scrolling ? full : this.cellThumbUrl(cell, true);
      // Holding this very picture, sharp or small, and decoded: it stays on
      // screen, and a sharper one comes in on the side (swapIn). Pointing the
      // tile straight at the new address dropped the tile to its colour and
      // faded the picture back in — in Safari, which blanks an image the
      // moment its address changes, even when the browser had it already.
      const holds = (current === full || current === this.cellThumbUrl(cell, true))
        && img.complete && img.naturalWidth > 0;
      // On screen first. Not "low" for the rest: the band ahead is where the
      // scroll is going, and a low hint let the browser hold those back until
      // they arrived on screen still empty.
      img.fetchPriority = this.onScreen(cell) ? 'high' : 'auto';

      // A recycled node usually comes back holding the very picture it is
      // being asked for again: scrolling down a row and back up is the
      // ordinary case, and the pool hands out the most recently released node
      // first. Clearing `ready` and re-assigning the same src there dropped
      // the tile to a shimmer and faded in a picture the browser already had
      // decoded — the flicker while scrolling was ours, not the network's.
      //
      // A thumbnail URL carries a version that changes whenever the bytes
      // change, so an image that is already here and decoded is by definition
      // the right one. Only a different picture, or one that failed, is worth
      // fetching again.
      if (holds) {
        if (current !== url) this.sharpen(node, cell);
      } else if (current !== url || (img.complete && !img.naturalWidth)) {
        img.src = url;
      }
      // A picture that is already here and decoded is shown at once.
      const shown = holds || (img.complete && img.naturalWidth > 0);
      img.classList.toggle('ready', shown);
      img.classList.remove('instant');
      node.paintedAt = shown ? 0 : performance.now();
      node.classList.remove('loading');
      clearTimeout(node.shimmerTimer);

      // Otherwise, wait before admitting to a wait. A thumbnail the browser
      // has cached is decoded within a frame or two, and a shimmer that
      // appears and disappears inside a tenth of a second is not information —
      // it is the flicker. Only a load that is genuinely slow gets to say so.
      // A tile showing its colour is already saying a picture is coming.
      if (!shown && !cell.swatch) {
        node.shimmerTimer = setTimeout(() => {
          if (!img.complete) node.classList.add('loading');
        }, SHIMMER_DELAY);
      }
    } else {
      img.hidden = true;
      img.removeAttribute('src');
      node.classList.remove('loading');
      node.appendChild(glyphFor(kind));
    }

    const live = kind === 'picture' && (cell.flags & FLAG_LIVE) !== 0;
    node.classList.toggle('is-live', live);
    if (!live) this.stopLive(node);
    if (kind !== 'picture') {
      const badge = document.createElement('span');
      badge.className = 'badge kind';
      badge.innerHTML = ICON[kind];
      node.appendChild(badge);
    } else if (live) {
      const badge = document.createElement('span');
      badge.className = 'badge kind live';
      badge.title = i18n.t('Live Photo');
      badge.innerHTML = LIVE_ICON;
      node.appendChild(badge);
    }
    if ((cell.flags & FLAG_RAW) !== 0) {
      const badge = document.createElement('span');
      badge.className = 'badge raw';
      badge.title = i18n.t('RAW kept with this photo');
      badge.textContent = 'RAW';
      node.appendChild(badge);
    }
    if (cell.dur) {
      const badge = document.createElement('span');
      badge.className = 'badge dur';
      badge.textContent = formatDuration(cell.dur);
      node.appendChild(badge);
    }
    if (favorite) {
      const badge = document.createElement('span');
      badge.className = 'badge fav';
      badge.innerHTML = ICON.heart;
      node.appendChild(badge);
    }

    // Visibility, for the admins who set it (flags 64 = public, 128 = hidden).
    if (this.showVisibility && (cell.flags & 192)) {
      const badge = document.createElement('span');
      const isPublic = (cell.flags & 64) !== 0;
      badge.className = `badge vis ${isPublic ? 'is-public' : 'is-hidden'}`;
      badge.title = isPublic ? i18n.t('Visible to everyone') : i18n.t('Hidden — admins only');
      badge.innerHTML = isPublic ? ICON.globe : ICON.eyeOff;
      node.appendChild(badge);
    }
  }

  refreshCell(id) {
    for (const [index, node] of this.mounted) {
      const cell = this.layout.cells[index];
      if (cell && cell.id === id) this.paintCell(node, cell);
    }
  }

  /* -- interaction ---------------------------------------------------- */

  onClick(event, isDouble = false) {
    const sectionButton = event.target.closest('[data-select-section]');
    if (sectionButton) {
      const head = sectionButton.parentElement;
      const key = head.dataset.head;
      const ids = (this.segments.find((s) => s.key === key)?.items || [])
        .map((item) => item[0]);
      const allSelected = ids.every((id) => this.selection.has(id));
      ids.forEach((id) => (allSelected ? this.selection.delete(id) : this.selection.add(id)));
      this.emitSelection();
      this.render();
      return;
    }

    const node = event.target.closest('.cell');
    if (!node) return;
    const id = Number(node.dataset.id);
    const index = Number(node.dataset.n);
    this.cursor = index;

    const viaPick = !!event.target.closest('.pick');
    const additive = event.metaKey || event.ctrlKey || viaPick;
    const ranged = event.shiftKey;

    if (ranged && this.lastAnchor >= 0) {
      // Shift-click and Shift+Arrow are the same gesture with different
      // hardware, so they share one run: clicking further out extends the
      // range, clicking back inside it shrinks it again.
      if (this.rangeBase === null) this.rangeBase = new Set(this.selection);
      this.applyRange();
      this.render();
      return;
    }

    this.rangeBase = null;

    if (additive || this.selecting) {
      if (this.selection.has(id)) this.selection.delete(id);
      else this.selection.add(id);
      this.lastAnchor = index;
      this.emitSelection();
      this.render();
      return;
    }

    this.lastAnchor = index;

    if (!isDouble) {
      this.dispatchEvent(new CustomEvent('open', { detail: { id, index } }));
    }
  }

  emitSelection() {
    this.selecting = this.selection.size > 0;
    this.container.classList.toggle('selecting', this.selecting);
    this.dispatchEvent(new CustomEvent('selection', {
      detail: { ids: [...this.selection] },
    }));
  }

  clearSelection() {
    this.selection.clear();
    this.rangeBase = null;
    this.emitSelection();
    this.render();
  }

  selectAll() {
    for (const cell of this.layout.cells) this.selection.add(cell.id);
    this.rangeBase = null;
    this.emitSelection();
    this.render();
  }

  /** Selection = what was there before this run, plus anchor…cursor. */
  applyRange() {
    const base = this.rangeBase || new Set();
    const [from, to] = [this.lastAnchor, this.cursor].sort((a, b) => a - b);
    this.selection.clear();
    for (const id of base) this.selection.add(id);
    for (let i = from; i <= to; i++) {
      const cell = this.layout.cells[i];
      if (cell) this.selection.add(cell.id);
    }
    this.emitSelection();
  }

  /* -- navigation ----------------------------------------------------- */

  get ids() {
    return this.layout.cells.map((c) => c.id);
  }

  /** Ids with a generated thumbnail (flag bit 2), for the viewer's filmstrip. */
  thumbedIds() {
    const set = new Set();
    for (const cell of this.layout.cells) if (cell.flags & 2) set.add(cell.id);
    return set;
  }

  indexOfId(id) {
    return this.layout.cells.findIndex((c) => c.id === id);
  }

  /** Where the screen is, as the photograph at its top and how far it sits
   *  from the top: what stays true when the window or the zoom changes. */
  anchor() {
    // Measured from the grid's own top: anything above it in the scroller
    // (On This Day) comes and goes, and must not move the place.
    const above = this.container.offsetTop || 0;
    const top = this.scroller.scrollTop - above;
    if (this.scroller.scrollTop <= 0) return { id: null, offset: 0 };
    const cell = this.layout.cells.find((c) => c.y + c.h > top);
    return cell ? { id: cell.id, offset: Math.round(cell.y - top) } : null;
  }

  /** Back to a place `anchor()` gave; false when that photograph is not in
   *  the grid any more. */
  scrollToAnchor(place) {
    if (!place) return false;
    if (place.id == null) { this.scroller.scrollTop = 0; return true; }
    const cell = this.layout.cells.find((c) => c.id === place.id);
    if (!cell) return false;
    this.scroller.scrollTop = Math.max(0, (this.container.offsetTop || 0) + cell.y - (place.offset || 0));
    this.render();
    return true;
  }

  /**
   * Move the keyboard cursor, and with Shift held, take the selection with it.
   *
   * The behaviour people already know from Explorer and Photos: Shift fixes an
   * anchor where the cursor was, and every Shift+Arrow after that selects the
   * run between the anchor and wherever the cursor has reached. Coming back
   * towards the anchor gives items up again — which is why the run remembers
   * what was selected before it started, rather than only ever adding.
   *
   * Up and down are worked out from where the tiles actually are, not from a
   * column count, because the justified and masonry layouts do not have one.
   */
  moveCursor(direction, { extend = false } = {}) {
    const cells = this.layout.cells;
    if (!cells.length) return;
    if (extend) {
      if (this.rangeBase === null) {
        this.lastAnchor = this.cursor < 0 ? 0 : this.cursor;
        this.rangeBase = new Set(this.selection);
      }
    } else {
      // A plain arrow ends the run but keeps what it selected: the next Shift
      // press starts a fresh range from here, as it does everywhere else.
      this.rangeBase = null;
      this.lastAnchor = this.cursor;
    }
    if (this.cursor < 0) {
      this.cursor = 0;
    } else if (direction === 'next') {
      this.cursor = Math.min(cells.length - 1, this.cursor + 1);
    } else if (direction === 'prev') {
      this.cursor = Math.max(0, this.cursor - 1);
    } else {
      const current = cells[this.cursor];
      const wantY = direction === 'down' ? current.y + current.h + 1 : current.y - 1;
      let best = this.cursor;
      let bestScore = Infinity;
      for (let i = 0; i < cells.length; i++) {
        const cell = cells[i];
        if (direction === 'down' ? cell.y < wantY : cell.y + cell.h > wantY + 1) continue;
        const score = Math.abs(cell.x - current.x) + Math.abs(cell.y - wantY) * 2;
        if (score < bestScore) {
          bestScore = score;
          best = i;
        }
      }
      this.cursor = best;
    }
    if (extend) this.applyRange();
    this.scrollToIndex(this.cursor);
    this.render();
  }

  scrollToIndex(index) {
    const cell = this.layout.cells[index];
    if (!cell) return;
    const top = this.scroller.scrollTop;
    const height = this.scroller.clientHeight;
    if (cell.y < top + 60) this.scroller.scrollTop = Math.max(0, cell.y - 70);
    else if (cell.y + cell.h > top + height) {
      this.scroller.scrollTop = cell.y + cell.h - height + 20;
    }
  }

  scrollToRatio(ratio) {
    const max = Math.max(0, this.layout.height - this.scroller.clientHeight);
    this.scroller.scrollTop = max * Math.max(0, Math.min(1, ratio));
  }

  currentId() {
    return this.layout.cells[this.cursor]?.id ?? null;
  }

  destroy() {
    this.scroller.removeEventListener('scroll', this.onScroll);
    this.resizeObserver.disconnect();
    clearTimeout(this.settleTimer);
    this.prefetcher.want([], { cancel: true });
  }
}

/**
 * Append `incoming` segments to `segments` in place; returns items added.
 *
 * A day cut in two by a piece boundary is joined back into one section, and
 * ids already present are dropped, so a photo that slid across the boundary
 * while the library changed between requests is not shown twice.
 */
export function mergeSegments(segments, incoming) {
  const held = new Set();
  for (const segment of segments) for (const item of segment.items) held.add(item[0]);
  let added = 0;
  for (const segment of incoming || []) {
    const items = segment.items.filter((item) => !held.has(item[0]));
    if (!items.length) continue;
    added += items.length;
    for (const item of items) held.add(item[0]);
    const last = segments[segments.length - 1];
    // A loop, not push(...items): spreading tens of thousands of arguments
    // overflows the call stack in some engines.
    if (last && last.key === segment.key) for (const item of items) last.items.push(item);
    else segments.push({ key: segment.key, items });
  }
  return added;
}

export { formatDuration, formatSection };
