/**
 * The console has one folder picker, serving two tabs with different rules.
 *
 * The Library tab uses it to choose where the index lives; the Archive tab
 * uses it to choose folders to sweep. A drive root is a fine thing to sweep
 * and a terrible library root, so the picker has to re-title, re-label its
 * button and hand the path back to whoever opened it.
 *
 *   node tests/picker_ui.mjs [adminPort]
 */
import { launch, ok, done, HOME, ADMIN } from './harness.mjs';
// The runner picks a free port, so the harness's ADMIN is the truth;
// an explicit port still wins for a hand-driven run.
const PORT = process.argv[2] || '';
const BASE = PORT ? `http://localhost:${PORT}` : ADMIN;
const browser = await launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
const errors = [];
page.on('pageerror', (e) => errors.push(`PAGEERROR ${e.message}`));

await page.goto(BASE, { waitUntil: 'networkidle' });
await page.fill('#gate input[type="text"]', 'dad');
await page.fill('#gate input[type="password"]', 'correcthorse1');
await page.click('#gate .btn.primary');
await page.waitForSelector('.admin-person', { state: 'attached', timeout: 15000 });

// The Library tab's picker still adds a library folder (default behaviour).
await page.click('#tabs button[data-tab="library"]');
await page.waitForTimeout(400);
await page.click('#change-root');
await page.waitForSelector('#folder-modal:not([hidden])');
// The modal is shown before its contents arrive: the shortcuts and the folder
// list are drawn when the browse request answers. Asserting on the chips the
// moment the modal appears was a race this lost every time.
await page.waitForSelector('.fm-shortcuts .chip', { timeout: 15000 })
  .catch(() => { /* assert on what is there, not on the wait */ });
ok('library picker keeps its own title',
   (await page.textContent('#folder-modal h2')).includes('library'),
   await page.textContent('#folder-modal h2'));
ok('library picker CTA is Use this folder',
   (await page.textContent('#fm-apply')).trim() === 'Use this folder',
   await page.textContent('#fm-apply'));
ok('shortcuts are offered', (await page.$$('.fm-shortcuts .chip')).length > 0);
await page.keyboard.press('Escape');
await page.waitForTimeout(200);

// The Archive tab's picker re-titles and hands the path back instead.
await page.click('#tabs button[data-tab="archive"]');
await page.waitForTimeout(600);
await page.click('#ar-browse-source');
await page.waitForSelector('#folder-modal:not([hidden])');
ok('archive picker re-titles the same modal',
   (await page.textContent('#folder-modal h2')).includes('sweep'),
   await page.textContent('#folder-modal h2'));
ok('archive picker CTA is Add as a source',
   (await page.textContent('#fm-apply')).trim() === 'Add as a source');

// /etc is not a valid *library* root, but it is a valid thing to sweep.
// A real folder, because what this checks is that choosing one adds it as an
// archive source rather than as a library. The path it used to type was
// /tmp/dchk/lib — absent on this machine, so the picker was right to refuse
// it, and the refusal was read as the wrong thing being added.
const CHOSEN = process.env.NINAIVU_SRC_A || process.env.NINAIVU_SHOTS || '.';
// Wait for the browse response before typing. `applyRoot` takes the typed
// path over the browsed one, but the list is drawn when the request answers
// and typing into the box before that lost the value — so Apply fell back to
// whatever folder the picker had opened on, which is the library.
await page.waitForSelector('.fm-shortcuts .chip', { timeout: 15000 })
  .catch(() => { /* type anyway, so the failure says what it did */ });
await page.fill('#fm-manual', CHOSEN);
await page.click('#fm-apply');
await page.waitForTimeout(500);
ok('choosing a folder adds it as a source, not as a library',
   await page.evaluate((wanted) => [...document.querySelectorAll('.ar-source-path')]
     .some((n) => n.textContent === wanted), CHOSEN),
   `wanted ${CHOSEN}; the list holds ` + JSON.stringify(
     await page.$$eval('.ar-source-path', (ns) => ns.map((n) => n.textContent))));
// What this asserts is that *the picker* did not add a library folder, so the
// question is whether the count changed — not what else is in the list. An
// archive adopted by an earlier test in the same instance is not this test's
// business.
const roots = await page.evaluate(async () =>
  (await (await fetch('/api/admin/overview')).json()).library.roots);
ok('the library was not touched', roots.length === 1, JSON.stringify(roots));

ok('no page errors', errors.length === 0, errors.join('; '));
await browser.close();
