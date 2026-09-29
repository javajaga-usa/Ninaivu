/**
 * The admin console (port 3000).
 *
 * A management tool, not a gallery: profiles, folder visibility, library
 * controls and an activity log — plus a live preview that answers the question
 * an admin actually has, which is "what will they see?"
 */

import {
  accountsApi, avatarNode, Gate, ProfileSheet, roleBadge,
} from './accounts.js';
import { onUnauthorized, reportUnauthorized, sessionRestored, subscribeProgress, thumbUrl, SCAN_COUNTS, timeLeft } from './api.js';
import { renderActivity, subscribeActivity } from './activity.js';
import { ArchivePanel } from './archive.js';
import { enterPressesTheButton } from './enter-key.js';
import { CloudPanel } from './cloud.js';
import { WorkloadPanel } from './workload.js';
import { DiskPanel } from './disks.js';
import { SafetyPanel } from './safety.js';
import { ScreenLock } from './lock.js';
import { AIServerPanel } from './ai-server.js';
import { AIModelsPanel } from './ai-models.js';
import { ComponentsPanel } from './components.js';
import { MigrationPanel } from './migration.js';
import { AdvancedPanel } from './advanced.js';
import { ServerPanel } from './server.js';
import { PerformancePanel } from './performance.js';
import { FacesPanel } from './faces.js';
import { StraightenPanel } from './straighten.js';
import { FirstDay } from './first-day.js';

const $ = (sel) => document.querySelector(sel);

/* Like $, but never null.
 *
 * admin.html is compiled once and cached by Flask, while this file is served
 * with no-cache and updates immediately, so between an edit and a restart the
 * browser runs new JavaScript against old markup. A bare
 * `$('#missing').onclick =` throws there and abandons the rest of the function
 * it is in — which is how adding a sign-out button silently killed the
 * storage-integrity button wired forty lines below it. Handlers bound to the
 * stub simply never fire.
 */
const DETACHED = document.createElement('div');
const $$ = (sel) => document.querySelector(sel) || DETACHED;
const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
};

async function json(url, options = {}) {
  const response = await fetch(url, {
    headers: {
      Accept: 'application/json',
      ...(options.body ? { 'Content-Type': 'application/json' } : {}),
    },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const text = await response.text();
  const data = text ? JSON.parse(text) : null;
  if (!response.ok) {
    // A 401 carrying `needs_password` is a request asking you to confirm
    // who you are before something irreversible — not a session that has
    // ended. See api.js: the same 401 means two different things, and
    // only one of them should raise the sign-in screen.
    if (response.status === 401 && !data?.needs_password) reportUnauthorized(url);
    throw Object.assign(new Error(data?.error || response.statusText),
      { status: response.status, data });
  }
  return data;
}

const adminApi = {
  locations: (hours) => json(`/api/admin/locations?hours=${encodeURIComponent(hours)}`),
  applyLocations: (body) => json('/api/admin/locations/apply', { method: 'POST', body }),
  undoLocations: () => json('/api/admin/locations/undo', { method: 'POST', body: {} }),
  overview: () => json('/api/admin/overview'),
  folders: () => json('/api/admin/folders'),
  preview: (query) => json(`/api/admin/preview?${new URLSearchParams(query)}`),
  settings: (body) => json('/api/admin/settings', { method: 'POST', body }),
  gemini: () => json('/api/admin/gemini'),
  setGemini: (key) => json('/api/admin/gemini', { method: 'POST', body: { key } }),
  attention: () => json('/api/admin/attention'),
  extensions: () => json('/api/admin/extensions'),
  setExtension: (name, enabled) =>
    json('/api/admin/extensions', { method: 'POST', body: { name, enabled } }),
  setFolderVisibility: (folder, visibility, confirm = false) =>
    json('/api/visibility/folder',
      { method: 'POST', body: { folder, visibility, confirm } }),
  undoVisibility: (batchId) =>
    json('/api/visibility/undo', { method: 'POST', body: { batch_id: batchId } }),
  visibilityHistory: () => json('/api/visibility/history'),
  audit: () => json('/api/audit'),
  browse: (path) => json(`/api/library/browse?path=${encodeURIComponent(path || '')}`),
  devices: () => json('/api/devices'),
  deviceBrowse: (path) => json(`/api/devices/browse?path=${encodeURIComponent(path || '')}`),
  setRoot: (path) => json('/api/library/root', { method: 'POST', body: { path } }),
  rescan: (full) => json('/api/scan', { method: 'POST', body: { full } }),
  libraries: () => json('/api/admin/libraries'),
  removeLibrary: (path, force) =>
    json(`/api/admin/libraries?path=${encodeURIComponent(path)}${force ? '&force=1' : ''}`,
      { method: 'DELETE' }),
  setActiveLibrary: (path) =>
    json('/api/admin/libraries/active', { method: 'POST', body: { path } }),
  assignable: () => json('/api/admin/assignable'),
  folder: (path, show = 'all') =>
    json(`/api/admin/folder?folder=${encodeURIComponent(path || '')}`
      + `&show=${encodeURIComponent(show)}`),
  problems: (limit = 12) => json(`/api/admin/problems?limit=${limit}`),
  backups: () => json('/api/admin/backups'),
  backupNow: () => json('/api/admin/backups/now', { method: 'POST' }),
  verifyBackup: () => json('/api/admin/backups/verify', { method: 'POST' }),
  clearProblems: () => json('/api/admin/problems/clear', { method: 'POST' }),
  recycleBin: () => json('/api/recycle'),
  restoreDeleted: (ids) =>
    json('/api/recycle/restore', { method: 'POST', body: { ids } }),
  purgeDeleted: (ids, password) =>
    json('/api/recycle/purge', { method: 'POST', body: { ids, password } }),
  startScrubber: () => json('/api/admin/scrubber/start', { method: 'POST' }),
  scrubberStatus: () => json('/api/admin/scrubber/status'),
  reveal: (assetId) => json(`/api/admin/reveal/${assetId}`, { method: 'POST' }),
  largeFiles: (minMb) =>
    json(`/api/admin/large-files?min_mb=${encodeURIComponent(minMb)}`),
  keepLargeFiles: (ids) =>
    json('/api/admin/large-files/keep', { method: 'POST', body: { ids } }),
  deleteAsset: (id, password) =>
    json('/api/delete', { method: 'POST', body: { ids: [id], password } }),
};

const state = {
  user: null, overview: null, people: [], roles: [], folders: [],
  libraries: [], assignable: [],
};
let currentPreview = 'guest';
let gate;
let profileSheet;
let archive;
let cloud;
let workload;
let diskPanel;
let safetyPanel;
let screenLock;
let aiServer;
let serverPanel;
let performancePanel;
let aiModels;
let firstDay;
let extras;
let migration;
let advanced;
let facesPanel;
let straightenPanel = null;

/* ======================================================================== */

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem('ninaivu.theme', JSON.stringify(theme));
    localStorage.setItem('mv.theme', JSON.stringify(theme));
  } catch { /* private */ }
}

window.addEventListener('storage', (event) => {
  if (event.key === 'ninaivu.theme' || event.key === 'mv.theme') {
    try {
      const next = JSON.parse(event.newValue);
      if (next && ['system', 'light', 'dark'].includes(next)) {
        document.documentElement.dataset.theme = next;
      }
    } catch {}
  }
});

/**
 * The console has no worker: the server answers /sw.js with 404 here, so
 * registering one only logged a 404 on every visit. A browser that installed
 * it before that is still running the old copy, so let that go too. This
 * origin is the console's own port, so the family app's worker is untouched.
 */
function removeServiceWorker() {
  if (!('serviceWorker' in navigator)) return;
  navigator.serviceWorker.getRegistrations()
    .then((registrations) => Promise.all(registrations.map((r) => r.unregister())))
    .catch(() => {});
}

function setupAdminIOSInstallPrompt() {
  const isIPad = /iPad/.test(navigator.userAgent) ||
    (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  const isIPhone = /iPhone|iPod/.test(navigator.userAgent);
  const isIOS = (isIPhone || isIPad) && !window.MSStream;
  const isStandalone = window.navigator.standalone === true || window.matchMedia('(display-mode: standalone)').matches;
  if (!isIOS || isStandalone) return;

  const dismissed = localStorage.getItem('ninaivu:admin-ios-install-dismissed');
  if (dismissed) return;

  const banner = document.getElementById('ios-install-banner');
  const title = document.getElementById('ios-install-title');
  const desc = document.getElementById('ios-install-desc');
  const closeBtn = document.getElementById('ios-install-close');
  if (!banner) return;

  if (isIPad) {
    if (title) title.textContent = 'Install Ninaivu Admin on your iPad';
    if (desc) {
      desc.innerHTML = 'Tap <svg class="ios-share-glyph" viewBox="0 0 24 24"><path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8M16 6l-4-4-4 4M12 2v13"/></svg> Share in Safari’s top toolbar, then select <strong>Add to Home Screen</strong> [+]';
    }
  } else {
    if (title) title.textContent = 'Install Ninaivu Admin on your iPhone';
    if (desc) {
      desc.innerHTML = 'Tap <svg class="ios-share-glyph" viewBox="0 0 24 24"><path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8M16 6l-4-4-4 4M12 2v13"/></svg> Share below, then select <strong>Add to Home Screen</strong> [+]';
    }
  }

  setTimeout(() => {
    banner.hidden = false;
  }, 2500);

  closeBtn?.addEventListener('click', () => {
    banner.hidden = true;
    localStorage.setItem('ninaivu:admin-ios-install-dismissed', '1');
  });
}

async function init() {
  try {
    applyTheme(JSON.parse(localStorage.getItem('ninaivu.theme') || localStorage.getItem('mv.theme') || '"system"'));
  } catch { applyTheme('system'); }

  removeServiceWorker();
  setupAdminIOSInstallPrompt();

  gate = new Gate($('#gate'), {
    toast,
    onSignedIn: (user) => { sessionRestored(); start(user); },
  });
  // Covers the console after fifteen minutes with nobody there; nothing
  // running stops. See static/js/lock.js.
  screenLock = new ScreenLock({ onUnlocked: () => refresh() });

  // A session can end while the console sits open — reset, signed out from
  // another device, or simply expired. Until now every request after that
  // failed with a toast and the only way back was to reload the page by hand.
  // Now the first 401 raises the same sign-in screen the console boots with,
  // over whatever tab is on screen, and signing back in resumes there.
  onUnauthorized(async () => {
    clearInterval(uploadPoll);
    screenLock?.stop();
    // Only a session that started can end. A 401 before anybody has signed in
    // means a request went out too early — show the gate, which is the right
    // thing either way, but do not tell somebody their session expired when
    // they have not had one.
    if (state.user) {
      toast('Your session has ended. Please sign in again.', true);
    }
    let authState = {};
    try {
      authState = await accountsApi.state();
    } catch {
      // Server unreachable rather than signed out. Show the form anyway —
      // it is the only thing that can recover, and it says so when it fails.
      authState = { face: 'admin' };
    }
    gate.show(authState, authState.setup_required ? 'setup' : 'login');
  });
  profileSheet = new ProfileSheet($('#profile-sheet'), {
    face: 'admin',
    toast, onChange: (user) => { state.user = user; renderIdentity(); },
  });

  wireChrome();

  let auth;
  try {
    auth = await accountsApi.state();
    state.auth = auth;
    renderAppLinks(auth);
  } catch {
    toast('Cannot reach the server. Retrying automatically…', true);
    const retryInterval = setInterval(async () => {
      try {
        const a = await accountsApi.state();
        clearInterval(retryInterval);
        state.auth = a;
        renderAppLinks(a);
        toast('Connected to Ninaivu server.');
        if (a.setup_required) return gate.show(a, 'setup');
        if (!a.signed_in || a.user.role !== 'admin') return gate.show(a, 'login');
        start(a.user);
      } catch { /* keep retrying */ }
    }, 3000);
    return;
  }

  // The console is where a brand-new install gets its first administrator.
  if (auth.setup_required) return gate.show(auth, 'setup');
  if (!auth.signed_in || auth.user.role !== 'admin') return gate.show(auth, 'login');
  start(auth.user);
}

async function start(user) {
  if (!user || user.role !== 'admin') {
    gate.show(await accountsApi.state(), 'login');
    return;
  }
  state.user = user;
  renderIdentity();
  await screenLock?.start(user);
  subscribeProgress(onProgress);
  // Everything else that is running, in the same strip — see onActivity.
  subscribeActivity(onActivity);
  // Both ask admin-only endpoints, so they wait for a signed-in administrator.
  loadNotifications();
  loadScrubberStatus();
  await refresh();
  await loadPendingUploads();
  loadAttention();
  // The first day: once, right after the administrator is made.
  firstDay ||= new FirstDay({
    json, toast, openPage: (page) => showTab(page), refresh,
    pickFolder: (options) => openFolderPicker(options),
  });
  firstDay.maybeOpen();
  // Pages an extension brings (the AI server page is Creative Studio's) are
  // shown only while that extension is on.
  refreshExtensions();
  clearInterval(uploadPoll);
  uploadPoll = setInterval(() => {
    if (document.hidden || state.user?.role !== 'admin') return;
    loadPendingUploads();
    // The Overview's "Needs you" and the group marks follow the same clock.
    if (document.querySelector('#tabs button[data-tab="overview"].active')) loadAttention();
  }, 10000);
  // Google's consent screen sends the browser back to `/#cloud?connected=1`,
  // so land on the tab that asked rather than on Overview with no explanation.
  if ((location.hash || '').startsWith('#cloud')) showTab('cloud');
  // The console opens on the Overview, which is where this answer lives.
  safetyPanel?.load();
  if (user.must_change) profileSheet.open(user);
}

async function refresh() {
  try {
    state.overview = await adminApi.overview();
  } catch (exc) {
    toast(exc.message, true);
    return;
  }
  renderOverview();
  renderLibrary();
  await Promise.all([loadPeople(), loadFolders(), loadAssignable()]);
  renderPreview(currentPreview);
}

function renderIdentity() {
  const button = $('#profile-btn');
  button.innerHTML = '';
  if (!state.user) return;
  button.appendChild(avatarNode(state.user, 30));
  button.title = `${state.user.name} · ${state.user.role_label}`;
}

/* ======================================================================== */

function wireChrome() {
  // Enter in a field does what the button beside it does, everywhere.
  enterPressesTheButton();
  $$('#uploads-refresh').onclick = () => { loadPendingUploads(); loadPhoneBackups(); };
  $$('#pb-trusted').onchange = (event) => savePhoneBackupTrust(event.target.checked);
  renderAppLinks();
  $$('#open-home').onclick = () => renderAppLinks();
  // Every handler below is bound with `?.` on purpose. These elements live in
  // admin.html, which Flask compiles once and caches, while this file is
  // served with no-cache and updates immediately. Between an edit and a
  // restart the browser therefore runs new JavaScript against old markup, and
  // one `$('#missing').onclick =` used to throw and abandon the rest of this
  // function — which is how adding a sign-out button silently killed the
  // storage-integrity button forty lines below it.
  document.querySelectorAll('#tabs button').forEach((tab) => {
    tab.onclick = () => showTab(tab.dataset.tab);
  });
  // The markup hides other sections' pages, which is right only for the top bar.
  syncTabVisibility();
  document.querySelectorAll('[data-open-page]').forEach((button) => {
    button.onclick = () => {
      showTab(button.dataset.openPage);
      $('#page-title')?.focus({ preventScroll: true });
      $('#page-title')?.scrollIntoView({ block: 'start' });
    };
  });
  // A group is not a destination of its own: choosing one opens the first page
  // in it, so a click always lands somewhere rather than on an empty section.
  document.querySelectorAll('#tab-groups button').forEach((group) => {
    group.onclick = () => {
      const first = document.querySelector(`#tabs button[data-group="${group.dataset.group}"]`);
      if (first) showTab(first.dataset.tab);
    };
  });
  $$('#theme-btn').onclick = () => {
    const order = ['system', 'light', 'dark'];
    const current = document.documentElement.dataset.theme || 'system';
    applyTheme(order[(order.indexOf(current) + 1) % order.length]);
  };
  $$('#profile-btn').onclick = () => profileSheet.open(state.user);
  $$('#signout-btn').onclick = async () => {
    try {
      await accountsApi.logout();
    } catch { /* the session may already be gone; land on the door either way */ }
    window.location.reload();
  };
  $$('#add-person-btn').onclick = () => toggleAddPerson();
  $$('#change-root').onclick = () => openFolderPicker();
  $$('#rescan-btn').onclick = () => runScan(false);
  $$('#full-rescan-btn').onclick = () => runScan(true);
  $$('#vis-undo-btn').onclick = undoLastVisibility;
  $$('#fold-hidden-only').onchange = (event) => {
    foldState.hiddenOnly = event.target.checked;
    renderFolderScreen();
  };
  $$('#backup-now').onclick = async () => {
    const button = $('#backup-now');
    button.disabled = true;
    const label = button.textContent;
    button.textContent = 'Copying…';
    try {
      await adminApi.backupNow();
      toast('A copy of the index has been kept.');
      await loadBackups();
    } catch (exc) {
      toast(exc.message, true);
    } finally {
      button.disabled = false;
      button.textContent = label;
    }
  };
  $$('#backup-verify').onclick = async () => {
    const button = $('#backup-verify');
    button.disabled = true;
    const label = button.textContent;
    button.textContent = 'Checking…';
    try {
      const result = await adminApi.verifyBackup();
      toast(result.verified.ok ? 'The newest backup restores cleanly.'
        : `The newest backup would not restore: ${result.verified.error}`, !result.verified.ok);
      await loadBackups();
    } catch (exc) {
      toast(exc.message, true);
    } finally {
      button.disabled = false;
      button.textContent = label;
    }
  };
  $$('#problem-refresh').onclick = () => loadProblems();
  $$('#problem-clear').onclick = async () => {
    try {
      await adminApi.clearProblems();
      await loadProblems();
    } catch (exc) { toast(exc.message, true); }
  };
  $$('#bin-refresh').onclick = () => loadRecycleBin();
  $$('#lf-refresh').onclick = () => loadLargeFiles();
  $$('#lf-floor').onchange = () => loadLargeFiles();
  $$('#bin-restore').onclick = () => restoreChosen();
  $$('#bin-delete').onclick = () => purgeChosen();
  $$('#purge-modal').addEventListener('click', (event) => {
    if (event.target === $('#purge-modal')) closePurge();
  });
  $$('#lf-delete-modal').addEventListener('click', (event) => {
    if (event.target === $('#lf-delete-modal')) $('#lf-delete-cancel')?.click();
  });
  $$('#bin-all').onclick = () => {
    const present = binState.items.filter((row) => row.present);
    const all = present.length && present.every((row) => binState.chosen.has(row.id));
    binState.chosen = all ? new Set() : new Set(present.map((row) => row.id));
    renderRecycleBin();
  };
  $$('#fm-manual').addEventListener('input', refreshApply);
  $$('#fm-cancel').onclick = () => ($('#folder-modal').hidden = true);
  $$('#fm-apply').onclick = () => applyRoot();

  // The Archive tab is a self-contained tool; hand it the console's toast,
  // the shared folder picker and a way to say "the library changed".
  archive = new ArchivePanel({
    toast,
    pickFolder: (options) => openFolderPicker(options),
    onLibraryChanged: () => refresh(),
    getDevices: () => adminApi.devices(),
  });
  archive.wire();

  // The Cloud tab is the same shape: its own module, the console's toast, and
  // it only polls while it is the tab on screen.
  cloud = new CloudPanel({ toast });
  cloud.wire();
  workload = new WorkloadPanel({ toast });
  workload.wire();
  diskPanel = new DiskPanel({ toast });
  diskPanel.wire();
  safetyPanel = new SafetyPanel({ toast, openPage: (page) => showTab(page) });
  safetyPanel.wire();

  // The AI server tab: its own module too. It loads when shown and never polls.
  aiServer = new AIServerPanel({ toast });
  aiServer.wire();
  aiModels = new AIModelsPanel({ toast });
  extras = new ComponentsPanel({ toast });
  // Migration: what to copy to another machine, and where the library
  // went once it is there. It asks the server nothing until it is open.
  migration = new MigrationPanel({ toast });
  advanced = new AdvancedPanel({ toast });
  advanced.wire();

  // The Server page: the desktop control panel's readings and controls. It
  // only polls while it is the page on screen.
  serverPanel = new ServerPanel({ toast });
  serverPanel.wire();
  // A change that needs a restart goes through the Server page's own restart,
  // which asks first and then waits for Ninaivu to come back.
  performancePanel = new PerformancePanel({
    toast,
    openPage: (page) => showTab(page),
    restart: () => { showTab('server'); serverPanel.confirmRestart(null); },
  });
  performancePanel.wire();

  // The Faces tab: same self-contained shape, and it only asks the server
  // anything while it is the tab on screen.
  facesPanel = new FacesPanel({ toast });
  facesPanel.wire();

  // The Straighten tab: a long-running pass over the library, so like the
  // others it only polls while it is the tab on screen.
  straightenPanel = new StraightenPanel({ toast });
  straightenPanel.wire();
  $$('#faces-review-close').onclick = () => ($('#faces-review').hidden = true);
  $$('#faces-review').addEventListener('click', (event) => {
    if (event.target === $('#faces-review')) $('#faces-review').hidden = true;
  });
  wireScrubber();
  wireLocations();
  wireNotifications();
  wireDigest();
  $$('#delete-cancel').onclick = () => closeDelete();
  $$('#delete-modal').addEventListener('click', (event) => {
    if (event.target === $('#delete-modal')) closeDelete();
  });
  $$('#profile-sheet').addEventListener('click', (event) => {
    if (event.target === $('#profile-sheet')) $('#profile-sheet').hidden = true;
  });
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    const open = [...document.querySelectorAll('.modal, .sheet')].find((m) => !m.hidden);
    if (open) open.hidden = true;
  });
}

