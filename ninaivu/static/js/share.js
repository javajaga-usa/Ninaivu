/* The share page is deliberately its own small thing rather than the family
   app in a different hat. Somebody opening this link has no account, no
   session and no business loading the gallery's machinery — and every URL it
   uses carries the token, because that token is the only authority here. */
import * as i18n from './i18n.js';

const TOKEN = document.body.dataset.shareToken;
const main = document.getElementById('main');
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};

function askForPassword(message) {
  main.replaceChildren();
  const form = el('form');
  form.appendChild(el('p', 'muted', i18n.t('This link is password protected.')));
  const input = document.createElement('input');
  input.type = 'password'; input.required = true;
  input.autocomplete = 'off'; input.placeholder = i18n.t('Password');
  input.setAttribute('aria-label', i18n.t('Share password'));
  const error = el('p', 'error', message || '');
  const button = el('button', null, i18n.t('Open'));
  button.type = 'submit';
  form.append(input, button, error);
  form.onsubmit = async (event) => {
    event.preventDefault();
    button.disabled = true;
    try {
      const res = await fetch(`/api/share/${TOKEN}/unlock`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password: input.value }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) { error.textContent = body.error || i18n.t('That did not work.'); return; }
      await load();
    } catch {
      error.textContent = i18n.t('Could not reach Ninaivu. Try opening the link again.');
    } finally { button.disabled = false; }
  };
  main.appendChild(form);
  input.focus();
}

function renderAlbum(data) {
  document.getElementById('title').textContent = data.album?.name || i18n.t('Shared photographs');
  document.getElementById('count').textContent = data.total === 1
    ? i18n.t('1 photograph')
    : i18n.t('{count} photographs', { count: data.total });
  const grid = el('div', 'grid');
  for (const item of data.items) {
    const link = document.createElement('a');
    link.href = item.view || item.src; link.target = '_blank'; link.rel = 'noopener';
    const img = document.createElement('img');
    img.src = item.thumb; img.alt = item.filename || item.name || ''; img.loading = 'lazy';
    link.appendChild(img);
    grid.appendChild(link);
  }
  main.replaceChildren(grid);
}

function renderOne(item) {
  document.getElementById('title').textContent = item.filename || item.name || i18n.t('Shared photograph');
  const stage = el('div', 'single');
  const isVideo = (item.kind === 'video');
  const isAudio = item.kind === 'audio';
  const node = document.createElement(isVideo ? 'video' : isAudio ? 'audio' : 'img');
  node.src = item.view || item.src;
  node.onerror = () => {
    if (!isVideo && !isAudio && item.thumb && node.getAttribute('src') !== item.thumb) {
      node.src = item.thumb;
    } else {
      stage.appendChild(el('p', 'error', i18n.t('This media could not be displayed.')));
    }
  };
  if (isVideo || isAudio) { node.controls = true; node.playsInline = true; }
  else { node.alt = item.filename || item.name || i18n.t('Shared photograph'); }
  stage.appendChild(node);
  main.replaceChildren(stage);
}

async function load() {
  try {
    const res = await fetch(`/api/share/${TOKEN}`, { headers: { Accept: 'application/json' } });
    if (res.status === 401) { askForPassword(); return; }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      main.replaceChildren(el('p', 'empty',
        data.error || i18n.t('This link is no longer available.')));
      return;
    }
    if (data.scope === 'album') renderAlbum(data); else renderOne(data.item);
  } catch (err) {
    main.replaceChildren(el('p', 'empty', i18n.t('This link could not be opened.')));
  }
}
// Whatever the person opening this link reads. They have no account here and
// no stored preference, so this is their browser's answer — which is the right
// one: a link sent to somebody who reads Tamil opens in Tamil.
await i18n.start();
const from = document.getElementById('from');
if (from) {
  from.textContent = i18n.t('Shared from {name}',
    { name: document.body.dataset.appName || 'Ninaivu' });
}
load();
