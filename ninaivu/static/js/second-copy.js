/**
 * Mugil → Second copy on another disk (storage/mirror.py).
 *
 * The folder, whether the copy is kept and how often, and the copy itself:
 * Copy now, Check the copy, Stop, and what it has done. Before a folder is
 * used, "Will it fit?" asks what that kind of disk could not keep as it is
 * (storage/fit.py): names it refuses, two names it would take for one,
 * files too large for it, each with a name that would work. Choosing a new
 * folder asks the same by itself. The server does the checking; this only
 * says what it answers.
 *
 * Browse opens the console's folder picker (disks and network shares under
 * /Volumes included), and a folder chosen or typed is described at once
 * (storage/mirror_place.py): there or not, writable, room free and needed,
 * a network share, on the same disk as the library, holding a copy already.
 */

import { reportUnauthorized } from './api.js';
import * as i18n from './i18n.js';
import { percent, renderStatus } from './status-card.js';

const $ = (sel) => document.querySelector(sel);

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
  const data = text ? JSON.parse(text) : null;
  if (!response.ok) {
    if (response.status === 401) reportUnauthorized(url);
    throw Object.assign(new Error(i18n.fromServer(data, response.statusText)),
      { status: response.status, data });
  }
  return data;
}

const size = (n) => {
  const value = Number(n) || 0;
  if (value < 1024) return `${value} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let amount = value / 1024;
  let unit = 0;
  while (amount >= 1024 && unit < units.length - 1) { amount /= 1024; unit += 1; }
  return `${amount < 10 ? amount.toFixed(1) : Math.round(amount)} ${units[unit]}`;
};

const count = (n) => (Number(n) || 0).toLocaleString(i18n.locale());

const when = (seconds) => (seconds
  ? new Date(seconds * 1000).toLocaleString(i18n.locale(),
    { dateStyle: 'medium', timeStyle: 'short' })
  : '');

const PROBLEM = {
  too_large: () => i18n.t('too large for this disk'),
  forbidden_character: () => i18n.t('a character this disk does not take'),
  trailing_dot_or_space: () => i18n.t('ends in a dot or a space'),
  reserved_name: () => i18n.t('a name this disk keeps for itself'),
  name_too_long: () => i18n.t('a name too long for this disk'),
  same_name_different_case: () => i18n.t('this disk sees it as the same name as another file'),
  same_name_different_spelling: () => i18n.t('this disk sees it as the same name as another file'),
};

/** How many problems are listed on the page; the counts are always whole. */
const LISTED = 20;

export class SecondCopy {
  constructor({ toast, pickFolder = null }) {
    this.toast = toast || (() => {});
    this.pickFolder = pickFolder;
    this.about = null;
    this.timer = null;
    this.visible = false;
    this.status = null;
    this.fit = null;
    i18n.onChange(() => {
      if (this.status) this.render(this.status);
      if (this.fit) this.renderFit(this.fit);
      if (this.about) this.renderAbout(this.about);
    });
  }

  wire() {
    if (!$('#sc-block')) return;
    $('#sc-check').onclick = () => this.check();
    const browse = $('#sc-browse');
    if (browse) {
      browse.hidden = !this.pickFolder;
      browse.onclick = () => this.pickFolder?.({
        title: i18n.t('Choose where the second copy is kept'),
        cta: i18n.t('Use this folder'),
        anyFolder: true,
        start: $('#sc-folder').value.trim() || this.status?.folder || '/Volumes',
        pick: (path) => {
          $('#sc-folder').value = path;
          this.describe(path);
        },
      });
    }
    $('#sc-folder').addEventListener('change', () => this.describe($('#sc-folder').value.trim()));
    const afresh = $('#sc-afresh');
    if (afresh) {
      afresh.onclick = () => {
        if (!window.confirm(i18n.t('Copy the whole library to this share from the beginning? Only do this if the disk behind the share really is a new one.'))) return;
        this.act(() => json('/api/mirror/afresh', { method: 'POST' }),
          i18n.t('Copying to the other disk'));
      };
    }
    $('#sc-save').onclick = () => this.save();
    $('#sc-start').onclick = () => this.act(() => json('/api/mirror/start', { method: 'POST' }),
      i18n.t('Copying to the other disk'));
    $('#sc-verify').onclick = () => this.act(() => json('/api/mirror/verify', { method: 'POST' }),
      i18n.t('Checking every file on the copy'));
    $('#sc-stop').onclick = () => this.act(() => json('/api/mirror/stop', { method: 'POST' }));
  }

  show() { this.visible = true; this.refresh(); }

  hide() { this.visible = false; clearTimeout(this.timer); this.timer = null; }

  async refresh() {
    clearTimeout(this.timer);
    try {
      this.render(await json('/api/mirror/status'));
    } catch (exc) {
      if (exc.status === 404 || exc.status === 403) { $('#sc-block').hidden = true; return; }
      if (exc.status !== 401 && exc.status !== 423) this.toast(exc.message, true);
    }
    this.schedule();
  }

  schedule() {
    clearTimeout(this.timer);
    if (!this.visible || !this.status) return;
    this.timer = setTimeout(() => this.refresh(), this.status.running ? 4000 : 60000);
  }

  async act(call, done) {
    const buttons = [...document.querySelectorAll('#sc-block button')];
    buttons.forEach((b) => { b.disabled = true; });
    try {
      this.render(await call());
      if (done) this.toast(done);
    } catch (exc) {
      this.toast(exc.message, true);
    } finally {
      buttons.forEach((b) => { b.disabled = false; });
    }
    this.schedule();
  }

  /** Say what *folder* is before it is used: storage/mirror_place.py. */
  async describe(folder) {
    if (!folder) { $('#sc-about').hidden = true; this.about = null; return; }
    try {
      this.renderAbout(await json(`/api/mirror/folder?path=${encodeURIComponent(folder)}`));
    } catch (exc) {
      this.toast(exc.message, true);
    }
  }

  renderAbout(about) {
    this.about = about;
    const list = $('#sc-about');
    list.textContent = '';
    const say = (text, warn = false) => {
      const line = document.createElement('li');
      line.textContent = text;
      if (warn) line.className = 'sc-warn';
      list.append(line);
    };
    if (!about.there) {
      say(i18n.fromServer(about.away_said || about.problem || 'That folder is not there. Is the disk plugged in?'), true);
    } else {
      if (about.network) {
        say(i18n.t('A network share ({kind}) at {mount}. If it drops, the copy waits and carries on when it is back.', {
          kind: about.filesystem || '?', mount: about.mount_point || about.folder }));
      }
      if (about.free_bytes !== null && about.free_bytes !== undefined) {
        say(i18n.t('{free} free of {total}; the copy needs about {needed} more.', {
          free: size(about.free_bytes), total: size(about.total_bytes), needed: size(about.needed_bytes) }),
        about.short_of_room);
      }
      if (about.short_of_room) {
        say(i18n.t('There is not room for the whole copy there. It copies what fits and says when the disk is full.'), true);
      }
      say(about.writable ? i18n.t('Ninaivu can write there.') : i18n.t('Ninaivu cannot write to that folder.'),
        !about.writable);
      if (about.same_disk_as) {
        say(i18n.t('This is the same disk as the library ({folder}). A copy there guards against mistakes, not against the disk failing.', {
          folder: about.same_disk_as }), true);
      }
      if (about.holds === 'this') say(i18n.t('The second copy is already in this folder; it carries on where it was.'));
      if (about.holds === 'another') {
        say(i18n.t('This folder holds another second copy. Files already there with the same bytes are kept, not copied again.'));
      }
      if (about.holds === 'library' && about.library_here) {
        say(i18n.t('This folder already holds the library, in the same folders ({found} of {looked} files looked at are there). The first copy compares each file with the library and keeps the ones with the same bytes as they are, writing nothing; only files missing there are written.', {
          found: count(about.library_here.found), looked: count(about.library_here.looked_at) }));
      }
      if (about.problem) say(i18n.fromServer(about.problem), true);
    }
    list.hidden = !list.children.length;
  }

  async check() {
    const folder = $('#sc-folder').value.trim();
    if (!folder) {
      this.toast(i18n.t('Choose a folder to put the files in.'), true);
      return;
    }
    try {
      this.renderFit(await json(`/api/admin/fit?limit=${LISTED}&target=${encodeURIComponent(folder)}`));
    } catch (exc) {
      this.toast(exc.message, true);
    }
  }

  async save() {
    const folder = $('#sc-folder').value.trim();
    const moved = folder !== (this.status?.folder || '');
    await this.act(() => json('/api/mirror/settings', {
      method: 'POST',
      body: { folder, enabled: $('#sc-enabled').checked, every_hours: Number($('#sc-every').value) },
    }), i18n.t('Saved'));
    // A new folder: say at once what that disk could not keep.
    if (moved && folder) await this.check();
  }

  renderFit(fit) {
    this.fit = fit;
    $('#sc-fit').hidden = false;
    const kind = fit.filesystem ? fit.filesystem.toUpperCase() : i18n.t('an unknown kind');
    const problems = Object.values(fit.problems || {}).reduce((a, b) => a + b, 0);
    $('#sc-fit-says').textContent = fit.fits
      ? i18n.t('That disk ({kind}) can keep all {files} files as they are.', {
        kind, files: count(fit.files) })
      : i18n.t('That disk ({kind}) could not keep {count} of the {files} files as they are. The first ones:', {
        kind, count: count(fit.problem_files || problems), files: count(fit.files) });
    const list = $('#sc-problems');
    list.textContent = '';
    for (const item of (fit.listed || []).slice(0, LISTED)) {
      const line = document.createElement('li');
      const why = (PROBLEM[item.problem] || (() => item.problem))();
      line.textContent = item.suggestion
        ? i18n.t('{path}: {why}. It could be called {suggestion}.', { path: item.path, why, suggestion: item.suggestion })
        : i18n.t('{path}: {why}.', { path: item.path, why });
      list.append(line);
    }
    if (!fit.fits) {
      const line = document.createElement('li');
      line.textContent = i18n.t('The second copy writes these under the names shown and lists them in NINAIVU-NAMES.txt at the top of the copy. Files too large for the disk are left out.');
      list.append(line);
    }
    if (fit.coarse_times) {
      const line = document.createElement('li');
      line.textContent = i18n.t('This kind of disk keeps file times to two seconds, so a copy\'s time can differ a little from the original\'s.');
      list.append(line);
    }
  }

  render(status) {
    if (!status) return;
    this.status = status;
    $('#sc-block').hidden = false;
    if (document.activeElement !== $('#sc-folder')) $('#sc-folder').value = status.folder || '';
    $('#sc-enabled').checked = Boolean(status.enabled);
    const every = String(Math.round(status.every_hours || 0));
    $('#sc-every').value = ['24', '168', '0'].includes(every) ? every : '24';
    $('#sc-run-row').hidden = !status.enabled;
    $('#sc-start').hidden = Boolean(status.running);
    $('#sc-verify').hidden = Boolean(status.running);
    $('#sc-stop').hidden = !status.running;
    const afresh = $('#sc-afresh');
    if (afresh) afresh.hidden = !status.copy_missing || Boolean(status.running);
    if (!this.about && status.folder && document.activeElement !== $('#sc-folder')) {
      this.describe(status.folder);
    }
    const card = this.card(status);
    $('#sc-state').textContent = card.state;
    $('#sc-state').dataset.tone = card.tone;
    renderStatus($('#sc-status'), card);
  }

  /** What the second copy is doing, as one card (status-card.js): the state
   * first, then how far, then the facts one to a line. */
  card(status) {
    const job = status.running ? (status.job || 'copying') : '';
    const facts = [];
    const warn = (text) => facts.push({ text, tone: 'warn' });
    const owed = Number(status.owed) || 0;
    const files = Number(status.files) || 0;
    const onCopy = i18n.t('{copied} of {files} files are on the copy ({size}).', {
      copied: count(status.copied), files: count(files), size: size(status.copied_bytes) });
    const wholeBar = files ? {
      value: status.copied, max: files,
      label: i18n.t('{percent}% of the library is on the copy', {
        percent: count(percent(status.copied, files)) }),
    } : null;

    let tone;
    let state;
    let headline;
    let bar = null;
    if (job === 'checking') {
      tone = 'busy';
      state = i18n.t('Checking');
      headline = i18n.t('Reading every file on the copy back and comparing it with its fingerprint.');
      const total = Number(status.to_check) || Number(status.copied) || 0;
      bar = total ? {
        value: status.checked, max: total,
        label: i18n.t('{checked} of {total} files read back', {
          checked: count(status.checked), total: count(total) }),
      } : null;
    } else if (job === 'restoring') {
      tone = 'busy';
      state = i18n.t('Putting back');
      headline = i18n.t('Putting back from the copy what the library no longer has.');
    } else if (job) {
      tone = 'busy';
      state = i18n.t('Copying');
      headline = onCopy;
      bar = wholeBar;
      if (status.copied_this_run) {
        facts.push(i18n.t('This run: {count} copied ({size}).', {
          count: count(status.copied_this_run), size: size(status.bytes_this_run) }));
      }
    } else if (!status.enabled) {
      tone = 'off';
      state = i18n.t('Off');
      headline = status.copied ? onCopy : i18n.t('No second copy is kept. Choose a folder on another disk and tick Keep the copy.');
    } else if (!status.there) {
      tone = 'warn';
      state = status.network ? i18n.t('The share is not connected') : i18n.t('The disk is not plugged in');
      headline = status.away_said ? i18n.fromServer(status.away_said)
        : i18n.t('Nothing can be copied until the disk is back. The copy carries on by itself when it is.');
    } else if (status.error || status.copy_missing || status.problem) {
      tone = 'warn';
      state = i18n.t('Needs you');
      headline = status.error ? i18n.fromServer(status.error)
        : status.copy_missing ? i18n.t('The share is connected but the second copy is not on it. The disk behind the share may have dropped (after the Mac slept, say); nothing is written there until it is back. If it is a new disk, press Start afresh here.')
          : i18n.fromServer(status.problem);
      bar = wholeBar;
    } else if (owed > 0) {
      tone = 'wait';
      state = i18n.t('Behind');
      headline = i18n.t('{owed} files ({size}) still to copy.', {
        owed: count(owed), size: size(status.owed_bytes) });
      bar = wholeBar;
    } else {
      tone = 'ok';
      state = i18n.t('Up to date');
      headline = files ? i18n.t('Every file in the library is on the copy ({size}).', { size: size(status.copied_bytes) })
        : i18n.t('The library has no files yet.');
    }

    if (job && status.waiting) {
      facts.push({ text: i18n.t('Paused: {reason}', { reason: i18n.fromServer(status.waiting) }), tone: 'warn' });
    }
    if (job && status.current) facts.push({ text: i18n.t('Now: {path}', { path: status.current }), tone: 'quiet' });
    if (job === 'checking' || job === 'restoring' || (tone === 'warn' && !job)) {
      if (status.enabled || status.copied) facts.push(onCopy);
    }
    if (!job) {
      const last = this.lastRun(status.last);
      if (last) facts.push(last);
    }
    if (status.short_of_room) warn(i18n.t('There is not room for the whole copy there. It copies what fits and says when the disk is full.'));
    if (status.library_here) {
      facts.push(i18n.t('This folder already holds the library, in the same folders ({found} of {looked} files looked at are there). Copy now compares each file with the library and keeps the ones with the same bytes as they are, writing nothing; only files missing there are written.', {
        found: count(status.library_here.found), looked: count(status.library_here.looked_at) }));
    }
    if (status.too_large) {
      warn(i18n.t('{count} files are too large for this disk and were left out (for example {path}). A disk formatted as exFAT takes them.', {
        count: count(status.too_large), path: (status.too_large_examples || [])[0] || '' }));
    }
    // Not said twice: the last run's own line already says when it finished.
    const lastDone = !job && status.last?.outcome === 'done' ? status.last.job : '';
    if (status.last_verified && status.verify_checked !== 0 && lastDone !== 'checking') {
      if (status.verify_bad || status.verify_missing) {
        warn(i18n.t('Last checked {when}: {bad} no longer matched and {missing} were missing; they are copied again.', {
          when: when(status.last_verified), bad: count(status.verify_bad), missing: count(status.verify_missing) }));
      } else {
        facts.push(i18n.t('Last checked {when}: every file matched.', { when: when(status.last_verified) }));
      }
    }
    if (status.last_finished && lastDone !== 'copying') facts.push(i18n.t('Last whole copy {when}.', { when: when(status.last_finished) }));
    if (!job && status.enabled && status.there && status.next_at) {
      facts.push(status.next_at * 1000 <= Date.now()
        ? i18n.t('Next copy: as soon as the household is quiet.')
        : i18n.t('Next copy {when}.', { when: when(status.next_at) }));
    } else if (!job && status.enabled && !(Number(status.every_hours) > 0)) {
      facts.push(i18n.t('Copies only when you press Copy now.'));
    }
    if (status.free_bytes !== null && status.free_bytes !== undefined) {
      facts.push({ text: i18n.t('{free} free on that disk.', { free: size(status.free_bytes) }), tone: 'quiet' });
    }
    if (status.already_there) {
      facts.push({ text: i18n.t('{count} files were on that disk already with the same bytes and were left as they were, not copied again.', {
        count: count(status.already_there) }), tone: 'quiet' });
    }
    if (status.renamed) {
      facts.push({ text: i18n.t('{count} files are kept under names this disk takes; NINAIVU-NAMES.txt on the copy says which.', {
        count: count(status.renamed) }), tone: 'quiet' });
    }
    return { tone, state, headline, bar, facts };
  }

  /** What the last copy or check came to (mirror.py ``last``), or ''. */
  lastRun(last) {
    if (!last || !last.finished) return '';
    const at = when(last.finished);
    if (last.job === 'checking') {
      switch (last.outcome) {
        case 'done':
          return last.bad || last.missing
            ? { text: i18n.t('Check finished {when}: {checked} read back, {bad} no longer matched and {missing} were missing; they are copied again.', {
              when: at, checked: count(last.checked), bad: count(last.bad), missing: count(last.missing) }), tone: 'warn' }
            : i18n.t('Check finished {when}: all {checked} files read back intact.', { when: at, checked: count(last.checked) });
        case 'nothing': return i18n.t('Nothing is on the copy yet to check. Press Copy now first.');
        case 'stopped': return i18n.t('Check stopped {when} after {checked} files. It carries on from there next time.', { when: at, checked: count(last.checked) });
        case 'set_aside': return i18n.t('Check paused {when} after {checked} files for the copy that was due; it carries on after it.', { when: at, checked: count(last.checked) });
        default: return { text: i18n.t('Check stopped {when} with a problem; see above.', { when: at }), tone: 'warn' };
      }
    }
    switch (last.outcome) {
      case 'done': {
        const parts = [last.copied
          ? i18n.t('Copy finished {when}: {count} files copied.', { when: at, count: count(last.copied) })
          : i18n.t('Copy finished {when}: nothing new to copy.', { when: at })];
        if (last.found) parts.push(i18n.t('{count} were on the disk already.', { count: count(last.found) }));
        if (last.failed) parts.push(i18n.t('{count} could not be read and are tried again next time.', { count: count(last.failed) }));
        if (last.library_away) parts.push(i18n.t('A library folder was not connected; its files are still owed.'));
        return { text: parts.join(' '), tone: last.failed || last.library_away ? 'warn' : '' };
      }
      case 'stopped': return i18n.t('Copy stopped {when} after {count} files. It carries on from there next time.', { when: at, count: count(last.copied) });
      default: return { text: i18n.t('Copy stopped {when} with a problem after {count} files; see above.', { when: at, count: count(last.copied) }), tone: 'warn' };
    }
  }
}
