/**
 * Viewer fit check.
 *
 * Regression guard for: a portrait photo rendering at its natural size and
 * running off the bottom of the screen, so you only saw the top half. The
 * cause was a CSS grid track sizing to its content, which made `max-height:
 * 100%` on the image mean "100% of the image".
 *
 *   node tests/fit.mjs           # against a running Ninaivu on :5000
 *
 * Every shape must report overflows* = false at every viewport size.
 */
import { launch, ok, done, HOME, ADMIN } from './harness.mjs';
import fs from 'fs';
const b = await launch();
fs.mkdirSync('/tmp/fit', { recursive: true });
const ctx = await b.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: 'dark', deviceScaleFactor: 2 });
const p = await ctx.newPage();
p.on('pageerror', e => console.log('PAGEERROR', e.message));
await p.goto(HOME, { waitUntil: 'networkidle' }); await p.waitForTimeout(2000);
// Sign in as the admin so every shape is visible.
if (await p.locator('#gate').isVisible()) {
  const byName = p.locator('.gate-skip').filter({ hasText: 'username' });
  if (await byName.count()) { await byName.click(); await p.waitForTimeout(600); }
  await p.fill('input[name="username"]', 'alex');
  await p.fill('input[name="password"]', 'correcthorse1');
  await p.click('.gate-submit');
  await p.waitForTimeout(3000);
  await p.keyboard.press('Escape');
  await p.waitForTimeout(600);
}
console.log('cells:', await p.locator('.cell').count());
await p.screenshot({path:'/tmp/fit/00-grid.png'});

const n = await p.locator('.cell').count();
for (let i = 0; i < n; i++) {
  await p.locator('.cell').nth(i).click();
  await p.waitForTimeout(1600);
  const info = await p.evaluate(() => {
    const img = document.querySelector('#viewer-stage img:not(.placeholder)');
    if (!img) return null;
    const r = img.getBoundingClientRect();
    const stage = document.querySelector('#viewer-stage').getBoundingClientRect();
    return {
      name: document.querySelector('#v-name')?.textContent,
      natural: `${img.naturalWidth}x${img.naturalHeight}`,
      shown: `${Math.round(r.width)}x${Math.round(r.height)}`,
      top: Math.round(r.top), bottom: Math.round(r.bottom),
      left: Math.round(r.left), right: Math.round(r.right),
      viewportH: window.innerHeight, viewportW: window.innerWidth,
      overflowsTop: r.top < 0, overflowsBottom: r.bottom > window.innerHeight,
      overflowsLeft: r.left < 0, overflowsRight: r.right > window.innerWidth,
      stageH: Math.round(stage.height),
    };
  });
  console.log(JSON.stringify(info));
  if (i < 2) await p.screenshot({path:`/tmp/fit/0${i+1}-viewer-${info?.name?.split('_')[0]}.png`});
  await p.keyboard.press('Escape'); await p.waitForTimeout(500);
}
await b.close();