// The line under each page's title. Kept beside showTab rather than in the
// markup so a page added to #tabs without one simply shows no line.
const PAGE_DESCRIPTIONS = {
  overview: 'Your family library, at a glance.',
  folders: 'Browse, organize and care for your media files.',
  'large-files': 'Find the files taking up the most space.',
  archive: 'Bring your memories together in one organized archive.',
  people: 'Manage the people who share your library.',
  visibility: 'The rules for everyone, then who can see each part of your library.',
  faces: 'Review and organize the people in your photos.',
  uploads: 'Review family contributions before they enter the library.',
  straighten: 'Review suggested orientation corrections.',
  'ai-models': 'What the scan does with AI, and the models it does it with.',
  'ai-server': 'Configure the service that powers your AI features.',
  library: 'Library folders, and how they are indexed.',
  cloud: 'Mugil: the encrypted copy of the library in the cloud, and how it is going.',
  restore: 'Bring photographs back from the cloud copy — carefully, and never over what is here.',
  health: 'Drives, storage checks, problems and the copies of the index.',
  settings: 'This home\u2019s name, extensions, and what is installed on this computer.',
  activity: 'Check recent activity, problems and state backups.',
  extras: 'Install what Ninaivu can run without but is better with, on this computer.',
  migration: 'Move Ninaivu to another computer, and tell it where the library went.',
  advanced: 'Every setting in its group, with what it means and its default.',
  server: 'Watch the machine Ninaivu runs on, change its resource mode, restart it and read its log.',
  performance: 'What this computer can do for Ninaivu, and what would help it do more.',
};

// Keep in step with the sidebar breakpoint in admin.css.
const sidebarLayout = window.matchMedia('(min-width: 1100px)');

// The sidebar lists every page under its section; the narrow top bar shows
// only the pages of the chosen section.
const activeExtensions = new Set();

function syncTabVisibility() {
  const group = document.querySelector('#tab-groups button.active')?.dataset.group || '';
  document.querySelectorAll('#tabs button').forEach((tab) => {
    const needs = tab.dataset.needsExtension;
    tab.hidden = (!sidebarLayout.matches && Boolean(group) && tab.dataset.group !== group)
      || Boolean(needs && !activeExtensions.has(needs));
  });
}
sidebarLayout.addEventListener('change', syncTabVisibility);

// Scroll *box* just enough that *item* is in it. Not scrollIntoView: that
// scrolls every ancestor too, so opening a page from a shortcut halfway down
// the Overview would move the page as well as the list.
function keepInView(item, box) {
  if (!item || !box) return;
  const inner = item.getBoundingClientRect();
  const outer = box.getBoundingClientRect();
  const margin = 24;
  if (box.scrollWidth > box.clientWidth) {
    if (inner.left < outer.left) box.scrollLeft -= outer.left - inner.left + margin;
    else if (inner.right > outer.right) box.scrollLeft += inner.right - outer.right + margin;
  }
  if (box.scrollHeight > box.clientHeight) {
    if (inner.top < outer.top) box.scrollTop -= outer.top - inner.top + margin;
    else if (inner.bottom > outer.bottom) box.scrollTop += inner.bottom - outer.bottom + margin;
  }
}

function showTab(name) {
  // Which group owns this page. Deep links and the post-sign-in resume both
  // call showTab directly, so the group row is derived here rather than in the
  // click handlers -- otherwise arriving at a page would leave the wrong
  // section highlighted and its siblings hidden.
  const opened = document.querySelector(`#tabs button[data-tab="${name}"]`);
  const group = opened ? opened.dataset.group : '';
  const title = $('#page-title');
  // The label alone: the Uploads tab also carries a live count, which the
  // heading would freeze at whatever it was when the page opened.
  if (title) title.textContent = opened?.querySelector('.tab-label')?.textContent || 'Overview';
  const section = $('#page-section');
  if (section) section.textContent =
    document.querySelector(`#tab-groups button[data-group="${group}"]`)?.textContent || 'Home';
  const description = $('#page-description');
  if (description) description.textContent = PAGE_DESCRIPTIONS[name] || '';
  document.querySelectorAll('#tab-groups button').forEach((button) => {
    const selected = button.dataset.group === group;
    button.classList.toggle('active', selected);
    button.setAttribute('aria-selected', String(selected));
  });
  syncTabVisibility();
  document.querySelectorAll('#tabs button').forEach((tab) => {
    const selected = tab.dataset.tab === name;
    tab.classList.toggle('active', selected);
    // #tabs declares role="tablist"/role="tab", which promises a screen
    // reader that the current tab is exposed via aria-selected — the visual
    // ".active" class alone says nothing to assistive tech.
    tab.setAttribute('aria-selected', String(selected));
  });
  // A page opened from elsewhere — a shortcut, a deep link, the resume after
  // sign-in — may be one the sidebar or the phone's tab row has scrolled out
  // of sight; the highlight is no use where nobody can see it.
  keepInView(opened, sidebarLayout.matches ? opened?.closest('.admin-nav') : $('#tabs'));
  if (!sidebarLayout.matches) {
    keepInView(document.querySelector('#tab-groups button.active'), $('#tab-groups'));
  }
  document.querySelectorAll('.panel').forEach(
    (panel) => panel.classList.toggle('active', panel.dataset.panel === name));
  if (name === 'activity') loadActivity();
  if (name === 'overview') safetyPanel?.load();
  if (name === 'uploads') { loadPendingUploads(); loadPhoneBackups(); }
  if (name === 'visibility') { loadFolders(); refreshUndo(); }
  if (name === 'folders') { loadFolderScreen(foldState.path); loadRecycleBin(); }
  if (name === 'large-files') loadLargeFiles();
  // Loaded when the page opens, the way every other panel here loads —
  // not once at sign-in. A section whose only chance to fill itself was
  // the moment somebody signed in stays blank until a reload if that one
  // call fails, and cannot show what today would send.
  if (name === 'health') { loadProblems(); loadBackups(); loadDigest(); }
  if (name === 'library') { renderLibraryFolders(); loadLocations(); }
  // Only the visible tab holds an event stream open.
  if (name === 'archive') archive?.show(); else archive?.hide();
  if (name === 'cloud') cloud?.show(); else cloud?.hide();
  if (name === 'restore') cloud?.showRestore(); else cloud?.hideRestore();
  if (name === 'activity') workload?.show(); else workload?.hide();
  if (name === 'health') diskPanel?.show(); else diskPanel?.hide();
  if (name === 'ai-server') aiServer?.show();
  if (name === 'server') serverPanel?.show(); else serverPanel?.hide();
  if (name === 'performance') performancePanel?.show(); else performancePanel?.hide();
  if (name === 'ai-models') aiModels?.show(); else aiModels?.hide();
  if (name === 'ai-models' || name === 'settings') refreshExtensions();
  if (name === 'settings') extras?.show(); else extras?.hide();
  if (name === 'migration') migration?.show(); else migration?.hide();
  if (name === 'advanced') advanced?.show(); else advanced?.hide();
  if (name === 'faces') facesPanel?.show(); else facesPanel?.hide();
  if (name === 'straighten') straightenPanel?.show(); else straightenPanel?.hide();
  if (name === 'overview') {
    // Visibility may have changed on another tab; re-ask rather than
    // showing a stale "what will they see" answer.
    renderPreview(currentPreview);
    loadAttention();
  }
}

/* -- Overview ------------------------------------------------------------ */

/* The "Family app" link lives in the top bar, so it has to be correct from the
   first paint rather than after the Library tab happens to render. Open the
   console from a laptop against the machine in the study and `localhost`
   would be the laptop's own localhost, so it is composed from the address this
   browser is already talking to.

   If opened via mDNS (e.g. `ninaivu-admin.local`), pointing straight back at
   `location.hostname` hits HostRouter which serves the admin console again;
   so `-admin` is stripped from the mDNS hostname (yielding `ninaivu.local`).
   Similarly, default HTTP/HTTPS ports (80/443) are cleanly formatted without
   spurious port suffixes. */
function computeFamilyUrl(appInfo) {
  const info = appInfo || state.overview?.app || state.auth || {};
  const currentProto = window.location.protocol;
  const currentHost = window.location.hostname;

  let homePort = info.home_port;
  if (!homePort) {
    homePort = currentProto === 'https:' ? 443 : 80;
  }

  let scheme = info.scheme;
  if (!scheme) {
    if (homePort === 443) {
      scheme = 'https:';
    } else if (homePort === 80) {
      scheme = 'http:';
    } else {
      scheme = currentProto;
    }
  } else if (!scheme.endsWith(':')) {
    scheme = `${scheme}:`;
  }

  let targetHost = currentHost;
  const hostnames = info.hostnames || {};
  if (hostnames.family && (currentHost.toLowerCase() === hostnames.admin?.toLowerCase())) {
    targetHost = hostnames.family;
  } else if (/-admin\.local$/i.test(targetHost)) {
    targetHost = targetHost.replace(/-admin\.local$/i, '.local');
  } else if (/-admin$/i.test(targetHost)) {
    targetHost = targetHost.replace(/-admin$/i, '');
  }

  const isDefaultPort = (scheme === 'https:' && homePort === 443) || (scheme === 'http:' && homePort === 80);
  const portSuffix = isDefaultPort ? '' : `:${homePort}`;
  return `${scheme}//${targetHost}${portSuffix}/`;
}

function renderAppLinks(appInfo) {
  const link = $('#open-home');
  if (!link) return;
  link.href = computeFamilyUrl(appInfo);
}

function renderOverview() {
  renderAppLinks();
  const data = state.overview;
  const stats = data.stats || {};
  const cards = $('#overview-cards');
  cards.innerHTML = '';

  // Every card opens the page that changes what it counts. A number that
  // says "AI: off" and stops there tells you the problem and hides the fix.
  const card = (label, value, sub, tone, page) => {
    const node = el(page ? 'button' : 'div', `card${tone ? ` ${tone}` : ''}`);
    if (page) {
      node.type = 'button';
      node.onclick = () => showTab(page);
      node.setAttribute('aria-label', `${label}: ${value}. Open ${page.replace('-', ' ')}`);
    }
    node.appendChild(el('div', 'card-label', label));
    node.appendChild(el('div', 'card-value', value));
    if (sub) node.appendChild(el('div', 'card-sub', sub));
    return node;
  };

  cards.append(
    card('In the library', (stats.count || 0).toLocaleString(),
      `${stats.pictures || 0} photos · ${stats.videos || 0} videos · ${stats.audio || 0} audio`,
      '', 'folders'),
    card('Public', (stats.public || 0).toLocaleString(), 'visible to guests',
      stats.public ? 'good' : '', 'visibility'),
    card('Hidden', (stats.hidden || 0).toLocaleString(), 'admins only',
      stats.hidden ? 'warn' : '', 'visibility'),
    card('Profiles', String(data.people.total),
      Object.entries(data.people.by_role)
        .filter(([, n]) => n)
        .map(([role, n]) => `${n} ${role}`).join(' · '), '', 'people'),
    card('Signed in now', String(data.people.sessions), 'active sessions', '', 'activity'),
    aiCard(card, data.ai || {}),
  );
}

