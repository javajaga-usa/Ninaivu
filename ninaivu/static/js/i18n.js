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

/**
 * "1 item" / "5 items", in the current language. Counts were built with
 * English template strings in a dozen places, so a Tamil gallery said
 * "8 items" under every heading.
 */
export function items(count) {
  const n = Number(count) || 0;
  return n === 1 ? t(ONE_ITEM) : t(MANY_ITEMS, { count: n.toLocaleString(locale()) });
}

// Written as i18n.key() calls so the guard that keeps the locale files honest
// (tests/test_language.py) sees them.
const i18n = { key };
const ONE_ITEM = i18n.key('1 item');
const MANY_ITEMS = i18n.key('{count} items');

/** A role's name — "Family member" — in the current language. */
export function role(label) {
  return t(ROLES[label] || label || '');
}
const ROLES = {
  Guest: i18n.key('Guest'),
  'Family member': i18n.key('Family member'),
  Admin: i18n.key('Administrator'),
};

/** The locale for dates and numbers: the chosen language — and, for English,
 *  the browser's own English, so an American household reads 4/3 as April
 *  and a British one reads 3/4 as April. It used to be British everywhere. */
export function locale() {
  if (current === 'ta') return 'ta-IN';
  try {
    for (const tag of navigator.languages || [navigator.language || '']) {
      if (!/^en(-|$)/i.test(String(tag))) continue;
      // Only a tag Intl accepts: some systems report "en-US@posix", and a
      // date formatted with that throws instead of printing a date.
      try { return Intl.getCanonicalLocales(String(tag))[0]; } catch { /* next */ }
    }
  } catch { /* no navigator: a test, or a worker */ }
  return 'en-GB';
}

/**
 * A date range a person would write, in the current language — "12–14 May
 * 2023" or its Tamil. The server writes occasion titles in English; the page
 * has their dates and writes them itself.
 */
export function dateRange(startSeconds, endSeconds) {
  const a = new Date(startSeconds * 1000);
  const b = new Date(endSeconds * 1000);
  const opts = { day: 'numeric', month: 'long', year: 'numeric', timeZone: 'UTC' };
  try {
    const format = new Intl.DateTimeFormat(locale(), opts);
    if (a.toISOString().slice(0, 10) === b.toISOString().slice(0, 10)) return format.format(a);
    return format.formatRange ? format.formatRange(a, b) : `${format.format(a)} – ${format.format(b)}`;
  } catch {
    return '';
  }
}

/**
 * A folder whose path is a date — "2025/05/11", "2025-05-11", "2025/05",
 * "2025" — as a date a person would write, in the current language: "11 May
 * 2025", "May 2025". Anything else, including a date that does not exist
 * ("2025/02/30"), is null, and the folder keeps its own name.
 */
