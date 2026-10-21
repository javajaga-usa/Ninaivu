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

// The rule bar: hidden here, with the button that takes it off.
ok('the folder says it is hidden',
   /Hidden folder/.test(await p.textContent('#fold-rule')), await p.textContent('#fold-rule'));
ok('…and offers to stop hiding it', await p.$('#fold-unhide-folder') !== null);

// A file opens full size here, in the console, wherever the console is opened.
await p.click('.fold-item .fold-open');
await p.waitForSelector('#fold-viewer:not([hidden])');
ok('the viewer shows the file', await p.$('#fv-stage img') !== null);
ok('…named', (await p.textContent('#fv-name')).startsWith('IMG_'), await p.textContent('#fv-name'));
await p.waitForFunction(() => /\/api\/preview\//.test(document.querySelector('#fv-stage img')?.src || ''),
  null, { timeout: 8000 }).catch(() => {});
ok('…and loads the full-size copy',
   /\/api\/preview\//.test(await p.$eval('#fv-stage img', (i) => i.src)));
ok('the viewer offers to show a hidden file to the family',
   (await p.textContent('#fv-visibility')) === 'Show to family');
await p.screenshot({ path: `${SHOTS}/fold-viewer.png` });

// Show this one to the family, from the viewer.
await p.click('#fv-visibility');
await p.waitForFunction(() => document.querySelector('#fv-visibility').textContent === 'Hide',
  null, { timeout: 8000 });
const shown = await p.$$eval('.fold-item', (ts) => ts.filter((t) => !t.classList.contains('is-hidden')).length);
ok('one file is no longer hidden, at once', shown === 1, String(shown));
// …and flag it explicit.
await p.click('#fv-explicit');
await p.waitForFunction(() => document.querySelector('#fv-explicit').textContent === 'Not explicit',
  null, { timeout: 8000 });
ok('it carries an Explicit badge', await p.$('.fold-item.is-explicit .badge.xp') !== null);
await p.keyboard.press('Escape');
await p.waitForSelector('#fold-viewer', { state: 'hidden' });

// Choose them all, clear the flag and hide them again from the bar.
await p.click('#fold-act-all');
ok('choosing all ticks every file',
   (await p.$$eval('.fold-pick', (cs) => cs.filter((c) => c.checked).length)) === 3);
await p.click('#fold-act-clean');
await p.waitForFunction(() => !document.querySelector('.fold-item.is-explicit'), null, { timeout: 8000 });
ok('"Not explicit" clears the flag', true);
await p.click('#fold-act-hide');
await p.waitForFunction(() => document.querySelectorAll('.fold-item.is-hidden').length === 3,
  null, { timeout: 8000 });
ok('"Hide" hides what was chosen', true);

// Stop hiding the folder: the files the rule hid come back.
// Every file in it was just hidden one by one, so nothing would be shown and
// nothing is asked.
await p.click('#fold-unhide-folder');
await p.waitForSelector('#fold-hide-folder', { timeout: 8000 });
// Items hidden one by one stay as they were decided: only the rule comes off.
const after = await p.$$eval('.fold-item', (ts) => ts.filter((t) => t.classList.contains('is-hidden')).length);
ok('stopping hiding takes the rule off', await p.$('#fold-hide-folder') !== null);
ok('…files hidden one by one stay hidden', after === 3, String(after));

// Hide the folder again from the same bar.
await p.click('#fold-hide-folder');
await p.waitForSelector('#fold-unhide-folder', { timeout: 8000 });
ok('"Hide this folder" sets the rule', /Hidden folder/.test(await p.textContent('#fold-rule')));

// Hidden filter, from the top of the library: every hidden file, wherever it is.
await p.evaluate(() => document.querySelector('#fold-trail button').click());
await p.waitForTimeout(1000);
await p.click('#fold-filters .chip-hidden');
await p.waitForTimeout(1000);
const filtered = await p.$$eval('.fold-card .fold-name', ns => ns.map(n => n.textContent));
ok('"Hidden" narrows to folders that contain hidden media',
   JSON.stringify(filtered) === '["2024"]', JSON.stringify(filtered));
ok('…and lists the hidden files from the folders inside',
   (await p.$$('.fold-item')).length === 3, String((await p.$$('.fold-item')).length));
ok('…saying which folder each is in',
   (await p.$$eval('.fold-item .where', (ws) => ws.map((w) => w.textContent))).every((w) => w === '2024/private'));
ok('no console errors', errs.length === 0, errs.join('; '));

await b.close();
