/**
 * The whole-library password gate, in every direction, plus the exposure
 * warning stacking on top of it.
 *
 *   node tests/visibility_password_ui.mjs      (wants an instance on 3000)
 * @fixture visibility
 */
import { launch, ok, done, HOME, ADMIN } from './harness.mjs';
const SHOTS = process.env.NINAIVU_SHOTS || '.';
const b = await launch();

const p = await b.newPage({ viewport: { width: 1500, height: 1000 } });
const errs=[]; p.on('pageerror', e=>errs.push(e.message));
await p.goto(ADMIN, { waitUntil: 'networkidle' });
await p.fill('#gate input[type="text"]', 'dad');
await p.fill('#gate input[type="password"]', 'correcthorse1');
await p.click('#gate .btn.primary');
await p.waitForSelector('.admin-person', { state:'attached', timeout:15000 });
await p.click('#tabs button[data-tab="visibility"]');
await p.waitForTimeout(1200);



/* family → hidden: cannot expose anything, must still ask for the password */
await p.evaluate(() => {
  const row = document.querySelector('#folder-tree').firstElementChild;
  [...row.querySelectorAll('button')].find(x => /nobody/i.test(x.textContent)).click();
});
await p.waitForTimeout(1000);
ok('family → hidden on the whole library asks for the password',
   await p.evaluate(() => !document.querySelector('#vispw-modal').hidden));
console.log(`      "${(await p.textContent('#vispw-what')).trim().replace(/\s+/g,' ')}"`);

/* wrong password */
await p.fill('#vispw-input', 'nope');
await p.click('#vispw-confirm');
await p.waitForTimeout(1200);
ok('a wrong password keeps the box open and says why',
   await p.evaluate(() => !document.querySelector('#vispw-modal').hidden
     && !document.querySelector('#vispw-error').hidden));
console.log(`      "${(await p.textContent('#vispw-error')).trim()}"`);
const stillFamily = await p.evaluate(async () =>
  (await (await fetch('/api/assets?limit=99&visibility=hidden')).json()).total);
ok('nothing changed on a wrong password', stillFamily === 0, String(stillFamily));

/* cancel */
await p.click('#vispw-cancel');
await p.waitForTimeout(600);
ok('cancelling closes it', await p.evaluate(() => document.querySelector('#vispw-modal').hidden));

/* right password */
await p.evaluate(() => {
  const row = document.querySelector('#folder-tree').firstElementChild;
  [...row.querySelectorAll('button')].find(x => /nobody/i.test(x.textContent)).click();
});
await p.waitForTimeout(900);
await p.fill('#vispw-input', 'correcthorse1');
await p.click('#vispw-confirm');
await p.waitForTimeout(1500);
const nowHidden = await p.evaluate(async () =>
  (await (await fetch('/api/assets?limit=99&visibility=hidden')).json()).total);
ok('the right password applies it', nowHidden === 9, String(nowHidden));

/* hidden → family: password AND the exposure warning, in that order */
await p.evaluate(() => {
  const row = document.querySelector('#folder-tree').firstElementChild;
  [...row.querySelectorAll('button')].find(x => /family/i.test(x.textContent)).click();
});
await p.waitForTimeout(900);
ok('hidden → family asks for the password first',
   await p.evaluate(() => !document.querySelector('#vispw-modal').hidden));
ok('…and mentions what it would reveal',
   await p.evaluate(() => !document.querySelector('#vispw-expose').hidden));
console.log(`      "${(await p.textContent('#vispw-expose')).trim()}"`);
await p.fill('#vispw-input', 'correcthorse1');
await p.click('#vispw-confirm');
await p.waitForTimeout(1200);
ok('then the exposure confirmation',
   await p.evaluate(() => !document.querySelector('#expose-modal').hidden));
await p.click('#expose-confirm');
await p.waitForTimeout(1500);
ok('and it applies', await p.evaluate(async () =>
  (await (await fetch('/api/assets?limit=99&visibility=hidden')).json()).total) === 0);

/* a named folder is still one click */
await p.evaluate(() => {
  const rows = [...document.querySelectorAll('#folder-tree > *')];
  const personal = rows.find(r => /personal/.test(r.textContent));
  [...personal.querySelectorAll('button')].find(x => /nobody/i.test(x.textContent)).click();
});
await p.waitForTimeout(1200);
ok('a named folder needs no password',
   await p.evaluate(() => document.querySelector('#vispw-modal').hidden) &&
   await p.evaluate(async () =>
     (await (await fetch('/api/assets?limit=99&visibility=hidden')).json()).total) === 3);

ok('no page errors', errs.length === 0, errs.join('; '));
await p.screenshot({ path: `${SHOTS}/pw-modal.png` });
await b.close();
