/**
 * What more than one console page does: shared by the folder screen, Large
 * files, People and the console shell.
 */

import { admin } from '../../api.js';
import * as i18n from '../../i18n.js';
import { consoleToast as toast, el } from '../../ui.js';

// The three levels as the server names them, which is also how the tags on
// the page have always read.
const VISIBILITY_WORDS = {
  public: i18n.key('public'),
  family: i18n.key('family'),
  hidden: i18n.key('hidden'),
};

/** A visibility level ("public", "family", "hidden"), in the current language. */
export function visibilityWord(level) {
  return VISIBILITY_WORDS[level] ? i18n.t(VISIBILITY_WORDS[level]) : level;
}

/** The levels by the number the server stores. */
export const VIS_NAMES_BY_VALUE = { 0: 'public', 1: 'family', 2: 'hidden' };

/** Ask the server to open its own file manager with `item` selected.
 *
 * Only ever works when the admin console and Ninaivu's server are the same
 * machine — the server says so plainly (409) when they are not, rather than
 * pretending the click did something. */
export async function revealItem(item) {
  try {
    await admin.postAdminRevealByAssetId(item.id);
  } catch (exc) {
    toast(exc.message, true);
  }
}

/** A control with its label above it. */
export function labelled(label, control) {
  const wrap = el('label', 'field');
  wrap.appendChild(el('span', null, label));
  wrap.appendChild(control);
  return wrap;
}
