/**
 * Layout engines.
 *
 * Every mode produces the same shape — a flat array of absolutely positioned
 * cells plus sticky section headers — so the virtual scroller never needs to
 * know which layout is active. Positions are computed once per
 * (width, zoom, mode, data) change and then reused for every scroll frame.
 */

export const MODES = ['justified', 'masonry', 'grid', 'film'];

/** Target cell height/width per zoom step, in CSS pixels. */
const ZOOM_STEPS = [110, 150, 200, 270, 360];

export function targetSize(zoom) {
  return ZOOM_STEPS[Math.max(0, Math.min(ZOOM_STEPS.length - 1, zoom))];
}

/** Bucket size for the scroll-position index. */
const BUCKET = 600;

const HEADER_H = 42;
const HEADER_GAP = 6;

function makeIndex(cells, headers, height) {
  const buckets = new Map();
  const add = (bucket, kind, i) => {
    let list = buckets.get(bucket);
    if (!list) buckets.set(bucket, (list = []));
    list.push(kind === 'c' ? i : ~i); // ~i marks a header
  };
  for (let i = 0; i < cells.length; i++) {
    const c = cells[i];
    const from = Math.floor(c.y / BUCKET);
    const to = Math.floor((c.y + c.h) / BUCKET);
    for (let b = from; b <= to; b++) add(b, 'c', i);
  }
  for (let i = 0; i < headers.length; i++) {
    const h = headers[i];
    const from = Math.floor(h.y / BUCKET);
    const to = Math.floor((h.y + HEADER_H) / BUCKET);
    for (let b = from; b <= to; b++) add(b, 'h', i);
  }
  return { buckets, height, cells, headers, bucketSize: BUCKET };
}

/**
 * @param {Array<{key:string, items:Array<Array<number>>}>} segments
 * @param {{width:number, mode:string, zoom:number, gap:number, headers:boolean}} opts
 */
export function computeLayout(segments, opts) {
  const { width, mode, zoom } = opts;
  const gap = opts.gap ?? 6;
  const target = targetSize(zoom);
  const showHeaders = opts.headers !== false;

  const cells = [];
  const headers = [];
  let y = 0;
  let flatIndex = 0;
  const next = () => flatIndex++;

  // Days small enough to share a row. Worked out once per segment, and only
  // as far as the first item that makes it too wide, so this stays linear.
  const packing = showHeaders ? packPlan(segments, mode, width, gap, target) : null;

  for (let s = 0; s < segments.length; s++) {
    const segment = segments[s];
    if (!segment.items.length) continue;

    // A run of small days goes side by side, each under its own header.
    const run = packing ? packing.runFrom(s) : 1;
    if (run > 1) {
      y = layoutPacked(segments, s, run, packing, cells, headers, y, gap, next,
        () => flatIndex);
      y += 22;
      s += run - 1;
      continue;
    }

    if (showHeaders) {
      headers.push({
        key: segment.key,
        y,
        count: segment.items.length,
        firstCell: flatIndex,
      });
      y += HEADER_H + HEADER_GAP;
    }

    const before = flatIndex;
    if (mode === 'grid') {
      y = layoutGrid(segment, cells, y, width, gap, target, () => flatIndex++);
    } else if (mode === 'masonry') {
      y = layoutMasonry(segment, cells, y, width, gap, target, () => flatIndex++);
    } else if (mode === 'film') {
      y = layoutJustified(segment, cells, y, width, 2, Math.round(target * 0.55),
        () => flatIndex++, false);
    } else {
      y = layoutJustified(segment, cells, y, width, gap, target,
        () => flatIndex++, true);
    }
    if (flatIndex === before && headers.length) headers.pop();
    y += 22; // breathing room between sections
  }

  return makeIndex(cells, headers, Math.max(0, y - 22));
}

/* -- small days, side by side --------------------------------------- */

/** Space between two days sharing a row. */
const PACK_GAP = 20;
/** A day is only packed if it leaves at least a third of the row empty. */
const PACK_MAX = 0.66;
/** The narrowest a packed day may be: its header needs room for the date,
 *  the count and "Select all" (on two lines — see .section-head.packed). */
const PACK_MIN = 170;

/**
 * The tiles a day would have laid out on one row at the size every other day
 * uses, or null when it has too many to leave room beside it.
 *
 * The sizes are the ones the full-width layouts give a short last row — the
 * justified row at its target height, the grid's squares and the masonry's
 * columns at the width's own column size — so a packed day looks exactly like
 * the same day would on a row of its own, only with a neighbour.
 */
