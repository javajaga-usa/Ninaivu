/**
 * The offline worker after an update: the new scripts, not last week's.
 *
 * The page names its entry point with a fingerprint (app.js?v=…), but the
 * modules app.js imports are asked for bare (./grid.js). The worker answered
 * those from its own cache first and refreshed them behind, so the first visit
 * after an update ran the new app.js against the old modules — an export one
 * of them no longer had, or had not yet, and the page never started.
 *
 * And thumbnails: the worker sent each one with `cache: 'no-store'`, so every
 * reload downloaded every picture again in full, though each carries an ETag
 * and the server checks access before answering 304.
 *
 * Changes a module on disk while the server runs and puts it back after.
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { launch, ok, done, signInAtHome } from './harness.mjs';

const MODULE = new URL('../ninaivu/static/js/enter-key.js', import.meta.url);
const original = readFileSync(MODULE, 'utf8');

const b = await launch();
const context = await b.newContext({ viewport: { width: 1200, height: 800 } });
const page = await context.newPage();
try {
  await signInAtHome(page);
  await page.evaluate(() => navigator.serviceWorker.ready);
  // Under the worker now, so it has cached what the page loaded.
  await page.reload({ waitUntil: 'networkidle' });
  ok('the worker controls the page',
    await page.evaluate(() => Boolean(navigator.serviceWorker.controller)));

  writeFileSync(MODULE, `${original}\nglobalThis.__updatedModule = true;\n`);
  await page.reload({ waitUntil: 'networkidle' });
  ok('a module changed on the server is the one the page runs, on the first visit after',
    await page.evaluate(() => globalThis.__updatedModule === true));

  // And with no server, the modules still come from the worker's copy.
  await context.setOffline(true);
  await page.reload({ waitUntil: 'load' }).catch(() => {});
  ok('offline, the shell still runs the modules it kept',
    await page.evaluate(() => globalThis.__updatedModule === true));
  await context.setOffline(false);
  await page.reload({ waitUntil: 'networkidle' });

  // Thumbnails revalidate rather than download again. The worker's own
  // resource timings say what crossed the wire: Chromium counts a response's
  // headers as 300 bytes, so a download is the body plus 300 and a 304 is the
  // 300 alone. Asked for with fetch(), because an
  // <img> on a reload can be answered from memory without reaching the worker.
  const worker = context.serviceWorkers()[0];
  await worker.evaluate(() => performance.setResourceTimingBufferSize(10000));
  await page.waitForSelector('#grid img[src*="/api/thumb/"]', { state: 'attached', timeout: 20000 });
  const src = await page.$eval('#grid img[src*="/api/thumb/"]', (img) => img.getAttribute('src'));
  await page.evaluate(async (url) => { await (await fetch(url)).blob(); }, src);
  await worker.evaluate(() => performance.clearResourceTimings());
  await page.evaluate(async (url) => { await (await fetch(url)).blob(); }, src);
  const thumbs = await worker.evaluate(() => performance.getEntriesByType('resource')
    .filter((entry) => entry.name.includes('/api/thumb/'))
    .map((entry) => [entry.transferSize, entry.encodedBodySize]));
  ok('the thumbnail went through the worker', thumbs.length === 1, JSON.stringify(thumbs));
  ok('a thumbnail seen before is revalidated, not downloaded again',
    thumbs.length === 1 && thumbs[0][0] < thumbs[0][1] + 300, JSON.stringify(thumbs));
} finally {
  writeFileSync(MODULE, original);
}
await done(b);
