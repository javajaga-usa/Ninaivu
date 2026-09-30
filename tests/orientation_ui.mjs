/**
 * Photographs the right way up, and fitting the screen, in the viewer.
 *
 * Three separate things have to line up for this to look right, and each of
 * them was broken in its own way:
 *
 *  - The **grid** shows thumbnails, which the scanner bakes upright. That part
 *    worked already for EXIF and now works for photographs with no tag at all.
 *  - The **viewer** shows the *original* file, untouched, so it has to be told
 *    the turn — otherwise a photograph looks right in the grid and lies on its
 *    side the moment you open it.
 *  - A CSS rotation does not re-run `object-fit`, so a landscape photograph
 *    turned on its side was laid out landscape and then spun, running straight
 *    off the top and bottom of the screen.
 *
 * And the rotate button has to *stick*, or the automatic guess has no
 * correction path and is worse than no guess at all.
 *
 *   node tests/orientation_ui.mjs
 *      (wants an instance on 5000 whose library is the orientation fixtures,
 *       admin dad / correcthorse1)
 *
 * @manual - run by hand. The library is photographs of people the right way up
 * and lying on their sides (framed_lying_left, framed_upside_down, ...), which
 * cannot be generated, and the orientation and face models must be installed
 * to judge them. tests/run_browser_tests.py skips it, saying so.
 */
import { launch, ok, done, HOME, ADMIN } from './harness.mjs';

const b = await launch();

const signIn = async (page, who = 'dad', pass = 'correcthorse1') => {
  await page.goto(HOME, { waitUntil: 'networkidle' });
  await page.evaluate(async ([u, p]) => {
    await fetch('/api/auth/logout', { method: 'POST' });
    await fetch('/api/auth/login', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: u, password: p }),
    });
  }, [who, pass]);
  await page.reload({ waitUntil: 'networkidle' });
  await page.waitForFunction(() => document.querySelectorAll('.cell').length > 4,
    null, { timeout: 30000 });
  await page.waitForTimeout(1200);
};

const openNamed = async (page, needle) => {
  const id = await page.evaluate(async (n) => {
    const r = await (await fetch('/api/assets?limit=200')).json();
    return r.items.find((i) => (i.name || '').includes(n))?.id;
  }, needle);
  if (!id) throw new Error(`no asset matching ${needle}`);
  await page.evaluate((i) => [...document.querySelectorAll('.cell')]
    .find((c) => Number(c.dataset.id) === i)?.click(), id);
  await page.waitForTimeout(1600);
  return id;
};

const stage = () => ({
  box: document.querySelector('#viewer-stage').getBoundingClientRect(),
  img: document.querySelector('#viewer-stage img:not(.placeholder)'),
});

const measure = (page) => page.evaluate(() => {
  const el = document.querySelector('#viewer-stage img:not(.placeholder)');
  const box = document.querySelector('#viewer-stage').getBoundingClientRect();
  if (!el) return null;
  const r = el.getBoundingClientRect();
  return {
    w: Math.round(r.width), h: Math.round(r.height),
    boxW: Math.round(box.width), boxH: Math.round(box.height),
    transform: getComputedStyle(el).transform,
  };
});

const page = await b.newPage({ viewport: { width: 1440, height: 900 } });
const errs = [];
page.on('pageerror', (e) => errs.push(e.message));
await signIn(page);

/* ---------- what the index worked out ---------- */

const facts = await page.evaluate(async () => {
  const r = await (await fetch('/api/assets?limit=200')).json();
  return r.items.map((i) => ({
    name: i.name, w: i.width, h: i.height,
    rot: i.rotation, src: i.rotation_source,
  }));
});
const named = (n) => facts.find((f) => f.name.includes(n)) || {};

ok('a sideways photograph with no metadata was turned back',
  named('framed_lying_left').rot === 90, JSON.stringify(named('framed_lying_left')));
ok('and the other way round too',
  named('framed_lying_right').rot === 270, JSON.stringify(named('framed_lying_right')));
ok('an upside-down one as well',
  named('framed_upside_down').rot === 180, JSON.stringify(named('framed_upside_down')));
