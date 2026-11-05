/**
 * The whole index, out of this computer and into another one
 * (storage/full_export.py). One block on the Migration page.
 *
 * Exporting runs in the background while Ninaivu goes on; the block polls
 * only while it does. Importing is two presses, like the reroot below it: a
 * check that reads what the file holds, then reads every file in it through
 * and changes nothing, and only then an import, which Ninaivu does as it
 * restarts. Each press says at once what is happening, before the server
 * has answered.
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
    const error = new Error(i18n.fromServer(data, response.statusText));
    // It answered: while an import restarts Ninaivu, that alone says it is back.
    error.status = response.status;
    throw error;
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
  preparing: i18n.key('Preparing: measuring what goes in and checking it fits…'),
  copying: i18n.key('Copying the index and the thumbnails…'),
  checking: i18n.key('Writing down a checksum for every file…'),
  verifying: i18n.key('Reading every file back against its checksum…'),
  packing: i18n.key('Packing it into one file…'),
};

const CHECK_STEPS = {
  reading: i18n.key('Reading the list of what is in it…'),
  checking: i18n.key('Reading every file in it against its checksum…'),
};

/** A finished job's error, in this page's language when the server sent
 *  the sentence with its values (words.Said). */
const reason = (job) => i18n.fromServer({ said: job.said, params: job.params, error: job.error || '' });

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
/** How often to keep asking once Ninaivu is overdue: it may still come back. */
const OVERDUE_POLL_MS = 10000;
/** How long to wait for an import's restart when the server did not say. */
const DEFAULT_PATIENCE_S = 5 * 60;

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
    // A move asked for with a restart: Ninaivu is down while it copies and
    // checks the index, and the page waits for it and then shows the result.
    this.moving = null;
    // The same for a check of the index asked for with a restart.
    this.checking = null;
    i18n.onChange(() => { if (this.state) this.render(); });
  }

  wire() {
    if (this.wired) return;
    this.wired = true;
    $('#index-move-start')?.addEventListener('click', () => this.moveIndex());
    $('#index-recovery-cleanup')?.addEventListener('click', () => this.cleanRecovery());
    $('#index-check-start')?.addEventListener('click', () => this.checkIndex());
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
      if (this.moving || this.checking) {
        // Down while it moves or checks the index: any answer is it back.
        if (exc.status) (this.moving || this.checking).back = true;
        this.renderMove(this.state || {});
        this.renderCheck(this.state || {});
        this.timer = setTimeout(() => this.refresh(), RESTART_POLL_MS);
      } else if (this.importing) {
        // Ninaivu is down while it imports: keep saying so, and keep asking.
        // An answer of any kind (a sign-in asked for, the imported index
        // knowing nothing of this session) is Ninaivu back; no answer for
        // longer than the import should take is Ninaivu not coming back, and
        // the page says so rather than counting on for ever.
        if (exc.status) this.importing.back = true;
        this.renderImporting();
        this.timer = setTimeout(() => this.refresh(), this.overdue() ? OVERDUE_POLL_MS : RESTART_POLL_MS);
      } else {
        this.toast(exc.message, true);
      }
      return;
    }
    if (this.moving && !this.state.index_move_pending) {
      this.moving = null;            // back, and the move is done or refused
    } else if (this.moving && !this.state.restarting && Date.now() - this.moving.since > 15000) {
      // Still answering with the move waiting, and nothing restarting it.
      this.moving = null;
      this.moveRestartFailed = true;
    }
    if (this.checking && !this.state.index_check?.wanted) {
      this.checking = null;          // back, and checked
    } else if (this.checking && !this.state.restarting && Date.now() - this.checking.since > 15000) {
      this.checking = null;
      this.checkRestartFailed = true;
    }
    const last = this.state.last_import;
    if (this.importing && last && last.at >= this.importing.asked && !this.state.pending) {
      this.importing = null;
    } else if (this.importing && this.state.pending && !this.state.restarting) {
      // This server is still answering, the import still waits, and the
      // helper that was to restart it has given up: no restart is coming.
      this.importing = null;
      this.restartFailed = true;
    }
    this.render();
    if (!$('#ix-block')?.isConnected) return;
    if (this.state.export?.running || this.state.check?.running || this.state.recovery_cleanup?.running) this.timer = setTimeout(() => this.refresh(), POLL_MS);
    else if (this.importing || this.moving || this.checking) this.timer = setTimeout(() => this.refresh(), RESTART_POLL_MS);
  }

  /** Whether Ninaivu has been gone longer than this import should take. */
  overdue() {
    const imp = this.importing;
    return Boolean(imp && !imp.back && Date.now() - imp.since > 1000 * (imp.patience || DEFAULT_PATIENCE_S));
  }

  /** The import report while Ninaivu restarts to do it. */
  renderImporting() {
    const report = $('#ix-import-report');
    report.hidden = false;
    const gone = took((Date.now() - this.importing.since) / 1000);
    if (this.importing.back) {
      const box = el('div', 'mg-alert');
      box.append(el('strong', null, i18n.t('Ninaivu is back. Sign in again to see how the import went.')));
      box.append(el('code', 'mg-path', this.importing.bundle));
      report.replaceChildren(box);
      return;
    }
    if (this.overdue()) {
      const box = el('div', 'mg-alert bad');
      box.append(el('strong', null, i18n.t('Ninaivu has not come back after {time}. It may have stopped.', { time: gone })));
      box.append(el('code', 'mg-path', this.importing.bundle));
      box.append(el('div', 'hint', i18n.t('Start Ninaivu again on that computer, the way it is usually started (the Control Panel, or the launcher). If the import was done, it starts with the imported index; if not, with the one that was there.')));
      if (this.importing.restartLog) {
        box.append(el('div', 'hint subtle', i18n.t('What the restart did is in {path}, and what Ninaivu said as it started is in {log}, on that computer.',
          { path: this.importing.restartLog, log: this.importing.serverLog || '' })));
      }
      box.append(el('div', 'hint subtle', i18n.t('This page keeps checking and shows the result if it comes back.')));
      report.replaceChildren(box);
      return;
    }
    const box = el('div', 'mg-alert');
    box.append(el('strong', null, i18n.t('Importing: Ninaivu is restarting to put the export in place.')));
    box.append(el('code', 'mg-path', this.importing.bundle));
    box.append(el('progress', 'ix-bar'));
    if (this.importing.size) {
      box.append(el('div', 'hint', i18n.t('It unpacks {size}, checks every file again, and then puts them in place.',
        { size: bytes(this.importing.size) })));
    }
    box.append(el('div', 'hint', i18n.t('Waiting for it to answer again, {time} so far. A large export takes a few minutes; this page checks every few seconds and shows the result here.',
      { time: gone })));
    report.replaceChildren(box);
  }

  render() {
    const s = this.state;
    if (!s) return;
    this.renderMove(s);
    this.renderCheck(s);
    const cleanup = s.recovery_cleanup || {};
    const cleanupButton = $('#index-recovery-cleanup');
    if (cleanupButton) {
      cleanupButton.hidden = !s.index_move?.ok || !s.index_move?.recovery_files;
      cleanupButton.disabled = Boolean(cleanup.running || s.index_move_pending);
    }
    const cleanupReport = $('#index-recovery-report');
    if (cleanupReport && (cleanup.running || cleanup.removed != null || cleanup.error)) {
      cleanupReport.textContent = cleanup.error || (cleanup.running
        ? i18n.t('Cleaning the recovery copy…')
        : i18n.t('Recovery cleanup finished. Removed {count} files ({bytes}); preserved {kept} changed or linked files. The forwarding file is kept.',
          { count: cleanup.removed || 0, bytes: bytes(cleanup.bytes), kept: cleanup.preserved || 0 }));
    }
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
      // No total yet while preparing: a bar that moves without a value.
      if (total) {
        bar.max = Math.max(1, total);
        bar.value = Math.min(done || 0, bar.max);
      }
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
        { when: when(ex.finished), time: took(ex.seconds), reason: reason(ex) })));
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
    } else if (this.looked) {
      this.showLook(this.looked.found);
    } else {
      imported.replaceChildren();
      if (s.pending) {
        const box = el('div', 'mg-alert');
        box.append(el('strong', null, i18n.t('An import is waiting for Ninaivu to restart.')));
        box.append(el('code', 'mg-path', s.pending.bundle));
        box.append(el('div', 'hint subtle', i18n.t('Asked for {when}.', { when: when(s.pending.asked_at) })));
        if (this.restartFailed) {
          box.append(el('div', 'hint', i18n.t('Ninaivu could not restart itself to do it. What the restart did is in {path} on the Ninaivu computer. Restart Ninaivu to do it.',
            { path: s.restart_log || '' })));
        } else if (s.restart_problem) {
          box.append(el('div', 'hint subtle', i18n.t('Restart Ninaivu to do it.')));
        }
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

  async cleanRecovery() {
    const button = $('#index-recovery-cleanup');
    const report = $('#index-recovery-report');
    button.disabled = true;
    report.textContent = i18n.t('Checking the recovery copy…');
    try {
      const preview = await json('/api/admin/migration/recovery-cleanup');
      if (!window.confirm(i18n.t('Remove unchanged recovery files from {path}? Up to {count} files ({bytes}) will be checked. Added or changed files and the forwarding file will stay. Check that your relocated library works before continuing.',
        { path: preview.source, count: preview.files, bytes: bytes(preview.bytes) }))) {
        report.textContent = '';
        return;
      }
      this.state = await json('/api/admin/migration/recovery-cleanup', { confirm: true });
      this.render();
      this.timer = setTimeout(() => this.refresh(), POLL_MS);
    } catch (error) { report.textContent = error.message; }
    finally { button.disabled = Boolean(this.state?.recovery_cleanup?.running); }
  }

  /** Where the index is, and how its move stands. */
  renderMove(s) {
    const loc = s.index_location
      || { folder: s.index_folder, last: s.index_move, pending: s.index_move_pending };
    const location = $('#index-move-current');
    if (location && (loc.folder || s.index_folder)) {
      location.replaceChildren(i18n.t('Current index folder: {path}', { path: loc.folder || s.index_folder }));
      if (loc.free_bytes != null) {
        location.append(' ', i18n.t('({size} free on its drive)', { size: bytes(loc.free_bytes) }));
      }
      if (loc.forwarded_from) {
        location.append(el('div', 'hint subtle', i18n.t('Launchers that name {path} are sent here by the index-location.json file in it. Keep that file.',
          { path: loc.forwarded_from })));
      }
    }
    const button = $('#index-move-start');
    if (button) button.disabled = Boolean(this.moving || loc.pending);
    const report = $('#index-move-report');
    if (!report) return;
    if (this.moving) {
      const box = el('div', 'mg-alert');
      if (this.moving.back) {
        box.append(el('strong', null, i18n.t('Ninaivu is back. Sign in again to see how the move went.')));
      } else {
        box.append(el('strong', null, i18n.t('Ninaivu is restarting to move the index.')));
        box.append(el('code', 'mg-path', this.moving.destination));
        box.append(el('progress', 'ix-bar'));
        box.append(el('div', 'hint', i18n.t('It copies the index, then reads every file back to check it. On a large index this takes a while; the Control Panel on that computer shows how far it has got.')));
        box.append(el('div', 'hint subtle', i18n.t('Waiting for it to answer again, {time} so far. This page checks every few seconds and shows the result here.',
          { time: took((Date.now() - this.moving.since) / 1000) })));
      }
      report.replaceChildren(box);
      return;
    }
    if (loc.pending) {
      const box = el('div', 'mg-alert');
      box.append(el('strong', null, loc.pending.mode === 'switch'
        ? i18n.t('Switching to the index in this folder is scheduled. Restart Ninaivu to complete it.')
        : i18n.t('The move is scheduled. Restart Ninaivu to complete it.')));
      box.append(el('code', 'mg-path', loc.pending.destination));
      if (loc.pending.asked_at) box.append(el('div', 'hint subtle', i18n.t('Asked for {when}.', { when: when(loc.pending.asked_at) })));
      if (this.moveRestartFailed) {
        box.append(el('div', 'hint', i18n.t('Ninaivu could not restart itself to move the index. Restart Ninaivu to do it.')));
      }
      report.replaceChildren(box);
      return;
    }
    const last = loc.last;
    if (!last) {
      if (!this.scheduledNote) report.replaceChildren();
      return;
    }
    if (last.ok && last.switched) {
      const box = el('div');
      box.append(el('strong', null, i18n.t('Switched to this index {when}.', { when: when(last.at) })));
      box.append(el('div', 'hint', i18n.t('The index in {path} is kept as it was. Deleting index-location.json there goes back to it.', { path: last.source || '' })));
      report.replaceChildren(box);
      return;
    }
    if (last.ok) {
      const box = el('div');
      box.append(el('strong', null, i18n.t('The index was moved and verified. The original folder is kept as a recovery copy.')));
      if (last.at) {
        box.append(el('div', 'hint', last.seconds != null
          ? i18n.t('Moved here {when} from {path}, in {time}.', { when: when(last.at), path: last.source || '', time: took(last.seconds) })
          : i18n.t('Moved here {when} from {path}.', { when: when(last.at), path: last.source || '' })));
      }
      if (last.files != null) {
        box.append(el('div', 'hint subtle', i18n.t('{count} files, {size}. {already} were already there and were not copied again.',
          { count: count(last.files), size: bytes(last.bytes), already: count(last.already_there) })));
      }
      if (last.copied_again?.length) {
        box.append(el('div', 'hint', i18n.t('Copied again because the first copy read back differently: {files}',
          { files: last.copied_again.join(', ') })));
      }
      if (loc.recovery_folder && !loc.recovery_present) {
        box.append(el('div', 'hint subtle', i18n.t('The recovery copy has been removed.')));
      }
      report.replaceChildren(box);
      return;
    }
    const box = el('div', 'mg-alert bad');
    box.append(el('strong', null, i18n.t('The index move failed. The original index is still in use.')));
    box.append(document.createTextNode(' ' + (last.error || '')));
    if (last.destination) box.append(el('code', 'mg-path', last.destination));
    if (last.at) box.append(el('div', 'hint subtle', i18n.t('Tried {when}.', { when: when(last.at) })));
    if (last.kept_copy) {
      box.append(el('div', 'hint', i18n.t('What was copied is kept in that folder. Moving there again continues from it instead of starting over.')));
    }
    report.replaceChildren(box);
  }

  /** The last check of the index's databases, and one waiting. */
  renderCheck(s) {
    const report = $('#index-check-report');
    if (!report) return;
    const button = $('#index-check-start');
    const check = s.index_check || {};
    if (button) button.disabled = Boolean(this.checking || this.moving || check.wanted);
    const box = el('div');
    if (this.checking) {
      box.className = 'mg-alert';
      box.append(el('strong', null, this.checking.back
        ? i18n.t('Ninaivu is back. Sign in again to see how the check went.')
        : i18n.t('Ninaivu is restarting to check the index. This page shows the result when it is back.')));
      if (!this.checking.back) box.append(el('progress', 'ix-bar'));
      report.replaceChildren(box);
      return;
    }
    if (check.wanted) {
      box.className = 'mg-alert';
      box.append(el('strong', null, i18n.t('A check of the index is waiting for Ninaivu to restart.')));
      if (check.wanted.reason === 'error') {
        box.append(el('div', 'hint', i18n.t('Ninaivu saw an error that says a database is damaged ({when}):', { when: when(check.wanted.at) })));
        box.append(el('code', 'mg-path', check.wanted.detail || ''));
      } else if (check.wanted.at) {
        box.append(el('div', 'hint subtle', i18n.t('Asked for {when}.', { when: when(check.wanted.at) })));
      }
      if (this.checkRestartFailed) box.append(el('div', 'hint', i18n.t('Restart Ninaivu to do it.')));
      report.replaceChildren(box);
      return;
    }
    const last = check.last;
    if (!last) { report.replaceChildren(); return; }
    const dbs = last.databases || [];
    const failed = dbs.filter((d) => !d.ok);
    const repaired = dbs.filter((d) => d.ok && d.how);
    if (failed.length) {
      box.className = 'mg-alert bad';
      box.append(el('strong', null, i18n.t('Checked {when}: {names} could not be repaired, and is as it was. Restore it from a backup on the Backup & health page.',
        { when: when(last.at), names: failed.map((d) => d.name).join(', ') })));
      for (const d of failed) box.append(el('code', 'mg-path', `${d.name}: ${(d.left || d.problems || []).join('; ')}`));
    } else if (repaired.length) {
      box.className = 'mg-alert';
      box.append(el('strong', null, i18n.t('Checked {when}: repaired {names}.', { when: when(last.at), names: repaired.map((d) => d.name).join(', ') })));
      for (const d of repaired) {
        if (d.how === 'rebuild' && d.skipped) {
          box.append(el('div', 'hint', i18n.t('{name} was rebuilt from what could be read; {count} unreadable stretches were left out. A scan brings back photographs that are still in the library.',
            { name: d.name, count: count(d.skipped) })));
        }
        box.append(el('div', 'hint subtle', i18n.t('The damaged original is kept in {path}. Delete it once Ninaivu works as it should, to free the space.', { path: d.damaged_copy || '' })));
      }
    } else {
      box.className = 'hint';
      box.append(i18n.t('Checked {when}: all sound.', { when: when(last.at) }));
    }
    report.replaceChildren(box);
  }

  async checkIndex() {
    if (!window.confirm(i18n.t('Check the index and restart Ninaivu? Everybody using Ninaivu is interrupted while it restarts.'))) return;
    const button = $('#index-check-start');
    button.disabled = true;
    this.checkRestartFailed = false;
    let result;
    try {
      result = await json('/api/admin/migration/check-index', {});
    } catch (error) {
      $('#index-check-report').textContent = error.message;
      button.disabled = false;
      return;
    }
    if (result.restarting) {
      this.checking = { since: Date.now() };
      this.renderCheck(this.state || {});
      clearTimeout(this.timer);
      this.timer = setTimeout(() => this.refresh(), RESTART_POLL_MS);
    } else {
      $('#index-check-report').textContent = i18n.t('A check of the index is waiting for Ninaivu to restart.') + ' ' + (result.restart_problem || '');
    }
  }

  async moveIndex() {
    const folder = $('#index-move-folder').value.trim();
    if (!folder) { this.toast(i18n.t('Give the new index folder on the Ninaivu computer.'), true); return; }
    if (!window.confirm(i18n.t('Move the index and restart Ninaivu? Everybody using Ninaivu will be interrupted. The original folder is kept as a recovery copy.'))) return;
    const button = $('#index-move-start');
    const report = $('#index-move-report');
    button.disabled = true;
    report.textContent = i18n.t('Scheduling the index move…');
    this.moveRestartFailed = false;
    let result;
    try {
      result = await json('/api/admin/migration/move-index', { folder });
    } catch (error) {
      report.textContent = error.message;
      button.disabled = false;
      return;
    }
    if (result.restarting) {
      this.moving = { since: Date.now(), destination: result.destination || folder };
      this.renderMove(this.state || {});
      clearTimeout(this.timer);
      this.timer = setTimeout(() => this.refresh(), RESTART_POLL_MS);
    } else {
      // Kept on screen until the page is next drawn from the server.
      this.scheduledNote = true;
      report.textContent = i18n.t('The move is scheduled. Restart Ninaivu to complete it.') + ' ' + (result.restart_problem || '');
    }
  }

  async exportNow() {
    const folder = $('#ix-folder').value.trim();
    if (!folder) {
      this.toast(i18n.t('Give the folder to write the export to.'), true);
      return;
    }
    $('#ix-export').disabled = true;
    // Said before the server answers, so the press is never met with nothing.
    const report = $('#ix-export-report');
    report.hidden = false;
    report.replaceChildren(el('strong', null, i18n.t('Preparing the export…')), el('progress', 'ix-bar'));
    try {
      this.state = await json('/api/admin/migration/export', {
        folder, without_secrets: $('#ix-without-secrets').checked,
      });
    } catch (exc) {
      report.replaceChildren(el('div', 'mg-alert bad', exc.message));
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
      if (this.state) this.state.check = found.check;
      this.showLook(found);
      clearTimeout(this.timer);
      this.timer = setTimeout(() => this.refresh(), POLL_MS);
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
    // Import need not wait for the check: it checks every file again as it
    // unpacks, and keeps the index that is here if one does not match. Only
    // a check that has found a problem holds it back.
    const passed = this.showCheck(report, found);
    const check = this.state?.check;
    const failed = Boolean(check && check.bundle === found.bundle && !check.running && !check.ok);
    if (!failed) {
      if (!passed) report.append(el('div', 'hint', i18n.t('You need not wait for the check: Import checks every file again as it puts them in place, and keeps the index that is here if one is not right.')));
      report.append(el('div', 'mg-alert', i18n.t('Importing replaces everything this computer’s Ninaivu knows. The index that is here now is kept beside it, in case.')));
    }
    $('#ix-import').disabled = failed;
  }

  /** How the reading-through of *found* is going; true once it passed. */
  showCheck(report, found) {
    const check = this.state?.check;
    if (!check || check.bundle !== found.bundle) return false;
    if (check.running) {
      report.append(el('strong', null, i18n.t('Checking it, step {step} of {steps}: {what}', {
        step: check.step_number || 1, steps: check.steps || 2,
        what: i18n.t(CHECK_STEPS[check.step] || CHECK_STEPS.reading) })));
      const bar = el('progress', 'ix-bar');
      if (check.bytes_total) {
        bar.max = check.bytes_total;
        bar.value = Math.min(check.bytes_done || 0, bar.max);
      }
      report.append(bar);
      if (check.files_total) {
        const percent = check.bytes_total ? Math.floor((100 * (check.bytes_done || 0)) / check.bytes_total) : 0;
        report.append(el('div', 'hint', i18n.t('{done} of {total} files · {size_done} of {size_total} · {percent}%', {
          done: count(check.files_done), total: count(check.files_total),
          size_done: bytes(check.bytes_done), size_total: bytes(check.bytes_total), percent })));
      }
      report.append(el('div', 'hint subtle', i18n.t('Running for {time}. Nothing is changed while it is checked.',
        { time: took(check.seconds) })));
      return false;
    }
    if (!check.ok) {
      report.append(el('div', 'mg-alert bad', i18n.t('The check found a problem, so it cannot be imported: {reason}', { reason: reason(check) })));
      return false;
    }
    report.append(el('div', 'hint', i18n.t('Checked in {time}: all {count} files are there and as they were written.',
      { time: took(check.seconds), count: count(check.files_done) })));
    return true;
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
    const report = $('#ix-import-report');
    report.hidden = false;
    report.replaceChildren(el('strong', null, i18n.t('Preparing the import…')), el('progress', 'ix-bar'));
    let result;
    try {
      result = await json('/api/admin/migration/import', { path });
    } catch (exc) {
      this.toast(exc.message, true);
      this.showLook(this.looked.found);
      report.append(el('div', 'mg-alert bad', exc.message));
      return;
    }
    this.state = result;
    this.looked = null;
    this.restartFailed = false;
    if (result.restarting) {
      this.importing = {
        bundle: result.pending?.bundle || path,
        asked: result.pending?.asked_at || Date.now() / 1000,
        since: Date.now(),
        size: result.looked?.unpacked_bytes || 0,
        patience: result.patience_seconds || DEFAULT_PATIENCE_S,
        restartLog: result.restart_log || '',
        serverLog: result.server_log || '',
      };
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
      this.restartFailed = false;
    } catch (exc) {
      this.toast(exc.message, true);
      return;
    }
    this.render();
  }
}
