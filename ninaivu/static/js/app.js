/** Application shell: state, filters, chrome, and everything wired together. */

import {
  api, forgetInflight, onUnauthorized, sessionRestored, setAnonymousViewer, thumbUrl,
} from './api.js';
import { fetchActivity, renderActivity } from './activity.js';
import * as i18n from './i18n.js';
import { Grid, mergeSegments } from './grid.js';
import { Viewer } from './viewer.js';
import { MODES, scrubberTicks, sectionAt } from './layout.js';
import {
  accountsApi, avatarNode, Gate, ProfileSheet,
} from './accounts.js';
import { enterPressesTheButton } from './enter-key.js';
import { PhoneBackup } from './phone-backup.js';
import { PrintScan } from './print-scan.js';
import { FindPrint } from './find-print.js';
import { ScreenLock } from './lock.js';
import { DowntimeNotice } from './downtime.js';
import { familyCommands, initPalette } from './palette.js';
import { hideScrollBarsUntilUsed } from './scrollbars.js';
import { claimBanner } from './handover.js';
import { VideoMoments } from './video-moments.js';
import { state, store } from './store.js';
import { $, closeMenus, humanBytes, toast, wireMenu, wireModalFocus } from './ui.js';
import {
  clearFilters, connectSidebar, loadAlbums, loadOccasions, loadPeople, occasionTitle,
  openStudioFor, renderAlbums, renderFilterBar, renderFolders, renderOccasions,
  renderTagCloud, renderYears, showPerson, syncChips, wireAlbums,
} from './gallery/sidebar.js';
import { connectSelection, deleteSelected, wireSelection } from './gallery/selection.js';
import { connectSearch, showVoiceSearch, stopSearch, wireSearch } from './gallery/search.js';

/* The family's moments and the things it makes (the frame, time capsules,
 * books, films, Our year, Then and now, a life story, festivals, young faces,
 * Ask Ninaivu, asking the family and the family tree) are fetched the first
 * time one is opened, not with the gallery: about 200 KB a phone no longer
 * downloads before the first photograph. Each import() is written out in
 * full so the server names it at its stamped URL (server/modules.py). */
const features = {
  askFamily: () => import('./ask-family.js'),
  familyTree: () => import('./family-tree.js'),
  books: () => import('./books.js'),
  films: () => import('./films.js'),
  ourYear: () => import('./our-year.js'),
  lifeStory: () => import('./life-story.js'),
  festivals: () => import('./festivals.js'),
  youngFaces: () => import('./young-faces.js'),
  thenAndNow: () => import('./then-and-now.js'),
  frame: () => import('./frame.js'),
  capsules: () => import('./capsules.js'),
  ask: () => import('./ask-ninaivu.js'),
};

/** *name* from feature *which*, fetched when first called; a fetch that
 *  fails (the server restarting) is said, and asked again next time. */
function later(which, name) {
  return (...args) => features[which]().then((module) => module[name](...args), (err) => {
    toast(err.message, true);
    return undefined;
  });
}

const openAskFamily = later('askFamily', 'openAskFamily');
const openFamilyTree = later('familyTree', 'openFamilyTree');
const openBookSheet = later('books', 'openBookSheet');
const openBooksList = later('books', 'openBooksList');
const filmsAvailable = later('films', 'filmsAvailable');
const openFilmSheet = later('films', 'openFilmSheet');
const openOurYear = later('ourYear', 'openOurYear');
const openLifeStory = later('lifeStory', 'openLifeStory');
const openFestivals = later('festivals', 'openFestivals');
const openYoungFaces = later('youngFaces', 'openYoungFaces');
const openThenAndNow = later('thenAndNow', 'openThenAndNow');
const showThenNowStrip = later('thenAndNow', 'showThenNowStrip');
const openFrame = later('frame', 'openFrame');
const announceCapsules = later('capsules', 'announceCapsules');
const openCapsules = later('capsules', 'openCapsules');
const openSealSheet = later('capsules', 'openSealSheet');
const openAsk = later('ask', 'openAsk');

/** The photo frame asked for by the page's address (frame.js says the same). */
const frameAsked = () => location.hash === '#frame';

/* The search box (gallery/search.js) asks the gallery to load again. */
connectSearch({ reload: (options) => reload(options) });

/* The selection bar (gallery/selection.js) too. */
connectSelection({
  grid: () => grid,
  reload: (options) => reload(options),
  refreshStatus: () => refreshStatus(),
});

/* The sidebar (gallery/sidebar.js) is given what it needs of the gallery. */
connectSidebar({
  grid: () => grid,
  viewer: () => viewer,
  reload: (options) => reload(options),
  sidebarAsk: (ask) => sidebarAsk(ask),
  openItems: (ids, index) => openItems(ids, index),
  openPage: (name) => openPage(name),
  stopSearch,
  features: {
    openAskFamily, openFamilyTree, openBookSheet, openBooksList, filmsAvailable, openFilmSheet,
    openLifeStory,
  },
});

/** Then and now's strip hidden, without fetching it to hide what it never drew. */
function hideThenNowStrip() {
  const banner = document.getElementById('then-now-banner');
  if (banner) banner.hidden = true;
}

// What the Primary is called on the Secondary's copy when the pairing does
// not name it (replica/front.py), so a Tamil page says it in Tamil.
i18n.translateNames([i18n.key('the main computer')]);

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
  // Before any request: the first one refused for maintenance shows the notice.
  new DowntimeNotice();
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
  initPalette({ commands: paletteCommands, typed: paletteSearch });
  hideScrollBarsUntilUsed();

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
  navigator.serviceWorker.register('/sw.js').catch(dropStaleWorkers);
}

/* The browser would not fetch the worker. Most often that is a certificate it
   does not trust: a new computer, or a fresh portable copy with its own. A
   worker left from before then answers the home page with "Ninaivu is
   Offline" while the server is up, because it cannot reach the server either,
   so it is let go; a later visit that can fetch the worker registers it again. */