/* Ninaivu started with AI off says "none" for both its engine and its model,
   and the card read "none / none". */
function aiCard(card, ai) {
  if (ai.semantic) return card('AI', 'Semantic', ai.model || 'search and tagging', '', 'ai-models');
  if (!ai.engine || ai.engine === 'none') {
    return card('AI', 'Off', 'search by what is in a photo is off — turn it on', '', 'ai-models');
  }
  if (ai.engine === 'loading') return card('AI', 'Starting', 'search and tagging', '', 'ai-models');
  return card('AI', ai.engine,
    ai.model && ai.model !== 'none' ? ai.model : 'search and tagging', '', 'ai-models');
}

async function loadAssignable() {
  try {
    state.assignable = (await adminApi.assignable()).folders;
  } catch { state.assignable = []; }
}

function renderLibraryFolders() {
  const list = $('#library-list');
  if (!list) return;
  const folders = state.overview?.library?.folders || [];
  list.innerHTML = '';

  if (!folders.length) {
    list.appendChild(el('p', 'hint', 'No folders yet — add one to get started.'));
    return;
  }

  for (const folder of folders) {
    const row = el('div', `library-item${folder.exists ? '' : ' missing'}`);

    const main = el('div', 'li-main');
    const title = el('div', 'li-name');
    title.appendChild(el('strong', null, folder.name));
    if (folder.active) title.appendChild(el('span', 'tag family', 'default'));
    if (!folder.exists) title.appendChild(el('span', 'tag hidden', 'missing'));
    main.appendChild(title);
    main.appendChild(el('code', 'li-path', folder.path));
    const bits = [`${folder.count.toLocaleString()} items`];
    if (folder.assigned) {
      bits.push(`${folder.assigned} ${folder.assigned === 1 ? 'person' : 'people'} assigned`);
    }
    main.appendChild(el('div', 'li-meta', bits.join(' · ')));
    row.appendChild(main);

    const actions = el('div', 'li-actions');
    if (!folder.active) {
      const makeDefault = el('button', 'btn small ghost', 'Make default');
      makeDefault.type = 'button';
      makeDefault.title = 'The folder the Visibility tab works on';
      makeDefault.onclick = async () => {
        try {
          await adminApi.setActiveLibrary(folder.path);
          await refresh();
        } catch (exc) { toast(exc.message, true); }
      };
      actions.appendChild(makeDefault);
    }
    const remove = el('button', 'btn small ghost', 'Remove');
    remove.type = 'button';
    remove.title = 'Stop indexing this folder. Your files are not touched.';
    remove.onclick = () => removeLibrary(folder);
    actions.appendChild(remove);
    row.appendChild(actions);

    list.appendChild(row);
  }
}

async function removeLibrary(folder) {
  try {
    await adminApi.removeLibrary(folder.path, false);
    toast(`${folder.name} is no longer indexed. Your files were not touched.`);
    await refresh();
  } catch (exc) {
    if (exc.status === 409) {
      // Someone is assigned to it — say who, and let the admin decide.
      if (!confirm(`${exc.message}\n\nRemove it anyway and clear those assignments?`)) return;
      try {
        await adminApi.removeLibrary(folder.path, true);
        toast(`${folder.name} removed; assignments cleared.`);
        await refresh();
      } catch (inner) { toast(inner.message, true); }
      return;
    }
    toast(exc.message, true);
  }
}

function renderLibrary() {
  const data = state.overview;
  renderLibraryFolders();
  // Open the console from a laptop against the machine in the study and
  const box = $('#overview-library');
  box.innerHTML = '';
  const folderCount = (data.library.folders || []).length;
  const rows = [
    ['Library folders', folderCount
      ? `${folderCount} folder${folderCount === 1 ? '' : 's'}`
      : 'None yet'],
    ['Family app', data.app.home_url],
    ['Guests without a login', data.app.open_browsing ? 'Allowed' : 'Blocked'],
    ['Watching for changes', data.app.watch ? 'Yes' : 'No'],
  ];
  const list = el('dl', 'kv');
  for (const [key, value] of rows) {
    list.appendChild(el('dt', null, key));
    const dd = el('dd');
    if (key === 'Family app' && value && (value.startsWith('http://') || value.startsWith('https://'))) {
      const a = el('a', null, value);
      a.href = value;
      a.target = '_blank';
      a.rel = 'noopener';
      dd.appendChild(a);
    } else {
      dd.textContent = value;
    }
    // The default folder's path on a line of its own, in the same type the
    // Settings page shows paths in: run on after the count it broke mid-word
    // wherever the column happened to end.
    if (key === 'Library folders' && folderCount && data.library.root) {
      dd.appendChild(el('code', 'kv-path', data.library.root));
    }
    list.appendChild(dd);
  }
  box.appendChild(list);

  // Settings. Each switch is rendered on the page of the thing it governs:
  // the rules for everyone on Visibility, indexing on Library settings, the
  // AI passes on AI models, and only this home's name here on Settings. They
  // all sat on one page before, three pages from what they changed.
  const settings = $('#settings');
  const access = $('#access-settings') || settings;
  const indexing = $('#library-switches') || settings;
  const aiSwitches = $('#ai-switches') || settings;
  for (const box of new Set([settings, access, indexing, aiSwitches])) box.innerHTML = '';
  renderDatePolicy(access);

  // What the household is called. This is the default everyone sees; family
  // members may keep their own name for it instead, which only they see.
  const nameBlock = el('div', 'setting-field');
  nameBlock.appendChild(el('label', null, 'Name for this home'));
  const nameRow = el('div', 'row');
  const nameInput = el('input', 'input');
  nameInput.value = data.app.house_name || '';
  nameInput.maxLength = 40;
  nameInput.placeholder = 'Ninaivu';
  nameInput.setAttribute('aria-label', 'Name for this home');
  const nameSave = el('button', 'btn', 'Save');
  nameSave.type = 'button';
  const saveHouseName = async () => {
    nameSave.disabled = true;
    try {
      const result = await adminApi.settings({ house_name: nameInput.value });
      state.overview.app.house_name = result.settings.house_name;
      nameInput.value = result.settings.house_name || '';
      toast(`Everyone now sees \u201c${result.settings.house_name_effective}\u201d.`);
    } catch (exc) {
      toast(exc.message, true);
    } finally {
      nameSave.disabled = false;
    }
  };
  nameSave.onclick = saveHouseName;
  nameInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') saveHouseName(); });
  nameRow.append(nameInput, nameSave);
  nameBlock.appendChild(nameRow);
  nameBlock.appendChild(el('p', 'hint',
    'Shown at the top of the family app and on the home screen icon. '
    + 'Family members can keep their own name for it instead \u2014 that one '
    + 'is private to them.'));
  settings.appendChild(nameBlock);
  const toggles = [
    [access, 'open_browsing', 'Let visitors browse public media without signing in',
      data.app.open_browsing],
    [access, 'nsfw_filter', 'Screen explicit content and hide it behind a toggle',
      data.app.nsfw_filter],
    [access, 'hide_screens', 'Hide screenshots, documents and photos of screens (admins only)',
      data.app.hide_screens ?? true],
    [indexing, 'watch', 'Watch the folder and index new files automatically', data.app.watch],
    [aiSwitches, 'video_keyframes', 'Describe videos by several moments across the clip',
      (data.app.video_keyframes ?? 5) >= 2,
      'A holiday video is a beach, then a restaurant, then a car park, and one '
      + 'frame near the start describes none of them. It is also the slowest '
      + 'thing a scan does — five decodes and five passes through the model '
      + 'per video, which on a processor is hours for a few thousand clips. '
      + 'Turned off, videos are still found and still searchable; they are '
      + 'described by their poster frame alone.'],
    // The three below are off by default and until now had no switch at all
    // — only a start-up flag, or config.json by hand — so a household had no
    // way to know they existed, let alone turn them on.
    [aiSwitches, 'place_names', 'Name the places photographs were taken',
      data.app.place_names ?? false,
      'Turns the coordinates a phone records into a town and a country you '
      + 'can search for. Fast — a lookup against a list kept on this '
      + 'machine; twenty thousand photographs take seconds. The list, about '
      + '11 MB, is downloaded once, the first time.'],
    [aiSwitches, 'ocr_enabled', 'Read the words in photographs',
      data.app.ocr_enabled ?? false,
      'So a search for a shop name, a menu or a street sign finds the '
      + 'photograph it is in. Needs the text reader from Extras. Slow on a '
      + 'processor, and it reads every photograph once.'],
    [aiSwitches, 'faces_enabled', 'Find the people in photographs',
      data.app.faces_enabled ?? false,
      'Groups photographs by who is in them. The slowest thing a scan does '
      + 'after describing videos — roughly half a second a photograph '
      + 'on a processor, so most of a day for a large library. Turned off '
      + 'part-way, it stops where it is and carries on from there next time.'],
  ];
  const needLines = {};                     // key → the line to fill in below
  for (const [where, key, label, value, hint] of toggles) {
    const row = el('label', 'toggle');
    const input = el('input');
    input.type = 'checkbox';
    input.checked = value;
    input.onchange = async () => {
      try {
        const result = await adminApi.settings({ [key]: input.checked });
        toast('Saved.');
        // What the server made of it, not what was sent: this row is a switch
        // over a setting that is a number, and storing the checkbox back would
        // leave the page believing "5 moments" was `true`.
        const saved = result?.settings?.[key];
        state.overview.app[key] = saved === undefined ? input.checked : saved;
      } catch (exc) {
        toast(exc.message, true);
        input.checked = !input.checked;
      }
    };
    row.append(input, el('span', null, label));
    where.appendChild(row);
    if (hint) where.appendChild(el('p', 'hint', hint));
    if (where === aiSwitches) {
      const line = el('p', 'hint switch-needs');
      line.hidden = true;
      where.appendChild(line);
      needLines[key] = [line, value];
    }
  }
  annotateSwitches(needLines);

  const caps = $('#caps');
  caps.innerHTML = '';
  const labels = {
    ffmpeg: 'ffmpeg — video thumbnails and metadata',
    heif: 'pillow-heif — iPhone HEIC photos',
    opencv: 'OpenCV — fallback video frames',
  };
  for (const [key, present] of Object.entries(data.capabilities)) {
    const row = el('div', `cap ${present ? 'on' : 'off'}`);
    row.appendChild(el('span', 'cap-dot'));
    row.appendChild(el('span', null, labels[key] || key));
    row.appendChild(el('span', 'cap-state', present ? 'installed' : 'not installed'));
    caps.appendChild(row);
  }
}

/* What each AI switch needs on this machine, and whether it is here — from
   the models page's answer, filled in after the switches are drawn. A switch
   that is on with nothing behind it is not a feature working; it says so,
   and links to the model. Failure just leaves the lines hidden. */
async function annotateSwitches(needLines) {
  let readiness;
  try {
    readiness = (await json('/api/admin/ai-models')).readiness || {};
  } catch {
    return;
  }
  for (const [key, [line, value]] of Object.entries(needLines)) {
    const need = readiness[key];
    if (!need || !need.needs) continue;
    line.replaceChildren();
    line.className = `hint switch-needs ${need.ready ? 'ready' : 'missing'}`;
    if (need.ready) {
      line.textContent = `Ready: ${need.needs} is installed.`;
    } else {
      line.append(value
        ? `On, but nothing happens yet: needs ${need.needs}, which is not installed. `
        : `Needs ${need.needs}, which is not installed. `);
      if (need.model) {
        const link = el('a', null, 'Install it below');
        link.href = `#model-${need.model}`;
        link.onclick = (e) => {
          e.preventDefault();
          document.querySelector(`[data-model-id="${need.model}"]`)?.scrollIntoView({ behavior: 'smooth', block: 'center' });
        };
        line.append(link, '.');
      }
    }
    line.hidden = false;
  }
}

/* -- Needs you ------------------------------------------------------------ */

// The queues, counted, at the top of the Overview. A row is shown only when
// something is in it, so a quiet library says so in one line rather than
// listing four things that are empty. The sidebar groups that hold a queue
// with something in it are marked as well.
const ATTENTION_GROUPS = { uploads: 'queues', straighten: 'queues', faces: 'people', problems: 'backup' };

async function loadAttention() {
  const list = $('#attention-list');
  const headline = $('#attention-headline');
  if (!list || !headline) return;
  let data;
  try {
    data = await adminApi.attention();
  } catch {
    headline.textContent = 'Could not check what is waiting.';
    return;
  }
  const waiting = (data.items || []).filter((item) => item.count > 0);
  headline.textContent = waiting.length
    ? `${data.total} ${data.total === 1 ? 'thing is' : 'things are'} waiting for you.`
    : 'Nothing is waiting for you.';
  list.replaceChildren();
  const groups = new Set(waiting.map((item) => ATTENTION_GROUPS[item.key]).filter(Boolean));
  document.querySelectorAll('#tab-groups button[data-group]').forEach((button) => {
    if (button.dataset.group in { queues: 1, people: 1, backup: 1 }) {
      button.classList.toggle('needs-attention', groups.has(button.dataset.group));
    }
  });
  for (const item of waiting) {
    const button = document.createElement('button');
    button.type = 'button';
    button.dataset.openPage = item.page;
    const count = document.createElement('span');
    count.className = 'shortcut-symbol attention-count';
    count.textContent = item.count > 99 ? '99+' : String(item.count);
    const text = document.createElement('span');
    const title = document.createElement('strong');
    title.textContent = item.title;
    const detail = document.createElement('small');
    detail.textContent = item.detail;
    text.append(title, detail);
    const arrow = document.createElement('span');
    arrow.setAttribute('aria-hidden', 'true');
    arrow.textContent = '→';
    button.append(count, text, arrow);
    button.onclick = () => showTab(item.page);
    list.append(button);
  }
}

/* -- Extensions ----------------------------------------------------------- */

// Each installed extension, with what it does when it is on. The switch is
// saved at once and takes effect at the next start, and the page says so.
function showExtensions(listing) {
  const list = $('#extensions-list');
  const none = $('#extensions-none');
  const state = $('#extensions-state');
  if (!list) return;
  list.replaceChildren();
  const items = listing.extensions || [];
  none.hidden = items.length > 0;
  const active = new Set(listing.active || []);
  activeExtensions.clear();
  for (const name of active) activeExtensions.add(name);
  syncTabVisibility();
  const pending = items.filter((e) => e.enabled !== active.has(e.name)).length;
  state.textContent = pending ? 'Restart Ninaivu to apply' : (active.size ? `${active.size} on` : 'All off');
  for (const ext of items) {
    const li = document.createElement('li');
    li.className = 'ext-row';
    const label = document.createElement('label');
    label.className = 'switch';
    const box = document.createElement('input');
    box.type = 'checkbox';
    box.checked = !!ext.enabled;
    box.disabled = (ext.problems || []).length > 0 && !ext.enabled;
    box.addEventListener('change', async () => {
      box.disabled = true;
      try {
        showExtensions(await adminApi.setExtension(ext.name, box.checked));
        toast(box.checked
          ? `${ext.title} is on from the next start of Ninaivu.`
          : `${ext.title} is off from the next start of Ninaivu.`);
      } catch (exc) {
        box.checked = !box.checked;
        toast(exc.message, true);
      } finally {
        box.disabled = false;
      }
    });
    const title = document.createElement('strong');
    title.textContent = ext.title || ext.name;
    label.append(box, ' ', title);
    li.append(label);
    const summary = document.createElement('p');
    summary.className = 'hint';
    summary.textContent = ext.summary || '';
    li.append(summary);
    const leaves = document.createElement('p');
    leaves.className = 'hint';
    if (ext.data_leaves_the_machine) {
      const strong = document.createElement('strong');
      strong.textContent = 'When it is on, something leaves this computer: ';
      leaves.append(strong, ext.destination || 'see its README.');
    } else {
      leaves.textContent = 'Nothing leaves this computer.';
    }
    li.append(leaves);
    if (ext.downloads) {
      const dl = document.createElement('p');
      dl.className = 'hint subtle';
      dl.textContent = `Downloads: ${ext.downloads}`;
      li.append(dl);
    }
    for (const problem of ext.problems || []) {
      const p = document.createElement('p');
      p.className = 'hint';
      p.textContent = `Not usable: ${problem}`;
      li.append(p);
    }
    list.append(li);
  }
  // The Gemini key form belongs to that extension and only makes sense while
  // its routes are there — that is, while it is on in the running server.
  const gemini = $('#gemini-block');
  if (gemini) {
    gemini.hidden = !active.has('gemini');
    if (!gemini.hidden) refreshGemini();
  }
}

