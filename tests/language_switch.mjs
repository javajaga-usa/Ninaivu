/**
 * Changing language leaves what the page wrote alone.
 *
 * The library's folder is marked up as "No folder selected" so that it reads
 * properly before the status has arrived. Once it had, every change of
 * language rewrote it from that markup: the sidebar said "No folder selected"
 * over a library that had one, until something happened to ask the server
 * again.
 *
 * Runs the real `i18n.js` against a page just big enough to show it, with the
 * real Tamil strings.
 *
 *   node tests/language_switch.mjs   (no server needed)
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const STATIC = new URL('../ninaivu/static/', import.meta.url);

/** An element: its text, and its attributes by name. */
function element(attributes, text) {
  const attrs = { ...attributes };
  return {
    textContent: text,
    dataset: { get i18n() { return attrs['data-i18n']; } },
    getAttribute: (name) => (name in attrs ? attrs[name] : null),
    setAttribute: (name, value) => { attrs[name] = String(value); },
    hasAttribute: (name) => name in attrs,
  };
}

const path = element({ 'data-i18n': 'No folder selected' }, 'No folder selected');
const signIn = element({ 'data-i18n': 'Sign in' }, 'Sign in');
const empty = element({ 'data-i18n': 'No folder selected' }, 'No folder selected');
const switchBtn = element({ 'data-i18n-title': 'Switch profile', title: 'Switch profile' }, '');
const nodes = [path, signIn, empty, switchBtn];

globalThis.document = {
  documentElement: {},
  querySelectorAll(selector) {
    const name = selector.match(/^\[([\w-]+)\]$/)[1];
    return nodes.filter((node) => node.hasAttribute(name));
  },
};
globalThis.localStorage = { getItem: () => null, setItem() {} };
Object.defineProperty(globalThis, 'navigator', {
  value: { languages: ['en-GB'] }, configurable: true,
});
globalThis.fetch = async (url) => {
  const file = new URL(url.replace(/^\/static\//, ''), STATIC);
  return { ok: true, json: async () => JSON.parse(readFileSync(file, 'utf8')) };
};

const source = readFileSync(new URL('js/i18n.js', STATIC), 'utf8');
const i18n = await import('data:text/javascript,' + encodeURIComponent(source));
const ta = JSON.parse(readFileSync(new URL('i18n/ta.json', STATIC), 'utf8'));

await i18n.start();
assert.equal(path.textContent, 'No folder selected');

// What refreshStatus() does once the server has answered.
path.textContent = 'D:\\Family Photos';
switchBtn.setAttribute('title', 'Switch profile (Dad)');
// Somewhere else, the page writes the same placeholder for itself.
empty.textContent = i18n.t('No folder selected');

await i18n.use('ta');
assert.equal(path.textContent, 'D:\\Family Photos', 'the library path was replaced');
assert.equal(switchBtn.getAttribute('title'), 'Switch profile (Dad)',
  'an attribute the page set was replaced');
assert.equal(signIn.textContent, ta['Sign in'], 'untouched markup is still translated');
assert.equal(empty.textContent, ta['No folder selected'],
  'a placeholder still showing its own words follows the language');

await i18n.use('en');
assert.equal(path.textContent, 'D:\\Family Photos', 'the library path was replaced');
assert.equal(signIn.textContent, 'Sign in');
assert.equal(empty.textContent, 'No folder selected');

// The page can hand a slot back by writing the placeholder into it again.
path.textContent = i18n.t('No folder selected');
await i18n.use('ta');
assert.equal(path.textContent, ta['No folder selected']);

console.log('language switch: ok');
