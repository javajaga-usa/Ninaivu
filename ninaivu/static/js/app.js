/** Application shell: state, filters, chrome, and everything wired together. */

import { api, onUnauthorized, sessionRestored, setAnonymousViewer, thumbUrl } from './api.js';
import { fetchActivity, renderActivity } from './activity.js';
import * as i18n from './i18n.js';
import { Grid, mergeSegments } from './grid.js';
import { Viewer } from './viewer.js';
import { MODES, scrubberTicks, sectionAt } from './layout.js';
import { accountsApi, avatarNode, Gate, ProfileSheet } from './accounts.js';
import { enterPressesTheButton } from './enter-key.js';
import { PhoneBackup } from './phone-backup.js';
import { ScreenLock } from './lock.js';
import { initPalette } from './palette.js';

const $ = (sel) => document.querySelector(sel);
const store = {
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

const state = {
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

let grid;
let viewer;
let searchController = null;
/**
 * Where the gallery's result set continues. A library too large for one
 * request arrives in pieces: `next` is the offset of the next piece, or null
 * when everything is held. Replaced wholesale by every reload, so a piece that
 * arrives for a query no longer on screen can tell it is stale.
 */
let paging = { next: null, filters: null, signal: null, loading: false };
/** Items per piece as the grid is scrolled; about a quarter of a second each. */
const GALLERY_PAGE = 25000;
/** The most one request may ask for (the server's own cap). */
const GALLERY_MAX_PAGE = 200000;
let searchTimer = null;
let reconnectTimer = null;
let isCheckingConnection = false;
let serverIsOffline = false;
let hasStarted = false;

/* ========================================================================
   Boot
   ======================================================================== */

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  store.set('theme', theme);
  try { localStorage.setItem('ninaivu.theme', JSON.stringify(theme)); } catch { /* private */ }
}

window.applyTheme = applyTheme;
window.addEventListener('storage', (event) => {
  if (event.key === 'mv.theme' || event.key === 'ninaivu.theme') {
    try {
      const next = JSON.parse(event.newValue);
      if (next && ['system', 'light', 'dark'].includes(next)) {
        document.documentElement.dataset.theme = next;
        store.set('theme', next);
      }
    } catch {}
  }
});

let gate;
let screenLock;
let profileSheet;

async function init() {
  // Enter in a field does what the button beside it does, everywhere.
  enterPressesTheButton();
  const initialTheme = store.get('theme', null)
    || (() => {
      try {
        const raw = localStorage.getItem('ninaivu.theme');
        return raw ? JSON.parse(raw) : null;
      } catch { return null; }
    })()
    || 'system';
  applyTheme(initialTheme);

  // Before the first paint anybody will notice: the page is marked up in
  // English and translated in place, so doing this after the gallery drew
  // would show a flicker of one language turning into another.
  await i18n.start();

  grid = new Grid($('#scroller'), $('#grid'));
  grid.setMode(store.get('layout', 'justified'));
  grid.setZoom(store.get('zoom', 2));
  $('#zoom').value = String(grid.zoom);
  showLayout(grid.mode);

  viewer = new Viewer($('#viewer'));
  viewer.toast = toast;
  // Covers the gallery after fifteen minutes with nobody there. A slideshow
  // or the photo frame is somebody watching, and does not lock.
  screenLock = new ScreenLock({
    isBusy: () => Boolean(viewer?.slideshow || viewer?.isKiosk),
    onUnlocked: () => { reload(); refreshStatus().catch(() => {}); },
  });
  // Rotating rewrites the thumbnails the whole household sees, so it is an
  // admin's to do — the same reasoning as deleting. For everyone else the
  // button stays a look at this one photograph, for this one viewing.
  // A rotation changes the photograph's shape, which changes how the grid
  // lays its cell out — so the gallery is reloaded rather than repainted.
  // Rotations are rare and done one at a time; a reload is the honest,
  // simple answer and it cannot leave a stale cell behind.
  viewer.onChange = () => { reload(); };

  wireChrome();
  wireGrid();
  wireViewer();
  wireKeyboard();
  initPalette();

  gate = new Gate($('#gate'), {
    toast,
    onSignedIn: async (user) => {
      sessionRestored();
      forgetCachedShell();
      // Signing in can change the name: this person may keep their own.
      try { applyHomeName(await accountsApi.state()); } catch { /* keep the house name */ }
      start(user);
    },
  });

  // The same thing that happens to the console happens here: a profile is
  // disabled or signed out from the console, and the tablet on the kitchen
  // wall keeps showing a gallery it can no longer load. Send it back to the
  // picker rather than leaving somebody tapping a dead screen.
  onUnauthorized(async () => {
    let authState = {};
    try {
      authState = await accountsApi.state();
    } catch {
      authState = {};
    }
    screenLock?.stop();
    bootDone();
    gate.show(authState, authState.setup_required ? 'setup' : 'picker');
  });
  profileSheet = new ProfileSheet($('#profile-sheet'), {
    toast,
    onChange: async (user) => {
      state.user = user;
      renderIdentity();
      try { applyHomeName(await accountsApi.state()); } catch { /* leave it */ }
    },
  });

  wireAccounts();

  // Handy from the console, and what the performance suite measures against.
  window.__mv = { grid, viewer, state, reload, api };

  let auth;
  try {
    auth = await accountsApi.state();
  } catch {
    setServerOffline();
    return;
  }

  await finishStartup(auth);
}

/** Everything that depends on knowing who the viewer is. */
/* What this viewer calls the home.

   The server has already resolved it — their own name if they set one, the
   household's otherwise — so there is no fallback chain duplicated here to
   drift out of step with the server's. */
function applyHomeName(authState) {
  const given = (authState && (authState.home_name || authState.house_name)) || '';
  // "Ninaivu" is what the server answers when nobody has named the home: that
  // is the product's own name, not somebody's choice, so it is written in the
  // language the page is in (நினைவு), not shown as the English the server sent.
  state.homeNameGiven = Boolean(given) && given !== 'Ninaivu';
  const name = state.homeNameGiven ? given : i18n.t('Ninaivu');
  // The canonical form, which renderLibraryName compares against.
  state.homeName = state.homeNameGiven ? given : 'Ninaivu';

  // textContent, never innerHTML: this is something a family member typed.
  const brand = document.querySelector('.brand span');
  if (brand) brand.textContent = name;
  document.title = name;

  const frameName = document.querySelector('#kiosk-home');
  if (frameName) frameName.textContent = name;
  renderLibraryName();

  // The profile sheet shows the household name as its placeholder, so an
  // empty box reads as "using the household name" rather than "unset".
  if (profileSheet) profileSheet.houseName = (authState && authState.house_name) || '';
}

// Nobody has named this home, so what the tab and the frame say is Ninaivu's
// own name, and it is written in the language the page is in. (A name somebody
// typed is left exactly as typed.) `state.homeName` is left alone on purpose:
// it is how the library box tells "named" from "not named".
i18n.onChange(() => {
  if (state.homeNameGiven) return;
  const own = i18n.t('Ninaivu');
  document.title = own;
  const frameName = document.querySelector('#kiosk-home');
  if (frameName) frameName.textContent = own;
});

/* Install the offline shell.
 *
 * It caches the app itself and the thumbnails this viewer has already been
 * served — nothing else. Originals, listings and anything from the console go
 * to the network every time, because those differ per person and a shared
 * tablet must not hand the next person the last one's gallery.
 *
 * Failure here is silent on purpose: an unsupported browser, a private
 * window, or a page served over plain HTTP on a non-localhost address all
 * refuse registration, and none of them is a reason to bother anybody.
 */
function registerServiceWorker() {
  if (!('serviceWorker' in navigator)) return;
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}

/* Signing out wipes what was cached, so the next person to pick up the tablet
   starts clean rather than seeing a stranger's thumbnails. */
function forgetCachedShell() {
  try {
    navigator.serviceWorker?.controller?.postMessage('ninaivu:forget');
  } catch { /* no worker, nothing to forget */ }
}

/** Opened on the tailnet address (Tailscale), away from home. */
function onTailnet() {
  return location.hostname.endsWith('.ts.net');
}

/* The search box's hint, the longest that fits. A phone showed "Descri", and
   the desktop cut its example off mid-word; semantic search's hint was also
   English whatever the language. */
function setSearchHint() {
  const input = $('#search');
  if (!input) return;
  const hints = state.semanticSearch
    ? [i18n.t('Describe what you remember'), i18n.t('Search')]
    // Without a search model the box searches names, folders, text read from
    // pictures and dates — "last summer" is read by a calendar, no model. A
    // place name works only once place names are on, so it is not promised.
    : [i18n.t('Search names and dates — try “last summer”'), i18n.t('Search — try “last summer”'), i18n.t('Search')];
  const style = getComputedStyle(input);
  const room = input.clientWidth - parseFloat(style.paddingLeft || 0)
    - parseFloat(style.paddingRight || 0);
  setSearchHint.canvas ||= document.createElement('canvas');
  const measure = setSearchHint.canvas.getContext('2d');
  measure.font = `${style.fontWeight} ${style.fontSize} ${style.fontFamily}`;
  input.placeholder = hints.find((hint) => measure.measureText(hint).width <= room)
    || hints[hints.length - 1];
}

function watchSearchHint() {
  const input = $('#search');
  if (!input) return;
  setSearchHint();
  if (window.ResizeObserver) new ResizeObserver(() => setSearchHint()).observe(input);
  i18n.onChange(() => {
    setSearchHint();
    // What the page built itself, as opposed to what apply() redraws.
    renderOccasions();
    renderAlbums();
    grid?.relabel?.();
    if (grid?.layout) buildScrubber();
    renderLibraryName();
    renderFolders();
    syncChips();
    // "6 items · 398.1 KB" is built here too, so it would keep the old language.
    if (state.lastCounts) renderCounts(state.lastCounts);
  });
}

/* Android's browsers offer to install the app themselves, through this event;
   iOS never does, so it is told how (below). */
function setupAndroidInstallPrompt() {
  window.addEventListener('beforeinstallprompt', (event) => {
    event.preventDefault();
    let dismissed = false;
    try { dismissed = Boolean(localStorage.getItem('ninaivu:install-dismissed')); } catch { /* private window */ }
    const banner = document.getElementById('ios-install-banner');
    const install = document.getElementById('install-btn');
    if (dismissed || !banner || !install) return;
    const title = document.getElementById('ios-install-title');
    const desc = document.getElementById('ios-install-desc');
    if (title) title.textContent = i18n.t('Install Ninaivu');
    if (desc) desc.textContent = i18n.t('Open it from your home screen, like an app.');
    install.hidden = false;
    install.onclick = async () => {
      banner.hidden = true;
      event.prompt();
      await event.userChoice.catch(() => null);
    };
    document.getElementById('ios-install-close')?.addEventListener('click', () => {
      banner.hidden = true;
      try { localStorage.setItem('ninaivu:install-dismissed', '1'); } catch { /* private window */ }
    });
    banner.hidden = false;
  });
}

function setupIOSInstallPrompt() {
  // A desktop-class iPad says "MacIntel" and has a touchscreen. So does an
  // Android phone emulated on a Mac, which is not an iPad.
  const isIPad = /iPad/.test(navigator.userAgent) ||
    (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1
      && !/Android/.test(navigator.userAgent));
  const isIPhone = /iPhone|iPod/.test(navigator.userAgent);
  const isIOS = (isIPhone || isIPad) && !window.MSStream;
  const isStandalone = window.navigator.standalone === true || window.matchMedia('(display-mode: standalone)').matches;
  if (!isIOS || isStandalone) return;

  const dismissed = localStorage.getItem('ninaivu:ios-install-dismissed');
  if (dismissed) return;

  const banner = document.getElementById('ios-install-banner');
  const title = document.getElementById('ios-install-title');
  const desc = document.getElementById('ios-install-desc');
  const closeBtn = document.getElementById('ios-install-close');
  if (!banner) return;

  if (isIPad) {
    if (title) title.textContent = i18n.t('Install Ninaivu on your iPad');
    if (desc) {
      desc.innerHTML = 'Tap <svg class="ios-share-glyph" viewBox="0 0 24 24"><path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8M16 6l-4-4-4 4M12 2v13"/></svg> Share in Safari’s top toolbar, then select <strong>Add to Home Screen</strong> [+]';
    }
  } else {
    if (title) title.textContent = i18n.t('Install Ninaivu on your iPhone');
    if (desc) {
      desc.innerHTML = 'Tap <svg class="ios-share-glyph" viewBox="0 0 24 24"><path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8M16 6l-4-4-4 4M12 2v13"/></svg> Share below, then select <strong>Add to Home Screen</strong> [+]';
    }
  }

  setTimeout(() => {
    banner.hidden = false;
  }, 2500);

  closeBtn?.addEventListener('click', () => {
    banner.hidden = true;
    localStorage.setItem('ninaivu:ios-install-dismissed', '1');
  });
}

async function finishStartup(auth) {
  hasStarted = true;
  applyHomeName(auth);
  registerServiceWorker();
  setupIOSInstallPrompt();
  setupAndroidInstallPrompt();
  watchSearchHint();
  // The certificate is for the house's own addresses. On the tailnet one the
  // browser already trusts a public certificate, and installing Ninaivu's there
  // only made a duplicate in the keychain.
  const certHint = $('#help-cert');
  if (certHint && onTailnet()) certHint.hidden = true;
  setupNetworkStatusListeners();

  // First run has to happen on the admin console.
  if (auth.setup_required) {
    bootDone();
    gate.show(auth, 'setup');
    return;
  }
  // Nobody signed in yet: ask who's watching before loading anything.
  if (!auth.signed_in) {
    bootDone();
    gate.show(auth, 'picker');
    return;
  }
  await start(auth.user);
}

function isNetworkFailure(error) {
  if (!error) return false;
  if (error instanceof TypeError) return true;
  const msg = String(error.message || '').toLowerCase();
  return msg.includes('failed to fetch') ||
         msg.includes('networkerror') ||
         msg.includes('load failed') ||
         msg.includes('cannot reach') ||
         msg.includes('connection refused') ||
         msg.includes('504');
}

function setServerOffline(reason = i18n.t('Cannot reach the Ninaivu server.')) {
  // Whatever else is true, the cover comes off: a boot overlay left over a
  // page that is telling somebody the server is unreachable hides the one
  // thing they need to read.
  bootDone();
  serverIsOffline = true;

  const banner = $('#offline-banner');
  if (banner) banner.hidden = false;
  const bannerText = $('#offline-banner-text');
  if (bannerText) bannerText.textContent = `Can’t reach Ninaivu at ${location.host}. Reconnecting automatically…`;

  if (!grid?.layout?.cells?.length) {
    const empty = $('#empty');
    if (empty) {
      empty.classList.add('offline');
      empty.hidden = false;
    }
    const skeleton = $('#skeleton');
    if (skeleton) skeleton.classList.remove('on');
    const title = $('#empty-title');
    // Worded as what is known, not as a diagnosis: a stopped server and a
    // certificate this browser does not trust fail identically from here, and
    // the offline copy of this page hides the browser's own certificate warning.
    if (title) title.textContent = i18n.t('Can’t reach Ninaivu');
    const text = $('#empty-text');
    // On the tailnet address the likeliest cause is neither: Tailscale is off on
    // this device, and a certificate is not the answer there (it is a public one).
    const away = onTailnet();
    if (text) text.textContent = away
      ? `This device can’t reach Ninaivu at ${location.host}. Away from home it is reached through Tailscale, so check that Tailscale is on and connected on this device.`
      : `This browser can’t reach Ninaivu at ${location.host}. Either the Ninaivu server is stopped, or this browser doesn’t trust Ninaivu’s HTTPS certificate.`;
    const hint = $('#empty-hint');
    if (hint) {
      hint.hidden = false;
      hint.textContent = away
        ? i18n.t('Open the Tailscale app, connect, then retry. If Tailscale is connected, the Ninaivu server may be stopped.')
        : i18n.t('Check what the browser sees: a security warning means the certificate needs installing on this device; a “can’t reach this page” error means the server needs starting.');
    }
    const check = $('#empty-check');
    if (check) check.hidden = false;
    const action = $('#empty-action');
    if (action) {
      action.hidden = false;
      action.textContent = i18n.t('Retry Connection');
      action.onclick = () => checkConnection(true);
    }
  }

  const badge = $('#ai-badge');
  if (badge) {
    badge.textContent = i18n.t('Server offline');
    badge.className = 'ai-badge offline';
    badge.hidden = false;
  }

  const rootPath = $('#root-path');
  if (rootPath && (rootPath.textContent === i18n.t('No folder selected') || !state.status?.has_library)) {
    rootPath.textContent = i18n.t('Server offline');
  }

  if (!reconnectTimer) {
    reconnectTimer = setInterval(() => checkConnection(false), 3000);
  }
}

async function checkConnection(manual = false) {
  if (isCheckingConnection) return;
  isCheckingConnection = true;

  const retryBtn = $('#offline-retry-btn');
  const emptyAction = $('#empty-action');
  if (manual) {
    if (retryBtn) retryBtn.textContent = i18n.t('Checking…');
    if (emptyAction && $('#empty')?.classList.contains('offline')) emptyAction.textContent = i18n.t('Checking…');
  }

  try {
    const auth = await accountsApi.state();
    if (reconnectTimer) {
      clearInterval(reconnectTimer);
      reconnectTimer = null;
    }
    serverIsOffline = false;

    const banner = $('#offline-banner');
    if (banner) banner.hidden = true;
    const empty = $('#empty');
    if (empty?.classList.contains('offline')) {
      empty.classList.remove('offline');
      empty.hidden = true;
    }
    const hint = $('#empty-hint');
    if (hint) hint.hidden = true;
    const check = $('#empty-check');
    if (check) check.hidden = true;

    toast(i18n.t('Connected to Ninaivu server.'));

    if (!hasStarted) {
      await finishStartup(auth);
    } else {
      applyHomeName(auth);
      await refreshStatus();
      await Promise.all([reload(), refreshFacets(), loadMemories()]);
    }
  } catch {
    if (manual) {
      toast(i18n.t('Server is still unreachable.'), true);
    }
  } finally {
    isCheckingConnection = false;
    if (retryBtn) retryBtn.textContent = i18n.t('Retry now');
    if (emptyAction && $('#empty')?.classList.contains('offline')) {
      emptyAction.textContent = i18n.t('Retry Connection');
    }
  }
}

function setupNetworkStatusListeners() {
  window.addEventListener('offline', () => {
    setServerOffline(i18n.t('You are offline.'));
  });
  window.addEventListener('online', () => {
    checkConnection(false);
  });
}

/* The first load ---------------------------------------------------------
 *
 * Real steps, not a bar that fills on a timer. Each one moves when the thing
 * it names has actually happened, so the percentage is a fact rather than an
 * animation — and if a step never arrives, the bar stops where it stopped and
 * says what it is waiting for, which is the truth.
 *
 * The weights are roughly what each step costs on a large library, so the bar
 * does not race to 90% and then sit there.
 */
// The third item is a key, not a sentence: `i18n.t` is called where the line
// is put on screen, because this array is built as the file loads and the
// locale has not been fetched yet — translating here would freeze all four in
// English, and these are the first words anybody sees.
const BOOT_STEPS = [
  ['signed-in', 15, i18n.key('Checking who you are')],
  ['status', 30, i18n.key('Opening your library')],
  ['photographs', 85, i18n.key('Finding your photographs')],
  ['painted', 100, i18n.key('Almost there')],
];
let bootAt = 0;

function bootStep(name) {
  const box = $('#boot');
  if (!box || box.hidden) return;
  const step = BOOT_STEPS.find((s) => s[0] === name);
  if (!step || step[1] <= bootAt) return;
  bootAt = step[1];
  const fill = $('#boot-fill');
  if (fill) fill.style.width = `${bootAt}%`;
  const percent = $('#boot-percent');
  if (percent) percent.textContent = `${bootAt}%`;
  const text = $('#boot-step');
  // The next step's name, because what it says it is doing should be the
  // thing it is waiting on, not the thing it has just finished.
  const next = BOOT_STEPS[BOOT_STEPS.indexOf(step) + 1];
  if (text && next) text.textContent = i18n.t(next[2]);
}

// Whatever goes wrong, the cover comes off within this long. Every path that
// ends in something worth looking at calls bootDone itself; this is for the
// path nobody thought of, because an overlay that outlives its page is a
// worse bug than the empty page it replaced.
setTimeout(() => bootDone(), 30000);

function bootDone() {
  const box = $('#boot');
  if (!box || box.hidden) return;
  bootStep('painted');
  box.classList.add('done');
  // Left in the DOM for the length of the fade, then taken out so it can
  // never swallow a click.
  setTimeout(() => { box.hidden = true; }, 400);
}

async function start(user) {
  state.user = user || { role: 'guest', anonymous: true, can: {} };
  // The language follows the person: what they chose once, on any device,
  // wins over what this browser asks for. A device with no choice saved
  // for this person keeps the browser's language until they pick one.
  if (state.user.language && state.user.language !== i18n.language()
      && i18n.LANGUAGES.some((l) => l.code === state.user.language)) {
    await i18n.use(state.user.language);
    showLanguage();
  }
  // Must be set before anything below fires a request: a guest hits a
  // family-gated 401 on the very first round of calls (see api.js).
  setAnonymousViewer(state.user.anonymous === true);
  renderIdentity();
  // Before anything is loaded, so a page opened on a locked session is
  // covered at once rather than after the gallery has drawn.
  await screenLock?.start(state.user);
  bootStep('signed-in');
  try {
    await refreshStatus();
  } catch (error) {
    console.error('status failed', error);
  }
  bootStep('status');
  // Permissions and the gallery must render even if one call above failed,
  // otherwise a single bad response leaves an empty, feature-less page.
  applyPermissions();
  // The counts are deliberately not awaited: they are the only call here
  // whose cost grows with the library, and nothing on screen needs them to
  // show a photograph. They fill the sidebar in when they arrive.
  refreshCounts();
  await Promise.all([reload(), refreshFacets(), loadMemories()]);
  bootStep('photographs');
  bootDone();

  if (state.user.must_change) {
    profileSheet.open(state.user);
    toast(i18n.t('Choose your own password to finish setting up.'));
  }
}

/* ========================================================================
   Identity and permissions
   ======================================================================== */

function renderIdentity() {
  const user = state.user || {};
  const button = $('#profile-btn');
  button.innerHTML = '';
  const anonymous = user.anonymous !== false;

  // Shown to everybody: the menu behind it holds the language, the theme
  // and help, which somebody who has not signed in needs as much as anyone.
  button.hidden = false;
  $('#signin-btn').hidden = !anonymous;
  // Its sibling. Both ship hidden in the markup; this one had no line, so the
  // control existed, carried a handler, and could never be clicked.
  $('#switch-btn').hidden = anonymous;
  $('#profile-open').hidden = anonymous;

  const head = $('#profile-menu-head');
  head.textContent = '';
  head.hidden = anonymous;
  if (!anonymous) {
    button.appendChild(avatarNode(user, 30));
    button.title = `${user.name} · ${i18n.role(user.role_label)}`;
    button.setAttribute('aria-label', `${i18n.t('Profile and settings')} — ${user.name}`);
    const name = document.createElement('strong');
    name.textContent = user.name;
    const role = document.createElement('span');
    role.textContent = i18n.role(user.role_label);
    head.append(avatarNode(user, 36), name, role);
  } else {
    // Nobody to draw, so the outline of a person where the face would be.
    button.innerHTML = '<svg class="profile-anon" viewBox="0 0 24 24" aria-hidden="true">'
      + '<circle cx="12" cy="8" r="4"/><path d="M4.5 20a7.5 7.5 0 0 1 15 0"/></svg>';
    button.title = i18n.t('Profile and settings');
    button.setAttribute('aria-label', i18n.t('Profile and settings'));
  }
}

/**
 * Show only what this role can actually use. The server refuses the rest
 * regardless; this keeps the interface from offering dead ends.
 */
function applyPermissions() {
  const can = state.user?.can || {};
  document.body.dataset.role = state.user?.role || 'guest';

  document.querySelectorAll('.admin-only').forEach((node) => {
    node.hidden = !can.set_visibility;
  });
  grid.showVisibility = !!can.set_visibility;
  // Library and people management live on the admin console; from here we
  // only offer the link to it.
  const consoleLink = $('#console-link');
  if (consoleLink) {
    consoleLink.hidden = !can.manage_library;
    // Always give it a real destination. Falling back to 3000 is right far
    // more often than leaving "#", which is a link that looks live and does
    // nothing at all.
    const adminPort = state.status?.admin_port || 3000;
    consoleLink.href = `${window.location.protocol}//${window.location.hostname}:${adminPort}`;
  }

  // Favourites and downloads are family-and-above.
  $('[data-view="favorites"]').hidden = !can.favorite;
  $('#sel-fav').hidden = !can.favorite;
  $('#sel-unfav').hidden = !can.favorite;
  $('#sel-download').hidden = !can.download;
  $('#sel-export').hidden = !can.download;
  // Deleting is the one management action that belongs in the gallery,
  // because choosing what to delete means looking at it. It reads the same
  // capability block as every other control here — it used to reach into
  // state.status instead, which is filled by a separate request, so a slow
  // or failed /api/status left an admin with no delete button at all.
  $('#sel-delete').hidden = !can.delete_media;
  // Household profiles can correct shared photographs. Guests keep the
  // button as a temporary look at this one photograph, for this one viewing.
  viewer.canRotate = !!can.rotate;
  viewer.canDownload = !!can.download;
  const rotate = $('#v-rotate');
  rotate.title = viewer.canRotate ? i18n.t('Rotate and save (R)') : i18n.t('Rotate for this view only (R)');
  rotate.setAttribute('aria-label', rotate.title);
  $('#v-fav').hidden = !can.favorite;
  $('#v-download').hidden = !can.download;
  // Same capability as the selection bar's Delete button (see the comment
  // above), so the two never disagree about who gets to delete.
  const viewerDelete = $('#v-delete');
  if (viewerDelete) viewerDelete.hidden = !can.delete_media;
  $('[data-view="duplicates"]').hidden = !can.download;

  // Guests get a plain gallery: no AI badge, no scan strip, no jargon.
  $('#ai-badge').hidden = !can.manage_library;
  if (!can.manage_library) {
    $('#scan-strip').hidden = true;
    const tagsTitle = $('#tags-block .side-title');
    if (tagsTitle) tagsTitle.textContent = i18n.t('Explore');
  }
}

/* ========================================================================
   Data
   ======================================================================== */

/**
 * Say when a library folder is not plugged in.
 *
 * Photographs on an unplugged drive stay in the index, so the gallery is
 * neither empty nor wrong — it is short, which looks exactly like photographs
 * having gone missing. Ninaivu knew which folder was away and said nothing;
 * this is the saying.
 */
function showAwayLibraries(status) {
  const banner = $('#away-banner');
  if (!banner) return;
  const away = Number(status?.libraries_away || 0);
  banner.hidden = !away;
  if (!away) return;
  const paths = status.libraries_away_paths || [];
  const text = away === 1
    ? i18n.t('One of your library folders is not connected. Its photographs are still listed but cannot be opened until it is back.')
    : i18n.t('{count} of your library folders are not connected. Their photographs are still listed but cannot be opened until they are back.',
      { count: away });
  // The paths only come back for an admin, and are the useful part when they do.
  $('#away-banner-text').textContent = paths.length
    ? `${text} (${paths.join(', ')})` : text;
}


async function refreshStatus() {
  countsAskedAt = Date.now();
  try {
    state.status = await api.status();
  } catch (err) {
    if (isNetworkFailure(err)) {
      setServerOffline();
      return;
    }
    toast(i18n.t('Cannot reach the server.'), true);
    return;
  }
  showAwayLibraries(state.status);
  const { root, root_label: label, stats, ai, user } = state.status;
  if (user) {
    state.user = user;
    renderIdentity();
  }

  renderLibraryName();
  renderCounts(stats);

  const badge = $('#ai-badge');
  if (!ai || ai.engine === 'none' || ai.engine === 'off') {
    badge.textContent = i18n.t('AI: off');
    badge.className = 'ai-badge';
  } else if (ai.semantic) {
    badge.textContent = `AI: semantic search (${ai.model.split('/')[0]})`;
    badge.className = 'ai-badge semantic';
    state.semanticSearch = true;
    setSearchHint();
  } else {
    badge.textContent = i18n.t('AI: basic tagging');
    badge.className = 'ai-badge ready';
  }
  // The strip is the activity poll's to draw — /api/status carries the scan
  // but knows nothing about the other jobs, and half a list is worse than a
  // list that arrives a moment later. This starts that loop, or restarts it
  // after the tab was hidden or the server went away and came back.
  kickActivity();
}

/* The library card's name.

   The server sends a folder's name ("library") or, to an admin, its whole
   path, which is a fact about a disk rather than a name for anybody's
   photographs. The card says what the home is called when somebody has named
   it, and otherwise the folder's own name written as a name ("Library"); the
   path itself stays in the tooltip. */
function renderLibraryName() {
  const box = $('#root-path');
  if (!box || !state.status) return;
  const { root, root_label: label } = state.status;
  const raw = label || root || '';
  // "Ninaivu" is what the server answers when nobody has named the home.
  const named = state.homeName && state.homeName !== 'Ninaivu' ? state.homeName : '';
  box.textContent = raw ? (named || libraryName(raw)) : i18n.t('No folder selected');
  box.title = root || label || '';
  box.classList.toggle('is-name', Boolean(raw));
}

/** "library" -> "Library", "/srv/family_photos" -> "Family Photos"; the two
 *  labels the server writes as sentences are translated rather than cased. */
function libraryName(raw) {
  const several = /^(\d+) library folders$/.exec(raw);
  if (several) return i18n.t('{count} library folders', { count: several[1] });
  if (raw === 'Your library') return i18n.t('Your library');
  const last = raw.replace(/[\\/]+$/, '').split(/[\\/]/).pop() || raw;
  return last.replace(/[_]+/g, ' ').replace(/(^|\s)(\p{Ll})/gu,
    (_, space, letter) => space + letter.toUpperCase());
}

/* The sidebar's numbers.
 *
 * They arrive after the photographs now, because counting a library is the
 * one piece of work here that grows with it: on 196,174 items with a cold
 * cache it took twenty-three seconds, and the gallery used to wait for all
 * of it before asking for a single picture. Nothing here is needed to draw
 * one, so it is fetched alongside and filled in when it lands.
 */
function renderCounts(stats) {
  if (!stats || typeof stats.count !== 'number') return;
  state.lastCounts = stats;
  $('#root-meta').textContent =
    `${i18n.items(stats.count)} · ${humanBytes(stats.bytes)}`;
  $('#count-all').textContent = stats.count.toLocaleString();
  $('#count-fav').textContent = stats.favorites || '';
  $('#count-pic').textContent = stats.pictures || '';
  $('#count-vid').textContent = stats.videos || '';
  // The Live Photos row has been in the sidebar for some time with a place
  // for a number that nothing ever filled in, so it always read as empty.
  $('#count-live').textContent = stats.live || '';
  // A row with nothing behind it is a promise the library cannot keep yet.
  // Live Photos appears once there is one; On This Day, once a year has
  // passed — both come back by themselves when the library grows into them.
  const live = $('#nav-live');
  if (live && !live.classList.contains('active')) live.hidden = !stats.live;
  $('#count-aud').textContent = stats.audio || '';
  $('#count-dup').textContent = stats.duplicate_groups || '';
  $('#count-hidden').textContent = stats.hidden || '';
  $('#count-public').textContent = stats.public || '';
}

async function refreshCounts() {
  try {
    const body = await api.counts();
    if (body?.stats) {
      state.status = { ...(state.status || {}), stats: body.stats };
      renderCounts(body.stats);
    }
  } catch { /* the numbers are a nicety; the photographs are not */ }
}

async function refreshFacets() {
  if (!state.status?.has_library) return;
  try {
    state.facets = await api.facets();
  } catch { return; }
  renderTagCloud();
  renderYears();
  renderFolders();
  loadPeople();
  loadOccasions();
  loadAlbums();
}

function currentFilters() {
  const filters = { ...state.filters };
  if (state.view === 'favorites') filters.favorites = true;
  if (state.view === 'pictures') filters.kinds = ['picture'];
  if (state.view === 'videos') filters.kinds = ['video'];
  if (state.view === 'audio') filters.kinds = ['audio'];
  if (state.view === 'duplicates') filters.duplicates = true;
  if (state.view === 'hidden') filters.visibility = 'hidden';
  if (state.view === 'public') filters.visibility = 'public';
  if (state.view === 'live') filters.is_live = '1';
  return filters;
}

/**
 * `resetScroll: true` is for callers whose change means the *query* is
 * different — a search, a filter, a sort order, a different view — where
 * whatever the grid was scrolled to almost certainly is not in the new
 * results at all. Left false (the default), the grid keeps its scroll
 * position, which is what an in-place refresh wants: a bulk edit, a
 * background rescan, a viewer mutation syncing back. See Grid.setData().
 */
async function reload({ resetScroll = false } = {}) {
  if (!state.status?.has_library) {
    showEmpty(
      i18n.t('No library folder yet'),
      state.user?.can?.manage_library
        ? i18n.t('Choose a folder and Ninaivu will index it in the background.')
        : i18n.t('An admin has not set up the library yet.'),
    );
    return;
  }
  setLoading(true);
  searchController?.abort();
  searchController = new AbortController();
  const signal = searchController.signal;
  const filters = currentFilters();
  // An in-place refresh must hand back as much as was already held, or the
  // grid shrinks under a person scrolled deep into it and loses their place.
  const held = resetScroll ? 0 : grid.layout.cells.length;
  paging = { next: null, filters, signal, loading: false };

  try {
    const data = await api.segments(filters, signal, {
      limit: Math.min(GALLERY_MAX_PAGE, Math.max(GALLERY_PAGE, held)),
    });
    const segments = data.segments;
    let { next_offset: next } = data;
    while (next != null && next < held) {
      const more = await api.segments(filters, signal, {
        limit: Math.min(GALLERY_MAX_PAGE, held - next), offset: next,
      });
      mergeSegments(segments, more.segments);
      next = more.next_offset;
    }
    state.semantic = data.semantic;
    $('#search-mode').hidden = !data.semantic;
    paging.next = next;
    grid.setData(segments, { resetScroll });
    buildScrubber();
    showTruncation({ ...data, next_offset: next });

    if (!data.total && state.user?.anonymous && !anyFilter()) {
      // A guest, with nothing marked public: there is no filter to clear, and
      // "This filter has no items yet" sent them looking for one.
      showEmpty(i18n.t('Nothing shared with guests yet'),
        i18n.t('Sign in to see the family’s photographs.'), { signIn: true });
    } else if (!data.total) {
      showEmpty(
        state.filters.q ? i18n.t('No matches') : i18n.t('Nothing to show'),
        state.filters.q
          ? `Nothing matched “${state.filters.q}”. Try fewer words, or a description like “red car at night”.`
          : i18n.t('This filter has no items yet.'),
      );
    } else {
      $('#empty').hidden = true;
    }
  } catch (error) {
    if (error.name !== 'AbortError') {
      if (isNetworkFailure(error)) {
        setServerOffline();
      } else {
        toast(error.message || i18n.t('Could not load media'), true);
      }
    }
  } finally {
    setLoading(false);
  }
  renderFilterBar();
  loadDuplicatesIfNeeded();
  loadMoreIfNear();
}

/** Fetch the next piece once the scroll is within a few screens of the end. */
async function loadMoreIfNear() {
  const page = paging;
  if (page.next == null || page.loading || !page.signal || page.signal.aborted) return;
  const view = grid.scroller.clientHeight || 1;
  const remaining = grid.layout.height - (grid.scroller.scrollTop + view);
  if (remaining > view * 3) return;

  page.loading = true;
  try {
    const data = await api.segments(page.filters, page.signal, {
      limit: GALLERY_PAGE, offset: page.next,
    });
    if (page !== paging) return;           // a reload replaced this result set
    page.next = data.next_offset;
    grid.appendData(data.segments);
    showTruncation(data);
  } catch (error) {
    if (error.name === 'AbortError' || page !== paging) return;
    if (isNetworkFailure(error)) setServerOffline();
    else toast(error.message || i18n.t('Could not load more media'), true);
    page.next = null;                      // stop retrying on every scroll frame
    return;
  } finally {
    page.loading = false;
  }
  // A short piece may still leave the screen unfilled.
  if (page === paging) loadMoreIfNear();
}

// The grid lays out at most one request's worth of items. A library larger
// than that used to end the scroll with no sign anything was missing, which on
// a million photographs reads exactly like losing the older nine-tenths.
function showTruncation(data) {
  const notice = $('#truncated-notice');
  if (!notice) return;
  // A result set that continues is not cut short — it is still arriving.
  if (!data.truncated || data.next_offset != null) {
    notice.hidden = true;
    return;
  }
  const which = { date_desc: i18n.key('Showing the newest {shown} of {total}. Pick a year, a folder or a search to see the rest.'),
    date_asc: i18n.key('Showing the oldest {shown} of {total}. Pick a year, a folder or a search to see the rest.') }[state.filters.sort || 'date_desc']
    || i18n.key('Showing the first {shown} of {total}. Pick a year, a folder or a search to see the rest.');
  notice.textContent = i18n.t(which, {
    shown: Number(data.returned).toLocaleString(i18n.locale()), total: i18n.items(data.total) });
  notice.hidden = false;
}

function setLoading(on) {
  state.loading = on;
  $('#skeleton').classList.toggle('on', on && !grid.layout.cells.length);
  if (on && !grid.layout.cells.length) {
    $('#skeleton').innerHTML = '<i></i>'.repeat(18);
    if (!serverIsOffline) $('#empty').hidden = true;
  }
}

/** Whether anything narrows the gallery: search, a chip, a kind, favourites. */
function anyFilter() {
  const f = state.filters;
  return Boolean(f.q || f.tag || f.folder || f.camera || f.from || f.to || f.person
    || f.occasion || f.album || f.near || f.favorites || f.duplicates || f.kinds?.length);
}

function showEmpty(title, text, { signIn = false } = {}) {
  const empty = $('#empty');
  empty.classList.remove('offline');
  empty.hidden = false;
  $('#empty-title').textContent = title;
  $('#empty-text').textContent = text;
  const action = $('#empty-action');
  const needsFolder = !state.status?.has_library;
  const canFix = state.user?.can?.manage_library;
  if (signIn) {
    action.hidden = false;
    action.textContent = i18n.t('Sign in');
    action.onclick = () => $('#signin-btn').click();
  } else if (needsFolder) {
    // Setting the library up happens on the console — send them there.
    action.hidden = !canFix;
    action.textContent = i18n.t('Open the admin console');
    action.onclick = () => {
      const port = state.status?.admin_port || 3000;
      window.open(`${window.location.protocol}//${window.location.hostname}:${port}`, '_blank');
    };
  } else {
    action.hidden = false;
    action.textContent = i18n.t('Clear filters');
    action.onclick = clearFilters;
  }
}

/* ========================================================================
   Chrome
   ======================================================================== */

function wireChrome() {
  $('#sidebar-toggle').onclick = () => {
    const shell = document.querySelector('.shell');
    const button = $('#sidebar-toggle');
    // Two different classes carry "open" depending on layout — mobile draws
    // the sidebar as an overlay that defaults to closed, desktop draws it
    // inline and defaults to open — so the two branches invert what the
    // toggled class means. aria-expanded has to follow whichever one just
    // fired, not the other.
    if (window.innerWidth <= 900) {
      const expanded = shell.classList.toggle('mobile-open');
      button.setAttribute('aria-expanded', String(expanded));
    } else {
      const collapsed = shell.classList.toggle('collapsed');
      button.setAttribute('aria-expanded', String(!collapsed));
    }
  };

  document.querySelector('.shell')?.addEventListener('click', (event) => {
    const shell = document.querySelector('.shell');
    if (!shell?.classList.contains('mobile-open')) return;
    if (event.target.closest('.sidebar') || event.target.closest('#sidebar-toggle')) return;
    shell.classList.remove('mobile-open');
    $('#sidebar-toggle')?.setAttribute('aria-expanded', 'false');
  });

  $('#theme-btn').onclick = cycleTheme;
  // One button, two languages: a picker would be more than this needs. If a
  // third is ever added this becomes a menu, and the label already says which
  // one you are about to get.
  const langButton = $('#lang-btn');
  if (langButton) langButton.onclick = async () => {
    const codes = i18n.LANGUAGES.map((l) => l.code);
    const next = codes[(codes.indexOf(i18n.language()) + 1) % codes.length];
    await i18n.use(next);
    showLanguage();
    rememberLanguage(next);
  };
  showLanguage();
  document.addEventListener('ninaivu:language', (event) => {
    showLanguage();
    rememberLanguage(event.detail);
  });
  $('#help-btn').onclick = () => ($('#help-modal').hidden = false);
  $('#offline-retry-btn')?.addEventListener('click', () => checkConnection(true));

  // The topbar i18n.t("More") menu: everything that doesn't fit next to search and
  // profile/sign-out on a narrow screen (see the responsive CSS). Inert
  // above that width — #more-btn stays hidden there, so this never fires.
  const moreBtn = $('#more-btn');
  const moreMenu = $('#topbar-more');
  moreBtn.onclick = (event) => {
    event.stopPropagation();
    const open = moreMenu.classList.toggle('open');
    moreBtn.setAttribute('aria-expanded', String(open));
  };
  // A button inside the menu is a one-shot action (upload, map, a layout
  // choice, theme, help) — close the menu once it's picked rather than
  // leaving it sitting open over the gallery. The zoom slider isn't a
  // <button>, so dragging it doesn't trigger this.
  moreMenu.addEventListener('click', (event) => {
    if (event.target.closest('button')) {
      moreMenu.classList.remove('open');
      moreBtn.setAttribute('aria-expanded', 'false');
    }
  });
  document.addEventListener('click', (event) => {
    if (!moreMenu.classList.contains('open')) return;
    if (moreMenu.contains(event.target) || event.target === moreBtn) return;
    moreMenu.classList.remove('open');
    moreBtn.setAttribute('aria-expanded', 'false');
  });
  $('#help-close').onclick = () => ($('#help-modal').hidden = true);

  wireMenu($('#view-btn'), $('#view-menu'));
  wireMenu($('#profile-btn'), $('#profile-menu'));

  document.querySelectorAll('[data-layout]').forEach((button) => {
    button.onclick = () => setLayout(button.dataset.layout);
  });

  $('#zoom').oninput = (event) => {
    grid.setZoom(Number(event.target.value));
    store.set('zoom', grid.zoom);
  };

  document.querySelectorAll('[data-view]').forEach((button) => {
    button.onclick = async () => {
      if (button.dataset.view === 'memories') {
        await loadMemories();
        if (activeMemories.length) viewer.open(activeMemories.map((item) => item.id), 0);
        else toast(i18n.t('No memories for this day yet.'));
        document.querySelector('.shell').classList.remove('mobile-open');
        return;
      }
      state.view = button.dataset.view;
      document.querySelectorAll('[data-view]').forEach(
        (b) => b.classList.toggle('active', b === button),
      );
      document.querySelector('.shell').classList.remove('mobile-open');
      reload({ resetScroll: true });
    };
  });

  $('#sort').onchange = (event) => {
    state.filters.sort = event.target.value;
    reload({ resetScroll: true });
  };

  $('#clear-filters').onclick = clearFilters;

  wireSearch();
  wireSelection();
  wireScrubber();
  wireVisibility();
  wireMemories();
  wireMap();
  wireUpload();
  new PhoneBackup({ toast, onFiled: () => reload() }).wire();
  wireSharing();
  wireAlbums();
  wireDuplicatesReview();

  document.querySelectorAll('.modal').forEach((modal) => {
    modal.addEventListener('click', (event) => {
      if (event.target === modal) modal.hidden = true;
    });
  });
}

function setLayout(mode) {
  if (!MODES.includes(mode)) return;
  grid.setMode(mode);
  store.set('layout', mode);
  showLayout(mode);
  buildScrubber();
}

/** Mark the layout in use, and put its icon on the view button. */
function showLayout(mode) {
  let current = null;
  document.querySelectorAll('[data-layout]').forEach((button) => {
    const on = button.dataset.layout === mode;
    button.classList.toggle('active', on);
    button.setAttribute('aria-pressed', String(on));
    if (on) current = button;
  });
  const icon = $('#view-btn-icon');
  const svg = current?.querySelector('svg');
  if (icon && svg) icon.replaceChildren(svg.cloneNode(true));
}

/* -- small menus ---------------------------------------------------------- */

//: The topbar's drop-down menus (view, profile), so Escape can close
//: whichever is open.
const menus = [];

/**
 * A button that opens a small menu under it.
 *
 * The same behaviour as the "More" menus: the button toggles it, a click
 * outside or on an item closes it, Escape closes it and gives focus back to
 * the button. Opened from the keyboard, focus goes to the first item and the
 * arrow keys move between items. An item marked `data-keep-open` (the theme
 * and the language, which somebody may press twice to get where they want)
 * leaves the menu open.
 */
function wireMenu(button, menu) {
  if (!button || !menu) return;
  const items = () => [...menu.querySelectorAll('button')]
    .filter((item) => !item.hidden && item.offsetParent !== null);
  const close = (returnFocus = false) => {
    if (!menu.classList.contains('open')) return false;
    const hadFocus = menu.contains(document.activeElement);
    menu.classList.remove('open');
    button.setAttribute('aria-expanded', 'false');
    if (returnFocus && hadFocus) button.focus();
    return true;
  };
  menus.push(close);
  button.addEventListener('click', (event) => {
    event.stopPropagation();
    const open = !menu.classList.contains('open');
    menus.forEach((other) => other !== close && other());
    menu.classList.toggle('open', open);
    button.setAttribute('aria-expanded', String(open));
    if (open && event.detail === 0) items()[0]?.focus();
  });
  menu.addEventListener('click', (event) => {
    const item = event.target.closest('button');
    if (item && !item.hasAttribute('data-keep-open')) close();
  });
  menu.addEventListener('keydown', (event) => {
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
    event.preventDefault();
    event.stopPropagation();          // not the gallery's own arrow keys
    const list = items();
    const at = list.indexOf(document.activeElement);
    const next = at < 0 ? 0 : (at + (event.key === 'ArrowDown' ? 1 : -1) + list.length) % list.length;
    list[next]?.focus();
  });
  document.addEventListener('click', (event) => {
    if (menu.contains(event.target) || button.contains(event.target)) return;
    close();
  });
}

/** Close whichever topbar menu is open. True if one was. */
function closeMenus() {
  return menus.map((close) => close(true)).some(Boolean);
}

/** Save the language on the profile, so every device of theirs follows. */
async function rememberLanguage(code) {
  if (!state.user || state.user.anonymous || !state.user.id) return;
  try {
    state.user = await accountsApi.updateMe({ language: code });
  } catch { /* this device still remembers it */ }
}

/** The label on the language button: what you get if you press it. */
function showLanguage() {
  const label = $('#lang-label');
  if (!label) return;
  const codes = i18n.LANGUAGES.map((l) => l.code);
  const next = i18n.LANGUAGES[(codes.indexOf(i18n.language()) + 1) % codes.length];
  // One letter, not the word: the full name stays in the tooltip and the
  // accessible name, in its own language.
  label.textContent = next.letter;
  label.lang = next.code;             // so "அ" is drawn and read as Tamil
  const button = $('#lang-btn');
  if (button) {
    button.title = next.name;
    button.setAttribute('aria-label', next.name);
  }
}

function cycleTheme() {
  const order = ['system', 'light', 'dark'];
  const current = document.documentElement.dataset.theme || 'system';
  applyTheme(order[(order.indexOf(current) + 1) % order.length]);
  toast(`Theme: ${document.documentElement.dataset.theme}`);
}
window.cycleTheme = cycleTheme;

/* -- accounts ----------------------------------------------------------- */

function wireAccounts() {
  $('#profile-open').onclick = () => profileSheet.open(state.user);
  $('#signin-btn').onclick = async () => {
    gate.show(await accountsApi.state(), 'picker');
  };
  $('#switch-btn').onclick = async () => {
    forgetCachedShell();
    await accountsApi.logout();
    window.location.reload();
  };
  $('#profile-sheet').addEventListener('click', (event) => {
    if (event.target === $('#profile-sheet')) $('#profile-sheet').hidden = true;
  });
}

/* -- visibility (admins browsing the gallery) ---------------------------- */

/**
 * Per-item visibility only. Folder rules and library settings live on the
 * admin console; this is the equivalent of tagging a photo you can already see.
 */
function wireVisibility() {
  document.querySelectorAll('[data-sel-vis]').forEach((button) => {
    button.onclick = async () => {
      const ids = [...grid.selection];
      if (!ids.length) return;
      try {
        const result = await accountsApi.setVisibility(ids, button.dataset.selVis);
        toast(`${result.updated} item${result.updated === 1 ? '' : 's'} → ${visLabel(button.dataset.selVis)}.`);
        grid.clearSelection();
        await refreshStatus();
        reload();
      } catch (exc) { toast(exc.message, true); }
    };
  });

  document.querySelectorAll('#v-visibility button').forEach((button) => {
    button.onclick = async () => {
      const item = viewer.item;
      if (!item) return;
      try {
        await accountsApi.setVisibility([item.id], button.dataset.vis);
        item.visibility = button.dataset.vis;
        syncViewerVisibility();
        toast(`Now visible to ${visLabel(button.dataset.vis)}.`);
        refreshStatus();
      } catch (exc) { toast(exc.message, true); }
    };
  });
}

function visLabel(visibility) {
  return { public: 'everyone', family: 'family only', hidden: 'admins only' }[visibility]
    || visibility;
}

// Named here, translated where shown: this table is built before any locale
// has loaded, so i18n.t() here would freeze the English (see i18n.key).
const VIS_REASONS = {
  hidden: i18n.key('Hidden because it was in a hidden file or folder on disk'),
  folder: i18n.key('Follows the rule set on its folder'),
  item: i18n.key('Set on this item'),
  screen: i18n.key('Hidden because it looks like a screenshot, a document or a photo of a screen'),
  default: '',
};

function syncViewerVisibility() {
  const picker = $('#v-visibility');
  if (!picker || picker.hidden) return;
  const current = viewer.item?.visibility || 'family';
  picker.querySelectorAll('button').forEach((button) => {
    button.classList.toggle('active', button.dataset.vis === current);
  });
  // Say where the setting came from. An admin looking at a hidden photo
  // otherwise has no way to tell whether they hid it or the filesystem did.
  const reason = VIS_REASONS[viewer.item?.visibility_source];
  picker.title = reason ? i18n.t(reason) : i18n.t('Who can see this');
  picker.dataset.source = viewer.item?.visibility_source || 'default';
}

/* -- search ------------------------------------------------------------- */

function wireSearch() {
  const input = $('#search');
  const list = $('#suggestions');

  input.addEventListener('input', () => {
    const value = input.value.trim();
    $('#clear-search').hidden = !value;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.filters.q = value;
      reload({ resetScroll: true });
    }, value ? 260 : 0);
    updateSuggestions(value);
  });

  input.addEventListener('keydown', (event) => {
    const options = [...list.querySelectorAll('li')];
    const active = list.querySelector('[aria-selected="true"]');
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      if (!options.length) return;
      event.preventDefault();
      const index = options.indexOf(active);
      const next = event.key === 'ArrowDown'
        ? Math.min(options.length - 1, index + 1)
        : Math.max(0, index - 1);
      options.forEach((li, i) => li.setAttribute('aria-selected', String(i === next)));
    } else if (event.key === 'Enter' && active) {
      active.click();
    } else if (event.key === 'Escape') {
      list.hidden = true;
      input.blur();
    }
  });

  input.addEventListener('blur', () => setTimeout(() => (list.hidden = true), 140));
  $('#clear-search').onclick = () => {
    clearTimeout(searchTimer);
    input.value = '';
    $('#clear-search').hidden = true;
    state.filters.q = '';
    list.hidden = true;
    reload({ resetScroll: true });
  };
}

