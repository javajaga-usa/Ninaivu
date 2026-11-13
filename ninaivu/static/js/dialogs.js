/** Shared dialog lifecycle. Existing hidden/open attributes remain supported.
 * Forms own their work; this layer owns focus, stacking and viewport bounds. */
const SELECTOR = '.modal, .sheet, .palette, dialog';
const FOCUSABLE = 'button, input, select, textarea, a[href], summary, [tabindex], [contenteditable="true"]';
let layer;
let headingId = 0;

export function installDialogLayer() {
  if (!layer) layer = new DialogLayer();
  return layer;
}

/** Register once, or update a controller's options on reuse.
 * initialFocus is a selector (or false for the dialog itself). onDismiss owns
 * guarded closing and may return a promise; onClose runs once for every actual
 * close, including direct hidden/open changes. dispose releases a controller.
 */
export function registerDialog(node, options = {}) {
  return installDialogLayer().register(node, options);
}

export function activeDialog() {
  return layer?.top()?.node || null;
}

class DialogLayer {
  constructor() {
    this.records = new Map();
    this.disposed = new WeakSet();
    this.stack = [];
    this.lastFocus = document.activeElement;
    this.scan(document.body);
    this.observer = new MutationObserver(records => {
      const changed = [];
      for (const record of records) {
        if (record.type === 'childList') {
          for (const node of record.addedNodes) if (node.nodeType === 1) this.scan(node);
        } else if (record.target.isConnected && !this.disposed.has(record.target)
            && (this.records.has(record.target) || record.target.matches(SELECTOR))) {
          this.register(record.target);
          changed.push(record.target);
        }
      }
      this.sync(changed);
    });
    this.observer.observe(document.body, { subtree: true, childList: true,
      attributes: true, attributeFilter: ['hidden', 'open'] });
    document.addEventListener('focusin', event => {
      const candidate = event.target.closest(SELECTOR);
      if (candidate && !this.records.has(candidate) && !this.disposed.has(candidate)) this.register(candidate);
      this.sync(candidate ? [candidate] : []);
      const top = this.top();
      if (top && !this.suspended() && !top.node.contains(event.target)) this.focus(top);
      else this.lastFocus = event.target;
    });
    document.addEventListener('keydown', event => {
      if (event.key === 'Tab' || event.key === 'Escape') this.sync();
      if (event.key === 'Escape') this.escapeRecord = this.top();
      if (event.key === 'Escape' && this.escapeRecord?.dismissing) {
        event.preventDefault(); event.stopPropagation();
      }
      this.tab(event);
    }, true);
    document.addEventListener('keydown', event => {
      const top = this.top();
      if (event.defaultPrevented || event.key !== 'Escape' || this.suspended()) return;
      // A feature's own handler may already have closed its dialog. Never let
      // that same key dismiss the parent or become a gallery shortcut.
      if (this.escapeRecord && this.escapeRecord !== top) { event.preventDefault(); return; }
      if (!top || !this.escapeRecord) return;
      if (top.options.escape) {
        event.preventDefault();
        this.dismiss(top, 'escape');
      }
    });
    document.addEventListener('pointerdown', event => { this.pointerStart = event.target; }, true);
    document.addEventListener('click', event => {
      const top = this.top();
      if (!event.defaultPrevented && top?.options.backdrop && event.target === top.node
          && this.pointerStart === top.node && !this.suspended()) this.dismiss(top, 'backdrop');
      this.pointerStart = null;
    });
    const resize = () => {
      cancelAnimationFrame(this.viewportFrame);
      this.viewportFrame = requestAnimationFrame(() => this.viewport());
    };
    window.addEventListener('resize', resize);
    window.visualViewport?.addEventListener('resize', resize);
    window.visualViewport?.addEventListener('scroll', resize);
    this.sync();
  }

  scan(root) {
    if (root.matches?.(SELECTOR) && !this.disposed.has(root)) this.register(root);
    root.querySelectorAll(SELECTOR).forEach(node => { if (!this.disposed.has(node)) this.register(node); });
  }

