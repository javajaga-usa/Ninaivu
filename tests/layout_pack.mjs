/**
 * Small days side by side, and the scrubber's marks — the arithmetic
 * without the browser.
 *
 * A run of days with a photograph or two each used to give every day a row of
 * its own, one tile and two thirds of the screen empty. They now share a row,
 * each under its own narrow header. What must not change is everything else:
 * a busy day is laid out exactly as it was, the cells stay in date order (the
 * keyboard, Shift ranges and "Select all" count on it), and every header
 * still points at its own first photograph.
 *
 * Run:  node tests/layout_pack.mjs
 */

import assert from 'node:assert/strict';
import { computeLayout, scrubberTicks, sectionAt } from '../ninaivu/static/js/layout.js';
import { folderDate } from '../ninaivu/static/js/i18n.js';

let nextId = 1;
/** A day of `count` photographs, all `aspect` wide for 1 tall. */
function day(key, count, aspect = 1.5) {
  const items = [];
  for (let i = 0; i < count; i++) items.push([nextId++, Math.round(aspect * 100), 0, 2, 0, 0, '']);
  return { key, items };
}

const OPTS = { width: 1200, mode: 'justified', zoom: 2, gap: 6 };   // target 200px

let failures = 0;
function check(name, fn) {
  try {
    fn();
    console.log(`  ok   ${name}`);
  } catch (error) {
    failures += 1;
    console.log(`  FAIL ${name}\n       ${error.message}`);
  }
}

console.log('Packing small days');

check('three one-photo days share one row, each under its own header', () => {
  const layout = computeLayout([day('2025-05-11', 1), day('2025-05-10', 1), day('2025-05-09', 1)], OPTS);
  assert.equal(layout.headers.length, 3);
  assert.deepEqual(layout.headers.map((h) => h.y), [0, 0, 0]);
  // 1.5 × 200 = 300 wide each, 20 apart.
  assert.deepEqual(layout.headers.map((h) => h.x), [0, 320, 640]);
  assert.deepEqual(layout.headers.map((h) => h.w), [300, 300, 300]);
  assert.deepEqual(layout.headers.map((h) => h.firstCell), [0, 1, 2]);
  assert.deepEqual(layout.cells.map((c) => c.n), [0, 1, 2]);
  assert.deepEqual(layout.cells.map((c) => [c.x, c.y, c.w, c.h]),
    [[0, 48, 300, 200], [320, 48, 300, 200], [640, 48, 300, 200]]);
  assert.equal(layout.height, 48 + 200 + 6);
});

check('a day too big to share keeps its full-width row', () => {
  const layout = computeLayout([day('2025-05-11', 12), day('2025-05-10', 1), day('2025-05-09', 1)], OPTS);
  const [busy, ...small] = layout.headers;
  assert.equal(busy.w, undefined);
  assert.equal(busy.x, undefined);
  assert.equal(small[0].y, small[1].y);
  assert.ok(small[0].y > busy.y);
  assert.equal(small[1].firstCell, 13);
});

check('a small day with nobody beside it is laid out as it always was', () => {
  const alone = computeLayout([day('2025-05-11', 1)], OPTS);
  assert.equal(alone.headers[0].w, undefined);
  assert.deepEqual([alone.cells[0].x, alone.cells[0].y, alone.cells[0].w, alone.cells[0].h],
    [0, 48, 300, 200]);
});

check('days that do not fit the width go on to the next row', () => {
  const layout = computeLayout([1, 2, 3, 4, 5].map((d) => day(`2025-05-0${d}`, 1)), OPTS);
  const ys = layout.headers.map((h) => h.y);
  assert.equal(ys[0], ys[2]);          // 300 + 20 + 300 + 20 + 300 = 940
  assert.ok(ys[3] > ys[0]);            // a fourth would need 1260
  assert.equal(ys[3], ys[4]);
  // Still in order, so sectionAt and the scrubber find them.
  for (let i = 1; i < ys.length; i++) assert.ok(ys[i] >= ys[i - 1]);
  assert.equal(sectionAt(layout, ys[3] + 1).y, ys[3]);
});

check('a narrow day is given room for its header', () => {
  const layout = computeLayout([day('2025-05-11', 1, 0.5), day('2025-05-10', 1, 0.5)], OPTS);
  assert.deepEqual(layout.headers.map((h) => h.w), [170, 170]);
  assert.deepEqual(layout.cells.map((c) => c.x), [0, 190]);
});

check('the grid packs its squares at the size every other day uses', () => {
  const opts = { ...OPTS, mode: 'grid' };
  const full = computeLayout([day('2025-05-11', 30)], opts).cells[0];
  const layout = computeLayout([day('2025-05-10', 1), day('2025-05-09', 2)], opts);
  assert.equal(layout.headers.length, 2);
  assert.equal(layout.headers[0].y, layout.headers[1].y);
  for (const cell of layout.cells) assert.equal(cell.w, full.w);
});

check('without headers nothing is packed', () => {
  const layout = computeLayout([day('2025-05-11', 1), day('2025-05-10', 1)],
    { ...OPTS, headers: false });
  assert.equal(layout.headers.length, 0);
  assert.ok(layout.cells[1].y > layout.cells[0].y);
});

console.log('Scrubber marks');

const head = (key, y) => ({ key, y });

check('one mark per month, the year written where it changes', () => {
  const ticks = scrubberTicks([
    head('2025-12-30', 0), head('2025-12-02', 50), head('2025-11-20', 200),
    head('2025-01-03', 400), head('2024-12-24', 600), head('2024-11-01', 800),
  ], 1000, 1000);
  assert.deepEqual(ticks.map((t) => t.month), ['2025-12', '2025-11', '2025-01', '2024-12', '2024-11']);
  assert.deepEqual(ticks.map((t) => t.year), [true, false, false, true, false]);
});

check('no two marks closer than the gap; a new year displaces the month before it', () => {
  const ticks = scrubberTicks([
    head('2025-02-01', 0), head('2025-01-01', 100), head('2024-12-01', 110),
    head('2024-11-01', 120), head('2024-10-01', 500),
  ], 1000, 1000, 26);
  assert.deepEqual(ticks.map((t) => t.month), ['2025-02', '2024-12', '2024-10']);
  for (let i = 1; i < ticks.length; i++) assert.ok((ticks[i].frac - ticks[i - 1].frac) * 1000 >= 26);
});

check('marks never run past the bottom of the rail', () => {
  const ticks = scrubberTicks([head('2025-02-01', 0), head('2025-01-01', 5000)], 1000, 1000);
  assert.equal(ticks[1].frac, 1);
});

check('the undated get a mark of their own', () => {
  const ticks = scrubberTicks([head('2025-02-01', 0), head('unknown', 800)], 1000, 1000);
  assert.equal(ticks[1].month, null);
  assert.equal(ticks[1].key, 'unknown');
});

console.log('Folders named by date');

check('a date-shaped folder reads as a date', () => {
  const text = folderDate('2025/05/11');
  assert.ok(text && text.includes('2025') && text.includes('11') && /May/.test(text), text);
  assert.ok(/May/.test(folderDate('2025-05')), folderDate('2025-05'));
  assert.ok(folderDate('2025').includes('2025'));
});

check('anything else keeps its own name', () => {
  assert.equal(folderDate('Trips/Ooty'), null);
  assert.equal(folderDate('2025/02/30'), null);
  assert.equal(folderDate('2025/13'), null);
  assert.equal(folderDate('IMG_2025'), null);
});

console.log(failures ? `\n${failures} failed` : '\nall passed');
process.exit(failures ? 1 : 0);
