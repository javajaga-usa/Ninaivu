/**
 * Letting go of a Live Photo before its clip has arrived.
 *
 * @fixture pairs
 *
 * Holding the photograph asks the clip to play with its sound. When the
 * browser refuses sound (iOS Safari may: a press is not a tap there), the
 * clip plays muted instead. But the same fallback also caught the play that
 * letting go had cut short — `pause()` rejects a pending `play()` too — and
 * started the clip again, muted, after the hand was off the photograph. On a
 * slow connection or a clip being converted that was every short hold, and
 * the second press of the Live button (stop) did the same.
 *
 *   python tests/run_browser_tests.py tests/live_release_ui.mjs
 */
import { launch, ok, done, signInAtHome } from './harness.mjs';

const b = await launch();
// Reduced motion: nothing plays by itself when the viewer opens, so whatever
// plays is what the hand asked for.
const ctx = await b.newContext({
  serviceWorkers: 'block', viewport: { width: 1280, height: 860 }, reducedMotion: 'reduce',
});
const p = await ctx.newPage();
const errs = [];
p.on('pageerror', (e) => errs.push(e.message));

await signInAtHome(p);
await p.waitForFunction(() => document.querySelectorAll('#grid .cell.is-live').length > 0,
  { timeout: 20000 });

// The clip takes its time, as it does over the internet or while it is converted.
await ctx.route('**/api/live-video/**', async (route) => {
  await new Promise((resolve) => { setTimeout(resolve, 2500); });
  await route.continue().catch(() => {});
});

await p.locator('#grid .cell.is-live').first().click();
await p.waitForSelector('.viewer-stage .live-badge', { timeout: 8000 });
await p.evaluate(() => {
  const clip = document.querySelector('.viewer-stage .live-clip');
  window.__playedAfter = 0;
  window.__let = false;
  clip.addEventListener('playing', () => { if (window.__let) window.__playedAfter += 1; });
});

// Held long enough to ask for the clip, let go before it has come.
const box = await p.locator('.viewer-stage > img:not(.placeholder)').boundingBox();
await p.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
await p.mouse.down();
await p.waitForFunction(() => document.querySelector('.viewer-stage .live-badge.on'),
  null, { timeout: 3000 }).catch(() => {});
ok('holding asks for the clip',
  await p.locator('.viewer-stage .live-badge.on').count() === 1);
await p.mouse.up();
await p.evaluate(() => { window.__let = true; });
await p.waitForTimeout(5000);
ok('let go, the clip does not start afterwards',
  await p.evaluate(() => window.__playedAfter) === 0
  && await p.locator('.viewer-stage .live-clip.playing').count() === 0,
  `${await p.evaluate(() => window.__playedAfter)} plays after letting go`);

// The Live button: pressed, then pressed again to stop before the clip comes.
await p.evaluate(() => { window.__let = false; window.__playedAfter = 0; });
const clipSrc = await p.evaluate(() => document.querySelector('.viewer-stage .live-clip').currentSrc);
await p.evaluate((src) => {
  // A fresh load, so the clip is not there yet for the second press either.
  const clip = document.querySelector('.viewer-stage .live-clip');
  clip.src = `${src}${src.includes('?') ? '&' : '?'}again=1`;
}, clipSrc);
await p.locator('.viewer-stage .live-badge').click();
await p.waitForTimeout(150);
await p.locator('.viewer-stage .live-badge').click();
await p.evaluate(() => { window.__let = true; });
await p.waitForTimeout(5000);
ok('stopped with the Live button before it came, it stays stopped',
  await p.evaluate(() => window.__playedAfter) === 0
  && await p.locator('.viewer-stage .live-badge.on').count() === 0,
  `${await p.evaluate(() => window.__playedAfter)} plays after stopping`);

// A browser that refuses sound on a hold, as iOS Safari may: the clip plays
// muted, and letting go stops it.
await ctx.unroute('**/api/live-video/**');
await p.evaluate(() => {
  const native = HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play = function play() {
    if (!this.muted) return Promise.reject(new DOMException('no sound without a tap', 'NotAllowedError'));
    return native.call(this);
  };
  const clip = document.querySelector('.viewer-stage .live-clip');
  clip.src = clip.currentSrc.replace(/[?&]again=1$/, '');
  window.__let = false;
});
await p.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
await p.mouse.down();
await p.waitForFunction(() => document.querySelector('.viewer-stage .live-clip.playing'),
  null, { timeout: 8000 }).catch(() => {});
const refused = await p.evaluate(() => {
  const clip = document.querySelector('.viewer-stage .live-clip');
  return { playing: clip.classList.contains('playing'), muted: clip.muted };
});
ok('refused sound, the hold still plays the clip, muted',
  refused.playing && refused.muted, JSON.stringify(refused));
await p.mouse.up();
await p.waitForTimeout(400);
ok('and letting go stops it',
  await p.locator('.viewer-stage .live-clip.playing').count() === 0
  && await p.locator('.viewer-stage .live-badge.on').count() === 0);

ok('no page errors', errs.length === 0, errs.join(' | '));
await done(b);
