/**
 * The Setup check page: ports, drives, Tailscale and permissions, each line
 * saying what was found and, when something is wrong, what to do about it in
 * plain words (server/setup_check.py). Nothing on this page changes anything;
 * a fix that is done on another console page has a button that opens it.
 */

import { admin } from '../../api.js';
import * as i18n from '../../i18n.js';
import { $, consoleToast as toast, el, whenShort } from '../../ui.js';

const STATE_WORDS = {
  ok: i18n.key('Fine'),
  warn: i18n.key('Worth a look'),
  fix: i18n.key('Needs fixing'),
  info: i18n.key('For your information'),
};

let shell = null;
let asking = null;
let last = null;

/** Wired once, when the page is first opened (admin.js, openPage). */
export function wire(console) {
  shell = console;
  $('#sc-refresh').onclick = () => load(true);
  i18n.onChange(() => { if (last) render(last); });
}

/** Asked each time the page is opened; the server reuses a look from the last half minute. */
export function show() {
  return load(false);
}

async function load(fresh) {
  if (asking) return asking;
  const button = $('#sc-refresh');
  button.disabled = true;
  if (fresh || !last) $('#sc-headline').textContent = i18n.t('Checking this computer…');
  asking = admin.getAdminSetupCheck(fresh ? { query: { fresh: 1 } } : {})
    .then((data) => { last = data; render(data); })
    .catch((exc) => toast(exc.message, true))
    .finally(() => { asking = null; button.disabled = false; });
  return asking;
}

/** A sentence the server sent, in the current language. */
function words(message) {
  return message ? i18n.fromServer(message) : '';
}

function headline(counts) {
  const fix = counts.fix || 0;
  const warn = counts.warn || 0;
  if (fix) {
    return fix === 1 ? i18n.t('1 thing needs fixing') : i18n.t('{count} things need fixing', { count: fix });
  }
  if (warn) {
    return warn === 1 ? i18n.t('Set up, with 1 thing worth a look') : i18n.t('Set up, with {count} things worth a look', { count: warn });
  }
  return i18n.t('Everything is set up');
}

function render(data) {
  const counts = data.counts || {};
  const head = $('#sc-headline');
  head.textContent = headline(counts);
  head.dataset.state = counts.fix ? 'fix' : (counts.warn ? 'warn' : 'ok');
  const when = $('#sc-when');
  when.hidden = !data.checked_at;
  if (data.checked_at) when.textContent = i18n.t('Checked {when}', { when: whenShort(data.checked_at) });

  const tab = document.querySelector('#tabs button[data-tab="setup-check"] .tab-count');
  if (tab) {
    tab.hidden = !counts.fix;
    tab.textContent = String(counts.fix || '');
  }

  const areas = $('#sc-areas');
  areas.innerHTML = '';
  const checks = data.checks || [];
  for (const area of data.areas || []) {
    const mine = checks.filter((check) => check.area === area.key);
    if (!mine.length) continue;
    const section = el('section', 'sc-area');
    section.dataset.area = area.key;
    section.appendChild(el('h3', null, words(area.title)));
    // What needs doing first, then what is worth a look, then the rest.
    const order = { fix: 0, warn: 1, info: 2, ok: 3 };
    mine.sort((a, b) => (order[a.state] ?? 4) - (order[b.state] ?? 4));
    const list = el('ul', 'sc-list');
    for (const check of mine) list.appendChild(row(check));
    section.appendChild(list);
    areas.appendChild(section);
  }
}

function row(check) {
  const item = el('li', `sc-check ${check.state}`);
  item.dataset.key = check.key;
  const mark = el('span', 'sc-mark', i18n.t(STATE_WORDS[check.state] || STATE_WORDS.info));
  item.appendChild(mark);
  const body = el('div', 'sc-body');
  body.appendChild(el('p', 'sc-title', words(check.title)));
  if (check.detail) body.appendChild(el('p', 'sc-detail', words(check.detail)));
  if (check.fix) {
    const fix = el('p', 'sc-fix');
    fix.appendChild(el('strong', null, `${i18n.t('What to do')}: `));
    fix.appendChild(document.createTextNode(words(check.fix)));
    body.appendChild(fix);
  }
  if (check.command) body.appendChild(commandLine(check.command));
  const actions = el('div', 'sc-actions');
  if (check.page && shell && document.querySelector(`#tabs button[data-tab="${CSS.escape(check.page)}"]`)) {
    const open = el('button', 'btn small', i18n.t('Open that page'));
    open.type = 'button';
    open.onclick = () => shell.showTab(check.page);
    actions.appendChild(open);
  }
  if (check.link) {
    const link = el('a', 'btn ghost small', i18n.t('Open Tailscale’s page'));
    link.href = check.link;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    actions.appendChild(link);
  }
  if (actions.childElementCount) body.appendChild(actions);
  item.appendChild(body);
  return item;
}

function commandLine(command) {
  const wrap = el('div', 'sc-command');
  wrap.appendChild(el('code', null, command));
  const copy = el('button', 'btn ghost small', i18n.t('Copy'));
  copy.type = 'button';
  copy.onclick = async () => {
    try {
      await navigator.clipboard.writeText(command);
      toast(i18n.t('Copied'));
    } catch {
      // Over plain HTTP from another device there is no clipboard: select
      // the command so it can be copied by hand.
      const range = document.createRange();
      range.selectNodeContents(wrap.querySelector('code'));
      const chosen = window.getSelection();
      chosen.removeAllRanges();
      chosen.addRange(range);
      toast(i18n.t('Select the command and copy it by hand.'));
    }
  };
  wrap.appendChild(copy);
  return wrap;
}