async function refreshExtensions() {
  try {
    showExtensions(await adminApi.extensions());
  } catch { /* the page still works without it */ }
}

/* -- Google Gemini key --------------------------------------------------- */

// The key is never sent back to the page. What comes back is whether there is
// one, where it came from, and its last four characters.
function showGemini(status) {
  const state = $('#gemini-state');
  const input = $('#gemini-key');
  const save = $('#gemini-save');
  const remove = $('#gemini-remove');
  if (!state || !input) return;
  const fromEnvironment = status.source === 'environment';
  if (!status.set) {
    state.textContent = 'Off — no key';
  } else if (fromEnvironment) {
    state.textContent = `On — key ${status.hint} from ${status.variable}`;
  } else {
    state.textContent = `On — key ${status.hint}`;
  }
  // A key from the server's environment wins over one typed here, so offering
  // to save one would be offering to write down something never used.
  input.disabled = fromEnvironment;
  save.disabled = fromEnvironment;
  input.placeholder = fromEnvironment ? 'Set in the server’s environment'
    : status.set ? 'Paste a new key to replace it' : 'Paste an API key';
  remove.hidden = !status.set || fromEnvironment;
}

async function refreshGemini() {
  wireGemini();
  try {
    showGemini(await adminApi.gemini());
  } catch { /* the page still works without it */ }
}

function wireGemini() {
  const form = $('#gemini-form');
  if (!form || form.dataset.wired) return;
  form.dataset.wired = '1';
  const input = $('#gemini-key');
  const save = $('#gemini-save');
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const key = input.value.trim();
    if (!key) return;
    save.disabled = true;
    save.textContent = 'Checking…';
    try {
      showGemini(await adminApi.setGemini(key));
      toast('Gemini is on. The key worked.');
    } catch (exc) {
      toast(exc.message, true);
    } finally {
      // Cleared either way: a key sitting in a form field is a key anybody
      // walking past the screen can copy.
      input.value = '';
      save.disabled = false;
      save.textContent = 'Save';
    }
  });
  $('#gemini-remove')?.addEventListener('click', async () => {
    try {
      showGemini(await adminApi.setGemini(''));
      toast('Gemini is off. The key was removed from this machine.');
    } catch (exc) {
      toast(exc.message, true);
    }
  });
}

/* -- Preview ------------------------------------------------------------- */

function renderPreviewTabs(active) {
  const tabs = $('#preview-tabs');
  tabs.innerHTML = '';
  const options = [
    { key: 'guest', label: 'A guest' },
    { key: 'family', label: 'A family member' },
    ...state.people
      .filter((p) => p.role !== 'admin' && p.active)
      .map((p) => ({ key: `person:${p.id}`, label: p.name })),
  ];
  for (const option of options) {
    const button = el('button', `chip${option.key === active ? ' active' : ''}`,
      option.label);
    button.type = 'button';
    button.onclick = () => renderPreview(option.key);
    tabs.appendChild(button);
  }
}

async function renderPreview(key) {
  currentPreview = key;
  renderPreviewTabs(key);
  const box = $('#preview');
  box.innerHTML = '<p class="hint">Checking…</p>';
  const query = key.startsWith('person:')
    ? { person: key.split(':')[1] }
    : { role: key };
  let data;
  try {
    data = await adminApi.preview(query);
  } catch (exc) {
    box.innerHTML = '';
    box.appendChild(el('p', 'hint', exc.message));
    return;
  }

  box.innerHTML = '';
  const summary = el('div', 'preview-summary');
  summary.appendChild(el('strong', null, `${data.total.toLocaleString()} items`));
  const detail = [`as ${data.as}`];
  if (data.scope) detail.push(`limited to ${data.scope}`);
  summary.appendChild(el('span', 'hint', detail.join(' · ')));
  box.appendChild(summary);

  if (!data.total) {
    box.appendChild(el('p', 'empty-note',
      'Nothing at all. Publish a folder on the Visibility tab to let them see something.'));
    return;
  }

  const strip = el('div', 'preview-strip');
  for (const item of data.items.slice(0, 12)) {
    const tile = el('div', 'preview-tile');
    if (item.has_thumb) {
      const img = el('img');
      img.src = thumbUrl(item.id, 256, item.thumb_v);
      img.alt = item.name;
      img.loading = 'lazy';
      tile.appendChild(img);
    } else {
      tile.appendChild(el('span', 'preview-glyph', '♪'));
    }
    tile.title = `${item.name} — ${item.folder || 'root'}`;
    strip.appendChild(tile);
  }
  box.appendChild(strip);
}

/* -- People -------------------------------------------------------------- */

async function loadPeople() {
  try {
    const data = await accountsApi.people();
    state.people = data.people;
    state.roles = data.roles;
  } catch (exc) {
    toast(exc.message, true);
    return;
  }
  renderPeople();
}

function renderPeople() {
  state.libraryCount = (state.overview?.library?.folders || []).length;
  const list = $('#people-list');
  list.innerHTML = '';
  for (const person of state.people) list.appendChild(personCard(person));
}

function personCard(person) {
  const card = el('div', `admin-person${person.active ? '' : ' disabled'}`);

  const head = el('div', 'ap-head');
  head.appendChild(avatarNode(person, 44));
  const identity = el('div', 'ap-identity');
  const line = el('div', 'ap-name');
  line.appendChild(el('strong', null, person.name));
  line.appendChild(roleBadge(person.role, person.role_label));
  if (person.id === state.user.id) line.appendChild(el('span', 'you-tag', 'you'));
  if (!person.active) line.appendChild(el('span', 'off-tag', 'disabled'));
  identity.appendChild(line);

  const bits = [`@${person.username}`];
  if (person.role === 'admin') bits.push('every folder');
  else if (person.library) bits.push(`only ${person.library}`);
  else if (person.scope) bits.push(`only ${person.scope}`);
  else bits.push('all library folders');
  if (typeof person.visible_count === 'number') {
    bits.push(`${person.visible_count.toLocaleString()} items visible`);
  }
  bits.push({
    password: 'password required', pin: 'PIN required', open: 'tap to enter',
  }[person.entry]);
  if (person.sessions) bits.push(`${person.sessions} device${person.sessions > 1 ? 's' : ''}`);
  identity.appendChild(el('div', 'ap-meta', bits.join(' · ')));
  head.appendChild(identity);
  card.appendChild(head);

  const controls = el('div', 'ap-controls');

  controls.appendChild(labelled('Role', (() => {
    const select = el('select', 'select');
    for (const role of state.roles) {
      const option = el('option', null, role.label);
      option.value = role.value;
      if (role.value === person.role) option.selected = true;
      select.appendChild(option);
    }
    select.onchange = () => {
      const body = { role: select.value };
      // An administrator signs in on the console, which only takes a
      // password, so one without a password could never sign in.
      if (select.value === 'admin' && person.role !== 'admin' && !person.has_password) {
        const password = prompt(
          `${person.name} has no password yet. Temporary password for them — they'll choose their own on first sign-in.`,
          randomPassword());
        if (!password) { select.value = person.role; return; }
        body.password = password;
      }
      patchPerson(person.id, body);
    };
    return select;
  })()));

  controls.appendChild(labelled('Library folder', (() => {
    const select = el('select', 'select wide');
    const all = el('option', null,
      state.libraryCount > 1 ? 'All library folders' : 'The whole library');
    all.value = '';
    select.appendChild(all);

    for (const folder of state.assignable) {
      const option = el('option', null,
        `${'\u00a0\u00a0'.repeat(folder.depth)}${folder.is_root ? '📁 ' : ''}${folder.label} (${folder.count})`);
      option.value = folder.path;
      if (samePath(folder.path, person.library)) option.selected = true;
      select.appendChild(option);
    }

    // A folder that no longer exists must still show, or saving would
    // silently move them somewhere else.
    if (person.library && !state.assignable.some(
      (f) => samePath(f.path, person.library))) {
      const orphan = el('option', null, `${person.library} (missing)`);
      orphan.value = person.library;
      orphan.selected = true;
      select.appendChild(orphan);
    }

    select.disabled = person.role === 'admin';
    select.title = person.role === 'admin'
      ? 'Admins always see every library folder'
      : 'The only folder this profile can see';
    select.onchange = () => patchPerson(person.id, { library: select.value });
    return select;
  })()));

  if (person.role !== 'admin') {
    controls.appendChild(labelled('Entry', (() => {
      const wrap = el('div', 'row');
      const select = el('select', 'select');
      for (const [value, text] of [['open', 'Tap to enter'], ['pin', 'PIN']]) {
        const option = el('option', null, text);
        option.value = value;
        if ((person.entry === 'pin' ? 'pin' : 'open') === value) option.selected = true;
        select.appendChild(option);
      }
      select.onchange = async () => {
        if (select.value === 'open') {
          await patchPerson(person.id, { pin: '' });
          return;
        }
        const pin = prompt(`Set a PIN for ${person.name} (4–8 digits).`, '');
        if (!pin) { select.value = person.entry === 'pin' ? 'pin' : 'open'; return; }
        await patchPerson(person.id, { pin });
      };
      wrap.appendChild(select);
      return wrap;
    })()));
  }

  const actions = el('div', 'ap-actions');
  const toggle = el('button', 'btn small ghost', person.active ? 'Disable' : 'Enable');
  toggle.type = 'button';
  toggle.disabled = person.id === state.user.id;
  toggle.onclick = () => patchPerson(person.id, { active: !person.active });
  actions.appendChild(toggle);

  const reset = el('button', 'btn small ghost', 'Reset password');
  reset.type = 'button';
  reset.onclick = async () => {
    const password = prompt(
      `Temporary password for ${person.name}. They'll choose their own on first sign-in.`,
      randomPassword());
    if (!password) return;
    try {
      await accountsApi.updatePerson(person.id, { password });
      toast(`New password for ${person.name}: ${password}`);
      loadPeople();
    } catch (exc) { toast(exc.message, true); }
  };
  actions.appendChild(reset);

  if (person.sessions) {
    const signout = el('button', 'btn small ghost', 'Sign out everywhere');
    signout.type = 'button';
    signout.onclick = async () => {
      await accountsApi.signOutPerson(person.id);
      toast(`${person.name} was signed out.`);
      loadPeople();
    };
    actions.appendChild(signout);
  }

  // Deleting is irreversible, so it sits apart from the reversible actions
  // and always asks first.
  const remove = el('button', 'btn small danger', 'Delete');
  remove.type = 'button';
  remove.dataset.action = 'delete-person';
  remove.dataset.person = String(person.id);
  if (person.id === state.user.id) {
    remove.disabled = true;
    remove.title = "You can't delete the profile you're signed in with.";
  } else {
    remove.title = `Remove ${person.name} permanently`;
    remove.onclick = () => confirmDelete(person);
  }
  actions.appendChild(remove);

  controls.appendChild(actions);
  card.appendChild(controls);
  return card;
}

/** Ask before removing a profile, and say exactly what goes with it. */
function confirmDelete(person) {
  const modal = $('#delete-modal');
  const losses = [];
  if (person.favorites) {
    losses.push(`${person.favorites.toLocaleString()} favourite${person.favorites > 1 ? 's' : ''}`);
  }
  if (person.sessions) {
    losses.push(`${person.sessions} signed-in device${person.sessions > 1 ? 's' : ''}`);
  }

  $('#delete-who').textContent = `${person.name} (@${person.username})`;
  $('#delete-loses').textContent = losses.length
    ? `Their ${losses.join(' and ')} will be removed with them.`
    : 'They have no favourites or open sessions.';

  const confirm = $('#delete-confirm');
  confirm.onclick = async () => {
    confirm.disabled = true;
    try {
      const result = await accountsApi.deletePerson(person.id);
      closeDelete();
      const gone = result.removed?.favorites
        ? `${person.name}'s profile and ${result.removed.favorites} favourites were deleted.`
        : `${person.name}'s profile was deleted.`;
      toast(gone);
      await refresh();
    } catch (exc) {
      toast(exc.message, true);
    } finally {
      confirm.disabled = false;
    }
  };

  modal.hidden = false;
  confirm.focus();
}

function closeDelete() {
  $('#delete-modal').hidden = true;
}

/** Path equality that tolerates slashes and Windows case. */
function samePath(a, b) {
  if (!a || !b) return false;
  const norm = (p) => {
    const t = String(p).replace(/\\/g, '/').replace(/\/+$/, '');
    return /^[a-zA-Z]:/.test(t) ? t.toLowerCase() : t;
  };
  return norm(a) === norm(b);
}

function labelled(label, control) {
  const wrap = el('label', 'field');
  wrap.appendChild(el('span', null, label));
  wrap.appendChild(control);
  return wrap;
}

async function patchPerson(id, body) {
  try {
    await accountsApi.updatePerson(id, body);
    toast('Saved.');
  } catch (exc) {
    toast(exc.message, true);
  }
  // Assignment counts and "what they see" both live on the overview payload,
  // so re-read it rather than leaving the Library tab showing stale numbers.
  try {
    state.overview = await adminApi.overview();
    renderOverview();
    renderLibraryFolders();
  } catch { /* the toast above already reported it */ }
  await loadPeople();
  renderPreview(currentPreview);
}

function toggleAddPerson() {
  const block = $('#add-person-block');
  if (!block.hidden) { block.hidden = true; return; }
  block.hidden = false;
  block.innerHTML = '';
  block.appendChild(el('h2', null, 'Add someone'));

  const form = el('form', 'add-form');
  const grid = el('div', 'add-grid');

  const name = input('name', 'Name');
  const username = input('username', 'username');
  username.required = true;
  username.autocapitalize = 'none';

  const role = el('select', 'select');
  role.name = 'role';
  for (const option of state.roles) {
    const node = el('option', null, option.label);
    node.value = option.value;
    if (option.value === 'family') node.selected = true;
    role.appendChild(node);
  }

  const scope = el('select', 'select');
  scope.name = 'library';
  const all = el('option', null, 'All library folders');
  all.value = '';
  scope.appendChild(all);
  for (const folder of state.assignable) {
    const option = el('option', null,
      `${'\u00a0\u00a0'.repeat(folder.depth)}${folder.is_root ? '\u{1F4C1} ' : ''}${folder.label} (${folder.count})`);
    option.value = folder.path;
    scope.appendChild(option);
  }

  const entry = el('select', 'select');
  entry.name = 'entry';
  for (const [value, text] of [
    ['open', 'Tap to enter — no secret'],
    ['pin', 'PIN'],
    ['password', 'Username + password'],
  ]) {
    const option = el('option', null, text);
    option.value = value;
    entry.appendChild(option);
  }

  const secret = input('secret', 'PIN or password');
  secret.hidden = true;
  entry.onchange = () => {
    secret.hidden = entry.value === 'open';
    secret.placeholder = entry.value === 'pin' ? '4–8 digits' : 'at least 8 characters';
    secret.value = entry.value === 'pin' ? '' : randomPassword();
  };

  grid.append(
    labelled('Name', name), labelled('Username', username),
    labelled('Role', role), labelled('Library folder', scope),
    labelled('How they sign in', entry), labelled('PIN / password', secret),
  );
  form.appendChild(grid);

  const error = el('p', 'gate-error');
  error.hidden = true;
  form.appendChild(error);

  const submit = el('button', 'btn primary', 'Create profile');
  submit.type = 'submit';
  form.appendChild(submit);

  form.onsubmit = async (event) => {
    event.preventDefault();
    error.hidden = true;
    const body = {
      name: name.value,
      username: username.value,
      role: role.value,
      library: scope.value,
      password: entry.value === 'password' ? secret.value : '',
      pin: entry.value === 'pin' ? secret.value : '',
    };
    try {
      await accountsApi.createPerson(body);
      const how = entry.value === 'open' ? 'no secret needed'
        : `${entry.value}: ${secret.value}`;
      toast(`${body.name || body.username} added — ${how}`);
      block.hidden = true;
      await loadPeople();
    } catch (exc) {
      error.textContent = exc.message;
      error.hidden = false;
    }
  };
  block.appendChild(form);
}

