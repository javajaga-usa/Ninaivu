import { launch, ok, done, HOME, ADMIN } from './harness.mjs';
const browser = await launch();
const ctx = await browser.newContext({ viewport: { width: 1600, height: 1000 }, colorScheme: 'dark' });
const page = await ctx.newPage();
page.on('pageerror', e => console.log('PAGEERROR', e.message));

const N_DAYS = 2000, PER_DAY = 50;
await page.route('**/api/segments*', async route => {
  const segments = []; let id = 1;
  for (let d = 0; d < N_DAYS; d++) {
    const day = new Date(Date.now() - d * 86400000).toISOString().slice(0, 10);
    const items = [];
    for (let i = 0; i < PER_DAY; i++) items.push([id++, 80 + ((id * 37) % 120), id % 13 === 0 ? 1 : 0, 2 | (id % 11 === 0 ? 1 : 0), id % 13 === 0 ? 42 : 0]);
    segments.push({ key: day, items });
  }
  await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ total: id-1, returned: id-1, truncated:false, semantic:false, segments, thumb_sizes:[256,640] }) });
});
const px = Buffer.from('R0lGODlhAQABAIAAAP///wAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw==','base64');
await page.route('**/api/thumb/**', r => r.fulfill({ contentType:'image/gif', headers:{'cache-control':'max-age=99999'}, body: px }));

const t0 = Date.now();
await page.goto(HOME, { waitUntil: 'domcontentloaded' });
await page.waitForFunction(() => window.__mv && document.querySelectorAll('.cell').length > 5, null, { timeout: 30000 });
console.log(`grid usable after: ${Date.now()-t0} ms   (${(N_DAYS*PER_DAY).toLocaleString()} items)`);

const r = await page.evaluate(({ N_DAYS, PER_DAY }) => {
  const g = window.__mv.grid;
  const el = g.scroller;
  const out = {};
  out.itemsInLayout = g.layout.cells.length;
  out.cellsInDom = document.querySelectorAll('.cell').length;
  out.virtualHeightPx = Math.round(g.layout.height);

  // 1. Full layout recompute for the whole library.
  let t = performance.now();
  for (let i = 0; i < 5; i++) g.relayout(false);
  out.fullRelayoutMs = +((performance.now()-t)/5).toFixed(1);

  // 2. Cost of one virtualised render pass at a random scroll offset.
  const samples = [];
  for (let i = 0; i < 200; i++) {
    el.scrollTop = Math.random() * (g.layout.height - el.clientHeight);
    const s = performance.now();
    g.render();
    samples.push(performance.now() - s);
  }
  samples.sort((a,b)=>a-b);
  out.renderMedianMs = +samples[100].toFixed(2);
  out.renderP95Ms = +samples[190].toFixed(2);
  out.renderWorstMs = +samples[199].toFixed(2);

  // 3. Realistic continuous scroll: 120 px per frame.
  return new Promise(resolve => {
    el.scrollTop = 0;
    const frames = []; let last = performance.now(); let n = 0;
    const step = (t) => {
      frames.push(t - last); last = t;
      el.scrollTop += 120;
      if (++n < 180) requestAnimationFrame(step);
      else {
        const f = frames.slice(10).sort((a,b)=>a-b);
        out.scrollMedianFrameMs = +f[Math.floor(f.length/2)].toFixed(1);
        out.scrollP95FrameMs = +f[Math.floor(f.length*0.95)].toFixed(1);
        out.scrollFps = Math.round(1000 / f[Math.floor(f.length/2)]);
        resolve(out);
      }
    };
    requestAnimationFrame(step);
  });
}, { N_DAYS, PER_DAY });

console.log(JSON.stringify(r, null, 1));
await browser.close();
