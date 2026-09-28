/**
 * Regression check: on a wide screen the viewer toolbar must look and behave
 * exactly as it did before the mobile "More"-menu fold — every tool inline,
 * #v-more hidden, .viewer-tools-more not rendered as a box (display:contents).
 */
import { launch, ok, done, HOME, signInAtHome } from './harness.mjs';

const b = await launch();
const page = await b.newPage({ viewport: { width: 1280, height: 900 } });
const errs = []; page.on('pageerror', (e) => errs.push(e.message));

await signInAtHome(page);
await page.waitForSelector('#scroller', { state: 'attached', timeout: 15000 });
await page.waitForFunction(() => document.querySelectorAll('#grid .cell').length > 0, { timeout: 15000 });
await page.waitForTimeout(300);
await page.click('#grid .cell');
await page.waitForSelector('.viewer:not([hidden])', { timeout: 5000 }).catch(() => {});
await page.waitForTimeout(400);
await page.screenshot({ path: '/tmp/desktop_viewer_toolbar.png' });

const info = await page.evaluate(() => {
  const moreBtn = document.querySelector('#v-more');
  const moreMenu = document.querySelector('#viewer-tools-more');
  const winWidth = window.innerWidth;
  const allVisible = (id) => {
    const el = document.querySelector(id);
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.right <= winWidth && r.left >= 0;
  };
  return {
    moreBtnDisplay: getComputedStyle(moreBtn).display,
    moreMenuDisplay: getComputedStyle(moreMenu).display,
    favVisible: allVisible('#v-fav'),
    similarVisible: allVisible('#v-similar'),
    infoVisible: allVisible('#v-info'),
    downloadVisible: allVisible('#v-download'),
    closeVisible: allVisible('#v-close'),
  };
});
console.log(JSON.stringify(info, null, 2));

ok('the More button stays hidden on a wide screen', info.moreBtnDisplay === 'none');
ok('the fold group renders as `contents` (no visible box) on a wide screen', info.moreMenuDisplay === 'contents');
ok('Favourite is inline and visible', info.favVisible);
ok('Find-similar is inline and visible', info.similarVisible);
ok('Details is inline and visible', info.infoVisible);
ok('Download is inline and visible', info.downloadVisible);
ok('Close is inline and visible', info.closeVisible);
ok('no console errors', errs.length === 0, errs.join('; '));

await done(b);
