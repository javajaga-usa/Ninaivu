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
  verifying: i18n.key('Reading every file back against its checksum…'),
  packing: i18n.key('Packing it into one file…'),
};

/** 75 as "1 min 15 s": how long something took, or has been running. */
function took(seconds) {
  const s = Math.max(0, Math.round(Number(seconds) || 0));
  if (s < 60) return i18n.t('{seconds} s', { seconds: s });
  const m = Math.floor(s / 60);
  if (m < 60) return i18n.t('{minutes} min {seconds} s', { minutes: m, seconds: s % 60 });
  return i18n.t('{hours} h {minutes} min', { hours: Math.floor(m / 60), minutes: m % 60 });
}

const count = (n) => Number(n || 0).toLocaleString(i18n.locale());

/** Seconds between two polls while something is running, and while
 *  waiting for Ninaivu to come back from an import. */
const POLL_MS = 1000;
const RESTART_POLL_MS = 3000;

export class IndexExport {
  constructor({ toast, pickFolder } = {}) {
    this.toast = toast || (() => {});
    this.pickFolder = pickFolder || null;
    this.state = null;
    // The last check, and the path it was a check of: an import is only
    // offered for exactly the file that was checked.
    this.looked = null;
    this.timer = 0;
    // An import asked for with a restart: when it was asked, so the page can
    // say it is waiting and then show that import's result, not an older one.
    this.importing = null;
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
    clearTimeout(this.timer);
    try {
      this.state = await json('/api/admin/migration/export');
    } catch (exc) {
      if (this.importing) {
        // Ninaivu is down while it imports: keep saying so, and keep asking.
        this.renderImporting();
        this.timer = setTimeout(() => this.refresh(), RESTART_POLL_MS);
      } else {
        this.toast(exc.message, true);
      }
      return;
    }
    const last = this.state.last_import;
    if (this.importing && last && last.at >= this.importing.asked && !this.state.pending) {
      this.importing = null;
    }
    this.render();
    if (!$('#ix-block')?.isConnected) return;
    if (this.state.export?.running) this.timer = setTimeout(() => this.refresh(), POLL_MS);
    else if (this.importing) this.timer = setTimeout(() => this.refresh(), RESTART_POLL_MS);
  }

  /** The import report while Ninaivu restarts to do it. */
  renderImporting() {
    const report = $('#ix-import-report');
    report.hidden = false;
    const box = el('div', 'mg-alert');
    box.append(el('strong', null, i18n.t('Importing: Ninaivu is restarting to put the export in place.')));
    box.append(el('code', 'mg-path', this.importing.bundle));
    box.append(el('div', 'hint', i18n.t('Waiting for it to answer again, {time} so far. A large export takes a few minutes; this page checks every few seconds and shows the result here.',
      { time: took((Date.now() - this.importing.since) / 1000) })));
    report.replaceChildren(box);
  }

  render() {
    const s = this.state;
    if (!s) return;
    const ex = s.export || {};
    $('#ix-export').disabled = Boolean(ex.running);
    const report = $('#ix-export-report');
    report.replaceChildren();
    if (ex.running) {
      const head = el('strong', null, ex.step_number
        ? i18n.t('Exporting, step {step} of {steps}: {what}', {
          step: ex.step_number, steps: ex.steps, what: i18n.t(STEPS[ex.step] || STEPS.copying) })
        : i18n.t(STEPS[ex.step] || STEPS.copying));
      report.append(head);
      const bar = el('progress', 'ix-bar');
      const total = ex.bytes_total || ex.files_total || 0;
      const done = ex.bytes_total ? ex.bytes_done : ex.files_done;
      bar.max = Math.max(1, total);
      bar.value = Math.min(done || 0, bar.max);
      report.append(bar);
      if (ex.files_total) {
        const percent = total ? Math.floor((100 * (done || 0)) / total) : 0;
        report.append(el('div', 'hint', i18n.t('{done} of {total} files · {size_done} of {size_total} · {percent}%', {
          done: count(ex.files_done), total: count(ex.files_total),
          size_done: bytes(ex.bytes_done), size_total: bytes(ex.bytes_total), percent })));
      }
      report.append(el('div', 'hint subtle', i18n.t('Running for {time}, into {folder}. Ninaivu goes on as usual meanwhile.',
        { time: took(ex.seconds), folder: ex.folder || '' })));
    } else if (ex.finished && !ex.ok) {
      report.append(el('div', 'mg-alert bad', i18n.t('The export on {when} failed after {time}: {reason}',
        { when: when(ex.finished), time: took(ex.seconds), reason: ex.error || '' })));
    } else if (ex.bundle) {
      report.append(el('strong', null, i18n.t('Exported {when}, in {time}.', { when: when(ex.finished), time: took(ex.seconds) })));
      report.append(el('code', 'mg-path', ex.bundle));
      report.append(el('div', 'hint subtle', ex.files_total
        ? i18n.t('{size} in one file, {count} files in it.', { size: bytes(ex.bytes), count: count(ex.files_total) })
        : i18n.t('{size} in one file.', { size: bytes(ex.bytes) })));
    } else {
      report.append(el('div', 'hint subtle', i18n.t('No export has been made on this computer yet.')));
    }
    report.hidden = !report.children.length;

    const imported = $('#ix-import-report');
    if (this.importing) {
      this.renderImporting();
    } else if (!this.looked) {
      imported.replaceChildren();
      if (s.pending) {
        const box = el('div', 'mg-alert');
        box.append(el('strong', null, i18n.t('An import is waiting for Ninaivu to restart.')));
        box.append(el('code', 'mg-path', s.pending.bundle));
        box.append(el('div', 'hint subtle', i18n.t('Asked for {when}.', { when: when(s.pending.asked_at) })));
        if (s.restart_problem) box.append(el('div', 'hint subtle', i18n.t('Restart Ninaivu to do it.')));
        const cancel = el('button', 'btn small ghost', i18n.t('Do not import it'));
        cancel.type = 'button';
        cancel.onclick = () => this.cancel();
        box.append(cancel);
        imported.append(box);
      } else if (s.last_import) {
        const last = s.last_import;
        if (last.ok) {
          imported.append(el('strong', null, last.seconds != null
            ? i18n.t('Imported {when}, in {time}.', { when: when(last.at), time: took(last.seconds) })
            : i18n.t('Imported {when}.', { when: when(last.at) })));
          imported.append(el('code', 'mg-path', last.bundle));
          const c = last.counts || {};
          if (c.items != null) {
            imported.append(el('div', 'hint', i18n.t('{items} items, {people} named people, {albums} albums, {stories} stories, {profiles} profiles.', {
              items: count(c.items), people: count(c.people), albums: count(c.albums),
              stories: count(c.stories), profiles: count(c.profiles) })));
          }
          if (last.full) {
            imported.append(el('div', 'hint subtle', i18n.t('A full export: {count} thumbnails came with it.', { count: count(last.thumbnails) })));
          }
        } else {
          imported.append(el('div', 'mg-alert bad', i18n.t('The import on {when} did not happen: {reason} The index that was here is still in use.',
            { when: when(last.at), reason: last.error || '' })));
        }
      } else {
        imported.append(el('div', 'hint subtle', i18n.t('Nothing has been imported on this computer yet.')));
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
    this.timer = setTimeout(() => this.refresh(), POLL_MS);
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
    if (result.restarting) {
      this.importing = { bundle: result.pending?.bundle || path, asked: result.pending?.asked_at || Date.now() / 1000, since: Date.now() };
      clearTimeout(this.timer);
      this.timer = setTimeout(() => this.refresh(), RESTART_POLL_MS);
    }
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
