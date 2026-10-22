/**
 * The password dialogs: one look and one behaviour everywhere.
 *
 * Deleting and turning files in the family app, and erasing, deleting and
 * replacing in the console, each ask for the password in a small dialog. They
 * grew one at a time, and the box itself was the weak part: a bare field with
 * no room, no way to see what had been typed, and in the family app not even
 * a label above it. This opens each one and checks that the box is a proper
 * field: it has the focus, Show and Hide work, Enter tries the password and a
 * wrong one is said plainly inside the dialog. The People screen's temporary
 * password and PIN, which were the browser's own prompt() boxes, now use the
 * same dialog, and the lock screen's box gets the same Show button.
 *
 * Screenshots of each, in the light and dark looks, go to $NINAIVU_SHOTS.
 *
 *   node tests/password_prompt_ui.mjs   (wants an instance on 3000 and 5000)
 * @fixture several
 */
import fs from 'node:fs';
import path from 'node:path';
import { launch, ok, done, signInAtHome, signInAtConsole } from './harness.mjs';

const SHOTS = path.join(process.env.NINAIVU_SHOTS || 'shots', 'password-prompts');
fs.mkdirSync(SHOTS, { recursive: true });

const b = await launch();
const errs = [];

async function theme(page, look) {
  await page.evaluate((look) => { document.documentElement.dataset.theme = look; }, look);
  await page.waitForTimeout(250);
}

/** The open dialog's card, with a little of the page around it. */
async function shoot(page, modal, name) {
  await page.waitForTimeout(350);      // the card's rise animation
  const box = await page.locator(`${modal} .modal-card`).boundingBox();
  if (!box) return;
  const pad = 24;
  await page.screenshot({
    path: path.join(SHOTS, `${name}.png`),
    clip: { x: Math.max(0, box.x - pad), y: Math.max(0, box.y - pad),
      width: box.width + pad * 2, height: box.height + pad * 2 },
  });
}

/** What every password box should be, checked the same way each time. */
async function checkBox(page, label, modal, input) {
  ok(`${label}: the password box has the focus`,
    await page.evaluate((id) => document.activeElement?.id === id, input.slice(1)));
  const reveal = page.locator(`${modal} .gate-reveal`);
  ok(`${label}: there is a Show button in the box`, await reveal.count() === 1);
  if (await reveal.count() !== 1) return;
  await page.fill(input, 'abc');
  await reveal.click();
  ok(`${label}: Show shows what was typed`,
    await page.$eval(input, (n) => n.type === 'text'));
  ok(`${label}: and says Hide`, (await reveal.textContent()).trim() === 'Hide');
  await reveal.click();
  ok(`${label}: Hide hides it again`, await page.$eval(input, (n) => n.type === 'password'));
}

async function wrongPassword(page, label, modal, input, error) {
  await page.fill(input, 'not-the-password');
  await page.press(input, 'Enter');
  await page.waitForSelector(`${error}:not([hidden])`, { timeout: 8000 }).catch(() => {});
  ok(`${label}: Enter tries the password and a wrong one is said in the dialog`,
    await page.evaluate(([m, e]) => !document.querySelector(m).hidden
      && !document.querySelector(e).hidden, [modal, error]));
  ok(`${label}: the message is read out`,
    await page.$eval(error, (n) => n.getAttribute('role') === 'alert'));
  ok(`${label}: the box is ready for another try`,
    await page.evaluate((id) => document.activeElement?.id === id
      && !document.getElementById(id).value, input.slice(1)));
}

/* ---------- the family app: Delete and Turn ---------- */

const fam = await b.newPage({ viewport: { width: 1200, height: 820 }, deviceScaleFactor: 2 });
fam.on('pageerror', (e) => errs.push(e.message));
await signInAtHome(fam);
await fam.waitForFunction(() => document.querySelectorAll('.cell').length >= 2,
  null, { timeout: 25000 });
await fam.evaluate(() => document.querySelector('.cell')?.click());
await fam.waitForTimeout(300);
await fam.evaluate(() => {
  if (!document.querySelector('#selection-bar') || document.querySelector('#selection-bar').hidden) {
    document.querySelector('[data-select-section]')?.click();
  }
});
await fam.keyboard.press('Escape');                  // the viewer, if the click opened one
await fam.waitForTimeout(300);
await fam.evaluate(() => {
  if (document.querySelector('#selection-bar').hidden) {
    document.querySelector('[data-select-section]')?.click();
  }
});
await fam.waitForTimeout(400);

for (const look of ['light', 'dark']) {
  await theme(fam, look);
  await fam.evaluate(() => document.querySelector('#sel-delete').click());
  await fam.waitForSelector('#delete-modal:not([hidden])');
  if (look === 'light') await checkBox(fam, 'family Delete', '#delete-modal', '#delete-password');
  await fam.fill('#delete-password', 'secret-pass');
  await shoot(fam, '#delete-modal', `family-delete-${look}`);
  await wrongPassword(fam, `family Delete (${look})`, '#delete-modal', '#delete-password', '#delete-error');
  await shoot(fam, '#delete-modal', `family-delete-error-${look}`);
  await fam.click('#delete-cancel');

  await fam.evaluate(() => document.querySelector('#sel-rot-right').click());
  await fam.waitForSelector('#rotate-modal:not([hidden])');
  if (look === 'light') await checkBox(fam, 'family Turn', '#rotate-modal', '#rotate-password');
  await fam.fill('#rotate-password', '');
  await shoot(fam, '#rotate-modal', `family-rotate-${look}`);
  if (look === 'light') {
    await wrongPassword(fam, 'family Turn', '#rotate-modal', '#rotate-password', '#rotate-error');
  }
  await fam.click('#rotate-cancel');
}
await theme(fam, 'light');