  register(node, options = {}) {
    this.disposed.delete(node);
    let record = this.records.get(node);
    if (record) { Object.assign(record.options, options); return record.controller; }
    const escape = node.matches('.gate, .lock-screen') ? false
      : node.tagName === 'DIALOG' || node.classList.contains('palette') ? null : true;
    record = { node, options: { escape, backdrop: false, ...options },
      opener: null, zIndex: node.style.zIndex, inert: node.inert, tabIndex: node.getAttribute('tabindex'),
      role: node.getAttribute('role'), modal: node.getAttribute('aria-modal'), label: node.getAttribute('aria-labelledby') };
    if (node.matches('.modal, .sheet, dialog')) {
      if (!node.hasAttribute('role')) node.setAttribute('role', 'dialog');
      if (!node.hasAttribute('aria-modal')) node.setAttribute('aria-modal', 'true');
    }
    node.classList.add('dialog-managed');
    if (!node.hasAttribute('tabindex')) node.tabIndex = -1;
    record.cancel = event => {
      if (this.top() === record && record.options.escape !== null && !event.defaultPrevented) {
        event.preventDefault();
        if (record.options.escape) this.dismiss(record, 'escape');
      }
    };
    node.addEventListener('cancel', record.cancel);
    record.keydown = event => {
      // Handle guarded keyboard dismissals before the browser's CloseWatcher:
      // repeated native cancel events are not always cancellable in Chromium.
      if (node.tagName === 'DIALOG' && event.key === 'Escape' && !event.defaultPrevented && this.top() === record
          && (record.options.escape === false || (record.options.escape === true && !this.suspended()))) {
        event.preventDefault(); event.stopPropagation();
        if (record.options.escape) this.dismiss(record, 'escape');
      }
    };
    if (node.tagName === 'DIALOG') node.addEventListener('keydown', record.keydown);
    record.controller = {
      open: ({ opener = document.activeElement } = {}) => {
        if (!this.visible(record)) record.opener = opener;
        if (node.tagName === 'DIALOG') { if (!node.open) node.showModal(); }
        else node.hidden = false;
        this.sync([node]);
        if (record.options.initialFocus !== undefined && !this.suspended()) this.focus(record);
      },
      close: () => {
        if (node.tagName === 'DIALOG') { if (node.open) node.close(); }
        else node.hidden = true;
        this.sync();
      },
      requestClose: reason => this.dismiss(record, reason || 'close'),
      dispose: () => { record.controller.close(); this.release(record, true); this.sync(); },
    };
    this.records.set(node, record);
    return record.controller;
  }

  visible(record) {
    const node = record.node;
    return node.isConnected && !node.hidden && (node.tagName !== 'DIALOG' || node.open)
      && node.getClientRects().length > 0;
  }

  top() { return this.stack.at(-1); }

  label(record) {
    if (record.label !== null || record.node.hasAttribute('aria-label') || record.node.classList.contains('palette')) return;
    const current = record.node.getAttribute('aria-labelledby');
    if (current && current !== record.derivedLabel) return;
    const heading = record.node.querySelector('h1, h2, h3');
    if (record.heading !== heading) {
      if (record.createdHeadingId) record.heading.removeAttribute('id');
      record.heading = heading;
      record.createdHeadingId = false;
    }
    if (!heading) { record.node.removeAttribute('aria-labelledby'); record.derivedLabel = null; return; }
    if (!heading.id) {
      let id;
      do { id = `dialog-heading-${++headingId}`; } while (document.getElementById(id));
      heading.id = id;
      record.createdHeadingId = true;
    }
    record.derivedLabel = heading.id;
    record.node.setAttribute('aria-labelledby', heading.id);
  }

  suspended() {
    // Authentication and the screen lock retain their own non-dismissible flow.
    return Boolean(document.querySelector('.gate:not([hidden]), .lock-screen:not([hidden])'));
  }

  controls(record) {
    return [...record.node.querySelectorAll(FOCUSABLE)].filter(node => node.tabIndex >= 0
      && !node.matches(':disabled') && !node.closest('[inert]') && node.getClientRects().length
      && getComputedStyle(node).visibility !== 'hidden');
  }

  focus(record, target = null) {
    if (typeof record.options.initialFocus === 'string') target ||= record.node.querySelector(record.options.initialFocus);
    if (target && (!target.isConnected || target.matches(':disabled') || target.closest('[hidden], [inert]')
        || !target.getClientRects().length || getComputedStyle(target).visibility === 'hidden')) target = null;
    target ||= record.options.initialFocus === false ? record.node : this.controls(record)[0] || record.node;
    target.focus({ preventScroll: true });
  }

  tab(event) {
    const top = this.top();
    if (event.key !== 'Tab' || event.defaultPrevented || !top || this.suspended()) return;
    const controls = this.controls(top), first = controls[0], last = controls.at(-1);
    if (!first) { event.preventDefault(); this.focus(top); return; }
    const active = document.activeElement;
    if (!top.node.contains(active) || active === top.node || (event.shiftKey ? active === first : active === last)) {
      event.preventDefault(); this.focus(top, event.shiftKey ? last : first);
    }
  }