async function updateSuggestions(value) {
  const list = $('#suggestions');
  if (!value || value.length < 2) {
    list.hidden = true;
    return;
  }
  let data;
  try {
    data = await api.suggest(value);
  } catch { return; }

  if ($('#search').value.trim() !== value) return;

  const entries = [
    ...(data.concepts || []).map((c) => ({ type: 'concept', value: c })),
    ...(data.suggestions || []),
  ].slice(0, 10);

  list.innerHTML = '';
  if (!entries.length) {
    list.hidden = true;
    return;
  }
  for (const entry of entries) {
    const li = document.createElement('li');
    li.setAttribute('role', 'option');
    const kind = document.createElement('span');
    kind.className = 'kind';
    kind.textContent = entry.type;
    const label = document.createElement('span');
    label.textContent = entry.value;
    li.append(kind, label);
    if (entry.count) {
      const n = document.createElement('span');
      n.className = 'n';
      n.textContent = entry.count;
      li.appendChild(n);
    }
    li.onclick = () => {
      clearTimeout(searchTimer);
      list.hidden = true;
      if (entry.type === 'tag') {
        $('#search').value = '';
        state.filters.q = '';
        state.filters.tag = entry.value;
      } else if (entry.type === 'folder') {
        state.filters.folder = entry.value;
      } else if (entry.type === 'camera') {
        state.filters.camera = entry.value;
      } else {
        $('#search').value = entry.value;
        state.filters.q = entry.value;
      }
      reload({ resetScroll: true });
    };
    list.appendChild(li);
  }
  list.hidden = false;
}

