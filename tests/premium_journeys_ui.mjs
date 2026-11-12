/** Delayed responses, retry and keyboard continuity across family journeys. */
import assert from 'node:assert/strict';
import { launch, signInAtHome } from './harness.mjs';

const browser = await launch();
const waitFor = async (page, condition) => {
  const deadline = Date.now() + 10000;
  while (!condition()) {
    assert.ok(Date.now() < deadline, 'the expected request should start within ten seconds');
    await page.waitForTimeout(20);
  }
};
try {
  const context = await browser.newContext({ serviceWorkers: 'block' });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await signInAtHome(page);
  await page.waitForSelector('#grid .cell');

  let answerSuggestion;
  await page.route('**/api/suggest**', async route => {
    await new Promise(resolve => { answerSuggestion = resolve; });
    await route.fulfill({ json: { suggestions: [{ type: 'tag', value: 'holiday' }] } });
  });
  await page.locator('#search').fill('holiday');
  await page.waitForFunction(() => document.querySelector('#search').value === 'holiday');
  await waitFor(page, () => answerSuggestion);
  await page.keyboard.press('Escape');
  answerSuggestion();
  await page.waitForTimeout(350);
  assert.equal(await page.locator('#suggestions').isHidden(), true);
  assert.equal(await page.locator('#search').getAttribute('aria-expanded'), 'false');
  assert.equal(await page.locator('#search').evaluate(node => node === document.activeElement), true);
  await page.locator('#clear-search').click();
  assert.equal(await page.locator('#search').evaluate(node => node === document.activeElement), true);
  console.log('PASS: dismissed late suggestions stay closed and clearing search keeps focus');

  await page.evaluate(() => document.querySelector('#upload-btn').click());
  await page.waitForSelector('#upload-modal:not([hidden])');
  assert.equal(await page.locator('#upload-dropzone').evaluate(node => node === document.activeElement), true);
  await page.locator('#upload-close').focus();
  await page.keyboard.press('Tab');
  assert.equal(await page.locator('#upload-dropzone').evaluate(node => node === document.activeElement), true);
  let answerUpload;
  let uploads = 0;
  await page.route('**/api/upload', async route => {
    uploads++;
    await new Promise(resolve => { answerUpload = resolve; });
    await route.fulfill({ status: 500, json: { error: 'Try again' } });
  });
  const file = { name: 'retry.png', mimeType: 'image/png', buffer: Buffer.from('sample') };
  await page.locator('#upload-file-input').setInputFiles(file);
  await waitFor(page, () => answerUpload);
  assert.equal(await page.locator('#upload-file-input').isDisabled(), true);
  assert.equal(await page.locator('#upload-dropzone').getAttribute('aria-busy'), 'true');
  answerUpload();
  await page.waitForFunction(() => !document.querySelector('#upload-file-input').disabled);
  assert.equal(await page.locator('#upload-file-input').inputValue(), '');
  assert.match(await page.locator('#upload-status').textContent(), /Try again/);
  assert.equal(uploads, 1);
  await page.locator('#upload-close').click();
  console.log('PASS: upload focus wraps, failures stay visible and the same file can be selected again');

  await page.locator('#grid .cell').first().click();
  await page.waitForSelector('#viewer:not([hidden])');
  await page.waitForFunction(() => document.querySelector('#v-name').textContent.trim());
  await page.evaluate(() => document.querySelector('#v-share').click());
  await page.waitForSelector('#share-modal:not([hidden])');
  let answerShare;
  await page.route('**/api/shares', async route => {
    await new Promise(resolve => { answerShare = resolve; });
    await route.fulfill({ json: { share_url: '/share/old-response' } });
  });
  await page.locator('#share-create-btn').click();
  await waitFor(page, () => answerShare);
  await page.locator('#share-close').click();
  await page.evaluate(() => document.querySelector('#v-share').click());
  await page.waitForSelector('#share-modal:not([hidden])');
  answerShare();
  await page.waitForTimeout(250);
  assert.equal(await page.locator('#share-link-result').isHidden(), true);
  assert.equal(await page.locator('#share-create-btn').isEnabled(), true);
  assert.equal(await page.locator('#share-url-text').inputValue(), '');
  await page.unroute('**/api/shares');
  await page.route('**/api/shares', route => route.fulfill({ json: { share_url: '/share/current' } }));
  await page.locator('#share-create-btn').click();
  await page.waitForSelector('#share-link-result:not([hidden])');
  assert.match(await page.locator('#share-url-text').inputValue(), /\/share\/current$/);
  assert.equal(await page.locator('#share-copy-btn').evaluate(node => node === document.activeElement), true);
  assert.deepEqual(errors, []);
  console.log('PASS: reopening sharing ignores the previous request and leaves a clean, usable dialog');
} finally {
  await browser.close();
}
