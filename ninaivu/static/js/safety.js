/**
 * "Is everything safe?" — the Overview's one answer from every protection
 * Ninaivu has (ninaivu/server/safety.py): the copy in Drive, the test restores,
 * the index copy, local backups, the drives, the archive, the storage check.
 *
 * Worst first, each with a line saying why and a button to the page that can
 * do something about it.
 */

import { reportUnauthorized } from './api.js';

const $ = (sel) => document.querySelector(sel);

const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
};

const PAGES = { cloud: 'Mugil', activity: 'Activity', archive: 'Archive' };

export class SafetyPanel {
  constructor({ toast, openPage }) {
    this.toast = toast || (() => {});
    this.openPage = openPage || (() => {});
  }

  wire() {
    const button = $('#sf-refresh');
    if (!button) return;
    button.onclick = () => this.load(true);
  }

  async load(fresh = false) {
    if (!$('#sf-block')) return;
    try {
      const response = await fetch(`/api/admin/safety${fresh ? '?fresh=1' : ''}`,
        { headers: { Accept: 'application/json' } });
      if (response.status === 401) { reportUnauthorized('/api/admin/safety'); return; }
      // A server from before this page, or one that is locked: leave it be.
      if (response.status === 404) { $('#sf-block').hidden = true; return; }
      if (!response.ok) return;
      this.render(await response.json());
    } catch (exc) {
      this.toast(exc.message, true);
    }
  }

  render(report) {
    $('#sf-block').hidden = false;
    const headline = $('#sf-headline');
    headline.textContent = report.headline;
    headline.className = `sf-headline sf-${report.verdict}`;
    // A check with nothing to check yet ("nothing to test until there is a
    // copy in Drive") is true and worth being able to read, but at full
    // weight beside the ones asking for something it was half the list.
    // They go under one line at the foot, closed.
    const live = report.checks.filter((check) => check.status !== 'off');
    const idle = report.checks.filter((check) => check.status === 'off');
    const rows = live.map((check) => this.row(check));
    if (idle.length) {
      const list = el('ul', 'sf-checks');
      list.replaceChildren(...idle.map((check) => this.row(check)));
      const details = el('details');
      details.append(el('summary', null, idle.length === 1
        ? '1 more check has nothing to check yet'
        : `${idle.length} more checks have nothing to check yet`), list);
      const fold = el('li', 'sf-idle');
      fold.append(details);
      rows.push(fold);
    }
    $('#sf-checks').replaceChildren(...rows);
  }

  row(check) {
    const row = el('li', 'sf-check');
    const text = el('div', 'sf-text');
    text.append(el('span', 'sf-title', check.title), el('span', 'sf-summary', check.summary));
    if (check.detail && check.status !== 'ok') text.append(el('span', 'sf-detail', check.detail));
    const dot = el('span', `sf-dot sf-${check.status}`);
    dot.title = check.status;
    row.append(dot, text);
    if (check.status !== 'ok' && check.status !== 'off' && PAGES[check.page]) {
      const go = el('button', 'btn ghost small', `Open ${PAGES[check.page]}`);
      go.type = 'button';
      go.onclick = () => this.openPage(check.page);
      row.append(go);
    } else {
      row.append(el('span'));
    }
    return row;
  }
}
