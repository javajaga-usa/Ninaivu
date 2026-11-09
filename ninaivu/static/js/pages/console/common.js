/**
 * What more than one console page does: shared by the folder screen, Large
 * files, People and the console shell.
 */

import { admin } from '../../api.js';
import { consoleToast as toast, el } from '../../ui.js';

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