ok('one that was already upright was left alone',
  named('framed_person').rot === 0, JSON.stringify(named('framed_person')));
ok('each turn says it came from the faces',
  ['framed_lying_left', 'framed_lying_right', 'framed_upside_down']
    .every((n) => named(n).src === 'faces'),
  JSON.stringify(['framed_lying_left', 'framed_lying_right'].map(named)));

ok('a photograph with a real EXIF tag was never guessed at',
  facts.filter((f) => f.name.startsWith('orientation_'))
    .every((f) => f.rot === 0 && f.src === 'none'),
  JSON.stringify(facts.filter((f) => f.name.startsWith('orientation_')).slice(0, 2)));
ok('a picture with no people in it is not turned on a hunch',
  named('sideways_no_tag').rot === 0, JSON.stringify(named('sideways_no_tag')));
ok('videos are left to the browser',
  facts.filter((f) => f.name.endsWith('.mp4')).every((f) => f.rot === 0));

/* ---------- the viewer shows it the same way up ---------- */

await openNamed(page, 'framed_lying_left');
const turned = await measure(page);
ok('the viewer applies the turn to the original',
  /matrix/.test(turned.transform) && turned.transform !== 'none', turned.transform);
ok('a turned photograph still fits inside the screen',
  turned.w <= turned.boxW + 2 && turned.h <= turned.boxH + 2, JSON.stringify(turned));

await page.keyboard.press('Escape');
await page.waitForTimeout(600);

/* ---------- and it fits whichever way the window is ---------- */

for (const [w, h, label] of [[1440, 900, 'a wide window'], [820, 1180, 'a tall window']]) {
  await page.setViewportSize({ width: w, height: h });
  await page.waitForTimeout(600);
  await openNamed(page, 'framed_lying_left');
  const m = await measure(page);
  ok(`it fits in ${label}`, m.w <= m.boxW + 2 && m.h <= m.boxH + 2, JSON.stringify(m));
  await page.keyboard.press('Escape');
  await page.waitForTimeout(500);
}
await page.setViewportSize({ width: 1440, height: 900 });
await page.waitForTimeout(500);

/* ---------- rotating, and having it stay ---------- */

const id = await openNamed(page, 'framed_person');
// Rotate lives in the viewer's "More" menu.
await page.click('#v-more');
await page.click('#v-rotate');
await page.waitForTimeout(2500);

const afterClick = await page.evaluate(async (i) =>
  (await (await fetch(`/api/asset/${i}`)).json()), id);
ok('an admin rotating saves it', afterClick.rotation === 90,
  JSON.stringify({ rot: afterClick.rotation, src: afterClick.rotation_source }));
ok('and it is recorded as a decision a person made',
  afterClick.rotation_source === 'manual', afterClick.rotation_source);
ok('the recorded shape follows the turn',
  afterClick.width > afterClick.height,
  `${afterClick.width}x${afterClick.height}`);

// The real question: does it survive coming back tomorrow?
await page.reload({ waitUntil: 'networkidle' });
await page.waitForFunction(() => document.querySelectorAll('.cell').length > 4,
  null, { timeout: 30000 });
await page.waitForTimeout(1500);
const persisted = await page.evaluate(async (i) =>
  (await (await fetch(`/api/asset/${i}`)).json()).rotation, id);
ok('it is still turned after a reload', persisted === 90, String(persisted));

// Put it back, so the fixture library is left as it was found.
await page.evaluate(async (i) => {
  await fetch(`/api/asset/${i}/rotate`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ rotation: 0 }),
  });
}, id);

/* ---------- a family member gets a look, not a change ---------- */

await signIn(page, 'maya', 'summerdays24').catch(() => {});
const asFamily = await page.evaluate(async (i) => {
  const r = await fetch(`/api/asset/${i}/rotate`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ rotation: 90 }),
  });
  return r.status;
}, id).catch(() => null);
if (asFamily !== null) {
  ok('a family member cannot save a rotation', [401, 403, 404].includes(asFamily),
    String(asFamily));
}

ok('no page errors', errs.length === 0, errs.join(' | '));
await done(b);
