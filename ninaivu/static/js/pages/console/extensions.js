/**
 * The extensions on Home & extensions (each one's switch, what it does and
 * whether anything leaves this computer), whether family members may use an
 * outside one, and the Gemini key. Loaded when Home & extensions or AI
 * models is first opened (admin.js, openPage). Which extensions are on is
 * also asked at sign-in, by admin.js, for the pages they bring.
 */

import { consoleJson as json } from '../../api.js';
import * as i18n from '../../i18n.js';
import { consoleState as state } from '../../store.js';
import { $, consoleToast as toast } from '../../ui.js';

const extensionsApi = {
  extensions: () => json('/api/admin/extensions'),
  setExtension: (name, enabled) =>
    json('/api/admin/extensions', { method: 'POST', body: { name, enabled } }),
  gemini: () => json('/api/admin/gemini'),
  setGemini: (key) => json('/api/admin/gemini', { method: 'POST', body: { key } }),
  settings: (body) => json('/api/admin/settings', { method: 'POST', body }),
};

/** The console around the page (admin.js). */
let shell;

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
  shell.setActiveExtensions([...active]);
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
        showExtensions(await extensionsApi.setExtension(ext.name, box.checked));
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

/** The page has been opened, or the language changed while it is open. */
export async function show() {
  try {
    showExtensions(await extensionsApi.extensions());
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
      await extensionsApi.settings({ outside_ai_for_family: box.checked });
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
    showGemini(await extensionsApi.gemini());
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
      showGemini(await extensionsApi.setGemini(key));
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
      showGemini(await extensionsApi.setGemini(''));
      toast(i18n.t('Gemini is off. The key was removed from this machine.'));
    } catch (exc) {
      toast(exc.message, true);
    }
  });
}

/** Loaded: kept for the switches, which tell the console which pages show. */
export function wire(context) {
  shell = context;
}
