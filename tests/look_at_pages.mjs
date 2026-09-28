/**
 * Open the console pages nobody has ever looked at, and photograph them.
 *
 * Migration, AI models and the weekly digest were built, tested and shipped
 * without a person seeing them rendered. Tests say the markup is present and
 * the endpoints answer; they say nothing about whether the thing reads right,
 * lines up, or has an empty box where a number should be.
 *
 * Not a test — it asserts nothing. It signs in, opens each page, waits for it
 * to settle, writes a full-page screenshot and dumps the visible text.
 *
 *   node tests/look_at_pages.mjs <output directory>
 */
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { launch, ADMIN, ADMIN_USER, ADMIN_PASS } from './harness.mjs';

const out = process.argv[2] || 'page-shots';
await mkdir(out, { recursive: true });

const PAGES = ['overview', 'migration', 'ai-models', 'archive', 'extras'];

const browser = await launch();
const page = await browser.newPage({ viewport: { width: 1400, height: 1000 } });

const noise = [];
page.on('pageerror', (e) => noise.push(`PAGEERROR ${e.message}`));
page.on('console', (m) => {
  if (m.type() === 'error') noise.push(`CONSOLE ${m.text()}`);
});

await page.goto(ADMIN, { waitUntil: 'networkidle' });
await page.fill('#gate input[type="text"]', ADMIN_USER);
await page.fill('#gate input[type="password"]', ADMIN_PASS);
await page.click('#gate .btn.primary');
await page.waitForSelector('#tabs', { state: 'visible', timeout: 20000 });

for (const name of PAGES) {
  const tab = await page.$(`#tabs button[data-tab="${name}"]`);
  if (!tab) {
    console.log(`MISSING  no tab for ${name}`);
    continue;
  }
  await tab.click();
  await page.waitForTimeout(1200);          // let it fetch and settle
  await page.screenshot({
    path: path.join(out, `${name}.png`), fullPage: true,
  });
  const text = await page.$eval(
    `.panel[data-panel="${name}"]`,
    (el) => el.innerText.replace(/\n{3,}/g, '\n\n').trim(),
  ).catch(() => '(no panel found)');
  await writeFile(path.join(out, `${name}.txt`), text, 'utf8');
  const lines = text.split('\n').filter(Boolean).length;
  console.log(`SHOT  ${name}  (${lines} lines of text)`);
}

console.log(noise.length ? `\nNOISE:\n  ${noise.join('\n  ')}`
  : '\nno console errors on any of them');
await browser.close();
