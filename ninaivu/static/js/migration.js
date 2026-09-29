/**
 * The Migration page — moving Ninaivu to another computer.
 *
 * Two jobs on one page, because they are the two halves of the same evening.
 * What to copy, with sizes when you ask for them. And then, on the new
 * machine, pointing the index at wherever the library actually landed.
 *
 * That second one rewrites every path in the index and renames every
 * thumbnail, so it is deliberately two presses: a check that changes nothing
 * and reports what it found, and only then a move. The button that does the
 * moving stays disabled until a check has been run against the same two
 * folders — not to be clever, but because the failure this prevents is
 * somebody rerooting to a typo and having no obvious way back.
 */

import { reportUnauthorized } from './api.js';
import * as i18n from './i18n.js';

const $ = (sel) => document.querySelector(sel);

const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
};

async function json(url, options = {}) {
  const response = await fetch(url, {
    headers: {
      Accept: 'application/json',
      ...(options.body ? { 'Content-Type': 'application/json' } : {}),
    },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const text = await response.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = null; }
  if (!response.ok) {
    if (response.status === 401) reportUnauthorized(url);
    throw new Error(data?.error || response.statusText);
  }
  return data;
}

function bytes(n) {
  if (!n) return '';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let value = n;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
  return `${value >= 10 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
}

export class MigrationPanel {
  constructor({ toast }) {
    this.toast = toast || (() => {});
    this.visible = false;
    this.data = null;
    // The last check, and what it was a check of. A move is only offered for
    // exactly the pair that was checked.
    this.checked = null;
    this.report = null;       // the last report on screen, to redraw in another language
    i18n.onChange(() => {
      if (!this.visible) return;
      this.render();
      if (this.report) this.showReport(...this.report);
    });
  }

  show() {
    this.visible = true;
    this.wire();
    this.refresh();
  }

  hide() { this.visible = false; }

  wire() {
    if (this.wired) return;
    this.wired = true;
    $('#mg-measure')?.addEventListener('click', () => this.refresh({ sizes: true }));
    $('#mg-check')?.addEventListener('click', () => this.reroot(true));
    $('#mg-apply')?.addEventListener('click', () => this.reroot(false));
    for (const id of ['#mg-from', '#mg-to']) {
      // Editing either folder invalidates the check: the button must never
      // apply a move to a pair nobody has looked at.
      $(id)?.addEventListener('input', () => {
        this.checked = null;
        $('#mg-apply').disabled = true;
      });
    }
  }

  async refresh({ sizes = false } = {}) {
    const note = $('#mg-measure');
    if (sizes && note) { note.disabled = true; note.textContent = i18n.t('Measuring…'); }
    try {
      this.data = await json(`/api/admin/migration${sizes ? '?sizes=1' : ''}`);
      this.render();
    } catch (error) {
      this.toast(error.message, true);
    } finally {
      if (sizes && note) { note.disabled = false; note.textContent = i18n.t('Measure again'); }
    }
  }

  render() {
    const data = this.data;
    if (!data) return;

    const list = $('#mg-pieces');
    list.replaceChildren();
    for (const piece of data.pieces) {
      const row = el('div', 'mg-piece');
      const head = el('div', 'mg-piece-head');
      head.append(el('strong', null, i18n.t(piece.label)));
      if (piece.present === false) {
        head.append(el('span', 'mg-flag bad', i18n.t('not on this machine')));
      } else if (piece.bytes) {
        head.append(el('span', 'mg-flag', piece.files === 1
          ? i18n.t('{size} · 1 file', { size: bytes(piece.bytes) })
          : i18n.t('{size} · {count} files', { size: bytes(piece.bytes), count: piece.files.toLocaleString() })));
      }
      row.append(head);
      row.append(el('code', 'mg-path', piece.path));
      row.append(el('div', 'hint subtle', i18n.t(piece.what)));
      if (piece.note) row.append(el('div', 'hint subtle', i18n.t(piece.note)));
      list.append(row);
    }

    // The folders the index records, which is what a reroot is given — not
    // the ones in the settings. After a move those two disagree, and it is
    // the index that has the rows pointing at it.
    const roots = $('#mg-roots');
    roots.replaceChildren();
    for (const root of data.recorded_roots) {
      const option = document.createElement('option');
      option.value = root;
      roots.append(option);
    }

    const missing = $('#mg-missing');
    missing.replaceChildren();
    missing.hidden = !data.missing_roots.length;
    if (data.missing_roots.length) {
      const box = el('div', 'mg-alert');
      box.append(el('strong', null, data.missing_roots.length === 1
        ? i18n.t('One library folder in the index is not on this machine.')
        : i18n.t('{count} library folders in the index are not on this machine.', { count: data.missing_roots.length })));
      for (const root of data.missing_roots) box.append(el('code', 'mg-path', root));
      box.append(el('div', 'hint subtle',
        i18n.t('That is what this page is for — unless the drive is simply unplugged, in which case plug it in rather than rerooting.')));
      missing.append(box);
      // Pre-fill, since it is almost certainly what they came here to fix.
      if (!$('#mg-from').value) $('#mg-from').value = data.missing_roots[0];
    }
  }

  async reroot(dryRun) {
    const from = $('#mg-from').value.trim();
    const to = $('#mg-to').value.trim();
    if (!from || !to) {
      this.toast(i18n.t('Give the folder it is recorded as, and the folder it is at now.'), true);
      return;
    }
    if (!dryRun) {
      const checked = this.checked;
      if (!checked || checked.from !== from || checked.to !== to) {
        this.toast(i18n.t('Check it first.'), true);
        return;
      }
      if (!window.confirm([
        i18n.t('Point the index at {folder}?', { folder: to }),
        i18n.t('{items} items will be repointed and {thumbnails} thumbnails renamed.', {
          items: checked.items.toLocaleString(), thumbnails: checked.renamed.toLocaleString() }),
        i18n.t('Indexing stands down while this runs.'),
      ].join('\n\n'))) return;
    }

    const buttons = [$('#mg-check'), $('#mg-apply')];
    buttons.forEach((b) => { b.disabled = true; });
    const report = $('#mg-report');
    report.hidden = false;
    this.report = null;
    report.replaceChildren(el('div', 'hint', dryRun ? i18n.t('Checking…') : i18n.t('Moving…')));

    try {
      const result = await json('/api/admin/migration/reroot', {
        method: 'POST',
        body: { from, to, dry_run: dryRun },
      });
      this.showReport(result, dryRun);
      if (dryRun) {
        this.checked = { from, to, items: result.items, renamed: result.renamed };
      } else {
        this.checked = null;
        this.toast(i18n.t('The library has been moved.'));
        await this.refresh();
      }
    } catch (error) {
      report.replaceChildren(el('div', 'mg-alert bad', error.message));
      this.checked = null;
    } finally {
      $('#mg-check').disabled = false;
      $('#mg-apply').disabled = !this.checked;
    }
  }

  showReport(result, dryRun) {
    this.report = [result, dryRun];
    const report = $('#mg-report');
    report.replaceChildren();
    report.append(el('strong', null, dryRun
      ? i18n.t('Nothing has been changed. Here is what would happen:')
      : i18n.t('Done.')));

    const renamed = result.renamed.toLocaleString();
    const lines = [
      i18n.t('{count} items are recorded under {folder}.', { count: result.items.toLocaleString(), folder: result.old_root }),
      dryRun ? i18n.t('{count} thumbnails would be renamed.', { count: renamed })
        : i18n.t('{count} thumbnails were renamed.', { count: renamed }),
    ];
    if (result.without_thumbnails) {
      lines.push(i18n.t('{count} items have no thumbnail to rename, or had already been moved.', {
        count: result.without_thumbnails.toLocaleString() }));
    }
    for (const line of lines) report.append(el('div', 'hint', line));

    for (const warning of result.warnings || []) {
      report.append(el('div', 'mg-alert', warning));
    }
    for (const what of result.stopped || []) {
      report.append(el('div', 'hint subtle', i18n.t('{what} was stopped for this.', { what })));
    }
    if (!dryRun) {
      const rows = Object.entries(result.rows || {});
      if (rows.length) {
        report.append(el('div', 'hint subtle',
          rows.map(([name, n]) => `${name}: ${n.toLocaleString()}`).join(' · ')));
      }
      report.append(el('div', 'hint',
        i18n.t('Indexing is running again. Restart Ninaivu when convenient so that everything reads the new folder from the settings rather than from memory.')));
    }
  }
}
