/**
 * The screen lock — both apps, after a spell with nobody there.
 *
 * Locking is not signing out. The session stays good, so whatever the page is
 * doing carries on behind the lock: a phone backup keeps sending, the server's
 * own work never noticed. What the lock does is cover everything, and the
 * server refuses to show anything more (423 Locked) until the person gives the
 * PIN or password they signed in with — so removing this overlay in the
 * browser's developer tools reveals a page that cannot load a single photo.
 * See the screen lock in ninaivu/server/auth.py.
 *
 * "Somebody there" is a tap, a key, a scroll, the mouse moving, a video or
 * song playing, or a slideshow the app says is running. Polling for status is
 * not somebody, which is why this has to be decided here and told to the
 * server rather than inferred from requests.
 *
 * Tabs of the same app share one session, so they share one lock: activity in
 * any tab keeps them all open, and locking or unlocking one does the rest.
 */

import * as i18n from './i18n.js';

/** How often the page looks at the clock. */
const TICK = 15000;
/** How often the server is told somebody is there, at most. */
const PING_EVERY = 60000;
/** Tabs tell each other about activity this often, at most. */
const SHARE_EVERY = 5000;

const nativeFetch = window.fetch.bind(window);

async function call(url, body) {
  const response = await nativeFetch(url, {
    method: body === undefined ? 'GET' : 'POST',
    headers: { Accept: 'application/json',
      ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: 'same-origin',
  });
  let data = null;
  try { data = await response.json(); } catch { /* not JSON */ }
  return { status: response.status, ok: response.ok, data: data || {} };
}

export class ScreenLock {
  /**
   * @param {object} options
   * @param {() => boolean} [options.isBusy] the app's own "somebody is
   *   watching": a slideshow, a photo frame.
   * @param {() => void} [options.onSignedOut] the session ended.
   * @param {() => void} [options.onUnlocked] open again: reload whatever the
   *   page could not fetch while it was locked.
   */
  constructor({ isBusy, onSignedOut, onUnlocked } = {}) {
    this.isBusy = isBusy || (() => false);
    this.onSignedOut = onSignedOut || (() => window.location.reload());
    this.onUnlocked = onUnlocked || (() => {});
    this.running = false;
    this.locked = false;
    this.lockAfter = 0;
    this.lastInput = Date.now();
    this.lastPing = Date.now();
    this.lastShared = 0;
    this.timer = null;
    this.root = null;
    this.user = null;
    this.unlockWith = 'password';
    this.channel = null;
    this.noticed = this.noticed.bind(this);
    this.watchFetch();
  }

  /** Begin watching, for a signed-in person. Quietly does nothing if the
   *  server has no lock (older, or switched off). */
  async start(user) {
    this.stop();
    if (!user || user.anonymous || !user.id) return;
    let state;
    try {
      state = await call('/api/auth/session');
    } catch {
      return;
    }
    if (!state.ok || !state.data.signed_in || !state.data.lock_after) return;
    this.user = state.data.user || user;
    this.unlockWith = state.data.unlock_with || 'password';
    this.lockAfter = Number(state.data.lock_after) * 1000;
    this.running = true;
    this.lastInput = Date.now();
    this.lastPing = Date.now();
    for (const type of ['pointerdown', 'keydown', 'wheel', 'touchstart']) {
      window.addEventListener(type, this.noticed, { capture: true, passive: true });
    }
    for (const type of ['pointermove', 'scroll']) {
      window.addEventListener(type, this.noticed, { capture: true, passive: true });
    }
    try {
      this.channel = new BroadcastChannel('ninaivu-lock');
      this.channel.onmessage = (event) => this.heard(event.data || {});
    } catch { /* one tab is all there is */ }
    this.timer = setInterval(() => this.tick(), TICK);
    if (state.data.locked) this.show();
  }

  stop() {
    this.running = false;
    clearInterval(this.timer);
    this.timer = null;
    for (const type of ['pointerdown', 'keydown', 'wheel', 'touchstart', 'pointermove', 'scroll']) {
      window.removeEventListener(type, this.noticed, { capture: true });
    }
    this.channel?.close();
    this.channel = null;
    this.hide();
  }

  /* -- noticing somebody ------------------------------------------------ */

  noticed() {
    if (this.locked) return;
    const now = Date.now();
    this.lastInput = now;
    if (now - this.lastShared > SHARE_EVERY) {
      this.lastShared = now;
      this.channel?.postMessage({ type: 'active', at: now });
    }
  }

  heard(message) {
    if (message.type === 'active') {
      this.lastInput = Math.max(this.lastInput, Number(message.at) || 0);
    } else if (message.type === 'locked') {
      this.show({ quiet: true });
    } else if (message.type === 'unlocked' && this.locked) {
      this.hide();
      this.lastInput = Date.now();
      this.onUnlocked();
    }
  }

  somethingPlaying() {
    return [...document.querySelectorAll('video, audio')]
      .some((media) => !media.paused && !media.ended);
  }

  async tick() {
    if (!this.running || this.locked) return;
    const now = Date.now();
    if (this.somethingPlaying() || this.isBusy()) this.noticed();
    // Idle first. A tab the browser put to sleep can wake twenty minutes
    // later with input still unreported; telling the server about it then
    // would keep open a screen nobody has touched since.
    if (now - this.lastInput >= this.lockAfter) {
      try { await call('/api/auth/lock', {}); } catch { /* locked here regardless */ }
      this.show();
      return;
    }
    if (this.lastInput > this.lastPing && now - this.lastPing >= PING_EVERY) {
      this.lastPing = now;
      try {
        const answer = await call('/api/auth/active', {});
        if (answer.status === 423) this.show();
        else if (answer.status === 401) this.onSignedOut();
      } catch { /* the server is away; the next tick tries again */ }
    }
  }

  /** Any request the server refused because the session is locked — another
   *  tab locked it, or this one slept through its own timer — shows the lock. */
  watchFetch() {
    if (window.__ninaivuLockFetch) return;
    window.__ninaivuLockFetch = true;
    window.fetch = async (...args) => {
      const response = await nativeFetch(...args);
      if (response.status === 423 && this.running) this.show();
      return response;
    };
  }

  /* -- the lock screen ------------------------------------------------ */

  show({ quiet = false } = {}) {
    if (!this.running || this.locked) return;
    this.locked = true;
    document.body.classList.add('screen-locked');
    if (!quiet) this.channel?.postMessage({ type: 'locked' });
    this.root = this.build();
    document.body.appendChild(this.root);
    this.root.querySelector('input, button')?.focus();
  }

  /** The same lock, drawn again in the language just chosen. */
  redraw() {
    if (!this.locked) return;
    const fresh = this.build();
    this.root?.replaceWith(fresh);
    this.root = fresh;
    this.root.querySelector('input, button')?.focus();
  }

  hide() {
    this.locked = false;
    document.body.classList.remove('screen-locked');
    this.root?.remove();
    this.root = null;
  }

  build() {
    const user = this.user || {};
    const root = document.createElement('div');
    root.className = 'lock-screen';
    root.setAttribute('role', 'dialog');
    root.setAttribute('aria-modal', 'true');
    root.setAttribute('aria-labelledby', 'lock-title');

    const card = document.createElement('form');
    card.className = 'lock-card';
    card.noValidate = true;

    const face = document.createElement('div');
    face.className = 'lock-avatar';
    face.style.background = user.color || 'var(--accent)';
    if (user.avatar) {
      const img = document.createElement('img');
      img.src = user.avatar;
      img.alt = '';
      face.append(img);
    } else {
      face.textContent = user.initials || '';
    }

    const title = document.createElement('h2');
    title.id = 'lock-title';
    title.textContent = i18n.t('Ninaivu is locked');
    const who = document.createElement('p');
    who.className = 'lock-who';
    who.textContent = user.name || user.username || '';

    card.append(face, title, who);

    let field = null;
    if (this.unlockWith !== 'none') {
      field = document.createElement('input');
      field.className = 'input lock-secret';
      field.type = 'password';
      field.autocomplete = this.unlockWith === 'pin' ? 'off' : 'current-password';
      if (this.unlockWith === 'pin') field.inputMode = 'numeric';
      field.placeholder = this.unlockWith === 'pin' ? i18n.t('PIN') : i18n.t('Password');
      field.setAttribute('aria-label', field.placeholder);
      card.append(field);
    }

    const error = document.createElement('p');
    error.className = 'lock-error';
    error.setAttribute('role', 'alert');

    const unlock = document.createElement('button');
    unlock.type = 'submit';
    unlock.className = 'btn primary lock-unlock';
    unlock.textContent = i18n.t('Unlock');

    const note = document.createElement('p');
    note.className = 'lock-note';
    note.textContent = i18n.t('Anything in progress carries on while it is locked.');

    const out = document.createElement('button');
    out.type = 'button';
    out.className = 'btn ghost small lock-signout';
    out.textContent = i18n.t('Sign out');
    out.onclick = () => this.signOut();

    // A language switch, as the sign-in card has: the lock covers the
    // topbar's button, and a lock screen in a language you cannot read is
    // a locked door with no handle.
    const languages = document.createElement('div');
    languages.className = 'gate-languages';
    if (i18n.LANGUAGES.length > 1) {
      for (const { code, name } of i18n.LANGUAGES) {
        const pick = document.createElement('button');
        pick.type = 'button';
        pick.className = 'gate-lang' + (code === i18n.language() ? ' on' : '');
        pick.lang = code;
        pick.textContent = name;
        pick.setAttribute('aria-pressed', String(code === i18n.language()));
        pick.onclick = async () => {
          await i18n.use(code);
          this.redraw();
          // The page behind the lock keeps the choice on the profile, so
          // unlocking and reloading does not put the old language back.
          document.dispatchEvent(new CustomEvent('ninaivu:language', { detail: code }));
        };
        languages.appendChild(pick);
      }
    }

    card.append(error, unlock, note, out, languages);
    card.onsubmit = (event) => {
      event.preventDefault();
      this.unlock(field ? field.value : '', { field, error, unlock });
    };
    root.append(card);
    return root;
  }

  async unlock(secret, { field, error, unlock }) {
    if (field && !secret) {
      field.focus();
      return;
    }
    unlock.disabled = true;
    error.textContent = '';
    try {
      const answer = await call('/api/auth/unlock', { secret });
      if (answer.ok) {
        this.hide();
        this.lastInput = Date.now();
        this.lastPing = Date.now();
        this.channel?.postMessage({ type: 'unlocked' });
        this.onUnlocked();
        return;
      }
      if (answer.status === 401) {
        error.textContent = i18n.t('Too many wrong attempts, so you have been signed out.');
        setTimeout(() => this.onSignedOut(), 1500);
        return;
      }
      if (answer.status === 404) {
        // A server from before the lock: there is nothing to unlock.
        this.hide();
        return;
      }
      error.textContent = this.unlockWith === 'pin'
        ? i18n.t("That PIN isn't right.") : i18n.t("That password isn't right.");
      if (field) { field.value = ''; field.focus(); }
    } catch {
      error.textContent = i18n.t('Cannot reach Ninaivu right now.');
    } finally {
      unlock.disabled = false;
    }
  }

  async signOut() {
    try { await call('/api/auth/logout', {}); } catch { /* signing out anyway */ }
    this.stop();
    this.onSignedOut();
  }
}