/* -- filter bar --------------------------------------------------------- */

function renderFilterBar() {
  const bar = $('#active-filters');
  bar.innerHTML = '';
  const chips = [];
  if (state.filters.q) chips.push([i18n.t('Search'), state.filters.q, () => {
    state.filters.q = '';
    $('#search').value = '';
    $('#clear-search').hidden = true;
  }]);
  if (state.filters.tag) chips.push(['Tag', state.filters.tag, () => (state.filters.tag = '')]);
  if (state.filters.folder) chips.push([i18n.t('Folder'), state.filters.folder, () => (state.filters.folder = '')]);
  if (state.filters.camera) chips.push([i18n.t('Camera'), state.filters.camera, () => (state.filters.camera = '')]);
  if (state.filters.person) {
    const person = (state.people || []).find((p) => p.id === state.filters.person);
    chips.push([i18n.t('Person'), person?.name || i18n.t('Someone'), () => (state.filters.person = 0)]);
  }
  if (state.filters.near) chips.push([i18n.t('Taken near'), state.nearLabel || i18n.t('this photo'), () => {
    state.filters.near = 0;
    state.nearLabel = '';
  }]);
  if (state.filters.from) chips.push([i18n.t('Year'), state.filters.from.slice(0, 4), () => {
    state.filters.from = '';
    state.filters.to = '';
  }]);
  if (state.filters.occasion) {
    const trip = (state.occasions || []).find((o) => o.id === state.filters.occasion);
    chips.push([i18n.t('Trip'), (trip && occasionTitle(trip)) || i18n.t('Trip'), () => (state.filters.occasion = 0)]);
  }
  if (state.filters.album) {
    const album = (state.albums || []).find((a) => a.id === state.filters.album);
    chips.push([i18n.t('Album'), album?.name || i18n.t('Album'), () => (state.filters.album = 0)]);
  }

  for (const [label, value, clear] of chips) {
    const pill = document.createElement('span');
    pill.className = 'pill';
    const b = document.createElement('b');
    b.textContent = `${label}: `;
    const text = document.createElement('span');
    text.textContent = value;
    const button = document.createElement('button');
    button.type = 'button';
    button.setAttribute('aria-label', `Remove ${label} filter`);
    button.innerHTML = '<svg viewBox="0 0 24 24"><path d="M18 6 6 18M6 6l12 12"/></svg>';
    button.onclick = () => {
      clear();
      syncChips();
      reload({ resetScroll: true });
    };
    pill.append(b, text, button);
    bar.appendChild(pill);
  }

  if (state.filters.album) {
    const album = (state.albums || []).find((a) => a.id === state.filters.album);
    if (album) {
      const shareBtn = document.createElement('button');
      shareBtn.className = 'btn ghost small';
      shareBtn.innerHTML = '<svg viewBox="0 0 24 24" style="width:13px;height:13px;margin-right:4px;vertical-align:-2px;"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/></svg>Share Album';
      shareBtn.onclick = () => openShareModal({ type: 'album', id: album.id, name: album.name });
      bar.appendChild(shareBtn);

      const delBtn = document.createElement('button');
      delBtn.className = 'btn ghost small';
      delBtn.style.color = 'var(--danger)';
      delBtn.textContent = i18n.t('Delete Album');
      delBtn.onclick = async () => {
        if (!confirm(`Delete album "${album.name}"? Photographs inside will not be deleted.`)) return;
        try {
          await api.deleteAlbum(album.id);
          toast(`Album "${album.name}" deleted.`);
          state.filters.album = 0;
          await loadAlbums();
          syncChips();
          reload({ resetScroll: true });
        } catch (err) {
          toast(i18n.t('Could not delete album: {reason}', { reason: err.message }), true);
        }
      };
      bar.appendChild(delBtn);
    }
  }

  $('#filter-bar').hidden = chips.length === 0;
}

