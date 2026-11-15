/**
 * Back up this phone — the family app's side of ninaivu/media/phone_backup.py.
 *
 * The person chooses photos and videos; this asks the server which of them it
 * already has, then sends the rest one at a time, in pieces, carrying on from
 * the server's own count of what arrived. Nothing about progress is kept only
 * here: close the tab halfway through a video, choose the same photos again
 * tomorrow, and it picks up at the byte it had reached.
 *
 * What a web page cannot do, and is honest about: it cannot read the phone's
 * library by itself, and it cannot keep going in the background — a phone
 * pauses a page that is not on screen. So the person chooses what to send,
 * the screen is kept awake while it sends, and the page says to leave it open.
 *
 * On a slow or patchy link (a village's mobile data, a train) it bends rather
 * than breaks. Pieces start small and grow or shrink with the speed the last
 * ones went at, so a dropped connection costs seconds, not minutes; a piece
 * that stalls is given up on and sent again; with no connection at all it
 * waits for one instead of trying every few seconds; and one file the server
 * turns away is marked and the rest carry on. If the page is closed part-way,
 * it remembers which files had not finished, and says so next time.
 */

import * as i18n from './i18n.js';

const $ = (sel) => document.querySelector(sel);

/** Bytes per request: the first piece on a link not measured yet, and the
 *  smallest and largest it is allowed to become. */
const PIECE_START = 1024 * 1024;
export const PIECE_MIN = 256 * 1024;
export const PIECE_MAX = 8 * 1024 * 1024;
/** Pieces are sized to take about this long at the speed measured. */
const PIECE_SECONDS = 5;
const PIECE_STEP = 64 * 1024;
/** Answers that mean "not now", tried again like a dropped connection. */
const NOT_NOW = new Set([408, 429, 502, 503, 504]);
/** Answers that end the whole run: signed out, or the server's disk full. */
const STOP_ALL = new Set([401, 403, 507]);
/** How many files one check asks about. The server takes up to 5,000. */
const CHECK_BATCH = 2000;
/** How long the end of a batch may take: the server files it then. */
export const FINISH_MS = 15 * 60000;
const DEVICE_KEY = 'ninaivu.backup.device';
const NAME_KEY = 'ninaivu.backup.name';
const UNFINISHED_KEY = 'ninaivu.backup.unfinished';
/** How many unfinished file names are remembered between visits. */
const UNFINISHED_NAMES = 200;

const STATES = {
  staged: i18n.key('Waiting for approval'),
  done: i18n.key('In the library'),
  duplicate: i18n.key('Already in the library'),
  receiving: i18n.key('Part-way'),
  failed: i18n.key('Could not be backed up'),
  unsupported: i18n.key('Not a photo or video'),
};

