/** Saving on an iPhone keeps the app on screen: node --test tests/save_file.mjs. */
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { SHARE_LIMIT, fileNameFrom, isIOS, saveFile } from '../ninaivu/static/js/save-file.js';

const IPHONE = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15';
const MAC = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15';

/** A browser that records what saveFile asked of it. */
function browser({ ua = IPHONE, share = true, shareFails = null, body = 'jpeg' } = {}) {
  const seen = { clicked: [], opened: [], shared: [], fetched: [], cards: [] };
  Object.defineProperty(globalThis, 'navigator', { configurable: true, writable: true, value: {
    userAgent: ua, platform: ua.includes('Mac') ? 'MacIntel' : 'iPhone', maxTouchPoints: 0,
    ...(share ? {
      canShare: () => true,
      share: async (data) => { seen.shared.push(data.files[0]); if (shareFails) throw Object.assign(new Error('no'), { name: shareFails }); },
    } : {}),
  } });
  globalThis.File = class { constructor(parts, name, opts) { this.name = name; this.type = opts.type; } };
  globalThis.fetch = async (url) => {
    seen.fetched.push(url);
    return { ok: true, blob: async () => ({ type: 'image/jpeg', body }),
             headers: { get: () => `attachment; filename=x.jpg; filename*=UTF-8''Amma%20at%20the%20temple.jpg` } };
  };
  globalThis.window = { open: (url) => seen.opened.push(url) };
  const holder = { appendChild: (node) => seen.cards.push(node) };
  globalThis.document = {
    body: { appendChild() {} },
    getElementById: () => holder,
    createElement: (tag) => ({ tag, dataset: {}, append() {}, setAttribute() {}, addEventListener() {},
                               remove() {}, click() { seen.clicked.push(this.href); } }),
  };
  return seen;
}

test('an iPhone, an iPad asking for the desktop site, and nothing else count as iOS', () => {
  assert.equal(isIOS({ userAgent: IPHONE }), true);
  assert.equal(isIOS({ userAgent: MAC, platform: 'MacIntel', maxTouchPoints: 5 }), true);
  assert.equal(isIOS({ userAgent: MAC, platform: 'MacIntel', maxTouchPoints: 0 }), false);
});

test('the name the server gave the file is the one the share sheet saves', () => {
  assert.equal(fileNameFrom(`attachment; filename=a.jpg; filename*=UTF-8''%E0%AE%85.jpg`), 'அ.jpg');
  assert.equal(fileNameFrom('attachment; filename="IMG_1.HEIC"'), 'IMG_1.HEIC');
  assert.equal(fileNameFrom(null), 'photo');
});

test('a computer downloads as it always did', async () => {
  const seen = browser({ ua: MAC });
  await saveFile('/api/download/7');
  assert.deepEqual(seen.clicked, ['/api/download/7']);
  assert.equal(seen.shared.length, 0);
});

test('an iPhone saves a photograph through the share sheet and stays in the app', async () => {
  const seen = browser();
  await saveFile('/api/download/7', { size: 4_000_000 });
  assert.deepEqual(seen.fetched, ['/api/download/7']);
  assert.equal(seen.shared[0].name, 'Amma at the temple.jpg');
  assert.equal(seen.clicked.length + seen.opened.length, 0);
});

test('a ZIP, a huge video or an old phone opens in a new tab, never over the app', async () => {
  let seen = browser();
  await saveFile('/api/download/zip?ids=1,2', { archive: true });
  assert.deepEqual(seen.opened, ['/api/download/zip?ids=1,2']);
  seen = browser();
  await saveFile('/api/download/9', { size: SHARE_LIMIT + 1 });
  assert.deepEqual(seen.opened, ['/api/download/9']);
  assert.equal(seen.fetched.length, 0);
  seen = browser({ share: false });
  await saveFile('/api/download/9');
  assert.deepEqual(seen.opened, ['/api/download/9']);
});

test('a share refused because the tap is too long ago offers one more tap', async () => {
  let seen = browser({ shareFails: 'NotAllowedError' });
  await saveFile('/api/download/7');
  assert.equal(seen.cards.length, 1);
  seen = browser({ shareFails: 'AbortError' });
  await saveFile('/api/download/7');
  assert.equal(seen.cards.length, 0);
});
