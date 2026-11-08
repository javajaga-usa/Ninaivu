/**
 * What the pages share: who is signed in, the library as last read, and the
 * settings kept on this device.
 *
 * Moved here from app.js and admin.js unchanged, so the page modules under
 * static/js/pages/ read and change the same objects their page shell does.
 */

/** Settings kept on this device (localStorage, under `mv.`). */
export const store = {
  get(key, fallback) {
    try {
      const raw = localStorage.getItem(`mv.${key}`);
      return raw === null ? fallback : JSON.parse(raw);
    } catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem(`mv.${key}`, JSON.stringify(value)); } catch { /* private mode */ }
  },
};

/** The family app: the signed-in person, the view and filters, and the library. */
export const state = {
  user: null,
  view: 'all',
  filters: { q: '', kinds: [], favorites: false, duplicates: false,
             tag: '', folder: '', camera: '', from: '', to: '', person: 0,
             occasion: 0, album: 0, near: 0, sort: 'date_desc' },
  people: [],
  occasions: [],
  albums: [],
  status: null,
  facets: null,
  semantic: false,
  loading: false,
};

/** The console: the signed-in administrator and the library as the Overview read it. */
export const consoleState = {
  user: null, overview: null, people: [], roles: [], folders: [],
  libraries: [], assignable: [],
};
