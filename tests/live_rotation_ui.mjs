/** The saved turn from Straighten must also turn a Live Photo's moving preview. */
import { launch, ok, done, HOME, signInAtHome } from './harness.mjs';

const browser = await launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 900 }, serviceWorkers: 'block' });
const errors = [];
page.on('pageerror', error => errors.push(error.message));
// Hold the short fixture clip on screen long enough to inspect its geometry.
await page.addInitScript(() => {
  const play = HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play = function(...args) {
    if (this.classList.contains('live-peek')) this.loop = true;
    return play.apply(this, args);
  };
});
await signInAtHome(page);
const item = await page.evaluate(async () =>
  (await (await fetch('/api/assets?is_live=1&limit=1')).json()).items[0]);
if (!item) throw new Error('The Live Photo fixture is missing');

try {
  for (const angle of [90, 180, 270, 0]) {
    // The same persisted rotation Straighten applies (and Undo restores).
    const response = await page.request.post(`${HOME}/api/asset/${item.id}/rotate`, {
      data: { rotation: angle },
    });
    ok(`saved ${angle} degree correction`, response.ok());
    await page.reload({ waitUntil: 'networkidle' });
    await page.click('#nav-live');
    const cell = page.locator(`#grid .cell[data-id="${item.id}"]`);
    await cell.waitFor();
    await cell.hover();
    const video = cell.locator('video.live-peek');
    await video.waitFor();
    const geometry = await video.evaluate(element => {
      const matrix = new DOMMatrix(getComputedStyle(element).transform);
      const angle = (Math.round(Math.atan2(matrix.b, matrix.a) * 180 / Math.PI) + 360) % 360;
      const box = element.getBoundingClientRect();
      const cell = element.parentElement.getBoundingClientRect();
      return { angle, width: box.width, height: box.height, cellWidth: cell.width, cellHeight: cell.height };
    });
    ok(`Live MOV follows the ${angle} degree correction`, geometry.angle === angle, JSON.stringify(geometry));
    ok(`turned MOV fills its tile at ${angle} degrees`,
      Math.abs(geometry.width - geometry.cellWidth) < 2
      && Math.abs(geometry.height - geometry.cellHeight) < 2, JSON.stringify(geometry));
    await page.mouse.move(0, 0);
  }
} finally {
  await page.request.post(`${HOME}/api/asset/${item.id}/rotate`, { data: { rotation: item.rotation || 0 } });
}
ok('no script errors', errors.length === 0, errors.join('; '));
await done(browser);
