/** Behavioral regressions runnable without a server: node tests/action_logic.mjs. */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { Viewer } from '../ninaivu/static/js/viewer.js';
import { api, buildQuery } from '../ninaivu/static/js/api.js';

assert.equal(buildQuery({ is_live: '1' }).get('is_live'), '1');
assert.equal(buildQuery({}).has('is_live'), false);

// Stands in for every element the viewer looks up, the More menu among them,
// which close() now shuts (and asks whether focus was inside it).
const button = { classList: { toggle() {}, add() {}, remove() {} }, setAttribute() {}, getAttribute() {},
  contains: () => false, focus() {} };
// app.js translates what it says; the checks below read the English.
const i18n = { t: (text, values) => text.replace(/\{(\w+)\}/g, (_, key) => values?.[key] ?? '') };
const makeViewer = () => Object.assign(Object.create(Viewer.prototype), {
  item: { id: 1, favorite: false }, root: { hidden: false, querySelector: () => button },
  dispatchEvent(event) { this.lastEvent = event; }, toast(message) { this.error = message; },
});
globalThis.CustomEvent ??= class { constructor(type, options) { this.type = type; Object.assign(this, options); } };

let resolveUpdate;
api.update = () => new Promise(resolve => { resolveUpdate = resolve; });
let viewer = makeViewer();
const first = viewer.item;
const pending = viewer.toggleFavorite();
await viewer.toggleFavorite(); // repeated clicks must not send overlapping writes
viewer.item = { id: 2, favorite: false };
resolveUpdate({});
await pending;
assert.equal(viewer.lastEvent.detail.id, 1);
assert.equal(first.favorite, true);
assert.equal(viewer.item.favorite, false);

api.update = async () => { throw new Error('offline'); };
viewer = makeViewer();
await viewer.toggleFavorite();
assert.equal(viewer.item.favorite, false);
assert.match(viewer.error, /offline/);
assert.equal(viewer.favoritePending, false);
assert.equal(viewer.handleKey({ key: 'f', ctrlKey: true }), false);

viewer.isKiosk = true;
viewer.toggleKiosk = () => { viewer.isKiosk = false; };
assert.equal(viewer.handleKey({ key: 'Escape' }), true);
assert.equal(viewer.root.hidden, false);
globalThis.document = { body: { style: {} } };
viewer.isKiosk = true;
viewer.stage = { innerHTML: 'photo' };
viewer.close();
assert.equal(viewer.isKiosk, false);
assert.equal(viewer.root.hidden, true);

viewer = makeViewer();
viewer.ids = [1];
let resolveItem;
viewer.fetchItem = () => new Promise(resolve => { resolveItem = resolve; });
viewer.renderStage = () => { throw new Error('closed viewer must not render'); };
const loading = viewer.goTo(0);
viewer.root.hidden = true;
resolveItem({ id: 1 });
await loading;

const source = readFileSync(new URL('../ninaivu/static/js/app.js', import.meta.url), 'utf8');
let keyboard;
let galleryActions = 0;
const modal = { hidden: false };
const context = {
  document: { addEventListener(type, handler) { keyboard = handler; },
    querySelector: () => null, querySelectorAll: () => [modal] },
  viewer: { isOpen: true, handleKey() { galleryActions++; }, closeMoreMenu() {} },
  closeMenus: () => false,
  $: () => ({ classList: { contains: () => false, remove() {} }, setAttribute() {} }),
};
vm.runInNewContext(source.slice(source.indexOf('function wireKeyboard()'), source.indexOf('\nlet lastPhase')), context);
context.wireKeyboard();
const event = { key: 'f', target: { matches: () => false, closest: () => null }, preventDefault() {} };
keyboard(event);
assert.equal(galleryActions, 0);

keyboard({ ...event, key: ' ', target: { matches: () => false, closest: () => ({}) } });
assert.equal(galleryActions, 0);
keyboard({ ...event, key: 'Escape' });
assert.equal(modal.hidden, true);
assert.equal(galleryActions, 0);

