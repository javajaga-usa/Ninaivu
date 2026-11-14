/**
 * The Health page's Backup drill: one button that restores a few photographs
 * from every copy and holds them to the library's fingerprints
 * (ninaivu/copies/drill.py). With no copy set up it says there is nothing
 * to check, in English and in Tamil.
 *
 *   node tests/backup_drill_ui.mjs   (wants the console up)
 * @fixture several
 */
import { launch, ok, done, signInAtConsole } from './harness.mjs';

const b = await launch();
const errs = [];
const context = await b.newContext({ viewport: { width: 1280, height: 900 }, serviceWorkers: 'block' });
const page = await context.newPage();
page.on('pageerror', (e) => errs.push(e.message));
await signInAtConsole(page);

async function openHealth() {
  await page.click('#tab-groups button[data-group="backup"]').catch(() => {});
  await page.click('#tabs button[data-tab="health"]');
  await page.waitForFunction(() => document.querySelector('#drill-headline')?.textContent.trim(),
    null, { timeout: 15000 });
}

await openHealth();
ok('the button is there', await page.isVisible('#drill-run'));

await page.click('#drill-run');
await page.waitForFunction(async () => {
  const s = await (await fetch('/api/admin/backup-drill')).json();
  return !s.running && s.last;
}, null, { timeout: 60000, polling: 500 });
await page.waitForFunction(() => !document.querySelector('#drill-run').disabled, null, { timeout: 15000 });
const asked = await page.evaluate(async () => (await fetch('/api/admin/backup-drill')).json());
ok('the drill finished and was kept', Boolean(asked.last?.status), JSON.stringify(asked).slice(0, 200));
const headline = (await page.textContent('#drill-headline')).trim();
ok('the headline gives the verdict', /^(Passed|FAILED|Not finished|Nothing to check)/.test(headline), headline);

await page.evaluate(() => { localStorage.setItem('ninaivu.lang', 'ta'); });
await page.reload();
await openHealth();
const button = (await page.textContent('#drill-run')).trim();
ok('in Tamil, the button is in Tamil', button === 'என் காப்புப்பிரதியைச் சரிபார்', button);

ok('no script errors', errs.length === 0, errs.join('\n'));
await done(b);
