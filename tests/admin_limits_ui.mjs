/**
 * A library root that cannot be set, and a delete that asks one thing.
 *
 * Both live on the server and are tested there. What this checks is the half
 * a person actually meets: the console does not offer the whole-library
 * control at all, and the gallery's Delete goes straight to the password for
 * any photograph — there is no hiding step in front of it any more.
 *
 *   node tests/admin_limits_ui.mjs   (wants both apps up,
 *                                     admin dad / correcthorse1)
 */
import { launch, ok, done, HOME, ADMIN } from './harness.mjs';

const b = await launch();

/* ================= the console ================= */

const con = await b.newPage({ viewport: { width: 1500, height: 1000 } });
const conErrs = [];
con.on('pageerror', (e) => conErrs.push(e.message));
await con.goto(ADMIN, { waitUntil: 'networkidle' });
await con.fill('#gate input[type="text"]', 'dad');
await con.fill('#gate input[type="password"]', 'correcthorse1');
await con.click('#gate .btn.primary');
await con.waitForSelector('.admin-person', { state: 'attached', timeout: 20000 });
await con.click('#tabs button[data-tab="visibility"]');
await con.waitForTimeout(2500);

const rootRow = await con.evaluate(() => {
  const row = [...document.querySelectorAll('.folder-row')]
    .find((r) => r.classList.contains('is-root'));
  if (!row) return null;
  return {
    buttons: row.querySelectorAll('.vis-picker button').length,
    note: row.querySelector('.folder-note')?.textContent.trim() || '',
    text: row.innerText.replace(/\s+/g, ' ').trim(),
  };
});
ok('the library root is still listed', rootRow !== null, JSON.stringify(rootRow));
ok('but it has no buttons', rootRow?.buttons === 0, JSON.stringify(rootRow));
ok('and it says where to set it instead',
  /folder|photograph/i.test(rootRow?.note || ''), rootRow?.note);

const namedButtons = await con.evaluate(() => {
  const row = [...document.querySelectorAll('.folder-row')]
    .find((r) => !r.classList.contains('is-root'));
  return row ? row.querySelectorAll('.vis-picker button').length : -1;
});
ok('a named folder still has all three', namedButtons === 3, String(namedButtons));

ok('the whole-library password box is gone with the control it guarded',
  await con.evaluate(() => document.querySelector('#vispw-modal') === null));

// And the server agrees, whatever the page does.
const refused = await con.evaluate(async () => {
  const r = await fetch('/api/visibility/folder', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ folder: '', visibility: 'hidden', confirm: true,
                           password: 'correcthorse1' }),
  });
  return { status: r.status, body: await r.json() };
});
ok('the route refuses the root even when asked directly',
  refused.status === 403, JSON.stringify(refused));

/* ================= the gallery ================= */

const fam = await b.newPage({ viewport: { width: 1440, height: 900 } });
const famErrs = [];
fam.on('pageerror', (e) => famErrs.push(e.message));
await fam.goto(HOME, { waitUntil: 'networkidle' });
await fam.evaluate(async () => {
  await fetch('/api/auth/login', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: 'dad', password: 'correcthorse1' }),
  });
});
await fam.reload({ waitUntil: 'networkidle' });
await fam.waitForFunction(() => document.querySelectorAll('.cell').length > 2,
  null, { timeout: 30000 });
await fam.waitForTimeout(1500);

const pick = async (n) => {
  await fam.evaluate((i) => document.querySelectorAll('.cell')[i]
    ?.dispatchEvent(new MouseEvent('click', { bubbles: true, ctrlKey: true })), n);
  await fam.waitForTimeout(300);
};
const toasts = () => fam.evaluate(() =>
  [...document.querySelectorAll('.toast')].map((n) => n.textContent.trim()));

/* ---------- a visible photograph goes straight to the password ---------- */

// By id, not by position. A library with a sound recording in it puts an
// admin-only item at the front of the grid, and picking "the first cell"
// would quietly test the hidden case instead of this one.
const visible = await fam.evaluate(async () => {
  const r = await (await fetch('/api/assets?limit=200')).json();
  return r.items.find((i) => i.kind === 'picture' && i.visibility !== 'hidden')?.id;
});
ok('the library has a visible photograph to try this on', Boolean(visible));
await fam.evaluate((id) => {
  const cell = [...document.querySelectorAll('.cell')]
    .find((c) => Number(c.dataset.id) === id);
  cell?.dispatchEvent(new MouseEvent('click', { bubbles: true, ctrlKey: true }));
}, visible);
await fam.waitForTimeout(300);
await fam.waitForSelector('#sel-delete:not([hidden])', { timeout: 8000 });
await fam.click('#sel-delete');
await fam.waitForTimeout(1200);

ok('the password box opens for a visible photograph',
  !(await fam.evaluate(() => document.querySelector('#delete-modal').hidden)));
const said = await toasts();
ok('and nothing tells anybody to go and hide it first',
  !said.some((t) => /must be hidden|set them to/i.test(t)), JSON.stringify(said));

await fam.fill('#delete-password', 'correcthorse1');
await fam.click('#delete-confirm');
await fam.waitForTimeout(3000);
const gone = await fam.evaluate(async (id) =>
  (await fetch(`/api/asset/${id}`)).status, visible);
ok('and it is deleted', gone === 404, String(gone));

/* ---------- and erasing it from the bin asks again ---------- */

const entry = await con.evaluate(async () =>
  (await (await fetch('/api/recycle')).json()).items[0]);
ok('the deleted photograph is in the bin', Boolean(entry), JSON.stringify(entry));

const noPassword = await con.evaluate(async (id) => {
  const r = await fetch('/api/recycle/purge', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids: [id] }),
  });
  return { status: r.status, body: await r.json() };
}, entry?.id);
ok('erasing without the password is refused',
  noPassword.status === 401 && noPassword.body.needs_password === true,
  JSON.stringify(noPassword));

// Put it back, so the fixture library is left as it was found.
await con.evaluate(async () => {
  const bin = await (await fetch('/api/recycle')).json();
  const ids = bin.items.map((i) => i.id);
  if (ids.length) {
    await fetch('/api/recycle/restore', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids }),
    });
  }
});

ok('no page errors in the console', conErrs.length === 0, conErrs.join(' | '));
ok('no page errors in the gallery', famErrs.length === 0, famErrs.join(' | '));
await done(b);
