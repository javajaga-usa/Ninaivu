/**
 * The gallery's search box (app.js): what is typed or said is searched after
 * a short pause, with suggestions under the box (things, tags, folders,
 * cameras). Always on screen, so imported with the gallery; a module of its
 * own so app.js is the gallery's wiring and not everything.
 *
 * What it needs of the gallery around it comes through connectSearch().
 */

import { api } from '../api.js';
import * as i18n from '../i18n.js';
import { state } from '../store.js';
import { $, toast } from '../ui.js';
import { VoiceSearch } from '../voice-search.js';

/** The gallery around the search box (app.js), given once at start. */
let ctx;

/** Given by app.js before anything here is wired. */
export function connectSearch(context) {
  ctx = context;
}

/** The microphone in the search box (static/js/voice-search.js). */
let voiceSearch = null;
/** The pause after typing before the gallery is asked again. */
let searchTimer = null;

/** Forget a search that was about to be asked: something else was chosen. */
export function stopSearch() {
  clearTimeout(searchTimer);
}

/** Show or hide the microphone (a setting, and for the family only). */
export function showVoiceSearch(on) {
  voiceSearch?.show(on);
}

export function wireSearch() {
  const input = $('#search');
  const list = $('#suggestions');
  // What was said is searched exactly as if it had been typed.
  voiceSearch = new VoiceSearch($('#voice-search'), {
    toast,
    onHeard: (query) => {
      input.value = query;
      input.dispatchEvent(new Event('input', { bubbles: true }));
    },
  });

  input.addEventListener('input', () => {
    const value = input.value.trim();
    $('#clear-search').hidden = !value;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.filters.q = value;
      ctx.reload({ resetScroll: true });
    }, value ? 260 : 0);
    updateSuggestions(value);
  });

  input.addEventListener('keydown', (event) => {
    const options = [...list.querySelectorAll('li')];
    const active = list.querySelector('[aria-selected="true"]');
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      if (!options.length) return;
      event.preventDefault();
      const index = options.indexOf(active);
      const next = event.key === 'ArrowDown'
        ? Math.min(options.length - 1, index + 1)
        : Math.max(0, index - 1);
      options.forEach((li, i) => li.setAttribute('aria-selected', String(i === next)));
    } else if (event.key === 'Enter' && active) {
      active.click();
    } else if (event.key === 'Escape') {
      list.hidden = true;
      input.blur();
    }
  });

  input.addEventListener('blur', () => setTimeout(() => (list.hidden = true), 140));
  $('#clear-search').onclick = () => {
    clearTimeout(searchTimer);
    input.value = '';
    $('#clear-search').hidden = true;
    state.filters.q = '';
    list.hidden = true;
    ctx.reload({ resetScroll: true });
  };
}

// What each suggestion is, in the reader's language; the server sends the
// English word for its kind.
const SUGGESTION_KINDS = {
  concept: () => i18n.t('Thing'),
  tag: () => i18n.t('Tag'),
  folder: () => i18n.t('Folder'),
  camera: () => i18n.t('Camera'),
};

async function updateSuggestions(value) {
  const list = $('#suggestions');
  if (!value || value.length < 2) {
    list.hidden = true;
    return;
  }
  let data;
  try {
    data = await api.suggest(value);
  } catch { return; }

  if ($('#search').value.trim() !== value) return;

  const entries = [
    ...(data.concepts || []).map((c) => ({ type: 'concept', value: c })),
    ...(data.suggestions || []),
  ].slice(0, 10);

  list.innerHTML = '';
  if (!entries.length) {
    list.hidden = true;
    return;
  }
  for (const entry of entries) {
    const li = document.createElement('li');
    li.setAttribute('role', 'option');
    const kind = document.createElement('span');
    kind.className = 'kind';
    kind.textContent = SUGGESTION_KINDS[entry.type] ? SUGGESTION_KINDS[entry.type]() : entry.type;
    const label = document.createElement('span');
    label.textContent = entry.value;
    li.append(kind, label);
    if (entry.count) {
      const n = document.createElement('span');
      n.className = 'n';
      n.textContent = entry.count;
      li.appendChild(n);
    }
    li.onclick = () => {
      clearTimeout(searchTimer);
      list.hidden = true;
      if (entry.type === 'tag') {
        $('#search').value = '';
        state.filters.q = '';
        state.filters.tag = entry.value;
      } else if (entry.type === 'folder') {
        state.filters.folder = entry.value;
      } else if (entry.type === 'camera') {
        state.filters.camera = entry.value;
      } else {
        $('#search').value = entry.value;
        state.filters.q = entry.value;
      }
      ctx.reload({ resetScroll: true });
    };
    list.appendChild(li);
  }
  list.hidden = false;
}
