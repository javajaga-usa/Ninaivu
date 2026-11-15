// Albums kept for offline (static/sw.js, "ALBUMS KEPT FOR OFFLINE"): kept for
// one person, used only when the server cannot be reached, wiped when
// somebody else is on the device or on sign-out.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const stores = new Map();
function store(name) {
  if (!stores.has(name)) stores.set(name, new Map());
  const map = stores.get(name);
  const key = (r) => new URL(typeof r === 'string' ? r : r.url, 'http://ninaivu.test').href;
  return {
    match: async (r) => { const hit = map.get(key(r)); return hit ? hit.clone() : undefined; },
    put: async (r, response) => { map.set(key(r), response); },
    delete: async (r) => map.delete(key(r)),
    keys: async () => [...map.keys()].map((url) => ({ url })),
  };
}
const caches = {
  open: async (name) => store(name),
  delete: async (name) => stores.delete(name),
  keys: async () => [...stores.keys()],
};

let online = true;
const asked = [];
const handlers = {};
const context = vm.createContext({
  URL, URLSearchParams, Response, Date, Map, Set, setTimeout, clearTimeout, caches,
  fetch: async (request) => {
    const url = typeof request === 'string' ? request : request.url;
    asked.push(url);
    if (!online) throw new TypeError('Failed to fetch');
    const body = `picture ${url}`;
    const response = new Response(body, { status: 200 });
    Object.defineProperty(response, 'type', { value: 'basic' });
    return response;
  },
  self: {
    location: { origin: 'http://ninaivu.test', search: '' },
    addEventListener(name, handler) { handlers[name] = handler; },
  },
});
vm.runInContext(readFileSync(new URL('../ninaivu/static/sw.js', import.meta.url), 'utf8'), context);

async function message(data, ports) {
  const waits = [];
  handlers.message({ data, ports, waitUntil: (p) => waits.push(p), source: null });
  await Promise.all(waits);
}

async function list() {
  return JSON.parse(JSON.stringify(await listRaw()));
}
async function listRaw() {
  let answer;
  await message({ type: 'ninaivu:offline-list' }, [{ postMessage: (m) => { answer = m; } }]);
  return answer.albums;
}

async function thumb(path) {
  let answer;
  handlers.fetch({
    request: { url: `http://ninaivu.test${path}`, method: 'GET', mode: 'cors', headers: { has: () => false } },
    respondWith: (p) => { answer = p; },
  });
  return answer;
}

const items = [{ id: 1, a: 150, v: '9r0' }, { id: 2, a: 100, v: '3r0' }];
await message({ type: 'ninaivu:offline-keep', user: 5, album: { id: 7, name: 'Ooty' }, items, size: 640 });
let kept = await list();
assert.equal(kept.length, 1);
assert.equal(kept[0].name, 'Ooty');
assert.deepEqual(kept[0].items.map((i) => i.url), ['/api/thumb/1?s=640&v=9r0', '/api/thumb/2?s=640&v=3r0']);
assert.ok(asked.includes('http://ninaivu.test/api/thumb/1?s=640&v=9r0'), 'each thumbnail fetched once to keep');
console.log('PASS: an album opened is kept with its thumbnails');

// Online, a thumbnail is the server's; offline, the kept copy.
online = false;
const response = await thumb('/api/thumb/1?s=640&v=9r0');
assert.equal(await response.text(), 'picture http://ninaivu.test/api/thumb/1?s=640&v=9r0');
const other = await thumb('/api/thumb/1?s=256&v=9r0');
assert.ok(other, 'another size of a kept photograph falls back to the kept one');
await assert.rejects(() => thumb('/api/thumb/99?s=256'), 'a photograph not kept is not invented');
online = true;
console.log('PASS: kept thumbnails are used only when the server cannot be reached');

// Somebody else on the device: nothing of the last person's is left.
await message({ type: 'ninaivu:offline-viewer', user: 6 });
assert.deepEqual(await list(), []);
await message({ type: 'ninaivu:offline-keep', user: 6, album: { id: 8, name: 'Pongal' }, items, size: 640 });
await message({ type: 'ninaivu:offline-viewer', user: null });
assert.deepEqual(await list(), [], 'a guest, or the household switched it off');
console.log('PASS: another person, or a guest, wipes what was kept');

// Only the six most recently opened are kept.
for (let n = 1; n <= 8; n += 1) {
  await message({ type: 'ninaivu:offline-keep', user: 5, album: { id: n, name: `A${n}` }, items: [{ id: 100 + n, a: 100 }], size: 256 });
}
kept = await list();
assert.deepEqual(kept.map((a) => a.name), ['A8', 'A7', 'A6', 'A5', 'A4', 'A3']);
const cached = await (await caches.open('ninaivu-offline-v1')).keys();
assert.ok(!cached.some((k) => k.url.includes('/api/thumb/101')), 'an album let go takes its thumbnails with it');
console.log('PASS: six albums at most, the oldest let go with its thumbnails');

// Signing out forgets everything, the kept albums too.
await message('ninaivu:forget');
assert.deepEqual(await list(), []);
console.log('PASS: signing out wipes the kept albums');
