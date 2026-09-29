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

const OVERSCAN = 900;      // px rendered above and below the viewport
const POOL_LIMIT = 400;
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
/** Faster than this (px/ms: 2,500 px a second) is flying past rather than
 *  looking, and tiles take the small thumbnail — a quarter of the bytes. */
const FLING = 2.5;
/** No scroll for this long and it has stopped: sharpen, and look ahead. */
const SETTLE_MS = 140;
/** Screens fetched ahead of where a scroll stopped, in its direction. */
const PREFETCH_SCREENS = 1.5;

const KIND_NAMES = ['picture', 'video', 'audio'];

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
 * for. Changing what is wanted cancels what no longer is.
 */
class Prefetcher {
  constructor(limit = 4) {
    this.limit = limit;
    this.queue = [];
    this.active = new Map();     // url -> Image in flight
    this.done = new Set();
  }

  want(urls) {
    const wanted = new Set(urls);
    for (const [url, image] of this.active) {
      if (!wanted.has(url)) {
        this.active.delete(url);
        image.removeAttribute('src');      // stops the download
      }
    }
    if (this.done.size > 4000) this.done.clear();
    this.queue = urls.filter((url) => !this.active.has(url) && !this.done.has(url));
    this.pump();
  }

  pump() {
    while (this.active.size < this.limit && this.queue.length) {
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
    // How fast, and which way, the scroll is going (px per ms; down is +).
    this.velocity = 0;
    this.direction = 1;
    this.lastTop = 0;
    this.lastTime = 0;
    this.fast = false;
    this.settleTimer = null;
    this.prefetcher = new Prefetcher();

    this.onScroll = this.onScroll.bind(this);
    this.scroller.addEventListener('scroll', this.onScroll, { passive: true });

    this.resizeObserver = new ResizeObserver(() => this.measure());
    this.resizeObserver.observe(this.scroller);

    this.container.addEventListener('click', (event) => this.onClick(event));
    this.container.addEventListener('dblclick', (event) => this.onClick(event, true));
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
    this.prefetcher.want([]);
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
    this.dispatchEvent(new CustomEvent('layout', { detail: this.layout }));
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
    const direction = this.velocity < 0 ? -1 : this.velocity > 0 ? 1 : this.direction;
    // Turned round: what was fetched ahead is now behind.
    if (direction !== this.direction) this.prefetcher.want([]);
    this.direction = direction;
    this.lastTop = top;
    this.lastTime = now;
    this.fast = Math.abs(this.velocity) > FLING;
    clearTimeout(this.settleTimer);
    this.settleTimer = setTimeout(() => this.settle(), SETTLE_MS);
  }

  /** The band of tiles kept ready: longer ahead of a fast scroll, shorter behind. */
  band() {
    const view = this.scroller.clientHeight;
    const speed = Math.abs(this.velocity);
    const ahead = Math.min(OVERSCAN + speed * LOOKAHEAD_MS, OVERSCAN + view * MAX_AHEAD_SCREENS);
    const behind = this.fast ? TRAILING : OVERSCAN;
    const top = this.scroller.scrollTop;
    return this.direction >= 0
      ? [top - behind, top + view + ahead]
      : [top - ahead, top + view + behind];
  }

  /**
   * The scroll has stopped. Tiles that took the small thumbnail while flying
   * past get the sharp one — drawn over the small one when it arrives, so
   * nothing flickers — and the next screen and a half in the direction it was
   * going is fetched, so carrying on finds it ready.
   */
  settle() {
    this.velocity = 0;
    this.lastTime = 0;
    this.fast = false;
    this.render();
    const view = this.scroller.clientHeight;
    const top = this.scroller.scrollTop;
    const [from, to] = this.direction >= 0
      ? [top + view + OVERSCAN, top + view + OVERSCAN + view * PREFETCH_SCREENS]
      : [top - OVERSCAN - view * PREFETCH_SCREENS, top - OVERSCAN];
    const urls = [];
    for (const cell of visibleRange(this.layout, from, to).cells) {
      if (!this.mounted.has(cell.n)) {
        const url = this.cellThumbUrl(cell);
        if (url) urls.push(url);
      }
    }
    this.prefetcher.want(urls);
  }

  onScreen(cell) {
    const top = this.scroller.scrollTop;
    return cell.y + cell.h > top && cell.y < top + this.scroller.clientHeight;
  }

  /**
   * The sharp picture for a tile that took the small one flying past — once
   * the scroll is not flying, and only as it comes near the screen, so the ones
   * about to be looked at are not queued behind ones a screen further on.
   */
  sharpen(node, cell) {
    if (this.fast) return;
    const img = node.querySelector('img');
    const full = this.cellThumbUrl(cell);
    if (!full || !img || img.hidden || img.getAttribute('src') === full) return;
    const top = this.scroller.scrollTop;
    const view = this.scroller.clientHeight;
    if (cell.y + cell.h < top - view / 2 || cell.y > top + view * 1.5) return;
    img.fetchPriority = this.onScreen(cell) ? 'high' : 'auto';
    img.src = full;
  }

  render() {
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
        node.style.transform = `translate3d(${head.x || 0}px,${head.y}px,0)`;
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
      img.classList.add('ready');
      node.classList.remove('loading');
    });
    img.addEventListener('error', () => {
      clearTimeout(node.shimmerTimer);
      node.classList.remove('loading');
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
    // The sharp picture if a parked node holds it; while flying past, the
    // small one will do just as well.
    const urls = [this.cellThumbUrl(cell)];
    if (this.fast) urls.push(this.cellThumbUrl(cell, true));
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

  releaseCell(node) {
    node.remove();
    clearTimeout(node.shimmerTimer);
    if (this.pool.length < POOL_LIMIT) {
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
    for (const node of this.mounted.values()) this.releaseCell(node);
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
    node.style.transform = `translate3d(${cell.x}px,${cell.y}px,0)`;
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
      // Already holding the sharp one: keep it. Flying past: the small one,
      // sharpened when the scroll stops (see settle).
      const full = this.cellThumbUrl(cell);
      const url = img.getAttribute('src') === full || !this.fast
        ? full : this.cellThumbUrl(cell, true);
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
      if (img.getAttribute('src') !== url || (img.complete && !img.naturalWidth)) {
        img.src = url;
      }
      // A picture that is already here and decoded is shown at once.
      const shown = img.complete && img.naturalWidth > 0;
      img.classList.toggle('ready', shown);
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

    if (kind !== 'picture') {
      const badge = document.createElement('span');
      badge.className = 'badge kind';
      badge.innerHTML = ICON[kind];
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
    this.prefetcher.want([]);
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
