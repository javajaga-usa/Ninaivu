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
  prepend(child) { child.parent = this; this.children.unshift(child); return child; }
  after(node) {
    const siblings = this.parent.children;
    node.parent = this.parent;
    siblings.splice(siblings.indexOf(this) + 1, 0, node);
  }
  byId(id) {
    if (this.id === id) return this;
    for (const child of this.children) {
      const found = child.byId(id);
      if (found) return found;
    }
    return null;
  }
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
  querySelector: (selector) => (selector.startsWith('#') ? body.byId(selector.slice(1)) : null),
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
assert.equal(labels[0], 'Import media from the device', 'importing is the first thing offered');
assert.ok(notice.all('btn')[0].className.includes('primary'));
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
assert.equal(phone.all('btn').some((b) => b.textContent === 'Import media from the device'), false,
  'a phone the Mac cannot read is imported through Image Capture, not from here');
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

// -- a library folder found at a new place: asked, then a restart ------------
let moved = [{ old: '/Volumes/SSD/Archive', found: ['/Volumes/SSD 1/Archive'], confirmed: null }];
const movedCalls = [];
const mover = new DrivePrompt({
  json: async (url, options = {}) => {
    movedCalls.push([url, options.method || 'GET', options.body]);
    if (url === '/api/admin/drives') {
      return { drives: [], image_capture: false, export: null, moved_libraries: moved };
    }
    return {};
  },
  toast: (text) => movedCalls.push(['toast', text]),
  openImport: async () => {},
  openImportPage: () => {},
});
await mover.check();
const movedTray = body.all('drive-notices').at(-1);
const movedNotice = movedTray.children[0];
assert.ok(movedNotice.className.includes('drive-notice-moved'));
assert.equal(movedNotice.find('drive-notice-name').textContent, '/Volumes/SSD/Archive');
const use = movedNotice.all('btn').find((b) => b.textContent === 'Use /Volumes/SSD 1/Archive');
assert.ok(use, 'each folder found is offered');
moved = [{ ...moved[0], confirmed: '/Volumes/SSD 1/Archive' }];
await use.onclick();
assert.ok(movedCalls.some((c) => c[0] === '/api/admin/library/relocation' && c[1] === 'POST'
  && c[2].new === '/Volumes/SSD 1/Archive'));
assert.equal(movedTray.children.length, 1, 'one notice, drawn again for its new state');
const restart = movedTray.children[0].all('btn').find((b) => b.textContent === 'Restart now');
assert.ok(restart, 'once chosen, the restart that makes the move is offered');
await restart.onclick();
assert.ok(movedCalls.some((c) => c[0] === '/api/admin/server/restart'));
moved = [];
await mover.check();
assert.equal(movedTray.children.length, 0, 'moved: the notice goes');

// -- the Import page lists every drive plugged in now ------------------------
const page = new Node('div');
const devices = new Node('div');
devices.id = 'ar-devices';
page.appendChild(devices);
body.appendChild(page);
let plugged = [
  drive({ id: 'lib', label: 'SSD', path: '/Volumes/SSD', holds: 'library', pending: false }),
  drive({ id: 'red', label: 'J Red', path: '/Volumes/J Red', pending: false,
    sources: ['/Volumes/J Red/EXT4'] }),
  drive({ id: 'new', label: 'CARD', path: '/Volumes/CARD', pending: false, quiet: true }),
];
const pluggedCalls = [];
const lister = new DrivePrompt({
  json: async (url, options = {}) => {
    pluggedCalls.push([url, options.method || 'GET', options.body]);
    if (url === '/api/admin/drives') return { drives: plugged, image_capture: false, export: null };
    if (url === '/api/admin/drives/eject') return { message: 'You can unplug the drive now.' };
    return {};
  },
  toast: (text) => pluggedCalls.push(['toast', text]),
  openImport: async (path) => {
    pluggedCalls.push(['openImport', path]);
    return { state: 'added', folders: [] };
  },
  openImportPage: () => {},
  browseDrive: async (d) => pluggedCalls.push(['browse', d.path]),
});
await lister.check();
const box = body.byId('ar-plugged');
assert.ok(box, 'the drives are listed under the phones');
const cards = box.all('ar-device-card');
assert.equal(cards.length, 3);
const buttonsOf = (card) => card.all('btn').map((b) => b.textContent);
assert.deepEqual(buttonsOf(cards[0]), [], 'the library drive is shown, with nothing to press');
assert.match(cards[0].find('hint').textContent, /Holds the photo library/);
assert.deepEqual(buttonsOf(cards[1]), ['Choose a folder on it…', 'Eject'],
  'a drive with sources on it is not offered whole again');
assert.match(cards[1].find('hint').textContent, /Sources on it: \/Volumes\/J Red\/EXT4/);
assert.deepEqual(buttonsOf(cards[2]), ['Use the whole drive', 'Choose a folder on it…', 'Eject', 'Ask again']);
await cards[2].all('btn')[1].onclick();
assert.ok(pluggedCalls.some((c) => c[0] === 'browse' && c[1] === '/Volumes/CARD'));
await cards[2].all('btn')[0].onclick();
assert.ok(pluggedCalls.some((c) => c[0] === 'openImport' && c[1] === '/Volumes/CARD'));
await cards[2].all('btn')[2].onclick();
assert.ok(pluggedCalls.some((c) => c[0] === '/api/admin/drives/eject' && c[2].id === 'new'));
assert.ok(pluggedCalls.some((c) => c[0] === 'toast' && c[1] === 'You can unplug the drive now.'));

// Importing from it: no Eject while it is being read.
plugged = [drive({ id: 'red', label: 'J Red', path: '/Volumes/J Red', pending: false,
  sources: ['/Volumes/J Red/EXT4'], in_use: 'import' })];
await lister.check();
const busy = body.byId('ar-plugged').all('ar-device-card')[0];
assert.equal(buttonsOf(busy).includes('Eject'), false);
assert.match(busy.find('hint').textContent, /Importing from it now/);

// Folders on the drive already sources: the notice says so, not "added".
pluggedCalls.length = 0;
const inside = new DrivePrompt({
  json: async () => ({}),
  toast: (text) => pluggedCalls.push(['toast', text]),
  openImport: async () => ({ state: 'inside', folders: ['/Volumes/J Red/EXT4'] }),
  openImportPage: () => {},
});
await inside.importFrom(drive({ id: 'red' }));
assert.match(pluggedCalls.find((c) => c[0] === 'toast')[1], /sources already: \/Volumes\/J Red\/EXT4/);

console.log('drive notice: ok');
