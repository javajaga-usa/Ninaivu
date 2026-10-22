/**
 * The modern pass: a photograph grows out of its tile and shrinks back, menus
 * are frosted glass while the top bar stays solid, the gallery's first tiles
 * wait in remembered colours, the command bar searches and runs the console's
 * everyday jobs, and none of the movement happens for anyone who asked their
 * device for less motion.
 *
 *   node tests/modern_feel_ui.mjs   (wants the family app and the console up,
 *                                    with a few dozen photographs in the library)
 * @fixture year
 */
import { launch, ok, done, signInAtHome, signInAtConsole } from './harness.mjs';

const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const b = await launch();

/** Note every picture that flies, and every layer one flies on, as they come. */
const watchFlights = () => {
  window.__flights = { grew: 0, shrank: 0 };
  new MutationObserver((changes) => {
    for (const change of changes) {
      for (const node of change.addedNodes) {
        if (node.classList?.contains('flying-picture') && node.parentElement?.id === 'viewer') window.__flights.grew++;
        if (node.classList?.contains('flying-layer')) window.__flights.shrank++;
      }
    }
  }).observe(document, { childList: true, subtree: true });
};

async function gallery(motion) {
  const context = await b.newContext({
    viewport: { width: 1280, height: 860 }, reducedMotion: motion, serviceWorkers: 'block',
  });
  const page = await context.newPage();
  await page.addInitScript(watchFlights);
  await signInAtHome(page);
  await page.waitForSelector('#grid .cell img.ready', { timeout: 30000 });
  await wait(400);
  return { context, page };
}

/* -- with motion ----------------------------------------------------------- */
{
  const { context, page } = await gallery('no-preference');

  await page.click('#grid .cell:has(img.ready)');
  await page.waitForSelector('#viewer:not([hidden])');
  await wait(700);
  let seen = await page.evaluate(() => ({
    ...window.__flights,
    left: document.querySelectorAll('.flying-picture').length,
    growing: document.querySelector('#viewer').classList.contains('growing'),
    stage: getComputedStyle(document.querySelector('#viewer-stage')).visibility,
  }));
  ok('opening a photograph from its tile grows a picture out of it', seen.grew === 1, JSON.stringify(seen));
  ok('the picture in flight is gone once the viewer shows its own', seen.left === 0, JSON.stringify(seen));
  ok('the stage is on screen after the flight', !seen.growing && seen.stage === 'visible', JSON.stringify(seen));

  await page.keyboard.press('Escape');
  await page.waitForSelector('#viewer', { state: 'hidden' });
  seen = await page.evaluate(() => window.__flights);
  ok('closing it shrinks a picture back towards its tile', seen.shrank === 1, JSON.stringify(seen));
  await wait(600);
  seen = await page.evaluate(() => ({
    layers: document.querySelectorAll('.flying-layer').length,
    hiddenTiles: [...document.querySelectorAll('#grid .cell img')].filter((img) => img.style.visibility === 'hidden').length,
  }));
  ok('and leaves nothing behind: no layer, no tile still hidden', seen.layers === 0 && seen.hiddenTiles === 0, JSON.stringify(seen));

  // Scroll bars: clear at rest, shown while the gallery moves, clear again after.
  const bars = await page.evaluate(async () => {
    const scroller = document.querySelector('.scroller');
    const color = () => getComputedStyle(scroller).scrollbarColor;
    const resting = color();
    scroller.scrollTop += 300;
    await new Promise((r) => setTimeout(r, 120));
    const moving = color();
    await new Promise((r) => setTimeout(r, 1300));
    scroller.scrollTop = 0;
    return { resting, moving, after: scroller.classList.contains('bars-on') };
  });
  ok('the gallery’s scroll bar is clear at rest and shows while it scrolls',
     /transparent transparent|rgba\(0, 0, 0, 0\) rgba\(0, 0, 0, 0\)/.test(bars.resting) && bars.moving !== bars.resting,
     JSON.stringify(bars));

  // Glass for the menus, never for the top bar.
  await page.click('#view-btn');
  await wait(300);
  const look = await page.evaluate(() => {
    const menu = getComputedStyle(document.querySelector('.segmented.view-menu-list'));
    const bar = getComputedStyle(document.querySelector('.topbar'));
    const blur = (s) => s.backdropFilter || s.webkitBackdropFilter || '';
    return { menu: blur(menu), radius: menu.borderTopLeftRadius, bar: blur(bar) };
  });
  ok('the view menu is frosted glass', look.menu.includes('blur'), JSON.stringify(look));
  ok('a floating menu has the scale’s 16 px corners', look.radius === '16px', JSON.stringify(look));
  ok('the top bar stays solid', !look.bar.includes('blur'), JSON.stringify(look));
  await page.keyboard.press('Escape');

  // The first tiles, remembered for the next visit in their own colours.
  const kept = await page.evaluate(() => Object.keys(localStorage).filter((k) => k.startsWith('mv.skeleton.'))
    .map((k) => JSON.parse(localStorage.getItem(k))));
  ok('the gallery’s first colours are kept for the next load, per person',
     kept.length === 1 && Array.isArray(kept[0]) && kept[0].every(([c]) => /^[0-9a-f]{3}$/i.test(c)),
     JSON.stringify(kept).slice(0, 200));

  // Home and End.
  await page.keyboard.press('End');
  let cursor = await page.evaluate(() => [document.querySelector('#grid .cell.cursor')?.dataset.n,
                                          document.querySelectorAll('#grid .cell').length]);
  ok('End goes to the last photograph', cursor[0] !== undefined && Number(cursor[0]) > 0, String(cursor));
  await page.keyboard.press('Home');
  cursor = await page.evaluate(() => document.querySelector('#grid .cell.cursor')?.dataset.n);
  ok('Home goes back to the first', cursor === '0', String(cursor));

  // The command bar searches for what was typed.
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+k' : 'Control+k');
  await page.waitForSelector('#palette:not([hidden])');
  await page.keyboard.type('quiet harbour');
  const first = await page.evaluate(() => document.querySelector('#palette .palette-item')?.textContent || '');
  ok('the command bar’s first line searches for the words typed', first.includes('quiet harbour'), first);
  await page.keyboard.press('Enter');
  await wait(300);
  const query = await page.evaluate(() => document.querySelector('#search').value);
  ok('Enter there searches the gallery', query === 'quiet harbour', query);
  await page.keyboard.press('h');
  await wait(300);
  const back = await page.evaluate(() => document.querySelector('#search').value);
  ok('H goes back to all photos', back === '', back);
  await context.close();
}

