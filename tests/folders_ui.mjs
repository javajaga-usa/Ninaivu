/**
 * The console folder screen: drilling in, and the red treatment for anything
 * only an admin can see.
 *
 *   node tests/folders_ui.mjs      (wants an instance on 3000)
 * @fixture folders
 */
import { launch, ok, done, HOME, ADMIN } from './harness.mjs';
const SHOTS = process.env.NINAIVU_SHOTS || '.';
const b = await launch();

/* ---------- the console's folder screen ---------- */
const p = await b.newPage({ viewport: { width: 1500, height: 1000 }, deviceScaleFactor: 2 });
const errs=[]; p.on('pageerror', e=>errs.push(e.message));
await p.goto(ADMIN, { waitUntil: 'networkidle' });
await p.fill('#gate input[type="text"]', 'dad');
await p.fill('#gate input[type="password"]', 'correcthorse1');
await p.click('#gate .btn.primary');
await p.waitForSelector('.admin-person', { state:'attached', timeout:15000 });

// Hide one folder so there is something red to see.
await p.evaluate(async () => {
  await fetch('/api/visibility/folder', { method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ folder: '2024/private', visibility: 'hidden' }) });
});

ok('the console has a Folders tab', await p.$('#tabs button[data-tab="folders"]') !== null);
await p.click('#tabs button[data-tab="folders"]');
await p.waitForSelector('[data-panel="folders"].active');
await p.waitForTimeout(1500);

const cards = await p.$$eval('.fold-card .fold-name', ns => ns.map(n => n.textContent));
ok('top level shows the folders', JSON.stringify(cards) === '["2023","2024"]', JSON.stringify(cards));
ok('loose files in the root are listed too',
   (await p.$$('.fold-item')).length === 2, String((await p.$$('.fold-item')).length));
console.log(`      summary: "${(await p.textContent('#fold-summary')).trim().replace(/\s+/g,' ')}"`);
ok('the summary names how much is admin-only',
   /3 only admins can see/.test(await p.textContent('#fold-summary')));

// Drill in
await p.evaluate(() => [...document.querySelectorAll('.fold-card')]
  .find(c => c.textContent.includes('2024')).click());
await p.waitForTimeout(1200);
ok('the breadcrumb tracks the path',
   (await p.textContent('#fold-trail')).replace(/\s+/g,'').includes('/2024'),
   await p.textContent('#fold-trail'));

const priv = await p.evaluate(() => {
  const c = [...document.querySelectorAll('.fold-card')].find(x => x.textContent.includes('private'));
  return { allHidden: c.classList.contains('all-hidden'), text: c.textContent.replace(/\s+/g,' ').trim() };
});
ok('an entirely private folder is outlined in red', priv.allHidden, JSON.stringify(priv));
ok('…and says how many are hidden', /3 hidden/.test(priv.text), priv.text);

await p.evaluate(() => [...document.querySelectorAll('.fold-card')]
  .find(c => c.textContent.includes('private')).click());
await p.waitForTimeout(1200);
const tiles = await p.$$eval('.fold-item', ts => ts.map(t => ({
  hidden: t.classList.contains('is-hidden'),
  badge: t.querySelector('.badge')?.textContent || '',
  why: t.querySelector('.why')?.textContent || '',
})));
ok('every file inside is marked hidden', tiles.length === 3 && tiles.every(t => t.hidden),
   JSON.stringify(tiles));
ok('each carries a red Hidden badge', tiles.every(t => t.badge === 'Hidden'));
ok('…and says where the setting came from', tiles.every(t => t.why === 'folder rule'),
   JSON.stringify(tiles.map(t=>t.why)));
await p.screenshot({ path: `${SHOTS}/fold-detail.png` });

// hidden-only filter
await p.evaluate(() => loadFolderScreen('')).catch(() => {});
await p.evaluate(() => {
  const trail = document.querySelector('#fold-trail button');
  trail.click();
});
await p.waitForTimeout(1000);
await p.check('#fold-hidden-only');
await p.waitForTimeout(600);
const filtered = await p.$$eval('.fold-card .fold-name', ns => ns.map(n => n.textContent));
ok('"show hidden only" narrows to folders that contain hidden media',
   JSON.stringify(filtered) === '["2024"]', JSON.stringify(filtered));
ok('no console errors', errs.length === 0, errs.join('; '));

await b.close();
