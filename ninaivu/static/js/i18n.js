/**
 * The family app, in the language the person reading it chose.
 *
 * Kept per device rather than per profile, which is the same rule the theme
 * already follows: a household shares profiles across phones and a tablet in
 * the kitchen, and the language somebody wants is a fact about the person
 * holding the device, not about the account. It also means the choice works
 * before anybody has signed in, which is exactly when the sign-in screen
 * needs to be readable.
 *
 * Two rules that keep this honest:
 *
 * **English is the source, not a translation.** The keys are English
 * sentences. A missing Tamil string falls back to the English one rather
 * than showing `gallery.empty.title` to somebody's mother — an untranslated
 * sentence is a small failure, a key is a broken page.
 *
 * **Nothing is translated by guessing.** Only strings that appear in a
 * locale file are replaced. Photograph names, folder names, people's names
 * and anything else that came out of the library are left exactly as they
 * are, because they are not this application's words to change.
 */

const STORE_KEY = 'ninaivu.lang';

/** What can be chosen, and what each is called *in itself*. */
export const LANGUAGES = [
  { code: 'en', name: 'English' },
  { code: 'ta', name: 'தமிழ்' },
];

//: Loaded strings, by code. English is empty because English is the source:
//: every key *is* its English text, so there is nothing to look up.
const loaded = { en: {} };

let current = 'en';
const listeners = new Set();

/** The language this device has chosen, or the one the browser asks for. */
export function preferred() {
  try {
    const saved = localStorage.getItem(STORE_KEY);
    if (saved && LANGUAGES.some((l) => l.code === saved)) return saved;
  } catch { /* private mode */ }
  // `ta`, `ta-IN`, `ta-LK` — the tag before the dash is the language.
  for (const tag of navigator.languages || [navigator.language || '']) {
    const code = String(tag).split('-')[0].toLowerCase();
    if (LANGUAGES.some((l) => l.code === code)) return code;
  }
  return 'en';
}

/**
 * Name a translation key without translating it here.
 *
 * Returns the English unchanged. It is for the English that lives in a table
 * built while a file loads and is shown much later: `t()` at that point runs
 * before any locale has been fetched and would freeze the English for the life
 * of the page, and plain quotes there are invisible to the guard that keeps the
 * locale files honest, which would then call the translation unused.
 *
 * Mark with `key()` where the English is written; call `t()` where it is shown.
 */
export function key(text) {
  return text;
}

export function language() {
  return current;
}

/**
 * One string, in the current language.
 *
 * `key` is the English sentence. `vars` fills `{name}` style placeholders,
 * which is how a translated sentence gets to put the number in a different
 * place from where English puts it.
 */
export function t(key, vars) {
  const table = loaded[current] || {};
  let text = table[key] || key;
  if (vars) {
    for (const [name, value] of Object.entries(vars)) {
      text = text.split(`{${name}}`).join(String(value));
    }
  }
  return text;
}

/** Fetch a locale's strings. English needs none. */
async function load(code) {
  if (loaded[code]) return loaded[code];
  try {
    const response = await fetch(`/static/i18n/${code}.json`, {
      headers: { Accept: 'application/json' },
    });
    loaded[code] = response.ok ? await response.json() : {};
  } catch {
    // A locale that will not load leaves the page in English, which is
    // readable. Failing louder than that would trade a usable page for a
    // broken one.
    loaded[code] = {};
  }
  return loaded[code];
}

//: marker attribute -> the attribute it translates.
const ATTRIBUTES = [
  ['data-i18n-title', 'title'],
  ['data-i18n-label', 'aria-label'],
  ['data-i18n-placeholder', 'placeholder'],
];

//: What `apply()` last wrote into each node, by the attribute it wrote (and
//: `''` for the text). Held weakly, so a node that leaves the page takes its
//: entry with it.
const written = new WeakMap();

/**
 * Write `text` into one slot of `node`, unless the page has taken it over.
 *
 * Much of what is marked up is a placeholder the page later fills in: the
 * library's folder starts as "No folder selected" and becomes the path, the
 * empty gallery's heading becomes whichever reason it is empty. Rewriting
 * those from their markup on every change of language put "No folder
 * selected" back over a library that had one. So a slot is only this
 * function's while it still holds what this function last put there; once
 * anything else has written to it, it belongs to whatever wrote it.
 */
function put(node, slot, now, text, write) {
  let mine = written.get(node);
  if (!mine) written.set(node, (mine = {}));
  if (slot in mine && mine[slot] !== now) return;
  write(text);
  mine[slot] = text;
}

/**
 * Replace the text of everything marked up for translation, under `root`.
 *
 * `data-i18n` is the element's own text. The others are the attributes that
 * are read aloud or hovered, and are just as much part of the page as its
 * text. One marker per attribute rather than a list: the first version took
 * `title:Everyone, including guests` and split it on commas, which is fine
 * until a sentence has a comma in it — and that one does.
 */
export function apply(root = document) {
  for (const node of root.querySelectorAll('[data-i18n]')) {
    const key = node.dataset.i18n;
    if (key) put(node, '', node.textContent, t(key), (text) => { node.textContent = text; });
  }
  for (const [marker, attribute] of ATTRIBUTES) {
    for (const node of root.querySelectorAll(`[${marker}]`)) {
      const key = node.getAttribute(marker);
      if (key) {
        put(node, attribute, node.getAttribute(attribute), t(key),
          (text) => node.setAttribute(attribute, text));
      }
    }
  }
}

/** Change language, redraw, and tell anything that draws its own text. */
export async function use(code, { remember = true } = {}) {
  const wanted = LANGUAGES.some((l) => l.code === code) ? code : 'en';
  await load(wanted);
  current = wanted;
  document.documentElement.lang = wanted;
  if (remember) {
    try { localStorage.setItem(STORE_KEY, wanted); } catch { /* private mode */ }
  }
  apply();
  for (const listener of listeners) {
    try { listener(wanted); } catch { /* one bad listener is not the others' problem */ }
  }
  return wanted;
}

/** Called after every change, for anything that builds its own strings. */
export function onChange(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Set the language up before the first paint anybody will notice. */
export async function start() {
  return use(preferred(), { remember: false });
}