function input(name, placeholder) {
  const node = el('input', 'input');
  node.name = name;
  node.placeholder = placeholder;
  return node;
}

/* -- Visibility ---------------------------------------------------------- */

async function loadFolders() {
  try {
    const data = await adminApi.folders();
    state.folders = data.folders;
    state.rootRule = data.root_rule;
  } catch (exc) {
    toast(exc.message, true);
    return;
  }
  renderFolderTree();
}

/* The tree opens on its top folders (a dated library's years) with everything
   below folded: every folder at once was thousands of rows to scroll past to
   reach one. A folder opened stays open while the page is, so setting one
   does not fold the tree back up around it. */
const openFolders = new Set();

function parentOf(path) {
  const cut = path.lastIndexOf('/');
  return cut < 0 ? '' : path.slice(0, cut);
}

function shownInTree(path) {
  for (let above = parentOf(path); above; above = parentOf(above)) {
    if (!openFolders.has(above)) return false;
  }
  return true;
}

/* Tree order, a folder's name at a time. Sorted as text, "2019 trip" came
   between "2019" and "2019/07", so an opened 2019 showed its months under the
   wrong folder. */
function byTreeOrder(a, b) {
  const left = a.path.split('/');
  const right = b.path.split('/');
  for (let i = 0; i < Math.min(left.length, right.length); i += 1) {
    const order = left[i].localeCompare(right[i], undefined, { numeric: true });
    if (order) return order;
  }
  return left.length - right.length;
}

function renderFolderTree() {
  const tree = $('#folder-tree');
  tree.innerHTML = '';
  const total = state.overview?.stats?.count || 0;

  tree.appendChild(folderRow({
    path: '', name: 'Whole library', depth: 0, count: total,
    rule: state.rootRule,
  }));
  const parents = new Set(state.folders.map((folder) => parentOf(folder.path)));
  for (const folder of [...state.folders].sort(byTreeOrder)) {
    if (shownInTree(folder.path)) {
      tree.appendChild(folderRow(folder, parents.has(folder.path)));
    }
  }
  if (!state.folders.length) {
    tree.appendChild(el('p', 'hint', 'No folders indexed yet.'));
  }
}

function folderToggle(folder, hasChildren) {
  if (!hasChildren) return el('span', 'folder-toggle');
  const open = openFolders.has(folder.path);
  const toggle = el('button', 'folder-toggle');
  toggle.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 6 6 6-6 6"/></svg>';
  toggle.type = 'button';
  toggle.setAttribute('aria-expanded', String(open));
  toggle.setAttribute('aria-label', `${open ? 'Collapse' : 'Expand'} ${folder.name || folder.path}`);
  toggle.onclick = () => {
    if (open) {
      // Folding a folder folds everything inside it, so it opens as it closed.
      for (const path of [...openFolders]) {
        if (path === folder.path || path.startsWith(`${folder.path}/`)) openFolders.delete(path);
      }
    } else {
      openFolders.add(folder.path);
    }
    renderFolderTree();
    $(`#folder-tree .folder-toggle[data-path="${CSS.escape(folder.path)}"]`)?.focus();
  };
  toggle.dataset.path = folder.path;
  return toggle;
}

function folderRow(folder, hasChildren = false) {
  const row = el('div', 'folder-row');
  // The library root has no buttons. There is no decision anybody makes about
  // *every photograph in the house* that is worth a control, and it was the
  // one place where a slip cost everything — so it is a summary line now, and
  // the server refuses the change even if something asks for it.
  const isRoot = !folder.path;
  if (isRoot) row.classList.add('is-root');

  const name = el('div', 'folder-name');
  name.style.paddingLeft = `${folder.depth * 16}px`;
  if (!isRoot) name.appendChild(folderToggle(folder, hasChildren));
  name.appendChild(el('span', null, folder.name || folder.path || 'Whole library'));
  const count = el('span', 'hint', ` ${folder.count}`);
  name.appendChild(count);
  if (folder.rule) name.appendChild(el('span', `tag ${folder.rule}`, folder.rule));
  row.appendChild(name);

  const mix = el('div', 'folder-mix');
  for (const [key, value] of [['public', folder.public], ['family', folder.family],
    ['hidden', folder.hidden]]) {
    if (!value) continue;
    const chunk = el('i', `mix ${key}`);
    chunk.style.flex = String(value);
    chunk.title = `${value} ${key}`;
    mix.appendChild(chunk);
  }
  row.appendChild(mix);

  if (isRoot) {
    row.appendChild(el('div', 'folder-note',
      'Set on a folder below, or on the photographs themselves.'));
    return row;
  }

  const picker = el('div', 'vis-picker');
  for (const [value, text] of [['public', 'Everyone'], ['family', 'Family'],
    ['hidden', 'Nobody']]) {
    const button = el('button', null, text);
    button.type = 'button';
    button.dataset.vis = value;
    if (folder.rule === value) button.classList.add('active');
    button.onclick = () => applyFolderVisibility(folder.path, value);
    picker.appendChild(button);
  }
  row.appendChild(picker);
  return row;
}

/* -- Locations filled in ------------------------------------------------- */
/* Photographs without a location, taken close in time to one with; given its
   place in the index only (storage/geo.py), and all of it undoable. */

const locations = { data: null, shown: 30 };

/* "India" for IN, from the browser; the code itself if it cannot say. */
function countryLabel(code) {
  if (!code) return '';
  try {
    return new Intl.DisplayNames(['en'], { type: 'region' }).of(code) || code;
  } catch {
    return code;
  }
}

async function loadLocations() {
  const summary = $('#loc-summary');
  if (!summary) return;
  const hours = $('#loc-hours').value;
  summary.textContent = 'Looking…';
  try {
    locations.data = await adminApi.locations(hours);
  } catch (exc) {
    summary.textContent = exc.message;
    return;
  }
  locations.shown = 30;
  renderLocations();
}

function renderLocations() {
  const data = locations.data;
  if (!data) return;
  const span = $('#loc-hours').selectedOptions[0]?.textContent.toLowerCase() || '';
  $('#loc-summary').textContent = data.total
    ? `${data.total.toLocaleString()} photographs taken ${span} of one with a location, `
      + `on ${data.groups.length.toLocaleString()} days and places.`
      + (data.inferred ? ` ${data.inferred.toLocaleString()} already have a filled-in location.` : '')
    : (data.inferred
      ? `Nothing more to fill in. ${data.inferred.toLocaleString()} photographs have a filled-in location.`
      : 'Nothing to fill in: no photograph without a location was taken that close to one with.');
  const all = $('#loc-apply-all');
  all.hidden = !data.total;
  all.textContent = `Give all ${data.total.toLocaleString()} their place`;
  $('#loc-undo').hidden = !data.inferred;

  const list = $('#loc-list');
  list.innerHTML = '';
  for (const group of data.groups.slice(0, locations.shown)) {
    const row = el('div', 'loc-row');
    const text = el('div', 'loc-text');
    const place = [group.city, countryLabel(group.country)].filter(Boolean).join(', ')
      || 'a place with no name';
    text.append(el('strong', null, `${group.date || 'No date'} · ${place}`),
      el('span', 'hint', `${group.count.toLocaleString()} photograph${group.count === 1 ? '' : 's'}`
        + ` · up to ${Math.round(group.max_minutes)} min from one with a location`));
    const thumbs = el('div', 'loc-thumbs');
    for (const id of group.ids.slice(0, 5)) {
      const img = el('img');
      img.loading = 'lazy';
      img.alt = '';
      img.src = thumbUrl(id, 160);
      thumbs.appendChild(img);
    }
    const apply = el('button', 'btn ghost small', 'Give them this place');
    apply.type = 'button';
    apply.onclick = () => applyLocations({ ids: group.ids }, apply);
    row.append(text, thumbs, apply);
    list.appendChild(row);
  }
  const more = $('#loc-more');
  more.hidden = data.groups.length <= locations.shown;
}

async function applyLocations(body, button) {
  if (button) button.disabled = true;
  try {
    const done = await adminApi.applyLocations({ hours: Number($('#loc-hours').value), ...body });
    toast(`${done.applied.toLocaleString()} photographs given their place.`);
  } catch (exc) {
    toast(exc.message, true);
  }
  await loadLocations();
}

function wireLocations() {
  $('#loc-hours')?.addEventListener('change', loadLocations);
  $('#loc-apply-all')?.addEventListener('click', (event) => applyLocations({ all: true }, event.target));
  $('#loc-more')?.addEventListener('click', () => {
    locations.shown += 30;
    renderLocations();
  });
  $('#loc-undo')?.addEventListener('click', async () => {
    const count = locations.data?.inferred || 0;
    if (!window.confirm(`Take back the filled-in location of ${count.toLocaleString()} photographs?`)) return;
    try {
      const done = await adminApi.undoLocations();
      toast(`${done.undone.toLocaleString()} filled-in locations taken back.`);
    } catch (exc) {
      toast(exc.message, true);
    }
    await loadLocations();
  });
}

/* -- Library ------------------------------------------------------------- */

async function runScan(full) {
  try {
    await adminApi.rescan(full);
    toast(full ? 'Full re-index started.' : 'Scanning for changes…');
  } catch (exc) { toast(exc.message, true); }
}

let browsePath = '';
let browseSelectable = false;

/**
 * The one folder picker in the console.
 *
 * The Library tab uses it to choose where the index lives; the Archive tab
 * uses it to choose sources and a destination. Those have different rules —
 * `C:\\` is a perfectly good thing to *sweep for photos* but a terrible
 * library root — so `pick` takes over what happens on Use, and `anyFolder`
 * relaxes the library's selectability check for callers that don't need it.
 */
let picker = null;

function openFolderPicker(options = {}) {
  picker = {
    start: options.start ?? (state.overview?.library?.root || ''),
    title: options.title || 'Choose a library folder',
    cta: options.cta || 'Use this folder',
    anyFolder: !!options.anyFolder,
    pick: options.pick || null,
  };
  $('#folder-modal').hidden = false;
  $('#fm-manual').value = '';
  $('#folder-modal h2').textContent = picker.title;
  $('#fm-apply').textContent = picker.cta;
  return loadBrowse(picker.start);
}

/* The typed path and the browsed folder are two ways of answering the same
   question, so both have to feed the same decision. Without this the text box
   is inert: typing a perfectly good path leaves "Use this folder" greyed out
   whenever the tree happens to be sitting on a folder that cannot be picked. */
function refreshApply() {
  const typed = $('#fm-manual').value.trim();
  const apply = $('#fm-apply');
  apply.disabled = !typed && !browseSelectable;
  apply.title = apply.disabled ? 'Pick a folder inside this one, or type a path' : '';
}

async function loadBrowse(path) {
  const list = $('#fm-dirs');
  list.innerHTML = '<p class="hint" style="padding:12px">Loading…</p>';
  let data;
  try {
    data = await adminApi.browse(path);
  } catch (exc) {
    list.innerHTML = '';
    list.appendChild(el('p', 'gate-error', exc.message));
    return;
  }
  browsePath = data.path;
  browseSelectable = picker?.anyFolder ? true : data.selectable !== false;
  // Choosing from the tree is an answer, so it replaces whatever was typed
  // earlier — otherwise `typed || browsePath` submits the old string and the
  // folder the admin just clicked is silently ignored.
  $('#fm-manual').value = '';
  $('#fm-path').textContent = data.path;

  refreshApply();

  list.innerHTML = '';

  const folderIcon = '<svg viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/></svg>';
  const homeIcon = '<svg viewBox="0 0 24 24"><path d="M4 11 12 4l8 7v8a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1Z"/></svg>';

  // Shortcuts first: home, Pictures, and every drive on the machine, so
  // C:\Master is two clicks away instead of a climb from wherever we started.
  if (data.shortcuts?.length) {
    const bar = el('div', 'fm-shortcuts');
    for (const shortcut of data.shortcuts) {
      const chip = el('button', 'chip', shortcut.name || shortcut.path);
      chip.type = 'button';
      chip.title = shortcut.path;
      chip.onclick = () => loadBrowse(shortcut.path);
      bar.appendChild(chip);
    }
    list.appendChild(bar);
  }

  if (data.parent) {
    const up = el('button');
    up.type = 'button';
    up.innerHTML = homeIcon;
    up.appendChild(el('span', null, '.. (up one level)'));
    up.onclick = () => loadBrowse(data.parent);
    list.appendChild(up);
  }
  for (const dir of data.dirs) {
    const button = el('button');
    button.type = 'button';
    button.innerHTML = folderIcon;
    button.appendChild(el('span', null, dir.name));
    button.onclick = () => loadBrowse(dir.path);
    list.appendChild(button);
  }
  if (!data.dirs.length) {
    list.appendChild(el('p', 'hint',
      browseSelectable
        ? 'No subfolders here — use this folder, or go back up.'
        : 'No subfolders here.'));
  }
}

