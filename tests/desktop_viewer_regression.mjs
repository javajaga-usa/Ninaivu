/**
 * Regression check: on a wide screen the viewer toolbar keeps the everyday
 * tools on the bar — zoom, favourite, share, details, download, close — and
 * folds the rest behind the "More" button, whose menu starts closed and lists
 * them with their names.
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
    zoomVisible: allVisible('#v-zoom-in') && allVisible('#v-zoom-out'),
    shareVisible: allVisible('#v-share'),
    similarFolded: moreMenu.contains(document.querySelector('#v-similar')),
    infoVisible: allVisible('#v-info'),
    downloadVisible: allVisible('#v-download'),
    closeVisible: allVisible('#v-close'),
  };
});
console.log(JSON.stringify(info, null, 2));

ok('the More button is shown on a wide screen too', info.moreBtnDisplay !== 'none');
ok('the More menu starts closed', info.moreMenuDisplay === 'none');
ok('Favourite is inline and visible', info.favVisible);
ok('Zoom is inline and visible', info.zoomVisible);
ok('Share is inline and visible', info.shareVisible);
ok('Find-similar is folded into the More menu', info.similarFolded);
ok('Details is inline and visible', info.infoVisible);
ok('Download is inline and visible', info.downloadVisible);
ok('Close is inline and visible', info.closeVisible);

// Opened, the menu shows Find-similar with its name, inside the window.
await page.click('#v-more');
await page.waitForTimeout(150);
const opened = await page.evaluate(() => {
  const similar = document.querySelector('#v-similar');
  const r = similar.getBoundingClientRect();
  const label = similar.querySelector('.viewer-tools-more-label');
  return {
    visible: r.width > 0 && r.right <= window.innerWidth,
    labelled: getComputedStyle(label).display !== 'none' && label.textContent.trim().length > 0,
  };
});
ok('Find-similar is reachable, with its name, once More is opened', opened.visible && opened.labelled);
ok('no console errors', errs.length === 0, errs.join('; '));

await done(b);
