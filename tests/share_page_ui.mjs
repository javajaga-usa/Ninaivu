/**
 * The shared-link page runs, and speaks the language of whoever opens it.
 *
 * It is the one page in Ninaivu somebody outside the household ever sees, and
 * it was the one page with no translation wired in — so a link sent to a
 * relative who reads Tamil opened in English, with no control to change it.
 *
 * Wiring it in meant turning `share.js` from a plain deferred script into a
 * module, which is not a change to make without watching the page load: a
 * module that throws leaves the page on "Loading…" for ever, and nothing else
 * here would have noticed.
 *
 * A real link, minted through the API, and then opened in a context with no
 * session at all — which is the only honest way to test a page whose whole
 * point is being opened by somebody who is not signed in.
 */
import { launch, ok, done, signInAtHome, HOME } from './harness.mjs';

const browser = await launch();

/* -- mint a link, the way the viewer does ---------------------------------- */

const owner = await browser.newContext();
const ownerPage = await owner.newPage();
// The family gate opens as a profile picker, not a password form, so the
// harness signs in by posting and reloading.
await signInAtHome(ownerPage);

const token = await ownerPage.evaluate(async () => {
  const list = await fetch('/api/assets?limit=1', {
    headers: { Accept: 'application/json' },
  }).then((r) => r.json()).catch(() => null);
  const id = list?.items?.[0]?.id;
  if (!id) return { error: 'no asset to share' };
  const made = await fetch('/api/shares', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ scope: 'asset', target_id: id }),
  });
  const body = await made.json().catch(() => ({}));
  return made.ok ? { token: body.token || body.share?.token }
    : { error: `${made.status} ${body.error || ''}` };
});
await owner.close();

ok('a share link can be minted', Boolean(token.token), token.error || '');
if (!token.token) await done(browser);

/* -- and opened by somebody who is not signed in --------------------------- */

async function open(language) {
  const context = await browser.newContext({ locale: language });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push(m.text());
  });
  page.on('response', (r) => {
    if (r.status() >= 400) errors.push(`${r.status()} ${new URL(r.url()).pathname}`);
  });
  // A page with no icon link makes the browser ask for /favicon.ico, which
  // nothing serves. That was a 404 on every shared link anybody opened, and
  // it is why this page now carries the same inline icon as the gallery.
  await page.goto(`${HOME}/share/${token.token}`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(1200);
  const seen = await page.evaluate(() => ({
    text: document.body.innerText.trim(),
    lang: document.documentElement.lang,
    from: document.getElementById('from')?.textContent.trim() || '',
    title: document.getElementById('title')?.textContent.trim() || '',
  }));
  await context.close();
  return { ...seen, errors };
}

const english = await open('en-GB');
ok('the page runs as a module without throwing',
  english.errors.length === 0, english.errors.join(' | '));
ok('it does not sit on "Loading…" for ever',
  !english.text.includes('Loading…'), english.text.slice(0, 90));
ok('it names the household app the link came from',
  english.from.length > 0, english.from);
ok('the document is marked as English', english.lang === 'en', english.lang);

const tamil = await open('ta-IN');
ok('a reader whose browser asks for Tamil gets Tamil',
  tamil.lang === 'ta', tamil.lang);
ok('and the line naming where it came from is translated',
  tamil.from !== english.from, `both read "${english.from}"`);
ok('with nothing thrown on that path either',
  tamil.errors.length === 0, tamil.errors.join(' | '));

await done(browser);
