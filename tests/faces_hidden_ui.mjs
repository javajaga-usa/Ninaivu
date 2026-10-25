/**
 * The console's Faces tab: "Not bothered" and "Show more groups".
 *
 * A group nobody wants to name steps aside with one press and the next one
 * moves up; the hidden ones are listed below and can come back, one group or
 * all of them; and the groups waiting for a name come a page at a time, so
 * the person you are looking for is not lost below the first sixty. The
 * server's answers are stood in for; what it does with them is
 * tests/test_faces_hidden.py.
 *
 *   node tests/faces_hidden_ui.mjs     (wants an instance on 3000,
 *                                       admin dad / correcthorse1)
 */
import { launch, ok, done, signInAtConsole } from './harness.mjs';

const group = (n, size) => ({ cluster_key: `g${n}`, size, photo_count: size, cover: null });
let waiting = Array.from({ length: 63 }, (_, n) => group(n, 90 - n));
let hidden = [];
const asked = { hide: [], show: [] };

const b = await launch();
const shots = process.env.NINAIVU_SHOTS;
const p = await b.newPage({ viewport: { width: 1400, height: 900 } });
const errs = [];
p.on('pageerror', (e) => errs.push(e.message));

const reply = (route, body, status = 200) =>
  route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
const hiddenFaces = () => hidden.reduce((sum, c) => sum + c.size, 0);

await p.route('**/api/faces/status', (route) => reply(route, {
  engine: { available: true }, people: 0, faces: 900, confirmed: 0,
  photos_with_faces: 500, photos_pending: 0,
}));
await p.route('**/api/faces/people', (route) => reply(route, { people: [] }));
await p.route('**/api/faces/clusters?*', (route) => {
  const url = new URL(route.request().url());
  if (url.searchParams.get('hidden') === '1') {
    return reply(route, { clusters: hidden, more: false, hidden_faces: hiddenFaces() });
  }
  const offset = Number(url.searchParams.get('offset') || 0);
  const page = waiting.slice(offset, offset + 60);
  return reply(route, { clusters: page, more: waiting.length > offset + 60, hidden_faces: hiddenFaces() });
});
await p.route('**/api/faces/clusters', (route) => reply(route, {
  clusters: waiting.slice(0, 60), more: waiting.length > 60, hidden_faces: hiddenFaces(),
}));
await p.route('**/api/faces/clusters/hide', (route) => {
  const { cluster_key: key } = route.request().postDataJSON();
  asked.hide.push(key);
  const found = waiting.find((c) => c.cluster_key === key);
  waiting = waiting.filter((c) => c.cluster_key !== key);
  hidden.push(found);
  return reply(route, { cluster_key: key, hidden: found.size });
});
await p.route('**/api/faces/clusters/show', (route) => {
  const body = route.request().postDataJSON();
  asked.show.push(body);
  const back = body.all ? hidden : hidden.filter((c) => c.cluster_key === body.cluster_key);
  hidden = hidden.filter((c) => !back.includes(c));
  waiting = [...waiting, ...back].sort((x, y) => y.size - x.size);
  return reply(route, { shown: back.reduce((sum, c) => sum + c.size, 0) });
});

await signInAtConsole(p);
await p.evaluate(() => document.querySelector('#tabs button[data-tab="faces"]').click());
await p.waitForSelector('#faces-clusters .person-card', { timeout: 15000 });

const cards = () => p.$$eval('#faces-clusters .person-card', (els) => els.length);
ok('the first sixty groups are shown', (await cards()) === 60);
ok('"Show more groups" is offered', await p.isVisible('#faces-more'));
ok('nothing hidden yet, so no hidden list', await p.isHidden('#faces-hidden-block'));

await p.click('#faces-more');
await p.waitForFunction(() => document.querySelectorAll('#faces-clusters .person-card').length === 63);
ok('the next page is added below', (await cards()) === 63);
ok('and the button goes once there is no more', await p.isHidden('#faces-more'));

const first = p.locator('#faces-clusters .person-card').first();
await first.getByRole('button', { name: 'Not bothered' }).click();
await p.waitForFunction(() => document.querySelectorAll('#faces-clusters .person-card').length === 62);
ok('"Not bothered" asks to hide that group', asked.hide[0] === 'g0');
ok('its card goes and the rest stay', (await cards()) === 62);
ok('the hidden list appears with a count',
  /90/.test(await p.textContent('#faces-hidden-toggle')) && await p.isVisible('#faces-hidden-block'));
if (shots) await p.screenshot({ path: `${shots}/faces-not-bothered.png`, fullPage: false });

await p.click('#faces-hidden-toggle');
await p.waitForSelector('#faces-hidden .person-card');
ok('the hidden group is listed', (await p.$$('#faces-hidden .person-card')).length === 1);
if (shots) {
  await p.locator('#faces-hidden-block').scrollIntoViewIfNeeded();
  await p.screenshot({ path: `${shots}/faces-hidden-list.png` });
}
await p.locator('#faces-hidden .person-card').getByRole('button', { name: 'Show again' }).click();
await p.waitForFunction(() => !document.querySelector('#faces-hidden .person-card'));
ok('"Show again" brings that group back', asked.show[0]?.cluster_key === 'g0'
  && (await p.textContent('#faces-clusters')).includes('90 faces'));

ok('no page errors', errs.length === 0, errs.join('; '));
await done(b);
