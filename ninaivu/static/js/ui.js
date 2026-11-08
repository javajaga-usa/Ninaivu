/**
 * The helpers both pages draw with: finding and making elements, the toasts,
 * the words for sizes and counts, and the dialogs more than one page opens.
 *
 * Moved here from app.js and admin.js unchanged, so the pages split out of
 * them (static/js/pages/) draw exactly as they did.
 */

import { hideSecret } from './accounts.js';
import { admin } from './api.js';
import * as i18n from './i18n.js';

export const $ = (sel) => document.querySelector(sel);

/* Like $, but never null.
 *
 * admin.html is compiled once and cached by Flask, while the scripts are
 * served with no-cache and update immediately, so between an edit and a
 * restart the browser runs new JavaScript against old markup. A bare
 * `$('#missing').onclick =` throws there and abandons the rest of the function
 * it is in — which is how adding a sign-out button silently killed the
 * storage-integrity button wired forty lines below it. Handlers bound to the
 * stub simply never fire.
 */
const DETACHED = document.createElement('div');
export const $$ = (sel) => document.querySelector(sel) || DETACHED;

export const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
};

export function span(text) { return el('span', '', text); }

export function strong(text) {
  const node = document.createElement('strong');
  node.textContent = text;
  return node;
}

/* A translated sentence with nodes in it — a name in bold, say. The sentence
   is translated whole and the nodes are put where its placeholders fall, since
   the words either side of them are in a different order in another language.
   Plain values fill in as text. */
export function emphasised(key, parts) {
  return i18n.t(key).split(/(\{\w+\})/).filter(Boolean).map((piece) => {
    const name = /^\{(\w+)\}$/.exec(piece)?.[1];
    if (!name || !(name in parts)) return piece;
    const part = parts[name];
    return part instanceof Node ? part : String(part);
  });
}

/* A count in a sentence, in the current language. `one` and `many` are keys
   marked with i18n.key() where they are written — "1 file" and "{count}
   files" — because the words around a number move with the language. */
export function plural(n, one, many) {
  return n === 1 ? i18n.t(one) : i18n.t(many, { count: n.toLocaleString() });
}

/** A size for the console: "12 MB", "3.4 GB"; nothing for nothing. */
export function bytesShort(n) {
  if (!n) return '';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let value = n;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
  return `${value >= 10 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
}

/** A size for the family app: "0 B", "1.5 MB". */
export function humanBytes(n) {
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

/* -- messages -------------------------------------------------------------- */

/**
 * A message at the foot of the screen. *shownFor* is how long one that is not
 * an error stays: the family app's are 2.8 seconds, the console's 3.2.
 */
export function toast(message, isError = false, shownFor = 2800) {
  // Nothing to say while the screen is locked: nobody is there to read it,
  // and requests refused behind the lock would pile their errors up here.
  if (document.body.classList.contains('screen-locked')) return;
  const node = el('div', `toast${isError ? ' error' : ''}`, message);
  $('#toasts').appendChild(node);
  setTimeout(() => {
    // Fades and sinks a little on its way out (premium.css, `leaving`).
    node.classList.add('leaving');
    node.style.opacity = '0';
    setTimeout(() => node.remove(), 250);
  }, isError ? 5200 : shownFor);
}

/** The console's toast: the same, a little longer on screen. */
export function consoleToast(message, isError = false) {
  toast(message, isError, 3200);
}

/* -- small menus (the family app's top bar) --------------------------------- */

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
export function wireMenu(button, menu) {
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
export function closeMenus() {
  return menus.map((close) => close(true)).some(Boolean);
}

/* -- the console's delete dialog ------------------------------------------- */

/** What is about to be deleted, in a line: a name and size, or a count. */
export function lfDescribe(items) {
  if (items.length === 1) return `"${items[0].filename}" (${bytesShort(items[0].size)})`;
  return i18n.t('{files}, {size} together', {
    files: plural(items.length, i18n.key('1 file'), i18n.key('{count} files')),
    size: bytesShort(items.reduce((sum, item) => sum + (item.size || 0), 0)),
  });
}

/**
 * The console's one delete dialog (#lf-delete-modal), opened by Large files
 * and by the folder screen. With *password*, the files go to the bin through
 * the password-gated /api/delete; without, through the folders' own delete.
 */
export function askPasswordToDelete(items, done, { password = true } = {}) {
  const modal = $('#lf-delete-modal');
  const input = $('#lf-delete-password');
  const error = $('#lf-delete-error');
  const confirm = $('#lf-delete-confirm');
  input.closest('.field').hidden = !password;

  $('#lf-delete-title').textContent = items.length === 1
    ? i18n.t('Delete this file?')
    : i18n.t('Delete these files?');
  $('#lf-delete-what').textContent = lfDescribe(items);
  error.hidden = true;
  input.value = '';
  hideSecret(input);
  modal.hidden = false;
  (password ? input : confirm).focus();

  const close = () => {
    modal.hidden = true;
    input.onkeydown = null;
    modal.onkeydown = null;
    confirm.onclick = null;
    $('#lf-delete-cancel').onclick = null;
  };

  const attempt = async () => {
    if (password && !input.value) { input.focus(); return; }
    confirm.disabled = true;
    try {
      const ids = items.map((item) => item.id);
      const result = password
        ? await admin.postDelete({ body: { ids, password: input.value } })
        : await admin.postAdminFoldersDelete({ body: { ids } });
      close();
      await done(result);
    } catch (exc) {
      if (exc.status === 401 && exc.data?.needs_password) {
        error.textContent = i18n.fromServer(exc.data, exc.message);
        error.hidden = false;
        input.value = '';
        input.focus();
        return;
      }
      close();
      consoleToast(exc.message, true);
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
  modal.onkeydown = password ? null : (event) => {
    if (event.key === 'Escape') close();
  };
}
