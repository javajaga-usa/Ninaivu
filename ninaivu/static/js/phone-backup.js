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
 */

import * as i18n from './i18n.js';

const $ = (sel) => document.querySelector(sel);

/** Bytes per request. Small enough to retry cheaply on a phone's Wi-Fi. */
const PIECE = 8 * 1024 * 1024;
/** How many files one check asks about. The server takes up to 5,000. */
const CHECK_BATCH = 2000;
const DEVICE_KEY = 'ninaivu.backup.device';
const NAME_KEY = 'ninaivu.backup.name';

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

const sleep = (ms) => new Promise((resolve) => { setTimeout(resolve, ms); });

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
  const response = await fetch(url, options);
  let data = null;
  try { data = await response.json(); } catch { /* an HTML error page */ }
  if (!response.ok) {
    throw new Refused(data?.error || response.statusText, response.status, data);
  }
  return data;
}

const post = (url, body) => call(url, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
  body: JSON.stringify(body),
});

export class PhoneBackup {
  constructor({ toast, onFiled }) {
    this.toast = toast || (() => {});
    this.onFiled = onFiled || (() => {});
    this.device = deviceId();
    this.running = false;
    this.stopping = false;
    this.wakeLock = null;
  }

  wire() {
    const modal = $('#backup-modal');
    if (!modal) return;
    $('#backup-btn')?.addEventListener('click', () => this.open());
    $('#backup-close').addEventListener('click', () => {
      if (!this.running) modal.hidden = true;
      else this.toast(i18n.t('Keep this page open while it backs up. Phones pause pages that are in the background.'));
    });
    $('#backup-stop').addEventListener('click', () => { this.stopping = true; });
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
    // A phone that locks releases the wake lock; ask again when it is back.
    document.addEventListener('visibilitychange', () => {
      if (this.running && document.visibilityState === 'visible') this.keepAwake();
    });
  }

  async open() {
    $('#backup-modal').hidden = false;
    this.warnAboutMobileData();
    try {
      this.showSummary(await call(`/api/phone-backup/status?device_id=${encodeURIComponent(this.device)}`));
    } catch (err) {
      if (err.status !== 401) this.toast(err.message, true);
    }
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
      const skipped = files.length - plan.length;
      if (!plan.length) {
        this.status(i18n.t('Everything you chose is already backed up.'));
        return;
      }
      if (skipped) this.toast(i18n.t('{count} already backed up, skipped', { count: skipped }));
      const total = plan.reduce((sum, item) => sum + item.file.size, 0);
      let before = 0;
      for (const [index, item] of plan.entries()) {
        if (this.stopping) break;
        const progress = (sent) => this.progress(index, plan.length, before + sent, total);
        const answer = await this.send(item, progress);
        before += item.file.size;
        if (answer) {
          this.listed(item.file.name, answer.state);
          if (['staged', 'done', 'duplicate'].includes(answer.state)) sentFiles += 1;
        }
      }
      const finished = await post('/api/phone-backup/finish', { device_id: this.device });
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
      const answer = await post('/api/phone-backup/check', {
        device_id: this.device,
        files: batch.map((f) => ({ name: f.name, size: f.size, modified: f.lastModified })),
      });
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

  /** One file, in pieces, riding out a dropped connection. */
  async send({ file }, progress) {
    const begun = await this.patiently(() => post('/api/phone-backup/files', {
      device_id: this.device,
      device: $('#backup-device').value.trim() || i18n.t('My phone'),
      name: file.name,
      size: file.size,
      modified: file.lastModified,
    }));
    if (!begun) return null;
    let { offset } = begun;
    let answer = begun;
    progress(offset);
    while (answer.state === 'receiving' && offset < file.size) {
      if (this.stopping) return null;
      const piece = file.slice(offset, Math.min(file.size, offset + PIECE));
      try {
        answer = await this.patiently(() => call(
          `/api/phone-backup/files/${answer.id}?offset=${offset}`,
          { method: 'PUT', headers: { 'Content-Type': 'application/octet-stream' }, body: piece },
        ));
        if (!answer) return null;
        offset = answer.offset;
      } catch (err) {
        // Out of step: a piece arrived but its answer did not. Carry on from
        // the server's count rather than from ours.
        if (err.status === 409 && Number.isFinite(err.data?.offset)) {
          offset = err.data.offset;
          continue;
        }
        throw err;
      }
      progress(offset);
    }
    return answer;
  }

  /**
   * Try again after a dropped connection, for as long as the page is open
   * and nobody pressed Stop. A refusal from the server is not retried.
   */
  async patiently(attempt) {
    let wait = 1000;
    for (;;) {
      if (this.stopping) return null;
      try {
        return await attempt();
      } catch (err) {
        if (err instanceof Refused && err.status !== 502 && err.status !== 503
            && err.status !== 504) throw err;
        this.status(i18n.t('Connection lost — trying again…'));
        await sleep(wait);
        wait = Math.min(30000, wait * 2);
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