function clearFilters() {
  clearTimeout(searchTimer);
  state.filters = { ...state.filters, q: '', tag: '', folder: '', camera: '', from: '', to: '', person: 0,
                    occasion: 0, album: 0, near: 0 };
  $('#suggestions').hidden = true;
  $('#search').value = '';
  $('#clear-search').hidden = true;
  syncChips();
  reload({ resetScroll: true });
}

function syncChips() {
  document.querySelectorAll('#tag-cloud .chip').forEach((chip) => {
    chip.classList.toggle('active', chip.dataset.tag === state.filters.tag);
  });
  document.querySelectorAll('#year-list .chip').forEach((chip) => {
    chip.classList.toggle('active', chip.dataset.year === state.filters.from.slice(0, 4));
  });
  document.querySelectorAll('#folder-list button').forEach((button) => {
    button.classList.toggle('active', button.dataset.folder === state.filters.folder);
  });
  document.querySelectorAll('#people-row .person-chip').forEach((button) => {
    button.classList.toggle('active',
      Number(button.dataset.person) === state.filters.person);
  });
  document.querySelectorAll('#trip-list .trip-card').forEach((button) => {
    button.classList.toggle('active',
      Number(button.dataset.occasion) === state.filters.occasion);
  });
  document.querySelectorAll('#album-list .album-card').forEach((button) => {
    button.classList.toggle('active',
      Number(button.dataset.album) === state.filters.album);
  });
  const removeBtn = $('#sel-album-remove');
  if (removeBtn) {
    removeBtn.hidden = !state.filters.album;
  }
}

/* People.

   The list comes from the server already narrowed to this viewer: somebody
   who only appears in photographs they may not open is not in it at all. So
   there is nothing to filter here, and nothing that could be revealed by
   comparing what two people see. */
/** How many rows of faces show before "Show all". */
const PEOPLE_ROWS = 2;

function renderPeople() {
  const box = $('#people-row');
  const people = state.people || [];
  $('#people-block').hidden = people.length === 0;
  if (!box) return;
  box.innerHTML = '';
  // Two full rows of faces, then "Show all": a household of forty would
  // otherwise push albums and trips off the bottom of the sidebar. Full rows
  // whatever the width, so it is counted rather than assumed: four columns in
  // the sidebar, fewer on a narrow screen or a zoomed page.
  const columns = peopleColumns(box);
  box.dataset.columns = String(columns);
  watchPeopleWidth(box);
  const room = columns * PEOPLE_ROWS;
  const folds = people.length > room;
  let shown = folds && !state.peopleExpanded ? people.slice(0, room) : people;
  // Whoever the gallery is filtered by stays in view, folded or not.
  const chosen = people.find((p) => p.id === state.filters.person);
  if (chosen && !shown.includes(chosen)) shown = [...shown.slice(0, room - 1), chosen];
  for (const person of shown) box.appendChild(personButton(person));
  if (folds) {
    const more = document.createElement('button');
    more.type = 'button';
    more.className = 'people-more';
    more.textContent = state.peopleExpanded
      ? i18n.t('Show fewer')
      : i18n.t('Show all ({count})', { count: people.length });
    more.onclick = () => {
      state.peopleExpanded = !state.peopleExpanded;
      renderPeople();
    };
    box.appendChild(more);
  }
  syncChips();
}

/** Columns the people grid has now: the sidebar's four, or fewer on a narrow
 *  or zoomed page. Not laid out (a closed sidebar on a phone), the browser
 *  answers with the rule itself, and four is what it will be once it opens. */
function peopleColumns(box) {
  const template = getComputedStyle(box).gridTemplateColumns;
  if (!template || template === 'none' || template.includes('repeat')) return 4;
  return Math.max(1, template.split(' ').length);
}

// Folded to two full rows, so a change in width that changes the columns
// folds it again rather than leaving a row half empty. Watched from the first
// time the list is drawn, when the element is certainly there.
let peopleResize = null;
function watchPeopleWidth(box) {
  if (peopleResize || typeof ResizeObserver === 'undefined') return;
  peopleResize = new ResizeObserver(() => {
    if (box.childElementCount && Number(box.dataset.columns) !== peopleColumns(box)) {
      renderPeople();
    }
  });
  peopleResize.observe(box);
}

/** One person: a face in a circle, the name centred under it. */
function personButton(person) {
  const button = document.createElement('button');
  button.className = 'person-chip';
  button.type = 'button';
  button.dataset.person = String(person.id);
  button.title = `${person.name} — ${i18n.t('{count} photographs',
    { count: Number(person.photo_count || 0).toLocaleString() })}`;
  button.appendChild(person.cover_face_id ? faceImage(person) : initialsFor(person.name));
  const label = document.createElement('span');
  label.textContent = person.name;
  button.appendChild(label);
  button.onclick = () => {
    const id = Number(person.id);
    if (state.filters.person === id) {
      state.filters.person = 0;                       // the same face again: let go of it
    } else {
      // Somebody's photographs, not the overlap of them with whatever else was
      // switched on: a typed search, a folder, or a view of only videos would
      // mostly leave nothing to see. The sort order is the person's own to keep.
      clearTimeout(searchTimer);
      state.filters = { ...state.filters, q: '', tag: '', folder: '', camera: '', from: '', to: '',
                        occasion: 0, album: 0, near: 0, person: id };
      state.view = 'all';
      document.querySelectorAll('[data-view]').forEach(
        (b) => b.classList.toggle('active', b.dataset.view === 'all'));
      $('#suggestions').hidden = true;
      $('#search').value = '';
      $('#clear-search').hidden = true;
    }
    syncChips();
    reload({ resetScroll: true });
  };
  return button;
}

function faceImage(person) {
  const img = document.createElement('img');
  img.className = 'face';
  img.src = `/api/faces/thumb/${person.cover_face_id}`;
  img.alt = '';
  img.loading = 'lazy';
  img.decoding = 'async';
  // A crop that will not load becomes the initials, not a broken circle.
  img.onerror = () => img.replaceWith(initialsFor(person.name));
  return img;
}

/** Initials on a colour of their own, for somebody with no face to show. */
function initialsFor(name) {
  const face = document.createElement('div');
  face.className = 'face initials';
  const words = String(name || '?').trim().split(/\s+/).filter(Boolean);
  face.textContent = words.slice(0, 2).map((word) => Array.from(word)[0]).join('').toUpperCase() || '?';
  let hash = 0;
  for (const ch of String(name || '')) hash = (hash * 31 + ch.codePointAt(0)) >>> 0;
  face.style.backgroundColor = `hsl(${hash % 360} 45% 46%)`;
  return face;
}

async function loadPeople() {
  try {
    const data = await api.people();
    state.people = data.people || [];
  } catch {
    // Faces are an addition to the gallery, never a requirement for it. If the
    // endpoint is unavailable the row simply does not appear.
    state.people = [];
  }
  renderPeople();
}

/* Trips — the auto-albums the scanner already groups the library into (see
   ninaivu/media/occasions.py and db.rebuild_occasions). Read-only and
   derived, so there is nothing to manage here: just a way in. */
/** An occasion's title in the reader's language: its place, and its dates
 *  written by the page rather than the server's English. */
