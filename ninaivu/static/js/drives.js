/**
 * A pendrive, a memory card, an external hard drive or a phone was plugged in
 * to the Ninaivu computer: one question. From Ninaivu Lite 1.5.
 *
 *   Import media from this drive   → the Import page, with the drive as its source
 *   Export media to this drive     → the library copied onto it (drives.Exporter);
 *                                    not offered for a phone
 *   Not now                        → asked again only when it is plugged in again
 *   Don't ask about this drive again → remembered on the server, for a backup
 *                                    disk or a second SSD that stays plugged in;
 *                                    "Ask again" on the Import page undoes it
 *
 * A phone or camera on a Mac is not a folder anybody can read, so for one the
 * dialog says so and offers Image Capture on the Ninaivu computer instead.
 *
 * The console asks the server which drives are plugged in every few seconds
 * while it is open and on screen. On a Mac the server asks in a window on the
 * computer too (utils/drive_dialog.py); its Import… button opens the console
 * as `?drive=<path>&do=import`, which opens the Import page on arrival.
 *
 * The dialog is built here rather than in admin.html, which the browser may
 * still have cached from before this file existed.
 */

import * as i18n from './i18n.js';

const POLL_MS = 4000;

const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
};

function size(bytes) {
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let value = Number(bytes) || 0;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
  return `${value >= 10 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
}

function samePath(a, b) {
  const key = (p) => String(p).replace(/\\/g, '/').replace(/\/+$/, '');
  const norm = (p) => (/^[a-zA-Z]:/.test(key(p)) ? key(p).toLowerCase() : key(p));
  return norm(a) === norm(b);
}

export class DrivePrompt {
  /**
   * @param {{json: Function, toast: Function, openImport: (path: string) => Promise<void>,
   *          openImportPage: () => void}} deps
   */
  constructor({ json, toast, openImport, openImportPage }) {
    this.json = json;
    this.toast = toast;
    this.openImport = openImport;
    this.openImportPage = openImportPage;
    this.timer = null;
    this.modal = null;
    /** The drive on screen, and the copy to it the dialog follows, if any. */
    this.drive = null;
    this.copying = false;
    this.copyTimer = null;
    this.lastCopy = null;
    this.imageCapture = false;
    /** The drive ids already shown this visit, so Esc does not bring it straight back. */
    this.shown = new Set();
    i18n.onChange(() => {
      if (this.modal && !this.modal.hidden) this.redraw();
      if (this.lastList) this.renderQuiet(this.lastList);
    });
  }

  start() {
    if (this.timer) return;
    this.followLink();
    // A moment after sign-in, so a welcome the console opens then is
    // already on screen and this waits its turn instead of opening over it.
    setTimeout(() => { if (!document.hidden) this.check(); }, 1500);
    this.timer = setInterval(() => { if (!document.hidden) this.check(); }, POLL_MS);
  }

  /* -- asking ------------------------------------------------------------- */

  async check() {
    if (this.copying) return;
    let data;
    try {
      data = await this.json('/api/admin/drives');
    } catch { return; /* the console's own error handling covers a dead server */ }
    if (this.copying) return;
    this.imageCapture = Boolean(data.image_capture);
    this.lastList = data.drives;
    this.renderQuiet(data.drives);
    // A copy to a drive still going after the console was reloaded or
    // closed: shown again, so nobody pulls the drive mid-copy.
    if (data.export?.running && !this.copyTimer) {
      this.drive = { id: '', label: data.export.drive || '' };
      this.copying = true;
      this.lastCopy = data.export;
      this.redraw();
      this.modal.hidden = false;
      this.showCopy(data.export);
      this.follow();
      return;
    }
    if (this.modal && !this.modal.hidden) {
      // Taken out, or answered on another screen (the window on the Mac,
      // another tab): nothing left to ask here.
      const still = this.drive && data.drives.find((d) => d.id === this.drive.id);
      if (!still || !still.pending) this.close();
      return;
    }
    // Not over another dialog, or a locked screen: asked when they are gone.
    if (document.body.classList.contains('screen-locked')
        || document.querySelector('.modal:not([hidden]):not(.drive-modal), .sheet:not([hidden])')) return;
    const next = data.drives.find((d) => d.pending && !this.shown.has(d.id));
    if (next) this.ask(next);
  }

  /** The Mac's own window's answer, carried in the address. */
  async followLink() {
    const params = new URLSearchParams(window.location.search);
    const path = params.get('drive');
    const action = params.get('do');
    if (!path) return;
    params.delete('drive');
    params.delete('do');
    const rest = params.toString();
    window.history.replaceState(null, '', `${window.location.pathname}${rest ? `?${rest}` : ''}${window.location.hash}`);
    let data;
    try { data = await this.json('/api/admin/drives'); } catch { return; }
    const drive = data.drives.find((d) => samePath(d.path, path));
    if (!drive) {
      this.toast(i18n.t('That drive is no longer plugged in.'), true);
      return;
    }
    this.shown.add(drive.id);
    // Only opening the Import page follows a link straight away; nothing is
    // copied until Start is pressed there. Anything else is asked here first.
    if (action === 'import' && drive.readable !== false) this.importFrom(drive);
    else this.ask(drive);
  }

  ask(drive) {
    this.drive = drive;
    this.shown.add(drive.id);
    this.copying = false;
    this.redraw();
    this.modal.hidden = false;
    this.modal.querySelector('.btn.primary')?.focus();
  }

  build() {
    if (this.modal) return;
    const modal = el('div', 'modal drive-modal');
    modal.hidden = true;
    modal.setAttribute('role', 'dialog');
    modal.setAttribute('aria-modal', 'true');
    modal.setAttribute('aria-labelledby', 'drive-title');
    modal.addEventListener('click', (event) => { if (event.target === modal) this.notNow(); });
    modal.addEventListener('keydown', (event) => { if (event.key === 'Escape') this.notNow(); });
    modal.appendChild(el('div', 'modal-card narrow'));
    document.body.appendChild(modal);
    this.modal = modal;
  }

  redraw() {
    this.build();
    const card = this.modal.firstChild;
    card.replaceChildren();
    const drive = this.drive;
    if (!drive) return;
    const phone = drive.kind === 'phone';
    const heading = this.copying ? i18n.key('Copying to the drive')
      : (phone ? i18n.key('A phone was connected') : i18n.key('A drive was connected'));
    const title = el('h2', '', i18n.t(heading));
    title.id = 'drive-title';
    card.appendChild(title);
    const name = drive.label && drive.label !== drive.path && !drive.shell && drive.readable !== false
      ? `${drive.label} (${drive.path})` : drive.label || drive.path;
    card.appendChild(el('p', 'hint drive-name', name));
    if (drive.total) {
      card.appendChild(el('p', 'hint', i18n.t('{free} free of {total}',
        { free: size(drive.free), total: size(drive.total) })));
    }

    if (this.copying) {
      const bar = el('div', 'scan-bar');
      this.fill = el('i');
      bar.appendChild(this.fill);
      card.appendChild(bar);
      this.line = el('p', 'hint drive-progress');
      card.appendChild(this.line);
      const foot = el('div', 'modal-foot');
      foot.appendChild(el('div', 'spacer'));
      this.stopButton = el('button', 'btn ghost', i18n.t('Stop'));
      this.stopButton.type = 'button';
      this.stopButton.onclick = () => this.stopCopy();
      const close = el('button', 'btn primary', i18n.t('Close'));
      close.type = 'button';
      close.onclick = () => this.close();
      foot.append(this.stopButton, close);
      card.appendChild(foot);
      if (this.lastCopy) this.showCopy(this.lastCopy);
      return;
    }

    const choices = el('div', 'drive-choices');
    const choice = (label, hint, primary, action) => {
      const button = el('button', `btn ${primary ? 'primary' : ''} drive-choice`);
      button.type = 'button';
      button.appendChild(el('span', 'drive-choice-label', i18n.t(label)));
      button.appendChild(el('span', 'drive-choice-hint', i18n.t(hint)));
      button.onclick = action;
      return button;
    };
    if (drive.readable === false) {
      card.appendChild(el('p', 'hint', i18n.t(
        'A Mac does not let Ninaivu read a phone or camera over the cable. Copy its photos into a folder with Image Capture, then add that folder on the Import page.')));
      if (this.imageCapture) {
        choices.append(choice(i18n.key('Open Image Capture'),
          i18n.key('Opens on the Ninaivu computer. Choose the phone, pick a folder to import to, and press Download All.'),
          true, () => this.openImageCapture(drive)));
      }
      choices.append(choice(i18n.key('Open the Import page'),
        i18n.key('Add the folder Image Capture copied the photos into as a source.'),
        !this.imageCapture, () => this.goToImport(drive)));
    } else {
      card.appendChild(el('p', 'hint', i18n.t('What would you like to do with it?')));
      if (phone) {
        choices.append(choice(i18n.key('Import media from this phone'),
          i18n.key('Copy its camera photos and videos into the archive, sorted by date. Nothing on the phone is changed.'),
          true, () => this.importFrom(drive)));
      } else {
        choices.append(
          choice(i18n.key('Import media from this drive'),
            i18n.key('Copy its photos and videos into the archive, sorted by date. The drive is not changed.'),
            true, () => this.importFrom(drive)),
          choice(i18n.key('Export media to this drive'),
            i18n.key('Copy the library’s photos and videos onto the drive, in a “Ninaivu” folder.'),
            false, () => this.exportTo(drive)),
        );
      }
    }
    card.appendChild(choices);
    if (phone && drive.readable !== false) {
      card.appendChild(el('p', 'hint subtle drive-note', i18n.t(
        'Nothing showing? Unlock the phone and choose File transfer (on an iPhone, Trust this computer).')));
    }
    const foot = el('div', 'modal-foot');
    const never = el('button', 'btn ghost', i18n.t('Don’t ask about this drive again'));
    never.type = 'button';
    never.onclick = () => this.neverAsk();
    foot.appendChild(never);
    foot.appendChild(el('div', 'spacer'));
    const later = el('button', 'btn ghost', i18n.t('Not now'));
    later.type = 'button';
    later.onclick = () => this.notNow();
    foot.appendChild(later);
    card.appendChild(foot);
  }

  close() {
    if (this.modal) this.modal.hidden = true;
    this.copying = false;
    this.drive = null;
  }

  /* -- the answers -------------------------------------------------------- */

  async answer(drive, never = false) {
    try {
      await this.json('/api/admin/drives/answer', { method: 'POST', body: { id: drive.id, never } });
    } catch { /* asked again next time it is plugged in: harmless */ }
  }

  notNow() {
    if (this.copying) { this.close(); return; }
    if (this.drive) this.answer(this.drive);
    this.close();
  }

  async neverAsk() {
    const drive = this.drive;
    this.close();
    if (!drive) return;
    await this.answer(drive, true);
    this.toast(i18n.t('Ninaivu will not ask about this drive again. The Import page can undo it.'));
    this.check();
  }

  async importFrom(drive) {
    this.close();
    await this.answer(drive);
    await this.openImport(drive.path);
    this.toast(i18n.t('The drive is the source. Check the destination, then press Start.'));
  }

  async goToImport(drive) {
    this.close();
    await this.answer(drive);
    this.openImportPage();
  }

  async openImageCapture(drive) {
    try {
      await this.json('/api/admin/drives/image-capture', { method: 'POST' });
    } catch (exc) {
      this.toast(exc.message, true);
      return;
    }
    this.close();
    await this.answer(drive);
    this.toast(i18n.t('Image Capture is open on the Ninaivu computer. When the photos are in a folder, add it on the Import page.'));
  }

  async exportTo(drive) {
    try {
      await this.json('/api/admin/drives/export', { method: 'POST', body: { id: drive.id } });
    } catch (exc) {
      this.toast(exc.message, true);
      this.close();
      return;
    }
    this.drive = drive;
    this.copying = true;
    this.lastCopy = null;
    this.redraw();
    this.modal.hidden = false;
    this.follow();
  }

  /** Until the copy ends, even with the dialog closed: then say how it went. */
  follow() {
    if (this.copyTimer) return;
    this.copyTimer = setInterval(async () => {
      let state;
      try { state = await this.json('/api/admin/drives/export'); } catch { return; }
      this.lastCopy = state;
      const watching = this.copying;
      if (watching) this.showCopy(state);
      if (!state.running) {
        clearInterval(this.copyTimer);
        this.copyTimer = null;
        // Said in the dialog when it is open; a toast when it was closed.
        if (state.message && !watching) this.toast(said(state.message), state.phase === 'failed');
      }
    }, 1000);
  }

  showCopy(state) {
    if (!state || !this.fill) return;
    const total = state.bytes_total;
    const done = state.bytes_done;
    const percent = total ? Math.min(100, Math.round(((done || 0) / total) * 100))
      : (state.running ? 0 : 100);
    this.fill.style.width = `${percent}%`;
    const count = { done: (state.done || 0).toLocaleString(), total: (state.total || 0).toLocaleString() };
    if (!state.running) this.line.textContent = state.message ? said(state.message) : '';
    else if (state.phase === 'counting') this.line.textContent = i18n.t('Counting the photos and videos…');
    else this.line.textContent = i18n.t('{done} of {total} files', count);
    this.stopButton.hidden = !state.running;
  }

  async stopCopy() {
    try {
      await this.json('/api/admin/drives/export/stop', { method: 'POST' });
    } catch (exc) { this.toast(exc.message, true); }
  }

  /* -- drives set aside, on the Import page ----------------------------------- */

  renderQuiet(list) {
    const anchor = document.querySelector('#ar-devices');
    if (!anchor) return;
    let box = document.querySelector('#ar-quiet-drives');
    const quiet = (list || []).filter((d) => d.quiet);
    if (!quiet.length) {
      if (box) box.hidden = true;
      return;
    }
    if (!box) {
      box = el('div', 'hint ar-quiet-drives');
      box.id = 'ar-quiet-drives';
      anchor.after(box);
    }
    box.hidden = false;
    box.replaceChildren(el('span', '', i18n.t('Plugged in, not asked about:')));
    for (const drive of quiet) {
      const item = el('span', 'ar-quiet-drive', ` ${drive.label || drive.path} `);
      const again = el('button', 'btn small ghost', i18n.t('Ask again'));
      again.type = 'button';
      again.onclick = async () => {
        try {
          await this.json('/api/admin/drives/ask-again', { method: 'POST', body: { id: drive.id } });
        } catch (exc) { this.toast(exc.message, true); return; }
        this.shown.delete(drive.id);
        this.check();
      };
      item.appendChild(again);
      box.appendChild(item);
    }
  }
}

/** A server message: `{text, key, params}` in the language on screen. */
function said(message) {
  if (!message || typeof message === 'string') return message || '';
  return message.key ? i18n.t(message.key, message.params || {}) : (message.text || '');
}