const bytes = (n) => {
  const value = Number(n) || 0;
  if (value < 1024) return `${value} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let size = value / 1024;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
  return `${size < 10 ? size.toFixed(1) : Math.round(size)} ${units[unit]}`;
};

/**
 * The next piece's size, from how the last one went: big enough to take
 * about five seconds at the speed it went at, never more than twice the last
 * one, and halved after a drop, so a link that keeps breaking sends pieces it
 * can finish.
 */
export function nextPiece(current, { bytes = 0, seconds = 0, dropped = false } = {}) {
  const step = (n) => Math.max(PIECE_MIN, Math.min(PIECE_MAX, Math.floor(n / PIECE_STEP) * PIECE_STEP));
  if (dropped) return step(current / 2);
  if (!bytes || !(seconds > 0)) return step(current);
  return step(Math.min((bytes / seconds) * PIECE_SECONDS, current * 2));
}

/**
 * How long one piece may take before it is given up on and sent again: four
 * times what the speed measured says, at least half a minute, doubled for
 * each drop in a row (a link slower than ever measured still gets a piece
 * through), and never more than three minutes.
 */
export function pieceTimeout(size, speed, drops = 0) {
  const expected = speed > 0 ? (size / speed) * 1000 * 4 : 0;
  const floor = 30000 * 2 ** Math.min(drops, 3);
  return Math.round(Math.min(180000, Math.max(floor, expected)));
}

/** Whether *err* is the connection, not the server saying no. */
export function dropped(err) {
  return !(err instanceof Refused) || NOT_NOW.has(err.status);
}

/** The first piece's size: smaller on a link the phone says is slow. */
function firstPiece() {
  const kind = globalThis.navigator?.connection?.effectiveType || '';
  return kind === 'slow-2g' || kind === '2g' ? PIECE_MIN : PIECE_START;
}

const now = () => (globalThis.performance?.now?.() ?? Date.now());

function stored(key, make) {
  try {
    const found = localStorage.getItem(key);
    if (found) return found;
    const made = make();
    localStorage.setItem(key, made);
    return made;
  } catch {
    return make();
  }
}

/** This phone's backup id: random, made once, kept in the browser. */
function deviceId() {
  return stored(DEVICE_KEY, () => {
    const raw = globalThis.crypto?.randomUUID?.()
      || `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`;
    return `p${raw.replace(/[^A-Za-z0-9]/g, '').slice(0, 40)}`;
  });
}

class Refused extends Error {
  constructor(message, status, data) {
    super(message);
    this.status = status;
    this.data = data || {};
  }
}

async function call(url, options = {}) {
  // Each piece goes up as a raw body, which a cross-origin form could also
  // send; X-Requested-With is how a script says it is not one. See
  // _refuse_cross_origin_writes in ninaivu/__init__.py.
  const response = await globalThis.fetch(url, {
    ...options,
    headers: { 'X-Requested-With': 'fetch', ...(options.headers || {}) },
  });
  let data = null;
  try { data = await response.json(); } catch { /* an HTML error page */ }
  if (!response.ok) {
    throw new Refused(i18n.fromServer(data, response.statusText), response.status, data);
  }
  return data;
}

const post = (url, body, signal) => call(url, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
  body: JSON.stringify(body),
  signal,
});

export class PhoneBackup {
  constructor({ toast, onFiled }) {
    this.toast = toast || (() => {});
    this.onFiled = onFiled || (() => {});
    this.device = deviceId();
    this.running = false;
    this.stopping = false;
    this.wakeLock = null;
    // The link as measured: bytes a second, the next piece's size, and how
    // many drops in a row.
    this.speed = 0;
    this.piece = firstPiece();
    this.drops = 0;
    this.wait = 1000;
    // The piece on its way, and the wait for a connection, so Stop and
    // coming back on screen end them at once.
    this.inflight = null;
    this.waking = null;
  }

  /** Stop: the piece on its way is abandoned and any wait ends. */
  stop() {
    this.stopping = true;
    this.inflight?.abort();
    this.nudge();
  }

  /** End a wait for the connection early: it is back, or the page is. */
  nudge() {
    const wake = this.waking;
    this.waking = null;
    wake?.();
  }

  wire() {
    const modal = $('#backup-modal');
    if (!modal) return;
    $('#backup-btn')?.addEventListener('click', () => this.open());
    $('#backup-close').addEventListener('click', () => {
      if (!this.running) modal.hidden = true;
      else this.toast(i18n.t('Keep this page open while it backs up. Phones pause pages that are in the background.'));
    });
    $('#backup-stop').addEventListener('click', () => this.stop());
    $('#backup-choose').addEventListener('click', () => $('#backup-input').click());
    $('#backup-input').addEventListener('change', (event) => {
      const files = Array.from(event.target.files || []);
      event.target.value = '';
      if (files.length) this.run(files);
    });
    const name = $('#backup-device');
    name.value = stored(NAME_KEY, () => i18n.t('My phone'));
    name.addEventListener('change', () => {
      try { localStorage.setItem(NAME_KEY, name.value.trim()); } catch { /* private mode */ }
    });
    $('#backup-key-make')?.addEventListener('click', () => this.makeKey());
    $('#backup-key-copy')?.addEventListener('click', () => this.copyKey());
    // A phone that locks releases the wake lock; ask again when it is back,
    // and try the connection again at once rather than at the end of a wait.
    document.addEventListener('visibilitychange', () => {
      if (this.running && document.visibilityState === 'visible') {
        this.keepAwake();
        this.nudge();
      }
    });
    globalThis.addEventListener?.('online', () => this.nudge());
  }

  async open() {
    $('#backup-modal').hidden = false;
    this.warnAboutMobileData();
    try {
      this.showSummary(await call(`/api/phone-backup/status?device_id=${encodeURIComponent(this.device)}`));
    } catch (err) {
      if (err.status !== 401) this.toast(err.message, true);
    }
    $('#backup-key-new').hidden = true;
    this.loadKeys();
    if (!this.running) this.showUnfinished();
  }

  /* -- files that did not finish last time ----------------------------------
   * A page cannot keep the files themselves, so it keeps their names: closed
   * part-way, it says next time how many had not finished, and the person
   * chooses them again. The server carries each on from the byte it had. */

  unfinished() {
    try {
      const kept = JSON.parse(localStorage.getItem(UNFINISHED_KEY) || 'null');
      return Array.isArray(kept?.names) ? kept : null;
    } catch {
      return null;
    }
  }

  remember(names) {
    try {
      if (names.length) {
        localStorage.setItem(UNFINISHED_KEY, JSON.stringify({
          device: this.device, names: names.slice(0, UNFINISHED_NAMES), count: names.length,
        }));
      } else {
        localStorage.removeItem(UNFINISHED_KEY);
      }
    } catch { /* private mode: nothing kept, nothing said */ }
  }

  showUnfinished() {
    const kept = this.unfinished();
    if (!kept?.count) return;
    $('#backup-status').textContent = Number(kept.count) === 1
      ? i18n.t('One file did not finish last time. Choose it again and it carries on from where it stopped.')
      : i18n.t('{count} files did not finish last time. Choose them again and each carries on from where it stopped.',
        { count: Number(kept.count).toLocaleString() });
  }

  /* -- phone keys: a sync app backs the phone up by itself ------------------
   * ninaivu/media/phone_keys.py. A key is shown once, when it is made; after
   * that the list shows only its name and last four characters. */

  async loadKeys() {
    const box = $('#backup-keys');
    if (!box) return;
    let data;
    try {
      data = await call('/api/phone-keys');
    } catch (err) {
      // A server from before phone keys has no such endpoint.
      if (err.status === 404) $('#backup-auto').hidden = true;
      return;
    }
    box.replaceChildren();
    for (const item of data.keys || []) {
      const row = document.createElement('div');
      row.className = 'upload-item';
      const label = document.createElement('strong');
      label.textContent = `${item.name} · …${item.hint}`;
      const used = document.createElement('span');
      used.className = 'backup-state';
      used.textContent = item.last_used_at
        ? i18n.t('{count} sent · last used {when}', {
          count: Number(item.files || 0).toLocaleString(),
          when: new Date(item.last_used_at * 1000).toLocaleString(),
        })
        : i18n.t('Not used yet');
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'btn ghost small';
      remove.textContent = i18n.t('Revoke');
      remove.addEventListener('click', () => this.revokeKey(item));
      row.append(label, used, remove);
      box.append(row);
    }
  }

  async makeKey() {
    const button = $('#backup-key-make');
    button.disabled = true;
    try {
      const made = await post('/api/phone-keys', {
        name: $('#backup-device').value.trim() || i18n.t('My phone'),
      });
      $('#backup-key-url').textContent = made.webdav_url;
      $('#backup-key-user').textContent = made.username;
      $('#backup-key-value').textContent = made.key;
      $('#backup-key-new').hidden = false;
      this.loadKeys();
    } catch (err) {
      this.toast(err.message, true);
    } finally {
      button.disabled = false;
    }
  }

  async copyKey() {
    const key = $('#backup-key-value').textContent;
    try {
      await navigator.clipboard.writeText(key);
      this.toast(i18n.t('Key copied'));
    } catch {
      // Without HTTPS a page may not write to the clipboard: select the key
      // so a long press copies it.
      const range = document.createRange();
      range.selectNodeContents($('#backup-key-value'));
      const chosen = globalThis.getSelection?.();
      chosen?.removeAllRanges();
      chosen?.addRange(range);
      this.toast(i18n.t('Select the key and copy it'));
    }
  }

  async revokeKey(item) {
    if (!globalThis.confirm(i18n.t('Revoke the key for {name}? The app on that phone will stop backing up until it has a new key.', { name: item.name }))) return;
    try {
      await call(`/api/phone-keys/${item.id}`, { method: 'DELETE' });
      this.toast(i18n.t('Key revoked'));
    } catch (err) {
      this.toast(err.message, true);
    }
    this.loadKeys();
  }

  warnAboutMobileData() {
    const type = navigator.connection?.type;
    if (type === 'cellular') {
      $('#backup-status').textContent = i18n.t('You are on mobile data. Backups can be large; Wi-Fi is better.');
    }
  }

  showSummary(summary) {
    const phone = summary?.this_phone || {};
    $('#backup-safe').textContent = Number(phone.safe || 0).toLocaleString();
    $('#backup-waiting').textContent = Number(phone.waiting_review || 0).toLocaleString();
    $('#backup-partial').textContent = Number(phone.partial || 0).toLocaleString();
    $('#backup-last').textContent = phone.last_backup
      ? i18n.t('Last backup: {when}', { when: new Date(phone.last_backup * 1000).toLocaleString() })
      : i18n.t('Nothing backed up from this phone yet.');
  }

  /* -- one run ---------------------------------------------------------- */

  async run(files) {
    if (this.running) return;
    this.running = true;
    this.stopping = false;
    $('#backup-choose').disabled = true;
    $('#backup-stop').hidden = false;
    $('#backup-progress').hidden = false;
    $('#backup-list').replaceChildren();
    this.keepAwake();
    let sentFiles = 0;
    try {
      this.status(i18n.t('Checking what is already backed up…'));
      const plan = await this.plan(files);
      if (this.stopping) {
        this.status(i18n.t('Stopped. Choose the same photos again to carry on.'));
        return;
      }
      const skipped = files.length - plan.length;
      if (!plan.length) {
        this.status(i18n.t('Everything you chose is already backed up.'));
        return;
      }
      if (skipped) this.toast(i18n.t('{count} already backed up, skipped', { count: skipped }));
      const total = plan.reduce((sum, item) => sum + item.file.size, 0);
      const left = new Set(plan.map((item) => item.file.name));
      this.remember([...left]);
      let before = 0;
      for (const [index, item] of plan.entries()) {
        if (this.stopping) break;
        const progress = (sent) => this.progress(index, plan.length, before + sent, total);
        const answer = await this.sendOne(item, progress);
        before += item.file.size;
        if (answer) {
          this.listed(item.file.name, answer.state);
          if (['staged', 'done', 'duplicate'].includes(answer.state)) {
            sentFiles += 1;
            left.delete(item.file.name);
            this.remember([...left]);
          }
        }
      }
      const wasStopped = this.stopping;
      this.stopping = false;            // the finish is sent even after Stop
      // Filing the batch happens in this answer for an administrator or a
      // trusted family member, and moving hundreds of photographs onto a slow
      // drive takes minutes: given the half minute a small answer gets, it
      // was abandoned and sent again and again while the first still ran.
      const finished = await this.patiently((signal) => post('/api/phone-backup/finish',
        { device_id: this.device }, signal), FINISH_MS);
      this.stopping = wasStopped;
      if (!finished) return;
      this.showSummary(finished.summary);
      if (this.stopping) {
        this.status(i18n.t('Stopped. Choose the same photos again to carry on.'));
      } else {
        this.status(`${i18n.t('Backup finished — {count} more safely backed up.', { count: sentFiles })}${
          finished.needs_review ? ` ${i18n.t('They join the library once an admin approves them.')}` : ''}`);
      }
      if (finished.approved) this.onFiled();
    } catch (err) {
      this.status(i18n.t('Could not back up: {reason}', { reason: err.message }));
      this.toast(err.message, true);
    } finally {
      this.running = false;
      $('#backup-choose').disabled = false;
      $('#backup-stop').hidden = true;
      this.releaseWake();
    }
  }

  /** The chosen files the server does not already have, with where to start. */
  async plan(files) {
    const wanted = [];
    for (let start = 0; start < files.length; start += CHECK_BATCH) {
      const batch = files.slice(start, start + CHECK_BATCH);
      const answer = await this.patiently((signal) => post('/api/phone-backup/check', {
        device_id: this.device,
        files: batch.map((f) => ({ name: f.name, size: f.size, modified: f.lastModified })),
      }, signal));
      if (!answer) return wanted;
      answer.files.forEach((found, index) => {
        if (found.state === 'new' || found.state === 'receiving' || found.state === 'failed') {
          wanted.push({ file: batch[index] });
        } else if (found.state === 'unsupported') {
          this.listed(batch[index].name, 'unsupported');
        }
      });
      this.showSummary(answer.summary);
    }
    return wanted;
  }

  /**
   * One file. The server turning it away marks it and the run goes on; only
   * being signed out, or the server's disk being full, ends the run. A file
   * the server set aside part-way (it failed, or waited too long) is offered
   * once more from the start.
   */
  async sendOne(item, progress) {
    for (let attempt = 0; ; attempt += 1) {
      try {
        return await this.send(item, progress);
      } catch (err) {
        if (!(err instanceof Refused) || STOP_ALL.has(err.status)) throw err;
        if (err.status === 409 && !Number.isFinite(err.data?.offset) && attempt === 0) continue;
        this.toast(`${item.file.name}: ${err.message}`, true);
        return { state: 'failed' };
      }
    }
  }

  /** One file, in pieces sized to the link, riding out a dropped connection. */
  async send({ file }, progress) {
    const begun = await this.patiently((signal) => post('/api/phone-backup/files', {
      device_id: this.device,
      device: $('#backup-device').value.trim() || i18n.t('My phone'),
      name: file.name,
      size: file.size,
      modified: file.lastModified,
    }, signal));
    if (!begun) return null;
    let { offset } = begun;
    let answer = begun;
    progress(offset);
    const { id } = answer;
    while (answer.state === 'receiving' && offset < file.size) {
      if (this.stopping) return null;
      const size = Math.min(this.piece, file.size - offset);
      const piece = file.slice(offset, offset + size);
      const started = now();
      try {
        answer = await this.timed(pieceTimeout(size, this.speed, this.drops), (signal) => call(
          `/api/phone-backup/files/${id}?offset=${offset}`,
          { method: 'PUT', headers: { 'Content-Type': 'application/octet-stream' }, body: piece, signal },
        ));
        this.learn(size, (now() - started) / 1000);
        offset = answer.offset;
      } catch (err) {
        if (this.stopping) return null;
        // Out of step: a piece arrived but its answer did not. Carry on from
        // the server's count rather than from ours.
        if (err.status === 409 && Number.isFinite(err.data?.offset)) {
          offset = err.data.offset;
          progress(offset);
          continue;
        }
        if (!dropped(err)) throw err;
        this.piece = nextPiece(this.piece, { dropped: true });
        this.drops += 1;
        if (!(await this.waitForLink())) return null;
        continue;
      }
      progress(offset);
    }
    return answer;
  }

  /** Run *attempt* with a signal that gives up after *ms*, or on Stop. */
  async timed(ms, attempt) {
    const control = new AbortController();
    this.inflight = control;
    const timer = setTimeout(() => control.abort(), ms);
    try {
      return await attempt(control.signal);
    } finally {
      clearTimeout(timer);
      if (this.inflight === control) this.inflight = null;
    }
  }

  /** A piece of *bytes* went in *seconds*: the link is that fast now. */
  learn(bytes, seconds) {
    if (seconds > 0) {
      const measured = bytes / seconds;
      this.speed = this.speed ? this.speed * 0.6 + measured * 0.4 : measured;
    }
    this.piece = nextPiece(this.piece, { bytes, seconds });
    this.drops = 0;
    this.wait = 1000;
  }

  /**
   * Wait before trying again: longer each time, up to half a minute, ended
   * early when the phone says the connection is back or the page comes back
   * on screen. With no connection at all it waits for one. False on Stop.
   */
  async waitForLink() {
    if (this.stopping) return false;
    const offline = globalThis.navigator?.onLine === false;
    this.status(offline ? i18n.t('No connection — waiting for one…')
      : i18n.t('Connection lost — trying again…'));
    const ms = offline ? 5 * 60000 : this.wait;
    this.wait = Math.min(30000, this.wait * 2);
    await new Promise((resolve) => {
      const timer = setTimeout(() => { this.waking = null; resolve(); }, ms);
      this.waking = () => { clearTimeout(timer); resolve(); };
    });
    return !this.stopping;
  }

  /**
   * Try again after a dropped connection, for as long as the page is open
   * and nobody pressed Stop. A refusal from the server is not retried.
   */
  async patiently(attempt, ms = 0) {
    for (;;) {
      if (this.stopping) return null;
      try {
        const answer = await this.timed(Math.max(ms, pieceTimeout(0, 0, this.drops)), attempt);
        this.wait = 1000;
        return answer;
      } catch (err) {
        if (!dropped(err)) throw err;
        this.drops += 1;
        if (!(await this.waitForLink())) return null;
      }
    }
  }

  /* -- saying what is happening ------------------------------------------- */

  progress(index, count, sent, total) {
    $('#backup-fill').style.width = `${total ? Math.min(100, (sent / total) * 100) : 0}%`;
    this.status(i18n.t('{done} of {total} files · {sent} of {size}', {
      done: index, total: count, sent: bytes(sent), size: bytes(total),
    }));
  }

  status(text) {
    $('#backup-status').textContent = text;
  }

  listed(name, state) {
    const row = document.createElement('div');
    row.className = 'upload-item';
    const label = document.createElement('strong');
    label.textContent = name;                 // the phone's name for it, as text
    const where = document.createElement('span');
    where.className = `backup-state${state === 'failed' || state === 'unsupported' ? ' bad' : ''}`;
    where.textContent = i18n.t(STATES[state] || STATES.failed);
    row.append(label, where);
    $('#backup-list').prepend(row);
  }

  async keepAwake() {
    try {
      this.wakeLock = await navigator.wakeLock?.request('screen');
    } catch { /* not offered, or the page is hidden */ }
  }

  releaseWake() {
    this.wakeLock?.release?.().catch(() => {});
    this.wakeLock = null;
  }
}
