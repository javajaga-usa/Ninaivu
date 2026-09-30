/**
 * The command palette: Ctrl+K (⌘K on a Mac) opens one box that goes anywhere
 * and does anything the toolbar does.
 *
 * It owns no behaviour of its own. Every command is a button the page already
 * has (a view in the sidebar, the upload button, a layout, the theme), found in
 * the page each time the palette opens and pressed on the person's behalf. So
 * a button the person may not use, or that this device does not show, is not
 * offered, a new view appears here without anyone remembering to list it, and
 * a command can never do something the button would not.
 */
import * as i18n from './i18n.js';

const isMac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);

/** What a control says about itself, in the page's language. */
function nameOf(node) {
  const label = node.getAttribute('aria-label') || node.title
    || node.querySelector('span:not(.badge)')?.textContent || node.textContent;
  return String(label || '').replace(/\s+/g, ' ').trim();
}

/** Not there for this person, or not there on this screen. */
const offered = (node) => node && !node.closest('[hidden]') && !node.disabled;

/** The commands the page offers right now, in the order they are shown. */
function commands() {
  const found = [];
  const add = (group, node, hint) => {
    const name = nameOf(node);
    if (name && offered(node)) found.push({ group, name, hint, run: () => node.click() });
  };
  const from = (group, selector) => document.querySelectorAll(selector)
    .forEach((node) => add(group, node));

  from(i18n.t('Go to'), '.sidebar [data-view]');
  document.querySelectorAll('.sidebar .album-card').forEach((card) => {
    const name = card.querySelector('.album-text')?.firstElementChild?.textContent?.trim();
    if (name && offered(card)) found.push({ group: i18n.t('Albums'), name, run: () => card.click() });
  });
  const actions = i18n.t('Actions');
  for (const [id, hint] of [['upload-btn'], ['backup-btn'], ['map-btn'],
                            ['theme-btn', 'T'], ['lang-btn'], ['help-btn', '?'],
                            ['sel-all']]) {
    const node = document.getElementById(id);
    if (node) add(actions, node, hint);
  }
  from(i18n.t('Layout'), '[data-layout]');
  return found;
}

const words = (text) => text.toLowerCase().split(/\s+/).filter(Boolean);

export function initPalette() {
  if (document.getElementById('palette')) return;
  const root = document.createElement('div');
  root.id = 'palette';
  root.className = 'palette';
  root.hidden = true;
  root.innerHTML = `
    <div class="palette-card" role="dialog" aria-modal="true" >
      <div class="palette-input">
        <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
        <input type="text" id="palette-query" autocomplete="off" spellcheck="false"
               role="combobox" aria-expanded="true" aria-controls="palette-list"
>
        <kbd>Esc</kbd>
      </div>
      <ul class="palette-list" id="palette-list" role="listbox"></ul>
    </div>`;
  document.body.append(root);

  const input = root.querySelector('#palette-query');
  const list = root.querySelector('#palette-list');
  let all = [], shown = [], at = 0, returnTo = null;

  const draw = () => {
    const query = words(input.value);
    shown = all.filter((c) => query.every((w) => `${c.group} ${c.name}`.toLowerCase().includes(w)));
    at = Math.min(at, Math.max(shown.length - 1, 0));
    list.replaceChildren();
    if (!shown.length) {
      const none = document.createElement('li');
      none.className = 'palette-empty';
      none.textContent = i18n.t('Nothing matches');
      list.append(none);
      return;
    }
    let group = null;
    shown.forEach((command, index) => {
      if (command.group !== group) {
        group = command.group;
        const head = document.createElement('li');
        head.className = 'palette-group';
        head.setAttribute('role', 'presentation');
        head.textContent = group;
        list.append(head);
      }
      const row = document.createElement('li');
      row.className = 'palette-item';
      row.id = `palette-item-${index}`;
      row.setAttribute('role', 'option');
      row.setAttribute('aria-selected', String(index === at));
      const label = document.createElement('span');
      label.textContent = command.name;
      row.append(label);
      if (command.hint) {
        const key = document.createElement('kbd');
        key.textContent = command.hint;
        row.append(key);
      }
      row.onmousemove = () => { if (at !== index) { at = index; mark(); } };
      row.onclick = () => run(index);
      list.append(row);
    });
    input.setAttribute('aria-activedescendant', `palette-item-${at}`);
  };

  const mark = () => {
    list.querySelectorAll('.palette-item').forEach((row, index) => {
      row.setAttribute('aria-selected', String(index === at));
      if (index === at) row.scrollIntoView({ block: 'nearest' });
    });
    input.setAttribute('aria-activedescendant', `palette-item-${at}`);
  };

  const close = () => {
    root.hidden = true;
    if (returnTo?.isConnected) returnTo.focus();
    returnTo = null;
  };

  const run = (index) => {
    const command = shown[index];
    close();
    // After the palette has gone, so a dialog the command opens keeps the focus.
    if (command) setTimeout(command.run, 0);
  };

  const open = () => {
    returnTo = document.activeElement;
    // Said in the language of the moment, not of the day the page loaded.
    root.querySelector('.palette-card').setAttribute('aria-label', i18n.t('Command palette'));
    input.placeholder = i18n.t('Type a command or a place…');
    input.setAttribute('aria-label', i18n.t('Command palette'));
    all = commands();
    input.value = ''; at = 0;
    draw();
    root.hidden = false;
    input.focus();
  };

  root.addEventListener('keydown', (event) => {
    // Nothing typed here is a shortcut for the page underneath.
    event.stopPropagation();
    if (event.key === 'Escape') { event.preventDefault(); close(); }
    else if (event.key === 'ArrowDown') { event.preventDefault(); at = Math.min(at + 1, shown.length - 1); mark(); }
    else if (event.key === 'ArrowUp') { event.preventDefault(); at = Math.max(at - 1, 0); mark(); }
    else if (event.key === 'Enter') { event.preventDefault(); run(at); }
    else if (event.key === 'Tab') { event.preventDefault(); }
  });
  input.addEventListener('input', () => { at = 0; draw(); });
  root.addEventListener('mousedown', (event) => { if (event.target === root) close(); });

  document.addEventListener('keydown', (event) => {
    if (event.key.toLowerCase() !== 'k' || event.altKey || event.shiftKey) return;
    if (!(isMac ? event.metaKey : event.ctrlKey)) return;
    // Sudar has its own keys and its own dialog to keep the focus in.
    if (document.querySelector('.photo-editor[open], #ai-playground[open]')) return;
    event.preventDefault();
    if (root.hidden) open(); else close();
  });

  // The search box says the palette is there.
  const hint = document.getElementById('search-kbd');
  if (hint) hint.textContent = isMac ? '⌘K' : 'Ctrl K';
}
