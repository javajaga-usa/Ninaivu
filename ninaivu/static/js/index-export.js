/**
 * The whole index, out of this computer and into another one
 * (storage/full_export.py). One block on the Migration page.
 *
 * Exporting runs in the background while Ninaivu goes on; the block polls
 * only while it does. Importing is two presses, like the reroot below it: a
 * check that reads what the file holds and changes nothing, and only then an
 * import, which Ninaivu does as it restarts.
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

async function json(url, body) {
  const response = await fetch(url, {
    method: body === undefined ? 'GET' : 'POST',
    headers: { Accept: 'application/json', ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401) reportUnauthorized(url);
    throw new Error(i18n.fromServer(data, response.statusText));
  }
  return data;
}

function bytes(n) {
  if (!n) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let value = n;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
  return `${value >= 10 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
}

const when = (value) => {
  if (!value) return '';
  const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString(i18n.locale());
};

const STEPS = {
  copying: i18n.key('Copying the index and the thumbnails…'),
  checking: i18n.key('Writing down a checksum for every file…'),
  packing: i18n.key('Packing it into one file…'),
};

export class IndexExport {
  constructor({ toast, pickFolder } = {}) {
    this.toast = toast || (() => {});
    this.pickFolder = pickFolder || null;
    this.state = null;
    // The last check, and the path it was a check of: an import is only
    // offered for exactly the file that was checked.
    this.looked = null;
    this.timer = 0;
    i18n.onChange(() => { if (this.state) this.render(); });
  }

  wire() {
    if (this.wired) return;
    this.wired = true;
    $('#ix-export')?.addEventListener('click', () => this.exportNow());
    $('#ix-look')?.addEventListener('click', () => this.look());
    $('#ix-import')?.addEventListener('click', () => this.importNow());
    $('#ix-path')?.addEventListener('input', () => {
      this.looked = null;
      $('#ix-import').disabled = true;
    });
    for (const [button, input, title] of [
      ['#ix-browse', '#ix-folder', i18n.t('Choose where the export goes')],
      ['#ix-pick', '#ix-path', i18n.t('Choose the folder the export is in')],
    ]) {
      const node = $(button);
      if (!node) continue;
      node.hidden = !this.pickFolder;
      node.addEventListener('click', () => this.pickFolder?.({
        title,
        cta: i18n.t('Use this folder'),
        anyFolder: true,
        start: $(input).value.trim(),
        pick: (path) => { $(input).value = path; $(input).dispatchEvent(new Event('input')); },
      }));
    }
  }

  show() {
    this.wire();
    this.refresh();
  }

  hide() { clearTimeout(this.timer); }

  async refresh() {
    try {
      this.state = await json('/api/admin/migration/export');
    } catch (exc) {
      this.toast(exc.message, true);
      return;
    }
    this.render();
    clearTimeout(this.timer);
    if (this.state.export?.running && $('#ix-block')?.isConnected) {
      this.timer = setTimeout(() => this.refresh(), 2000);
    }
  }

  render() {
    const s = this.state;
    if (!s) return;
    const ex = s.export || {};
    $('#ix-export').disabled = Boolean(ex.running);
    const report = $('#ix-export-report');
    report.replaceChildren();
    if (ex.running) {
      report.append(el('div', 'hint', i18n.t(STEPS[ex.step] || STEPS.copying)));
      if (ex.expected_bytes) {
        report.append(el('div', 'hint subtle', i18n.t('About {size} in all. A large library takes a while; Ninaivu goes on as usual meanwhile.',
          { size: bytes(ex.expected_bytes) })));
      }
    } else if (ex.error) {
      report.append(el('div', 'mg-alert bad', ex.error));
    } else if (ex.bundle) {
      report.append(el('strong', null, i18n.t('Exported.')));
      report.append(el('code', 'mg-path', ex.bundle));
      report.append(el('div', 'hint subtle', i18n.t('{size}, finished {when}.', { size: bytes(ex.bytes), when: when(ex.finished) })));
    }
    report.hidden = !report.children.length;

    const imported = $('#ix-import-report');
    if (!this.looked) {
      imported.replaceChildren();
      if (s.pending) {
        const box = el('div', 'mg-alert');
        box.append(el('strong', null, i18n.t('An import is waiting for Ninaivu to restart.')));
        box.append(el('code', 'mg-path', s.pending.bundle));
        if (s.restart_problem) box.append(el('div', 'hint subtle', i18n.t('Restart Ninaivu to do it.')));
        const cancel = el('button', 'btn small ghost', i18n.t('Do not import it'));
        cancel.type = 'button';
        cancel.onclick = () => this.cancel();
        box.append(cancel);
        imported.append(box);
      } else if (s.last_import) {
        const last = s.last_import;
        imported.append(last.ok
          ? el('div', 'hint', i18n.t('Imported {when} from {file}.', { when: when(last.at), file: last.bundle }))
          : el('div', 'mg-alert bad', i18n.t('The import on {when} did not happen: {reason} The index that was here is still in use.',
            { when: when(last.at), reason: last.error || '' })));
      }
      imported.hidden = !imported.children.length;
    }
  }

  async exportNow() {
    const folder = $('#ix-folder').value.trim();
    if (!folder) {
      this.toast(i18n.t('Give the folder to write the export to.'), true);
      return;
    }
    $('#ix-export').disabled = true;
    try {
      this.state = await json('/api/admin/migration/export', {
        folder, without_secrets: $('#ix-without-secrets').checked,
      });
    } catch (exc) {
      this.toast(exc.message, true);
      $('#ix-export').disabled = false;
      return;
    }
    this.render();
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.refresh(), 2000);
  }

  async look() {
    const path = $('#ix-path').value.trim();
    if (!path) {
      this.toast(i18n.t('Give the export file, or the folder it is in.'), true);
      return;
    }
    const report = $('#ix-import-report');
    report.hidden = false;
    report.replaceChildren(el('div', 'hint', i18n.t('Reading it…')));
    $('#ix-import').disabled = true;
    try {
      const found = await json('/api/admin/migration/import/look', { path });
      this.looked = { path, found };
      this.showLook(found);
      $('#ix-import').disabled = false;
    } catch (exc) {
      this.looked = null;
      report.replaceChildren(el('div', 'mg-alert bad', exc.message));
    }
  }

  showLook(found) {
    const report = $('#ix-import-report');
    report.replaceChildren();
    report.append(el('strong', null, i18n.t('Nothing has been changed. This is what would come in:')));
    report.append(el('code', 'mg-path', found.bundle));
    report.append(el('div', 'hint', i18n.t('Made by Ninaivu {version} on {when}, {size}.',
      { version: found.version || '?', when: when(found.created_at), size: bytes(found.bytes) })));
    const c = found.counts || {};
    if (c.items != null) {
      report.append(el('div', 'hint', i18n.t('{items} items, {people} named people, {albums} albums, {stories} stories, {profiles} profiles.', {
        items: (c.items || 0).toLocaleString(), people: (c.people || 0).toLocaleString(),
        albums: (c.albums || 0).toLocaleString(), stories: (c.stories || 0).toLocaleString(),
        profiles: (c.profiles || 0).toLocaleString(),
      })));
    }
    report.append(el('div', 'hint', found.full
      ? i18n.t('A full export: {count} thumbnails come with it.', { count: (found.thumbnails || 0).toLocaleString() })
      : i18n.t('A backup, not a full export: the thumbnails, face crops and films are made again here after the import.')));
    if (found.left_out?.length) {
      report.append(el('div', 'hint subtle', i18n.t('The keys and certificates were left out of it; this computer keeps its own.')));
    }
    report.append(el('div', 'mg-alert', i18n.t('Importing replaces everything this computer’s Ninaivu knows. The index that is here now is kept beside it, in case.')));
  }

  async importNow() {
    const path = $('#ix-path').value.trim();
    if (!this.looked || this.looked.path !== path) {
      this.toast(i18n.t('Check it first.'), true);
      return;
    }
    if (!window.confirm([
      i18n.t('Import {file} and restart Ninaivu?', { file: this.looked.found.name }),
      i18n.t('Everybody using Ninaivu is interrupted while it restarts.'),
    ].join('\n\n'))) return;
    $('#ix-import').disabled = true;
    let result;
    try {
      result = await json('/api/admin/migration/import', { path });
    } catch (exc) {
      this.toast(exc.message, true);
      $('#ix-import').disabled = false;
      return;
    }
    this.looked = null;
    this.state = result;
    this.render();
    this.toast(result.restarting
      ? i18n.t('Ninaivu is restarting to import it. Sign in again in a minute or two.')
      : i18n.t('The import will be done when Ninaivu next starts. Restart it to do it now.'));
  }

  async cancel() {
    try {
      this.state = await json('/api/admin/migration/import/cancel', {});
    } catch (exc) {
      this.toast(exc.message, true);
      return;
    }
    this.render();
  }
}
