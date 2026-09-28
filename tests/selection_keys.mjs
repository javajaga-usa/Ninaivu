/**
 * Shift+Arrow selection, the arithmetic without the browser.
 *
 * The grid's range logic is the part that goes wrong quietly: an add-only
 * range looks right while you are extending it and only reveals itself when
 * you come back the other way and the items you passed stay selected. That is
 * exactly what a person notices and cannot explain, so it is checked here.
 *
 * Run:  node tests/selection_keys.mjs
 */

import assert from 'node:assert/strict';
import { Grid } from '../ninaivu/static/js/grid.js';

/** A grid with `count` cells in rows of `perRow`, and nothing else real. */
function fakeGrid(count = 12, perRow = 4) {
  const grid = Object.create(Grid.prototype);
  const cells = [];
  for (let n = 0; n < count; n++) {
    cells.push({
      n,
      id: 100 + n,
      x: (n % perRow) * 110,
      // Rows are 110 apart for 100-tall tiles: the real layouts leave a gap,
      // and the vertical search steps just past the bottom edge to find the
      // next row. Butting the rows together here would test a layout that
      // does not exist.
      y: Math.floor(n / perRow) * 110,
      w: 100,
      h: 100,
    });
  }
  grid.layout = { cells };
  grid.selection = new Set();
  grid.cursor = -1;
  grid.lastAnchor = -1;
  grid.rangeBase = null;
  grid.emitSelection = () => {};
  grid.render = () => {};
  grid.scrollToIndex = () => {};
  return grid;
}

const ids = (grid) => [...grid.selection].sort((a, b) => a - b);
const shift = (grid, direction) => grid.moveCursor(direction, { extend: true });

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

console.log('Shift+Arrow selection');

check('a plain arrow selects nothing', () => {
  const grid = fakeGrid();
  grid.moveCursor('next');
  grid.moveCursor('next');
  assert.deepEqual(ids(grid), []);
  assert.equal(grid.cursor, 1);
});

check('shift+right selects from where the cursor was', () => {
  const grid = fakeGrid();
  grid.moveCursor('next');           // cursor at 0
  shift(grid, 'next');               // 0..1
  assert.deepEqual(ids(grid), [100, 101]);
});

check('extending further takes more', () => {
  const grid = fakeGrid();
  grid.moveCursor('next');
  shift(grid, 'next');
  shift(grid, 'next');
  assert.deepEqual(ids(grid), [100, 101, 102]);
});

check('coming back gives them up again', () => {
  const grid = fakeGrid();
  grid.moveCursor('next');
  shift(grid, 'next');
  shift(grid, 'next');
  shift(grid, 'prev');
  assert.deepEqual(ids(grid), [100, 101], 'the third one must be released');
});

check('crossing back past the anchor selects the other way', () => {
  const grid = fakeGrid();
  for (let i = 0; i < 5; i++) grid.moveCursor('next');   // cursor at 4
  shift(grid, 'prev');
  shift(grid, 'prev');
  assert.deepEqual(ids(grid), [102, 103, 104]);
});

check('shift+down takes a whole row', () => {
  const grid = fakeGrid(12, 4);
  grid.moveCursor('next');           // cursor at 0
  shift(grid, 'down');               // 0..4
  assert.deepEqual(ids(grid), [100, 101, 102, 103, 104]);
});

check('shift+up releases it', () => {
  const grid = fakeGrid(12, 4);
  grid.moveCursor('next');
  shift(grid, 'down');
  shift(grid, 'up');
  assert.deepEqual(ids(grid), [100]);
});

check('what was already selected survives the run', () => {
  const grid = fakeGrid();
  grid.selection.add(111);           // picked out by hand, far away
  grid.moveCursor('next');
  shift(grid, 'next');
  shift(grid, 'prev');
  assert.ok(grid.selection.has(111), 'an earlier pick must not be swallowed');
  assert.deepEqual(ids(grid), [100, 111]);
});

check('a plain arrow ends the run and keeps what it selected', () => {
  const grid = fakeGrid();
  grid.moveCursor('next');
  shift(grid, 'next');               // 0..1 selected
  grid.moveCursor('next');           // plain move to 2
  shift(grid, 'next');               // a fresh run, 2..3
  assert.deepEqual(ids(grid), [100, 101, 102, 103]);
});

check('a run that starts with no cursor lands on the first item', () => {
  // The first arrow press with no cursor places one rather than moving, which
  // is how a plain arrow already behaves. Shift therefore selects just it.
  const grid = fakeGrid();
  shift(grid, 'next');
  assert.deepEqual(ids(grid), [100]);
  assert.equal(grid.cursor, 0);
});

check('it stops at the ends instead of wrapping', () => {
  const grid = fakeGrid(4, 4);
  for (let i = 0; i < 10; i++) shift(grid, 'next');
  assert.deepEqual(ids(grid), [100, 101, 102, 103]);
  assert.equal(grid.cursor, 3);
});

console.log(failures ? `\n${failures} failed` : '\nall passed');
process.exit(failures ? 1 : 0);
