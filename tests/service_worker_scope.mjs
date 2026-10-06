import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const handlers = {};
const intercepted = [];
const context = vm.createContext({
  URL, URLSearchParams, Response,
  fetch: (_request, options) => {
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
  staleWhileRevalidate = () => 'static';
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
assert.deepEqual(request('/api/thumb/1', { mode: 'cors' }), ['network']);
assert.deepEqual(request('/api/thumb/1', { range: true }), []);
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
