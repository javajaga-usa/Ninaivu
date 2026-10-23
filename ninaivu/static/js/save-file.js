/**
 * Saving a photograph without leaving the app.
 *
 * Everywhere but an iPhone or an iPad, a link with `download` saves the file
 * and the page stays where it was. On iOS it does not: Safari shows the file
 * in a preview of its own, and Ninaivu added to the Home Screen has no back
 * button to come out of it, so the only way back was to close the app.
 *
 * On iOS a photograph or a short video is fetched here and handed to the
 * share sheet, which offers "Save Image" or "Save Video" and closes back onto
 * the app. A ZIP, a very large file, or a phone too old for the share sheet
 * opens in a new tab instead, which on the Home Screen comes with a Done
 * button.
 */
import * as i18n from './i18n.js';

/**
 * The largest file fetched into memory to hand to the share sheet. A long
 * video can be gigabytes; held whole in a phone's memory it would be the app
 * that closed, not the preview.
 */
export const SHARE_LIMIT = 300 * 1024 * 1024;

/** True on an iPhone, an iPod or an iPad, including one asking for the desktop site. */
export function isIOS(nav = globalThis.navigator) {
  if (!nav) return false;
  // A desktop-class iPad says "MacIntel" and has a touchscreen.
  return /iPad|iPhone|iPod/.test(nav.userAgent || '') ||
    (nav.platform === 'MacIntel' && (nav.maxTouchPoints || 0) > 1);
}

/** The name the server gave the file, from its Content-Disposition. */
export function fileNameFrom(disposition, fallback = 'photo') {
  if (!disposition) return fallback;
  const encoded = /filename\*\s*=\s*(?:UTF-8|utf-8)''([^;]+)/.exec(disposition);
  if (encoded) {
    try { return decodeURIComponent(encoded[1].trim()); } catch { /* the plain name below */ }
  }
  const plain = /filename\s*=\s*"?([^";]+)"?/.exec(disposition);
  return plain ? plain[1].trim() : fallback;
}

/** A link with `download`, clicked: what every other browser does well. */
function plainDownload(url) {
  const link = document.createElement('a');
  link.href = url;
  link.download = '';
  link.rel = 'noopener';
  document.body.appendChild(link);
  link.click();
  link.remove();
}

/**
 * Offer the share sheet again from a tap of its own.
 *
 * Safari lets a page open the share sheet only straight after a tap, and a
 * large photograph can take longer than that to arrive. Then the file is
 * ready and one more tap, on this, saves it.
 */
function offerAgain(file) {
  const holder = document.getElementById('toasts') || document.body;
  const card = document.createElement('div');
  card.className = 'toast save-ready';
  card.setAttribute('role', 'status');
  const words = document.createElement('span');
  words.textContent = i18n.t('{name} is ready', { name: file.name });
  const save = document.createElement('button');
  save.type = 'button';
  save.className = 'btn small primary';
  save.textContent = i18n.t('Save');
  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'btn small ghost';
  close.textContent = i18n.t('Not now');
  const done = () => card.remove();
  save.onclick = () => {
    done();
    navigator.share({ files: [file] }).catch(() => {});
  };
  close.onclick = done;
  card.append(words, save, close);
  holder.appendChild(card);
  setTimeout(done, 30000);
}

/**
 * Save the file at `url` and keep the app on screen.
 *
 * `size` is the file's size in bytes when it is known; `archive` marks a ZIP,
 * which is streamed by the server and never goes through the share sheet.
 * Must be called from the tap itself: a new tab or the share sheet opened any
 * later is refused.
 */
export async function saveFile(url, { size = 0, archive = false, toast = null } = {}) {
  if (!url) return;
  if (!isIOS()) {
    plainDownload(url);
    return;
  }
  const shareable = !archive && typeof navigator.share === 'function' &&
    typeof navigator.canShare === 'function' && typeof File === 'function' &&
    (!size || size <= SHARE_LIMIT);
  if (!shareable) {
    // A new tab, so the preview has a Done button and the app stays behind it.
    window.open(url, '_blank', 'noopener');
    return;
  }

  toast?.(i18n.t('Getting it ready…'));
  let file;
  try {
    const response = await fetch(url, { credentials: 'same-origin' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const blob = await response.blob();
    const name = fileNameFrom(response.headers.get('Content-Disposition'));
    file = new File([blob], name, { type: blob.type || 'application/octet-stream' });
  } catch (err) {
    toast?.(i18n.t('Could not save it: {reason}', { reason: err.message }), true);
    return;
  }
  if (!navigator.canShare({ files: [file] })) {
    // A kind of file the share sheet will not take: the new tab after all,
    // from a tap of its own.
    offerTab(url, file.name);
    return;
  }
  try {
    await navigator.share({ files: [file] });
  } catch (err) {
    // Closed without saving: nothing to say.
    if (err?.name === 'AbortError') return;
    // The tap is too long ago for Safari: one more tap opens the sheet.
    offerAgain(file);
  }
}

/** A link to the file in a new tab, for a file the share sheet refuses. */
function offerTab(url, name) {
  const holder = document.getElementById('toasts') || document.body;
  const card = document.createElement('div');
  card.className = 'toast save-ready';
  card.setAttribute('role', 'status');
  const words = document.createElement('span');
  words.textContent = i18n.t('{name} is ready', { name });
  const open = document.createElement('a');
  open.className = 'btn small primary';
  open.href = url;
  open.target = '_blank';
  open.rel = 'noopener';
  open.textContent = i18n.t('Open');
  const done = () => card.remove();
  open.addEventListener('click', () => setTimeout(done, 0));
  card.append(words, open);
  holder.appendChild(card);
  setTimeout(done, 30000);
}

/**
 * Make a link with `download` save through saveFile on iOS.
 *
 * The link keeps its `href`, so a long press and every other browser go on
 * working as they did; only the tap on an iPhone or an iPad is taken over.
 * The file's size comes from `sizeOf`, or else the link's `data-size`.
 */
export function keepInApp(link, { sizeOf = () => Number(link?.dataset?.size) || 0, toast = null } = {}) {
  if (!link) return;
  link.addEventListener('click', (event) => {
    if (!isIOS() || !link.href) return;
    event.preventDefault();
    saveFile(link.href, { size: sizeOf(), archive: /\/zip\b/.test(link.href), toast });
  });
}