async function applyRoot() {
  const typed = $('#fm-manual').value.trim();
  const path = typed || browsePath;
  if (!path) return;

  // A caller that supplied its own handler just wants the path back.
  if (picker?.pick) {
    const handler = picker.pick;
    $('#folder-modal').hidden = true;
    handler(path);
    return;
  }

  const button = $('#fm-apply');
  const label = button.textContent;
  button.disabled = true;
  button.textContent = 'Opening…';
  try {
    await adminApi.setRoot(path);
    $('#folder-modal').hidden = true;
    toast('Indexing the library…');
    setTimeout(refresh, 1500);
  } catch (exc) {
    toast(exc.message, true);
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

/* -- Activity ------------------------------------------------------------ */

async function loadActivity() {
  const box = $('#activity');
  box.innerHTML = '<p class="hint">Loading…</p>';
  let data;
  try {
    data = await adminApi.audit();
  } catch (exc) { box.innerHTML = ''; box.appendChild(el('p', 'hint', exc.message)); return; }

  box.innerHTML = '';
  if (!data.entries.length) {
    box.appendChild(el('p', 'hint', 'Nothing yet.'));
    return;
  }
  const labels = {
    login: 'signed in', login_failed: 'failed sign-in', enter_profile: 'entered',
    create_person: 'added a profile', disable_person: 'disabled a profile',
    delete_person: 'deleted a profile',
    archive_copy: 'started a consolidation', archive_dry_run: 'started a dry run',
    archive_verify: 'started an archive audit', archive_stop: 'stopped an archive run',
    archive_adopt: 'added an archive to the library',
    archive_reset: 'cleared the archive report',
    enable_person: 'enabled a profile', reset_password: 'reset a password',
    password_changed: 'changed their password', set_visibility: 'changed visibility',
    set_folder_visibility: 'changed folder visibility', settings: 'changed settings',
    bootstrap_admin: 'created the first admin', signout_person: 'signed someone out',
    set_pin: 'set a PIN', clear_pin: 'removed a PIN',
  };
  for (const entry of data.entries) {
    const row = el('div', 'activity-row');
    row.appendChild(el('span', 'activity-when',
      new Date(entry.at * 1000).toLocaleString()));
    row.appendChild(el('span', 'activity-who', entry.display_name || 'someone'));
    row.appendChild(el('span', 'activity-what', labels[entry.action] || entry.action));
    if (entry.detail) row.appendChild(el('span', 'activity-detail', entry.detail));
    box.appendChild(row);
  }
}

/* -- Progress + toasts --------------------------------------------------- */

let lastPhase = null;

// The indexing bar on the Settings page, and the gallery refresh when a
// scan finishes. The strip in the heading is `onActivity` below, which
// shows this job alongside every other one that is running.
function onProgress(scan) {
  if (!scan) return;
  const strip = $('#scan-strip');
  if (strip) strip.hidden = !scan.running;
  if (scan.running) {
    const counter = scan.tag_total ? SCAN_COUNTS[scan.status] : null;
    const percent = counter
      ? Math.round((scan.tagged / scan.tag_total) * 100) : scan.percent;
    const counted = counter
      ? counter(scan.tagged.toLocaleString(), scan.tag_total.toLocaleString())
      : scan.message || `Indexing ${scan.processed.toLocaleString()} / ${scan.total.toLocaleString()}`;
    const left = timeLeft(scan.eta);
    const line = left ? `${counted} · ${left}` : counted;
    const text = scan.folder ? `${line} · ${scan.folder}` : line;
    const fill = $('#scan-fill');
    if (fill) fill.style.width = `${percent}%`;
    const strapline = $('#scan-text');
    if (strapline) strapline.textContent = text;
  }
  const phase = `${scan.status}:${scan.added}:${scan.removed}`;
  if (phase !== lastPhase && (scan.status === 'done' || scan.status === 'error')) {
    lastPhase = phase;
    refresh();
  }
}

// Everything running right now, in the heading, on whichever page is open.
//
// This was the indexing bar alone. A consolidation holding a USB disk, a
// cloud upload, a straightening pass, a storage check hashing every file, a
// model still downloading: each had exactly one page that knew about it, so a
// machine with its fan up and its disk light on had no answer anywhere except
// in whichever of those pages somebody thought to open.
//
// Each row goes through to the page that owns that job, because the question
// after "what is this?" is usually "how do I stop it?".
function onActivity(activity) {
  const strip = $('#scan-live');
  if (strip) strip.hidden = !activity?.running;
  renderActivity($('#job-rows'), activity?.jobs || [],
                 (page) => showTab(page));
}

/* --- the folder screen -------------------------------------------------- */

const foldState = { path: '', hiddenOnly: false, show: 'all', data: null };

// The kinds the Folders screen narrows to, in the server's words. The counts
// on each are for the open folder and everything beneath it.
const FOLD_FILTERS = [
  ['all', 'All', 'files', 'This folder has nothing indexed in it.'],
  ['picture', 'Photos', 'photos', 'No photos in here.'],
  ['video', 'Videos', 'videos', 'No videos in here.'],
  ['audio', 'Audio', 'audio files', 'No audio in here.'],
  ['screen', 'Screenshots & documents', 'screenshots and documents',
    'No screenshots or documents in here.'],
];

const FOLDER_ICON =
  '<svg viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/></svg>';

function bytesShort(n) {
  if (!n) return '';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let value = n;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
  return `${value >= 10 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
}

async function loadFolderScreen(path = '') {
  foldState.path = path || '';
  let data;
  try {
    data = await adminApi.folder(foldState.path, foldState.show);
  } catch (exc) {
    toast(exc.message, true);
    return;
  }
  foldState.data = data;
  renderFolderScreen();
}

function renderFolderScreen() {
  const data = foldState.data;
  if (!data) return;

  /* breadcrumb */
  const trail = $('#fold-trail');
  trail.innerHTML = '';
  data.trail.forEach((step, index) => {
    if (index) trail.append(el('span', 'sep', '/'));
    const button = el('button', index === data.trail.length - 1 ? 'here' : '', step.name);
    button.type = 'button';
    button.onclick = () => loadFolderScreen(step.path);
    trail.appendChild(button);
  });

  renderFolderFilters(data);
  const filter = FOLD_FILTERS.find(([key]) => key === (data.show || 'all')) || FOLD_FILTERS[0];

  /* what is in here, with the hidden count called out */
  const counts = data.counts || {};
  const total = (data.children || []).reduce((sum, c) => sum + c.n, 0) + (counts.n || 0);
  const hidden = data.total_hidden || 0;
  const summary = $('#fold-summary');
  summary.innerHTML = '';
  summary.append(
    span(`${(data.children || []).length.toLocaleString()} folders`),
    span(`${total.toLocaleString()} ${filter[2]}`),
  );
  if (hidden) {
    const mark = el('span', 'hidden-n');
    mark.append(el('span', 'n', hidden.toLocaleString()),
                document.createTextNode(' only admins can see'));
    summary.appendChild(mark);
  } else {
    summary.append(span('nothing hidden in here'));
  }
  if (data.rule) {
    summary.append(span(`folder rule: ${VIS_NAMES_BY_VALUE[data.rule.visibility]}`
      + (data.rule.at === data.folder ? '' : ` (from “${data.rule.at || 'the library'}”)`)));
  }

  /* subfolders */
  const grid = $('#fold-children');
  grid.innerHTML = '';
  const children = (data.children || [])
    .filter((c) => !foldState.hiddenOnly || c.hidden > 0);
  for (const child of children) {
    grid.appendChild(folderCard(child));
  }

  /* the files sitting directly in this folder */
  const items = (data.items || [])
    .filter((i) => !foldState.hiddenOnly || i.visibility_name === 'hidden');
  const head = $('#fold-items-head');
  head.hidden = items.length === 0;
  if (items.length) {
    const hiddenHere = items.filter((i) => i.visibility_name === 'hidden').length;
    $('#fold-items-title').textContent =
      `${items.length.toLocaleString()} file${items.length === 1 ? '' : 's'} here`;
    $('#fold-items-note').textContent = hiddenHere
      ? `${hiddenHere.toLocaleString()} of them are admin-only`
      : '';
  }
  const list = $('#fold-items');
  list.innerHTML = '';
  for (const item of items) list.appendChild(folderItem(item));

  const empty = $('#fold-empty');
  if (!children.length && !items.length) {
    empty.textContent = foldState.hiddenOnly
      ? 'Nothing in here is hidden.'
      : filter[3];
    empty.hidden = false;
  } else {
    empty.hidden = true;
  }
}

function renderFolderFilters(data) {
  const box = $('#fold-filters');
  if (!box) return;
  box.innerHTML = '';
  const kinds = data.kinds || {};
  const current = data.show || 'all';
  for (const [key, label] of FOLD_FILTERS) {
    const chip = el('button', `chip${key === current ? ' active' : ''}`);
    chip.type = 'button';
    chip.setAttribute('aria-pressed', String(key === current));
    chip.append(document.createTextNode(label),
      el('span', 'n', (kinds[key] || 0).toLocaleString()));
    chip.onclick = () => {
      if (foldState.show === key) return;
      foldState.show = key;
      loadFolderScreen(foldState.path);
    };
    box.appendChild(chip);
  }
}

function span(text) { return el('span', '', text); }

function folderCard(child) {
  const card = el('button', 'fold-card');
  card.type = 'button';
  // Entirely private is worth seeing before opening it.
  if (child.n && child.hidden === child.n) card.classList.add('all-hidden');
  card.onclick = () => loadFolderScreen(child.path);

  const cover = el('div', 'fold-cover');
  if (child.cover) {
    const img = document.createElement('img');
    img.src = thumbUrl(child.cover, 256, child.cover_v);
    img.alt = '';
    img.loading = 'lazy';
    cover.appendChild(img);
  } else {
    cover.innerHTML = FOLDER_ICON;
  }
  card.appendChild(cover);

  const body = el('div', 'fold-body');
  body.appendChild(el('div', 'fold-name', child.name));

  const bar = el('div', 'fold-bar');
  for (const [key, cls] of [['public', 'b-public'], ['family', 'b-family'],
                            ['hidden', 'b-hidden']]) {
    if (!child[key]) continue;
    const piece = el('i', cls);
    piece.style.width = `${(child[key] / child.n) * 100}%`;
    bar.appendChild(piece);
  }
  body.appendChild(bar);

  const meta = el('div', 'fold-meta');
  meta.appendChild(span(`${child.n.toLocaleString()} files`));
  if (child.bytes) meta.appendChild(span(bytesShort(child.bytes)));
  if (child.hidden) meta.appendChild(el('span', 'hid', `${child.hidden.toLocaleString()} hidden`));
  body.appendChild(meta);

  card.appendChild(body);
  card.title = child.hidden
    ? `${child.path} — ${child.hidden} of ${child.n} visible to admins only`
    : child.path;
  return card;
}

const VIS_WHY = {
  hidden: 'from disk',
  folder: 'folder rule',
  item: 'set here',
  screen: 'screenshot or document',
  default: '',
};

/** Ask the server to open its own file manager with `item` selected.
 *
 * Only ever works when the admin console and Ninaivu's server are the same
 * machine — the server says so plainly (409) when they are not, rather than
 * pretending the click did something. */
async function revealItem(item) {
  try {
    await adminApi.reveal(item.id);
  } catch (exc) {
    toast(exc.message, true);
  }
}

let uploadPoll;
let uploadSignature = '';
let uploadActionBusy = false;
const seenUploads = new Set();

/* -- Phone backups (ninaivu/media/phone_backup.py) --------------------------- */

async function loadPhoneBackups() {
  const box = $('#pb-people');
  if (!box) return;
  let data;
  try {
    data = await json('/api/admin/phone-backups');
  } catch (err) {
    // A server from before phone backup has no such endpoint.
    if (err.status === 404) { $('#pb-block').hidden = true; return; }
    toast(err.message, true);
    return;
  }
  $('#pb-trusted').checked = Boolean(data.trusted);
  box.replaceChildren();
  if (!data.people.length) {
    box.appendChild(el('p', 'hint subtle', 'Nobody has backed up a phone yet.'));
    return;
  }
  for (const person of data.people) {
    const row = el('div', 'pb-person');
    const when = person.last_backup
      ? `last backup ${new Date(person.last_backup * 1000).toLocaleString()}` : 'nothing finished yet';
    row.appendChild(el('span', 'pb-who', person.name));
    row.appendChild(el('span', 'hint', `${Number(person.safe || 0).toLocaleString()} safe · `
      + `${person.phones} phone${person.phones === 1 ? '' : 's'} · ${when}`));
    if (person.waiting) {
      const approve = el('button', 'btn small', `Approve all ${person.waiting.toLocaleString()}`);
      approve.type = 'button';
      approve.onclick = async () => {
        approve.disabled = true;
        try {
          const result = await json(`/api/admin/phone-backups/${person.user_id}/approve`,
            { method: 'POST' });
          toast(`Filed ${result.approved.toLocaleString()} of ${person.name}'s backups`
            + `${result.failed ? `; ${result.failed} could not be filed` : ''}.`, result.failed > 0);
          loadPendingUploads();
          loadPhoneBackups();
        } catch (err) {
          toast(err.message, true);
          approve.disabled = false;
        }
      };
      row.appendChild(approve);
    }
    box.appendChild(row);
  }
}

async function savePhoneBackupTrust(trusted) {
  try {
    await json('/api/admin/phone-backups/settings', { method: 'POST', body: { trusted } });
    toast(trusted ? 'Family phone backups are filed without asking'
      : 'Family phone backups wait for approval');
  } catch (err) {
    toast(err.message, true);
  }
  loadPhoneBackups();
}

async function loadPendingUploads() {
  if (uploadActionBusy) return;
  try {
    const data = await json('/api/admin/uploads');
    const count = $('#uploads-count');
    if (count) {
      count.textContent = data.total ? String(data.total) : '';
      count.hidden = !data.total;
    }
    // The group's mark is set by loadAttention, which counts every queue.
    const incoming = data.items.filter((item) => !seenUploads.has(item.id));
    for (const item of data.items) seenUploads.add(item.id);
    if (incoming.length) toast(`${incoming.length} family upload${incoming.length === 1 ? '' : 's'} awaiting approval. Open Uploads to review.`);
    const signature = JSON.stringify(data.items);
    if (signature === uploadSignature) return;
    uploadSignature = signature;
    const list = $('#pending-uploads');
    const drafts = new Map([...list.querySelectorAll('input[data-upload]')].map(input => [input.dataset.upload, input.value]));
    list.replaceChildren();
    if (!data.items.length) list.appendChild(el('p', 'hint', 'No uploads awaiting approval.'));
    for (const item of data.items) {
      const card = el('div', 'pending-upload');
      if (item.has_thumb) {
        const img = el('img');
        img.src = `/api/admin/uploads/${item.id}/preview?thumb=1`;
        img.alt = item.filename;
        img.loading = 'lazy';
        card.appendChild(img);
      }
      // An edit saved from the Playground is not a new photograph arriving, and
      // reviewing it is a different judgement: say so before the filename, which
      // is generated and tells an administrator nothing on its own.
      if (item.edited_from) {
        const badge = el('span', 'pending-kind', 'Edited copy');
        card.appendChild(badge);
        card.appendChild(el('strong', null, `Edit of ${item.edited_from}`));
        card.appendChild(el('p', 'hint', `Edited by ${item.uploader} · ${bytesShort(item.size)} · saved as ${item.filename}`));
      } else {
        card.appendChild(el('strong', null, item.filename));
        card.appendChild(el('p', 'hint', `Uploaded by ${item.uploader} · ${bytesShort(item.size)}`));
      }
      const preview = el('a', 'btn small ghost', 'Preview file');
      preview.href = `/api/admin/uploads/${item.id}/preview`;
      preview.target = '_blank';
      preview.rel = 'noopener';
      card.appendChild(preview);
      const input = el('input', 'input');
      input.type = 'date';
      // Only a by-date filing needs one. A derivative goes beside its source
      // whatever the date says, so demanding one would be asking for a decision
      // that changes nothing.
      input.required = item.files_by_date;
      input.dataset.upload = String(item.id);
      input.value = drafts.get(String(item.id)) || item.creation_date || '';
      card.appendChild(labelled(item.files_by_date ? 'Creation date' : 'Creation date (optional)', input));
      card.appendChild(el('p', 'hint', item.files_by_date
        ? `Date source: ${item.date_source}. Destination: ${item.destination}.`
        : `Keeps the original's date and who may see it. Destination: ${item.destination}, beside its source.`));
      const approve = el('button', 'btn', 'Approve and file');
      approve.type = 'button';
      approve.onclick = async () => {
        if (!input.reportValidity() || uploadActionBusy) return;
        uploadActionBusy = true;
        approve.disabled = true;
        try {
          // An empty date is not a date: send the key only when there is one,
          // or the server rejects the blank a derivative is allowed to leave.
          const result = await json(`/api/admin/uploads/${item.id}/approve`, {
            method: 'POST', body: input.value ? { creation_date: input.value } : {},
          });
          toast(`Approved ${item.edited_from ? `the edit of ${item.edited_from}` : item.filename}. Filed in ${result.item.folder}.`);
        } catch (exc) { toast(exc.message, true); }
        finally {
          uploadActionBusy = false;
          approve.disabled = false;
          await loadPendingUploads();
        }
      };
      // Not every upload belongs in the library. Refusing one deletes the
      // uploaded file; the queue keeps no copy, and nothing is filed.
      const reject = el('button', 'btn ghost danger', 'Delete');
      reject.type = 'button';
      reject.onclick = async () => {
        if (uploadActionBusy) return;
        const what = item.edited_from ? `the edit of ${item.edited_from}` : item.filename;
        if (!window.confirm(`Delete ${what}?\n\nIt will not be added to the library, and the `
          + `uploaded file is removed from this computer. This cannot be undone.`)) return;
        uploadActionBusy = true;
        reject.disabled = true;
        try {
          await json(`/api/admin/uploads/${item.id}/reject`, { method: 'POST', body: {} });
          toast(`Deleted ${what}. It was not added to the library.`);
        } catch (exc) { toast(exc.message, true); }
        finally {
          uploadActionBusy = false;
          reject.disabled = false;
          await loadPendingUploads();
        }
      };
      const actions = el('div', 'pending-upload-actions');
      actions.append(approve, reject);
      card.appendChild(actions);
      list.appendChild(card);
    }
  } catch (exc) {
    if (exc.status !== 401) console.debug('Upload review refresh unavailable', exc.message);
  }
}

async function renderDatePolicy(container) {
  const block = el('div', 'setting-field');
  container.appendChild(block);
  block.appendChild(el('h3', null, 'Family app date visibility'));
  block.appendChild(el('p', 'hint', 'Calendar albums use file creation dates. A named album uses its earliest dated file (excluding the bin); changing a file date can change its album date. Its contents must also match the date range. Unknown dates are hidden unless All dates is selected. Admin management always shows every date.'));
  try {
    const policy = await json('/api/admin/date-policy');
    // Four short controls side by side, the way the People page lays out a
    // profile's: stacked at full width they read as four separate settings.
    const grid = el('div', 'date-policy-grid');
    block.appendChild(grid);
    const cutoff = el('input', 'input');
    cutoff.type = 'date';
    cutoff.required = true;
    cutoff.value = policy.cutoff;
    grid.appendChild(labelled('Cutoff date', cutoff));
    const inputs = {};
    for (const [role, title] of [['admin', 'Admins in the family app'], ['family', 'Family members'], ['guest', 'Guests']]) {
      const select = el('select', 'select');
      for (const [value, label] of [['before', 'Before cutoff'], ['after', 'On or after cutoff'], ['all', 'All dates']]) {
        const option = el('option', null, label);
        option.value = value;
        option.selected = policy[role] === value;
        select.appendChild(option);
      }
      inputs[role] = select;
      grid.appendChild(labelled(title, select));
    }
    const save = el('button', 'btn', 'Save date visibility');
    save.type = 'button';
    save.onclick = async () => {
      if (!cutoff.reportValidity()) return;
      save.disabled = true;
      try {
        await json('/api/admin/date-policy', { method: 'POST', body: {
          cutoff: cutoff.value, ...Object.fromEntries(Object.entries(inputs).map(([role, input]) => [role, input.value])),
        } });
        toast('Date visibility saved. Refresh the family app to see updated albums.');
      } catch (exc) { toast(exc.message, true); }
      finally { save.disabled = false; }
    };
    block.appendChild(save);
  } catch (exc) { block.appendChild(el('p', 'hint', exc.message)); }
}