// One photograph really deleted, with Enter, so the console's bin has
// something to erase.
await fam.evaluate(() => document.querySelector('#sel-delete').click());
await fam.waitForSelector('#delete-modal:not([hidden])');
await fam.fill('#delete-password', 'correcthorse1');
await fam.press('#delete-password', 'Enter');
await fam.waitForSelector('#delete-modal[hidden]', { state: 'attached', timeout: 10000 }).catch(() => {});
ok('the right password and Enter delete, and the dialog closes',
  await fam.evaluate(() => document.querySelector('#delete-modal').hidden));

/* ---------- the console: Erase for good, Delete, Replace ---------- */

const con = await b.newPage({ viewport: { width: 1200, height: 820 }, deviceScaleFactor: 2 });
con.on('pageerror', (e) => errs.push(e.message));
await signInAtConsole(con);
await con.click('#tabs button[data-tab="folders"]');
await con.waitForSelector('[data-panel="folders"].active');
await con.waitForSelector('.bin-row', { timeout: 15000 }).catch(() => {});
await con.click('#bin-all').catch(() => {});

for (const look of ['light', 'dark']) {
  await theme(con, look);
  await con.click('#bin-delete');
  await con.waitForSelector('#purge-modal:not([hidden])');
  if (look === 'light') await checkBox(con, 'console Erase', '#purge-modal', '#purge-password');
  await con.fill('#purge-password', 'secret-pass');
  await shoot(con, '#purge-modal', `console-erase-${look}`);
  await wrongPassword(con, `console Erase (${look})`, '#purge-modal', '#purge-password', '#purge-error');
  await shoot(con, '#purge-modal', `console-erase-error-${look}`);
  await con.click('#purge-cancel');

  // Large files needs a large file to offer these; the look is what is
  // checked here, so the dialogs are simply shown.
  for (const [modal, name] of [['#lf-delete-modal', 'delete'], ['#lf-replace-modal', 'replace']]) {
    await con.evaluate((m) => {
      document.querySelector(m).hidden = false;
      document.querySelector(`${m}-what`.replace('-modal-what', '-what')).textContent =
        'IMG_2041.MOV · 1.4 GB';
    }, modal);
    ok(`console Large files ${name}: the box has a Show button`,
      await con.locator(`${modal} .gate-reveal`).count() === 1);
    await shoot(con, modal, `console-lf-${name}-${look}`);
    await con.evaluate((m) => { document.querySelector(m).hidden = true; }, modal);
  }
}
await theme(con, 'light');

// Put back what was deleted, so the tests after this one find the library
// as it was.
await con.click('#bin-restore').catch(() => {});
await con.waitForFunction(() => document.querySelectorAll('.bin-row').length === 0,
  null, { timeout: 15000 }).catch(() => {});
ok('what was deleted is put back', (await con.$$('.bin-row')).length === 0);

/* ---------- People: a temporary password and a PIN ---------- */

await con.click('#tabs button[data-tab="people"]');
await con.waitForSelector('[data-panel="people"].active');
await con.waitForSelector('.admin-person', { timeout: 15000 });
const reset = con.locator('.admin-person .ap-actions button', { hasText: 'Reset password' }).first();
let nativePrompt = false;
con.on('dialog', async (dialog) => { nativePrompt = true; await dialog.dismiss(); });
if (await reset.count()) {
  for (const look of ['light', 'dark']) {
    await theme(con, look);
    await reset.click();
    await con.waitForSelector('#secret-modal:not([hidden])', { timeout: 4000 }).catch(() => {});
    const open = await con.evaluate(() => !!document.querySelector('#secret-modal:not([hidden])'));
    if (look === 'light') {
      ok('Reset password asks in the app’s own dialog, not the browser’s prompt',
        open && !nativePrompt);
    }
    if (!open) break;
    if (look === 'light') {
      ok('the suggested temporary password is filled in and shown',
        await con.$eval('#secret-input', (n) => n.value.length >= 8 && n.type === 'text'));
      ok('it has the focus', await con.evaluate(() => document.activeElement?.id === 'secret-input'));
    }
    await shoot(con, '#secret-modal', `console-reset-password-${look}`);
    await con.keyboard.press('Escape');
    await con.waitForTimeout(200);
    ok(`Escape closes it (${look})`,
      await con.evaluate(() => document.querySelector('#secret-modal').hidden));
  }
  await theme(con, 'light');
}

/* ---------- the lock screen's password box ---------- */

for (const look of ['light', 'dark']) {
  await theme(fam, look);
  await fam.evaluate(async () => {
    const src = [...document.scripts].find((s) => s.src.includes('/static/js/app.js')).src;
    const { ScreenLock } = await import(src.replace('/app.js', '/lock.js'));
    const lock = new ScreenLock();
    Object.assign(lock, { running: true, user: { name: 'Dad', initials: 'D' }, unlockWith: 'password' });
    lock.show({ quiet: true });
    window.__lock = lock;
  });
  await fam.waitForSelector('.lock-card');
  if (look === 'light') {
    ok('the lock screen’s password box has a Show button',
      await fam.locator('.lock-card .gate-reveal').count() === 1);
  }
  await fam.fill('.lock-secret', 'secret-pass');
  await fam.waitForTimeout(400);
  const box = await fam.locator('.lock-card').boundingBox();
  await fam.screenshot({ path: path.join(SHOTS, `lock-screen-${look}.png`),
    clip: { x: box.x - 24, y: box.y - 24, width: box.width + 48, height: box.height + 48 } });
  await fam.evaluate(() => window.__lock.hide());
}

ok('no page errors', errs.length === 0, errs.join('; '));
await done(b);
