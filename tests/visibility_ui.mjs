/**
 * The visibility safety net, driven the way the incident happened:
 * Nobody on the whole library, then Family to put it back — which used to
 * publish everything the admin had hidden, with nothing left that remembered
 * it was hidden.
 *
 *   node tests/visibility_ui.mjs      (wants an instance on 3000)
 * @fixture visibility
 */
import { launch, ok, done, HOME, ADMIN } from './harness.mjs';
const SHOTS = process.env.NINAIVU_SHOTS || '.';
const b = await launch();

const p = await b.newPage({ viewport: { width: 1500, height: 1000 } });
const errs = []; p.on('pageerror', e => errs.push(e.message));
await p.goto(ADMIN, { waitUntil: 'networkidle' });
await p.fill('#gate input[type="text"]', 'dad');
await p.fill('#gate input[type="password"]', 'correcthorse1');
await p.click('#gate .btn.primary');
await p.waitForSelector('.admin-person', { state: 'attached', timeout: 15000 });

// Hide the personal folder deliberately, as an admin would.
await p.evaluate(async () => {
  await fetch('/api/visibility/folder', { method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ folder: 'personal', visibility: 'hidden' }) });
});
await p.click('#tabs button[data-tab="visibility"]');
await p.waitForSelector('[data-panel="visibility"].active');
await p.waitForTimeout(1200);

const hiddenBefore = await p.evaluate(async () =>
  (await (await fetch('/api/assets?limit=99&visibility=hidden')).json()).total);
ok('3 personal photos are hidden to start with', hiddenBefore === 3, String(hiddenBefore));

/* --- the mis-click: Nobody on the whole library --- */
const rows = await p.$$('.folder-tree .vis-picker, .folder-tree [class*="picker"]');
await p.evaluate(() => {
  const row = document.querySelector('#folder-tree').firstElementChild;
  [...row.querySelectorAll('button')].find(x => /nobody/i.test(x.textContent)).click();
});
await p.waitForTimeout(1500);
const allHidden = await p.evaluate(async () =>
  (await (await fetch('/api/assets?limit=99&visibility=hidden')).json()).total);
ok('clicking Nobody hides everything (no prompt — it exposes nothing)', allHidden === 9, String(allHidden));

/* --- the attempted fix: Family on the whole library --- */
await p.evaluate(() => {
  const row = document.querySelector('#folder-tree').firstElementChild;
  [...row.querySelectorAll('button')].find(x => /family/i.test(x.textContent)).click();
});
await p.waitForTimeout(1200);
const modalUp = await p.evaluate(() => !document.querySelector('#expose-modal').hidden);
ok('clicking Family now stops and asks', modalUp === true);
const what = await p.textContent('#expose-what');
console.log(`      "${what.trim().replace(/\s+/g,' ')}"`);
ok('the question names how many would be revealed', /9 files/.test(what), what);

// Cancel: nothing must change.
await p.click('#expose-cancel');
await p.waitForTimeout(900);
const afterCancel = await p.evaluate(async () =>
  (await (await fetch('/api/assets?limit=99&visibility=hidden')).json()).total);
ok('cancelling changes nothing', afterCancel === 9, String(afterCancel));

// Confirm: it goes through.
await p.evaluate(() => {
  const row = document.querySelector('#folder-tree').firstElementChild;
  [...row.querySelectorAll('button')].find(x => /family/i.test(x.textContent)).click();
});
await p.waitForTimeout(900);
await p.click('#expose-confirm');
await p.waitForTimeout(1500);
const afterConfirm = await p.evaluate(async () =>
  (await (await fetch('/api/assets?limit=99&visibility=hidden')).json()).total);
ok('confirming applies it', afterConfirm === 0, String(afterConfirm));

/* --- the way back --- */
ok('an Undo strip is offered', await p.evaluate(() => !document.querySelector('#vis-undo').hidden));
console.log(`      "${(await p.textContent('#vis-undo-detail')).trim()}"`);

await p.click('#vis-undo-btn');           // undo the Family
await p.waitForTimeout(1500);
await p.click('#vis-undo-btn');           // undo the Nobody
await p.waitForTimeout(1500);

const restored = await p.evaluate(async () =>
  (await (await fetch('/api/assets?limit=99&visibility=hidden')).json()).items.map(i => i.folder));
ok('undo restores exactly the 3 personal photos as hidden',
   restored.length === 3 && restored.every(f => f === 'personal'), JSON.stringify(restored));

ok('no page errors', errs.length === 0, errs.join('; '));
await p.screenshot({ path: `${SHOTS}/vis-final.png` });
await b.close();