function dropStaleWorkers() {
  navigator.serviceWorker.getRegistrations()
    .then((all) => all.forEach((registration) => registration.unregister()))
    .catch(() => {});
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
  // And on plain HTTP — the default — there is no certificate at all.
  const certHint = $('#help-cert');
  if (certHint) certHint.hidden = onTailnet() || location.protocol !== 'https:';
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

function setServerOffline() {
  // Whatever else is true, the cover comes off: a boot overlay left over a
  // page that is telling somebody the server is unreachable hides the one
  // thing they need to read.
  bootDone();
  serverIsOffline = true;

  const banner = $('#offline-banner');
  if (banner) banner.hidden = false;
  const bannerText = $('#offline-banner-text');
  if (bannerText) bannerText.textContent = i18n.t('Can’t reach Ninaivu at {host}. Reconnecting automatically…', { host: location.host });

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
    // A certificate is a possible cause only on HTTPS; on plain HTTP, the
    // default, it is the server or the network.
    const secure = location.protocol === 'https:';
    if (text) text.textContent = away
      ? `This device can’t reach Ninaivu at ${location.host}. Away from home it is reached through Tailscale, so check that Tailscale is on and connected on this device.`
      : secure
        ? `This browser can’t reach Ninaivu at ${location.host}. Either the Ninaivu server is stopped, or this browser doesn’t trust Ninaivu’s HTTPS certificate.`
        : `This browser can’t reach Ninaivu at ${location.host}. The Ninaivu server may be stopped, or this device may not be on the home network.`;
    const hint = $('#empty-hint');
    if (hint) {
      hint.hidden = false;
      hint.textContent = away
        ? i18n.t('Open the Tailscale app, connect, then retry. If Tailscale is connected, the Ninaivu server may be stopped.')
        : secure
          ? i18n.t('Check what the browser sees: a security warning means the certificate needs installing on this device; a “can’t reach this page” error means the server needs starting.')
          : i18n.t('Check that the Ninaivu computer is on and Ninaivu is running, and that this device is on the same Wi-Fi.');
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
    setServerOffline();
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
  // Somebody new is viewing: what was asked for the last viewer is not theirs.
  sidebarViewer += 1;
  forgetInflight();
  // Whoever was here before may have been somebody else: see Viewer.forget.
  viewer?.forget();
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
  // Asked for now, alongside the lock's own question, rather than after it:
  // away from home each question is a round trip, and these two do not
  // depend on each other. Nothing is shown from it until the lock is up.
  const statusAsked = api.status().then((value) => ({ value }), (error) => ({ error }));
  // Before anything is loaded, so a page opened on a locked session is
  // covered at once rather than after the gallery has drawn.
  await screenLock?.start(state.user);
  bootStep('signed-in');
  try {
    await refreshStatus(statusAsked);
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
  // A successor's takeover waiting: every administrator is told, here too.
  if (state.user.role === 'admin' && !document.querySelector('.handover-banner')) {
    claimBanner(document.querySelector('main.content'), { toast });
  }

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
  // "Only to me" is for anybody with a profile of their own in the household.
  $('#nav-private').hidden = !can.keep_private;
  syncPrivateControls();
  $('#sel-screen').hidden = !can.send_for_screening;
  $('#sel-fav').hidden = !can.favorite;
  $('#sel-unfav').hidden = !can.favorite;
  $('#sel-download').hidden = !can.download;
  $('#sel-export').hidden = !can.download;
  $('#sel-create').hidden = !can.download;
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
  viewer.stories.userName = state.user?.name || '';
  // A guest listens to stories; telling one is for the household (api_stories.py).
  viewer.stories.canTell = (state.user?.role || 'guest') !== 'guest';
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

async function refreshStatus(asked = null) {
  countsAskedAt = Date.now();
  try {
    if (asked) {
      const { value, error } = await asked;
      if (error) throw error;
      state.status = value;
    } else {
      state.status = await api.status();
    }
  } catch (err) {
    if (isNetworkFailure(err)) {
      setServerOffline();
      return;
    }
    toast(i18n.t('Cannot reach the server.'), true);
    return;
  }
  showAwayLibraries(state.status);
  const { stats, ai, user } = state.status;
  if (user) {
    state.user = user;
    renderIdentity();
  }

  renderLibraryName();
  renderCounts(stats);
  showMoments();

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
  $('#count-private').textContent = stats.private || '';
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
  // The numbers are a nicety; the photographs are not. Asked again after a
  // failure (sidebarAsk): a cold count on a large library is the slowest
  // answer at sign-in, and one that failed left the sidebar without numbers
  // until a scan happened to ask again.
  const body = await sidebarAsk(() => api.counts());
  if (body?.stats) {
    state.status = { ...(state.status || {}), stats: body.stats };
    renderCounts(body.stats);
  }
}

/**
 * The sidebar's lists: tags, years and folders (one question), people, trips
 * and albums (one each).
 *
 * Each is asked for and drawn on its own. People, trips and albums used to be
 * asked for only once the tag/year/folder answer had come back, and not at
 * all when it failed — so one failed or slow answer at sign-in left the
 * family app with no People, no Albums and no Folders until the next sign-in.
 * A failure is asked again twice, a little later each time; a sign-in or a
 * sign-out is not (see sidebarAsk).
 */
async function refreshFacets() {
  // Not set up yet: there is nothing to list. Not known (the status question
  // itself failed): ask anyway, the server knows.
  if (state.status && !state.status.has_library) return;
  await Promise.all([loadFacetLists(), loadPeople(), loadOccasions(), loadAlbums()]);
}

/** Who the sidebar is being drawn for. Moves at every sign-in and sign-out,
 *  so an answer asked for under the last one is not drawn for this one. */
let sidebarViewer = 0;

/**
 * *ask* with up to two more tries after a failure, 1.5 s and then 4 s
 * later. Undefined when it never answered, or when the viewer changed
 * meanwhile. Refused (401/403) is an answer, not a failure to retry.
 */
async function sidebarAsk(ask) {
  const viewer = sidebarViewer;
  for (const wait of [0, 1500, 4000]) {
    if (wait) await new Promise((done) => { setTimeout(done, wait); });
    if (viewer !== sidebarViewer) return undefined;
    try {
      const answer = await ask();
      return viewer === sidebarViewer ? answer : undefined;
    } catch (error) {
      if (viewer !== sidebarViewer) return undefined;
      if (error?.status === 401 || error?.status === 403) return null;
      console.warn('[sidebar] asking again', error?.status || '', error?.message || error);
    }
  }
  return null;
}

async function loadFacetLists() {
  const facets = await sidebarAsk(() => api.facets());
  if (facets === undefined) return;
  state.facets = facets || state.facets || {};
  renderTagCloud();
  renderYears();
  renderFolders();
}

function currentFilters() {
  const filters = { ...state.filters };
  if (state.view === 'favorites') filters.favorites = true;
  if (state.view === 'private') filters.private = true;
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
      { scene: 'folder' },
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
    // Asked for any words: the server answers with nothing when no picture
    // search model is there to read them.
    showVideoMoments(filters.q || '', !data.total);
    paging.next = next;
    grid.setData(segments, { resetScroll });
    rememberSkeleton();
    // A view opened again, or the app opened again, comes back where it was
    // left, as a photo app does.
    if (resetScroll || placeDue) {
      placeDue = false;
      restorePlace(filters);
    }
    buildScrubber();
    showTruncation({ ...data, next_offset: next });

    if (!data.total && state.user?.anonymous && !anyFilter()) {
      // A guest, with nothing marked public: there is no filter to clear, and
      // "This filter has no items yet" sent them looking for one.
      showEmpty(i18n.t('Nothing shared with guests yet'),
        i18n.t('Sign in to see the family’s photographs.'), { signIn: true, scene: 'guest' });
    } else if (!data.total) {
      showEmpty(
        state.filters.q ? i18n.t('No matches') : i18n.t('Nothing to show'),
        state.filters.q
          ? i18n.t('Nothing matched “{q}”. Try fewer words, or a description like “red car at night”.', { q: state.filters.q })
          : i18n.t('This filter has no items yet.'),
        { scene: state.filters.q ? 'search' : 'filter' },
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
  showMemoriesBanner();
  loadDuplicatesIfNeeded();
  loadMoreIfNear();
  fillTimeline();
}

/** Fetch the next piece once the scroll is within a few screens of the end.
 *  *at* is where the grid's last frame drew, when there is one: asking the
 *  scroller again just after it drew made the browser lay it all out twice. */
async function loadMoreIfNear(at = null) {
  const page = paging;
  if (page.next == null || page.loading || !page.signal || page.signal.aborted) return;
  const view = (at ? at.view : grid.scroller.clientHeight) || 1;
  const top = at ? at.top : grid.scroller.scrollTop;
  const remaining = grid.layout.height - (top + view);
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

/** Where this person's skeleton colours are kept: their own, so nobody
 *  else on this device is shown the colours of photographs only they see. */
const skeletonKey = () => `skeleton.${state.user?.id ?? 'guest'}`;

/** The first tiles of the gallery, as their colours and shapes, kept for the
 *  next time it loads (see setLoading). Only the plain gallery's: a search's
 *  would greet the next visit with the wrong picture. */
function rememberSkeleton() {
  if (anyFilter() || state.view !== 'all') return;
  const tiles = grid.layout.cells.slice(0, 30)
    .filter((cell) => cell.swatch)
    .map((cell) => [cell.swatch, Math.round(Math.min(Math.max(cell.aspect || 1, .5), 2.4) * 100) / 100]);
  if (tiles.length >= 6) store.set(skeletonKey(), tiles);
}

/**
 * While the gallery loads, tiles in the colours of the photographs that will
 * fill them, laid out in rows the way they will be: the colours and shapes of
 * the first photographs last time. Grey boxes the first time, or after a
 * search. A soft light passes over them, unless less motion was asked for.
 */
function drawSkeleton(box) {
  const tiles = store.get(skeletonKey(), []);
  box.replaceChildren();
  const count = Array.isArray(tiles) && tiles.length ? tiles.length : 18;
  for (let i = 0; i < count; i++) {
    const tile = document.createElement('i');
    const [swatch, aspect] = Array.isArray(tiles[i]) ? tiles[i] : ['', 4 / 3];
    if (/^[0-9a-f]{3}$/i.test(swatch)) tile.style.backgroundColor = `#${swatch}`;
    const shape = Number(aspect) > 0 ? Number(aspect) : 4 / 3;
    tile.style.flexGrow = String(shape);
    tile.style.flexBasis = `${Math.round(shape * 150)}px`;
    tile.style.setProperty('--i', String(i));
    box.append(tile);
  }
}

function setLoading(on) {
  state.loading = on;
  $('#skeleton').classList.toggle('on', on && !grid.layout.cells.length);
  if (on && !grid.layout.cells.length) {
    drawSkeleton($('#skeleton'));
    if (!serverIsOffline) $('#empty').hidden = true;
  }
}

/** Whether anything narrows the gallery: search, a chip, a kind, favourites. */
function anyFilter() {
  const f = state.filters;
  return Boolean(f.q || f.tag || f.folder || f.camera || f.from || f.to || f.person
    || f.occasion || f.album || f.near || f.favorites || f.duplicates || f.kinds?.length);
}

function showEmpty(title, text, { signIn = false, scene = 'filter' } = {}) {
  const empty = $('#empty');
  empty.classList.remove('offline');
  // Which badge the picture wears: search, folder, guest or filter.
  empty.dataset.scene = scene;
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

  // The pane's scroll bar is clear until it is wanted (style.css). Scrolling
  // without the pointer over it (keys, touch, a filter brought into view)
  // shows it until the pane rests.
  const sidebar = $('#sidebar');
  let sidebarRest = 0;
  sidebar?.addEventListener('scroll', () => {
    sidebar.classList.add('scrolling');
    clearTimeout(sidebarRest);
    sidebarRest = setTimeout(() => sidebar.classList.remove('scrolling'), 900);
  }, { passive: true });

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
  $('#about-btn').onclick = () => ($('#about-modal').hidden = false);
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
  $('#about-close').onclick = () => ($('#about-modal').hidden = true);

  wireMenu($('#add-btn'), $('#add-menu'));
  wireMenu($('#create-btn'), $('#create-menu'));
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
      syncPrivateControls();
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
  wireJump();
  wireVisibility();
  wirePrivate();
  wireScreening();
  wireMoments();
  wireMemories();
  wireMap();
  wireUpload();
  new PhoneBackup({ toast, onFiled: () => reload() }).wire();
  const printScan = new PrintScan({
    toast,
    openFolder: (folder) => {
      state.filters.folder = folder;
      syncChips();
      reload({ resetScroll: true });
    },
  });
  printScan.wire();
  findPrint = new FindPrint({ toast, openItems, takeIn: (batch) => printScan.openWith(batch) });
  findPrint.wire();
  findPrint.show(Boolean(state.status?.moments?.find_print) && !state.user?.anonymous
    && ['family', 'admin'].includes(state.user?.role));
  wireSharing();
  wireAlbums();

  document.querySelectorAll('.modal').forEach((modal) => {
    wireModalFocus(modal);
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
  toast(i18n.t('Theme: {theme}', { theme: i18n.t(THEME_NAMES[document.documentElement.dataset.theme] || 'System') }));
}
window.cycleTheme = cycleTheme;
// Named here, translated where shown (see i18n.key).
const THEME_NAMES = { light: i18n.key('Light'), dark: i18n.key('Dark'), system: i18n.key('System') };

/* -- accounts ----------------------------------------------------------- */

function wireAccounts() {
  $('#profile-open').onclick = () => profileSheet.open(state.user);
  $('#signin-btn').onclick = async () => {
    gate.show(await accountsApi.state(), 'picker');
  };
  $('#switch-btn').onclick = async () => {
    forgetCachedShell();
    // Reloaded whatever the answer: a sign-out that could not reach the
    // server left the button doing nothing at all.
    try {
      await accountsApi.logout();
    } catch { /* the reload shows what is true */ }
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
        toast(i18n.t('{items} → {who}.', { items: i18n.items(result.updated), who: visLabel(button.dataset.selVis) }));
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
      // Closing the viewer brings the gallery up to date. Marked before the
      // answer comes back, and the reload waits for it, so a viewer closed
      // in that moment neither skips the reload nor reads the old setting.
      const change = accountsApi.setVisibility([item.id], button.dataset.vis);
      visibilitySaving = change.catch(() => {});
      visibilityChanged = true;
      try {
        await change;
        item.visibility = button.dataset.vis;
        syncViewerVisibility();
        // The tile behind the viewer shows it at once: its badge was left as
        // it was, and only a change made from a selection reloaded the
        // gallery. Whether the photograph still belongs in this view (Hidden,
        // Public, a guest's) is settled when the viewer closes.
        showTileVisibility(item.id, button.dataset.vis);
        toast(i18n.t('Now visible to {who}.', { who: visLabel(button.dataset.vis) }));
        refreshStatus();
      } catch (exc) { toast(exc.message, true); }
    };
  });
}

/** Set since the viewer opened, so closing it brings the gallery up to date. */
let visibilityChanged = false;
/** The last change asked for from the viewer, settled or not. */
let visibilitySaving = Promise.resolve();
/** A photo stack changed from the viewer since it opened (stacks.js). */
let stacksChanged = false;

/** The visibility badge on one tile (flags 64 = public, 128 = hidden). */
function showTileVisibility(id, visibility) {
  const cell = grid.layout.cells.find((c) => c.id === id);
  if (!cell) return;
  cell.flags &= ~192;
  if (visibility === 'public') cell.flags |= 64;
  else if (visibility === 'hidden') cell.flags |= 128;
  grid.refreshCell(id);
}

/* -- Family moments: the photo frame and time capsules ------------------- */

/** The viewer over everything else, until it is closed again. */
function openItems(ids, index) {
  return new Promise((resolve) => {
    viewer.addEventListener('close', resolve, { once: true });
    viewer.open(ids, index);
  });
}

/** Find this print (static/js/find-print.js), made when the page is wired. */
let findPrint = null;

/** Whom the moments were last offered to: a sign-in brings them anew. */
let momentsShownTo;
let frameOpenedOnce = false;

/** Offer whichever of the moments are switched on (static/js/frame.js,
 *  static/js/capsules.js); open the frame for a page opened at /#frame. */
function showMoments() {
  const on = state.status?.moments || {};
  const family = !state.user?.anonymous && ['family', 'admin'].includes(state.user?.role);
  $('#frame-btn').hidden = !on.frame;
  $('#ask-btn').hidden = !(on.ask && family);
  $('#year-btn').hidden = !on.year;
  $('#then-btn').hidden = !on.then_now;
  $('#life-btn').hidden = !(on.life && family);
  $('#festivals-btn').hidden = !on.festivals;
  $('#young-btn').hidden = !(on.young_faces && family);
  findPrint?.show(on.find_print && family);
  $('#capsules-btn').hidden = !(on.capsules && family);
  $('#create-capsules-btn').hidden = !(on.capsules && family);
  // Making something takes copies of the photographs away: not for guests,
  // the same as "Make a book" and Download.
  $('#studio-btn').hidden = !state.user?.can?.download;
  $('#create-books-btn').hidden = !state.user?.role || state.user.role === 'guest';
  tidyCreateMenu();
  showVoiceSearch(on.voice && family);
  if (viewer) viewer.colourOn = !!(on.colour && family);
  if (viewer?.talk) viewer.talk.on = !!(on.talk && family);
  if (viewer) viewer.lettersOn = !!(on.letters && family);
  if (viewer?.stack) viewer.stack.on = !!on.stacks;
  $('#sel-capsule').hidden = !(on.capsules && family);
  const who = state.user?.anonymous ? 0 : (state.user?.id ?? 0);
  if (momentsShownTo === who) return;
  momentsShownTo = who;
  if (on.frame && frameAsked() && !frameOpenedOnce) {
    frameOpenedOnce = true;
    openFrame();
  }
  if (on.capsules && family) announceCapsules({ toast, openItems });
}

/** The Create menu shows a heading only over something under it, and the
 *  menu's button only when there is anything in it at all. */
function tidyCreateMenu() {
  const menu = $('#create-menu');
  let head = null;
  let any = false;
  for (const node of menu.children) {
    if (node.classList.contains('bar-menu-head')) {
      if (head) head.hidden = true;
      head = node;
    } else if (!node.hidden) {
      if (head) head.hidden = false;
      head = null;
      any = true;
    }
  }
  if (head) head.hidden = true;
  $('#create-wrap').hidden = !any;
}

function wireMoments() {
  $('#studio-btn').onclick = () => openStudioFor([...grid.selection]);
  $('#sel-create').onclick = () => openStudioFor([...grid.selection], { returnFocus: $('#sel-create') });
  $('#create-books-btn').onclick = () => openBooksList({ toast });
  $('#create-capsules-btn').onclick = () => openCapsules({ toast, openItems });
  $('#frame-btn').onclick = () => openFrame();
  // A question typed into the search box is carried over to Ask Ninaivu.
  $('#ask-btn').onclick = () => {
    const typed = ($('#search')?.value || '').trim();
    openAsk({ openItems, question: /[?？]$/.test(typed) ? typed : '' });
  };
  $('#capsules-btn').onclick = () => openCapsules({ toast, openItems });
  $('#year-btn').onclick = () => openOurYear({ toast, openItems });
  $('#then-btn').onclick = () => openThenAndNow({ toast, openItems });
  $('#life-btn').onclick = () => openLifeStory({ toast, openItems });
  // A book of a festival, for whoever may make books (not guests).
  $('#festivals-btn').onclick = () => openFestivals({
    toast, openItems, books: !!state.user?.role && state.user.role !== 'guest' });
  $('#young-btn').onclick = () => openYoungFaces({ toast, openItems });
  $('#then-now-all').onclick = () => openThenAndNow({ toast, openItems });
  $('#sel-capsule').onclick = () => {
    const ids = [...grid.selection];
    if (!ids.length) return;
    openSealSheet(ids, { toast, onSealed: () => grid.clearSelection() });
  };
  // Asked for by its address while the page is open: the frame itself says
  // so if it has been switched off since the page loaded.
  window.addEventListener('hashchange', () => {
    if (frameAsked()) openFrame();
  });
}

/* -- "Only to me" -------------------------------------------------------- */

/** Which of the two selection buttons fits the view: inside "Only to me" the
 *  choice is to give items back, everywhere else to keep them. */
function syncPrivateControls() {
  const can = !!state.user?.can?.keep_private;
  const inside = state.view === 'private';
  $('#sel-private').hidden = !can || inside;
  $('#sel-unprivate').hidden = !can || !inside;
}

async function keepPrivate(ids, keep) {
  if (!ids.length) return;
  try {
    await api.keepPrivate(ids, keep);
    toast(keep ? i18n.t('Kept only to you.') : i18n.t('Shown to the family again.'));
    grid.clearSelection();
    refreshCounts();
    reload();
  } catch (exc) { toast(exc.message, true); }
}

function wirePrivate() {
  $('#sel-private').onclick = () => keepPrivate([...grid.selection], true);
  $('#sel-unprivate').onclick = () => keepPrivate([...grid.selection], false);
  const button = $('#v-private');
  if (!button) return;
  button.onclick = async () => {
    const item = viewer.item;
    if (!item) return;
    const keep = !(item.private || item.private_owner);
    await keepPrivate([item.id], keep);
    item.private = keep;
    if (!keep) delete item.private_owner;
    syncViewerPrivate();
  };
}

/** The viewer's button says what pressing it will do. An administrator
 *  looking at somebody else's private item is told whose it is. */
function syncViewerPrivate() {
  const button = $('#v-private');
  if (!button) return;
  const item = viewer.item;
  button.hidden = !item || !state.user?.can?.keep_private;
  if (!item) return;
  const kept = !!(item.private || item.private_owner);
  button.classList.toggle('on', kept);
  button.querySelector('.viewer-tools-more-label').textContent =
    kept ? i18n.t('Show to family again') : i18n.t('Keep only to me');
  button.title = item.private_owner
    ? i18n.t('Only to {name}', { name: item.private_owner })
    : i18n.t('Hidden from other family members');
}

function visLabel(visibility) {
  return i18n.t(VIS_LABELS[visibility] || visibility);
}
// Named here, translated where shown (see i18n.key).
const VIS_LABELS = {
  public: i18n.key('everyone'), family: i18n.key('family only'), hidden: i18n.key('admins only'),
};

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

/* -- search: gallery/search.js ------------------------------------------- */

/* -- selection: gallery/selection.js -------------------------------------- */

/* -- duplicates review ---------------------------------------------------
 *
 * The review itself is a page of its own (pages/family/duplicates.js),
 * fetched the first time the Duplicates view is opened by somebody who may
 * download. Anywhere else the banner is simply put away.
 */
function loadDuplicatesIfNeeded() {
  const banner = $('#duplicates-banner');
  if (!banner) return;
  if (state.view !== 'duplicates' || !state.user?.can?.download) {
    banner.hidden = true;
    return;
  }
  openPage('duplicates').then((page) => page?.load());
}

/* -- scrubber ----------------------------------------------------------- */

/** The rail's height when the marks were placed: read once there, not on
 *  every scroll frame, where reading it would force a layout. */
let scrubberRail = 0;
/** Each month mark, with where it sits on the rail in pixels. */
let scrubberMarks = [];

function buildScrubber() {
  const scrubber = $('#scrubber');
  const track = $('#scrubber-track');
  const headers = grid.layout.headers;
  track.innerHTML = '';
  scrubberMarks = [];
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
    scrubberMarks.push({ mark, y: Math.max(7, Math.min(railHeight - 7, tick.frac * railHeight)) });
  }
  positionScrubber(grid.scroller.scrollTop /
    Math.max(1, grid.layout.height - grid.scroller.clientHeight));
  labelScrubber(sectionAt(grid.layout, grid.scroller.scrollTop + 40));
}

/* -- remembering the place ------------------------------------------------ */

/** Each view's place — All media, an album, a person, a search — kept on this
 *  device, so going back to it, or opening the app again, lands where it was
 *  left. The last few dozen views only; nothing leaves the device. */
const PLACES_KEY = 'places';
/** The first gallery of a visit returns to its place too. */
let placeDue = true;
const PLACES_MAX = 40;

function placeKey(filters) {
  const user = state.user?.id ?? 0;
  return JSON.stringify([user, state.view, filters]);
}

function rememberPlace() {
  if (!paging.filters || !grid.layout.cells.length) return;
  const at = grid.anchor();
  if (!at) return;
  const places = store.get(PLACES_KEY, []).filter((p) => p && p.k !== placeKey(paging.filters));
  places.push({ k: placeKey(paging.filters), ...at });
  store.set(PLACES_KEY, places.slice(-PLACES_MAX));
}

/** The place being returned to, for a moment after: On This Day can appear
 *  above the grid just after, and is put back under the same photograph. */
let returning = null;

function restorePlace(filters) {
  const place = store.get(PLACES_KEY, []).find((p) => p && p.k === placeKey(filters));
  if (!place || place.id == null) return;
  if (grid.scrollToAnchor(place)) returning = { place, until: performance.now() + 3000 };
}

function holdPlace() {
  if (returning && performance.now() < returning.until) grid.scrollToAnchor(returning.place);
  else returning = null;
}

/* -- jump to a month ----------------------------------------------------- */

/** The months and years this view has, from its own section headers: the
 *  first header of each month, so a jump lands at the month's start. */
function jumpIndex() {
  const years = new Map();          // "2023" -> Map("05" -> header)
  let undated = null;
  for (const header of grid.layout.headers || []) {
    const match = /^(\d{4})-(\d{2})/.exec(header.key || '');
    if (!match) {
      if (header.key !== 'match' && !undated) undated = header;
      continue;
    }
    if (!years.has(match[1])) years.set(match[1], new Map());
    const months = years.get(match[1]);
    if (!months.has(match[2])) months.set(match[2], header);
  }
  return { years, undated };
}

/** Shown for a view in date order with more than one month to go between. */
function syncJump() {
  const box = $('#jump');
  if (!box) return;
  const dated = ['date_desc', 'date_asc'].includes(state.filters.sort || 'date_desc')
    && !state.semantic;
  const { years } = jumpIndex();
  let months = 0;
  for (const m of years.values()) months += m.size;
  const show = dated && (months > 1 || paging.next != null);
  box.hidden = !show;
  if (!show) closeJump();
}

function closeJump() {
  const pop = $('#jump-pop');
  if (!pop || pop.hidden) return;
  pop.hidden = true;
  $('#jump').classList.remove('open');
  $('#jump-btn').setAttribute('aria-expanded', 'false');
}

/** Everything still to come of a view that arrives in pieces, so the list
 *  holds every month there is, not only those scrolled past already. */
async function loadAllPages(limit = GALLERY_PAGE) {
  const page = paging;
  while (page === paging && page.next != null && !page.signal?.aborted) {
    if (page.loading) { await new Promise((r) => setTimeout(r, 150)); continue; }
    page.loading = true;
    try {
      const data = await api.segments(page.filters, page.signal,
        { limit, offset: page.next });
      if (page !== paging) return;
      // Not under a hand on the timeline rail: the rail is drawn again for the
      // longer timeline, and doing that mid-drag moved the months under it.
      while (scrubbing() && page === paging) await new Promise((r) => setTimeout(r, 150));
      if (page !== paging) return;
      page.next = data.next_offset;
      grid.appendData(data.segments);
      showTruncation(data);
    } catch {
      page.next = null;
    } finally {
      page.loading = false;
    }
  }
}

function scrubbing() {
  return Boolean($('#scrubber')?.classList.contains('active'));
}

/** The rest of a large view, fetched as soon as its first piece is on screen
 *  rather than when the scroll comes near the end of it. The timeline rail is
 *  drawn from what is held, so until then it covered only the newest piece and
 *  drew itself again — longer — when the next arrived. In large pieces: each
 *  one lays the grid out again. */
function fillTimeline() {
  const page = paging;
  if (page.next == null) return;
  const start = () => { if (page === paging) loadAllPages(GALLERY_MAX_PAGE / 2); };
  if ('requestIdleCallback' in window) requestIdleCallback(start, { timeout: 600 });
  else setTimeout(start, 200);
}

function wireJump() {
  const btn = $('#jump-btn');
  if (!btn) return;
  // The list itself is a page of its own (pages/family/jump.js), fetched the
  // first time it is opened.
  btn.onclick = () => openPage('jump').then((page) => page?.openJump());
  document.addEventListener('pointerdown', (event) => {
    if (!$('#jump').contains(event.target)) closeJump();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !$('#jump-pop').hidden) {
      closeJump();
      btn.focus();
    }
  });
}

/** The marker's own label: the month and year it is on. Written only when
 *  the month changes — a scroll, or a drag of the handle, asks every frame —
 *  and with one formatter kept for the language in use: making one for each
 *  label cost more than everything else the rail does in a frame. */
let scrubberLabel = { key: null, locale: null, format: null };
function labelScrubber(section) {
  if (!section) return;
  const locale = i18n.locale();
  const key = section.key === 'match' ? 'match'
    : /^\d{4}-\d{2}/.test(section.key) ? section.key.slice(0, 7) : 'unknown';
  if (key === scrubberLabel.key && locale === scrubberLabel.locale) return;
  if (locale !== scrubberLabel.locale || !scrubberLabel.format) {
    scrubberLabel.format = new Intl.DateTimeFormat(locale, { month: 'short', year: 'numeric' });
  }
  scrubberLabel.key = key;
  scrubberLabel.locale = locale;
  const label = $('#scrubber-thumb').querySelector('span');
  if (key === 'match') label.textContent = i18n.t('Best matches');
  else if (key === 'unknown') label.textContent = i18n.t('Undated');
  else label.textContent = scrubberLabel.format.format(new Date(`${key}-01T00:00:00`));
}

function positionScrubber(ratio) {
  const thumb = $('#scrubber-thumb');
  const at = Math.max(0, Math.min(1, ratio));
  // Clamped like the marks, so the marker's own label is never cut in half
  // at either end of the rail. Moved with a transform: moving it with `top`
  // laid the page out again on every frame of a scroll or a drag.
  const rail = scrubberRail || 1;
  const thumbY = Math.max(12, Math.min(rail - 12, at * rail));
  thumb.style.transform = `translateY(${thumbY}px) translateY(-50%)`;
  // The marker carries the month it is on; a mark it would sit on top of
  // gives way, rather than showing through half-covered.
  for (const { mark, y } of scrubberMarks) {
    mark.classList.toggle('covered', Math.abs(y - thumbY) < 20);
  }
}

function wireScrubber() {
  const scrubber = $('#scrubber');
  const thumb = $('#scrubber-thumb');
  let dragging = false;
  // Where the rail is, measured once when the handle is taken hold of: it
  // does not move during a drag, and measuring it on every move made the
  // browser lay the page out there and then.
  let rail = null;
  let hoverTimer = 0;

  const seek = (clientY) => {
    const rect = scrubber.getBoundingClientRect();
    grid.scrollToRatio((clientY - rect.top) / rect.height);
  };

  // While the handle is held the grid follows it showing each tile's colour,
  // and the pictures are fetched for where it is let go, or held still
  // (Grid.beginScrub) — not for every month it passes on the way.
  thumb.addEventListener('pointerdown', (event) => {
    if (event.button !== 0) return;
    dragging = true;
    clearTimeout(hoverTimer);
    rail = scrubber.getBoundingClientRect();
    scrubber.classList.add('active');
    thumb.setPointerCapture(event.pointerId);
    grid.beginScrub();
    event.preventDefault();
  });
  thumb.addEventListener('pointermove', (event) => {
    if (!dragging || !rail) return;
    grid.scrubTo((event.clientY - rail.top) / Math.max(1, rail.height));
  });
  const letGo = () => {
    if (!dragging) return;
    dragging = false;
    rail = null;
    scrubber.classList.remove('active');
    grid.endScrub();
  };
  thumb.addEventListener('pointerup', letGo);
  thumb.addEventListener('pointercancel', letGo);
  thumb.addEventListener('lostpointercapture', letGo);
  scrubber.addEventListener('click', (event) => {
    if (event.target === thumb || thumb.contains(event.target)) return;
    seek(event.clientY);
  });
  // A mouse resting on the rail is about to take the grid there: the
  // pictures around that place are fetched while it rests, so the jump
  // lands on a screen already drawn.
  scrubber.addEventListener('pointermove', (event) => {
    if (dragging || event.pointerType !== 'mouse') return;
    clearTimeout(hoverTimer);
    hoverTimer = setTimeout(() => {
      const rect = scrubber.getBoundingClientRect();
      const ratio = Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height));
      const max = Math.max(0, grid.layout.height - grid.scroller.clientHeight);
      grid.prefetchAround(max * ratio);
    }, 150);
  });
  scrubber.addEventListener('pointerleave', () => clearTimeout(hoverTimer));
  // The marker is placed in pixels along the rail, so a window made taller
  // or shorter places the marks and the marker again.
  if ('ResizeObserver' in window) {
    let railSeen = 0;
    new ResizeObserver(() => {
      const height = scrubber.clientHeight;
      if (!height || height === railSeen) return;
      railSeen = height;
      if (grid?.layout && !scrubber.hidden) buildScrubber();
    }).observe(scrubber);
  }
}

/* ========================================================================
   Grid / viewer wiring
   ======================================================================== */

/* -- Send for screening --------------------------------------------------- */

/* A family member asks an administrator to look at items: a screenshot of a
   bank page, a document, something not for everyone. They leave every family
   member's view at once, the sender's too, and wait in the console's
   Screening queue, where an admin ignores (they come back), hides, marks
   explicit or deletes them (features/screening/routes.py). */

let screeningIds = [];

function openScreening(ids) {
  if (!ids.length) return;
  screeningIds = ids;
  $('#screen-count').textContent = ids.length === 1
    ? i18n.t('It leaves the family’s gallery now, and an admin decides whether it comes back.')
    : i18n.t('These {items} leave the family’s gallery now, and an admin decides whether they come back.',
      { items: i18n.items(ids.length) });
  $('#screen-note').value = '';
  $('#screen-modal').hidden = false;
  $('#screen-form').querySelector('input[name="screen-reason"]:checked')?.focus();
}

async function sendScreening(event) {
  event.preventDefault();
  const ids = screeningIds;
  $('#screen-modal').hidden = true;
  if (!ids.length) return;
  // Written in the sender's language, as the admin will read it.
  const picked = $('#screen-form').querySelector('input[name="screen-reason"]:checked')?.value;
  const reason = picked ? i18n.t(picked) : '';
  const typed = $('#screen-note').value.trim();
  const note = [reason, typed].filter(Boolean).join(' — ');
  try {
    const result = await api.sendForScreening(ids, note);
    const count = (result.sent || 0) + (result.already || 0);
    toast(count === 1 ? i18n.t('Sent to an admin. It is out of the family’s gallery until they decide.')
      : i18n.t('{items} sent to an admin. They are out of the family’s gallery until an admin decides.',
        { items: i18n.items(count) }));
    if (grid.selection.size) grid.clearSelection();
    // Gone from here at once: the viewer showing one of them closes, and the
    // gallery behind it is brought up to date where it stands.
    if (!$('#viewer').hidden && ids.includes(viewer.item?.id)) viewer.close();
    refreshCounts();
    reload();
  } catch (exc) { toast(exc.message, true); }
}

/** The viewer's button: for family members, on anything they can see. */
function syncViewerScreening() {
  const button = $('#v-screen');
  if (button) button.hidden = !viewer.item || !state.user?.can?.send_for_screening;
}

function wireScreening() {
  const modal = $('#screen-modal');
  if (!modal) return;
  const close = () => { modal.hidden = true; };
  $('#screen-close').onclick = $('#screen-cancel').onclick = close;
  modal.addEventListener('mousedown', (event) => { if (event.target === modal) close(); });
  modal.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') { event.stopPropagation(); close(); }
  });
  $('#screen-form').onsubmit = sendScreening;
  $('#sel-screen').onclick = () => openScreening([...grid.selection].slice(0, 2000));
  const button = $('#v-screen');
  if (button) button.onclick = () => { if (viewer.item) openScreening([viewer.item.id]); };
}

function wireGrid() {
  grid.addEventListener('open', (event) => {
    viewer.open(grid.ids, event.detail.index, grid.thumbedIds(),
                { from: grid.nodeAt(event.detail.index) });
  });
  // Closing, the photograph shrinks back into its tile (static/js/motion.js).
  viewer.tileFor = (id) => grid.nodeAt(grid.indexOfId(id));

  grid.addEventListener('selection', (event) => {
    const count = event.detail.ids.length;
    $('#selection-bar').hidden = count === 0;
    $('#sel-count').textContent = i18n.t('{count} selected', { count });
  });

  let scrubberIdle = 0;
  let placeTimer = 0;
  grid.addEventListener('scroll', (event) => {
    // A phone has no hover to bring the rail up; scrolling does, briefly.
    const scrubber = $('#scrubber');
    scrubber.classList.add('scrolling');
    clearTimeout(scrubberIdle);
    scrubberIdle = setTimeout(() => scrubber.classList.remove('scrolling'), 1500);
    clearTimeout(placeTimer);
    placeTimer = setTimeout(rememberPlace, 500);
    positionScrubber(event.detail.ratio);
    loadMoreIfNear(event.detail);
    labelScrubber(event.detail.section);
  });

  grid.addEventListener('layout', () => { buildScrubber(); syncJump(); });
  // On This Day arriving above the grid just after a return keeps the place.
  const banner = $('#memories-banner');
  if (banner && 'ResizeObserver' in window) new ResizeObserver(holdPlace).observe(banner);
  // Not once the person is scrolling for themselves.
  for (const kind of ['wheel', 'touchstart', 'keydown', 'pointerdown']) {
    grid.scroller.addEventListener(kind, () => { returning = null; }, { passive: true });
  }
  // Leaving the page, or the phone putting it away, keeps the place too.
  window.addEventListener('pagehide', rememberPlace);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') rememberPlace();
  });
}

