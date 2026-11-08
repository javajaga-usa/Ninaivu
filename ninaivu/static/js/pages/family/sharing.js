/**
 * The family app's share dialog: a link to a photograph or an album, loaded when first opened (app.js, openPage).
 */

import { api } from '../../api.js';
import * as i18n from '../../i18n.js';
import { state } from '../../store.js';
import { $, toast } from '../../ui.js';

/* -- Sharing ------------------------------------------------------------ */
let shareTargetItem = null;

/** Bind the share dialog: once, when it is first opened. */
export function wire() {
  $('#share-close')?.addEventListener('click', () => {
    $('#share-modal').hidden = true;
  });

  $('#share-create-btn')?.addEventListener('click', async () => {
    const button = $('#share-create-btn');
    if (!shareTargetItem || button.disabled) return;
    const target = shareTargetItem;
    button.disabled = true;
    const expiry = Number($('#share-expiry').value || 30);
    const pwd = $('#share-password').value.trim() || null;
    const scope = target.type === 'album' ? 'album' : 'asset';
    try {
      const res = await api.createShare(scope, target.id, expiry, pwd);
      if (shareTargetItem !== target) return;
      const fullUrl = `${window.location.origin}${res.share_url}`;
      $('#share-url-text').value = fullUrl;
      $('#share-link-result').hidden = false;
      $('#share-options').hidden = true;
      $('#share-create-btn').hidden = true;
      toast(i18n.t('Share link generated!'));
    } catch (err) {
      toast(i18n.t('Could not create share link: {reason}', { reason: err.message }), true);
    } finally {
      button.disabled = false;
    }
  });

  $('#share-copy-btn')?.addEventListener('click', async () => {
    const input = $('#share-url-text');
    input.select();
    try {
      if (!navigator.clipboard?.writeText) throw new Error(i18n.t('Clipboard unavailable'));
      await navigator.clipboard.writeText(input.value);
      toast(i18n.t('Link copied to clipboard!'));
    } catch {
      input.focus();
      input.select();
      toast(i18n.t('Could not copy automatically. Copy the selected link manually.'), true);
    }
  });
}

export function openShareModal(item) {
  shareTargetItem = item;
  $('#share-link-result').hidden = true;
  $('#share-options').hidden = false;
  $('#share-create-btn').hidden = false;
  $('#share-password').value = '';
  if (item?.type === 'album') {
    $('#share-title').textContent = i18n.t('Share album: {name}', { name: item.name });
  } else {
    $('#share-title').textContent = i18n.t('Share with Family & Friends');
  }
  // Share without strangers (api_share.py): said before the link is made.
  const blurNote = $('#share-blur-note');
  if (blurNote) blurNote.hidden = !state.status?.moments?.share_blur;
  $('#share-modal').hidden = false;
}