const handlers = {};
const input = { value: 'https://example.test/share', select() {}, focus() {} };
const messages = [];
const sharing = {
  viewer: { addEventListener() {} }, navigator: {},
  $: selector => selector === '#share-url-text' ? input : { addEventListener(type, handler) { handlers[selector] = handler; } },
  toast: message => messages.push(message), i18n,
};
vm.runInNewContext(source.slice(source.indexOf('let shareTargetItem'), source.indexOf('\nfunction openShareModal')), sharing);
sharing.wireSharing();
await handlers['#share-copy-btn']();
assert.match(messages.pop(), /manually/);
sharing.navigator.clipboard = { writeText: async () => { throw new Error('denied'); } };
await handlers['#share-copy-btn']();
assert.match(messages.pop(), /manually/);
sharing.navigator.clipboard.writeText = async () => {};
await handlers['#share-copy-btn']();
assert.match(messages.pop(), /Link copied/);
console.log('PASS: favorite failures and navigation, modal shortcuts, clipboard outcomes, viewer close and pending loads');

const filterContext = {
  state: { filters: { person: 7, q: 'beach', tag: 'trip', sort: 'date_desc' } },
  searchTimer: 1, clearTimeout() {},
  $: () => ({}), syncChips() {}, reload() {},
};
vm.runInNewContext(source.slice(source.indexOf('function clearFilters()'), source.indexOf('\nfunction syncChips()')), filterContext);
filterContext.clearFilters();
assert.equal(filterContext.state.filters.person, 0);
assert.equal(filterContext.state.filters.q, '');
assert.equal(filterContext.state.filters.sort, 'date_desc');
console.log('PASS: Live Photos query and clearing person/search filters');

const uploadMessages = [];
const uploadNodes = new Map();
const uploadContext = {
  $: selector => {
    if (selector === '#upload-list') return null;
    if (!uploadNodes.has(selector)) uploadNodes.set(selector, { style: {}, value: 'old selection' });
    return uploadNodes.get(selector);
  },
  FormData: class { append() {} },
  fetch: async () => ({ ok: true, json: async () => ({ total: 0, uploaded: [], errors: [{ filename: 'bad.txt', error: 'Unsupported' }] }) }),
  toast: message => uploadMessages.push(message), reload() {}, setTimeout() {}, i18n,
};
vm.runInNewContext(source.slice(source.indexOf('async function handleUpload('), source.indexOf('/* -- Sharing')), uploadContext);
await uploadContext.handleUpload([{ name: 'bad.txt' }]);
assert.match(uploadMessages[0], /Uploaded 0 files.*1 could not/);
assert.match(uploadMessages[1], /Unsupported/);
assert.equal(uploadNodes.get('#upload-file-input').value, '');
console.log('PASS: fully rejected uploads report failure and allow reselection');

// Quick rotate clicks: one save in flight per photograph, and the last click's
// turn is the one that ends up saved — not whichever request finished last.
{
  const sent = [];
  const answers = [];
  api.rotate = (id, rotation) => {
    sent.push([id, rotation]);
    return new Promise(resolve => answers.push(() => resolve({ rotation, rotation_source: 'manual', width: 1, height: 1, thumb_v: `v${rotation}` })));
  };
  const turner = Object.assign(makeViewer(), {
    item: { id: 7, kind: 'picture', rotation: 0 }, canRotate: true, turnsWanted: new Map(),
    transform: { scale: 1, x: 0, y: 0, rotate: 0 }, info: { hidden: true },
    applyTransform() {}, renderChrome() {},
  });
  const firstClick = turner.rotate();
  turner.rotate();
  turner.rotate();
  assert.deepEqual(sent, [[7, 90]], 'clicks during a save must not send overlapping writes');
  answers.shift()();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.deepEqual(sent, [[7, 90], [7, 270]], 'the latest turn is sent once the first save lands');
  answers.shift()();
  await firstClick;
  assert.equal(turner.item.rotation, 270);
  assert.equal(turner.item.thumb_v, 'v270');
  assert.equal(turner.turnsWanted.size, 0);
  console.log('PASS: quick rotate clicks save the last turn, one request at a time');
}
