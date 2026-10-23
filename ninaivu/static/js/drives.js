/**
 * A pendrive, a memory card, an external hard drive or a phone was plugged in
 * to the Ninaivu computer: one notice at the top of the console.
 *
 *   click the notice, or            → the Import page, with the drive as its
 *   Import media from the device      source (nothing is copied until Start)
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
 * A library folder that is not connected, found by its id on another disk (a
 * Mac mounts a second disk of the same name as "/Volumes/Photos 1"), is a
 * notice too: Use this folder records the choice, and Restart now makes the
 * move, which is done only at start (storage/library_id.py).
 *
 * The Import page lists every drive plugged in now, with what it holds and
 * what can be done with it: use the whole drive as a source, browse it for a
 * folder, eject it, or be asked about it again.
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
  constructor({ json, toast, openImport, openImportPage, browseDrive }) {
    this.json = json;
    this.toast = toast;
    this.openImport = openImport;
    this.openImportPage = openImportPage;
    this.browseDrive = browseDrive || (async () => {});
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
    /** Library folders found at a new place, and the ones set aside here. */
    this.lastMoved = [];
    this.movedDismissed = new Set();
    i18n.onChange(() => {
      if (this.modal && !this.modal.hidden) this.redraw();
      if (this.lastList) {
        this.renderPlugged(this.lastList);
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
    this.lastMoved = data.moved_libraries || [];
    this.renderPlugged(data.drives);
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
    const moved = (this.lastMoved || []).filter((m) => m.found?.length
      && !this.movedDismissed.has(m.old));
    if (!waiting.length && !moved.length && !this.tray) return;
    const tray = this.buildTray();
    // A moved library's notice is drawn again when its state changes (a
    // folder chosen, another found), and is keyed by what it shows.
    const movedKey = (m) => `moved:${m.old}|${m.found.join('|')}|${m.confirmed || ''}`;
    const ids = new Set([...waiting.map((d) => d.id), ...moved.map(movedKey)]);
    for (const node of [...tray.children]) if (!ids.has(node.dataset.id)) node.remove();
    const has = (id) => [...tray.children].some((node) => node.dataset.id === id);
    for (const entry of moved) {
      if (!has(movedKey(entry))) tray.prepend(this.movedNotice(entry, movedKey(entry)));
    }
    for (const drive of waiting) {
      if (!has(drive.id)) tray.appendChild(this.notice(drive));
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
    const act = (label, handler, look = 'ghost') => {
      const button = el('button', `btn small ${look}`, i18n.t(label));
      button.type = 'button';
      button.onclick = handler;
      actions.appendChild(button);
    };
    // Said in words as well: with only "Export" written under it, the notice
    // read as an offer to copy the library out, the opposite of what a drive
    // or a phone being plugged in is usually for.
    if (!unreadable) {
      act(i18n.key('Import media from the device'), () => this.importFrom(drive), 'primary');
    }
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

  /**
   * A library folder that is not connected, and the folders found carrying
   * its id. Which is the library is the administrator's to say: a backup copy
   * carries the same id, and while the library's own drive is unplugged the
   * copy looks exactly like a library that moved.
   */
  movedNotice(entry, id) {
    const node = el('div', 'drive-notice drive-notice-moved');
    node.dataset.id = id;
    const main = el('div', 'drive-notice-main');
    const icon = el('span', 'drive-notice-icon');
    icon.innerHTML = ICON;
    const text = el('span', 'drive-notice-text');
    const actions = el('div', 'drive-notice-actions');
    const act = (label, handler, look = 'ghost') => {
      const button = el('button', `btn small ${look}`, label);
      button.type = 'button';
      button.onclick = handler;
      actions.appendChild(button);
      return button;
    };
    if (entry.confirmed) {
      text.appendChild(el('strong', '', i18n.t('Restart Ninaivu to finish moving the library')));
      text.appendChild(el('span', 'drive-notice-name', entry.confirmed));
      text.appendChild(el('span', 'drive-notice-hint', i18n.t(
        'The library folder {old} is used from {new} after a restart. The photographs, albums and names come along; nothing is read again.',
        { old: entry.old, new: entry.confirmed })));
      act(i18n.t('Restart now'), () => this.restart(), 'primary');
    } else {
      text.appendChild(el('strong', '', i18n.t('Your library folder may be at a new place')));
      text.appendChild(el('span', 'drive-notice-name', entry.old));
      text.appendChild(el('span', 'drive-notice-hint', i18n.t(
        'It is not connected, but a folder with the same library mark was found on another disk. If that is the library, use it. If it is a backup copy, leave it and plug the library drive in.')));
      for (const folder of entry.found) {
        act(i18n.t('Use {folder}', { folder }), () => this.useMoved(entry, folder),
          entry.found.length === 1 ? 'primary' : 'ghost');
      }
    }
    main.append(icon, text);
    const later = el('button', 'icon-btn drive-notice-close');
    later.type = 'button';
    later.title = i18n.t('Not now');
    later.setAttribute('aria-label', i18n.t('Not now'));
    later.textContent = '×';
    later.onclick = () => {
      this.movedDismissed.add(entry.old);
      this.renderNotices(this.lastList || []);
    };
    actions.appendChild(later);
    node.append(main, actions);
    return node;
  }

  async useMoved(entry, folder) {
    try {
      await this.json('/api/admin/library/relocation', { method: 'POST', body: { old: entry.old, new: folder } });
    } catch (exc) {
      this.toast(exc.message, true);
      return;
    }
    this.toast(i18n.t('Chosen. Restart Ninaivu to move the library there.'));
    this.check();
  }

  async restart() {
    try {
      await this.json('/api/admin/server/restart', { method: 'POST', body: {} });
    } catch (exc) {
      this.toast(exc.message, true);
      return;
    }
    this.toast(i18n.t('Restarting. The console comes back in a moment.'));
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
    // Once the copy is over: let the drive go, so it can be pulled out.
    this.ejectButton = el('button', 'btn ghost', i18n.t('Eject'));
    this.ejectButton.type = 'button';
    this.ejectButton.hidden = true;
    this.ejectButton.onclick = async () => {
      if (await this.eject(drive)) this.close();
    };
    const close = el('button', 'btn primary', i18n.t('Close'));
    close.type = 'button';
    close.onclick = () => this.close();
    foot.append(this.stopButton, this.ejectButton, close);
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
    let result;
    try {
      result = await this.openImport(drive.path);
    } catch (exc) {
      // The notice is gone by now; say why nothing opened rather than leave
      // a click that seemed to do nothing.
      this.toast(exc?.message || i18n.t('The Import page could not be opened.'), true);
      return;
    }
    if (result?.state === 'inside') {
      this.toast(i18n.t('Folders on this drive are sources already: {folders}. Press Start to import from them, or remove them to import the whole drive.',
        { folders: result.folders.join(', ') }));
    } else if (result?.state === 'already') {
      this.toast(i18n.t('The drive is a source already. Check the destination, then press Start.'));
    } else {
      this.toast(i18n.t('The drive is the source. Check the destination, then press Start.'));
    }
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
    if (this.ejectButton) this.ejectButton.hidden = state.running || !this.drive?.id;
  }

  async stopCopy() {
    try {
      await this.json('/api/admin/drives/export/stop', { method: 'POST' });
    } catch (exc) { this.toast(exc.message, true); }
  }

  /* -- the drives plugged in, on the Import page ------------------------------ */

  /** Eject *drive*. True when it was let go. */
  async eject(drive) {
    let answer;
    try {
      answer = await this.json('/api/admin/drives/eject', { method: 'POST', body: { id: drive.id } });
    } catch (exc) {
      this.toast(exc.message, true);
      return false;
    }
    this.toast(said(answer?.message) || i18n.t('You can unplug the drive now.'));
    this.check();
    return true;
  }

  /** What the drive is to Ninaivu, in a few words; "" when nothing. */
  stateOf(drive) {
    const holds = {
      library: i18n.key('Holds the photo library'),
      data: i18n.key('Holds Ninaivu’s own data'),
      import: i18n.key('Imports are copied onto it'),
      copy: i18n.key('Holds the second copy or the backups'),
    }[drive.holds];
    if (holds) return i18n.t(holds);
    if (drive.in_use === 'import') return i18n.t('Importing from it now');
    if (drive.in_use === 'export') return i18n.t('The library is being copied onto it');
    if (drive.sources?.length) {
      return i18n.t('Sources on it: {folders}', { folders: drive.sources.join(', ') });
    }
    if (drive.quiet) return i18n.t('Not asked about when plugged in');
    return '';
  }

  /**
   * Every drive plugged in now, under the phones the Import page lists
   * itself. It used to list only the drives set aside with "Don't ask
   * again": a drive whose notice was closed could be reached only by typing
   * its path or browsing /Volumes for it.
   */
  renderPlugged(list) {
    const anchor = document.querySelector('#ar-devices');
    if (!anchor) return;
    let box = document.querySelector('#ar-plugged');
    // Windows' phones are the Import page's own cards, just above.
    const shown = (list || []).filter((d) => !d.shell);
    if (!shown.length) {
      if (box) box.hidden = true;
      return;
    }
    if (!box) {
      box = el('div', 'ar-devices ar-plugged');
      box.id = 'ar-plugged';
      anchor.after(box);
    }
    box.hidden = false;
    box.replaceChildren(el('p', 'hint ar-plugged-title', i18n.t('Plugged in now')));
    for (const drive of shown) {
      const phone = drive.kind === 'phone';
      const unreadable = drive.readable === false;
      const card = el('div', 'ar-device-card');
      card.dataset.id = drive.id;
      const info = el('div', 'ar-device-info');
      info.appendChild(el('span', 'ar-device-badge', `${phone ? '📱' : '💽'} ${drive.label || drive.path}`));
      const details = [];
      if (drive.total) {
        details.push(i18n.t('{free} free of {total}', { free: size(drive.free), total: size(drive.total) }));
      }
      const state = this.stateOf(drive);
      if (state) details.push(state);
      if (details.length) info.appendChild(el('span', 'hint', details.join(' · ')));
      card.appendChild(info);

      const actions = el('div', 'ar-device-actions');
      const act = (label, handler, look = 'ghost') => {
        const button = el('button', `btn small ${look}`, label);
        button.type = 'button';
        button.onclick = handler;
        actions.appendChild(button);
        return button;
      };
      if (unreadable) {
        if (this.imageCapture) act(i18n.t('Open Image Capture'), () => this.openImageCapture(drive));
      } else if (!drive.holds) {
        const added = (drive.sources || []).length > 0;
        if (!added) act(i18n.t('Use the whole drive'), () => this.importFrom(drive), 'primary');
        act(i18n.t('Choose a folder on it…'), () => this.browseDrive(drive));
        if (!phone && !drive.in_use) act(i18n.t('Eject'), () => this.eject(drive));
      }
      if (drive.quiet) {
        act(i18n.t('Ask again'), async () => {
          try {
            await this.json('/api/admin/drives/ask-again', { method: 'POST', body: { id: drive.id } });
          } catch (exc) { this.toast(exc.message, true); return; }
          this.dismissed.delete(drive.id);
          this.check();
        });
      }
      if (actions.children.length) card.appendChild(actions);
      box.appendChild(card);
    }
  }
}

/** A server message: `{text, key, params}` in the language on screen. */
function said(message) {
  if (!message || typeof message === 'string') return message || '';
  return message.key ? i18n.t(message.key, message.params || {}) : (message.text || '');
}
