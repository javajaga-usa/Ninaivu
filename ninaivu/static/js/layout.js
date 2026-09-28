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

  for (const segment of segments) {
    if (!segment.items.length) continue;

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
