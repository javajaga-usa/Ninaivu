import {reportUnauthorized} from '../../api.js';

export async function saveLibraryCopy(assetId, blob) {
  if (!Number.isSafeInteger(assetId) || assetId < 1 || blob.type !== 'image/png')
    throw new Error('Choose a library photo and export a PNG copy.');
  const response = await fetch(`/api/asset/${assetId}/edited-copy`, {
    method: 'POST', headers: {'Content-Type': 'image/png'}, body: blob,
  });
  if (response.status === 401) reportUnauthorized();
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Could not save the copy (${response.status}).`);
  // A family member's copy waits for an administrator, so there is no asset to
  // open yet and no id to check -- only the news that it is in the queue.
  if (data.status === 'pending')
    return {pending: true, message: data.message || 'Saved for an administrator to approve.'};
  if (!Number.isSafeInteger(data.id)) throw new Error('The save response was incomplete. Check your library before saving again.');
  return data;
}