/* -- with less motion ------------------------------------------------------ */
{
  const { context, page } = await gallery('reduce');
  await page.click('#grid .cell:has(img.ready)');
  await page.waitForSelector('#viewer:not([hidden])');
  await wait(400);
  await page.keyboard.press('Escape');
  await wait(400);
  const seen = await page.evaluate(() => ({
    ...window.__flights,
    menuAnimation: (() => {
      document.querySelector('#view-btn').click();
      return getComputedStyle(document.querySelector('.segmented.view-menu-list')).animationName;
    })(),
  }));
  ok('with less motion asked for, nothing flies either way', seen.grew === 0 && seen.shrank === 0, JSON.stringify(seen));
  ok('and the menus open without moving', seen.menuAnimation === 'none', JSON.stringify(seen));
  await context.close();
}

/* -- the console's command bar --------------------------------------------- */
{
  const context = await b.newContext({ viewport: { width: 1280, height: 860 } });
  const page = await context.newPage();
  await signInAtConsole(page);
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+k' : 'Control+k');
  await page.waitForSelector('#palette:not([hidden])');
  const rows = await page.evaluate(() => [...document.querySelectorAll('#palette .palette-item, #palette .palette-group')]
    .map((row) => row.textContent.trim()));
  ok('the console’s command bar has its everyday jobs', rows.some((r) => /Rescan all/.test(r)), rows.join(' | ').slice(0, 300));
  ok('and nothing that erases, moves or restarts',
     !rows.some((r) => /Erase|Delete|Restart|Full re-index|Move it/.test(r)), rows.join(' | ').slice(0, 300));
  // One that only opens a dialog, so the library is left as it was.
  await page.keyboard.type('add someone');
  await page.keyboard.press('Enter');
  await wait(400);
  const on = await page.evaluate(() => document.querySelector('#tabs [data-tab].active')?.dataset.tab);
  ok('running one opens its page', on === 'people', on);
  await page.keyboard.press('Escape');
  await context.close();
}

await done(b);
