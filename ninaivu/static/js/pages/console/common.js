/**
 * What more than one console page does: shared by the folder screen, Large
 * files and the console shell.
 */

import { admin } from '../../api.js';
import { consoleToast as toast } from '../../ui.js';

/** Show an item in the file manager of the computer Ninaivu runs on. */
export async function revealItem(item) {
  try {
    await admin.postAdminRevealByAssetId(item.id);
  } catch (exc) {
    toast(exc.message, true);
  }
}