function naturalRow(segment, mode, width, gap, target, limit) {
  const tiles = [];
  let x = 0;
  let h = 0;
  let w = 0;
  if (mode === 'grid') {
    const cols = Math.max(1, Math.floor((width + gap) / (target + gap)));
    w = h = (width - gap * (cols - 1)) / cols;
  } else if (mode === 'masonry') {
    const cols = Math.max(1, Math.round(width / (target + gap)));
    w = (width - gap * (cols - 1)) / cols;
  } else if (mode === 'film') {
    h = Math.max(48, Math.round(target * 0.55));
  } else {
    h = Math.max(48, target);
  }
  let rowH = 0;
  for (const item of segment.items) {
    let tileW = w;
    let tileH = h;
    if (mode === 'masonry') {
      tileH = w / Math.max(0.3, Math.min(3.2, item[1] / 100));
    } else if (mode !== 'grid') {
      tileW = Math.max(0.3, item[1] / 100) * h;
    }
    if (tiles.length) x += gap;
    if (x + tileW > limit) return null;
    tiles.push({ x, w: tileW, h: tileH });
    x += tileW;
    rowH = Math.max(rowH, tileH);
  }
  return { tiles, width: x, height: rowH };
}

/**
 * Which consecutive days share a row.
 *
 * Greedy, left to right: a run takes small days while they fit the width and
 * stops at the first that does not (or at a day too big to pack at all). A
 * run of one is laid out exactly as before, full width, so a library of busy
 * days looks the way it always has.
 */
function packPlan(segments, mode, width, gap, target) {
  const limit = width * PACK_MAX;
  const minBlock = Math.min(PACK_MIN, Math.floor((width - PACK_GAP) / 2));
  const rows = new Map();          // segment index -> its naturalRow, or null
  const rowOf = (i) => {
    if (!rows.has(i)) {
      const segment = segments[i];
      rows.set(i, segment && segment.items.length && segment.key !== 'match'
        ? naturalRow(segment, mode, width, gap, target, limit) : null);
    }
    return rows.get(i);
  };
  const blockWidth = (row) => Math.max(minBlock, Math.ceil(row.width));
  return {
    rowOf,
    blockWidth,
    runFrom(start) {
      let used = 0;
      let run = 0;
      for (let i = start; i < segments.length; i++) {
        const row = rowOf(i);
        if (!row) break;
        const w = blockWidth(row);
        const needed = used + (run ? PACK_GAP : 0) + w;
        if (needed > width) break;
        used = needed;
        run++;
      }
      return run;
    },
  };
}

/** Lay out one row of small days, each under its own narrow header. */
function layoutPacked(segments, start, run, packing, cells, headers, startY, gap, next,
  position) {
  const top = startY + HEADER_H + HEADER_GAP;
  let x = 0;
  let rowH = 0;
  for (let k = 0; k < run; k++) {
    const segment = segments[start + k];
    const row = packing.rowOf(start + k);
    const w = packing.blockWidth(row);
    headers.push({
      key: segment.key,
      y: startY,
      x,
      w,
      count: segment.items.length,
      firstCell: position(),
    });
    segment.items.forEach((item, i) => {
      const tile = row.tiles[i];
      push(cells, item, x + tile.x, top, tile.w, tile.h, next);
    });
    rowH = Math.max(rowH, row.height);
    x += w + PACK_GAP;
  }
  return top + Math.round(rowH) + gap;
}

/* ------------------------------------------------------------------ */

function push(cells, item, x, y, w, h, next) {
  cells.push({
    n: next(),
    id: item[0],
    x: Math.round(x),
    y: Math.round(y),
    w: Math.round(w),
    h: Math.round(h),
    aspect: item[1] / 100,
    kind: item[2],
    flags: item[3],
    dur: item[4] || 0,
    // The thumbnail's version, so a rotated photograph is drawn from the
    // rewritten file rather than the one the browser cached a year ago.
    v: item[5] || 0,
    // Its colour, three hex digits, shown until the picture arrives.
    swatch: item[6] || '',
  });
}

/** Google-Photos style justified rows: every row fills the width exactly. */
function layoutJustified(segment, cells, startY, width, gap, target, next, stretch) {
  const items = segment.items;
  let y = startY;
  let row = [];
  let ratioSum = 0;

  const flush = (isLast) => {
    if (!row.length) return;
    const available = width - gap * (row.length - 1);
    let h = available / ratioSum;

    // A short final row must not be blown up to fill the width — that would
    // distort its aspect ratios. Cap it at the target height and leave it
    // ragged, the way every good photo grid does.
    let justified = true;
    if (isLast && (!stretch || h > target * 1.35)) {
      h = target;
      justified = false;
    }
    h = Math.max(48, h);

    let x = 0;
    for (let i = 0; i < row.length; i++) {
      const item = row[i];
      const ratio = Math.max(0.3, item[1] / 100);
      let w = ratio * h;
      if (i === row.length - 1 && justified) {
        w = Math.max(48, width - x); // absorb rounding so the row is flush
      }
      push(cells, item, x, y, w, h, next);
      x += w + gap;
    }
    y += Math.round(h) + gap;
    row = [];
    ratioSum = 0;
  };

  for (const item of items) {
    const ratio = Math.max(0.3, item[1] / 100);
    row.push(item);
    ratioSum += ratio;
    const available = width - gap * (row.length - 1);
    if (available / ratioSum < target) flush(false);
  }
  flush(true);
  return y;
}