function occasionTitle(trip) {
  if (!trip.started_at) return trip.title || '';
  const dates = i18n.dateRange(trip.started_at, trip.ended_at || trip.started_at);
  if (!dates) return trip.title || '';
  return trip.place ? `${trip.place}, ${dates}` : dates;
}

function renderOccasions() {
  const box = $('#trip-list');
  const trips = state.occasions || [];
  $('#trips-block').hidden = trips.length === 0;
  if (!box) return;
  box.innerHTML = '';
  for (const trip of trips.slice(0, 40)) {
    const button = document.createElement('button');
    button.className = 'trip-card';
    button.type = 'button';
    button.dataset.occasion = String(trip.id);
    const tripTitle = occasionTitle(trip);
    button.title = `${tripTitle} — ${i18n.items(trip.count)}`;

    if (trip.cover_id) {
      const img = document.createElement('img');
      img.src = thumbUrl(trip.cover_id, 80);
      img.alt = '';
      img.loading = 'lazy';
      button.appendChild(img);
    }
    const text = document.createElement('span');
    text.className = 'trip-text';
    const title = document.createElement('b');
    title.textContent = tripTitle;
    const n = document.createElement('span');
    n.className = 'n';
    n.textContent = i18n.items(trip.count);
    text.append(title, n);
    button.appendChild(text);

    button.onclick = () => {
      state.filters.occasion = state.filters.occasion === trip.id ? 0 : trip.id;
      syncChips();
      reload({ resetScroll: true });
    };
    box.appendChild(button);
  }
  syncChips();
}

async function loadOccasions() {
  try {
    const data = await api.occasions();
    state.occasions = data.occasions || [];
  } catch {
    // Same fallback as people: an auto-album feed that fails to load must
    // never take the rest of the gallery down with it.
    state.occasions = [];
  }
  renderOccasions();
}

let albumTargetIds = [];

function renderAlbums() {
  const box = $('#album-list');
  const albums = state.albums || [];
  const block = $('#albums-block');
  if (block) block.hidden = false;
  if (!box) return;
  box.innerHTML = '';
  if (albums.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'hint';
    empty.style.padding = '6px 8px';
    empty.style.fontSize = '12px';
    empty.textContent = i18n.t('No albums yet. Click + to create one.');
    box.appendChild(empty);
    return;
  }
  for (const album of albums) {
    const button = document.createElement('button');
    button.className = 'album-card';
    button.type = 'button';
    button.dataset.album = String(album.id);
    button.title = `${album.name} — ${i18n.items(album.n)}`;

    if (album.cover_id) {
      const img = document.createElement('img');
      img.src = thumbUrl(album.cover_id, 80);
      img.alt = '';
      img.loading = 'lazy';
      button.appendChild(img);
    } else {
      const ph = document.createElement('div');
      ph.className = 'album-icon-ph';
      ph.innerHTML = '<svg viewBox="0 0 24 24"><rect width="18" height="18" x="3" y="3" rx="2"/><path d="M3 9h18M9 21V9"/></svg>';
      button.appendChild(ph);
    }

    const text = document.createElement('span');
    text.className = 'album-text';
    const title = document.createElement('b');
    title.textContent = album.name;
    const n = document.createElement('span');
    n.className = 'n';
    n.textContent = i18n.items(album.n);
    text.append(title, n);
    button.appendChild(text);

    button.onclick = () => {
      state.filters.album = state.filters.album === album.id ? 0 : album.id;
      syncChips();
      reload({ resetScroll: true });
    };
    box.appendChild(button);
  }
  syncChips();
}

async function loadAlbums() {
  try {
    const data = await api.albums();
    state.albums = data.albums || [];
  } catch {
    state.albums = [];
  }
  renderAlbums();
}

function openAlbumModal(ids = []) {
  albumTargetIds = ids;
  const modal = $('#album-modal');
  const title = $('#album-modal-title');
  const subtitle = $('#album-modal-subtitle');
  const input = $('#album-new-name');
  input.value = '';

  if (ids.length > 0) {
    title.textContent = `Add ${ids.length} ${ids.length === 1 ? 'photo' : 'photos'} to Album`;
    subtitle.textContent = i18n.t('Choose an album or enter a name to create a new one:');
  } else {
    title.textContent = i18n.t('Create New Album');
    subtitle.textContent = i18n.t('Enter a name for the new album:');
  }

  const list = $('#album-modal-list');
  list.innerHTML = '';
  const albums = state.albums || [];
  if (albums.length === 0) {
    list.innerHTML = '<div class="hint" style="padding:8px;">No existing albums yet.</div>';
  } else {
    for (const album of albums) {
      const row = document.createElement('button');
      row.className = 'album-modal-item';
      row.type = 'button';

      if (album.cover_id) {
        const img = document.createElement('img');
        img.src = thumbUrl(album.cover_id, 64);
        img.alt = '';
        row.appendChild(img);
      } else {
        const ph = document.createElement('div');
        ph.className = 'album-icon-ph';
        ph.innerHTML = '<svg viewBox="0 0 24 24"><rect width="18" height="18" x="3" y="3" rx="2"/><path d="M3 9h18M9 21V9"/></svg>';
        row.appendChild(ph);
      }

      const text = document.createElement('div');
      text.className = 'album-text';
      const b = document.createElement('b');
      b.textContent = album.name;
      const n = document.createElement('span');
      n.className = 'n';
      n.textContent = i18n.items(album.n);
      text.append(b, n);
      row.appendChild(text);

      row.onclick = async () => {
        if (albumTargetIds.length > 0) {
          try {
            await api.albumAdd(album.id, albumTargetIds);
            toast(i18n.t('Added {items} to "{album}".', { items: i18n.items(albumTargetIds.length), album: album.name }));
            modal.hidden = true;
            grid.clearSelection();
            await loadAlbums();
            if (state.filters.album === album.id) reload();
          } catch (err) {
            toast(i18n.t('Could not add to album: {reason}', { reason: err.message }), true);
          }
        } else {
          modal.hidden = true;
          state.filters.album = album.id;
          syncChips();
          reload({ resetScroll: true });
        }
      };
      list.appendChild(row);
    }
  }

  modal.hidden = false;
  setTimeout(() => input.focus(), 50);
}

function wireAlbums() {
  $('#new-album-btn')?.addEventListener('click', () => {
    openAlbumModal([]);
  });

  $('#album-create-btn')?.addEventListener('click', async () => {
    const input = $('#album-new-name');
    const name = input.value.trim();
    if (!name) {
      toast(i18n.t('Please enter an album name.'), true);
      input.focus();
      return;
    }
    const button = $('#album-create-btn');
    button.disabled = true;
    try {
      const created = await api.createAlbum(name, albumTargetIds);
      toast(albumTargetIds.length
        ? i18n.t('Album "{name}" made, with {items}.', { name, items: i18n.items(albumTargetIds.length) })
        : i18n.t('Album "{name}" made.', { name }));
      $('#album-modal').hidden = true;
      if (albumTargetIds.length) grid.clearSelection();
      await loadAlbums();
      state.filters.album = created.id;
      syncChips();
      reload({ resetScroll: true });
    } catch (err) {
      toast(i18n.t('Could not create album: {reason}', { reason: err.message }), true);
    } finally {
      button.disabled = false;
    }
  });

  $('#album-new-name')?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      $('#album-create-btn')?.click();
    }
  });

  $('#album-modal-close')?.addEventListener('click', () => {
    $('#album-modal').hidden = true;
  });
  $('#album-modal-close-x')?.addEventListener('click', () => {
    $('#album-modal').hidden = true;
  });

  viewer.addEventListener('album', (e) => {
    if (e.detail?.item) {
      openAlbumModal([e.detail.item.id]);
    }
  });
}

function renderTagCloud() {
  const box = $('#tag-cloud');
  const tags = state.facets?.tags || [];
  $('#tags-block').hidden = tags.length === 0;
  box.innerHTML = '';
  for (const tag of tags.slice(0, 28)) {
    const chip = document.createElement('button');
    chip.className = 'chip';
    chip.type = 'button';
    chip.dataset.tag = tag.name;
    chip.textContent = tag.name;
    const n = document.createElement('span');
    n.className = 'n';
    n.textContent = tag.count;
    chip.appendChild(n);
    chip.onclick = () => {
      state.filters.tag = state.filters.tag === tag.name ? '' : tag.name;
      syncChips();
      reload({ resetScroll: true });
    };
    box.appendChild(chip);
  }
  syncChips();
}

function renderYears() {
  const box = $('#year-list');
  const years = state.facets?.years || [];
  $('#years-block').hidden = years.length < 2;
  box.innerHTML = '';
  for (const year of years) {
    const chip = document.createElement('button');
    chip.className = 'chip';
    chip.type = 'button';
    chip.dataset.year = year.year;
    chip.textContent = year.year;
    const n = document.createElement('span');
    n.className = 'n';
    n.textContent = year.count;
    chip.appendChild(n);
    chip.onclick = () => {
      const active = state.filters.from.slice(0, 4) === year.year;
      state.filters.from = active ? '' : `${year.year}-01-01`;
      state.filters.to = active ? '' : `${year.year}-12-31`;
      syncChips();
      reload({ resetScroll: true });
    };
    box.appendChild(chip);
  }
}

function renderFolders() {
  const box = $('#folder-list');
  const folders = (state.facets?.folders || []).filter((f) => f.name);
  $('#folders-block').hidden = folders.length < 2;
  box.innerHTML = '';
  for (const folder of folders.slice(0, 40)) {
    const button = document.createElement('button');
    button.type = 'button';
    button.dataset.folder = folder.name;
    // The name shown is for reading; `dataset.folder` above, and the filter
    // below, keep the path exactly as the server gave it.
    const path = document.createElement('span');
    path.className = 'path';
    path.title = folder.name;
    const asDate = i18n.folderDate(folder.name);
    const cut = folder.name.replace(/\/+$/, '').lastIndexOf('/');
    if (asDate) {
      // "2025/05/11" is a day, and reads as one: "11 May 2025".
      path.textContent = asDate;
    } else if (cut > 0) {
      // A deep path: its last folder is the one that says what is in it, so
      // that is the part kept whole; the folders above it give way first.
      const parent = document.createElement('span');
      parent.className = 'parent';
      parent.textContent = folder.name.slice(0, cut + 1);
      const leaf = document.createElement('span');
      leaf.className = 'leaf';
      leaf.textContent = folder.name.slice(cut + 1).replace(/\/+$/, '');
      path.append(parent, leaf);
    } else {
      path.textContent = folder.name;
    }
    const n = document.createElement('span');
    n.className = 'n';
    n.textContent = folder.count;
    button.append(path, n);
    button.onclick = () => {
      state.filters.folder = state.filters.folder === folder.name ? '' : folder.name;
      syncChips();
      reload({ resetScroll: true });
    };
    box.appendChild(button);
  }
}

/* -- selection ---------------------------------------------------------- */

function wireSelection() {
  $('#sel-cancel').onclick = () => grid.clearSelection();
  $('#sel-all').onclick = () => grid.selectAll();
  $('#sel-fav').onclick = () => bulk({ favorite: true });
  $('#sel-unfav').onclick = () => bulk({ favorite: false });
  $('#sel-download').onclick = () => {
    // One selection, one file. This used to fire up to twenty separate
    // downloads 320 ms apart, which browsers block after the first few and
    // which silently dropped everything past the twentieth anyway.
    const ids = [...grid.selection];
    if (!ids.length) return;

    if (ids.length === 1) {
      const link = document.createElement('a');
      link.href = `/api/download/${ids[0]}`;
      link.download = '';
      link.click();
      return;
    }

    const capped = ids.slice(0, 2000);
    if (capped.length < ids.length) {
      toast(`Downloading the first ${capped.length} of ${ids.length}.`);
    } else {
      toast(`Preparing ${capped.length} photographs…`);
    }
    // The server streams the archive as it reads, so the browser starts
    // saving straight away rather than waiting on a spinner.
    window.location.href = `/api/download/zip?ids=${capped.join(',')}`;
  };
  wireExport();
  $('#sel-delete').onclick = () => deleteSelected();
  $('#sel-rot-left').onclick = () => rotateSelected(270);
  $('#sel-rot-right').onclick = () => rotateSelected(90);
  $('#sel-album').onclick = () => {
    const ids = [...grid.selection];
    if (!ids.length) return;
    openAlbumModal(ids);
  };
  $('#sel-album-remove').onclick = async () => {
    const ids = [...grid.selection];
    if (!ids.length || !state.filters.album) return;
    try {
      await api.albumRemove(state.filters.album, ids);
      toast(i18n.t('Removed {items} from the album.', { items: i18n.items(ids.length) }));
      grid.clearSelection();
      await loadAlbums();
      reload();
    } catch (err) {
      toast(i18n.t('Could not remove items: {reason}', { reason: err.message }), true);
    }
  };
}

/**
 * Export the selection converted to JPEG, PNG or WebP, optionally smaller.
 *
 * "Download" hands over the files as they are; this is for the photograph
 * that has to be a JPEG to be sent, or is a raw file nobody else can open.
 * The server converts as it streams the archive, so the browser saves it the
 * same way it saves a plain download.
 */
function wireExport() {
  const modal = $('#export-modal');
  if (!modal) return;
  const format = $('#export-format'), quality = $('#export-quality');
  const close = () => { modal.hidden = true; };
  const sync = () => {
    $('#export-quality-field').hidden = format.value === 'png';
    $('#export-quality-out').textContent = quality.value;
  };
  format.onchange = quality.oninput = sync;
  $('#sel-export').onclick = () => {
    const count = grid.selection.size;
    if (!count) return;
    $('#export-count').textContent = i18n.t(
      'Convert {items} to another format, and shrink them if you like.',
      { items: i18n.items(count) });
    sync();
    modal.hidden = false;
    format.focus();
  };
  $('#export-close').onclick = $('#export-cancel').onclick = close;
  modal.addEventListener('mousedown', (event) => { if (event.target === modal) close(); });
  $('#export-form').onsubmit = (event) => {
    event.preventDefault();
    const ids = [...grid.selection].slice(0, 2000);
    if (!ids.length) return close();
    const query = new URLSearchParams({
      ids: ids.join(','), format: format.value, edge: $('#export-edge').value,
      quality: quality.value,
    });
    if ($('#export-metadata').checked) query.set('metadata', 'keep');
    toast(i18n.t('Preparing your export…'));
    close();
    // Streamed by the server as each photograph is converted.
    window.location.href = `/api/download/zip?${query}`;
  };
}

/**
 * Turn the selected photographs and videos — in the files themselves.
 *
 * The viewer's rotate button writes a number into the index and leaves the
 * file alone, which is right for correcting one photograph. This is the other
 * job: a box of scanned prints that all came out sideways, where the point is
 * that they are still the right way up after they leave Ninaivu.
 *
 * It asks first, because it writes to the library. It asks once, not once per
 * file, and it says plainly what it is about to do.
 */
async function rotateSelected(turn) {
  const ids = [...grid.selection];
  if (!ids.length) return;

  const modal = $('#rotate-modal');
  const count = ids.length;
  $('#rotate-what').textContent =
    `${count.toLocaleString()} ${count === 1 ? 'file' : 'files'} will be turned a `
    + `quarter turn ${turn === 90 ? 'clockwise' : 'anticlockwise'}.`;
  modal.hidden = false;

  const close = () => {
    modal.hidden = true;
    $('#rotate-confirm').onclick = null;
    $('#rotate-cancel').onclick = null;
    document.onkeydown = null;
  };

  $('#rotate-cancel').onclick = close;
  document.onkeydown = (event) => { if (event.key === 'Escape') close(); };
  $('#rotate-confirm').onclick = async () => {
    close();
    toast(`Turning ${count.toLocaleString()} ${count === 1 ? 'file' : 'files'}…`);
    try {
      const result = await api.rotateFiles(ids, turn);
      const done = result.rotated || 0;
      if (done) {
        toast(`${done.toLocaleString()} ${done === 1 ? 'file' : 'files'} turned.`);
      }
      // What could not be turned is named rather than quietly dropped: an
      // admin who selected forty and saw "38 turned" deserves to know which
      // two did not move, and why.
      const skipped = result.skipped || [];
      if (skipped.length) {
        const first = skipped[0];
        toast(skipped.length === 1
          ? `${first.name} was left alone — ${first.why}`
          : `${skipped.length} were left alone, starting with ${first.name} — ${first.why}`,
        true);
      }
      grid.clearSelection();
      await reload();
    } catch (exc) {
      toast(exc.message, true);
    }
  };
}