function creationDateControl(item) {
  const row = el('div', 'setting-field');
  row.onclick = (event) => event.stopPropagation();
  row.onkeydown = (event) => event.stopPropagation();
  const input = el('input', 'input');
  input.type = 'date';
  input.value = item.date_key || '';
  input.required = true;
  row.appendChild(labelled('Creation date', input));
  const save = el('button', 'btn small', 'Save date and move');
  save.type = 'button';
  save.onclick = async (event) => {
    event.stopPropagation();
    if (!input.reportValidity()) return;
    save.disabled = true;
    try {
      const result = await json(`/api/admin/assets/${item.id}/creation-date`, {
        method: 'PATCH', body: { creation_date: input.value },
      });
      toast(`Saved. File is now in ${result.item.folder}.`);
      await loadFolderScreen(foldState.path);
    } catch (exc) { toast(exc.message, true); }
    finally { save.disabled = false; }
  };
  row.appendChild(save);
  row.appendChild(el('p', 'hint', 'Moves to year/month/day. Saved at midnight UTC; original embedded metadata is preserved.'));
  return row;
}

function folderItem(item) {
  const tile = el('div', 'fold-item');
  tile.setAttribute('role', 'button');
  tile.tabIndex = 0;
  const isHidden = item.visibility_name === 'hidden';
  if (isHidden) tile.classList.add('is-hidden');

  if (item.thumb) {
    const img = document.createElement('img');
    img.src = thumbUrl(item.id, 256, item.thumb_v);
    img.alt = item.filename;
    img.loading = 'lazy';
    tile.appendChild(img);
  }
  tile.appendChild(el('div', 'name', item.filename));
  tile.appendChild(creationDateControl(item));
  if (isHidden) {
    tile.appendChild(el('div', 'badge', 'Hidden'));
    const why = VIS_WHY[item.vis_source];
    if (why) tile.appendChild(el('div', 'why', why));
  }
  tile.title = `${item.filename} — ${item.visibility_name} — click to show this file where it lives`;
  tile.onclick = () => revealItem(item);
  tile.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); revealItem(item); }
  });
  return tile;
}

/* --- Large files ---------------------------------------------------------
 *
 * A worklist, not a filter: everything here still shows up everywhere else
 * in the library exactly as before. "Keep" only ever means "stop asking" —
 * see db.mark_large_files_reviewed — and "Delete" is the ordinary
 * password-gated /api/delete, the same one the gallery itself uses, so a
 * deleted file lands in the same recoverable _deleted folder either way. */

function lfDuration(seconds) {
  if (!seconds) return '';
  const s = Math.round(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = String(Math.floor(s % 60)).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${sec}` : `${m}:${sec}`;
}

async function loadLargeFiles() {
  const list = $('#lf-list');
  const summary = $('#lf-summary');
  const empty = $('#lf-empty');
  const minMb = $('#lf-floor')?.value || 500;
  summary.textContent = 'Looking…';
  let data;
  try {
    data = await adminApi.largeFiles(minMb);
  } catch (exc) {
    toast(exc.message, true);
    summary.textContent = '';
    return;
  }
  list.innerHTML = '';
  if (!data.items.length) {
    summary.textContent = '';
    empty.textContent = `Nothing over ${data.floor_mb} MB is waiting to be looked at.`;
    empty.hidden = false;
    return;
  }
  empty.hidden = true;
  summary.textContent =
    `${plural(data.items.length, 'file', 'files')} over ${data.floor_mb} MB, largest first.`;
  for (const item of data.items) list.appendChild(lfRow(item));
}

function lfRow(item) {
  const row = el('div', 'lf-row');

  if (item.thumb) {
    const img = document.createElement('img');
    img.className = 'lf-thumb';
    img.src = thumbUrl(item.id, 160, item.thumb_v);
    img.alt = '';
    img.loading = 'lazy';
    img.title = 'Show this file where it lives';
    img.onclick = () => revealItem(item);
    row.appendChild(img);
  } else {
    const placeholder = el('div', 'lf-thumb lf-thumb-empty', item.kind === 'video' ? '▶' : '');
    row.appendChild(placeholder);
  }

  const info = el('div', 'lf-info');
  info.appendChild(el('div', 'lf-name', item.filename));
  const meta = el('div', 'lf-meta');
  meta.appendChild(span(bytesShort(item.size)));
  if (item.duration) meta.appendChild(span(lfDuration(item.duration)));
  meta.appendChild(span(item.folder || '(library root)'));
  if (item.no_camera) meta.appendChild(el('span', 'tag warn', 'no camera info'));
  info.appendChild(meta);
  row.appendChild(info);

  const actions = el('div', 'lf-actions');
  const keep = el('button', 'btn ghost small', 'Keep');
  keep.title = 'Stop asking about this file';
  keep.onclick = async () => {
    keep.disabled = true;
    try {
      await adminApi.keepLargeFiles([item.id]);
      row.remove();
      toast(`Won't ask about "${item.filename}" again.`);
    } catch (exc) {
      toast(exc.message, true);
      keep.disabled = false;
    }
  };
  const del = el('button', 'btn danger small', 'Delete');
  del.onclick = () => lfDelete(item, row);
  actions.append(keep, del);
  row.appendChild(actions);

  return row;
}

function lfDelete(item, row) {
  const modal = $('#lf-delete-modal');
  const input = $('#lf-delete-password');
  const error = $('#lf-delete-error');
  const confirm = $('#lf-delete-confirm');

  $('#lf-delete-what').textContent = `"${item.filename}" (${bytesShort(item.size)})`;
  error.hidden = true;
  input.value = '';
  modal.hidden = false;
  input.focus();

  const close = () => {
    modal.hidden = true;
    input.onkeydown = null;
    confirm.onclick = null;
    $('#lf-delete-cancel').onclick = null;
  };

  const attempt = async () => {
    if (!input.value) { input.focus(); return; }
    confirm.disabled = true;
    try {
      await adminApi.deleteAsset(item.id, input.value);
      close();
      row.remove();
      toast(`"${item.filename}" moved to the bin.`);
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
    } finally {
      confirm.disabled = false;
    }
  };

  confirm.onclick = attempt;
  $('#lf-delete-cancel').onclick = close;
  input.onkeydown = (event) => {
    if (event.key === 'Enter') { event.preventDefault(); attempt(); }
    if (event.key === 'Escape') close();
  };
}

/* --- anything that went wrong ------------------------------------------- */
/*
 * The log file has everything and nobody will open it. This is the short
 * version: what has gone wrong since the machine was last started, in the one
 * place an administrator already looks when something feels off.
 */

async function loadProblems() {
  let data;
  try {
    data = await adminApi.problems(12);
  } catch (exc) {
    return;                       // the panel is a convenience, never a blocker
  }

  const list = $('#problems');
  const rows = data.problems || [];
  list.innerHTML = '';

  const pill = $('#problem-count');
  pill.hidden = rows.length === 0;
  pill.textContent = String(rows.length);
  $('#problems-empty').hidden = rows.length > 0;
  $('#problem-file').textContent = data.file || 'the log file';

  for (const row of rows) {
    const line = el('div', `problem ${row.level === 'error' || row.level === 'critical' ? 'error' : ''}`);
    line.appendChild(el('span', 'where', row.where || ''));
    line.appendChild(el('span', 'what', row.what || ''));
    line.appendChild(el('span', 'when', whenShort(row.at)));
    if (row.detail) line.appendChild(el('pre', 'detail', row.detail));
    list.appendChild(line);
  }
}

/* --- copies of the index ------------------------------------------------- */

async function loadBackups() {
  let data;
  try {
    data = await adminApi.backups();
  } catch (exc) {
    return;
  }

  const last = data.last;
  $('#backup-when').textContent = last ? whenShort(last.at) : 'never';
  $('#backup-size').textContent = last ? bytesShort(last.bytes) : 'no copy yet';
  $('#backup-count').textContent = String(data.count || 0);
  $('#backup-keep').textContent = data.keep ? `newest ${data.keep} kept` : '';
  $('#backup-every').textContent = data.every_hours
    ? (data.every_hours === 24 ? 'day' : `${data.every_hours} h`)
    : 'off';
  const sharedWith = data.same_drive_as || [];
  const driveWarning = $('#backup-drive-warning');
  if (driveWarning) {
    driveWarning.hidden = !sharedWith.length;
    driveWarning.textContent = sharedWith.length
      ? `These copies are on the same drive as ${sharedWith.join(' and ')}. They protect against `
        + 'mistakes, not against that drive failing. Set NINAIVU_BACKUP_DIR, or "backup_dir" in '
        + 'config.json in the state folder, to a folder on another drive.'
      : '';
  }
  $('#backup-where').textContent = data.error
    ? `The last attempt failed: ${data.error}`
    : `Copies are written to ${data.folder}. To put one back, stop Ninaivu and run `
      + 'tools/backup_restore.py restore <file>.';
  $('#backup-now').disabled = Boolean(data.running);
  // Every new backup is test-restored as soon as it is written; this is the
  // line that says whether the last one would actually come back.
  const check = $('#backup-check');
  if (check) {
    const verified = data.verified;
    check.hidden = !verified;
    check.classList.toggle('bad', Boolean(verified && !verified.ok));
    if (verified) {
      check.textContent = verified.ok
        ? `Restore check passed ${whenShort(verified.at)}: the newest backup unpacks, matches its checksums `
          + `and opens as a library of ${Number(verified.assets || 0).toLocaleString()} items.`
        : `Restore check FAILED ${whenShort(verified.at)}: ${verified.error}. Make a new backup and check again; `
          + 'if it fails again, the index itself may be damaged.';
    }
  }
}

/* --- the recycle bin ---------------------------------------------------- */
/*
 * Deleting already moves the file rather than erasing it, but a bin nobody
 * can look into is only half a promise. This is the other half: what went,
 * when, who by, and one button to put it back.
 */

const binState = { items: [], chosen: new Set(), folders: [] };

