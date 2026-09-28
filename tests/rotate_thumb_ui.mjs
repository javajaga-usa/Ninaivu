/**
 * A saved rotation showing up in the gallery, and doing it quietly.
 *
 * Thumbnails are served with `Cache-Control: immutable` and a year's lifetime,
 * which is right — they are derived files addressed by asset id, and re-asking
 * for them on every scroll would be waste. The consequence is that rewriting
 * one on disk changes nothing a browser will ever look at again: the rotation
 * saved, the file on disk was correct, and the grid went on showing yesterday's
 * bytes. `immutable` means the URL has to change when the content does.
 *
 * The second half is quieter still: saving a rotation should not announce
 * itself. The photograph turning is the confirmation.
 *
 *   node tests/rotate_thumb_ui.mjs
 *      (wants an instance on 5000 with an oblong photograph in it,
 *       admin dad / correcthorse1)
 */
import { launch, ok, done, HOME, ADMIN } from './harness.mjs';

const b = await launch();

const page = await b.newPage({ viewport: { width: 1440, height: 900 } });
const errs = [];
page.on('pageerror', (e) => errs.push(e.message));

// Every thumbnail the page asks for, so a re-fetch is observable.
const thumbRequests = [];
page.on('request', (r) => {
  if (r.url().includes('/api/thumb/')) thumbRequests.push(r.url());
});

await page.goto(HOME, { waitUntil: 'networkidle' });
await page.evaluate(async () => {
  await fetch('/api/auth/login', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: 'dad', password: 'correcthorse1' }),
  });
});
await page.reload({ waitUntil: 'networkidle' });
await page.waitForFunction(() => document.querySelectorAll('.cell').length > 3,
  null, { timeout: 30000 });
await page.waitForTimeout(1500);

/** The <img> src the grid is currently using for this asset. */
const gridSrc = (id) => page.evaluate((i) => {
  const cell = [...document.querySelectorAll('.cell')]
    .find((c) => Number(c.dataset.id) === i);
  return cell?.querySelector('img')?.getAttribute('src') || '';
}, id);

// An oblong photograph, so the turn is visible in the thumbnail's shape.
const target = await page.evaluate(async () => {
  const r = await (await fetch('/api/assets?limit=200')).json();
  return r.items.find((i) => i.kind === 'picture' && i.width !== i.height)?.id;
});
ok('there is an oblong photograph to work with', !!target, String(target));

const before = await gridSrc(target);
ok('the grid asks for thumbnails with a version on them',
  /[?&]v=/.test(before), before);

/* ---------- rotate it, from the viewer ---------- */

await page.evaluate((i) => [...document.querySelectorAll('.cell')]
  .find((c) => Number(c.dataset.id) === i)?.click(), target);
await page.waitForTimeout(1600);
thumbRequests.length = 0;

await page.click('#v-rotate');
await page.waitForTimeout(3000);

/* ---------- it saved, and it said nothing ---------- */

const saved = await page.evaluate(async (i) =>
  (await (await fetch(`/api/asset/${i}`)).json()), target);
ok('the rotation saved', saved.rotation === 90,
  `${saved.rotation} / ${saved.rotation_source}`);

const toasts = await page.evaluate(() =>
  [...document.querySelectorAll('.toast, #toasts > *')]
    .map((n) => n.textContent.trim()).filter(Boolean));
ok('saving does not announce itself', toasts.length === 0, JSON.stringify(toasts));

/* ---------- and the gallery shows it ---------- */

await page.keyboard.press('Escape');
await page.waitForTimeout(3000);

const after = await gridSrc(target);
ok('the thumbnail URL changed, so the browser fetches the new one',
  after && after !== before, `${before}  →  ${after}`);
ok('the new thumbnail was actually requested',
  thumbRequests.some((u) => u.includes(`/api/thumb/${target}`)),
  JSON.stringify(thumbRequests.slice(0, 3)));

const shape = await page.evaluate((i) => {
  const cell = [...document.querySelectorAll('.cell')]
    .find((c) => Number(c.dataset.id) === i);
  const img = cell?.querySelector('img');
  return img ? { w: img.naturalWidth, h: img.naturalHeight } : null;
}, target);
ok('the thumbnail the grid drew is the turned one',
  shape && shape.h > shape.w, JSON.stringify(shape));

/* ---------- put it back, quietly ---------- */

await page.evaluate(async (i) => {
  await fetch(`/api/asset/${i}/rotate`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ rotation: 0 }),
  });
}, target);

ok('no page errors', errs.length === 0, errs.join(' | '));
await done(b);
