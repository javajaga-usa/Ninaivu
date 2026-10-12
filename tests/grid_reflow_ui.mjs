/**
 * Making tiles does not make the browser lay the grid out once per tile.
 *
 * Each tile made during a scroll asked the scroller where the screen was
 * (`scrollTop`, to choose a fetch priority) after the tile before it had been
 * placed. Reading a scroll position after a change to the page lays the page
 * out again there and then, so a frame that made forty tiles laid the grid out
 * forty times: on a 50,000-item album that was 311 long tasks and 25 s of them
 * in three scrolls from top to bottom, against 100 and 6.5 s when the screen is
 * read once per render.
 *
 * Counted here as reads of the scroller's position during one render, which
 * does not depend on how fast this machine is.
 */
import { launch, ok, done, signInAtHome } from './harness.mjs';

const b = await launch();
const ctx = await b.newContext({ viewport: { width: 1400, height: 900 }, serviceWorkers: 'block' });
const p = await ctx.newPage();
const errs = [];
p.on('pageerror', (e) => errs.push(e.message));
await signInAtHome(p);

// A large album, served by the test.
await p.route('**/api/segments*', async (route) => {
  const segments = [];
  let id = 1;
  for (let d = 0; d < 400; d++) {
    const day = new Date(Date.now() - d * 86400000).toISOString().slice(0, 10);
    const items = [];
    for (let i = 0; i < 50; i++) { items.push([id, 80 + ((id * 37) % 120), 0, 2, 0]); id++; }
    segments.push({ key: day, items });
  }
  await route.fulfill({ contentType: 'application/json', body: JSON.stringify({
    total: id - 1, returned: id - 1, truncated: false, semantic: false, segments, thumb_sizes: [256, 640] }) });
});
const px = Buffer.from('R0lGODlhAQABAIAAAP///wAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw==', 'base64');
await p.route('**/api/thumb/**', (r) => r.fulfill({ contentType: 'image/gif', body: px }));
await p.reload({ waitUntil: 'domcontentloaded' });
await p.waitForFunction(() => window.__mv && window.__mv.grid.layout.cells.length >= 20000, null,
  { timeout: 30000 });
await p.waitForTimeout(500);

const counted = await p.evaluate(() => {
  const g = window.__mv.grid;
  const el = g.scroller;
  const proto = Object.getPrototypeOf(el);
  const find = (name) => {
    for (let o = proto; o; o = Object.getPrototypeOf(o)) {
      const d = Object.getOwnPropertyDescriptor(o, name);
      if (d) return d;
    }
    return null;
  };
  const top = find('scrollTop');
  const height = find('clientHeight');
  let reads = 0;
  Object.defineProperty(el, 'scrollTop', {
    configurable: true,
    get() { reads += 1; return top.get.call(this); },
    set(v) { top.set.call(this, v); },
  });
  Object.defineProperty(el, 'clientHeight', {
    configurable: true,
    get() { reads += 1; return height.get.call(this); },
  });
  const results = [];
  for (const ratio of [0.25, 0.5, 0.75]) {
    // A jump to a part of the album with nothing made yet, as a fling does.
    el.scrollTop = ratio * (g.layout.height - el.clientHeight);
    g.scrolling = true;
    const before = g.mounted.size;
    reads = 0;
    g.render();
    results.push({ reads, made: g.mounted.size, before });
  }
  delete el.scrollTop;
  delete el.clientHeight;
  return results;
});
for (const { reads, made } of counted) {
  ok(`a render making ${made} tiles reads the screen a few times, not once per tile`,
    made > 20 && reads <= 6, `${reads} reads for ${made} tiles`);
}
ok('no page errors', errs.length === 0, errs.join(' | '));
await done(b);