function whenShort(seconds) {
  if (!seconds) return '';
  const then = new Date(seconds * 1000);
  const days = Math.floor((Date.now() - then.getTime()) / 86400000);
  if (days <= 0) return `today, ${then.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
  if (days === 1) return 'yesterday';
  if (days < 7) return `${days} days ago`;
  return then.toLocaleDateString();
}

async function loadRecycleBin() {
  let data;
  try {
    data = await adminApi.recycleBin();
  } catch (exc) {
    // A library that has never had a deletion has no bin, and that is not an
    // error worth a red toast on every visit to the tab.
    binState.items = [];
    renderRecycleBin();
    if (exc.status && exc.status !== 404) toast(exc.message, true);
    return;
  }
  binState.items = data.items || [];
  binState.folders = data.folders || [];
  binState.summary = data.summary || {};
  binState.eraseAfter = data.erase_after_days || 0;
  // Drop anything that has gone since the last look, so "Put back" can never
  // be enabled for a row that is no longer there.
  const live = new Set(binState.items.map((row) => row.id));
  binState.chosen = new Set([...binState.chosen].filter((id) => live.has(id)));
  renderRecycleBin();
}

function renderRecycleBin() {
  const list = $('#bin-list');
  const rows = binState.items;
  list.innerHTML = '';

  const pill = $('#bin-count');
  pill.hidden = rows.length === 0;
  pill.textContent = String(rows.length);

  $('#bin-empty').hidden = rows.length > 0;
  $('#bin-policy').textContent = binState.eraseAfter
    ? ` Anything left here longer than ${binState.eraseAfter} days is erased`
      + ' automatically when Ninaivu starts.'
    : '';
  $('#bin-foot').hidden = rows.length === 0;
  $('#bin-note').hidden = rows.length === 0;

  for (const row of rows) {
    const line = el('label', 'bin-row');
    if (!row.present) line.classList.add('gone');

    const box = document.createElement('input');
    box.type = 'checkbox';
    box.checked = binState.chosen.has(row.id);
    box.disabled = !row.present;
    box.onchange = () => {
      if (box.checked) binState.chosen.add(row.id);
      else binState.chosen.delete(row.id);
      renderBinFoot();
    };
    line.appendChild(box);

    const preview = document.createElement('img');
    preview.className = 'bin-thumb';
    preview.alt = '';
    preview.loading = 'lazy';
    if (row.thumbnail) {
      preview.src = row.thumbnail;
      preview.onerror = () => preview.classList.add('missing');
    } else {
      preview.classList.add('missing');
    }
    line.appendChild(preview);

    const text = el('div', 'bin-what');
    text.appendChild(el('strong', '', row.filename));
    text.appendChild(el('span', 'path', row.rel_path));
    line.appendChild(text);

    line.appendChild(el('span', 'bin-when', whenShort(row.deleted_at)));
    line.appendChild(el('span', 'bin-size', bytesShort(row.size)));
    line.appendChild(el('span', row.present ? 'bin-state' : 'bin-state missing',
      row.present ? 'In the bin' : 'Moved or erased'));
    list.appendChild(line);
  }
  renderBinFoot();
}

function renderBinFoot() {
  const n = binState.chosen.size;
  const held = binState.summary || {};
  const total = held.items
    ? `${plural(held.items, 'file', 'files')}, ${bytesShort(held.bytes || 0)}`
    : '';
  $('#bin-selected').textContent = n
    ? `${plural(n, 'file', 'files')} chosen`
    : (total ? `${total} in the bin` : 'Nothing chosen');
  $('#bin-restore').disabled = n === 0;
  $('#bin-delete').disabled = n === 0;
}

/**
 * Erase the chosen files, after asking for the password.
 *
 * The password is asked here as well as in the gallery, and for a different
 * reason: the gallery's delete can be taken back from this screen, and this
 * one cannot be taken back from anywhere. An open console says somebody is
 * administering the library; it does not say they meant to lose a photograph.
 */
function purgeChosen() {
  const ids = [...binState.chosen];
  if (!ids.length) return;

  const modal = $('#purge-modal');
  const input = $('#purge-password');
  const error = $('#purge-error');
  const confirm = $('#purge-confirm');

  $('#purge-what').textContent =
    `${plural(ids.length, 'file', 'files')} will be erased from the disk.`;
  error.hidden = true;
  input.value = '';
  modal.hidden = false;
  input.focus();

  const attempt = async () => {
    if (!input.value) { input.focus(); return; }
    confirm.disabled = true;
    try {
      const result = await adminApi.purgeDeleted(ids, input.value);
      closePurge();
      toast(result.purged
        ? `${plural(result.purged, 'file is', 'files are')} gone for good.`
        : 'Nothing could be erased.', !result.purged);
      for (const problem of result.failed || []) {
        toast(`${problem.name}: ${problem.why}`, true);
      }
      binState.chosen.clear();
      await loadRecycleBin();
    } catch (exc) {
      if (exc.status === 401 && exc.data?.needs_password) {
        error.textContent = exc.data.error;
        error.hidden = false;
        input.value = '';
        input.focus();
        return;
      }
      closePurge();
      toast(exc.message, true);
    } finally {
      confirm.disabled = false;
    }
  };

  confirm.onclick = attempt;
  $('#purge-cancel').onclick = closePurge;
  input.onkeydown = (event) => {
    if (event.key === 'Enter') { event.preventDefault(); attempt(); }
    if (event.key === 'Escape') closePurge();
  };
}

function closePurge() {
  $('#purge-modal').hidden = true;
  $('#purge-password').value = '';
  $('#purge-password').onkeydown = null;
  $('#purge-confirm').onclick = null;
  $('#purge-cancel').onclick = null;
}

async function restoreChosen() {
  const ids = [...binState.chosen];
  if (!ids.length) return;
  const button = $('#bin-restore');
  button.disabled = true;
  try {
    const result = await adminApi.restoreDeleted(ids);
    toast(result.restored
      ? `${plural(result.restored, 'file is', 'files are')} back where they were. `
        + 'They reappear in the library after the next scan.'
      : 'Nothing could be put back.', !result.restored);
    for (const problem of result.failed || []) {
      toast(`${problem.name}: ${problem.why}`, true);
    }
    binState.chosen.clear();
    await loadRecycleBin();
    // Restoring puts files back on the disk but not into the index; a scan is
    // what makes them visible again, so start one rather than leaving the
    // household wondering where the photographs went.
    if (result.restored) await adminApi.rescan(false).catch(() => {});
  } catch (exc) {
    toast(exc.message, true);
  } finally {
    renderBinFoot();
  }
}

/* --- bulk visibility: ask twice, and keep the way back ------------------ */

const VIS_WORDS = { public: 'everyone, guests included',
                    family: 'family members and admins',
                    hidden: 'admins only' };

function strong(text) {
  const node = document.createElement('strong');
  node.textContent = text;
  return node;
}

function plural(n, one, many) {
  return `${n.toLocaleString()} ${n === 1 ? one : many}`;
}

/**
 * Apply a folder rule, stopping first if it would reveal anything.
 *
 * The server refuses a widening change that has not been confirmed, and says
 * how much it would expose. That refusal is the first question; this modal is
 * the second. One mis-click on a control whose three buttons sit a few pixels
 * apart should not be able to publish photographs somebody hid on purpose.
 */
async function applyFolderVisibility(path, value) {
  try {
    let result;
    let confirmed = false;
    // One question, asked only when it applies: a change that would reveal
    // something has to be confirmed after the reader has seen how much. The
    // whole-library password that used to live here went with the control it
    // guarded — the root is refused outright now, not asked about.
    for (let attempt = 0; attempt < 3; attempt += 1) {
      try {
        result = await adminApi.setFolderVisibility(path, value, confirmed);
        break;
      } catch (exc) {
        if (exc.status === 409 && exc.data?.needs_confirmation) {
          if (!await confirmExposure(exc.data.impact, path, value)) return;
          confirmed = true;
          continue;
        }
        throw exc;
      }
    }
    if (!result) return;
    toast(`${plural(result.updated, 'item', 'items')} → ${value}.`);
    state.overview = await adminApi.overview();
    renderOverview();
    await loadFolders();
    await refreshUndo();
    renderPreview(currentPreview);
  } catch (exc) { toast(exc.message, true); }
}

function confirmExposure(impact, path, value) {
  return new Promise((resolve) => {
    const where = path ? `“${path}”` : 'the whole library';
    $('#expose-what').innerHTML = '';
    $('#expose-what').append(
      document.createTextNode('Setting '),
      strong(where),
      document.createTextNode(' to '),
      strong(value),
      document.createTextNode(` makes it visible to ${VIS_WORDS[value] || value}. `),
      strong(plural(impact.exposed, 'file is', 'files are')),
      document.createTextNode(' currently more private than that, and would '
        + 'become visible.'),
    );

    // Name *why* they are private, because "I hid those myself" and "the
    // filesystem hid those" are different decisions to be second-guessing.
    const notes = [];
    if (impact.decided_individually) {
      notes.push(`${plural(impact.decided_individually, 'file was', 'files were')} `
        + 'set individually and would be overwritten');
    }
    if (impact.hidden_by_the_filesystem) {
      notes.push(`${plural(impact.hidden_by_the_filesystem, 'file is', 'files are')} `
        + 'hidden because of where it sits on disk');
    }
    if (impact.child_rules) {
      notes.push(`${plural(impact.child_rules, 'folder rule', 'folder rules')} `
        + 'inside it would be overridden');
    }
    $('#expose-detail').textContent = notes.length ? `${notes.join('; ')}.` : '';
    $('#expose-detail').hidden = !notes.length;

    const modal = $('#expose-modal');
    modal.hidden = false;
    const close = (answer) => {
      modal.hidden = true;
      $('#expose-confirm').onclick = null;
      $('#expose-cancel').onclick = null;
      resolve(answer);
    };
    $('#expose-confirm').onclick = () => close(true);
    $('#expose-cancel').onclick = () => close(false);
  });
}

async function refreshUndo() {
  const strip = $('#vis-undo');
  if (!strip) return;
  try {
    const { changes } = await adminApi.visibilityHistory();
    const last = (changes || []).find((c) => !c.undone_at && c.restorable > 0);
    if (!last) { strip.hidden = true; return; }
    const where = last.scope === 'folder'
      ? (last.folder ? `“${last.folder}”` : 'the whole library')
      : `${plural(last.affected, 'item', 'items')}`;
    $('#vis-undo-what').textContent =
      `Last change: ${where} → ${VIS_NAMES_BY_VALUE[last.visibility] || last.visibility}`;
    $('#vis-undo-detail').textContent = last.exposed
      ? `${plural(last.exposed, 'file', 'files')} became visible to more people. `
        + `Undo puts every file back exactly as it was.`
      : `${plural(last.restorable, 'file', 'files')} can be put back exactly as they were.`;
    strip.hidden = false;
  } catch { strip.hidden = true; }
}

const VIS_NAMES_BY_VALUE = { 0: 'public', 1: 'family', 2: 'hidden' };

async function undoLastVisibility() {
  try {
    const result = await adminApi.undoVisibility(null);
    toast(`Put ${plural(result.restored, 'file', 'files')} back as they were.`);
    state.overview = await adminApi.overview();
    renderOverview();
    await loadFolders();
    await refreshUndo();
    renderPreview(currentPreview);
  } catch (exc) { toast(exc.message, true); }
}

function toast(message, isError = false) {
  // Nothing to say while the screen is locked: nobody is there to read it,
  // and requests refused behind the lock would pile their errors up here.
  if (document.body.classList.contains('screen-locked')) return;
  const node = el('div', `toast${isError ? ' error' : ''}`, message);
  $('#toasts').appendChild(node);
  setTimeout(() => {
    node.style.opacity = '0';
    setTimeout(() => node.remove(), 250);
  }, isError ? 5200 : 3200);
}

function randomPassword() {
  const words = ['river', 'maple', 'copper', 'lantern', 'harbour', 'meadow',
    'cedar', 'amber', 'summit', 'willow', 'cobalt', 'ember'];
  const pick = () => words[Math.floor(Math.random() * words.length)];
  return `${pick()}-${pick()}-${Math.floor(10 + Math.random() * 89)}`;
}

/* ── Ninaivu 5.0 Bitrot Scrubber ────────────────────────────────────────── */

const notifyApi = {
  get: () => json('/api/admin/notifications'),
  save: (body) => json('/api/admin/notifications', { method: 'POST', body }),
  test: () => json('/api/admin/notifications/test', { method: 'POST' }),
};

function describeNotifications(data) {
  const state = $('#notify-state');
  if (!state) return;
  state.textContent = data.configured
    ? 'Notifications are on.'
    : 'Nothing is being sent — add a webhook or an email address.';
}

// Runs from start(), not wireChrome(): the console wires itself before anyone
// has signed in, and asking then was a 401 that announced "Your session has
// ended" to someone who never had one, and left this list empty afterwards.
async function loadNotifications() {
  const box = $('#notify-events');
  if (!box) return;

  let settings;
  try {
    settings = await notifyApi.get();
  } catch {
    return;                       // not an admin, or the console is older
  }

  const chosen = new Set(settings.events || []);
  box.innerHTML = '';
  for (const [key, description] of Object.entries(settings.known_events || {})) {
    const row = el('label', 'toggle');
    const input = el('input');
    input.type = 'checkbox';
    input.value = key;
    input.checked = chosen.has(key);
    row.append(input, el('span', null, description));
    box.appendChild(row);
  }
  fillNotificationForm(settings.form);
  describeNotifications(settings);
}

// What was saved, in the fields. Opened empty, the form sent empty strings
// for everything on Save, so changing one tick box wiped the webhook and the
// email setup. The password never comes back; a blank one keeps it.
function fillNotificationForm(form) {
  if (!form) return;              // a server from before it said
  const set = (id, value) => { const input = $(id); if (input) input.value = value ?? ''; };
  set('#notify-webhook', form.webhook);
  set('#notify-smtp-host', form.smtp_host);
  set('#notify-smtp-port', form.smtp_port || 587);
  set('#notify-smtp-user', form.smtp_user);
  set('#notify-smtp-to', form.smtp_to);
  const password = $('#notify-smtp-password');
  if (password) {
    password.value = '';
    password.placeholder = form.smtp_password_saved ? 'saved — leave blank to keep' : 'password';
  }
}

function wireNotifications() {
  $$('#notify-save').onclick = async () => {
    const events = [...document.querySelectorAll('#notify-events input:checked')].map((i) => i.value);
    try {
      const result = await notifyApi.save({
        webhook: $('#notify-webhook')?.value || '',
        smtp_host: $('#notify-smtp-host')?.value || '',
        smtp_port: Number($('#notify-smtp-port')?.value || 587),
        smtp_user: $('#notify-smtp-user')?.value || '',
        smtp_password: $('#notify-smtp-password')?.value || '',
        smtp_to: $('#notify-smtp-to')?.value || '',
        events,
      });
      describeNotifications(result.settings);
      toast('Saved.');
    } catch (exc) {
      toast(exc.message, true);
    }
  };

  $$('#notify-test').onclick = async () => {
    try {
      const result = await notifyApi.test();
      toast(result.sent
        ? 'Sent. Check wherever you pointed it.'
        : `Nothing was sent: ${result.reason || result.webhook || result.email}`,
        !result.sent);
    } catch (exc) {
      toast(exc.message, true);
    }
  };
}

/* --- the weekly photograph ---------------------------------------------
 *
 * Its own section rather than another switch under Notifications, because it
 * is a different promise: those go to whoever looks after the machine and
 * never contain a photograph, and this goes to the family and is one. It
 * shares the mail server and nothing else.
 */

const digestApi = {
  get: () => json('/api/admin/digest'),
  save: (body) => json('/api/admin/digest', { method: 'POST', body }),
  preview: () => json('/api/admin/digest/preview'),
  send: () => json('/api/admin/digest/send', { method: 'POST' }),
};

function describeDigest(settings) {
  const state = $('#digest-state');
  if (state) {
    if (!settings.enabled) state.textContent = 'Not being sent.';
    else if (!settings.ready) state.textContent = 'On, but there is nobody to send it to.';
    else if (settings.last_sent) state.textContent = `On. Last sent ${settings.last_sent}.`;
    else state.textContent = 'On. Nothing sent yet.';
    if (settings.last_error) state.textContent += ` Last attempt failed: ${settings.last_error}`;
  }
  const warning = $('#digest-no-mail');
  if (warning) warning.hidden = Boolean(settings.has_mail_server);
}

async function loadDigest() {
  const to = $('#digest-to');
  if (!to) return;
  const hours = $('#digest-hour');
  if (hours && !hours.options.length) {
    for (let h = 0; h < 24; h += 1) {
      const option = document.createElement('option');
      option.value = String(h);
      // Plain words: "9 in the morning" is what somebody means by it.
      option.textContent = h === 0 ? 'midnight' : h === 12 ? 'midday'
        : h < 12 ? `${h} in the morning` : `${h - 12} in the afternoon`;
      hours.append(option);
    }
  }

  let settings;
  try {
    settings = await digestApi.get();
  } catch {
    return;                       // not an admin, or the console is older
  }
  to.value = settings.to || '';
  if ($('#digest-link')) $('#digest-link').value = settings.link || '';
  if ($('#digest-enabled')) $('#digest-enabled').checked = Boolean(settings.enabled);
  if ($('#digest-weekday')) $('#digest-weekday').value = String(settings.weekday);
  if (hours) hours.value = String(settings.hour);
  describeDigest(settings);

  // What would go out today, so nobody has to wait until Sunday to find out
  // whether this works.
  try {
    const seen = await digestApi.preview();
    const line = $('#digest-preview');
    if (!line) return;
    line.textContent = seen.ready
      ? `Today it would send "${seen.subject}" — ${seen.year}, `
        + `${seen.faces} ${seen.faces === 1 ? 'person' : 'people'} in it, `
        + `chosen from ${(seen.others + 1).toLocaleString()} from this day.`
      : `Today it would send nothing: ${seen.reason}.`;
  } catch { /* the preview is a nicety, not the feature */ }
}

function wireDigest() {
  $$('#digest-save').onclick = async () => {
    try {
      const result = await digestApi.save({
        enabled: $('#digest-enabled')?.checked || false,
        to: $('#digest-to')?.value || '',
        link: $('#digest-link')?.value || '',
        weekday: Number($('#digest-weekday')?.value ?? 6),
        hour: Number($('#digest-hour')?.value ?? 9),
      });
      describeDigest(result.settings);
      toast('Saved.');
    } catch (exc) {
      toast(exc.message, true);
    }
  };

  $$('#digest-send').onclick = async () => {
    const button = $('#digest-send');
    if (button) button.disabled = true;
    try {
      const result = await digestApi.send();
      toast(result.sent
        ? `Sent: ${result.subject}`
        : `Nothing was sent: ${result.reason}`, !result.sent);
      await loadDigest();
    } catch (exc) {
      toast(exc.message, true);
    } finally {
      if (button) button.disabled = false;
    }
  };
}

function wireScrubber() {
  const startBtn = $('#scrubber-start-btn');
  startBtn?.addEventListener('click', async () => {
    startBtn.disabled = true;
    try {
      const res = await adminApi.startScrubber();
      toast(res.message || 'Scrubber started');
      pollScrubber();
    } catch (err) {
      toast('Failed to start scrubber: ' + err.message, true);
      startBtn.disabled = false;
    }
  });
}

let scrubberPollTimer = null;
async function pollScrubber() {
  clearTimeout(scrubberPollTimer);
  const data = await loadScrubberStatus();
  if (data?.progress?.running) {
    scrubberPollTimer = setTimeout(pollScrubber, 2000);
  } else {
    const startBtn = $('#scrubber-start-btn');
    if (startBtn) startBtn.disabled = false;
  }
}

async function loadScrubberStatus() {
  try {
    const data = await adminApi.scrubberStatus();
    const set = (id, value) => {
      const node = $(id);
      if (node) node.textContent = String(value || 0);
    };
    set('#scrub-total', data.total_assets);
    set('#scrub-verified', data.verified);
    set('#scrub-baseline', data.baseline);
    set('#scrub-changed', data.changed);
    set('#scrub-corrupt', data.corrupt);
    set('#scrub-missing', data.missing);
    set('#scrub-unreadable', data.unreadable);

    // Only the three that need somebody to go and look turn red. "Edited" and
    // "fingerprinted" are ordinary states, not problems.
    const alarm = (id, n) => {
      const card = $(id);
      if (card) card.classList.toggle('bad', (n || 0) > 0);
    };
    alarm('#scrub-corrupt-card', data.corrupt);
    alarm('#scrub-missing-card', data.missing);
    alarm('#scrub-unreadable-card', data.unreadable);

    const issues = data.recent_issues || [];
    const wrap = $('#scrubber-issues-wrap');
    const tbody = $('#scrubber-issues-tbody');
    if (issues.length && wrap && tbody) {
      wrap.hidden = false;
      tbody.innerHTML = '';
      // Built as nodes rather than with innerHTML: these strings are
      // filenames off the disk, and a photograph called
      // `<img src=x onerror=...>.jpg` would otherwise run in the console.
      issues.forEach((iss) => {
        const tr = document.createElement('tr');

        const pathCell = document.createElement('td');
        const path = document.createElement('code');
        path.textContent = iss.rel_path || '';
        pathCell.appendChild(path);

        const statusCell = document.createElement('td');
        const tag = document.createElement('span');
        tag.className = `tag ${iss.status === 'corrupt' ? 'danger' : 'warn'}`;
        tag.textContent = iss.status || '';
        statusCell.appendChild(tag);

        const whenCell = document.createElement('td');
        whenCell.textContent = iss.checked_at
          ? new Date(iss.checked_at * 1000).toLocaleString()
          : 'Just now';

        tr.append(pathCell, statusCell, whenCell);
        tbody.appendChild(tr);
      });
    } else if (wrap) {
      wrap.hidden = true;
    }
    return data;
  } catch (err) {
    console.debug('Scrubber status fetch note:', err);
  }
}

/* ======================================================================== */

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