function wireViewer() {
  viewer.addEventListener('change', () => {
    syncViewerVisibility(); syncViewerPrivate(); syncViewerScreening();
  });

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

  // A stack's top photograph chosen, or the stack taken apart: the timeline
  // is drawn again once the viewer closes (static/js/stacks.js).
  viewer.addEventListener('stacks', () => { stacksChanged = true; });

  viewer.addEventListener('close', (event) => {
    if (visibilityChanged) {
      visibilityChanged = false;
      visibilitySaving.then(() => reload());
    } else if (stacksChanged) {
      reload();
    }
    stacksChanged = false;
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
   Command bar (Ctrl+K, ⌘K on a Mac): static/js/palette.js
   ======================================================================== */

/**
 * Everything the page offers, then everybody with a name and every trip.
 *
 * The people and trips are the lists the sidebar already has: the server
 * hands this person only those they may see, so a family member is never
 * offered someone, or a trip, that only hidden photographs hold, and an
 * administrator, who sees everything, is offered everyone.
 */
function paletteCommands() {
  const found = familyCommands();
  const people = i18n.t('People');
  for (const person of state.people || []) {
    const name = String(person.name || '').trim();
    if (name) found.push({ group: people, name, run: () => showPerson(person.id) });
  }
  const trips = i18n.t('Trips');
  for (const trip of state.occasions || []) {
    const name = occasionTitle(trip);
    if (!name) continue;
    found.push({
      group: trips, name,
      run: () => { state.filters.occasion = trip.id; syncChips(); reload({ resetScroll: true }); },
    });
  }
  return found;
}

/** Whatever is typed can also be searched for, first in the list: the same
 *  search as the box at the top, with the same rules about what it finds. */
function paletteSearch(words) {
  return [{
    group: i18n.t('Search'),
    name: i18n.t('Search for “{words}”', { words }),
    hint: 'Enter',
    run: () => {
      stopSearch();
      $('#search').value = words;
      $('#clear-search').hidden = false;
      $('#suggestions').hidden = true;
      state.filters.q = words;
      reload({ resetScroll: true });
    },
  }];
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

    if (key === '[') { $('#sidebar-toggle')?.click(); return; }
    if (key === 'h') {
      // Back to every photograph, from a view, a person, a trip or a search.
      state.view = 'all';
      document.querySelectorAll('[data-view]').forEach(
        (b) => b.classList.toggle('active', b.dataset.view === 'all'));
      syncPrivateControls();
      clearFilters();
      return;
    }
    const moves = {
      arrowright: 'next', arrowleft: 'prev', arrowdown: 'down', arrowup: 'up',
      home: 'first', end: 'last',
    };
    if (moves[key]) {
      event.preventDefault();
      // Shift takes the selection along with the cursor, the way it does in
      // Explorer and in Photos.
      grid.moveCursor(moves[key], { extend: event.shiftKey });
      return;
    }
    if (event.key === 'Enter' && grid.cursor >= 0) {
      viewer.open(grid.ids, grid.cursor, grid.thumbedIds(), { from: grid.nodeAt(grid.cursor) });
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

/** The server's count of changes to who may see what (onActivity). */
let seenGeneration = null;

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
  // The away banner follows the drive here, on every tick. It was only drawn
  // from /api/status, which an open tab asks for when it is shown again or a
  // scan ends, so a drive plugged back in left "not connected" on screen.
  // When the number changes the rest of the status (counts, the library's
  // name) is asked for again too. An older server leaves the field out.
  if ('libraries_away' in activity) {
    const before = Number(state.status?.libraries_away || 0);
    const now = Number(activity.libraries_away || 0);
    showAwayLibraries(activity);
    if (state.status) {
      state.status.libraries_away = now;
      state.status.libraries_away_paths = activity.libraries_away_paths || [];
    }
    if (now !== before) refreshStatus();
  }
  // Who may see what moved: somebody kept an item "Only to me", sent it for
  // screening, or an admin hid, flagged or deleted it. The gallery is brought
  // up to date where it stands, so it leaves this screen too, within a tick.
  if ('seen' in activity) {
    const before = seenGeneration;
    seenGeneration = activity.seen;
    if (before !== null && before !== activity.seen) {
      refreshCounts();
      reload();
    }
  }
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
  if (lastPhase === null && (scan.status === 'done' || scan.status === 'error')) {
    // A scan that ended before the page opened: what the page just read is
    // already after it. Asking for the counts, the sidebar and the gallery
    // again on the first tick read the whole library twice on every open.
    lastPhase = phase;
  }
  if (phase !== lastPhase && (scan.status === 'done' || scan.status === 'error')) {
    lastPhase = phase;
    refreshStatus();
    refreshFacets();
    if (!state.loading) reload();
    if (scan.status === 'done' && (scan.added || scan.removed)) {
      toast(i18n.t('{added} added, {removed} removed.', { added: scan.added.toLocaleString(i18n.locale()), removed: scan.removed.toLocaleString(i18n.locale()) }));
    }
  } else if (scan.status === 'tagging' && scan.tagged && scan.tagged % 200 === 0) {
    refreshFacets();
  }
}

/* ========================================================================
   Ninaivu 5.0 Features: Memories, Geo Map, Upload, and Sharing
   ======================================================================== */

let activeMemories = [];

/**
 * On This Day belongs to the plain gallery only. A search, a filter in the
 * sidebar or a narrower view (Favourites, Videos, a person, an album…) is the
 * family asking for something particular, so the strip steps aside and comes
 * back once everything is cleared again.
 */
function memoriesBannerWanted() {
  return state.view === 'all' && !anyFilter();
}

/** Moments inside videos (static/js/video-moments.js), while switched on. */
let videoMoments = null;

function showVideoMoments(query, nothingElse = false) {
  if (!state.status?.moments?.video_moments) {
    videoMoments?.hide();
    return;
  }
  if (!videoMoments) {
    videoMoments = new VideoMoments($('#moments-banner'), {
      open: (ids, index, at) => viewer.open(ids, Math.max(0, index), null, { at }),
    });
  }
  videoMoments.show(query).then((shown) => {
    // Only scenes inside videos matched: they are the answer, not "No matches".
    if (shown && nothingElse) $('#empty').hidden = true;
  });
}

/** Whether Then and now has pairs from this week to show above the gallery. */
let thenNowToday = false;

function showMemoriesBanner() {
  const banner = $('#memories-banner');
  if (banner) banner.hidden = !activeMemories.length || !memoriesBannerWanted();
  const strip = $('#then-now-banner');
  if (strip) strip.hidden = !thenNowToday || !memoriesBannerWanted();
}

/** Then and now's strip (static/js/then-and-now.js), while switched on. */
async function loadThenNow() {
  thenNowToday = false;
  if (!state.status?.moments?.then_now) {
    hideThenNowStrip();
    return;
  }
  thenNowToday = await showThenNowStrip({ toast, openItems });
  showMemoriesBanner();
}

async function loadMemories() {
  activeMemories = [];
  if (!state.status?.has_library) return;
  loadThenNow();
  try {
    // Asked again after a failure, as the sidebar's lists are (sidebarAsk).
    const data = await sidebarAsk(() => api.memories());
    if (!data) return;
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

    showMemoriesBanner();
    carousel.innerHTML = '';
    const nowYear = new Date().getFullYear();
    // "2 years ago", "last year", "this year" in the reader's language: the
    // badge was English whatever language the app was in.
    const ago = new Intl.RelativeTimeFormat(i18n.locale(), { numeric: 'auto' });

    data.items.slice(0, 16).forEach((item) => {
      const card = document.createElement('div');
      card.className = 'memory-card';
      const itemYear = item.date ? parseInt(item.date.slice(0, 4), 10) : nowYear;
      const diff = nowYear - itemYear;
      const said = ago.format(-Math.max(0, diff), 'year');
      const diffLabel = said.charAt(0).toLocaleUpperCase(i18n.locale()) + said.slice(1);

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

/* -- Geo Map, Local Upload and Sharing ------------------------------------ */

function wireMap() {
  // The map is a page of its own (pages/family/map.js), fetched when first opened.
  $('#map-btn')?.addEventListener('click', () => openPage('map').then((page) => page?.openMap()));
  viewer.addEventListener('map', (event) => {
    const item = event.detail?.item;
    if (!item?.gps) return;
    viewer.close();
    const focus = { lat: item.gps[0], lon: item.gps[1] };
    openPage('map').then((page) => page?.openMap({ focus }));
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

function wireUpload() {
  // The upload dialog is a page of its own (pages/family/upload.js).
  $('#upload-btn')?.addEventListener('click', () => openPage('upload').then((page) => page?.open()));
}

function wireSharing() {
  // The share dialog is a page of its own (pages/family/sharing.js).
  viewer.addEventListener('share', (e) => {
    const item = e.detail.item;
    openPage('sharing').then((page) => page?.openShareModal(item));
  });
}

/* -- pages ----------------------------------------------------------------
 *
 * The parts of the family app that are opened rather than always on screen
 * are modules of their own (static/js/pages/family/), fetched the first time
 * they are opened. The import() calls are written out in full so the server
 * finds them and names each at its stamped URL in the import map
 * (server/modules.py).
 */
const PAGE_MODULES = {
  duplicates: () => import('./pages/family/duplicates.js'),
  jump: () => import('./pages/family/jump.js'),
  map: () => import('./pages/family/map.js'),
  sharing: () => import('./pages/family/sharing.js'),
  upload: () => import('./pages/family/upload.js'),
};
const loadedPages = new Map();

/** What a page may ask of the gallery around it. */
const shell = {
  get viewer() { return viewer; },
  get grid() { return grid; },
  get paging() { return paging; },
  jumpIndex: () => jumpIndex(),
  closeJump: () => closeJump(),
  loadAllPages: () => loadAllPages(),
  deleteSelected: (ids, done) => deleteSelected(ids, done),
  reload: (options) => reload(options),
};

/** The module of page *name*, loaded and wired once; null if it has none. */
function openPage(name) {
  const load = PAGE_MODULES[name];
  if (!load) return Promise.resolve(null);
  if (!loadedPages.has(name)) {
    loadedPages.set(name, load().then((module) => {
      module.wire?.(shell);
      return module;
    }).catch((err) => {
      // Not kept: the next opening asks again (the server was restarting, say).
      loadedPages.delete(name);
      toast(err.message, true);
      return null;
    }));
  }
  return loadedPages.get(name);
}

/* ======================================================================== */

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
