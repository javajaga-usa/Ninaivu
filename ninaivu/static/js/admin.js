/**
 * The admin console (port 3000).
 *
 * A management tool, not a gallery: profiles, folder visibility, library
 * controls and an activity log — plus a live preview that answers the question
 * an admin actually has, which is "what will they see?"
 */

import {
  accountsApi, avatarNode, Gate, ProfileSheet, upgradeSecret,
} from './accounts.js';
import {
  admin, consoleJson as json, onUnauthorized, sessionRestored, subscribeProgress, thumbUrl,
  SCAN_COUNTS, timeLeft,
} from './api.js';
import { renderActivity, subscribeActivity } from './activity.js';
import { ArchivePanel } from './archive.js';
import { enterPressesTheButton } from './enter-key.js';
import { keepInApp } from './save-file.js';
import { CloudPanel } from './cloud.js';
import { WorkloadPanel } from './workload.js';
import { JobStatusPanel } from './job-status.js';
import { DiskPanel } from './disks.js';
import { VideoTrial } from './video-trial.js';
import { JournalPanel } from './journal.js';
import { SafetyPanel } from './safety.js';
import { ScreenLock } from './lock.js';
import { AIServerPanel } from './ai-server.js';
import { AIModelsPanel } from './ai-models.js';
import { ComponentsPanel } from './components.js';
import { MigrationPanel } from './migration.js';
import { claimBanner, HandoverPanel } from './handover.js';
import { AdvancedPanel } from './advanced.js';
import { ServerPanel } from './server.js';
import { PerformancePanel } from './performance.js';
import { TuningPanel } from './tuning.js';
import { FacesPanel } from './faces.js';
import { StraightenPanel } from './straighten.js';
import { ScreeningPanel } from './screening.js';
import { FirstDay } from './first-day.js';
import { DrivePrompt } from './drives.js';
import { labelled, revealItem } from './pages/console/common.js';
import * as i18n from './i18n.js';
import { consoleCommands, initPalette } from './palette.js';
import { hideScrollBarsUntilUsed } from './scrollbars.js';
import { consoleState as state } from './store.js';
import {
  $, $$, askPasswordToDelete, bytesShort, consoleToast as toast, el, emphasised, plural, span,
  strong,
} from './ui.js';

const adminApi = {
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
  assignable: () => json('/api/admin/assignable'),
  folder: (path, show = 'all', deep = false, offset = 0) =>
    json(`/api/admin/folder?folder=${encodeURIComponent(path || '')}`
      + `&show=${encodeURIComponent(show)}${deep ? '&deep=1' : ''}`
      + `${offset ? `&offset=${offset}` : ''}`),
  clearFolderVisibility: (folder, confirm) =>
    json(`/api/visibility/folder?folder=${encodeURIComponent(folder)}${confirm ? '&confirm=1' : ''}`,
      { method: 'DELETE' }),
  setItemsVisibility: (ids, visibility) =>
    json('/api/visibility', { method: 'POST', body: { ids, visibility } }),
  setExplicit: (ids, nsfw) =>
    json('/api/assets/bulk', { method: 'POST', body: { ids, nsfw } }),
  revealFolder: (folder) =>
    json('/api/admin/reveal-folder', { method: 'POST', body: { folder } }),
  deleteFromFolders: (ids) =>
    json('/api/admin/folders/delete', { method: 'POST', body: { ids } }),
  deleteAssets: (ids, password) =>
    json('/api/delete', { method: 'POST', body: { ids, password } }),
};

let currentPreview = 'guest';
let gate;
let profileSheet;
let archive;
let cloud;
let workload;
let jobStatus;
let diskPanel;
let videoTrial;
let journalPanel;
let safetyPanel;
let screenLock;
let aiServer;
let serverPanel;
let performancePanel;
let tuningPanel;
let aiModels;
let firstDay;
let drivePrompt;
let extras;
let migration;
let handoverPanel;
let advanced;
let facesPanel;
let straightenPanel = null;
let screeningPanel = null;

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
 * The console's worker caches nothing. It is there so that opening the
 * console while the server is down shows "Ninaivu is Offline", as the family
 * app does, instead of the browser's own error. Registering it replaces any
 * older worker on this origin, which is the console's own port, so the family
 * app's worker is untouched. Failure is silent for the same reasons as in
 * app.js: plain HTTP on a LAN address and private windows refuse workers.
 */