  dismiss(record, reason) {
    if (this.top() !== record || record.dismissing) return;
    record.dismissing = true;
    const finish = () => { record.dismissing = false; this.sync(); };
    try {
      const pending = record.options.onDismiss ? record.options.onDismiss(reason) : record.controller.close();
      if (pending && typeof pending.then === 'function') return Promise.resolve(pending).finally(finish);
      finish();
    } catch (error) { finish(); throw error; }
  }

  sync(order = []) {
    if (this.syncing) return;
    this.syncing = true;
    try {
      const before = this.top();
      const closed = this.stack.filter(record => !this.visible(record));
      this.stack = this.stack.filter(record => this.visible(record));
      for (const node of [...order, ...this.records.keys()]) {
        const record = this.records.get(node);
        if (!record || this.stack.includes(record) || !this.visible(record)) continue;
        if (!record.opener) record.opener = record.node.contains(document.activeElement) ? this.lastFocus : document.activeElement;
        this.stack.push(record);
      }
      const top = this.top();
      for (const record of this.stack) this.label(record);
      for (const record of this.records.values()) {
        const index = this.stack.indexOf(record);
        record.node.inert = record.inert || (index >= 0 && record !== top && !record.node.contains(top.node));
        record.node.style.zIndex = index >= 0 ? String(300 + index * 2) : record.zIndex;
      }
      if (this.stack.length && !this.scroll) {
        this.scroll = { x: window.scrollX, y: window.scrollY };
        document.documentElement.classList.toggle('dialog-scrollable', innerWidth > document.documentElement.clientWidth);
        document.documentElement.classList.add('dialog-open');
      } else if (!this.stack.length && this.scroll) {
        document.documentElement.classList.remove('dialog-open', 'dialog-scrollable');
        window.scrollTo(this.scroll.x, this.scroll.y);
        this.scroll = null;
      }
      this.viewport();
      for (const record of closed) record.options.onClose?.();
      if (!this.suspended()) {
        if (top && !top.node.contains(document.activeElement)) {
          const opener = before && closed.includes(before) ? before.opener : null;
          this.focus(top, opener && top.node.contains(opener) && opener.isConnected ? opener : null);
        } else if (!top && before && closed.includes(before)
            && (before.node.contains(document.activeElement) || document.activeElement === document.body)) {
          const opener = before.opener;
          if (opener?.isConnected && !opener.closest('[hidden], [inert]')) opener.focus({ preventScroll: true });
        }
      }
      for (const record of closed) record.opener = null;
      for (const record of [...this.records.values()]) if (!record.node.isConnected) this.release(record);
    } finally { this.syncing = false; }
  }

  viewport() {
    if (!this.stack.length) return;
    const viewport = window.visualViewport;
    const standalone = matchMedia('(display-mode: standalone)').matches || navigator.standalone;
    const keyboard = viewport && viewport.height < document.documentElement.clientHeight * .75;
    const height = standalone && !keyboard ? 'var(--app-h, 100dvh)' : `${viewport?.height || innerHeight}px`;
    for (const { node } of this.stack) {
      node.classList.toggle('dialog-keyboard', Boolean(keyboard));
      node.style.setProperty('--dialog-height', height);
      node.style.setProperty('--dialog-top', `${viewport?.offsetTop || 0}px`);
      node.style.setProperty('--dialog-left', `${viewport?.offsetLeft || 0}px`);
      node.style.setProperty('--dialog-width', `${viewport?.width || innerWidth}px`);
    }
  }

  release(record, disposed = false) {
    if (disposed) this.disposed.add(record.node);
    this.records.delete(record.node);
    record.node.removeEventListener('cancel', record.cancel);
    record.node.removeEventListener('keydown', record.keydown);
    record.node.classList.remove('dialog-managed', 'dialog-keyboard');
    record.node.style.zIndex = record.zIndex;
    record.node.inert = record.inert;
    if (record.tabIndex === null) record.node.removeAttribute('tabindex');
    else record.node.setAttribute('tabindex', record.tabIndex);
    for (const [name, value] of [['role', record.role], ['aria-modal', record.modal], ['aria-labelledby', record.label]]) {
      if (value === null) record.node.removeAttribute(name);
      else record.node.setAttribute(name, value);
    }
    if (record.createdHeadingId) record.heading.removeAttribute('id');
    for (const name of ['--dialog-height', '--dialog-top', '--dialog-left', '--dialog-width']) record.node.style.removeProperty(name);
    this.stack = this.stack.filter(item => item !== record);
  }
}