export function folderDate(path) {
  const match = /^(\d{4})(?:[/-](\d{1,2})(?:[/-](\d{1,2}))?)?$/
    .exec(String(path || '').trim().replace(/[\\/]+$/, ''));
  if (!match) return null;
  const year = Number(match[1]);
  const month = match[2] ? Number(match[2]) : 0;
  const day = match[3] ? Number(match[3]) : 0;
  if (year < 1800 || year > 2200) return null;
  if (match[2] && (month < 1 || month > 12)) return null;
  const date = new Date(Date.UTC(year, Math.max(0, month - 1), day || 1));
  if (day && date.getUTCDate() !== day) return null;
  const opts = { timeZone: 'UTC', year: 'numeric' };
  if (month) opts.month = 'long';
  if (day) opts.day = 'numeric';
  try {
    return new Intl.DateTimeFormat(locale(), opts).format(date);
  } catch {
    return null;
  }
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

/*
 * A sentence with markup in it, translated whole.
 *
 * The console's help is full of sentences like "It moves into the
 * <strong>_deleted</strong> folder". Marked up a run of text at a time, each
 * run was its own key — "It moves into the", "folder at the top…" — and Tamil,
 * which puts the verb last and the folder first, came out as English word
 * order in Tamil words. So such a sentence is marked once, on the element
 * that holds it:
 *
 *   data-i18n-rich="It moves into the &lt;strong&gt;_deleted&lt;/strong&gt; folder…"
 *
 * The key is the element's English, whitespace collapsed, with its markup
 * written as bare tags — `<strong>`, `<em>`, `<b>`, `<i>`, `<code>`, `<kbd>`,
 * `<span>`, and `<a>` with no href — and `&lt;`/`&gt;` for a `<` or `>` that is
 * text. The translation carries the same tags wherever its own grammar wants
 * them.
 *
 * **Markup in a translation is never markup.** It is read by `parseRich()`,
 * which knows those tags and nothing else: no attributes, no entities beyond
 * the two, no innerHTML. Anything else in it and the translation is not used.
 *
 * **The page's elements are the page's.** The first time a sentence is met,
 * while it is still the template's English, the elements in it are noted by
 * tag in the order they come. A translation's first `<a>` is then that first
 * link — the same element, moved, so its href, its id, its class and anything
 * a script attached to it stay exactly as they were. A translation with a
 * different number of any tag than the English has cannot be matched up, and
 * the English is shown instead.
 *
 * **An empty tag keeps what is in it.** `<code></code>` in a key is a slot the
 * words do not reach — an icon, a `{{image}}` placeholder, a name the page
 * fills in — and an element whose words a script has since replaced is left
 * with them, by the same rule `put()` keeps for everything else.
 *
 * English is the key read the same way, so switching back puts the English
 * sentence together from the same elements.
 */

//: The markup a whole sentence may carry.
const RICH_TAGS = new Set(['strong', 'em', 'b', 'i', 'code', 'kbd', 'a', 'span']);

//: Each rich sentence's own elements, by tag, in page order — noted once,
//: from the English, and null for a sentence holding markup outside the set.
const richParts = new WeakMap();

//: The child nodes `apply()` last gave each rich sentence. Anything else there
//: means the page has rewritten it, and it is the page's from then on.
const richBuilt = new WeakMap();

/**
 * A rich string as a tree of `text` and `{ tag, children }`, or null if it is
 * anything but text and the tags above, properly nested.
 */
function parseRich(source) {
  const top = { tag: '', children: [] };
  const open = [top];
  const text = (run) => {
    if (run.includes('<')) return false;
    if (run) open[open.length - 1].children.push(run.replace(/&lt;/g, '<').replace(/&gt;/g, '>'));
    return true;
  };
  let at = 0;
  for (const match of String(source).matchAll(/<(\/?)([a-z]+)>/g)) {
    if (!text(source.slice(at, match.index))) return null;
    at = match.index + match[0].length;
    const [, closing, tag] = match;
    if (!RICH_TAGS.has(tag)) return null;
    if (closing) {
      if (open.length < 2 || open[open.length - 1].tag !== tag) return null;
      open.pop();
    } else {
      const node = { tag, children: [] };
      open[open.length - 1].children.push(node);
      open.push(node);
    }
  }
  if (!text(source.slice(at)) || open.length !== 1) return null;
  return top.children;
}

/** The elements a rich sentence was written with, by tag — or null. */
function partsOf(node) {
  const parts = {};
  const walk = (parent) => {
    for (const child of parent.children) {
      const tag = child.localName;
      if (!RICH_TAGS.has(tag)) return false;
      (parts[tag] ||= []).push(child);
      // An element's own markup (the share icon in its span) is its content,
      // which a translation does not reach; only the words' tags are counted.
      if (![...child.children].some((c) => !RICH_TAGS.has(c.localName)) && !walk(child)) return false;
    }
    return true;
  };
  return walk(node) ? parts : null;
}

/**
 * The nodes for one rich string, made of `parts`' elements; null when the
 * string does not parse or its tags do not match the English's one for one.
 */
function buildRich(source, parts) {
  const tree = parseRich(source);
  if (!tree) return null;
  const counts = {};
  const tally = (nodes) => {
    for (const n of nodes) {
      if (typeof n === 'string') continue;
      counts[n.tag] = (counts[n.tag] || 0) + 1;
      tally(n.children);
    }
  };
  tally(tree);
  for (const tag of new Set([...Object.keys(counts), ...Object.keys(parts)])) {
    if ((counts[tag] || 0) !== (parts[tag] || []).length) return null;
  }
  const next = {};
  const make = (nodes) => nodes.map((n) => {
    if (typeof n === 'string') return document.createTextNode(n);
    const element = parts[n.tag][next[n.tag] = (next[n.tag] ?? -1) + 1];
    if (n.children.length) {
      const inner = make(n.children);
      const words = inner.map((c) => c.textContent).join('');
      put(element, '', element.textContent, words, () => element.replaceChildren(...inner));
    }
    return element;
  });
  return make(tree);
}

/** Translate one rich sentence — see above. */
function applyRich(node, key) {
  if (!richParts.has(node)) richParts.set(node, partsOf(node));
  const parts = richParts.get(node);
  if (!parts) return;
  const built = richBuilt.get(node);
  if (built && (built.length !== node.childNodes.length
      || built.some((child, i) => child !== node.childNodes[i]))) return;
  // A translation that nests its tags differently from the English can fail
  // part-way through building; that sentence then stays in English rather
  // than taking the rest of the page's switch down with it.
  const attempt = (text) => { try { return buildRich(text, parts); } catch { return null; } };
  const nodes = attempt(t(key)) || attempt(key);
  if (!nodes) return;
  node.replaceChildren(...nodes);
  richBuilt.set(node, nodes);
}

/**
 * Replace the text of everything marked up for translation, under `root`.
 *
 * `data-i18n` is the element's own text. The others are the attributes that
 * are read aloud or hovered, and are just as much part of the page as its
 * text. One marker per attribute rather than a list: the first version took
 * `title:Everyone, including guests` and split it on commas, which is fine
 * until a sentence has a comma in it — and that one does.
 *
 * `data-i18n-rich` is a whole sentence with its markup; see `applyRich()`.
 */
export function apply(root = document) {
  for (const node of root.querySelectorAll('[data-i18n]')) {
    const key = node.dataset.i18n;
    if (key) put(node, '', node.textContent, t(key), (text) => { node.textContent = text; });
  }
  for (const node of root.querySelectorAll('[data-i18n-rich]')) {
    const key = node.getAttribute('data-i18n-rich');
    if (key) {
      try { applyRich(node, key); } catch { /* one sentence, never the page */ }
    }
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
