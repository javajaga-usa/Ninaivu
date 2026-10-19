/**
 * Library changes — the Health page's account of what happened to the
 * library's files (ninaivu/storage/journal.py): a line for each day, kind of
 * change and folder, newest first, and a warning above them when hundreds of
 * files went or were written over in one folder within minutes, which is what
 * a folder replaced by an older copy looks like. Shown only while the change
 * journal is switched on (Advanced settings), or while it holds anything.
 */

import { reportUnauthorized } from './api.js';
import * as i18n from './i18n.js';

const $ = (sel) => document.querySelector(sel);

async function json(url) {
  const response = await fetch(url, { headers: { Accept: 'application/json' } });
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    if (response.status === 401) reportUnauthorized(url);
    throw Object.assign(new Error(i18n.fromServer(data, response.statusText)),
      { status: response.status, data });
  }
  return data;
}

const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
};

//: Translated where the line is drawn: see i18n.key().
const KIND = {
  added: i18n.key('added'),
  edited: i18n.key('changed'),
  renamed: i18n.key('renamed'),
  moved: i18n.key('moved'),
  binned: i18n.key('put in the bin'),
  restored: i18n.key('taken out of the bin'),
  removed: i18n.key('gone'),
};

const when = (stamp) => new Date(stamp * 1000).toLocaleString(i18n.locale(),
  { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });

const dayName = (iso) => {
  const [y, m, d] = String(iso).split('-').map(Number);
  try {
    return new Date(y, m - 1, d).toLocaleDateString(i18n.locale(),
      { weekday: 'short', day: 'numeric', month: 'long', year: 'numeric' });
  } catch {
    return iso;
  }
};

export class JournalPanel {
  constructor({ toast }) {
    this.toast = toast || (() => {});
    this.report = null;
    i18n.onChange(() => { if (this.report) this.render(this.report); });
  }

  async show() {
    try {
      this.render(await json('/api/admin/journal?days=30'));
    } catch (exc) {
      if (exc.status === 404) { $('#journal-block').hidden = true; return; }
      if (exc.status !== 401 && exc.status !== 423) this.toast(exc.message, true);
    }
  }

  render(report) {
    this.report = report;
    const block = $('#journal-block');
    if (!block) return;
    block.hidden = !report.enabled && !report.rows;
    const warn = $('#journal-bursts');
    warn.replaceChildren(...(report.bursts || []).map((b) => el('p', 'journal-burst',
      i18n.t('{count} files {kind} in {folder} between {from} and {to}. If that was not you, the mirror and the backups still have them.', {
        count: b.count.toLocaleString(i18n.locale()),
        kind: i18n.t(KIND[b.kind] || b.kind),
        folder: b.area || i18n.t('the library folder'),
        from: when(b.first),
        to: when(b.last),
      }))));
    const list = $('#journal-days');
    list.replaceChildren();
    if (!report.days?.length) {
      list.append(el('p', 'hint', report.enabled
        ? i18n.t('Nothing has changed in the last 30 days.')
        : i18n.t('The change journal is off. Switch it on under Advanced settings.')));
      return;
    }
    let current = '';
    for (const line of report.days) {
      if (line.day !== current) {
        current = line.day;
        list.append(el('h3', 'journal-day', dayName(line.day)));
      }
      const row = el('div', 'journal-line');
      row.append(
        el('span', `journal-kind journal-${line.kind}`, i18n.t(KIND[line.kind] || line.kind)),
        el('span', 'journal-count', line.count.toLocaleString(i18n.locale())),
        el('span', 'journal-folder', line.area || i18n.t('the library folder')));
      list.append(row);
    }
  }
}
