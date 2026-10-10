/**
 * Phone keys and the TV album, as a person meets them.
 *
 * The family app's backup screen makes a key for this phone, shows it once
 * with the address and username to type into a sync app, and lists it after;
 * the console lists every key with a Revoke button, and its Server page
 * chooses the TV album and says whether it is showing.
 *
 *   node tests/phone_keys_tv_ui.mjs   (wants both apps up,
 *                                      admin dad / correcthorse1)
 */
import { launch, ok, done, ADMIN, signInAtHome } from './harness.mjs';

const b = await launch();
const shots = process.env.NINAIVU_SHOTS || '';

/* ================= the family app ================= */

const home = await b.newPage({ viewport: { width: 390, height: 844 } });
const homeErrs = [];
home.on('pageerror', (e) => homeErrs.push(e.message));
await signInAtHome(home);
await home.evaluate(() => document.querySelector('#backup-btn')?.click());
await home.waitForSelector('#backup-modal:not([hidden])', { timeout: 10000 });
ok('the backup screen offers automatic backup',
   await home.isVisible('#backup-key-make'));
await home.fill('#backup-device', 'Test phone');
await home.click('#backup-key-make');
await home.waitForSelector('#backup-key-new:not([hidden])', { timeout: 10000 });
const shown = await home.evaluate(() => ({
  url: document.querySelector('#backup-key-url').textContent,
  user: document.querySelector('#backup-key-user').textContent,
  key: document.querySelector('#backup-key-value').textContent,
}));
ok('the address ends in /dav/', shown.url.endsWith('/dav/'), shown.url);
ok('the username is the person signed in', shown.user === 'dad', shown.user);
ok('the key is shown', shown.key.startsWith('nk-'), shown.key);
await home.waitForSelector('#backup-keys .upload-item', { timeout: 10000 });
const listed = await home.locator('#backup-keys .upload-item').first().innerText();
ok('the new key is listed by name and its last characters',
   listed.includes('Test phone') && listed.includes(shown.key.slice(-4)), listed);
ok('the list never shows the key itself', !listed.includes(shown.key), listed);
const fits = await home.evaluate(() => {
  const card = document.querySelector('#backup-modal .modal-card').getBoundingClientRect();
  const end = document.querySelector('#backup-keys');
  end.scrollIntoView({ block: 'end' });
  const last = end.getBoundingClientRect();
  return last.bottom <= card.bottom + 1;
});
ok('everything stays inside the card on a phone', fits);
if (shots) {
  await home.evaluate(() => document.querySelector('#backup-auto').scrollIntoView());
  await home.screenshot({ path: `${shots}/family-phone-key.png` });
}

// The key works: a sync app can see the inbox with it.
const dav = await home.request.fetch(new URL('/dav/', shown.url).toString(), {
  method: 'PROPFIND',
  headers: { Authorization: `Basic ${Buffer.from(`dad:${shown.key}`).toString('base64')}`, Depth: '0' },
});
ok('the key opens the phone inbox', dav.status() === 207, String(dav.status()));
ok('no errors in the family app', homeErrs.length === 0, homeErrs.join('; '));

/* ================= the console ================= */

const con = await b.newPage({ viewport: { width: 1400, height: 1000 } });
const conErrs = [];
con.on('pageerror', (e) => conErrs.push(e.message));
await con.goto(ADMIN, { waitUntil: 'networkidle' });
await con.fill('#gate input[type="text"]', 'dad');
await con.fill('#gate input[type="password"]', 'correcthorse1');
await con.click('#gate .btn.primary');
await con.waitForSelector('.admin-person', { state: 'attached', timeout: 20000 });

// An album for the TV.
const made = await con.evaluate(async () => {
  const r = await fetch('/api/albums', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: `TV test ${Date.now()}` }),
  });
  return r.json();
});
ok('test fixture: an album to show', Boolean(made?.id), JSON.stringify(made));

await con.evaluate(() => document.querySelector('#tabs button[data-tab="uploads"]')?.click());
await con.waitForSelector('#pk-list .pb-person', { timeout: 10000 });
const keyRow = await con.locator('#pk-list .pb-person').first().innerText();
ok('the console lists the phone key', keyRow.includes('Test phone'), keyRow);
if (shots) await con.screenshot({ path: `${shots}/console-phone-keys.png`, fullPage: true });

await con.evaluate(() => document.querySelector('#tabs button[data-tab="server"]')?.click());
await con.waitForSelector('#tv-album-select option[value]:not([value="0"])',
  { state: 'attached', timeout: 10000 });
await con.selectOption('#tv-album-select', String(made.id));
await con.click('#tv-save');
await con.waitForFunction(() => {
  const text = document.querySelector('#tv-status')?.textContent || '';
  return text && !text.startsWith('Off');
}, null, { timeout: 15000 });
const status = await con.textContent('#tv-status');
ok('saving says what the TV album is doing', status.trim().length > 0, status);
if (shots) await con.locator('#tv-block').screenshot({ path: `${shots}/console-tv-album.png` });

// Off again, so the instance is left as it was found.
await con.selectOption('#tv-album-select', '0');
await con.click('#tv-save');
await con.waitForFunction(() => (document.querySelector('#tv-status')?.textContent || '')
  .startsWith('Off'), null, { timeout: 15000 });
ok('turning it off says so', (await con.textContent('#tv-status')).startsWith('Off'));
ok('no errors in the console', conErrs.length === 0, conErrs.join('; '));

await done(b);