/**
 * Delete items, after asking for the password.
 *
 * The password is asked for every time, not once per session. Choosing what to
 * delete means looking at the photographs, so this lives in the gallery — and
 * the gallery is the window left open on the sofa.
 *
 * `explicitIds` lets another screen (the duplicates review, below) reuse this
 * same confirm-and-delete flow for a list that has nothing to do with the
 * main grid's selection. Left out — as every existing caller leaves it — this
 * deletes the current `grid.selection`, exactly as before. `onDone` is an
 * optional callback fired after a successful delete, so a caller with its own
 * list to update (the duplicates modal) can refresh itself without a second
 * network round trip.
 */
async function deleteSelected(explicitIds, onDone) {
  const usingSelection = !Array.isArray(explicitIds);
  const ids = usingSelection ? [...grid.selection] : [...explicitIds];
  if (!ids.length) return;

  const modal = $('#delete-modal');
  const input = $('#delete-password');
  const error = $('#delete-error');
  const count = ids.length;

  $('#delete-what').textContent =
    `${count.toLocaleString()} ${count === 1 ? 'item' : 'items'} will be removed `
    + 'from the library.';
  error.hidden = true;
  input.value = '';
  modal.hidden = false;
  input.focus();

  const close = () => {
    modal.hidden = true;
    input.value = '';
    input.onkeydown = null;
    $('#delete-confirm').onclick = null;
    $('#delete-cancel').onclick = null;
  };

  const attempt = async () => {
    if (!input.value) { input.focus(); return; }
    try {
      const result = await api.deleteItems(ids, input.value);
      close();
      const moved = result.deleted;
      toast(`${moved.toLocaleString()} ${moved === 1 ? 'item' : 'items'} `
        + 'moved to the recycle bin.');
      if (result.failed?.length) {
        toast(`${result.failed.length} could not be moved: `
          + result.failed[0].why, true);
      }
      if (usingSelection) grid.clearSelection();
      await reload();
      onDone?.(result);
    } catch (exc) {
      if (exc.status === 401 && exc.data?.needs_password) {
        error.textContent = exc.data.error;
        error.hidden = false;
        input.value = '';
        input.focus();
        return;
      }
      close();
      toast(exc.message, true);
    }
  };

  $('#delete-confirm').onclick = attempt;
  $('#delete-cancel').onclick = close;
  input.onkeydown = (event) => {
    if (event.key === 'Enter') { event.preventDefault(); attempt(); }
    if (event.key === 'Escape') close();
  };
}

async function bulk(fields) {
  const ids = [...grid.selection];
  if (!ids.length) return;
  try {
    await api.bulk(ids, fields);
    toast(`Updated ${ids.length} item${ids.length > 1 ? 's' : ''}.`);
    grid.clearSelection();
    await refreshStatus();
    reload();
  } catch (error) {
    toast(error.message, true);
  }
}

/* -- duplicates review ---------------------------------------------------
 *
 * `/api/duplicates` already groups near-identical shots (a burst, or the same
 * photo saved twice) by perceptual hash and picks the sharpest, highest-
 * resolution one as `best` — never overriding a favourite or a rating someone
 * already gave it (see `db.best_of`). Nothing consumed that pick before this:
 * the sidebar's Duplicates view just filtered the ordinary grid to items that
 * have a `dup_group`, flat and unranked. This turns that existing judgment
 * into something a person can act on, without changing what the plain filter
 * view does for anyone who liked it as it was.
 */
let duplicateGroupsCache = null;

async function loadDuplicatesIfNeeded() {
  const banner = $('#duplicates-banner');
  if (!banner) return;
  if (state.view !== 'duplicates' || !state.user?.can?.download) {
    banner.hidden = true;
    return;
  }
  try {
    const data = await api.duplicates();
    duplicateGroupsCache = data.groups || [];
  } catch {
    duplicateGroupsCache = [];
  }
  renderDuplicatesSummary();
}

function renderDuplicatesSummary() {
  const banner = $('#duplicates-banner');
  if (!banner) return;
  const groups = duplicateGroupsCache || [];
  if (state.view !== 'duplicates' || !groups.length) {
    banner.hidden = true;
    return;
  }
  const wasted = groups.reduce((sum, g) => sum + (g.wasted || 0), 0);
  $('#duplicates-headline').textContent =
    `${groups.length} ${groups.length === 1 ? 'group' : 'groups'} of similar shots`
    + (wasted ? ` · ${humanBytes(wasted)} could be freed` : '');
  banner.hidden = false;
}

function renderDuplicateGroups() {
  const box = $('#duplicates-groups');
  if (!box) return;
  const groups = duplicateGroupsCache || [];
  box.replaceChildren();
  if (!groups.length) {
    const empty = document.createElement('p');
    empty.className = 'empty-note';
    empty.textContent = i18n.t('No duplicate groups right now.');
    box.appendChild(empty);
    return;
  }
  for (const group of groups) {
    const wrap = document.createElement('div');
    wrap.className = 'dup-group';

    const head = document.createElement('div');
    head.className = 'dup-group-head';
    const info = document.createElement('span');
    info.className = 'hint';
    const count = group.items.length;
    info.innerHTML = `<b>${count}</b> versions of this shot`
      + (group.wasted_h ? ` &middot; <b>${group.wasted_h}</b> to free` : '');
    const keepBtn = document.createElement('button');
    keepBtn.className = 'btn ghost small';
    keepBtn.type = 'button';
    keepBtn.textContent = i18n.t('Keep only the best');
    keepBtn.onclick = () => {
      const rest = group.items.filter((i) => i.id !== group.best).map((i) => i.id);
      if (!rest.length) return;
      deleteSelected(rest, () => {
        duplicateGroupsCache = (duplicateGroupsCache || [])
          .filter((g) => g.group !== group.group);
        renderDuplicateGroups();
        renderDuplicatesSummary();
      });
    };
    head.append(info, keepBtn);

    const thumbs = document.createElement('div');
    thumbs.className = 'dup-thumbs';
    group.items.forEach((item, index) => {
      const cell = document.createElement('div');
      cell.className = 'dup-item' + (item.id === group.best ? ' best' : '');
      const img = document.createElement('img');
      img.src = thumbUrl(item.id, 220, item.thumb_v);
      img.loading = 'lazy';
      img.alt = item.name || '';
      cell.appendChild(img);
      if (item.id === group.best) {
        const badge = document.createElement('span');
        badge.className = 'dup-badge';
        badge.textContent = 'Best';
        cell.appendChild(badge);
      }
      const meta = document.createElement('span');
      meta.className = 'dup-item-meta';
      const dims = item.width && item.height ? `${item.width}×${item.height} · ` : '';
      meta.textContent = `${dims}${item.size_h || ''}`;
      cell.appendChild(meta);
      cell.title = item.name || '';
      cell.onclick = () => {
        $('#duplicates-modal').hidden = true;
        viewer.open(group.items.map((i) => i.id), index);
      };
      thumbs.appendChild(cell);
    });

    wrap.append(head, thumbs);
    box.appendChild(wrap);
  }
}

function wireDuplicatesReview() {
  const banner = $('#duplicates-banner');
  if (!banner) return;

  $('#duplicates-review-btn')?.addEventListener('click', () => {
    renderDuplicateGroups();
    $('#duplicates-modal').hidden = false;
  });
  $('#duplicates-close')?.addEventListener('click', () => {
    $('#duplicates-modal').hidden = true;
  });
  $('#duplicates-close-x')?.addEventListener('click', () => {
    $('#duplicates-modal').hidden = true;
  });
  $('#duplicates-keep-all-best')?.addEventListener('click', () => {
    const groups = duplicateGroupsCache || [];
    const rest = groups.flatMap(
      (g) => g.items.filter((i) => i.id !== g.best).map((i) => i.id),
    );
    if (!rest.length) return;
    deleteSelected(rest, () => {
      duplicateGroupsCache = [];
      renderDuplicateGroups();
      renderDuplicatesSummary();
      $('#duplicates-modal').hidden = true;
    });
  });
}

/* -- scrubber ----------------------------------------------------------- */

/** The rail's height when the marks were placed: read once there, not on
 *  every scroll frame, where reading it would force a layout. */
let scrubberRail = 0;

function buildScrubber() {
  const scrubber = $('#scrubber');
  const track = $('#scrubber-track');
  const headers = grid.layout.headers;
  track.innerHTML = '';
  if (headers.length < 3 || grid.layout.height < grid.scroller.clientHeight * 2) {
    scrubber.hidden = true;
    return;
  }
  scrubber.hidden = false;

  // One tick per month, and never two ticks closer than 26px, so the rail
  // stays readable however dense the library is. Months are named, years are
  // written out where they change (see scrubberTicks in layout.js).
  // Placed where the marker will be when the grid is scrolled to that month:
  // the marker goes by scroll position, not by height, and a mark placed by
  // height sat a screen's worth below the month it named near the end.
  const total = Math.max(1, grid.layout.height - grid.scroller.clientHeight);
  const railHeight = Math.max(1, scrubber.clientHeight || grid.scroller.clientHeight);
  scrubberRail = railHeight;
  const locale = i18n.locale();
  const monthName = new Intl.DateTimeFormat(locale, { month: 'short' });
  const yearName = new Intl.DateTimeFormat(locale, { year: 'numeric' });

  for (const tick of scrubberTicks(headers, total, railHeight, 26)) {
    const mark = document.createElement('span');
    // Kept clear of the rail's ends, where half the label was cut off.
    mark.style.top = `clamp(7px, ${tick.frac * 100}%, calc(100% - 7px))`;
    mark.dataset.frac = String(tick.frac);
    if (tick.month) {
      const date = new Date(`${tick.month}-01T00:00:00`);
      mark.textContent = (tick.year ? yearName : monthName).format(date);
      mark.classList.toggle('year', tick.year);
    } else {
      mark.textContent = tick.key === 'match' ? i18n.t('Top') : i18n.t('Undated');
    }
    track.appendChild(mark);
  }
  positionScrubber(grid.scroller.scrollTop /
    Math.max(1, grid.layout.height - grid.scroller.clientHeight));
  labelScrubber(sectionAt(grid.layout, grid.scroller.scrollTop + 40));
}

/** The marker's own label: the month and year it is on. */
function labelScrubber(section) {
  const label = $('#scrubber-thumb').querySelector('span');
  if (!section) return;
  if (section.key === 'match') label.textContent = i18n.t('Best matches');
  else if (!/^\d{4}-\d{2}/.test(section.key)) label.textContent = i18n.t('Undated');
  else {
    label.textContent = new Date(`${section.key.slice(0, 7)}-01T00:00:00`)
      .toLocaleDateString(i18n.locale(), { month: 'short', year: 'numeric' });
  }
}

function positionScrubber(ratio) {
  const thumb = $('#scrubber-thumb');
  const at = Math.max(0, Math.min(1, ratio));
  // Clamped like the marks, so the marker's own label is never cut in half
  // at either end of the rail.
  thumb.style.top = `clamp(12px, ${at * 100}%, calc(100% - 12px))`;
  // The marker carries the month it is on; a mark it would sit on top of
  // gives way, rather than showing through half-covered.
  const rail = scrubberRail || 1;
  const thumbY = Math.max(12, Math.min(rail - 12, at * rail));
  for (const mark of $('#scrubber-track').children) {
    const y = Math.max(7, Math.min(rail - 7, Number(mark.dataset.frac) * rail));
    mark.classList.toggle('covered', Math.abs(y - thumbY) < 20);
  }
}

function wireScrubber() {
  const scrubber = $('#scrubber');
  const thumb = $('#scrubber-thumb');
  let dragging = false;

  const seek = (clientY) => {
    const rect = scrubber.getBoundingClientRect();
    grid.scrollToRatio((clientY - rect.top) / rect.height);
  };

  thumb.addEventListener('pointerdown', (event) => {
    dragging = true;
    scrubber.classList.add('active');
    thumb.setPointerCapture(event.pointerId);
  });
  thumb.addEventListener('pointermove', (event) => dragging && seek(event.clientY));
  thumb.addEventListener('pointerup', () => {
    dragging = false;
    scrubber.classList.remove('active');
  });
  scrubber.addEventListener('click', (event) => {
    if (event.target === thumb || thumb.contains(event.target)) return;
    seek(event.clientY);
  });
}

/* ========================================================================
   Grid / viewer wiring
   ======================================================================== */

function wireGrid() {
  grid.addEventListener('open', (event) => {
    viewer.open(grid.ids, event.detail.index, grid.thumbedIds());
  });

  grid.addEventListener('selection', (event) => {
    const count = event.detail.ids.length;
    $('#selection-bar').hidden = count === 0;
    $('#sel-count').textContent = `${count} selected`;
  });

  grid.addEventListener('scroll', (event) => {
    positionScrubber(event.detail.ratio);
    loadMoreIfNear();
    labelScrubber(event.detail.section);
  });

  grid.addEventListener('layout', () => buildScrubber());
}

function wireViewer() {
  viewer.addEventListener('change', () => syncViewerVisibility());

  viewer.addEventListener('mutated', async (event) => {
    const { id, favorite } = event.detail;
    if (favorite !== undefined) {
      const cell = grid.layout.cells.find((c) => c.id === id);
      if (cell) {
        cell.flags = favorite ? cell.flags | 1 : cell.flags & ~1;
        grid.refreshCell(id);
      }
    }
    refreshStatus();
  });

  viewer.addEventListener('tag', (event) => {
    viewer.close();
    state.filters.tag = event.detail.tag;
    state.filters.q = '';
    $('#search').value = '';
    syncChips();
    reload({ resetScroll: true });
  });

  viewer.addEventListener('jump', async (event) => {
    const index = grid.indexOfId(event.detail.id);
    if (index >= 0) {
      viewer.goTo(index);
    } else {
      viewer.ids = [event.detail.id, ...viewer.ids];
      viewer.goTo(0);
    }
  });

  viewer.addEventListener('close', (event) => {
    const index = grid.indexOfId(event.detail.id);
    if (index >= 0) {
      grid.cursor = index;
      grid.scrollToIndex(index);
      grid.render();
    }
  });

  // The same password-confirmation flow as the selection bar's Delete
  // button, just handed a single id instead of the live grid selection.
  viewer.addEventListener('delete', (event) => {
    const id = event.detail.id;
    if (!id) return;
    deleteSelected([id], () => viewer.close());
  });
}

/* ========================================================================
   Keyboard
   ======================================================================== */

