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
import { CloudPanel } from './cloud.js';
import { WorkloadPanel } from './workload.js';
import { JobStatusPanel } from './job-status.js';
import { DiskPanel } from './disks.js';
import { VideoTrial } from './video-trial.js';
import { JournalPanel } from './journal.js';
import { SafetyPanel } from './safety.js';
import { WeekPanel } from './health-week.js';
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
import { labelled } from './pages/console/common.js';
import * as i18n from './i18n.js';
import { consoleCommands, initPalette } from './palette.js';
import { hideScrollBarsUntilUsed } from './scrollbars.js';
import { consoleState as state } from './store.js';
import { $, $$, consoleToast as toast, el } from './ui.js';
import { installDialogLayer, registerDialog } from './dialogs.js';

const adminApi = {
  overview: () => json('/api/admin/overview'),
  preview: (query) => json(`/api/admin/preview?${new URLSearchParams(query)}`),
  settings: (body) => json('/api/admin/settings', { method: 'POST', body }),
  attention: () => json('/api/admin/attention'),
  extensions: () => json('/api/admin/extensions'),
  audit: () => json('/api/audit'),
  browse: (path) => json(`/api/library/browse?path=${encodeURIComponent(path || '')}`),
  devices: () => json('/api/devices'),
  deviceBrowse: (path) => json(`/api/devices/browse?path=${encodeURIComponent(path || '')}`),
  setRoot: (path) => json('/api/library/root', { method: 'POST', body: { path } }),
  rescan: (full) => json('/api/scan', { method: 'POST', body: { full } }),
  libraries: () => json('/api/admin/libraries'),
  assignable: () => json('/api/admin/assignable'),
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
let weekPanel;
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
  installDialogLayer();
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
  loadActiveExtensions();
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
  weekPanel?.load();
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
  await Promise.all([loadPeople(), loadAssignable()]);
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
    ifOpened('visibility', (page) => page.render());
    renderPreview(currentPreview);
  }
  loadAttention();
  ifOpened('folders', (page) => page.render());
  ifOpened('recycle-bin', (page) => page.render());
  ifOpened('library', (page) => page.relabel());
  ifOpened('visibility', (page) => page.refreshUndo());
  // The uploads list is only rebuilt when the queue changes; forget what it
  // last drew so the next look draws it again in the new language.
  ifOpened('uploads', (page) => page.relabel());
  if (current === 'uploads') { loadPendingUploads(); openPage('uploads').then((page) => page?.show()); }
  if (current === 'server') openPage('uploads').then((page) => page?.loadTvAlbum());
  if (current === 'large-files') openPage('large-files').then((page) => page?.show());
  if (current === 'health') openPage('health').then((page) => page?.show());
  if (current === 'activity') loadActivity();
  if (current === 'settings' || current === 'ai-models') {
    openPage('extensions').then((page) => page?.show());
  }
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
  weekPanel = new WeekPanel({ toast, openPage: (page) => showTab(page) });
  weekPanel.wire();

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
  wireScrubber();
  document.querySelectorAll('.modal, .sheet').forEach(node => {
    registerDialog(node, { escape: true, backdrop: true });
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
  // The Folders page, and its section the recycle bin, opened with it.
  folders: () => import('./pages/console/folders.js'),
  // Who sees which folder, the ask-twice and the undo; the folder screen
  // asks it too.
  visibility: () => import('./pages/console/visibility.js'),
  // The extensions and the Gemini key, on Home & extensions and AI models.
  extensions: () => import('./pages/console/extensions.js'),
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
  openPage: (name) => openPage(name),
  setActiveExtensions: (names) => setActiveExtensions(names),
  // Folder visibility, ask-twice and undo: the Visibility page's, used by
  // the folder screen too.
  applyFolderVisibility: (path, value) =>
    openPage('visibility').then((page) => page?.applyFolderVisibility(path, value)),
  confirmExposure: (impact, path, value) =>
    openPage('visibility').then((page) => (page ? page.confirmExposure(impact, path, value) : false)),
  refreshUndo: () => openPage('visibility').then((page) => page?.refreshUndo()),
  visibilityChanged: () => openPage('visibility').then((page) => page?.show()),
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
  if (name === 'overview') { safetyPanel?.load(); weekPanel?.load(); }
  if (name === 'uploads') { loadPendingUploads(); openPage('uploads').then((page) => page?.show()); }
  if (name === 'visibility') openPage('visibility').then((page) => page?.show());
  if (name === 'folders') {
    openPage('folders').then((page) => page?.load());
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
  if (name === 'ai-models' || name === 'settings') openPage('extensions').then((page) => page?.show());
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
    // Not a number of things: a warning (nothing copies the library).
    count.textContent = item.key === 'one_place' ? '!' : item.count > 99 ? '99+' : String(item.count);
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

/* -- Extensions ------------------------------------------------------------
 *
 * Which are on decides which pages show (an extension's own pages, such as
 * Creative Studio's AI server), so that is asked at sign-in; the list itself,
 * its switches and the Gemini key are the page module's
 * (pages/console/extensions.js), drawn when Home & extensions or AI models
 * is opened.
 */

function setActiveExtensions(names) {
  activeExtensions.clear();
  for (const name of names || []) activeExtensions.add(name);
  syncTabVisibility();
}

async function loadActiveExtensions() {
  try {
    setActiveExtensions((await adminApi.extensions()).active);
  } catch { /* the page still works without it */ }
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
