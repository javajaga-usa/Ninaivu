/**
 * The page's half of the offline shell (static/sw.js).
 *
 * Three small jobs, kept out of app.js so they can be read in one place:
 *
 *   lazyStyle(name)      a stylesheet the page names but does not load until
 *                        the screen that needs it is opened (the photo
 *                        editor's 43 KB, which nobody opening the gallery on
 *                        a phone in India should have to wait for)
 *   checkShell()         when the worker showed the copy of the page it kept,
 *                        because the server was slow to answer, ask it whether
 *                        the server's page was a different build; if it was,
 *                        load that one rather than run an old app against a
 *                        new server
 *   warmShell()          once the gallery has settled, hand the worker the
 *                        stamped files the page may open later, so it can
 *                        fetch them quietly while nobody is waiting
 */

const RELOAD_KEY = 'ninaivu.shell-reloaded';

/**
 * Turn on a stylesheet the page left switched off, and wait (a little) for it.
 *
 * The page names it with no href (a browser fetches even a disabled sheet),
 * and the link stays where the page put it, so the cascade is the one it
 * always was: an editor sheet appended at the end would beat the sheets
 * written to override it.
 */
export function lazyStyle(name, { timeoutMs = 4000 } = {}) {
  const link = document.querySelector(`link[data-lazy-style="${name}"]`);
  const href = link?.dataset.href;
  if (!link || !href || link.getAttribute('href')) return Promise.resolve();
  return new Promise((resolve) => {
    const finish = () => { clearTimeout(timer); resolve(); };
    const timer = setTimeout(finish, timeoutMs);
    link.addEventListener('load', finish, { once: true });
    link.addEventListener('error', finish, { once: true });
    link.setAttribute('href', href);
  });
}

/** Ask the worker something and wait for its answer, or give up. */
function ask(message, timeoutMs) {
  const worker = navigator.serviceWorker?.controller;
  if (!worker || typeof MessageChannel === 'undefined') return Promise.resolve(null);
  return new Promise((resolve) => {
    const channel = new MessageChannel();
    const timer = setTimeout(() => resolve(null), timeoutMs);
    channel.port1.onmessage = (event) => { clearTimeout(timer); resolve(event.data); };
    try {
      worker.postMessage(message, [channel.port2]);
    } catch {
      clearTimeout(timer);
      resolve(null);
    }
  });
}

function reloadedLately() {
  try {
    return Date.now() - Number(sessionStorage.getItem(RELOAD_KEY) || 0) < 60000;
  } catch {
    return true;            // nowhere to note it: never risk reloading twice
  }
}

function reload() {
  try { sessionStorage.setItem(RELOAD_KEY, String(Date.now())); } catch { return; }
  location.reload();
}

/**
 * Load the server's page instead, if the one on screen is an older build.
 *
 * *booting* says whether the gallery is still behind its opening cover: then
 * the reload is the cover staying up a moment longer. Once somebody is
 * looking at photographs it waits until they leave the page (switch app,
 * lock the phone), so nothing is pulled out from under them.
 */
export async function checkShell(booting = () => true) {
  const answer = await ask({ type: 'ninaivu:shell-changed?' }, 30000);
  if (!answer || !answer.changed || reloadedLately()) return;
  if (booting()) {
    reload();
    return;
  }
  const onHide = () => {
    if (document.visibilityState !== 'hidden') return;
    document.removeEventListener('visibilitychange', onHide);
    reload();
  };
  document.addEventListener('visibilitychange', onHide);
}

/** Every stamped file this page could ask for, from its import map and links. */
export function shellFiles(doc = document) {
  const urls = new Set();
  for (const map of doc.querySelectorAll('script[type="importmap"]')) {
    try {
      Object.values(JSON.parse(map.textContent).imports || {}).forEach((u) => urls.add(u));
    } catch { /* not a map we can read */ }
  }
  doc.querySelectorAll('link[data-lazy-style]').forEach((link) => urls.add(link.dataset.href));
  return [...urls].filter((u) => typeof u === 'string' && u.includes('?v='));
}

/**
 * Let the worker fetch the rest of the shell, a while after the gallery
 * showed. Not on a connection that asked to save data.
 */
export function warmShell({ delayMs = 5000 } = {}) {
  const workers = navigator.serviceWorker;
  if (!workers || navigator.connection?.saveData) return;
  const send = async () => {
    // The visit that first registers the worker is not one it controls, and
    // is the one that most needs the rest kept: so ask the active worker.
    const registration = await workers.ready.catch(() => null);
    const worker = workers.controller || registration?.active;
    try { worker?.postMessage({ type: 'ninaivu:warm', urls: shellFiles() }); } catch { /* gone */ }
  };
  setTimeout(() => {
    if ('requestIdleCallback' in window) window.requestIdleCallback(() => send(), { timeout: 5000 });
    else send();
  }, delayMs);
}