/** Uniform squares — the densest, most scannable contact sheet. */
function layoutGrid(segment, cells, startY, width, gap, target, next) {
  const cols = Math.max(1, Math.floor((width + gap) / (target + gap)));
  const size = (width - gap * (cols - 1)) / cols;
  let y = startY;
  segment.items.forEach((item, i) => {
    const col = i % cols;
    if (col === 0 && i > 0) y += size + gap;
    push(cells, item, col * (size + gap), y, size, size, next);
  });
  return y + size + gap;
}

/** Pinterest-style columns: full aspect ratios, no cropping. */
function layoutMasonry(segment, cells, startY, width, gap, target, next) {
  const cols = Math.max(1, Math.round(width / (target + gap)));
  const colW = (width - gap * (cols - 1)) / cols;
  const heights = new Array(cols).fill(startY);

  for (const item of segment.items) {
    let shortest = 0;
    for (let c = 1; c < cols; c++) if (heights[c] < heights[shortest]) shortest = c;
    const ratio = Math.max(0.3, Math.min(3.2, item[1] / 100));
    const h = colW / ratio;
    push(cells, item, shortest * (colW + gap), heights[shortest], colW, h, next);
    heights[shortest] += h + gap;
  }
  return Math.max(...heights);
}

/* ------------------------------------------------------------------ */

/** Cells (and headers) intersecting a scroll window, via the bucket index. */
export function visibleRange(layout, top, bottom) {
  const first = Math.max(0, Math.floor(top / layout.bucketSize));
  const last = Math.floor(bottom / layout.bucketSize);
  const cellIdx = new Set();
  const headIdx = new Set();
  for (let b = first; b <= last; b++) {
    const list = layout.buckets.get(b);
    if (!list) continue;
    for (const value of list) {
      if (value < 0) headIdx.add(~value);
      else cellIdx.add(value);
    }
  }
  const cells = [];
  for (const i of cellIdx) {
    const c = layout.cells[i];
    if (c.y + c.h >= top && c.y <= bottom) cells.push(c);
  }
  const headers = [];
  for (const i of headIdx) headers.push(layout.headers[i]);
  return { cells, headers };
}

/**
 * The marks down the timeline scrubber: one per month, as fractions of the
 * rail, never two closer than `minGap` pixels on a rail `railHeight` tall.
 * `height` is the scroll range the rail stands for (a header at `height` or
 * below it sits at the bottom).
 *
 * `year` marks the first month shown of a new year (and the first mark of
 * all), which the rail labels with the year instead of the month — "Dec",
 * "Nov", "2024", "Dec"… reads at a glance, where "Sept 25" read like a day.
 * A year's mark is worth more than a month's, so one that would crowd the
 * month shown just before it takes that month's place instead of being
 * dropped. Linear in the number of headers.
 *
 * Sections that are not dates ("match" for search results, "unknown" for the
 * undated) get a mark of their own, with `month` null.
 */
export function scrubberTicks(headers, height, railHeight, minGap = 26) {
  const total = height || 1;
  const rail = Math.max(1, railHeight);
  const ticks = [];
  const seen = new Set();
  let lastYear = null;
  for (const head of headers) {
    const dated = /^\d{4}-\d{2}/.test(head.key);
    const month = dated ? head.key.slice(0, 7) : head.key;
    if (seen.has(month)) continue;
    const frac = Math.min(1, head.y / total);
    const year = dated ? month.slice(0, 4) : null;
    const tick = { key: head.key, month: dated ? month : null, frac, year: false };
    tick.year = dated && year !== lastYear;
    const last = ticks[ticks.length - 1];
    if (last && (frac - last.frac) * rail < minGap) {
      // Too close. Only a new year pushes out the month before it.
      if (!tick.year || last.year) continue;
      const before = ticks[ticks.length - 2];
      if (before && (frac - before.frac) * rail < minGap) continue;
      ticks.pop();
    }
    seen.add(month);
    if (dated) lastYear = year;
    ticks.push(tick);
  }
  return ticks;
}

/** Which section is under a given scroll offset (drives the scrubber label). */
export function sectionAt(layout, offset) {
  const list = layout.headers;
  if (!list.length) return null;
  let lo = 0;
  let hi = list.length - 1;
  let found = list[0];
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (list[mid].y <= offset) {
      found = list[mid];
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  return found;
}

export { HEADER_H };