function wireKeyboard() {
  document.addEventListener('keydown', (event) => {
    const target = event.target;
    const typing = target.matches('input, textarea, select') || target.isContentEditable;
    const key = event.key.toLowerCase();
    if (!typing && key === 't' && !event.altKey && !event.ctrlKey && !event.metaKey) {
      cycleTheme();
      return;
    }
    if (document.querySelector('#ai-playground[open]')) return;
    if (event.defaultPrevented) return;
    if (event.key === 'Escape' && closeMenus()) return;
    if (event.key === 'Escape' && $('#topbar-more').classList.contains('open')) {
      $('#topbar-more').classList.remove('open');
      $('#more-btn').setAttribute('aria-expanded', 'false');
      return;
    }
    if (event.key === 'Escape' && $('#viewer-tools-more').classList.contains('open')) {
      viewer.closeMoreMenu(true);
      return;
    }
    if (document.querySelector('.photo-editor[open]')) return;
    if ((event.key === 'Enter' || event.key === ' ') && target.closest('button, a, [role="button"]')) return;
    const overlay = [...document.querySelectorAll('.modal, .sheet')].reverse()
      .find((m) => !m.hidden);
    if (overlay) {
      if (event.key === 'Escape') { overlay.hidden = true; event.preventDefault(); }
      return;
    }

    if (viewer.isOpen) {
      if (typing) return;
      if (viewer.handleKey(event)) event.preventDefault();
      return;
    }

    if (event.key === '/' && !typing) {
      event.preventDefault();
      $('#search').focus();
      $('#search').select();
      return;
    }
    if (event.key === 'Escape') {
      const overlay = [...document.querySelectorAll('.modal, .sheet')]
        .find((m) => !m.hidden);
      if (overlay) { overlay.hidden = true; return; }
      if (grid.selection.size) { grid.clearSelection(); return; }
      if (typing) target.blur();
      return;
    }
    if (typing) return;
    if (event.altKey || ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() !== 'a')) return;

    if (key === '?') { $('#help-modal').hidden = false; return; }
    if (key === 't') { cycleTheme(); return; }
    if (key === 'j') { setLayout('justified'); return; }
    if (key === 'm') { setLayout('masonry'); return; }
    if (key === 'g') { setLayout('grid'); return; }
    if (key === 'f' && !event.metaKey && !event.ctrlKey) { setLayout('film'); return; }
    if (key === 'a' && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      grid.selectAll();
      return;
    }
    if (key === '+' || key === '=') {
      $('#zoom').value = String(grid.zoom + 1);
      grid.setZoom(grid.zoom + 1);
      store.set('zoom', grid.zoom);
      return;
    }
    if (key === '-') {
      $('#zoom').value = String(grid.zoom - 1);
      grid.setZoom(grid.zoom - 1);
      store.set('zoom', grid.zoom);
      return;
    }

    const moves = {
      arrowright: 'next', arrowleft: 'prev', arrowdown: 'down', arrowup: 'up',
    };
    if (moves[key]) {
      event.preventDefault();
      // Shift takes the selection along with the cursor, the way it does in
      // Explorer and in Photos.
      grid.moveCursor(moves[key], { extend: event.shiftKey });
      return;
    }
    if (event.key === 'Enter' && grid.cursor >= 0) {
      viewer.open(grid.ids, grid.cursor, grid.thumbedIds());
      return;
    }
    if (key === 'x' && grid.cursor >= 0) {
      const id = grid.currentId();
      if (grid.selection.has(id)) grid.selection.delete(id);
      else grid.selection.add(id);
      grid.emitSelection();
      grid.render();
    }
  });
}

/* ========================================================================
   Progress + toasts
   ======================================================================== */

let lastPhase = null;
let progressPoll = null;
let countsAskedAt = 0;

// The item counts in the sidebar climb as a scan adds files, but counting them
// means counting the library, so while a scan runs they are brought up to date
// on this slower cadence and the strip itself asks only for the counters.
const COUNTS_EVERY_MS = 30000;


// Coming back to the tab is the moment to catch up on what happened while it
// was in the background, and to restart the loop that visibility stopped.
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) refreshStatus();
});

// Ask now, and let the answer set the next tick's cadence.
function kickActivity() {
  clearTimeout(progressPoll);
  pollProgress();
}

async function pollProgress() {
  let activity;
  try {
    activity = await fetchActivity();
  } catch (err) {
    // Offline has its own reconnect loop, and it ends in refreshStatus(),
    // which starts this one again.
    if (isNetworkFailure(err)) {
      setServerOffline();
      return;
    }
    progressPoll = setTimeout(pollProgress, 10000);
    return;
  }
  onActivity(activity);
}

// Everything Ninaivu is doing, not only the scan.
//
// The strip used to be one bar, and it was the library scan. Anything else
// that held the disk — a consolidation, a cloud upload, a storage check, a
// straightening pass — ran with nothing on screen to say so, and a machine
// with its fan up had no answer here for why. Each of those is now a line.
function onActivity(activity) {
  if (!activity) return;
  const scan = activity.scan;
  const strip = $('#scan-strip');
  // Guests never see it: the strip is hidden for them on sign-in, and a poll
  // that arrives afterwards must not put it back. The server sends them an
  // empty list anyway — which drives are being read is not their business —
  // and this is the same rule stated where the strip is drawn.
  const allowed = Boolean(state.user?.can?.manage_library);
  const running = Boolean(activity.running);
  strip.hidden = !running || !allowed;

  // Not clickable here. The console page that owns each job is not somewhere
  // the family app can send anybody, so the strip says what is happening and
  // leaves stopping it to the console.
  renderActivity($('#job-rows'), allowed ? activity.jobs : []);

  // Keep the strip moving.
  //
  // Progress is pushed over an event stream on the console, but that endpoint
  // is an admin route and does not exist on this port at all — so here the
  // only way to see work advance is to ask.
  //
  // Two cadences: quick while something is running, slow otherwise so a job
  // started from the console still turns up here within a few seconds. Both
  // stop when the tab is not being looked at, because a household leaves this
  // open on five devices and none of them should be asking about a library
  // nobody is watching; coming back to the tab starts them again.
  //
  // Each tick asks for counters only. It used to ask /api/status, which counts
  // the whole library — every two seconds, for the length of the scan, against
  // the database the scan was writing to.
  clearTimeout(progressPoll);
  if (!document.hidden) {
    progressPoll = setTimeout(pollProgress, running ? 2000 : 10000);
    if (running && Date.now() - countsAskedAt > COUNTS_EVERY_MS) {
    countsAskedAt = Date.now();
    refreshCounts();          // the numbers climb as a scan adds files
  }
  }

  if (!scan) return;
  // Refresh the gallery when a phase completes, not on every tick.
  const phase = `${scan.status}:${scan.added}:${scan.removed}`;
  if (phase !== lastPhase && (scan.status === 'done' || scan.status === 'error')) {
    lastPhase = phase;
    refreshStatus();
    refreshFacets();
    if (!state.loading) reload();
    if (scan.status === 'done' && (scan.added || scan.removed)) {
      toast(`${scan.added.toLocaleString()} added, ${scan.removed.toLocaleString()} removed.`);
    }
  } else if (scan.status === 'tagging' && scan.tagged && scan.tagged % 200 === 0) {
    refreshFacets();
  }
}

function toast(message, isError = false) {
  // Nothing to say while the screen is locked: nobody is there to read it,
  // and requests refused behind the lock would pile their errors up here.
  if (document.body.classList.contains('screen-locked')) return;
  const node = document.createElement('div');
  node.className = `toast${isError ? ' error' : ''}`;
  node.textContent = message;
  $('#toasts').appendChild(node);
  setTimeout(() => {
    node.style.opacity = '0';
    setTimeout(() => node.remove(), 250);
  }, isError ? 5200 : 2800);
}

function humanBytes(n) {
  if (!n) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  let value = n;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i++;
  }
  return `${i === 0 ? value : value.toFixed(1)} ${units[i]}`;
}

/* ========================================================================
   Ninaivu 5.0 Features: Memories, Geo Map, Upload, and Sharing
   ======================================================================== */

let activeMemories = [];

async function loadMemories() {
  activeMemories = [];
  if (!state.status?.has_library) return;
  try {
    const data = await api.memories();
    const banner = $('#memories-banner');
    const carousel = $('#memories-carousel');
    const navMem = $('#nav-memories');
    if (!data.total || !data.items?.length) {
      if (banner) banner.hidden = true;
      if ($('#count-memories')) $('#count-memories').textContent = '';
      if (navMem && !navMem.classList.contains('active')) navMem.hidden = true;
      return;
    }
    if (navMem) navMem.hidden = false;
    activeMemories = data.items;
    const countMem = $('#count-memories');
    if (countMem) countMem.textContent = String(data.total);
    if (!banner || !carousel) return;

    banner.hidden = false;
    carousel.innerHTML = '';
    const nowYear = new Date().getFullYear();

    data.items.slice(0, 16).forEach((item) => {
      const card = document.createElement('div');
      card.className = 'memory-card';
      const itemYear = item.date ? parseInt(item.date.slice(0, 4), 10) : nowYear;
      const diff = nowYear - itemYear;
      const diffLabel = diff > 0 ? `${diff} year${diff > 1 ? 's' : ''} ago` : 'This year';

      const img = document.createElement('img');
      img.src = thumbUrl(item.id, 320, item.thumb_v);
      img.alt = item.name || '';
      img.loading = 'lazy';
      const badge = document.createElement('span');
      badge.className = 'memory-badge';
      badge.textContent = diffLabel;
      const meta = document.createElement('div');
      meta.className = 'memory-meta';
      meta.textContent = `${item.date || ''} · ${item.city || item.name || ''}`;
      card.append(img, badge, meta);
      card.onclick = () => {
        const memoryIds = activeMemories.map((m) => m.id);
        const idx = memoryIds.indexOf(item.id);
        viewer.open(memoryIds, Math.max(0, idx));
      };
      carousel.appendChild(card);
    });
  } catch (err) {
    console.debug('Memories load note:', err);
  }
}

function wireMemories() {
  $('#memories-view-all')?.addEventListener('click', () => {
    if (activeMemories.length) {
      viewer.open(activeMemories.map((m) => m.id), 0);
    }
  });
}

/* -- Geo Map ------------------------------------------------------------ */
let leafletMap = null;
let leafletLoading = null;

/**
 * Fetch Leaflet the first time a map is actually opened.
 *
 * It is 147 KB of the app's JavaScript and a stylesheet besides, and it was
 * loading on every visit to the gallery whether or not anybody went near the
 * map — including on phones, on the household's own broadband, before a
 * single photograph appeared.
 */
function loadLeaflet() {
  if (window.L) return Promise.resolve();
  if (leafletLoading) return leafletLoading;
  leafletLoading = new Promise((resolve, reject) => {
    const css = document.createElement('link');
    css.rel = 'stylesheet';
    css.href = '/static/css/leaflet.css';
    document.head.appendChild(css);
    const script = document.createElement('script');
    script.src = '/static/js/leaflet.js';
    script.onload = () => resolve();
    script.onerror = () => reject(new Error(i18n.t('The map could not be loaded.')));
    document.head.appendChild(script);
  });
  return leafletLoading;
}
let worldOutline = null;

/* Whether the household has asked for street tiles.
 *
 * Off is the default, and it is the only setting in Ninaivu that decides
 * whether anything about the library leaves the machine. A tile server is
 * told which square of the world is on screen, one request per tile — open
 * the map on last summer's holiday and somebody else's logs know where the
 * family spent it. So the map is drawn from an outline that ships with
 * Ninaivu instead, and the tiles are something a household turns on knowing
 * what it is trading.
 */
function wantsTiles() {
  return document.body.dataset.mapTiles === '1';
}

/* The world, from a file in this repository.
 *
 * Natural Earth's 1:110m coastlines and borders, public domain, about 160 KB,
 * fetched once and kept. It draws which country and which coast, which is
 * what a map of a family library is usually being asked. It has no streets,
 * and past roughly a city's width there is nothing left in it to draw — the
 * pins are then their own map, which for photographs taken in one town is
 * what you were looking at anyway.
 */
async function loadWorld() {
  if (worldOutline) return worldOutline;
  const answer = await fetch('/static/data/world.geojson');
  if (!answer.ok) throw new Error(i18n.t('The world outline could not be loaded.'));
  worldOutline = await answer.json();
  return worldOutline;
}

async function drawBasemap(map) {
  if (wantsTiles()) {
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 18,
      attribution: '\u00a9 OpenStreetMap contributors',
    }).addTo(map);
    return;
  }
  try {
    L.geoJSON(await loadWorld(), {
      interactive: false,
      // Colours live in the stylesheet so the map follows the theme rather
      // than staying daylight-coloured in a dark room.
      style: () => ({ className: 'world-shape' }),
      attribution: i18n.t('Outline: Natural Earth'),
    }).addTo(map);
  } catch (err) {
    // A map of pins on an empty ground is still a map. Failing to draw the
    // sea is not a reason to show the household nothing.
    console.warn(err);
  }
}

/* The map's state. It asked for up to 1,500 photographs and drew one pin
   each, and read `gps_lat` from answers that said `lat`, so it drew none.
   Now the server groups what is on screen (storage/geo.py): every photograph
   with a location is on the map, a busy place is one circle with a count,
   and a circle opens to all of its photographs. */
const mapView = {
  year: '', person: '', trip: false,
  groups: null, route: null, here: null,
  places: null, openCountries: new Set(),
  seq: 0, timer: null, returning: false,
};

function mapQuery(extra = {}) {
  const query = { ...extra };
  if (mapView.year) query.year = mapView.year;
  if (mapView.person) query.person = mapView.person;
  return query;
}

/* A country's name from its code, in the page's language, by the browser. */
function countryName(code) {
  if (!code) return '';
  try {
    return new Intl.DisplayNames([i18n.language(), 'en'], { type: 'region' }).of(code) || code;
  } catch {
    return code;
  }
}

function placeLabel(city, country) {
  return [city, countryName(country)].filter(Boolean).join(', ') || i18n.t('Unnamed places');
}

function shortCount(n) {
  if (n < 1000) return String(n);
  return `${(n / 1000).toFixed(n < 10000 ? 1 : 0)}k`;
}

function dateRange(first, last) {
  return first && last && first !== last ? `${first} – ${last}` : (first || last || '');
}

/** Frame [south, north, west, east] on the map. */
function fitTo(bounds, maxZoom = 14) {
  if (!leafletMap || !bounds) return;
  const [south, north, west, east] = bounds;
  leafletMap.fitBounds([[south, west], [north, east]], { padding: [40, 40], maxZoom });
}

function wireMap() {
  $('#map-btn')?.addEventListener('click', () => openMap());
  $('#map-close')?.addEventListener('click', closeMap);
  $('#map-year')?.addEventListener('change', (event) => {
    mapView.year = event.target.value;
    if (!mapView.year && mapView.trip) setTrip(false);
    refreshMap();
  });
  $('#map-person')?.addEventListener('change', (event) => {
    mapView.person = event.target.value;
    refreshMap();
  });
  $('#map-trip')?.addEventListener('click', () => setTrip(!mapView.trip));
  $('#map-places-toggle')?.addEventListener('click', () => {
    const open = $('#map-places').classList.toggle('open');
    $('#map-places-toggle').setAttribute('aria-expanded', String(open));
    setTimeout(() => leafletMap?.invalidateSize(), 50);
  });
  // A photograph opened from the map goes back to the map when it closes.
  viewer.addEventListener('close', () => {
    if (!mapView.returning) return;
    mapView.returning = false;
    $('#map-modal').hidden = false;
    setTimeout(() => leafletMap?.invalidateSize(), 50);
  });
  viewer.addEventListener('map', (event) => {
    const item = event.detail?.item;
    if (!item?.gps) return;
    viewer.close();
    openMap({ focus: { lat: item.gps[0], lon: item.gps[1] } });
  });
  viewer.addEventListener('nearby', (event) => {
    const item = event.detail?.item;
    if (!item?.gps) return;
    viewer.close();
    state.filters.near = item.id;
    state.nearLabel = item.city || '';
    reload({ resetScroll: true });
  });
}

function closeMap() {
  mapView.returning = false;
  $('#map-modal').hidden = true;
}

async function openMap({ focus = null } = {}) {
  const modal = $('#map-modal');
  try {
    await loadLeaflet();
  } catch (err) {
    toast(err.message, true);
    return;
  }
  modal.hidden = false;
  if (typeof L === 'undefined') return;

  if (!leafletMap) {
    leafletMap = L.map('leaflet-map', { worldCopyJump: true }).setView([20, 0], 2);
    await drawBasemap(leafletMap);
    mapView.groups = L.layerGroup().addTo(leafletMap);
    mapView.route = L.layerGroup().addTo(leafletMap);
    mapView.here = L.layerGroup().addTo(leafletMap);
    leafletMap.on('moveend', () => {
      clearTimeout(mapView.timer);
      mapView.timer = setTimeout(refreshGroups, 120);
    });
  }
  // Its size is only known once the dialog is showing.
  setTimeout(() => leafletMap?.invalidateSize(), 50);
  setTimeout(() => leafletMap?.invalidateSize(), 250);

  // Opened from a photograph, the map shows it: a year or a person chosen
  // earlier would have filtered out the very photograph it was opened on.
  if (focus) {
    mapView.year = '';
    mapView.person = '';
    if (mapView.trip) setTrip(false);
    const person = $('#map-person');
    if (person) person.value = '';
  }
  fillPeople();
  mapView.here.clearLayers();
  const places = await loadPlaces();
  if (focus) {
    leafletMap.setView([focus.lat, focus.lon], 15);
    mapView.here.addLayer(L.circleMarker([focus.lat, focus.lon],
      { radius: 34, className: 'map-here', interactive: false }));
  } else if (places?.bounds) {
    fitTo(places.bounds);
  } else if (places && !places.total) {
    toast(i18n.t('No photos with GPS coordinates found yet.'));
  }
  refreshGroups();
}

