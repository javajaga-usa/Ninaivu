import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const handlers = {};
const intercepted = [];
const thumbFetches = [];
const context = vm.createContext({
  URL, URLSearchParams, Response, Date, Map,
  // Signing out also empties the stored shell; nothing is stored here.
  caches: { keys: async () => [], delete: async () => true },
  fetch: (request, options) => {
    if (String(request.url).includes('/api/thumb/')) {
      // A thumbnail is revalidated, or within minutes of the last answer
      // taken from the browser's own copy (sw.js, thumbnail()).
      assert.ok(['no-cache', 'force-cache'].includes(options.cache), options.cache);
      thumbFetches.push(options.cache);
      return Promise.resolve({ ok: true, status: 200 });
    }
    // Nothing else under /api/ is stored at all.
    assert.equal(options.cache, 'no-store');
    return 'network';
  },
  self: {
    location: { origin: 'http://ninaivu.test' },
    addEventListener(name, handler) { handlers[name] = handler; },
  },
});
vm.runInContext(readFileSync(new URL('../ninaivu/static/sw.js', import.meta.url), 'utf8'), context);
vm.runInContext(`
  networkFirstNavigation = () => 'navigation';
  cacheFirst = () => 'thumbnail';
  staticFile = () => 'static';
`, context);

function request(path, { mode = 'navigate', accept = 'text/html', range = false } = {}) {
  intercepted.length = 0;
  handlers.fetch({
    request: { url: 'http://ninaivu.test' + path, method: 'GET', mode,
      headers: { has: key => key === 'range' && range, get: () => accept } },
    respondWith(value) { intercepted.push(value); },
  });
  return [...intercepted];
}

assert.deepEqual(request('/'), ['navigation']);
for (const path of ['/api/file/1', '/api/download/1', '/api/assets', '/api/me',
                    '/share/token', '/api/share/token/file/1', '/cert',
                    // The offline screen sends people here to see what the browser
                    // really gets; answered from cache it would hide a certificate warning.
                    '/readyz']) {
  const expected = path.startsWith('/api/') ? ['network'] : [];
  assert.deepEqual(request(path), expected, `${path} must not be cached as navigation`);
  assert.deepEqual(request(path, { mode: 'cors' }), expected, `${path} with HTML Accept must stay on network`);
}
assert.deepEqual(request('/static/js/app.js', { mode: 'cors' }), ['static']);
assert.equal(request('/api/thumb/1', { mode: 'cors' }).length, 1);
assert.deepEqual(request('/api/thumb/1', { range: true }), []);

// A thumbnail the server answered for a moment ago is not asked about again
// (scrolling back made one round trip per tile); after a sign-out it is.
{
  const ask = async (path) => {
    thumbFetches.length = 0;
    await request(path, { mode: 'cors' })[0];
    return thumbFetches[0];
  };
  assert.equal(await ask('/api/thumb/7?s=256&v=1'), 'no-cache', 'first time: the server is asked');
  assert.equal(await ask('/api/thumb/7?s=256&v=1'), 'force-cache', 'again at once: the browser\'s copy');
  assert.equal(await ask('/api/thumb/8?s=256&v=1'), 'no-cache', 'another picture: asked');
  handlers.message({ data: 'ninaivu:forget', waitUntil() {} });
  assert.equal(await ask('/api/thumb/7?s=256&v=1'), 'no-cache', 'after signing out: asked again');
}
console.log('PASS: a thumbnail answered for a moment ago is not asked about again, until sign-out');
console.log('PASS: offline navigation only caches the home shell; private URLs stay on network');

// The console's copy: the offline page for its home page, nothing stored.
const consoleHandlers = {};
const consoleContext = vm.createContext({
  URL, URLSearchParams, Response,
  fetch: () => Promise.reject(new TypeError('Failed to fetch')),
  caches: { open() { throw new Error('the console copy must not open a cache'); } },
  self: {
    location: { origin: 'http://ninaivu.test', search: '?console=1' },
    addEventListener(name, handler) { consoleHandlers[name] = handler; },
  },
});
vm.runInContext(readFileSync(new URL('../ninaivu/static/sw.js', import.meta.url), 'utf8'), consoleContext);
function consoleRequest(path, { mode = 'navigate' } = {}) {
  const answered = [];
  consoleHandlers.fetch({
    request: { url: 'http://ninaivu.test' + path, method: 'GET', mode,
      headers: { has: () => false, get: () => 'text/html' } },
    respondWith(value) { answered.push(value); },
  });
  return answered;
}
const [home] = consoleRequest('/');
const page = await home;
assert.equal(page.status, 503);
assert.match(await page.text(), /Ninaivu is Offline/);
for (const path of ['/static/js/admin.js', '/api/admin/status', '/api/thumb/1']) {
  assert.deepEqual(consoleRequest(path, { mode: 'cors' }), [], `${path} must be left to the browser on the console`);
}
console.log('PASS: the console worker shows the offline page and stores nothing');

// The shell running offline: once the home page has had to come from the
// cache, a module is taken from the cache at once, not after waiting out the
// same unreachable server (a computer that is off does not refuse; it is
// silent until a timeout).
{
  const stored = new Map([['http://ninaivu.test/', 'shell'],
    ['http://ninaivu.test/static/js/grid.js', 'old grid']]);
  const cache = {
    match: async (request) => {
      const body = stored.get(typeof request === 'string' ? `http://ninaivu.test${request}` : request.url);
      return body === undefined ? undefined : new Response(body);
    },
    put: async () => {},
  };
  let silent = false;
  const awayHandlers = {};
  const away = vm.createContext({
    URL, URLSearchParams, Response,
    fetch: () => (silent ? new Promise(() => {}) : Promise.reject(new TypeError('Failed to fetch'))),
    caches: { open: async () => cache },
    self: {
      location: { origin: 'http://ninaivu.test' },
      addEventListener(name, handler) { awayHandlers[name] = handler; },
    },
  });
  vm.runInContext(readFileSync(new URL('../ninaivu/static/sw.js', import.meta.url), 'utf8'), away);
  const ask = (path, mode) => {
    const answered = [];
    awayHandlers.fetch({
      request: { url: 'http://ninaivu.test' + path, method: 'GET', mode,
        headers: { has: () => false, get: () => '' } },
      respondWith(value) { answered.push(value); },
    });
    return answered[0];
  };
  assert.equal(await (await ask('/', 'navigate')).text(), 'shell');
  silent = true;
  const module = await Promise.race([ask('/static/js/grid.js', 'cors'),
    new Promise((resolve) => { setTimeout(() => resolve(null), 500); })]);
  assert.ok(module, 'an offline shell waited on the server for its modules');
  assert.equal(await module.text(), 'old grid');
  console.log('PASS: offline, the shell takes its modules from the cache at once');
}