function registerServiceWorker() {
  if (!('serviceWorker' in navigator)) return;
  navigator.serviceWorker.register('/sw.js?console=1').catch(dropStaleWorkers);
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

  // The share glyph goes in as a substitution so the sentence around it can
  // be translated whole; the markup itself is nobody's words.
  const share = '<svg class="ios-share-glyph" viewBox="0 0 24 24"><path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8M16 6l-4-4-4 4M12 2v13"/></svg>';
  if (isIPad) {
    if (title) title.textContent = i18n.t('Install Ninaivu Admin on your iPad');
    if (desc) {
      desc.innerHTML = i18n.t('Tap {share} Share in Safari’s top toolbar, then select <strong>Add to Home Screen</strong> [+]', { share });
    }
  } else {
    if (title) title.textContent = i18n.t('Install Ninaivu Admin on your iPhone');
    if (desc) {
      desc.innerHTML = i18n.t('Tap {share} Share below, then select <strong>Add to Home Screen</strong> [+]', { share });
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

  // Before anything is drawn, as the family app does: the console is marked
  // up in English and translated in place, so starting later would show one
  // language turning into another.
  await i18n.start();
  wireLanguage();
  initPalette({ commands: consoleCommands });
  hideScrollBarsUntilUsed();

  registerServiceWorker();
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
      toast(i18n.t('Your session has ended. Please sign in again.'), true);
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
    toast(i18n.t('Cannot reach the server. Retrying automatically…'), true);
    const retryInterval = setInterval(async () => {
      try {
        const a = await accountsApi.state();
        clearInterval(retryInterval);
        state.auth = a;
        renderAppLinks(a);
        toast(i18n.t('Connected to Ninaivu server.'));
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
  // The language follows the person, as it does in the family app: what they
  // chose once, on any device, is what the console opens in.
  if (user.language && user.language !== i18n.language()
      && i18n.LANGUAGES.some((l) => l.code === user.language)) {
    await i18n.use(user.language);
  }
  renderIdentity();
  await screenLock?.start(user);
  subscribeProgress(onProgress);
  // Everything else that is running, in the same strip — see onActivity.
  subscribeActivity(onActivity);
  // Both ask admin-only endpoints, so they wait for a signed-in administrator.
  loadNotifications();
  pollScrubber();
  // The waiting-takeover line asks an administrator's endpoint too: made
  // here, once, so a console that is still at its sign-in form asks nothing.
  if (!document.querySelector('.handover-banner')) claimBanner($('#main'), { toast });
  await refresh();
  await loadPendingUploads();
  loadAttention();
  // The first day: once, right after the administrator is made.
  firstDay ||= new FirstDay({
    json, toast, openPage: (page) => showTab(page), refresh,
    pickFolder: (options) => openFolderPicker(options),
  });
  firstDay.maybeOpen();
  // Mugil → Two computers opens the same steps again, any time.
  const guide = document.getElementById('tc-guide');
  if (guide) guide.onclick = () => firstDay.openTwoComputers();
  // A drive, memory card or phone plugged into the Ninaivu computer: asked
  // about here, with Import as the first answer (drives.js).
  drivePrompt ||= new DrivePrompt({
    json, toast,
    openImport: async (path) => { showTab('archive'); return archive?.useSource(path); },
    openImportPage: () => showTab('archive'),
    browseDrive: async (drive) => { showTab('archive'); await archive?.browseFrom(drive.path, drive.label); },
  });
  drivePrompt.start();
  // Pages an extension brings (the AI server page is Creative Studio's) are
  // shown only while that extension is on.
  refreshExtensions();
  clearInterval(uploadPoll);
  uploadPoll = setInterval(() => {
    if (document.hidden || state.user?.role !== 'admin') return;
    loadPendingUploads();
    // "Needs you", the tab counts and the section marks follow the same
    // clock on every page, not only the Overview: the marks are there to
    // be seen from the other pages, and stood still at whatever they were
    // when the Overview was left.
    loadAttention();
  }, 10000);
  // Back on the page that was open. A refresh used to land on the Overview
  // whatever was open before it, so somebody watching an import refreshed
  // and found its numbers gone — they were still there, a page away.
  // Google's consent screen sends the browser back to `/#cloud?connected=1`,
  // which is the same rule: land on the tab that asked.
  const remembered = pageInAddress();
  if (remembered) showTab(remembered);
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
  button.title = `${state.user.name} · ${i18n.role(state.user.role_label)}`;
}

/* -- Language ------------------------------------------------------------ */

/* The same switch the family app's sign-in card and lock screen carry — one
   button per language, each named in itself — in the sidebar's foot and on
   the console's sign-in screen. Either placeholder may be missing from an
   older cached admin.html, so each is drawn only if it is there. */
function renderLanguageSwitches() {
  renderLanguageToggle($('#console-lang'));
  for (const holder of [$('#console-signin-lang')]) {
    if (!holder) continue;
    holder.replaceChildren();
    if (i18n.LANGUAGES.length < 2) continue;
    const row = el('div', 'gate-languages');
    row.setAttribute('role', 'group');
    row.setAttribute('aria-label', i18n.t('Language'));
    for (const { code, name } of i18n.LANGUAGES) {
      const pick = el('button', 'gate-lang', name);
      pick.type = 'button';
      pick.lang = code;
      const on = code === i18n.language();
      pick.classList.toggle('on', on);
      pick.setAttribute('aria-pressed', String(on));
      pick.onclick = async () => {
        if (code === i18n.language()) return;
        await i18n.use(code);
        rememberLanguage(code);
      };
      row.appendChild(pick);
    }
    holder.appendChild(row);
  }
}

/* The top bar's switch is one round letter, as in the family app: the letter
   of the language a press would give, its full name (in itself) the tooltip. */
function renderLanguageToggle(holder) {
  if (!holder) return;
  holder.replaceChildren();
  if (i18n.LANGUAGES.length < 2) return;
  const codes = i18n.LANGUAGES.map((l) => l.code);
  const next = i18n.LANGUAGES[(codes.indexOf(i18n.language()) + 1) % codes.length];
  const button = el('button', 'lang-toggle');
  button.type = 'button';
  button.title = next.name;
  button.setAttribute('aria-label', next.name);
  const letter = el('span', null, next.letter);
  letter.lang = next.code;
  button.appendChild(letter);
  button.onclick = async () => {
    await i18n.use(next.code);
    rememberLanguage(next.code);
  };
  holder.appendChild(button);
}

/** Save the language on the profile, so every device of theirs follows. */
async function rememberLanguage(code) {
  if (!state.user || !state.user.id) return;
  try {
    state.user = await accountsApi.updateMe({ language: code });
  } catch { /* this device still remembers it */ }
}

/* i18n.apply() redraws whatever admin.html marks up; everything this file
   builds itself is drawn again here. What it still holds is drawn from that;
   the page on screen, whose answer it did not keep, asks again. The other
   panels (Archive, Cloud and the rest) listen for the change themselves. */
function relabel() {
  renderLanguageSwitches();
  // Before anybody has signed in there is nothing drawn but the sign-in card,
  // which redraws itself.
  if (!state.user) return;
  renderIdentity();
  const current = document.querySelector('#tabs button.active')?.dataset.tab;
  if (current) showPageHeading(current);
  if (state.overview) {
    renderOverview();
    renderLibrary();
    ifOpened('people', (page) => page.renderPeople());
    renderFolderTree();
    renderPreview(currentPreview);
  }
  loadAttention();
  renderFolderScreen();
  ifOpened('recycle-bin', (page) => page.render());
  ifOpened('library', (page) => page.relabel());
  refreshUndo();
  // The uploads list is only rebuilt when the queue changes; forget what it
  // last drew so the next look draws it again in the new language.
  ifOpened('uploads', (page) => page.relabel());
  if (current === 'uploads') { loadPendingUploads(); openPage('uploads').then((page) => page?.show()); }
  if (current === 'server') openPage('uploads').then((page) => page?.loadTvAlbum());
  if (current === 'large-files') openPage('large-files').then((page) => page?.show());
  if (current === 'health') openPage('health').then((page) => page?.show());
  if (current === 'activity') loadActivity();
  if (current === 'settings') refreshExtensions();
  if (current === 'ai-models') refreshExtensions();
  ifOpened('notifications', (page) => page.relabel());
  pollScrubber();
}

function wireLanguage() {
  renderLanguageSwitches();
  i18n.onChange(relabel);
  // The lock screen says which language it switched to; keep it on the profile.
  document.addEventListener('ninaivu:language', (event) => rememberLanguage(event.detail));
}

/* ======================================================================== */

function wireChrome() {
  // Enter in a field does what the button beside it does, everywhere.
  enterPressesTheButton();
  $$('#uploads-refresh').onclick = () => {
    loadPendingUploads();
    openPage('uploads').then((page) => page?.show());
  };
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
  // Pressed by the command bar as it opens the page: handed over to the page.
  $$('#add-person-btn').onclick = () => openPage('people').then((page) => page?.toggleAddPerson());
  $$('#change-root').onclick = () => openFolderPicker();
  $$('#rescan-btn').onclick = () => runScan(false);
  $$('#full-rescan-btn').onclick = () => runScan(true);
  $$('#vis-undo-btn').onclick = undoLastVisibility;
  wireFolderScreen();
  // The command bar opens Health and presses these at once, so they are wired
  // here, before the page's module has come, and hand over to it.
  $$('#backup-now').onclick = () => openPage('health').then((page) => page?.backupNow());
  $$('#backup-verify').onclick = () => openPage('health').then((page) => page?.verifyBackup());
  // The dialogs that ask for the password get Show/Hide in the box.
  for (const input of document.querySelectorAll('.modal .field input[type=password]')) upgradeSecret(input);
  $$('#lf-delete-modal').addEventListener('click', (event) => {
    if (event.target === $('#lf-delete-modal')) $('#lf-delete-cancel')?.click();
  });
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
  jobStatus = new JobStatusPanel({ toast });
  jobStatus.wire();
  diskPanel = new DiskPanel({ toast });
  journalPanel = new JournalPanel({ toast });
  diskPanel.wire();
  videoTrial = new VideoTrial({ toast });
  videoTrial.wire();
  safetyPanel = new SafetyPanel({ toast, openPage: (page) => showTab(page) });
  safetyPanel.wire();

  // The AI server tab: its own module too. It loads when shown and never polls.
  aiServer = new AIServerPanel({ toast });
  aiServer.wire();
  aiModels = new AIModelsPanel({ toast });
  extras = new ComponentsPanel({ toast });
  // Migration: what to copy to another machine, and where the library
  // went once it is there. It asks the server nothing until it is open.
  migration = new MigrationPanel({ toast, pickFolder: (options) => openFolderPicker(options) });
  // The handover plan, and the line every administrator sees while somebody's
  // takeover is waiting (handover.js).
  if (!handoverPanel) {
    handoverPanel = new HandoverPanel({ toast, pickFolder: (options) => openFolderPicker(options) });
  }
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
  tuningPanel = new TuningPanel({
    toast,
    restart: () => { showTab('server'); serverPanel.confirmRestart(null); },
  });
  tuningPanel.wire();

  // The Faces tab: same self-contained shape, and it only asks the server
  // anything while it is the tab on screen.
  facesPanel = new FacesPanel({ toast, familyUrl: () => computeFamilyUrl() });
  facesPanel.wire();

  // The Straighten tab: a long-running pass over the library, so like the
  // others it only polls while it is the tab on screen.
  straightenPanel = new StraightenPanel({ toast });
  straightenPanel.wire();
  screeningPanel = new ScreeningPanel({ toast, onChange: () => loadAttention() });
  screeningPanel.wire();
  $$('#faces-review-close').onclick = () => ($('#faces-review').hidden = true);
  $$('#faces-review').addEventListener('click', (event) => {
    if (event.target === $('#faces-review')) $('#faces-review').hidden = true;
  });
  wireScrubber();
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
  overview: i18n.key('Your family library, at a glance.'),
  folders: i18n.key('Browse, organize and care for your media files.'),
  'large-files': i18n.key('Find the files taking up the most space.'),
  archive: i18n.key('Bring your memories together in one organized archive.'),
  people: i18n.key('Manage the people who share your library.'),
  visibility: i18n.key('The rules for everyone, then who can see each part of your library.'),
  faces: i18n.key('Review and organize the people in your photos.'),
  uploads: i18n.key('Review family contributions before they enter the library.'),
  straighten: i18n.key('Review suggested orientation corrections.'),
  screening: i18n.key('Items family members asked you to look at.'),
  'ai-models': i18n.key('What the scan does with AI, and the models it does it with.'),
  'ai-server': i18n.key('Configure the service that powers your AI features.'),
  library: i18n.key('Library folders, and how they are indexed.'),
  cloud: i18n.key('Mugil: the encrypted copy of the library in the cloud, and how it is going.'),
  restore: i18n.key('Bring photographs back from the cloud copy — carefully, and never over what is here.'),
  health: i18n.key('Drives, storage checks, problems and the copies of the index.'),
  settings: i18n.key('This home’s name, extensions, and what is installed on this computer.'),
  activity: i18n.key('Check recent activity, problems and state backups.'),
  extras: i18n.key('Install what Ninaivu can run without but is better with, on this computer.'),
  migration: i18n.key('Move Ninaivu to another computer, and tell it where the library went.'),
  handover: i18n.key('Who looks after the library if you cannot: written down, printed, and a safe way for them to take over.'),
  advanced: i18n.key('Every setting in its group, with what it means and its default.'),
  server: i18n.key('Watch the machine Ninaivu runs on, change its resource mode, restart it and read its log.'),
  performance: i18n.key('What this computer can do for Ninaivu, and what would help it do more.'),
  tuning: i18n.key('How much of this computer Ninaivu may use, sized to its cores, memory and drives.'),
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

/* The heading over the page: its section, its name and the line under it.
   Apart from showTab so a change of language can draw it again. */
function showPageHeading(name) {
  const opened = document.querySelector(`#tabs button[data-tab="${name}"]`);
  const group = opened ? opened.dataset.group : '';
  const title = $('#page-title');
  // The label alone: the Uploads tab also carries a live count, which the
  // heading would freeze at whatever it was when the page opened.
  if (title) title.textContent = opened?.querySelector('.tab-label')?.textContent || i18n.t('Overview');
  const section = $('#page-section');
  if (section) section.textContent =
    document.querySelector(`#tab-groups button[data-group="${group}"]`)?.textContent || i18n.t('Home');
  const description = $('#page-description');
  if (description) description.textContent = PAGE_DESCRIPTIONS[name] ? i18n.t(PAGE_DESCRIPTIONS[name]) : '';
}

// Sections that used to be pages of their own and now live inside another
// page. The server still names them — the Performance page's "Open Extras",
// a running install on Activity — and a name with no page behind it opened
// nothing at all.
const sectionPages = {
  extras: { page: 'settings', section: '#extras-block' },
};

/** The console page named in the address (`#archive`), if it is one. */
function pageInAddress() {
  const name = decodeURIComponent((location.hash || '').slice(1).split('?')[0]);
  if (!name || name === 'overview') return '';
  if (sectionPages[name]) return name;
  const tab = document.querySelector(`#tabs button[data-tab="${CSS.escape(name)}"]`);
  // A page an extension brings may not be there this time; Overview it is.
  return tab && !tab.dataset.needsExtension ? name : '';
}

/** Keep the open page in the address, so a refresh comes back to it. */
function rememberPage(name) {
  const current = decodeURIComponent((location.hash || '').slice(1).split('?')[0]);
  // A hash that already names this page keeps whatever follows it: the cloud
  // page reads `?connected=1` from there, and tidies it away itself.
  if (current === name) return;
  try {
    history.replaceState(null, '', name === 'overview'
      ? `${location.pathname}${location.search}` : `#${name}`);
  } catch { /* an address that cannot be changed only costs the memory */ }
}

/* -- pages ----------------------------------------------------------------
 *
 * Each page that has a module of its own (static/js/pages/console/) is
 * fetched the first time it is opened, and its controls are wired then. The
 * import() calls are written out in full so the server finds them and names
 * each at its stamped URL in the import map (server/modules.py).
 */
const PAGE_MODULES = {
  health: () => import('./pages/console/health.js'),
  'large-files': () => import('./pages/console/large-files.js'),
  library: () => import('./pages/console/library.js'),
  // Uploads, phone backups and keys, and the Server page's TV album.
  uploads: () => import('./pages/console/uploads.js'),
  // Loaded at sign-in, to draw the list loadPeople fetches.
  people: () => import('./pages/console/people.js'),
  // Sections of the Health page: fetched at sign-in, drawn once it is opened.
  notifications: () => import('./pages/console/notifications.js'),
  scrubber: () => import('./pages/console/scrubber.js'),
  // A section of the Folders page, opened with it.
  'recycle-bin': () => import('./pages/console/recycle-bin.js'),
};
const loadedPages = new Map();

/** The module of page *name*, loaded and wired once; null if it has none. */
function openPage(name) {
  const load = PAGE_MODULES[name];
  if (!load) return Promise.resolve(null);
  if (!loadedPages.has(name)) {
    loadedPages.set(name, load().then((module) => {
      module.wire?.(shell);
      return module;
    }).catch((exc) => {
      // Not kept: the next opening asks again (the server was restarting, say).
      loadedPages.delete(name);
      toast(exc.message, true);
      return null;
    }));
  }
  return loadedPages.get(name);
}

/** Run *use* with page *name*, if it has been opened; nothing is drawn otherwise. */
function ifOpened(name, use) {
  // A page that throws while drawing what a poll brought is said in the
  // console, as the poll's own try/catch said it before the page was a module,
  // rather than left as an unhandled rejection.
  loadedPages.get(name)?.then((page) => page && use(page))
    .catch((exc) => console.debug(`The ${name} page could not draw this`, exc?.message || exc));
}

/** What a page may ask of the console around it. */
const shell = {
  showTab: (name) => showTab(name),
  refresh: () => refresh(),
  loadAttention: () => loadAttention(),
  openFolderPicker: (options) => openFolderPicker(options),
  notifications: () => lastNotifications,
  scrubber: () => lastScrub,
  loadPeople: () => loadPeople(),
  loadPendingUploads: () => loadPendingUploads(),
  get uploads() { return uploads; },
  renderPreview: () => renderPreview(currentPreview),
  // The overview's numbers changed: draw what shows them again.
  overviewChanged: async () => {
    state.overview = await adminApi.overview();
    renderOverview();
    ifOpened('library', (page) => page.renderLibraryFolders());
  },
};

function showTab(name) {
  const section = sectionPages[name];
  if (section) {
    showTab(section.page);
    rememberPage(name);
    document.querySelector(section.section)?.scrollIntoView({ block: 'start' });
    return;
  }
  rememberPage(name);
  // Which group owns this page. Deep links and the post-sign-in resume both
  // call showTab directly, so the group row is derived here rather than in the
  // click handlers -- otherwise arriving at a page would leave the wrong
  // section highlighted and its siblings hidden.
  const opened = document.querySelector(`#tabs button[data-tab="${name}"]`);
  const group = opened ? opened.dataset.group : '';
  showPageHeading(name);
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
  if (name === 'uploads') { loadPendingUploads(); openPage('uploads').then((page) => page?.show()); }
  if (name === 'visibility') { loadFolders(); refreshUndo(); }
  if (name === 'folders') {
    loadFolderScreen(foldState.path);
    openPage('recycle-bin').then((page) => page?.load());
  }
  if (name === 'large-files') openPage('large-files').then((page) => page?.show());
  // Loaded when the page opens, the way every other panel here loads —
  // not once at sign-in. A section whose only chance to fill itself was
  // the moment somebody signed in stays blank until a reload if that one
  // call fails, and cannot show what today would send.
  if (name === 'health') openPage('health').then((page) => page?.show());
  if (name === 'health') { openPage('notifications'); openPage('scrubber'); }
  if (name === 'library') openPage('library').then((page) => page?.show());
  // Only the visible tab holds an event stream open.
  if (name === 'archive') archive?.show(); else archive?.hide();
  if (name === 'cloud') cloud?.show(); else cloud?.hide();
  if (name === 'restore') cloud?.showRestore(); else cloud?.hideRestore();
  if (name === 'activity') workload?.show(); else workload?.hide();
  if (name === 'activity') jobStatus?.show(); else jobStatus?.hide();
  if (name === 'health') diskPanel?.show(); else diskPanel?.hide();
  if (name === 'large-files') videoTrial?.show(); else videoTrial?.hide();
  if (name === 'health') journalPanel?.show();
  // The AI server page is Creative Studio's; without it, there is nothing to ask.
  if (name === 'ai-server' && activeExtensions.has('creative-studio')) aiServer?.show();
  if (name === 'server') {
    serverPanel?.show();
    openPage('uploads').then((page) => page?.loadTvAlbum());
  } else serverPanel?.hide();
  if (name === 'performance') performancePanel?.show(); else performancePanel?.hide();
  if (name === 'tuning') tuningPanel?.show(); else tuningPanel?.hide();
  if (name === 'ai-models') aiModels?.show(); else aiModels?.hide();
  if (name === 'ai-models' || name === 'settings') refreshExtensions();
  if (name === 'settings') extras?.show(); else extras?.hide();
  if (name === 'migration') migration?.show(); else migration?.hide();
  if (name === 'handover') handoverPanel?.show(); else handoverPanel?.hide();
  if (name === 'advanced') advanced?.show(); else advanced?.hide();
  if (name === 'faces') facesPanel?.show(); else facesPanel?.hide();
  if (name === 'straighten') straightenPanel?.show(); else straightenPanel?.hide();
  if (name === 'screening') screeningPanel?.show(); else screeningPanel?.hide();
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
      // The page by the name the sidebar gives it, which is already in the
      // reader's language.
      const pageName = document.querySelector(`#tabs button[data-tab="${page}"] .tab-label`)?.textContent
        || page.replace('-', ' ');
      node.setAttribute('aria-label', i18n.t('{label}: {value}. Open {page}', { label, value, page: pageName }));
    }
    node.appendChild(el('div', 'card-label', label));
    node.appendChild(el('div', 'card-value', value));
    if (sub) node.appendChild(el('div', 'card-sub', sub));
    return node;
  };

  cards.append(
    card(i18n.t('In the library'), (stats.count || 0).toLocaleString(),
      i18n.t('{photos} photos · {videos} videos · {audio} audio',
        { photos: stats.pictures || 0, videos: stats.videos || 0, audio: stats.audio || 0 }),
      '', 'folders'),
    card(i18n.t('Public'), (stats.public || 0).toLocaleString(), i18n.t('visible to guests'),
      stats.public ? 'good' : '', 'visibility'),
    card(i18n.t('Hidden'), (stats.hidden || 0).toLocaleString(), i18n.t('admins only'),
      stats.hidden ? 'warn' : '', 'visibility'),
    card(i18n.t('Profiles'), String(data.people.total),
      Object.entries(data.people.by_role)
        .filter(([, n]) => n)
        .map(([role, n]) => (ROLE_COUNTS[role] ? i18n.t(ROLE_COUNTS[role], { count: n }) : `${n} ${role}`))
        .join(' · '), '', 'people'),
    // People, not sessions: one person on a phone, a tablet and two tabs is
    // one person signed in. `sessions` is the fallback for an older server.
    card(i18n.t('Signed in now'), String(data.people.signed_in ?? data.people.sessions),
      i18n.t('people with a live session'), '', 'activity'),
    aiCard(card, data.ai || {}),
  );
}

// How many of each role the Profiles card counts — "2 admin · 1 family",
// as the server names the roles.
const ROLE_COUNTS = {
  guest: i18n.key('{count} guest'),
  family: i18n.key('{count} family'),
  admin: i18n.key('{count} admin'),
};

/* Ninaivu started with AI off says "none" for both its engine and its model,
   and the card read "none / none". */
function aiCard(card, ai) {
  if (ai.semantic) return card('AI', i18n.t('Semantic'), ai.model || i18n.t('search and tagging'), '', 'ai-models');
  if (!ai.engine || ai.engine === 'none') {
    return card('AI', i18n.t('Off'), i18n.t('search by what is in a photo is off — turn it on'), '', 'ai-models');
  }
  if (ai.engine === 'loading') return card('AI', i18n.t('Starting'), i18n.t('search and tagging'), '', 'ai-models');
  return card('AI', ai.engine,
    ai.model && ai.model !== 'none' ? ai.model : i18n.t('search and tagging'), '', 'ai-models');
}

async function loadAssignable() {
  try {
    state.assignable = (await adminApi.assignable()).folders;
  } catch { state.assignable = []; }
}

function renderLibrary() {
  const data = state.overview;
  ifOpened('library', (page) => page.renderLibraryFolders());
  // Open the console from a laptop against the machine in the study and
  const box = $('#overview-library');
  box.innerHTML = '';
  const folderCount = (data.library.folders || []).length;
  // The first of each row says which it is, the second is what is shown.
  const rows = [
    ['folders', i18n.t('Library folders'), folderCount
      ? (folderCount === 1 ? i18n.t('1 folder') : i18n.t('{count} folders', { count: folderCount }))
      : i18n.t('None yet')],
    ['app', i18n.t('Family app'), data.app.home_url],
    ['guests', i18n.t('Guests without a login'), data.app.open_browsing ? i18n.t('Allowed') : i18n.t('Blocked')],
    ['watch', i18n.t('Watching for changes'), data.app.watch ? i18n.t('Yes') : i18n.t('No')],
  ];
  const list = el('dl', 'kv');
  for (const [which, key, value] of rows) {
    list.appendChild(el('dt', null, key));
    const dd = el('dd');
    if (which === 'app' && value && (value.startsWith('http://') || value.startsWith('https://'))) {
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
    if (which === 'folders' && folderCount && data.library.root) {
      dd.appendChild(el('code', 'kv-path', data.library.root));
    }
    list.appendChild(dd);
  }
  box.appendChild(list);

  // Settings. Each switch is rendered on the page of the thing it governs:
  // the rules for everyone on Visibility, indexing on Library settings, the
  // AI passes on AI models, and only this home's name here on Home & extensions. They
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
  nameBlock.appendChild(el('label', null, i18n.t('Name for this home')));
  const nameRow = el('div', 'row');
  const nameInput = el('input', 'input');
  nameInput.value = data.app.house_name || '';
  nameInput.maxLength = 40;
  nameInput.placeholder = i18n.t('Ninaivu');
  nameInput.setAttribute('aria-label', i18n.t('Name for this home'));
  const nameSave = el('button', 'btn', i18n.t('Save'));
  nameSave.type = 'button';
  const saveHouseName = async () => {
    nameSave.disabled = true;
    try {
      const result = await adminApi.settings({ house_name: nameInput.value });
      state.overview.app.house_name = result.settings.house_name;
      nameInput.value = result.settings.house_name || '';
      toast(i18n.t('Everyone now sees “{name}”.', { name: result.settings.house_name_effective }));
    } catch (exc) {
      toast(exc.message, true);
    } finally {
      nameSave.disabled = false;
    }
  };
  nameSave.onclick = saveHouseName;
  nameInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') saveHouseName(); });
  nameRow.append(nameInput, nameSave);
  const versionLine = $('#app-version');
  if (versionLine) {
    versionLine.textContent = [
      data.app.version ? i18n.t('Ninaivu {version}', { version: data.app.version }) : '',
      data.app.copyright || '',
      data.app.licence ? i18n.t('Licence: {licence}', { licence: data.app.licence }) : '',
    ].filter(Boolean).join(' · ');
  }
  nameBlock.appendChild(nameRow);
  nameBlock.appendChild(el('p', 'hint',
    i18n.t('Shown at the top of the family app and on the home screen icon. Family members can keep their own name for it instead — that one is private to them.')));
  settings.appendChild(nameBlock);
  const toggles = [
    [access, 'open_browsing', i18n.t('Let visitors browse public media without signing in'),
      data.app.open_browsing],
    [access, 'nsfw_filter', i18n.t('Flag adult content as explicit (admins only; screenshots and documents are hidden, not explicit)'),
      data.app.nsfw_filter],
    [access, 'hide_screens', i18n.t('Hide screenshots, documents and photos of screens (admins only)'),
      data.app.hide_screens ?? true],
    [indexing, 'watch', i18n.t('Watch the folder and index new files automatically'), data.app.watch],
    [aiSwitches, 'video_keyframes', i18n.t('Describe videos by several moments across the clip'),
      (data.app.video_keyframes ?? 5) >= 2,
      i18n.t('A holiday video is a beach, then a restaurant, then a car park, and one frame near the start describes none of them. It is also the slowest thing a scan does — five decodes and five passes through the model per video, which on a processor is hours for a few thousand clips. Turned off, videos are still found and still searchable; they are described by their poster frame alone.')],
    // The three below are off by default and until now had no switch at all
    // — only a start-up flag, or config.json by hand — so a household had no
    // way to know they existed, let alone turn them on.
    [aiSwitches, 'place_names', i18n.t('Name the places photographs were taken'),
      data.app.place_names ?? false,
      i18n.t('Turns the coordinates a phone records into a town and a country you can search for. Fast — a lookup against a list kept on this machine; twenty thousand photographs take seconds. The list, about 11 MB, is downloaded once, the first time.')],
    [aiSwitches, 'ocr_enabled', i18n.t('Read the words in photographs'),
      data.app.ocr_enabled ?? false,
      i18n.t('So a search for a shop name, a menu or a street sign finds the photograph it is in. Needs the text reader from Extras. Slow on a processor, and it reads every photograph once.')],
    [aiSwitches, 'faces_enabled', i18n.t('Find the people in photographs'),
      data.app.faces_enabled ?? false,
      i18n.t('Groups photographs by who is in them. The slowest thing a scan does after describing videos — roughly half a second a photograph on a processor, so most of a day for a large library. Turned off part-way, it stops where it is and carries on from there next time.')],
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
        toast(i18n.t('Saved.'));
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
    ffmpeg: i18n.t('ffmpeg — video thumbnails and metadata'),
    heif: i18n.t('pillow-heif — iPhone HEIC photos'),
    opencv: i18n.t('OpenCV — fallback video frames'),
  };
  for (const [key, present] of Object.entries(data.capabilities)) {
    const row = el('div', `cap ${present ? 'on' : 'off'}`);
    row.appendChild(el('span', 'cap-dot'));
    row.appendChild(el('span', null, labels[key] || key));
    row.appendChild(el('span', 'cap-state', present ? i18n.t('installed') : i18n.t('not installed')));
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
      line.textContent = i18n.t('Ready: {name} is installed.', { name: need.needs });
    } else {
      line.append(value
        ? i18n.t('On, but nothing happens yet: needs {name}, which is not installed.', { name: need.needs })
        : i18n.t('Needs {name}, which is not installed.', { name: need.needs }), ' ');
      if (need.model) {
        const link = el('a', null, i18n.t('Install it below'));
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
// listing four things that are empty. Each count is also put on the tab of
// the page it opens, and the phone's section row marks the sections holding
// one of those tabs. The mark used to follow a list of its own here, which
// lit "Backup & health" for a handover plan nobody had written and "People"
// for faces without a name while no tab in either showed a number: a dot
// that could not be traced to anything. Now a section is marked only when a
// tab inside it carries the count that marked it.
function showTabCounts(items) {
  const marked = new Set();
  for (const item of items) {
    const tab = document.querySelector(`#tabs button[data-tab="${item.page}"]`);
    const badge = tab?.querySelector('.tab-count');
    if (!badge) continue;
    const count = Number(item.count) || 0;
    badge.textContent = count > 99 ? '99+' : count ? String(count) : '';
    badge.hidden = !count;
    if (count) marked.add(tab.dataset.group);
  }
  document.querySelectorAll('#tab-groups button[data-group]').forEach((button) => {
    button.classList.toggle('needs-attention', marked.has(button.dataset.group));
  });
}

async function loadAttention() {
  const list = $('#attention-list');
  const headline = $('#attention-headline');
  if (!list || !headline) return;
  let data;
  try {
    data = await adminApi.attention();
  } catch {
    headline.textContent = i18n.t('Could not check what is waiting.');
    return;
  }
  const waiting = (data.items || []).filter((item) => item.count > 0);
  showTabCounts(data.items || []);
  headline.textContent = !waiting.length ? i18n.t('Nothing is waiting for you.')
    : data.total === 1 ? i18n.t('1 thing is waiting for you.')
      : i18n.t('{count} things are waiting for you.', { count: data.total });
  list.replaceChildren();
  for (const item of waiting) {
    const button = document.createElement('button');
    button.type = 'button';
    button.dataset.openPage = item.page;
    const count = document.createElement('span');
    count.className = 'shortcut-symbol attention-count';
    count.textContent = item.count > 99 ? '99+' : String(item.count);
    const text = document.createElement('span');
    const title = document.createElement('strong');
    // Fixed sentences from the server, marked there with said().
    title.textContent = i18n.t(item.title);
    const detail = document.createElement('small');
    detail.textContent = i18n.t(item.detail);
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
  state.textContent = pending ? i18n.t('Restart Ninaivu to apply')
    : (active.size ? i18n.t('{count} on', { count: active.size }) : i18n.t('All off'));
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
          ? i18n.t('{name} is on from the next start of Ninaivu.', { name: ext.title })
          : i18n.t('{name} is off from the next start of Ninaivu.', { name: ext.title }));
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
    summary.textContent = ext.summary ? i18n.t(ext.summary) : '';
    li.append(summary);
    const leaves = document.createElement('p');
    leaves.className = 'hint';
    if (ext.data_leaves_the_machine) {
      const strong = document.createElement('strong');
      strong.textContent = i18n.t('When it is on, something leaves this computer:') + ' ';
      leaves.append(strong, ext.destination ? i18n.t(ext.destination) : i18n.t('see its README.'));
    } else {
      leaves.textContent = i18n.t('Nothing leaves this computer.');
    }
    li.append(leaves);
    if (ext.downloads) {
      const dl = document.createElement('p');
      dl.className = 'hint subtle';
      dl.textContent = i18n.t('Downloads: {what}', { what: i18n.t(ext.downloads) });
      li.append(dl);
    }
    for (const problem of ext.problems || []) {
      const p = document.createElement('p');
      p.className = 'hint';
      p.textContent = i18n.t('Not usable: {problem}', { problem });
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
  wireOutsideAi();
}

/* Whether family members may send photographs to an outside extension. */
function wireOutsideAi() {
  const box = $('#outside-ai-family');
  if (!box) return;
  const app = state.overview?.app;
  if (app) box.checked = !!app.outside_ai_for_family;
  if (box.dataset.wired) return;
  box.dataset.wired = '1';
  box.addEventListener('change', async () => {
    try {
      await adminApi.settings({ outside_ai_for_family: box.checked });
      if (state.overview?.app) state.overview.app.outside_ai_for_family = box.checked;
      toast(box.checked ? i18n.t('Family members can send photographs to outside extensions.')
        : i18n.t('Only an administrator can send photographs to outside extensions.'));
    } catch (exc) {
      box.checked = !box.checked;
      toast(exc.message, true);
    }
  });
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
    state.textContent = i18n.t('Off — no key');
  } else if (fromEnvironment) {
    state.textContent = i18n.t('On — key {hint} from {variable}', { hint: status.hint, variable: status.variable });
  } else {
    state.textContent = i18n.t('On — key {hint}', { hint: status.hint });
  }
  // A key from the server's environment wins over one typed here, so offering
  // to save one would be offering to write down something never used.
  input.disabled = fromEnvironment;
  save.disabled = fromEnvironment;
  input.placeholder = fromEnvironment ? i18n.t('Set in the server’s environment')
    : status.set ? i18n.t('Paste a new key to replace it') : i18n.t('Paste an API key');
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
    save.textContent = i18n.t('Checking…');
    try {
      showGemini(await adminApi.setGemini(key));
      toast(i18n.t('Gemini is on. The key worked.'));
    } catch (exc) {
      toast(exc.message, true);
    } finally {
      // Cleared either way: a key sitting in a form field is a key anybody
      // walking past the screen can copy.
      input.value = '';
      save.disabled = false;
      save.textContent = i18n.t('Save');
    }
  });
  $('#gemini-remove')?.addEventListener('click', async () => {
    try {
      showGemini(await adminApi.setGemini(''));
      toast(i18n.t('Gemini is off. The key was removed from this machine.'));
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
    { key: 'guest', label: i18n.t('A guest') },
    { key: 'family', label: i18n.t('A family member') },
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
  box.replaceChildren(el('p', 'hint', i18n.t('Checking…')));
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
  summary.appendChild(el('strong', null, i18n.items(data.total)));
  const detail = [i18n.t('as {who}', { who: data.as })];
  if (data.scope) detail.push(i18n.t('limited to {folder}', { folder: data.scope }));
  summary.appendChild(el('span', 'hint', detail.join(' · ')));
  box.appendChild(summary);

  if (!data.total) {
    box.appendChild(el('p', 'empty-note',
      i18n.t('Nothing at all. Publish a folder on the Visibility tab to let them see something.')));
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
    tile.title = `${item.name} — ${item.folder || i18n.t('root')}`;
    strip.appendChild(tile);
  }
  box.appendChild(strip);
}

/* -- People -------------------------------------------------------------- */
/* The page itself is pages/console/people.js. The list is fetched here, at
   sign-in and after every change, because the preview needs it too. */

async function loadPeople() {
  try {
    const data = await accountsApi.people();
    state.people = data.people;
    state.roles = data.roles;
  } catch (exc) {
    toast(exc.message, true);
    return;
  }
  await openPage('people').then((page) => page?.renderPeople());
}


/* -- Visibility ---------------------------------------------------------- */

// The three levels as the server names them, which is also how the tags on
// the page have always read.
const VISIBILITY_WORDS = {
  public: i18n.key('public'),
  family: i18n.key('family'),
  hidden: i18n.key('hidden'),
};

function visibilityWord(level) {
  return VISIBILITY_WORDS[level] ? i18n.t(VISIBILITY_WORDS[level]) : level;
}

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
    path: '', name: i18n.t('Whole library'), depth: 0, count: total,
    rule: state.rootRule,
  }));
  const parents = new Set(state.folders.map((folder) => parentOf(folder.path)));
  for (const folder of [...state.folders].sort(byTreeOrder)) {
    if (shownInTree(folder.path)) {
      tree.appendChild(folderRow(folder, parents.has(folder.path)));
    }
  }
  if (!state.folders.length) {
    tree.appendChild(el('p', 'hint', i18n.t('No folders indexed yet.')));
  }
}

function folderToggle(folder, hasChildren) {
  if (!hasChildren) return el('span', 'folder-toggle');
  const open = openFolders.has(folder.path);
  const toggle = el('button', 'folder-toggle');
  toggle.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 6 6 6-6 6"/></svg>';
  toggle.type = 'button';
  toggle.setAttribute('aria-expanded', String(open));
  toggle.setAttribute('aria-label', open ? i18n.t('Collapse {folder}', { folder: folder.name || folder.path })
    : i18n.t('Expand {folder}', { folder: folder.name || folder.path }));
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
  name.appendChild(el('span', null, folder.name || folder.path || i18n.t('Whole library')));
  const count = el('span', 'hint', ` ${folder.count}`);
  name.appendChild(count);
  if (folder.rule) name.appendChild(el('span', `tag ${folder.rule}`, visibilityWord(folder.rule)));
  row.appendChild(name);

  const mix = el('div', 'folder-mix');
  for (const [key, value] of [['public', folder.public], ['family', folder.family],
    ['hidden', folder.hidden]]) {
    if (!value) continue;
    const chunk = el('i', `mix ${key}`);
    chunk.style.flex = String(value);
    chunk.title = `${value} ${visibilityWord(key)}`;
    mix.appendChild(chunk);
  }
  row.appendChild(mix);

  if (isRoot) {
    row.appendChild(el('div', 'folder-note',
      i18n.t('Set on a folder below, or on the photographs themselves.')));
    return row;
  }

  const picker = el('div', 'vis-picker');
  for (const [value, text] of [['public', i18n.t('Everyone')], ['family', i18n.t('Family')],
    ['hidden', i18n.t('Nobody')]]) {
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

/* -- Library ------------------------------------------------------------- */

async function runScan(full) {
  try {
    await adminApi.rescan(full);
    toast(full ? i18n.t('Full re-index started.') : i18n.t('Scanning for changes…'));
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
    title: options.title || i18n.t('Choose a library folder'),
    cta: options.cta || i18n.t('Use this folder'),
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
  apply.title = apply.disabled ? i18n.t('Pick a folder inside this one, or type a path') : '';
}

async function loadBrowse(path) {
  const list = $('#fm-dirs');
  const loading = el('p', 'hint', i18n.t('Loading…'));
  loading.style.padding = '12px';
  list.replaceChildren(loading);
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
    up.appendChild(el('span', null, i18n.t('.. (up one level)')));
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
        ? i18n.t('No subfolders here — use this folder, or go back up.')
        : i18n.t('No subfolders here.')));
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
  button.textContent = i18n.t('Opening…');
  try {
    const res = await adminApi.setRoot(path);
    $('#folder-modal').hidden = true;
    // Library folders inside the one just added become part of it. The
    // server said which; the screen used to say only "Indexing", and a
    // folder vanishing from the list read as one lost.
    const takenIn = (res?.taken_in || []).map((folder) =>
      String(folder).replace(/[\\/]+$/, '').split(/[\\/]/).pop() || folder);
    toast(takenIn.length
      ? i18n.t('Indexing the library… {folders} now part of this folder, nothing moved or lost.', { folders: takenIn.join(', ') })
      : i18n.t('Indexing the library…'));
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
  box.replaceChildren(el('p', 'hint', i18n.t('Loading…')));
  let data;
  try {
    data = await adminApi.audit();
  } catch (exc) { box.innerHTML = ''; box.appendChild(el('p', 'hint', exc.message)); return; }

  box.innerHTML = '';
  if (!data.entries.length) {
    box.appendChild(el('p', 'hint', i18n.t('Nothing yet.')));
    return;
  }
  const labels = {
    login: i18n.t('signed in'), login_failed: i18n.t('failed sign-in'), enter_profile: i18n.t('entered'),
    create_person: i18n.t('added a profile'), disable_person: i18n.t('disabled a profile'),
    delete_person: i18n.t('deleted a profile'),
    archive_copy: i18n.t('started a consolidation'), archive_dry_run: i18n.t('started a dry run'),
    archive_verify: i18n.t('started an archive audit'), archive_stop: i18n.t('stopped an archive run'),
    archive_adopt: i18n.t('added an archive to the library'),
    archive_reset: i18n.t('cleared the archive report'),
    enable_person: i18n.t('enabled a profile'), reset_password: i18n.t('reset a password'),
    password_changed: i18n.t('changed their password'), set_visibility: i18n.t('changed visibility'),
    set_folder_visibility: i18n.t('changed folder visibility'), settings: i18n.t('changed settings'),
    bootstrap_admin: i18n.t('created the first admin'), signout_person: i18n.t('signed someone out'),
    set_pin: i18n.t('set a PIN'), clear_pin: i18n.t('removed a PIN'),
    handover_plan_saved: i18n.t('saved the handover plan'),
    handover_code_made: i18n.t('made a handover code'),
    handover_requested: i18n.t('asked to take over as administrator'),
    handover_cancelled: i18n.t('stopped a handover'),
    handover_complete: i18n.t('became an administrator by the handover plan'),
    handover_void: i18n.t('handover ended without a change'),
    handover_refused: i18n.t('handover refused'),
    handover_code_wrong: i18n.t('wrong handover code'),
    handover_limited: i18n.t('too many handover attempts'),
  };
  for (const entry of data.entries) {
    const row = el('div', 'activity-row');
    row.appendChild(el('span', 'activity-when',
      new Date(entry.at * 1000).toLocaleString(i18n.locale())));
    row.appendChild(el('span', 'activity-who', entry.display_name || i18n.t('someone')));
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
      : scan.message || i18n.t('Indexing {done} / {total}',
        { done: scan.processed.toLocaleString(), total: scan.total.toLocaleString() });
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

/* The Folders screen shows everything in the library, hidden and explicit
 * included, because it is where an admin goes to check those and act on
 * them: open a file full size, hide it or show it to the family again, flag
 * it explicit or clear the flag, move it to the bin, and hide a whole folder
 * or stop hiding it. Nothing here needs the computer Ninaivu runs on — the
 * file manager was the only way to see a file larger than its tile, and that
 * opened on the Mac, not in front of whoever was looking. */
const foldState = {
  path: '', show: 'all', deep: false, data: null, items: [],
  chosen: new Set(), viewing: -1,
};

// The kinds the Folders screen narrows to, in the server's words. The counts
// on each are for the open folder and everything beneath it.
// Each is [kind, the chip's label, how many of them, what an empty folder says],
// marked here and translated where they are shown.
const FOLD_FILTERS = [
  ['all', i18n.key('All'), i18n.key('{count} files'), i18n.key('This folder has nothing indexed in it.')],
  ['picture', i18n.key('Photos'), i18n.key('{count} photos'), i18n.key('No photos in here.')],
  ['video', i18n.key('Videos'), i18n.key('{count} videos'), i18n.key('No videos in here.')],
  ['audio', i18n.key('Audio'), i18n.key('{count} audio files'), i18n.key('No audio in here.')],
  ['screen', i18n.key('Screenshots & documents'), i18n.key('{count} screenshots and documents'),
    i18n.key('No screenshots or documents in here.')],
  ['hidden', i18n.key('Hidden'), i18n.key('{count} hidden files'), i18n.key('Nothing in here is hidden.')],
  ['explicit', i18n.key('Explicit'), i18n.key('{count} explicit files'),
    i18n.key('Nothing in here is flagged as explicit.')],
];

const FOLDER_ICON =
  '<svg viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/></svg>';

/** Open *path*. With `keep`, read the same folder again as far down as it
 *  had been shown, so an action on page three does not snap back to page one. */
async function loadFolderScreen(path = '', { keep = false } = {}) {
  const same = keep && path === foldState.path;
  const wanted = same ? Math.max(foldState.items.length, 1) : 1;
  let data;
  const items = [];
  try {
    do {
      // eslint-disable-next-line no-await-in-loop
      data = await adminApi.folder(path || '', foldState.show, foldState.deep, items.length);
      items.push(...(data.items || []));
    } while (items.length < wanted && items.length < (data.items_total || 0)
             && (data.items || []).length);
  } catch (exc) {
    toast(exc.message, true);
    return;
  }
  foldState.path = path || '';
  foldState.data = data;
  foldState.items = items;
  const ids = new Set(items.map((item) => item.id));
  foldState.chosen = new Set(same ? [...foldState.chosen].filter((id) => ids.has(id)) : []);
  renderFolderScreen();
}

async function loadMoreFiles() {
  const button = $('#fold-more');
  button.disabled = true;
  try {
    const data = await adminApi.folder(foldState.path, foldState.show, foldState.deep,
      foldState.items.length);
    foldState.items.push(...(data.items || []));
    foldState.data = { ...data, items: foldState.items };
    renderFolderScreen();
  } catch (exc) {
    toast(exc.message, true);
  } finally {
    button.disabled = false;
  }
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
  renderFolderRule(data);
  const filter = FOLD_FILTERS.find(([key]) => key === (data.show || 'all')) || FOLD_FILTERS[0];

  /* what is in here, with the hidden and explicit counts called out */
  const counts = data.counts || {};
  const total = (data.children || []).reduce((sum, c) => sum + c.n, 0) + (counts.n || 0);
  const hidden = data.total_hidden || 0;
  const explicit = data.total_explicit || 0;
  const summary = $('#fold-summary');
  summary.innerHTML = '';
  summary.append(
    span(i18n.t('{count} folders', { count: (data.children || []).length.toLocaleString() })),
    span(i18n.t(filter[2], { count: total.toLocaleString() })),
  );
  if (hidden) {
    const mark = el('span', 'hidden-n');
    mark.append(el('span', 'n', hidden.toLocaleString()),
                document.createTextNode(` ${i18n.t('only admins can see')}`));
    summary.appendChild(mark);
  } else {
    summary.append(span(i18n.t('nothing hidden in here')));
  }
  if (explicit) {
    const mark = el('span', 'explicit-n');
    mark.append(el('span', 'n', explicit.toLocaleString()),
                document.createTextNode(` ${i18n.t('flagged explicit')}`));
    summary.appendChild(mark);
  }

  /* subfolders */
  const grid = $('#fold-children');
  grid.innerHTML = '';
  const children = data.children || [];
  for (const child of children) grid.appendChild(folderCard(child));

  /* the files: directly in this folder, or everything beneath it */
  const items = foldState.items;
  const shown = items.length;
  const totalItems = data.items_total ?? shown;
  const head = $('#fold-items-head');
  head.hidden = shown === 0;
  if (shown) {
    const where = data.deep ? i18n.t('in here and every folder inside')
      : '';
    $('#fold-items-title').textContent = totalItems === 1 ? i18n.t('1 file here')
      : i18n.t('{count} files here', { count: totalItems.toLocaleString() });
    const hiddenHere = items.filter((i) => i.visibility_name === 'hidden').length;
    const notes = [];
    if (where) notes.push(where);
    if (shown < totalItems) {
      notes.push(i18n.t('showing {shown} of {total}',
        { shown: shown.toLocaleString(), total: totalItems.toLocaleString() }));
    }
    if (hiddenHere) {
      notes.push(i18n.t('{count} of them are admin-only', { count: hiddenHere.toLocaleString() }));
    }
    $('#fold-items-note').textContent = notes.join(' · ');
  }
  const list = $('#fold-items');
  list.innerHTML = '';
  items.forEach((item, index) => list.appendChild(folderItem(item, index)));
  const more = $('#fold-more');
  more.hidden = shown >= totalItems;
  more.textContent = i18n.t('Show {count} more', {
    count: Math.min(data.page || 200, totalItems - shown).toLocaleString() });

  const empty = $('#fold-empty');
  if (!children.length && !items.length) {
    empty.textContent = i18n.t(filter[3]);
    empty.hidden = false;
  } else {
    empty.hidden = true;
  }
  renderFoldPick();
}

function renderFolderFilters(data) {
  const box = $('#fold-filters');
  if (!box) return;
  box.innerHTML = '';
  const kinds = data.kinds || {};
  const current = data.show || 'all';
  for (const [key, label] of FOLD_FILTERS) {
    // Explicit is only worth a chip when something was flagged.
    if (key === 'explicit' && !kinds.explicit && current !== 'explicit') continue;
    const chip = el('button', `chip${key === current ? ' active' : ''}`);
    if (key === 'hidden' || key === 'explicit') chip.classList.add(`chip-${key}`);
    chip.type = 'button';
    chip.setAttribute('aria-pressed', String(key === current));
    chip.append(document.createTextNode(i18n.t(label)),
      el('span', 'n', (kinds[key] || 0).toLocaleString()));
    chip.onclick = () => {
      if (foldState.show === key) return;
      foldState.show = key;
      // Hidden and explicit files are found across a whole year at a time,
      // not one day's folder after another.
      if ((key === 'hidden' || key === 'explicit') && !foldState.deep) {
        foldState.deep = true;
        $('#fold-deep').checked = true;
      }
      loadFolderScreen(foldState.path);
    };
    box.appendChild(chip);
  }
}

/* Who can see this folder, and the one button that changes it: hide it, or
 * stop hiding it. A folder hidden because a folder above it is says which,
 * and opens it, since that is where the rule can be taken off. */
function renderFolderRule(data) {
  const bar = $('#fold-rule');
  bar.replaceChildren();
  bar.className = 'fold-rule';
  const rule = data.rule;
  const here = Boolean(rule) && rule.at === data.folder;
  const hidden = Boolean(rule) && rule.visibility === 2;
  if (hidden) bar.classList.add('is-hidden');
  const text = el('div', 'fold-rule-text');
  if (!data.folder) {
    text.append(el('strong', '', i18n.t('The whole library')),
      el('span', 'hint', i18n.t('Open a folder to hide it, or choose files below.')));
    bar.appendChild(text);
    if (data.local) bar.appendChild(el('div', 'fold-rule-actions')).appendChild(revealFolderButton(data));
    return;
  }
  const level = rule ? visibilityWord(VIS_NAMES_BY_VALUE[rule.visibility]) : '';
  if (!rule) {
    text.append(el('strong', '', i18n.t('Not hidden')),
      el('span', 'hint', i18n.t('This folder follows the family rules. Hiding it keeps everything in it, and anything added later, for admins only.')));
  } else if (here) {
    text.append(el('strong', '', hidden ? i18n.t('Hidden folder')
      : i18n.t('Folder rule: {level}', { level })),
    el('span', 'hint', hidden
      ? i18n.t('Only admins see what is in here, including anything added later.')
      : i18n.t('Set on this folder.')));
  } else {
    text.append(el('strong', '', hidden ? i18n.t('Hidden by “{folder}”', { folder: rule.at })
      : i18n.t('Folder rule: {level} (from “{folder}”)', { level, folder: rule.at })),
    el('span', 'hint', i18n.t('The rule is set on a folder above this one.')));
  }
  bar.appendChild(text);

  const actions = el('div', 'fold-rule-actions');
  if (data.local) actions.appendChild(revealFolderButton(data));
  if (rule && !here) {
    const open = el('button', 'btn ghost small', i18n.t('Open “{folder}”', { folder: rule.at.split('/').pop() }));
    open.type = 'button';
    open.onclick = () => loadFolderScreen(rule.at);
    actions.appendChild(open);
  }
  if (here) {
    const clear = el('button', 'btn small', hidden ? i18n.t('Stop hiding this folder')
      : i18n.t('Remove this folder rule'));
    clear.type = 'button';
    clear.id = 'fold-unhide-folder';
    clear.onclick = () => clearFolderRule(data.folder);
    actions.appendChild(clear);
  }
  if (!hidden) {
    const hide = el('button', 'btn danger small', i18n.t('Hide this folder'));
    hide.type = 'button';
    hide.id = 'fold-hide-folder';
    hide.onclick = async () => {
      await applyFolderVisibility(data.folder, 'hidden');
      await loadFolderScreen(foldState.path, { keep: true });
    };
    actions.appendChild(hide);
  }
  bar.appendChild(actions);
}

/** At the Ninaivu computer: open this folder in Finder or Explorer, where
 *  the files really are. Elsewhere the button is not shown at all. */
function revealFolderButton(data) {
  const button = el('button', 'btn ghost small', i18n.t('Show in Finder or Explorer'));
  button.type = 'button';
  button.id = 'fold-reveal-folder';
  button.title = data.path || '';
  button.onclick = async () => {
    try {
      await adminApi.revealFolder(data.folder);
    } catch (exc) { toast(exc.message, true); }
  };
  return button;
}

/** "Stop hiding": the rule comes off and its files follow whatever covers
 *  them without it. Showing anything to more people is asked about first. */
async function clearFolderRule(path) {
  try {
    let result;
    try {
      result = await adminApi.clearFolderVisibility(path, false);
    } catch (exc) {
      if (!(exc.status === 409 && exc.data?.needs_confirmation)) throw exc;
      if (!await confirmExposure(exc.data.impact, path, null)) return;
      result = await adminApi.clearFolderVisibility(path, true);
    }
    toast(i18n.t('“{folder}” is no longer hidden. {items} changed.',
      { folder: path, items: i18n.items(result.updated) }));
    state.overview = await adminApi.overview();
    renderOverview();
    await loadFolders();
    await refreshUndo();
    await loadFolderScreen(foldState.path, { keep: true });
  } catch (exc) { toast(exc.message, true); }
}

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
  if (child.rule === 'hidden') cover.appendChild(el('span', 'fold-flag', i18n.t('Hidden folder')));
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
  meta.appendChild(span(i18n.t('{count} files', { count: child.n.toLocaleString() })));
  if (child.bytes) meta.appendChild(span(bytesShort(child.bytes)));
  if (child.hidden) {
    meta.appendChild(el('span', 'hid', i18n.t('{count} hidden', { count: child.hidden.toLocaleString() })));
  }
  if (child.explicit) {
    meta.appendChild(el('span', 'xp', i18n.t('{count} explicit', { count: child.explicit.toLocaleString() })));
  }
  body.appendChild(meta);

  card.appendChild(body);
  card.title = child.hidden
    ? `${child.path} — ${i18n.t('{hidden} of {total} visible to admins only', { hidden: child.hidden, total: child.n })}`
    : child.path;
  return card;
}

const VIS_WHY = {
  hidden: i18n.key('from disk'),
  folder: i18n.key('folder rule'),
  item: i18n.key('set here'),
  screen: i18n.key('screenshot or document'),
  private: i18n.key('only to its owner'),
  screening: i18n.key('held while an admin screens it'),
  kind: i18n.key('audio stays admin-only'),
  default: '',
};

function folderItem(item, index) {
  const tile = el('div', 'fold-item');
  tile.dataset.id = item.id;
  const isHidden = item.visibility_name === 'hidden';
  if (isHidden) tile.classList.add('is-hidden');
  if (item.explicit) tile.classList.add('is-explicit');
  if (foldState.chosen.has(item.id)) tile.classList.add('chosen');

  const open = el('button', 'fold-open');
  open.type = 'button';
  open.setAttribute('aria-label', i18n.t('Open {name}', { name: item.filename }));
  if (item.thumb) {
    const img = document.createElement('img');
    img.src = thumbUrl(item.id, 256, item.thumb_v);
    img.alt = '';
    img.loading = 'lazy';
    open.appendChild(img);
  } else {
    open.appendChild(el('span', 'fold-nothumb', (item.kind || '').toUpperCase()));
  }
  if (item.kind === 'video') open.appendChild(el('span', 'fold-play', '▶'));
  open.onclick = () => openFoldViewer(index);
  tile.appendChild(open);

  const pick = document.createElement('input');
  pick.type = 'checkbox';
  pick.className = 'fold-pick';
  pick.checked = foldState.chosen.has(item.id);
  pick.setAttribute('aria-label', i18n.t('Choose {name}', { name: item.filename }));
  pick.onchange = () => {
    if (pick.checked) foldState.chosen.add(item.id); else foldState.chosen.delete(item.id);
    tile.classList.toggle('chosen', pick.checked);
    renderFoldPick();
  };
  tile.appendChild(pick);

  const badges = el('div', 'fold-badges');
  if (isHidden) badges.appendChild(el('span', 'badge', i18n.t('Hidden')));
  if (item.explicit) badges.appendChild(el('span', 'badge xp', i18n.t('Explicit')));
  if (badges.childElementCount) tile.appendChild(badges);

  const caption = el('div', 'name', item.filename);
  tile.appendChild(caption);
  if (foldState.deep && item.folder !== foldState.path) {
    tile.appendChild(el('div', 'where', item.folder || i18n.t('the library')));
  }
  const why = isHidden ? VIS_WHY[item.vis_source] : '';
  if (why) tile.appendChild(el('div', 'why', i18n.t(why)));
  tile.title = `${item.filename} — ${visibilityWord(item.visibility_name)}`;
  return tile;
}

/* -- choosing files, and what can be done with them ------------------------ */

function foldChosenItems() {
  return foldState.items.filter((item) => foldState.chosen.has(item.id));
}

function renderFoldPick() {
  const bar = $('#fold-pickbar');
  const chosen = foldChosenItems();
  bar.hidden = !foldState.items.length;
  $('#fold-picked').textContent = chosen.length
    ? plural(chosen.length, i18n.key('1 file chosen'), i18n.key('{count} files chosen'))
    : i18n.t('Nothing chosen');
  for (const id of ['#fold-act-hide', '#fold-act-show', '#fold-act-explicit',
    '#fold-act-clean', '#fold-act-delete', '#fold-act-none']) {
    $(id).disabled = !chosen.length;
  }
  const all = $('#fold-act-all');
  all.textContent = chosen.length === foldState.items.length && chosen.length
    ? i18n.t('Choose none') : i18n.t('Choose all');
}

function foldChooseAll() {
  if (foldState.chosen.size === foldState.items.length) foldState.chosen.clear();
  else foldState.chosen = new Set(foldState.items.map((item) => item.id));
  renderFolderScreen();
}

/** Hide (admins only) or show to the family again. */
async function foldSetVisibility(items, level) {
  if (!items.length) return;
  try {
    const result = await adminApi.setItemsVisibility(items.map((item) => item.id), level);
    const still = items.filter((item) => item.private).length;
    let message = level === 'hidden'
      ? i18n.t('{items} hidden. Only admins can see them now.', { items: i18n.items(items.length) })
      : i18n.t('{items} shown to the family again.', { items: i18n.items(items.length) });
    if (result.note) message += ` ${i18n.fromServer({ error: result.note }, result.note)}`;
    if (level !== 'hidden' && still) {
      message += ` ${i18n.t('Files somebody keeps “Only to me” stay hidden until they let them go.')}`;
    }
    toast(message);
  } catch (exc) {
    toast(exc.message, true);
    return;
  }
  await afterFoldChange();
}

/** Flag as explicit, or clear the flag. A flag set by hand is never put back
 *  by the model on a later pass. */
async function foldSetExplicit(items, explicit) {
  if (!items.length) return;
  try {
    await adminApi.setExplicit(items.map((item) => item.id), explicit);
    toast(explicit
      ? i18n.t('{items} flagged explicit. Only admins can see them.', { items: i18n.items(items.length) })
      : i18n.t('{items} no longer flagged explicit.', { items: i18n.items(items.length) }));
  } catch (exc) {
    toast(exc.message, true);
    return;
  }
  await afterFoldChange();
}

async function afterFoldChange() {
  await loadFolderScreen(foldState.path, { keep: true });
  if (foldState.viewing >= 0) showFoldViewerItem();
  refreshUndo();
}

/* -- the viewer: a file full size, and the same actions on it -------------- */

function openFoldViewer(index) {
  foldState.viewing = index;
  const modal = $('#fold-viewer');
  modal.hidden = false;
  showFoldViewerItem();
  $('#fv-close').focus();
}

function closeFoldViewer() {
  const modal = $('#fold-viewer');
  modal.hidden = true;
  foldState.viewing = -1;
  $('#fv-stage').replaceChildren();          // stops a video that was playing
}

function stepFoldViewer(by) {
  const next = foldState.viewing + by;
  if (next < 0 || next >= foldState.items.length) return;
  foldState.viewing = next;
  showFoldViewerItem();
}

function showFoldViewerItem() {
  const item = foldState.items[foldState.viewing];
  if (!item) { closeFoldViewer(); return; }
  const stage = $('#fv-stage');
  stage.replaceChildren();
  if (item.kind === 'video' || item.kind === 'audio') {
    foldPlay(stage, item);
  } else {
    const img = document.createElement('img');
    img.alt = item.filename;
    // The thumbnail first, so there is something to look at at once.
    if (item.thumb) img.src = thumbUrl(item.id, 1024, item.thumb_v);
    const full = new Image();
    full.onload = () => { if (foldState.items[foldState.viewing] === item) img.src = full.src; };
    full.src = `/api/preview/${item.id}`;
    if (item.rotation) img.style.transform = `rotate(${item.rotation}deg)`;
    stage.appendChild(img);
  }

  $('#fv-name').textContent = item.filename;
  // At the Ninaivu computer, where the file really is; elsewhere, its folder.
  const where = foldState.data?.local && item.path ? item.path : (item.folder || i18n.t('the library'));
  const facts = [where, visibilityWord(item.visibility_name)];
  if (item.size) facts.push(bytesShort(item.size));
  if (item.date_key) facts.push(item.date_key);
  $('#fv-facts').textContent = facts.join(' · ');
  $('#fv-count').textContent = `${foldState.viewing + 1} / ${foldState.items.length}`;
  $('#fv-prev').disabled = foldState.viewing <= 0;
  $('#fv-next').disabled = foldState.viewing >= foldState.items.length - 1;

  const badges = $('#fv-badges');
  badges.replaceChildren();
  if (item.visibility_name === 'hidden') {
    badges.appendChild(el('span', 'badge', i18n.t('Hidden')));
    const why = VIS_WHY[item.vis_source];
    if (why) badges.appendChild(el('span', 'hint', i18n.t(why)));
  }
  if (item.explicit) badges.appendChild(el('span', 'badge xp', i18n.t('Explicit')));

  const isHidden = item.visibility_name === 'hidden';
  const vis = $('#fv-visibility');
  vis.textContent = isHidden ? i18n.t('Show to family') : i18n.t('Hide');
  vis.className = isHidden ? 'btn small' : 'btn danger small';
  vis.onclick = () => foldSetVisibility([item], isHidden ? 'family' : 'hidden');
  const flag = $('#fv-explicit');
  flag.textContent = item.explicit ? i18n.t('Not explicit') : i18n.t('Mark explicit');
  flag.onclick = () => foldSetExplicit([item], !item.explicit);
  $('#fv-delete').onclick = () => foldDelete([item]);
  $('#fv-download').href = `/api/download/${item.id}`;
  $('#fv-download').dataset.size = item.size || 0;
  const reveal = $('#fv-reveal');
  reveal.hidden = !foldState.data?.local;
  reveal.onclick = () => revealItem(item);
  $('#fv-date').replaceChildren(creationDateControl(item));
}

/** Play a video or a recording in the viewer. What a browser opens (MP4,
 *  MOV, WebM, MP3…) plays from the file itself, with seeking. What it does
 *  not (3GP, MKV, AVI, WMV and the like) plays from a copy Ninaivu converts
 *  once and keeps, the same copy the family app's viewer uses; so does a
 *  file the browser gives up on, such as HEVC in a MOV outside Safari. */
function foldPlay(stage, item) {
  if (item.playable && !item.needs_proxy) {
    const media = document.createElement(item.kind === 'audio' ? 'audio' : 'video');
    media.src = `/api/file/${item.id}`;
    media.controls = true;
    media.preload = 'metadata';
    media.playsInline = true;
    if (item.kind === 'video' && item.thumb) media.poster = thumbUrl(item.id, 1024, item.thumb_v);
    media.onerror = () => {
      if (foldState.items[foldState.viewing] !== item) return;
      stage.replaceChildren();
      foldPlayConverted(stage, item);
    };
    stage.appendChild(media);
    return;
  }
  foldPlayConverted(stage, item);
}

async function foldPlayConverted(stage, item) {
  const url = `/api/proxy/${item.id}`;
  const panel = el('div', 'fv-wait');
  if (item.thumb) {
    const still = document.createElement('img');
    still.src = thumbUrl(item.id, 1024, item.thumb_v);
    still.alt = '';
    panel.appendChild(still);
  }
  const note = el('div', 'fv-wait-note');
  const heading = el('strong', '', i18n.t('Making a copy this browser can play'));
  const detail = el('span', 'hint',
    i18n.t('{format} files do not play in a browser, so Ninaivu converts a copy once. The original is not changed.',
      { format: (item.filename.split('.').pop() || '').toUpperCase() }));
  const bar = el('div', 'fv-wait-bar');
  const fill = document.createElement('i');
  bar.appendChild(fill);
  note.append(heading, detail, bar);
  panel.appendChild(note);
  stage.appendChild(panel);

  const gone = () => foldState.items[foldState.viewing] !== item || !panel.isConnected;
  for (let attempt = 0; attempt < 1800; attempt += 1) {
    if (gone()) return;
    let response;
    try {
      // eslint-disable-next-line no-await-in-loop
      response = await fetch(url, { headers: { Accept: 'application/json' } });
    } catch {
      heading.textContent = i18n.t('Could not reach the server');
      bar.remove();
      return;
    }
    if (gone()) return;
    if (response.ok) {
      const media = document.createElement(item.kind === 'audio' ? 'audio' : 'video');
      media.src = url;
      media.controls = true;
      media.autoplay = true;
      media.playsInline = true;
      stage.replaceChildren(media);
      return;
    }
    let payload = {};
    // eslint-disable-next-line no-await-in-loop
    try { payload = await response.json(); } catch { /* not JSON */ }
    if (response.status !== 202) {
      heading.textContent = i18n.t('This file cannot be played here');
      detail.textContent = `${i18n.fromServer(payload, payload.message || '')} ${
        i18n.t('Download it to check it on this computer.')}`.trim();
      bar.remove();
      return;
    }
    fill.style.width = `${Math.max(2, payload.percent || 0)}%`;
    // eslint-disable-next-line no-await-in-loop
    await new Promise((resolve) => { setTimeout(resolve, 1000); });
  }
  bar.remove();
}

/** Move files to the library's bin: one confirmation, no password (they
 *  can be put back from Recently deleted, just below), one request. */
function foldDelete(items) {
  if (!items.length) return;
  askPasswordToDelete(items, async (result) => {
    for (const item of items) foldState.chosen.delete(item.id);
    const failed = (result.failed || []).length;
    if (failed) {
      toast(i18n.t('{count} could not be moved to the bin.', { count: failed }), true);
    } else {
      toast(items.length === 1
        ? i18n.t('“{name}” moved to the bin.', { name: items[0].filename })
        : i18n.t('{count} files moved to the bin.', { count: items.length }));
    }
    if (foldState.viewing >= 0 && foldState.viewing >= foldState.items.length - items.length) {
      foldState.viewing = Math.max(0, foldState.items.length - items.length - 1);
    }
    await loadFolderScreen(foldState.path, { keep: true });
    if (!foldState.items.length) closeFoldViewer();
    else if (foldState.viewing >= 0) showFoldViewerItem();
    openPage('recycle-bin').then((page) => page?.load());
  }, { password: false });
}

function wireFolderScreen() {
  $$('#fold-deep').onchange = (event) => {
    foldState.deep = event.target.checked;
    loadFolderScreen(foldState.path);
  };
  $$('#fold-more').onclick = loadMoreFiles;
  $$('#fold-act-all').onclick = foldChooseAll;
  $$('#fold-act-none').onclick = () => { foldState.chosen.clear(); renderFolderScreen(); };
  $$('#fold-act-hide').onclick = () => foldSetVisibility(foldChosenItems(), 'hidden');
  $$('#fold-act-show').onclick = () => foldSetVisibility(foldChosenItems(), 'family');
  $$('#fold-act-explicit').onclick = () => foldSetExplicit(foldChosenItems(), true);
  $$('#fold-act-clean').onclick = () => foldSetExplicit(foldChosenItems(), false);
  $$('#fold-act-delete').onclick = () => foldDelete(foldChosenItems());
  $$('#fv-close').onclick = closeFoldViewer;
  // On an iPhone a plain download opened over the console with no way back.
  keepInApp($$('#fv-download'), { toast });
  $$('#fv-prev').onclick = () => stepFoldViewer(-1);
  $$('#fv-next').onclick = () => stepFoldViewer(1);
  $$('#fold-viewer').addEventListener('keydown', (event) => {
    if (event.target.closest('input, textarea, select')) return;
    if (event.key === 'Escape') closeFoldViewer();
    if (event.key === 'ArrowLeft') stepFoldViewer(-1);
    if (event.key === 'ArrowRight') stepFoldViewer(1);
  });
}

let uploadPoll;
const seenUploads = new Set();
// The uploads queue as last fetched, and whether an approval is under way;
// the Uploads page's module (pages/console/uploads.js) draws the one and sets
// the other.
const uploads = { last: null, busy: false };

let uploadTotal = null;

async function loadPendingUploads() {
  if (uploads.busy) return;
  try {
    const data = await admin.getAdminUploads();
    uploads.last = data;
    // The tab's count and the section mark are loadAttention's, which counts
    // every queue; asked again when this one has moved, so an approved
    // upload takes its number and its dot with it.
    if (uploadTotal !== null && uploadTotal !== data.total) loadAttention();
    uploadTotal = data.total;
    const incoming = data.items.filter((item) => !seenUploads.has(item.id));
    for (const item of data.items) seenUploads.add(item.id);
    if (incoming.length) {
      toast(incoming.length === 1 ? i18n.t('1 family upload awaiting approval. Open Uploads to review.')
        : i18n.t('{count} family uploads awaiting approval. Open Uploads to review.', { count: incoming.length }));
    }
    ifOpened('uploads', (page) => page.render(data));
  } catch (exc) {
    if (exc.status !== 401) console.debug('Upload review refresh unavailable', exc.message);
  }
}

async function renderDatePolicy(container) {
  const block = el('div', 'setting-field');
  container.appendChild(block);
  block.appendChild(el('h3', null, i18n.t('Family app date visibility')));
  block.appendChild(el('p', 'hint', i18n.t('Calendar albums use file creation dates. A named album uses its earliest dated file (excluding the bin); changing a file date can change its album date. Its contents must also match the date range. Unknown dates are hidden unless All dates is selected. Admin management always shows every date.')));
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
    grid.appendChild(labelled(i18n.t('Cutoff date'), cutoff));
    const inputs = {};
    for (const [role, title] of [['admin', i18n.t('Admins in the family app')], ['family', i18n.t('Family members')], ['guest', i18n.t('Guests')]]) {
      const select = el('select', 'select');
      for (const [value, label] of [['before', i18n.t('Before cutoff')], ['after', i18n.t('On or after cutoff')], ['all', i18n.t('All dates')]]) {
        const option = el('option', null, label);
        option.value = value;
        option.selected = policy[role] === value;
        select.appendChild(option);
      }
      inputs[role] = select;
      grid.appendChild(labelled(title, select));
    }
    const save = el('button', 'btn', i18n.t('Save date visibility'));
    save.type = 'button';
    save.onclick = async () => {
      if (!cutoff.reportValidity()) return;
      save.disabled = true;
      try {
        await json('/api/admin/date-policy', { method: 'POST', body: {
          cutoff: cutoff.value, ...Object.fromEntries(Object.entries(inputs).map(([role, input]) => [role, input.value])),
        } });
        toast(i18n.t('Date visibility saved. Refresh the family app to see updated albums.'));
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
  row.appendChild(labelled(i18n.t('Creation date'), input));
  const save = el('button', 'btn small', i18n.t('Save date and move'));
  save.type = 'button';
  save.onclick = async (event) => {
    event.stopPropagation();
    if (!input.reportValidity()) return;
    save.disabled = true;
    try {
      const result = await json(`/api/admin/assets/${item.id}/creation-date`, {
        method: 'PATCH', body: { creation_date: input.value },
      });
      toast(i18n.t('Saved. File is now in {folder}.', { folder: result.item.folder }));
      await loadFolderScreen(foldState.path);
    } catch (exc) { toast(exc.message, true); }
    finally { save.disabled = false; }
  };
  row.appendChild(save);
  row.appendChild(el('p', 'hint', i18n.t('Moves to year/month/day. Saved at midnight UTC; original embedded metadata is preserved.')));
  return row;
}

/* --- bulk visibility: ask twice, and keep the way back ------------------ */

const VIS_WORDS = { public: i18n.key('everyone, guests included'),
                    family: i18n.key('family members and admins'),
                    hidden: i18n.key('admins only') };

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
    toast(i18n.t('{items} → {level}.', { items: i18n.items(result.updated), level: visibilityWord(value) }));
    state.overview = await adminApi.overview();
    renderOverview();
    await loadFolders();
    await refreshUndo();
    renderPreview(currentPreview);
  } catch (exc) { toast(exc.message, true); }
}

function confirmExposure(impact, path, value) {
  return new Promise((resolve) => {
    const where = path ? `“${path}”` : i18n.t('the whole library');
    $('#expose-what').replaceChildren(
      ...(value === null
        ? emphasised(i18n.key('Stopping hiding {where} lets the family see what is in it again.'),
          { where: strong(where) })
        : emphasised(i18n.key('Setting {where} to {level} makes it visible to {who}.'), {
          where: strong(where),
          level: strong(visibilityWord(value)),
          who: VIS_WORDS[value] ? i18n.t(VIS_WORDS[value]) : value,
        })),
      ' ',
      ...emphasised(impact.exposed === 1
        ? i18n.key('{count} file is currently more private than that, and would become visible.')
        : i18n.key('{count} files are currently more private than that, and would become visible.'),
      { count: strong(Number(impact.exposed || 0).toLocaleString()) }),
    );

    // Name *why* they are private, because "I hid those myself" and "the
    // filesystem hid those" are different decisions to be second-guessing.
    const notes = [];
    if (impact.decided_individually) {
      notes.push(plural(impact.decided_individually,
        i18n.key('1 file was set individually and would be overwritten'),
        i18n.key('{count} files were set individually and would be overwritten')));
    }
    if (impact.hidden_by_the_filesystem) {
      notes.push(plural(impact.hidden_by_the_filesystem,
        i18n.key('1 file is hidden because of where it sits on disk'),
        i18n.key('{count} files are hidden because of where they sit on disk')));
    }
    if (impact.child_rules) {
      notes.push(plural(impact.child_rules,
        i18n.key('1 folder rule inside it would be overridden'),
        i18n.key('{count} folder rules inside it would be overridden')));
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
      ? (last.folder ? `“${last.folder}”` : i18n.t('the whole library'))
      : i18n.items(last.affected);
    const level = VIS_NAMES_BY_VALUE[last.visibility];
    $('#vis-undo-what').textContent = i18n.t('Last change: {where} → {level}',
      { where, level: level ? visibilityWord(level) : last.visibility });
    $('#vis-undo-detail').textContent = last.exposed
      ? `${plural(last.exposed, i18n.key('1 file became visible to more people.'),
        i18n.key('{count} files became visible to more people.'))} ${
        i18n.t('Undo puts every file back exactly as it was.')}`
      : plural(last.restorable, i18n.key('1 file can be put back exactly as it was.'),
        i18n.key('{count} files can be put back exactly as they were.'));
    strip.hidden = false;
  } catch { strip.hidden = true; }
}

const VIS_NAMES_BY_VALUE = { 0: 'public', 1: 'family', 2: 'hidden' };

async function undoLastVisibility() {
  try {
    const result = await adminApi.undoVisibility(null);
    toast(plural(result.restored, i18n.key('Put 1 file back as it was.'),
      i18n.key('Put {count} files back as they were.')));
    state.overview = await adminApi.overview();
    renderOverview();
    await loadFolders();
    await refreshUndo();
    renderPreview(currentPreview);
  } catch (exc) { toast(exc.message, true); }
}


/* ── Ninaivu 5.0 Bitrot Scrubber ────────────────────────────────────────── */

// The notification settings, asked for at sign-in and drawn by the Health
// page's module (pages/console/notifications.js) once it is opened.
let lastNotifications = null;

// Runs from start(), not wireChrome(): the console wires itself before anyone
// has signed in, and asking then was a 401 that announced "Your session has
// ended" to someone who never had one, and left this list empty afterwards.
async function loadNotifications() {
  try {
    lastNotifications = await admin.getAdminNotifications();
  } catch {
    return;                       // not an admin, or the console is older
  }
  ifOpened('notifications', (page) => page.render(lastNotifications));
}

function wireScrubber() {
  const startBtn = $('#scrubber-start-btn');
  startBtn?.addEventListener('click', async () => {
    startBtn.disabled = true;
    try {
      const res = await admin.postAdminScrubberStart();
      // The server's two answers are fixed sentences, so they are said here in
      // the console's language rather than repeated in English.
      toast(res.ok === false ? i18n.t('Scrubber is already running') : i18n.t('Scrubber started'));
      pollScrubber();
    } catch (err) {
      toast(i18n.t('Failed to start scrubber: {reason}', { reason: err.message }), true);
      startBtn.disabled = false;
    }
  });
  const stopBtn = $('#scrubber-stop-btn');
  stopBtn?.addEventListener('click', async () => {
    stopBtn.disabled = true;
    try {
      await admin.postAdminScrubberStop();
      toast(i18n.t('The storage check stops after the file it is reading. Check every file carries on from there.'));
    } catch (err) {
      toast(err.message, true);
    }
    pollScrubber();
  });
}

// The storage check's status, asked for at sign-in and every two seconds
// while a check runs; drawn by the Health page's module
// (pages/console/scrubber.js) once it is opened.
let lastScrub;
let scrubberPollTimer = null;
async function pollScrubber() {
  clearTimeout(scrubberPollTimer);
  let data;
  try {
    data = await admin.getAdminScrubberStatus();
  } catch (err) {
    console.debug('Scrubber status fetch note:', err);
  }
  lastScrub = data;
  ifOpened('scrubber', (page) => page.show(data));
  if (data?.progress?.running) scrubberPollTimer = setTimeout(pollScrubber, 2000);
}

/* ======================================================================== */

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