async function refreshMap() {
  await loadPlaces();
  refreshGroups();
  if (mapView.trip) loadRoute();
}

function fillPeople() {
  const select = $('#map-person');
  if (!select) return;
  const people = (state.people || []).filter((person) => person.name);
  select.hidden = !people.length;
  const signature = people.map((person) => person.id).join(',');
  if (select.dataset.signature === signature) return;
  select.dataset.signature = signature;
  select.length = 1;
  for (const person of people) select.add(new Option(person.name, String(person.id)));
  select.value = mapView.person;
}

function fillYears(years) {
  const select = $('#map-year');
  if (!select) return;
  const signature = (years || []).join(',');
  if (select.dataset.signature !== signature) {
    select.dataset.signature = signature;
    select.length = 1;
    for (const year of years || []) select.add(new Option(String(year), String(year)));
  }
  select.value = mapView.year;
}

async function loadPlaces() {
  let data;
  try {
    data = await api.geoPlaces(mapQuery());
  } catch (err) {
    toast(i18n.t('Could not load map points: {reason}', { reason: err.message }), true);
    return null;
  }
  mapView.places = data;
  fillYears(data.years);
  renderPlaces(data);
  return data;
}

/* The places, country by country, most photographed first. A country opens
   to its cities; a city frames the map, and its button opens its photos. */
function renderPlaces(data) {
  const list = $('#map-places-list');
  if (!list || !data) return;
  list.innerHTML = '';
  $('#map-places-summary').textContent = i18n.t('{count} photographs in {places} places',
    { count: data.total.toLocaleString(), places: data.places.length.toLocaleString() });

  const countries = new Map();
  for (const place of data.places) {
    const country = countries.get(place.country)
      || { code: place.country, count: 0, places: [], bounds: null };
    country.count += place.count;
    country.places.push(place);
    const [s, n, w, e] = place.bounds;
    country.bounds = country.bounds
      ? [Math.min(country.bounds[0], s), Math.max(country.bounds[1], n),
        Math.min(country.bounds[2], w), Math.max(country.bounds[3], e)]
      : [s, n, w, e];
    countries.set(place.country, country);
  }

  for (const country of [...countries.values()].sort((a, b) => b.count - a.count)) {
    const open = mapView.openCountries.has(country.code);
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'map-place map-country';
    row.setAttribute('aria-expanded', String(open));
    const toggle = document.createElement('span');
    toggle.className = 'map-place-toggle';
    toggle.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 6 6 6-6 6"/></svg>';
    const name = document.createElement('span');
    name.className = 'map-place-name';
    name.textContent = country.code ? countryName(country.code) : i18n.t('Unnamed places');
    const count = document.createElement('span');
    count.className = 'map-place-count';
    count.textContent = country.count.toLocaleString();
    row.append(toggle, name, count);
    row.onclick = () => {
      if (open) mapView.openCountries.delete(country.code);
      else mapView.openCountries.add(country.code);
      renderPlaces(mapView.places);
      if (!open) fitTo(country.bounds, 8);
    };
    list.appendChild(row);
    if (!open) continue;

    for (const place of country.places) {
      const city = document.createElement('div');
      city.className = 'map-place map-city';
      const go = document.createElement('button');
      go.type = 'button';
      go.className = 'map-place-name';
      go.textContent = place.city || i18n.t('Unnamed places');
      go.onclick = () => fitTo(place.bounds);
      const tally = document.createElement('span');
      tally.className = 'map-place-count';
      tally.textContent = place.count.toLocaleString();
      const view = document.createElement('button');
      view.type = 'button';
      view.className = 'icon-btn map-place-view';
      view.title = i18n.t('View photos');
      view.setAttribute('aria-label', `${i18n.t('View photos')}: ${go.textContent}`);
      view.innerHTML = '<svg viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="9" cy="10" r="1.6"/><path d="m21 16-5-5-8 8"/></svg>';
      view.onclick = () => openPlacePhotos({ country: place.country, city: place.city });
      city.append(go, tally, view);
      list.appendChild(city);
    }
  }
}

async function refreshGroups() {
  if (!leafletMap || $('#map-modal').hidden) return;
  const seq = ++mapView.seq;
  const bounds = leafletMap.getBounds();
  let data;
  try {
    data = await api.geoClusters(mapQuery({
      south: bounds.getSouth(), north: bounds.getNorth(),
      west: bounds.getWest(), east: bounds.getEast(), zoom: leafletMap.getZoom(),
    }));
  } catch (err) {
    if (seq === mapView.seq) {
      toast(i18n.t('Could not load map points: {reason}', { reason: err.message }), true);
    }
    return;
  }
  if (seq !== mapView.seq) return;           // the map moved on while this was asked
  mapView.groups.clearLayers();
  for (const group of mergeOverlapping(data.clusters || [])) {
    mapView.groups.addLayer(groupMarker(group));
  }
}

/* The server groups by a grid, and a town on a grid line came back as two
   circles on top of each other. Circles nearer than this on screen become
   one; it keeps each part's own bounds, so opening it opens exactly its
   photographs and none from between them. */
const MERGE_PX = 44;

function mergeOverlapping(groups) {
  const merged = [];
  for (const group of [...groups].sort((a, b) => b.count - a.count)) {
    const at = leafletMap.latLngToLayerPoint([group.lat, group.lon]);
    const near = merged.find((other) => other.at.distanceTo(at) < MERGE_PX);
    if (!near) {
      merged.push({ ...group, at, parts: [group.bounds] });
      continue;
    }
    const total = near.count + group.count;
    near.lat = (near.lat * near.count + group.lat * group.count) / total;
    near.lon = (near.lon * near.count + group.lon * group.count) / total;
    near.count = total;
    near.parts.push(group.bounds);
    const [s, n, w, e] = group.bounds;
    near.bounds = [Math.min(near.bounds[0], s), Math.max(near.bounds[1], n),
      Math.min(near.bounds[2], w), Math.max(near.bounds[3], e)];
  }
  return merged;
}

function groupMarker(group) {
  const many = group.count > 1;
  const html = `<div class="map-cluster"><img src="${thumbUrl(group.cover, 160)}" alt="" loading="lazy">`
    + (many ? `<span class="map-count">${shortCount(group.count)}</span>` : '') + '</div>';
  const icon = L.divIcon({ className: 'map-cluster-icon', html, iconSize: [52, 52], iconAnchor: [26, 26] });
  const marker = L.marker([group.lat, group.lon], { icon, title: placeLabel(group.city, group.country) });
  if (many) {
    // Bound once, drawn when opened. Bound in the click itself, Leaflet's own
    // toggle ran in the same click and closed the popup it had just opened.
    marker.bindPopup(() => groupPopup(group, marker), { minWidth: 200 });
  } else {
    marker.on('click', () => openMapPhotos([group.cover]));
  }
  return marker;
}

function groupPopup(group, marker) {
  const box = document.createElement('div');
  box.className = 'map-popup';
  const img = document.createElement('img');
  img.src = thumbUrl(group.cover, 320);
  img.alt = '';
  const title = document.createElement('strong');
  title.textContent = placeLabel(group.city, group.country);
  const count = document.createElement('span');
  count.className = 'hint';
  count.textContent = i18n.t('{count} photographs', { count: group.count.toLocaleString() });
  const actions = document.createElement('div');
  actions.className = 'map-popup-actions';
  const view = document.createElement('button');
  view.type = 'button';
  view.className = 'btn primary small';
  view.textContent = i18n.t('View photos');
  view.onclick = () => {
    marker.closePopup();
    openGroupPhotos(group.parts || [group.bounds]);
  };
  const zoom = document.createElement('button');
  zoom.type = 'button';
  zoom.className = 'btn ghost small';
  zoom.textContent = i18n.t('Zoom in');
  zoom.onclick = () => {
    marker.closePopup();
    fitTo(group.bounds, Math.min(18, leafletMap.getZoom() + 4));
  };
  actions.append(view, zoom);
  box.append(img, title, count, actions);
  return box;
}

/* A circle's photographs: each part it was made of, asked for on its own. */
async function openGroupPhotos(parts) {
  let answers;
  try {
    answers = await Promise.all(parts.map(([south, north, west, east]) =>
      api.geoPhotos(mapQuery({ south, north, west, east }))));
  } catch (err) {
    toast(err.message, true);
    return;
  }
  const ids = [...new Set(answers.flatMap((answer) => answer.ids || []))];
  const total = answers.reduce((sum, answer) => sum + (answer.total || 0), 0);
  if (!ids.length) return;
  if (total > ids.length) {
    toast(i18n.t('Showing the newest {shown} of {total}',
      { shown: ids.length.toLocaleString(), total: total.toLocaleString() }));
  }
  openMapPhotos(ids);
}

async function openPlacePhotos(query) {
  let data;
  try {
    data = await api.geoPhotos(mapQuery(query));
  } catch (err) {
    toast(err.message, true);
    return;
  }
  if (!data.ids?.length) return;
  if (data.total > data.ids.length) {
    toast(i18n.t('Showing the newest {shown} of {total}',
      { shown: data.ids.length.toLocaleString(), total: data.total.toLocaleString() }));
  }
  openMapPhotos(data.ids);
}

/* The viewer is drawn beneath dialogs, so the map steps aside while a
   photograph is open, and comes back when it closes. */
function openMapPhotos(ids) {
  mapView.returning = true;
  $('#map-modal').hidden = true;
  viewer.open(ids, 0);
}

function setTrip(on) {
  if (on && !mapView.year) {
    toast(i18n.t('Choose a year to see its trips.'));
    return;
  }
  mapView.trip = on;
  const button = $('#map-trip');
  button?.setAttribute('aria-pressed', String(on));
  button?.classList.toggle('active', on);
  if (on) loadRoute();
  else mapView.route?.clearLayers();
}

/* A year's photographs in the order they were taken, as a route of stops. */
async function loadRoute() {
  mapView.route.clearLayers();
  if (!mapView.trip || !mapView.year) return;
  let data;
  try {
    data = await api.geoTrail(mapQuery());
  } catch (err) {
    toast(err.message, true);
    return;
  }
  const stops = data.stops || [];
  if (!stops.length) {
    toast(i18n.t('No photos with locations in {year}.', { year: mapView.year }));
    return;
  }
  const line = L.polyline(stops.map((stop) => [stop.lat, stop.lon]),
    { className: 'map-trail', interactive: false });
  mapView.route.addLayer(line);
  stops.forEach((stop, index) => {
    const dot = L.circleMarker([stop.lat, stop.lon], { radius: 6, className: 'map-stop' });
    const tip = document.createElement('span');
    tip.textContent = `${index + 1}. ${placeLabel(stop.city, stop.country)} · `
      + `${dateRange(stop.first, stop.last)} · ${i18n.t('{count} photographs', { count: stop.count })}`;
    dot.bindTooltip(tip);
    dot.on('click', () => openMapPhotos(stop.ids));
    mapView.route.addLayer(dot);
  });
  leafletMap.fitBounds(line.getBounds(), { padding: [40, 40], maxZoom: 12 });
}

/* -- Local Upload ------------------------------------------------------- */
function wireUpload() {
  const modal = $('#upload-modal');
  const btn = $('#upload-btn');
  const dropzone = $('#upload-dropzone');
  const fileInput = $('#upload-file-input');
  const closeBtn = $('#upload-close');

  btn?.addEventListener('click', () => { modal.hidden = false; });
  closeBtn?.addEventListener('click', () => {
    modal.hidden = true;
    reload();
  });

  dropzone?.addEventListener('click', () => fileInput?.click());
  fileInput?.addEventListener('change', (e) => {
    if (e.target.files?.length) handleUpload(Array.from(e.target.files));
  });

  dropzone?.addEventListener('dragover', (e) => {
    e.preventDefault();
    dropzone.classList.add('dragover');
  });
  dropzone?.addEventListener('dragleave', () => dropzone.classList.remove('dragover'));
  dropzone?.addEventListener('drop', (e) => {
    e.preventDefault();
    dropzone.classList.remove('dragover');
    if (e.dataTransfer.files?.length) {
      handleUpload(Array.from(e.dataTransfer.files));
    }
  });
}

async function handleUpload(files) {
  if (!files.length) return;
  const progress = $('#upload-progress');
  const fill = $('#upload-fill');
  const list = $('#upload-list');
  progress.hidden = false;
  fill.style.width = '20%';

  const formData = new FormData();
  files.forEach((f) => formData.append('files', f));

  try {
    fill.style.width = '60%';
    const response = await fetch('/api/upload', {
      method: 'POST',
      body: formData,
    });
    const result = await response.json();
    fill.style.width = '100%';

    if (!response.ok) throw new Error(result.error || i18n.t('Upload failed'));

    const count = result.total ?? result.uploaded?.length ?? 0;
    const failed = files.length - count;
    toast(`Uploaded ${count} file${count === 1 ? '' : 's'}${result.pending_approval ? ' for admin approval' : ''}.${failed > 0 ? ` ${failed} could not be uploaded.` : ''}`, failed > 0);
    for (const error of result.errors || []) toast(`${error.filename}: ${error.error}`, true);
    if (count && !result.pending_approval) reload();
    $('#upload-file-input').value = '';
    if (list) {
      (result.uploaded || []).forEach((u) => {
        const item = document.createElement('div');
        item.className = 'upload-item';
        // Built as nodes, not markup: a filename is whatever the person
        // uploading chose to call the file, and every other line in this
        // codebase treats such text as text.
        const tick = document.createElement('span');
        tick.append('✓ ');
        const name = document.createElement('strong');
        name.textContent = u.filename;
        tick.appendChild(name);
        const where = document.createElement('span');
        where.className = 'hint';
        where.textContent = result.pending_approval ? i18n.t('Awaiting admin approval') : u.rel_path;
        item.replaceChildren(tick, where);
        list.prepend(item);
      });
    }
    setTimeout(() => { progress.hidden = true; }, 1200);
  } catch (err) {
    toast(i18n.t('Upload failed: {reason}', { reason: err.message }), true);
    progress.hidden = true;
  }
}

/* -- Sharing ------------------------------------------------------------ */
let shareTargetItem = null;

function wireSharing() {
  viewer.addEventListener('share', (e) => {
    shareTargetItem = e.detail.item;
    openShareModal(shareTargetItem);
  });

  $('#share-close')?.addEventListener('click', () => {
    $('#share-modal').hidden = true;
  });

  $('#share-create-btn')?.addEventListener('click', async () => {
    const button = $('#share-create-btn');
    if (!shareTargetItem || button.disabled) return;
    const target = shareTargetItem;
    button.disabled = true;
    const expiry = Number($('#share-expiry').value || 30);
    const pwd = $('#share-password').value.trim() || null;
    const scope = target.type === 'album' ? 'album' : 'asset';
    try {
      const res = await api.createShare(scope, target.id, expiry, pwd);
      if (shareTargetItem !== target) return;
      const fullUrl = `${window.location.origin}${res.share_url}`;
      $('#share-url-text').value = fullUrl;
      $('#share-link-result').hidden = false;
      $('#share-options').hidden = true;
      $('#share-create-btn').hidden = true;
      toast(i18n.t('Share link generated!'));
    } catch (err) {
      toast(i18n.t('Could not create share link: {reason}', { reason: err.message }), true);
    } finally {
      button.disabled = false;
    }
  });

  $('#share-copy-btn')?.addEventListener('click', async () => {
    const input = $('#share-url-text');
    input.select();
    try {
      if (!navigator.clipboard?.writeText) throw new Error(i18n.t('Clipboard unavailable'));
      await navigator.clipboard.writeText(input.value);
      toast(i18n.t('Link copied to clipboard!'));
    } catch {
      input.focus();
      input.select();
      toast(i18n.t('Could not copy automatically. Copy the selected link manually.'), true);
    }
  });
}

function openShareModal(item) {
  shareTargetItem = item;
  $('#share-link-result').hidden = true;
  $('#share-options').hidden = false;
  $('#share-create-btn').hidden = false;
  $('#share-password').value = '';
  if (item?.type === 'album') {
    $('#share-title').textContent = `Share Album: ${item.name}`;
  } else {
    $('#share-title').textContent = i18n.t('Share with Family & Friends');
  }
  $('#share-modal').hidden = false;
}

/* ======================================================================== */

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
