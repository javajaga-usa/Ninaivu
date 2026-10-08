/**
 * Voice stories in the viewer, on a phone's screen: open the panel, record a
 * story with the browser's (fake) microphone, save it, see the count, hear it
 * listed, delete it — and the microphone is not asked for until Record.
 */
import { launch, ok, done, HOME, signInAtHome } from './harness.mjs';

const browser = await launch({
  args: ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream'],
});
const context = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true });
await context.grantPermissions(['microphone'], { origin: HOME });
const page = await context.newPage();
const errors = [];
page.on('pageerror', (e) => errors.push(e.message));
page.on('dialog', (dialog) => dialog.accept());
// Counted from the page: the microphone must be asked for only when Record is pressed.
await page.addInitScript(() => {
  window.__micAsks = 0;
  const md = navigator.mediaDevices;
  if (md?.getUserMedia) {
    const real = md.getUserMedia.bind(md);
    md.getUserMedia = (c) => { window.__micAsks += 1; return real(c); };
  }
});

await signInAtHome(page);
await page.waitForFunction(() => document.querySelectorAll('#grid .cell').length > 0, { timeout: 15000 });
await page.click('#grid .cell');
await page.waitForSelector('.viewer:not([hidden])', { timeout: 5000 });
await page.waitForTimeout(400);

const bar = await page.evaluate(() => {
  const b = document.querySelector('#v-stories').getBoundingClientRect();
  return { visible: b.width > 0 && b.right <= window.innerWidth, asks: window.__micAsks };
});
ok('the Stories button is on the bar within a phone screen', bar.visible);
ok('opening a photograph does not ask for the microphone', bar.asks === 0);

await page.click('#v-stories');
await page.waitForSelector('#viewer-stories:not([hidden])', { timeout: 3000 });
await page.waitForSelector('#stories-actions:not([hidden])', { timeout: 3000 });
await page.waitForTimeout(400);                    // the panel slides in
const panel = await page.evaluate(() => {
  const r = document.querySelector('#viewer-stories').getBoundingClientRect();
  const close = document.querySelector('#v-stories-close').getBoundingClientRect();
  return { r: [r.left, r.right, r.top, window.innerWidth], c: [close.top, close.width],
           fits: r.left >= 0 && r.right <= window.innerWidth + 1, closeShown: close.top >= 0 && close.width > 0,
           empty: !document.querySelector('#stories-empty').hidden, asks: window.__micAsks };
});
ok('the panel fills the phone screen and its close is in reach', panel.fits && panel.closeShown,
  JSON.stringify(panel));
ok('an item with no story says so', panel.empty);
ok('opening the panel does not ask for the microphone either', panel.asks === 0);

await page.click('#story-record-start');
await page.waitForSelector('#story-stop:not([hidden])', { timeout: 5000 }).catch(async () => {
  ok('the recorder starts', false, await page.textContent('#story-status'));
  await done(browser);
  process.exit(1);
});
ok('Record asks for the microphone', await page.evaluate(() => window.__micAsks === 1));
await page.waitForTimeout(1600);
const timer = await page.textContent('#story-timer');
ok('the timer counts while recording', timer !== '0:00', timer);
await page.click('#story-stop');
await page.waitForSelector('#story-preview:not([hidden])', { timeout: 5000 });
await page.fill('#story-text', 'அப்பாவின் கடை, 1968');
const speaker = await page.inputValue('#story-speaker');
ok('the speaker is the signed-in person by default', speaker.length > 0, speaker);
await page.fill('#story-speaker', 'Paati');
await page.click('#story-save');
await page.waitForFunction(() => document.querySelectorAll('#stories-list .story').length === 1, { timeout: 8000 });
const saved = await page.evaluate(() => ({
  count: document.querySelector('#v-stories-count').textContent,
  shown: !document.querySelector('#v-stories-count').hidden,
  speaker: document.querySelector('#stories-list .story strong').textContent,
  text: document.querySelector('#stories-list .story-text')?.textContent,
  formHidden: document.querySelector('#story-recorder').hidden,
}));
ok('the story is listed with its speaker and words', saved.speaker === 'Paati' && saved.text === 'அப்பாவின் கடை, 1968',
  JSON.stringify(saved));
ok('the Stories button shows the count', saved.shown && saved.count === '1', JSON.stringify(saved));
ok('the recorder closes once saved', saved.formHidden);

const found = await page.evaluate(async () => {
  const r = await fetch('/api/assets?q=%E0%AE%95%E0%AE%9F%E0%AF%88&plain=1', { headers: { Accept: 'application/json' } });
  return (await r.json()).total;
});
ok('the typed words are found by the search', found === 1, String(found));

// Somebody holding a share link of it hears the story, and can do no more.
const token = await page.evaluate(async () => {
  const id = Number(document.querySelector('#v-download').href.split('/').pop());
  const made = await fetch('/api/shares', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ scope: 'asset', target_id: id }),
  });
  const body = await made.json();
  return body.token || body.share?.token;
});
const stranger = await browser.newContext({ viewport: { width: 390, height: 844 } });
const shared = await stranger.newPage();
const sharedErrors = [];
shared.on('pageerror', (e) => sharedErrors.push(e.message));
await shared.goto(`${HOME}/share/${token}`, { waitUntil: 'networkidle' });
await shared.waitForSelector('details.stories .story audio', { timeout: 5000 }).catch(() => {});
const heard = await shared.evaluate(() => ({
  summary: document.querySelector('details.stories summary')?.textContent,
  speaker: document.querySelector('details.stories .story strong')?.textContent,
  buttons: document.querySelectorAll('details.stories button').length,
}));
ok('a share link lists the story to listen to', heard.speaker === 'Paati' && heard.buttons === 0,
  JSON.stringify(heard));
ok('the share page runs without errors', sharedErrors.length === 0, sharedErrors.join(' | '));
await stranger.close();

// Escape from inside the panel closes the panel, not the viewer.
await page.focus('#v-stories-close');
await page.keyboard.press('Escape');
await page.waitForTimeout(200);
const escaped = await page.evaluate(() => ({
  panel: document.querySelector('#viewer-stories').hidden,
  viewer: !document.querySelector('#viewer').hidden,
}));
ok('Escape closes the panel and leaves the photograph open', escaped.panel && escaped.viewer, JSON.stringify(escaped));

await page.keyboard.press('v');
await page.waitForSelector('#viewer-stories:not([hidden])', { timeout: 3000 });
ok('V opens the stories from the keyboard', true);
await page.click('#stories-list .story .btn.danger');
await page.waitForFunction(() => document.querySelectorAll('#stories-list .story').length === 0, { timeout: 5000 });
ok('a story can be deleted, and the count goes', await page.evaluate(
  () => document.querySelector('#v-stories-count').hidden));
ok('no script errors', errors.length === 0, errors.join(' | '));

await done(browser);
