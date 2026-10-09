/**
 * A pendrive, a memory card, an external hard drive or a phone was plugged in
 * to the Ninaivu computer: one notice at the top of the console.
 *
 *   click the notice                → the Import page, with the drive as its
 *                                     source (nothing is copied until Start)
 *   Export media to this drive      → the library copied onto it (drives.Exporter);
 *                                     not offered for a phone
 *   Don't ask about this drive again → remembered on the server, for a backup
 *                                     disk or a second SSD that stays plugged in;
 *                                     "Ask again" on the Import page undoes it
 *   ✕                               → asked again only when it is plugged in again
 *
 * It is a notice in the page, under the top of the window, and not a window of
 * its own: nothing opens over the work in hand, and nothing opens outside the
 * browser. A copy to a drive is the one thing that still opens a dialog,
 * because somebody pulling the drive out half way is worth stopping.
 *
 * A phone or camera on a Mac is not a folder anybody can read, so for one the
 * notice says so and offers Image Capture on the Ninaivu computer instead.
 *
 * The console asks the server which drives are plugged in every few seconds
 * while it is open and on screen.
 *
 * The notice is built here rather than in admin.html, which the browser may
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

/** A drive's name, with where it is when that says more than the name. */
function nameOf(drive) {
  return drive.label && drive.path && drive.label !== drive.path && !drive.shell
    && drive.readable !== false
    ? `${drive.label} (${drive.path})` : drive.label || drive.path;
}

const ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="14" width="18" height="6" rx="2"/>'
  + '<path d="M7 17h.01M12 3v8m0 0-3-3m3 3 3-3"/></svg>';

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
    /** The notices, one for each drive that is waiting to be asked about. */
    this.tray = null;
    /** The dialog of a copy to a drive: the one thing that still opens one. */
    this.modal = null;
    /** The drive being copied to, and the copy the dialog follows, if any. */
    this.drive = null;
    this.copying = false;
    this.copyTimer = null;
    this.lastCopy = null;
    this.imageCapture = false;
    /** Drives answered here, kept off screen until the server has caught up. */
    this.dismissed = new Set();
    i18n.onChange(() => {
      if (this.modal && !this.modal.hidden) this.redraw();
      if (this.lastList) {
        this.renderQuiet(this.lastList);
        this.tray?.replaceChildren();
        this.renderNotices(this.lastList);
      }
    });
  }

  start() {
    if (this.timer) return;
    // A moment after sign-in, so a welcome the console opens then is
    // already on screen and this waits its turn instead of landing on it.
    setTimeout(() => { if (!document.hidden) this.check(); }, 1500);
    this.timer = setInterval(() => { if (!document.hidden) this.check(); }, POLL_MS);
  }

  /* -- asking ------------------------------------------------------------- */

  async check() {
    let data;
    try {
      data = await this.json('/api/admin/drives');
    } catch { return; /* the console's own error handling covers a dead server */ }
    this.imageCapture = Boolean(data.image_capture);
    this.lastList = data.drives;
    this.renderQuiet(data.drives);
    this.renderNotices(data.drives);
    if (this.copying) return;
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
    }
  }

  /* -- the notices -------------------------------------------------------- */

  buildTray() {
    if (this.tray) return this.tray;
    const tray = el('div', 'drive-notices');
    tray.hidden = true;
    tray.setAttribute('role', 'region');
    tray.setAttribute('aria-live', 'polite');
    document.body.appendChild(tray);
    this.tray = tray;
    return tray;
  }

  /** One notice for each drive still waiting for an answer, none for the rest. */
  renderNotices(list) {
    // An answer is kept off screen only until the server has caught up with
    // it: once it says "not pending" (or the drive is gone) the next time it
    // is pending is a new plug-in, and is asked about, even when the drive
    // was out and in again between two looks.
    const settled = new Set((list || []).filter((d) => !d.pending).map((d) => d.id));
    const present = new Set((list || []).map((d) => d.id));
    for (const id of [...this.dismissed]) {
      if (!present.has(id) || settled.has(id)) this.dismissed.delete(id);
    }
    const waiting = (list || []).filter((d) => d.pending && !this.dismissed.has(d.id));
    if (!waiting.length && !this.tray) return;
    const tray = this.buildTray();
    const ids = new Set(waiting.map((d) => d.id));
    for (const node of [...tray.children]) if (!ids.has(node.dataset.id)) node.remove();
    for (const drive of waiting) {
      if (![...tray.children].some((node) => node.dataset.id === drive.id)) {
        tray.appendChild(this.notice(drive));
      }
    }
    tray.hidden = !tray.children.length;
  }

  notice(drive) {
    const phone = drive.kind === 'phone';
    const unreadable = drive.readable === false;
    const node = el('div', 'drive-notice');
    node.dataset.id = drive.id;

    // The whole of the top part is the answer "yes, import it".
    const main = el('button', 'drive-notice-main');
    main.type = 'button';
    const icon = el('span', 'drive-notice-icon');
    icon.innerHTML = ICON;
    const text = el('span', 'drive-notice-text');
    text.appendChild(el('strong', '',
      i18n.t(phone ? i18n.key('A phone was connected') : i18n.key('A drive was connected'))));
    let detail = nameOf(drive);
    if (drive.total) {
      detail += ` · ${i18n.t('{free} free of {total}', { free: size(drive.free), total: size(drive.total) })}`;
    }
    text.appendChild(el('span', 'drive-notice-name', detail));
    text.appendChild(el('span', 'drive-notice-hint', i18n.t(unreadable
      ? i18n.key('A Mac does not let Ninaivu read a phone or camera over the cable. Copy its photos into a folder with Image Capture, then add that folder on the Import page.')
      : i18n.key('Click to import its photos and videos.'))));
    main.append(icon, text);
    main.onclick = () => (unreadable ? this.goToImport(drive) : this.importFrom(drive));

    const actions = el('div', 'drive-notice-actions');
    const act = (label, handler) => {
      const button = el('button', 'btn small ghost', i18n.t(label));
      button.type = 'button';
      button.onclick = handler;
      actions.appendChild(button);
    };
    if (unreadable && this.imageCapture) {
      act(i18n.key('Open Image Capture'), () => this.openImageCapture(drive));
    }
    if (!unreadable && !phone) {
      act(i18n.key('Export media to this drive'), () => this.exportTo(drive));
    }
    act(i18n.key('Don’t ask about this drive again'), () => this.neverAsk(drive));
    const later = el('button', 'icon-btn drive-notice-close');
    later.type = 'button';
    later.title = i18n.t('Not now');
    later.setAttribute('aria-label', i18n.t('Not now'));
    later.textContent = '×';
    later.onclick = () => this.notNow(drive);
    actions.appendChild(later);

    node.append(main, actions);
    return node;
  }

  /** Take its notice off the screen, and keep it off until the server agrees. */
  dismiss(drive) {
    if (!drive?.id) return;
    this.dismissed.add(drive.id);
    this.renderNotices(this.lastList || []);
  }

  /* -- the dialog of a copy ------------------------------------------------ */

  build() {
    if (this.modal) return;
    const modal = el('div', 'modal drive-modal');
    modal.hidden = true;
    modal.setAttribute('role', 'dialog');
    modal.setAttribute('aria-modal', 'true');
    modal.setAttribute('aria-labelledby', 'drive-title');
    modal.addEventListener('click', (event) => { if (event.target === modal) this.close(); });
    modal.addEventListener('keydown', (event) => { if (event.key === 'Escape') this.close(); });
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
    const title = el('h2', '', i18n.t('Copying to the drive'));
    title.id = 'drive-title';
    card.appendChild(title);
    card.appendChild(el('p', 'hint drive-name', nameOf(drive)));
    if (drive.total) {
      card.appendChild(el('p', 'hint', i18n.t('{free} free of {total}',
        { free: size(drive.free), total: size(drive.total) })));
    }

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

  async notNow(drive) {
    this.dismiss(drive);
    await this.answer(drive);
  }

  async neverAsk(drive) {
    this.dismiss(drive);
    await this.answer(drive, true);
    this.toast(i18n.t('Ninaivu will not ask about this drive again. The Import page can undo it.'));
    this.check();
  }

  async importFrom(drive) {
    this.dismiss(drive);
    await this.answer(drive);
    try {
      await this.openImport(drive.path);
    } catch (exc) {
      // The notice is gone by now; say why nothing opened rather than leave
      // a click that seemed to do nothing.
      this.toast(exc?.message || i18n.t('The Import page could not be opened.'), true);
      return;
    }
    this.toast(i18n.t('The drive is the source. Check the destination, then press Start.'));
  }

  async goToImport(drive) {
    this.dismiss(drive);
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
    this.dismiss(drive);
    await this.answer(drive);
    this.toast(i18n.t('Image Capture is open on the Ninaivu computer. When the photos are in a folder, add it on the Import page.'));
  }

  async exportTo(drive) {
    try {
      await this.json('/api/admin/drives/export', { method: 'POST', body: { id: drive.id } });
    } catch (exc) {
      this.toast(exc.message, true);
      return;
    }
    this.dismiss(drive);
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
        this.dismissed.delete(drive.id);
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
