import {reportUnauthorized} from '../../api.js';
import * as i18n from '../../i18n.js';

export async function saveLibraryCopy(assetId, blob) {
  if (!Number.isSafeInteger(assetId) || assetId < 1 || blob.type !== 'image/png')
    throw new Error(i18n.t('Choose a library photo and export a PNG copy.'));
  // X-Requested-With: a raw body is what a cross-origin form could also send,
  // so the server takes one only from a script that says so.
  const response = await fetch(`/api/asset/${assetId}/edited-copy`, {
    method: 'POST', headers: {'Content-Type': 'image/png', 'X-Requested-With': 'fetch'}, body: blob,
  });
  if (response.status === 401) reportUnauthorized();
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || i18n.t('Could not save the copy ({status}).', {status: response.status}));
  // A family member's copy waits for an administrator, so there is no asset to
  // open yet and no id to check -- only the news that it is in the queue.
  if (data.status === 'pending')
    return {pending: true, message: data.message || i18n.t('Saved for an administrator to approve.')};
  if (!Number.isSafeInteger(data.id)) throw new Error(i18n.t('The save response was incomplete. Check your library before saving again.'));
  return data;
}
