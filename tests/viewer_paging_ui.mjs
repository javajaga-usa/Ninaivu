/**
 * Paging fast through the viewer does not leave a queue of originals behind.
 *
 * @fixture busy
 *
 * Each photograph opened warms its neighbours: the next three the way the
 * person is going, fetched in full, since the viewer shows the original. Those
 * fetches could not be called off, nor could the photograph paged past, so
 * holding the arrow key through forty left 43 originals downloading (5 now) —
 * several megabytes each from a phone — in front of the one on screen, on the six
 * connections a browser allows. Over the internet that was minutes of
 * downloads nobody would look at. Warming now lets go of a neighbour that is
 * no longer one, and the stage stops the photograph it is clearing.
 */
import { launch, ok, done, signInAtHome } from './harness.mjs';

const b = await launch();
const ctx = await b.newContext({ viewport: { width: 1280, height: 860 }, serviceWorkers: 'block' });
const p = await ctx.newPage();
const errs = [];
p.on('pageerror', (e) => errs.push(e.message));
await signInAtHome(p);
await p.waitForFunction(() => document.querySelectorAll('#grid .cell').length > 20, null, { timeout: 30000 });

// Everything takes its time, as it does from a phone over the internet.
const cdp = await ctx.newCDPSession(p);
await cdp.send('Network.enable');
const inFlight = new Map(); let seen = 0; let cancelled = 0;
cdp.on('Network.requestWillBeSent', (e) => {
  if (e.request.url.includes("/api/file/")) { seen++; inFlight.set(e.requestId, e.request.url); }
});
cdp.on('Network.loadingFinished', (e) => inFlight.delete(e.requestId));
cdp.on("Network.loadingFailed", (e) => { if (inFlight.delete(e.requestId) && e.canceled) cancelled++; });
await p.locator('#grid .cell').first().click();
await p.waitForSelector('.viewer-stage img', { timeout: 8000 });
await p.waitForTimeout(600);
await ctx.route('**/api/file/**', async (route) => {
  await new Promise((resolve) => { setTimeout(resolve, 5000); });
  await route.continue().catch(() => {});
});
for (let i = 0; i < 40; i++) {
  await p.keyboard.press('ArrowRight');
  await p.waitForTimeout(60);
}
await p.waitForTimeout(800);
const pending = [...inFlight.values()];
ok('paging through forty leaves only the neighbours of where it stopped downloading',
  pending.length <= 6, `${pending.length} originals still downloading, ${seen} asked for, ${cancelled} cancelled`);
ok('no page errors', errs.length === 0, errs.join(' | '));
await done(b);
