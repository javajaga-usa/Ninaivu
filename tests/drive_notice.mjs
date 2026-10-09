/**
 * A drive plugged in is a notice at the top of the console, not a dialog.
 *
 * It used to open a centred dialog in the console and, on a Mac, a second
 * window of the operating system's own over whatever was in front. Now the
 * console shows one notice per waiting drive under the top of the window;
 * clicking it opens the Import page with the drive as its source, and nothing
 * opens over the page.
 *
 * Runs the real `drives.js` against a page just big enough to hold it.
 *
 *   node tests/drive_notice.mjs   (no server needed)
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

class Node {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.parent = null;
    this.className = '';
    this.dataset = {};
    this.attrs = {};
    this.hidden = false;
    this.textContent = '';
    this.innerHTML = '';
  }

  appendChild(child) { child.parent = this; this.children.push(child); return child; }
  append(...nodes) { nodes.forEach((node) => this.appendChild(node)); }
  replaceChildren(...nodes) { this.children = []; nodes.forEach((node) => this.appendChild(node)); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this); }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  addEventListener() {}
  get firstChild() { return this.children[0]; }

  find(className) {
    for (const child of this.children) {
      if (child.className.split(' ').includes(className)) return child;
      const found = child.find(className);
      if (found) return found;
    }
    return null;
  }

  all(className) {
    return this.children.flatMap((child) => [
      ...(child.className.split(' ').includes(className) ? [child] : []),
      ...child.all(className),
    ]);
  }
}

const body = new Node('body');
globalThis.document = {
  hidden: false,
  body,
  createElement: (tag) => new Node(tag),
  querySelector: () => null,
};

// The module under test, with its one import replaced by a stand-in that
// answers every string with itself.
globalThis.__i18n = {
  t: (text, params = {}) => String(text).replace(/\{(\w+)\}/g, (_, key) => params[key]),
  key: (text) => text,
  onChange: () => {},
};
const source = readFileSync(new URL('../ninaivu/static/js/drives.js', import.meta.url), 'utf8')
  .replace("import * as i18n from './i18n.js';", 'const i18n = globalThis.__i18n;');
const { DrivePrompt } = await import('data:text/javascript,' + encodeURIComponent(source));

const calls = [];
const drive = (extra = {}) => ({
  id: 'd1', label: 'EOS_DIGITAL', path: '/Volumes/EOS_DIGITAL', kind: 'drive',
  readable: true, pending: true, total: 64e9, free: 12e9, ...extra,
});
let list = [drive()];
let imageCapture = false;
const prompt = new DrivePrompt({
  json: async (url, options = {}) => {
    calls.push([url, options.method || 'GET']);
    if (url === '/api/admin/drives') return { drives: list, image_capture: imageCapture, export: null };
    return {};
  },
  toast: (text) => calls.push(['toast', text]),
  openImport: async (path) => calls.push(['openImport', path]),
  openImportPage: () => calls.push(['openImportPage']),
});

// -- a drive waiting: a notice, and no dialog --------------------------------
await prompt.check();
const tray = body.find('drive-notices');
assert.ok(tray, 'the notices have a place at the top of the page');
assert.equal(tray.hidden, false);
assert.equal(tray.children.length, 1);
assert.equal(prompt.modal, null, 'nothing opens in a window over the page');
const notice = tray.children[0];
assert.equal(notice.find('drive-notice-text').find('drive-notice-name').textContent.startsWith(
  'EOS_DIGITAL (/Volumes/EOS_DIGITAL)'), true);
assert.equal(notice.find('drive-notice-hint').textContent, 'Click to import its photos and videos.');
const labels = notice.all('btn').map((b) => b.textContent);
assert.ok(labels.includes('Export media to this drive'));
assert.ok(labels.includes('Don’t ask about this drive again'));

// The same drive asked about on the next look is the same notice.
await prompt.check();
assert.equal(tray.children.length, 1);

// -- clicking it opens the Import page with the drive as its source ----------
await notice.find('drive-notice-main').onclick();
assert.deepEqual(calls.filter((c) => c[0] === 'openImport'), [['openImport', '/Volumes/EOS_DIGITAL']]);
assert.ok(calls.some((c) => c[0] === '/api/admin/drives/answer' && c[1] === 'POST'));
assert.equal(tray.children.length, 0, 'answered, so the notice goes');
assert.equal(tray.hidden, true);
// The server has not caught up yet: it is still "pending" for a poll or two,
// and must not come straight back.
await prompt.check();
assert.equal(tray.children.length, 0);
// Unplugged and plugged in again, it is asked about again.
list = [];
await prompt.check();
list = [drive()];
await prompt.check();
assert.equal(tray.children.length, 1);

// -- Not now, and Don't ask again, go without opening anything ---------------
calls.length = 0;
await tray.children[0].find('drive-notice-close').onclick();
assert.equal(tray.children.length, 0);
assert.equal(calls.some((c) => c[0] === 'openImport' || c[0] === 'openImportPage'), false);
assert.ok(calls.some((c) => c[0] === '/api/admin/drives/answer'));

// -- a phone a Mac cannot read: says so, and goes to the Import page ---------
list = [drive({ id: 'p1', kind: 'phone', readable: false, label: 'iPhone', path: 'iPhone' })];
imageCapture = true;
calls.length = 0;
await prompt.check();
const phone = tray.children[0];
assert.match(phone.find('drive-notice-hint').textContent, /Image Capture/);
assert.ok(phone.all('btn').some((b) => b.textContent === 'Open Image Capture'));
assert.equal(phone.all('btn').some((b) => b.textContent === 'Export media to this drive'), false);
await phone.find('drive-notice-main').onclick();
assert.ok(calls.some((c) => c[0] === 'openImportPage'));

// -- several drives: one notice each ------------------------------------------
list = [drive({ id: 'a', label: 'A', path: '/Volumes/A' }), drive({ id: 'b', label: 'B', path: '/Volumes/B' })];
await prompt.check();
assert.equal(tray.children.length, 2);


// -- unplugged and plugged in again between two looks: asked again -----------
list = [drive({ id: 'r1', label: 'R', path: '/Volumes/R' })];
await prompt.check();
await tray.children[0].find('drive-notice-close').onclick();
assert.equal(tray.children.length, 0);
list = [drive({ id: 'r1', label: 'R', path: '/Volumes/R', pending: false })];
await prompt.check();                       // the server has the answer
list = [drive({ id: 'r1', label: 'R', path: '/Volumes/R', pending: true })];
await prompt.check();                       // out and in within one poll
assert.equal(tray.children.length, 1);

// -- the Import page could not be opened: said, not silence -------------------
const failing = new DrivePrompt({
  json: async (url) => (url === '/api/admin/drives'
    ? { drives: [drive({ id: 'f1' })], image_capture: false, export: null } : {}),
  toast: (text, isError) => calls.push(['toast', text, isError]),
  openImport: async () => { throw new Error('The archive page is not ready.'); },
  openImportPage: () => {},
});
calls.length = 0;
await failing.check();
const failTray = body.all('drive-notices').at(-1);
await failTray.children[0].find('drive-notice-main').onclick();
assert.deepEqual(calls.filter((c) => c[0] === 'toast'), [['toast', 'The archive page is not ready.', true]]);

console.log('drive notice: ok');
