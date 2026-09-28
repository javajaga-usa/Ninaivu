/**
 * Deleting from the gallery: admin-only, password every time, moved to the
 * recycle bin rather than erased, and restorable from the console.
 *
 *   node tests/delete_ui.mjs       (wants an instance on 3000 and 5000)
 */
import { launch, ok, done, HOME, ADMIN } from './harness.mjs';
const b = await launch();

const p = await b.newPage({ viewport: { width: 1500, height: 1000 }, deviceScaleFactor: 2 });
const errs=[]; p.on('pageerror', e=>errs.push(e.message));
await p.goto(HOME, { waitUntil: 'networkidle' });
await p.evaluate(async () => {
  await fetch('/api/auth/login', { method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ username:'dad', password:'correcthorse1' }) });
});
await p.reload({ waitUntil: 'networkidle' });
await p.waitForTimeout(2500);

const before = await p.evaluate(async () =>
  (await (await fetch('/api/assets?limit=99')).json()).total);
console.log(`      library holds ${before} items`);

// Select two items the way a person does.
// Enter selection the way a person does: the section's "Select all".
await p.evaluate(() => {
  const button = document.querySelector('[data-select-section]');
  if (button) button.click();
});
await p.waitForTimeout(400);
const selInfo = await p.evaluate(() => ({
  n: Number((document.querySelector('#sel-count')?.textContent || '0').split(' ')[0]),
  count: document.querySelector('#sel-count')?.textContent || '',
  barHidden: document.querySelector('#selection-bar').hidden,
  delHidden: document.querySelector('#sel-delete').hidden,
}));
console.log(`      selection bar: ${selInfo.count}`);
ok('the Delete button is offered to an admin', selInfo.delHidden === false, JSON.stringify(selInfo));

// Drive it via the handler, since selection UI wiring varies.
await p.evaluate(() => document.querySelector('#sel-delete').click());
await p.waitForTimeout(800);
ok('clicking Delete asks for the password',
   await p.evaluate(() => !document.querySelector('#delete-modal').hidden));
console.log(`      "${(await p.textContent('#delete-what')).trim()}"`);

await p.fill('#delete-password', 'wrongone');
await p.click('#delete-confirm');
await p.waitForTimeout(1200);
ok('a wrong password is refused and the box stays open',
   await p.evaluate(() => !document.querySelector('#delete-modal').hidden
     && !document.querySelector('#delete-error').hidden));
console.log(`      "${(await p.textContent('#delete-error')).trim()}"`);
const stillThere = await p.evaluate(async () =>
  (await (await fetch('/api/assets?limit=99')).json()).total);
ok('nothing was deleted on a wrong password', stillThere === before, String(stillThere));

await p.fill('#delete-password', 'correcthorse1');
await p.click('#delete-confirm');
await p.waitForTimeout(2500);
const after = await p.evaluate(async () =>
  (await (await fetch('/api/assets?limit=99')).json()).total);
ok('the right password deletes them', after === before - selInfo.n,
   `${before} → ${after}, expected -${selInfo.n}`);
ok('the dialog closed', await p.evaluate(() => document.querySelector('#delete-modal').hidden));
ok('no page errors', errs.length === 0, errs.join('; '));

/* the console can see and restore them */
const c = await b.newPage();
await c.goto(ADMIN, { waitUntil: 'networkidle' });
await c.fill('#gate input[type="text"]', 'dad');
await c.fill('#gate input[type="password"]', 'correcthorse1');
await c.click('#gate .btn.primary');
await c.waitForSelector('.admin-person', { state:'attached', timeout:15000 });
const bin = await c.evaluate(async () => (await (await fetch('/api/recycle')).json()));
ok('the console lists them in the recycle bin', bin.summary.items === selInfo.n,
   JSON.stringify(bin.summary));
ok('and says where they are on disk', bin.folders[0].endsWith('_deleted'), bin.folders[0]);

const restored = await c.evaluate(async (ids) =>
  (await (await fetch('/api/recycle/restore', { method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({ ids }) })).json()),
  bin.items.map(i => i.id));
ok('restoring puts them back', restored.restored === selInfo.n, JSON.stringify(restored));

await b.close();
