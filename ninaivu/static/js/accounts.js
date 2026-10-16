/**
 * Sign-in, profiles and the admin People panel.
 *
 * The server is the only authority on permissions — everything here is
 * presentation. Hiding a button the API would refuse anyway keeps the
 * interface honest about what each role can actually do.
 */

import { reportUnauthorized } from './api.js';
import * as i18n from './i18n.js';
import { takeoverSection } from './handover.js';

const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
};

async function json(url, options = {}) {
  const isForm = options.body instanceof FormData;
  // A FormData body must keep the browser's own multipart Content-Type,
  // boundary and all — setting application/json here silently drops the file.
  // X-Requested-With: the avatar goes up as multipart, which a cross-origin
  // form could also send, so the server takes one only from a script that
  // says so. See _refuse_cross_origin_writes in ninaivu/__init__.py.
  const headers = { Accept: 'application/json', 'X-Requested-With': 'fetch' };
  if (options.body && !isForm && typeof options.body !== 'string') {
    headers['Content-Type'] = 'application/json';
  }
  const response = await fetch(url, {
    ...options,
    headers: { ...headers, ...(options.headers || {}) },
    body: isForm || typeof options.body === 'string' || options.body === undefined
      ? options.body
      : JSON.stringify(options.body),
  });
  const text = await response.text();
  const data = text ? JSON.parse(text) : null;
  if (!response.ok) {
    // A 401 carrying `needs_password` is a request asking you to confirm
    // who you are before something irreversible — not a session that has
    // ended. See api.js: the same 401 means two different things, and
    // only one of them should raise the sign-in screen.
    if (response.status === 401 && !data?.needs_password) reportUnauthorized(url);
    throw Object.assign(new Error(i18n.fromServer(data, response.statusText)), { status: response.status, data });
  }
  return data;
}

export const accountsApi = {
  state: () => json('/api/auth/state'),
  setup: (body) => json('/api/auth/setup', { method: 'POST', body }),
  login: (body) => json('/api/auth/login', { method: 'POST', body }),
  logout: () => json('/api/auth/logout', { method: 'POST' }),
  forgot: (body) => json('/api/auth/forgot', { method: 'POST', body }),
  me: () => json('/api/me'),
  updateMe: (body) => json('/api/me', { method: 'POST', body }),
  changePassword: (body) => json('/api/me/password', { method: 'POST', body }),
  uploadAvatar: (file) => {
    const form = new FormData();
    form.append('avatar', file);
    return json('/api/me/avatar', { method: 'POST', body: form });
  },
  removeAvatar: () => json('/api/me/avatar', { method: 'DELETE' }),
  profiles: () => json('/api/auth/profiles'),
  enter: (id, secret) => json('/api/auth/enter', { method: 'POST', body: { id, secret } }),
  people: () => json('/api/people'),
  createPerson: (body) => json('/api/people', { method: 'POST', body }),
  updatePerson: (id, body) => json(`/api/people/${id}`, { method: 'POST', body }),
  signOutPerson: (id) => json(`/api/people/${id}/signout`, { method: 'POST' }),
  deletePerson: (id) => json(`/api/people/${id}`, { method: 'DELETE' }),
  scopeFolders: () => json('/api/people/folders'),
  setVisibility: (ids, visibility) =>
    json('/api/visibility', { method: 'POST', body: { ids, visibility } }),
  setFolderVisibility: (folder, visibility) =>
    json('/api/visibility/folder', { method: 'POST', body: { folder, visibility } }),
  visibilityRules: () => json('/api/visibility/rules'),
};

/* ======================================================================== */

/* The colours behind people's initials — the same eight as AVATAR_COLORS in
   ninaivu/server/auth.py, each dark enough for white initials to read. The
   server already sends each person's colour; this is only the fallback for a
   payload without one, worked out the same way (from the id) so a face never
   changes colour between the picker, the lock screen and the top bar. */
export const AVATAR_COLOURS = [
  '#1f6fb2', '#6247d6', '#b5306f', '#b4531a',
  '#1d7a47', '#0d7477', '#c02e36', '#7a5c1e',
];

export function avatarColour(person) {
  if (person && person.color) return person.color;
  const id = Number(person && person.id);
  if (Number.isFinite(id)) return AVATAR_COLOURS[Math.abs(id) % AVATAR_COLOURS.length];
  let hash = 0;
  for (const ch of String((person && person.name) || '')) hash = (hash * 31 + ch.codePointAt(0)) >>> 0;
  return AVATAR_COLOURS[hash % AVATAR_COLOURS.length];
}

export function avatarNode(person, size = 32) {
  const wrap = el('span', 'avatar');
  wrap.style.width = `${size}px`;
  wrap.style.height = `${size}px`;
  wrap.style.fontSize = `${Math.round(size * 0.38)}px`;
  if (person.avatar) {
    const img = el('img');
    img.src = person.avatar;
    img.alt = '';
    wrap.appendChild(img);
  } else {
    wrap.style.background = avatarColour(person);
    wrap.appendChild(el('span', null, person.initials || '?'));
  }
  if (person.role) wrap.dataset.role = person.role;
  return wrap;
}

export function roleBadge(role, label) {
  const badge = el('span', `role-badge role-${role}`, label || role);
  return badge;
}

/* ========================================================================
   Sign-in / first-run screen
   ======================================================================== */

export class Gate {
  constructor(root, { onSignedIn, toast }) {
    this.root = root;
    this.onSignedIn = onSignedIn;
    this.toast = toast;
    this.mode = 'picker';
    this.selected = null;
  }

  show(state, mode) {
    this.state = state;
    this.mode = mode
      || (state.setup_required ? 'setup'
        : state.face === 'admin' ? 'login' : 'picker');
    this.selected = null;
    this.root.hidden = false;
    this.render();
  }

  hide() {
    this.root.hidden = true;
  }

  render() {
    this.root.innerHTML = '';
    const card = el('div', this.mode === 'picker' ? 'gate-card picker' : 'gate-card');
    card.appendChild(this.brand());

    if (this.mode === 'setup') this.renderSetup(card);
    else if (this.mode === 'login') this.renderLogin(card);
    else if (this.mode === 'unlock') this.renderUnlock(card);
    else if (this.mode === 'forgot') this.renderForgot(card);
    else this.renderPicker(card);

    card.appendChild(this.languages());
    this.root.appendChild(card);
    setTimeout(() => card.querySelector('input')?.focus(), 60);
  }

  /**
   * A language switch on the sign-in card.
   *
   * It belongs here and not only in the topbar: `.gate` is `position: fixed;
   * inset: 0`, so the topbar's own language button is underneath it. Somebody
   * who reads only Tamil met an English sign-in screen with the control that
   * would have changed it covered up.
   *
   * Only shown when there is a choice to make.
   */
  languages() {
    const row = el('div', 'gate-languages');
    if (i18n.LANGUAGES.length < 2) return row;
    for (const { code, name } of i18n.LANGUAGES) {
      const pick = el('button', 'gate-lang', name);
      pick.type = 'button';
      pick.lang = code;
      pick.setAttribute('aria-pressed', String(code === i18n.language()));
      if (code === i18n.language()) pick.classList.add('on');
      pick.onclick = async () => {
        await i18n.use(code);
        this.render();                 // built from scratch, so this is enough
      };
      row.appendChild(pick);
    }
    return row;
  }

  brand() {
    const brand = el('div', 'gate-brand');
    brand.innerHTML = `
      <svg viewBox="0 0 32 32" aria-hidden="true" class="gate-mark">
        <path d="M4 15 16 5l12 10v11a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2Z"/>
        <path d="M12 28v-7a4 4 0 0 1 8 0v7"/>
      </svg>`;
    // The household's own name on the family app, as the page shows once in;
    // the console keeps Ninaivu's.
    // "Ninaivu" from the server means nobody has named the home: that is the
    // product's own name, so it is written in the page's language.
    const given = this.state?.house_name;
    const named = Boolean(given) && given !== 'Ninaivu';
    const name = this.state?.face === 'admin' || !named ? i18n.t('Ninaivu') : given;
    brand.appendChild(el('h1', null, name));
    if (this.state?.face === 'admin') {
      brand.appendChild(el('span', 'gate-face', i18n.t('Admin console')));
    }
    return brand;
  }

  /* -- who's watching? -------------------------------------------------- */

  renderPicker(card) {
    const profiles = this.state.profiles || [];
    card.appendChild(el('p', 'gate-lede', profiles.length
      ? i18n.t("Who's watching?")
      : i18n.t('No profiles yet — an admin can add them in the console.')));

    const grid = el('div', 'picker-grid');
    for (const person of profiles) {
      const tile = el('button', 'picker-tile');
      tile.type = 'button';
      tile.appendChild(avatarNode(person, 88));
      const name = el('span', 'picker-name', person.name);
      if (person.locked) {
        const lock = el('span', 'picker-lock');
        lock.innerHTML = '<svg viewBox="0 0 24 24"><rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg>';
        name.appendChild(lock);
      }
      tile.appendChild(name);
      tile.appendChild(el('span', 'picker-role', i18n.role(person.role_label)));
      tile.onclick = () => this.choose(person);
      grid.appendChild(tile);
    }

    if (this.state.open_browsing) {
      const tile = el('button', 'picker-tile guest-tile');
      tile.type = 'button';
      const face = el('span', 'avatar');
      face.style.cssText = 'width:88px;height:88px;background:var(--surface-2);color:var(--text-3)';
      face.innerHTML = '<svg viewBox="0 0 24 24" style="width:34px;height:34px"><circle cx="12" cy="8" r="3.6"/><path d="M4.5 20a7.5 7.5 0 0 1 15 0"/></svg>';
      tile.appendChild(face);
      tile.appendChild(el('span', 'picker-name', i18n.t('Just looking')));
      tile.appendChild(el('span', 'picker-role', i18n.t('Public media only')));
      tile.onclick = () => {
        this.hide();
        this.onSignedIn(null);
      };
      grid.appendChild(tile);
    }
    card.appendChild(grid);

    const foot = el('div', 'picker-foot');
    const byName = el('button', 'gate-skip',
      i18n.t('Sign in with a username instead'));
    byName.type = 'button';
    byName.onclick = () => { this.mode = 'login'; this.render(); };
    foot.appendChild(byName);
    card.appendChild(foot);
  }

  async choose(person) {
    if (!person.locked) {
      try {
        const result = await accountsApi.enter(person.id, '');
        this.hide();
        this.onSignedIn(result.user);
      } catch (exc) {
        this.toast(exc.message, true);
      }
      return;
    }
    this.selected = person;
    this.mode = 'unlock';
    this.render();
  }

  /* -- PIN / password for a locked profile ------------------------------ */

  renderUnlock(card) {
    const person = this.selected;
    const isPin = person.kind === 'pin';

    const head = el('div', 'unlock-head');
    head.appendChild(avatarNode(person, 64));
    const who = el('div');
    who.appendChild(el('strong', null, person.name));
    who.appendChild(el('div', 'hint',
      isPin ? i18n.t('Enter your PIN') : i18n.t('Enter your password')));
    head.appendChild(who);
    card.appendChild(head);

    const form = el('form', 'gate-form');
    const input = el('input', 'pin-input');
    input.type = 'password';
    input.name = 'secret';
    input.autocomplete = isPin ? 'one-time-code' : 'current-password';
    if (isPin) {
      input.inputMode = 'numeric';
      input.pattern = '[0-9]*';
      input.maxLength = 8;
      input.placeholder = '••••';
    }
    form.appendChild(isPin ? input : secretBox(input));

    const error = el('p', 'gate-error');
    error.hidden = true;
    form.appendChild(error);

    const submit = el('button', 'btn primary gate-submit', i18n.t('Enter'));
    submit.type = 'submit';
    form.appendChild(submit);

    form.onsubmit = async (event) => {
      event.preventDefault();
      error.hidden = true;
      submit.disabled = true;
      try {
        const result = await accountsApi.enter(person.id, input.value);
        this.hide();
        this.onSignedIn(result.user);
      } catch (exc) {
        error.textContent = exc.message;
        error.hidden = false;
        submit.disabled = false;
        input.value = '';
        input.focus();
      }
    };
    card.appendChild(form);
    card.appendChild(this.forgotLink(
      isPin ? i18n.t('Forgot your PIN?') : i18n.t('Forgot your password?'), { person }));

    const back = el('button', 'gate-skip', i18n.t('← Someone else'));
    back.type = 'button';
    back.onclick = () => { this.mode = 'picker'; this.render(); };
    card.appendChild(back);
  }

  /* -- username + password ---------------------------------------------- */

  renderSetup(card) {
    card.appendChild(el('p', 'gate-lede', i18n.t(
      'Welcome. Create the administrator profile — the person who decides what everyone else can see.')));
    this.credentialForm(card, {
      withName: true,
      withCode: !!this.state.setup_code_required,
      submit: i18n.t('Create profile'), busy: i18n.t('Creating…'),
      action: (body) => accountsApi.setup(body),
    });
  }

  renderLogin(card) {
    card.appendChild(el('p', 'gate-lede', this.state.face === 'admin'
      ? i18n.t('Sign in with your administrator account.')
      : i18n.t("Your family's media, at home.")));
    const form = this.credentialForm(card, {
      submit: i18n.t('Sign in'), busy: i18n.t('Signing in…'),
      action: (body) => accountsApi.login(body),
    });
    card.appendChild(this.forgotLink(i18n.t('Forgot your password?'), {
      username: () => form.elements.username?.value || '',
    }));

    if (this.state.face !== 'admin' && (this.state.profiles || []).length) {
      const back = el('button', 'gate-skip', i18n.t('← Back to profiles'));
      back.type = 'button';
      back.onclick = () => { this.mode = 'picker'; this.render(); };
      card.appendChild(back);
    } else if (this.state.face !== 'admin' && this.state.open_browsing) {
      const skip = el('button', 'gate-skip', i18n.t('Continue as a guest'));
      skip.type = 'button';
      skip.onclick = () => { this.hide(); this.onSignedIn(null); };
      card.appendChild(skip);
    }
  }

  credentialForm(card, { withName = false, withCode = false, submit: label, busy, action }) {
    const form = el('form', 'gate-form');
    form.autocomplete = 'on';
    if (withCode) {
      form.appendChild(el('p', 'gate-lede', i18n.t(
        'You are setting up from another device. Enter the setup code shown where Ninaivu started — in its window or its log.')));
      form.appendChild(this.field('setup_code', i18n.t('Setup code'), 'text', {
        autocomplete: 'off', required: true, autocapitalize: 'characters', placeholder: 'A1B2C3',
      }));
    }
    if (withName) {
      form.appendChild(this.field('name', i18n.t('Your name'), 'text', {
        autocomplete: 'name', placeholder: i18n.t('e.g. Alex'),
      }));
    }
    form.appendChild(this.field('username', i18n.t('Username'), 'text', {
      autocomplete: 'username', required: true, autocapitalize: 'none',
      placeholder: withName ? i18n.t('lowercase, no spaces') : '',
    }));
    form.appendChild(this.field('password', i18n.t('Password'), 'password', {
      autocomplete: withName ? 'new-password' : 'current-password', required: true,
      placeholder: withName ? i18n.t('at least 8 characters') : '',
    }));
    if (withName) {
      // A new password is typed twice: one typo in a single box and the
      // administrator is locked out of the console on the first day.
      form.appendChild(this.field('password_confirm', i18n.t('Password again'), 'password', {
        autocomplete: 'new-password', required: true,
        placeholder: i18n.t('type it once more'),
      }));
    }

    const error = el('p', 'gate-error');
    error.hidden = true;
    form.appendChild(error);

    const button = el('button', 'btn primary gate-submit', label);
    button.type = 'submit';
    form.appendChild(button);

    form.onsubmit = async (event) => {
      event.preventDefault();
      error.hidden = true;
      if (withName && form.elements.password.value !== form.elements.password_confirm.value) {
        error.textContent = i18n.t(PASSWORDS_DIFFER);
        error.hidden = false;
        form.elements.password_confirm.focus();
        return;
      }
      button.disabled = true;
      button.textContent = busy;
      try {
        const result = await action(Object.fromEntries(new FormData(form).entries()));
        this.hide();
        this.onSignedIn(result.user);
      } catch (exc) {
        error.textContent = exc.message;
        error.hidden = false;
        button.disabled = false;
        button.textContent = label;
      }
    };
    card.appendChild(form);
    return form;
  }

  /* -- forgot password -------------------------------------------------- */

  forgotLink(text, from) {
    const link = el('button', 'gate-forgot', text);
    link.type = 'button';
    link.onclick = () => {
      this.forgot = {
        person: from.person || null,
        username: from.username ? from.username() : '',
        pin: from.person?.kind === 'pin',
      };
      this.mode = 'forgot';
      this.render();
    };
    return link;
  }

  /**
   * "Forgot password?" for a server that sends no email. There is no link to
   * mail, so the request goes to the administrators instead: the console's
   * Overview and People pages show it, and an administrator gives a
   * temporary password (or a new PIN). An administrator who forgot their own
   * is told how to set a new one on the computer Ninaivu runs on.
   */
  renderForgot(card) {
    const { person, username, pin } = this.forgot || {};
    const admin = this.state.face === 'admin' || person?.role === 'admin';
    card.appendChild(el('h2', 'gate-title',
      pin ? i18n.t('Forgot your PIN?') : i18n.t('Forgot your password?')));
    card.appendChild(el('p', 'gate-lede', i18n.t(
      'Ninaivu stays at home and sends no email, so a new one is set by an administrator.')));

    const family = el('div', 'gate-help');
    family.appendChild(el('strong', null, i18n.t('Ask an administrator')));
    family.appendChild(el('p', null, i18n.t(
      'They will see your request in the console and give you a temporary password or a new PIN. You choose your own the next time you sign in.')));
    const form = el('form', 'gate-form');
    if (!person) {
      form.appendChild(this.field('username', i18n.t('Username'), 'text', {
        autocomplete: 'username', required: true, autocapitalize: 'none', value: username,
      }));
    }
    const error = el('p', 'gate-error');
    error.hidden = true;
    form.appendChild(error);
    const ask = el('button', admin ? 'btn gate-submit' : 'btn primary gate-submit',
      i18n.t('Let an administrator know'));
    ask.type = 'submit';
    form.appendChild(ask);
    form.onsubmit = async (event) => {
      event.preventDefault();
      error.hidden = true;
      ask.disabled = true;
      try {
        await accountsApi.forgot(person ? { id: person.id }
          : { username: form.elements.username.value });
        const done = el('p', 'gate-done', i18n.t(
          'Done. An administrator will see it the next time they open the console. Telling them in person is quicker still.'));
        done.setAttribute('role', 'status');
        form.replaceWith(done);
      } catch (exc) {
        error.textContent = exc.message;
        error.hidden = false;
        ask.disabled = false;
      }
    };
    family.appendChild(form);

    const own = el('div', 'gate-help');
    own.appendChild(el('strong', null, i18n.t('If you are the administrator')));
    own.appendChild(el('p', null, i18n.t(
      'Another administrator can reset it on the console’s People page. Or, on the computer Ninaivu runs on, open the Ninaivu Control Panel and press “Reset a password…”, or run this command:')));
    own.appendChild(el('code', 'gate-command', `ninaivu reset-password ${person?.username || username || i18n.t('your-username')}`));

    // Whoever is likelier to be reading comes first.
    card.append(...(admin ? [own, family] : [family, own]));

    const back = el('button', 'gate-skip', i18n.t('← Back to sign in'));
    back.type = 'button';
    back.onclick = () => {
      if (person) { this.selected = person; this.mode = 'unlock'; } else { this.mode = 'login'; }
      this.render();
    };
    card.appendChild(back);
  }

  field(name, label, type, attrs = {}) {
    const wrap = el('label', 'gate-field');
    wrap.appendChild(el('span', null, label));
    const input = el('input');
    input.name = name;
    input.type = type;
    Object.entries(attrs).forEach(([key, value]) => {
      if (value === true) input.setAttribute(key, '');
      else if (value) input.setAttribute(key, value);
    });
    wrap.appendChild(type === 'password' ? secretBox(input) : input);
    return wrap;
  }
}

const PASSWORDS_DIFFER = i18n.key("The two passwords don't match. Type the same password in both boxes.");

/**
 * A password box with a Show/Hide button inside it and a note when Caps Lock
 * is on: the two things that most often make a right password come out wrong.
 */
export function secretBox(input) {
  const box = el('div', 'gate-secret');
  box.appendChild(input);
  const reveal = el('button', 'gate-reveal', i18n.t('Show'));
  reveal.type = 'button';
  reveal.setAttribute('aria-pressed', 'false');
  reveal.onclick = () => {
    const showing = input.type === 'password';
    input.type = showing ? 'text' : 'password';
    reveal.textContent = showing ? i18n.t('Hide') : i18n.t('Show');
    reveal.setAttribute('aria-pressed', String(showing));
    input.focus();
  };
  box.appendChild(reveal);
  const caps = el('small', 'gate-caps', i18n.t('Caps Lock is on'));
  caps.hidden = true;
  const check = (event) => {
    if (event.getModifierState) caps.hidden = !event.getModifierState('CapsLock');
  };
  input.addEventListener('keydown', check);
  input.addEventListener('keyup', check);
  input.addEventListener('blur', () => { caps.hidden = true; });
  const wrap = el('div', 'gate-secret-wrap');
  wrap.append(box, caps);
  return wrap;
}

/* ========================================================================
   Profile sheet — name, colour, picture, password
   ======================================================================== */

export class ProfileSheet {
  constructor(root, { toast, onChange, houseName, face }) {
    this.root = root;
    this.toast = toast;
    this.onChange = onChange;
    //: shown as the placeholder, so "empty" visibly means "the household name"
    this.houseName = houseName || '';
    //: 'home' or 'admin' — decides whether leaving is called signing out
    this.face = face || 'home';
  }

  open(user) {
    this.user = user;
    this.root.hidden = false;
    this.render();
  }

  close() {
    this.root.hidden = true;
  }

  render() {
    const user = this.user;
    this.root.innerHTML = '';
    const card = el('div', 'sheet-card');

    const head = el('div', 'sheet-head');
    head.appendChild(el('h2', null, i18n.t('Your profile')));
    const close = el('button', 'icon-btn');
    close.type = 'button';
    close.setAttribute('aria-label', i18n.t('Close'));
    close.innerHTML = '<svg viewBox="0 0 24 24"><path d="M18 6 6 18M6 6l12 12"/></svg>';
    close.onclick = () => this.close();
    head.appendChild(close);
    card.appendChild(head);

    // --- picture -------------------------------------------------------
    const pictureRow = el('div', 'profile-picture');
    const preview = avatarNode(user, 88);
    pictureRow.appendChild(preview);

    const actions = el('div', 'profile-picture-actions');
    const pick = el('button', 'btn', i18n.t('Change picture'));
    pick.type = 'button';
    const input = el('input');
    input.type = 'file';
    input.accept = 'image/png,image/jpeg,image/webp,image/gif';
    input.hidden = true;
    pick.onclick = () => input.click();
    input.onchange = async () => {
      const file = input.files?.[0];
      if (!file) return;
      pick.disabled = true;
      pick.textContent = i18n.t('Uploading…');
      try {
        const result = await accountsApi.uploadAvatar(file);
        this.user = result.user;
        this.onChange(result.user);
        this.render();
        this.toast(i18n.t('Picture updated.'));
      } catch (exc) {
        this.toast(exc.message, true);
        pick.disabled = false;
        pick.textContent = i18n.t('Change picture');
      }
    };
    actions.append(pick, input);

    if (user.avatar) {
      const remove = el('button', 'btn ghost', i18n.t('Remove'));
      remove.type = 'button';
      remove.onclick = async () => {
        const result = await accountsApi.removeAvatar();
        this.user = result.user;
        this.onChange(result.user);
        this.render();
      };
      actions.appendChild(remove);
    }
    actions.appendChild(el('p', 'hint',
      i18n.t('JPEG, PNG, WebP or GIF, up to 6 MB. Cropped to a square.')));
    pictureRow.appendChild(actions);
    card.appendChild(pictureRow);

    // --- identity ------------------------------------------------------
    const identity = el('div', 'sheet-section');
    identity.appendChild(el('h3', null, i18n.t('Display name')));
    const nameRow = el('div', 'row');
    const nameInput = el('input', 'input');
    nameInput.value = user.name;
    nameInput.maxLength = 60;
    const saveName = el('button', 'btn', i18n.t('Save'));
    saveName.type = 'button';
    saveName.onclick = async () => {
      try {
        const updated = await accountsApi.updateMe({ name: nameInput.value });
        this.user = updated;
        this.onChange(updated);
        this.render();
        this.toast(i18n.t('Profile updated.'));
      } catch (exc) { this.toast(exc.message, true); }
    };
    nameRow.append(nameInput, saveName);
    identity.appendChild(nameRow);

    // --- what they call the home ---------------------------------------
    //
    // Only they ever see this, which is exactly what makes it safe to let a
    // child set it to something daft. Guests are left out: a guest tile is
    // usually shared and short-lived.
    if (user.role !== 'guest') {
      identity.appendChild(el('h3', null, i18n.t('Name for this home')));
      identity.appendChild(el('p', 'hint', i18n.t(
        'Shown at the top of your gallery, and on your phone’s tab. Only you see it — leave it empty to use the household name.')));
      const homeRow = el('div', 'row');
      const homeInput = el('input', 'input');
      homeInput.value = user.home_label || '';
      homeInput.maxLength = 40;
      homeInput.placeholder = this.houseName || i18n.t('Ninaivu');
      homeInput.setAttribute('aria-label', i18n.t('Name for this home'));
      const saveHome = el('button', 'btn', i18n.t('Save'));
      saveHome.type = 'button';
      const commitHome = async () => {
        saveHome.disabled = true;
        try {
          const updated = await accountsApi.updateMe({ home_label: homeInput.value });
          this.user = updated;
          this.onChange(updated);
          this.render();
          this.toast(updated.home_label
            ? i18n.t('This home is now “{name}” for you.',
              { name: updated.home_label })
            : i18n.t('Back to the household name.'));
        } catch (exc) {
          this.toast(exc.message, true);
          saveHome.disabled = false;
        }
      };
      saveHome.onclick = commitHome;
      homeInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') commitHome(); });
      homeRow.append(homeInput, saveHome);
      identity.appendChild(homeRow);
    }

    identity.appendChild(el('h3', null, i18n.t('Colour')));
    const swatches = el('div', 'swatches');
    for (const color of AVATAR_COLOURS) {
      const swatch = el('button', 'swatch');
      swatch.type = 'button';
      swatch.style.background = color;
      swatch.setAttribute('aria-label', color);
      if (color === user.color) swatch.classList.add('active');
      swatch.onclick = async () => {
        const updated = await accountsApi.updateMe({ color });
        this.user = updated;
        this.onChange(updated);
        this.render();
      };
      swatches.appendChild(swatch);
    }
    identity.appendChild(swatches);

    // --- an administrator's tile on the family app's sign-in screen ------
    if (user.role === 'admin') {
      identity.appendChild(el('h3', null, i18n.t('Sign-in screen')));
      const row = el('label', 'toggle');
      const input = el('input');
      input.type = 'checkbox';
      input.checked = user.on_picker !== false;
      input.onchange = async () => {
        try {
          const updated = await accountsApi.updateMe({ on_picker: input.checked });
          this.user = updated;
          this.onChange(updated);
          this.toast(updated.on_picker
            ? i18n.t('You’re on the sign-in screen.')
            : i18n.t('You’ll sign in with your username.'));
        } catch (exc) {
          this.toast(exc.message, true);
          input.checked = !input.checked;
        }
      };
      row.append(input, el('span', null,
        i18n.t('Show my picture on the family app’s sign-in screen')));
      identity.appendChild(row);
      identity.appendChild(el('p', 'hint', i18n.t(
        'Untick to leave it off and sign in with your username instead.')));
    }
    card.appendChild(identity);

    // --- password ------------------------------------------------------
    const security = el('div', 'sheet-section');
    security.appendChild(el('h3', null, user.must_change
      ? i18n.t('Set your password') : i18n.t('Change password')));
    if (user.must_change) {
      security.appendChild(el('p', 'hint warn', i18n.t(
        'An admin set a temporary password for you. Choose your own now.')));
    }
    const passwordForm = el('form', 'stack');
    if (!user.must_change) {
      const current = el('input', 'input');
      current.type = 'password';
      current.name = 'current';
      current.placeholder = i18n.t('Current password');
      current.autocomplete = 'current-password';
      passwordForm.appendChild(current);
    }
    const next = el('input', 'input');
    next.type = 'password';
    next.name = 'password';
    next.placeholder = i18n.t('New password (8+ characters)');
    next.autocomplete = 'new-password';
    passwordForm.appendChild(next);

    const again = el('input', 'input');
    again.type = 'password';
    again.name = 'password_confirm';
    again.placeholder = i18n.t('New password again');
    again.autocomplete = 'new-password';
    passwordForm.appendChild(again);

    const passwordError = el('p', 'gate-error');
    passwordError.hidden = true;
    passwordForm.appendChild(passwordError);

    const savePassword = el('button', 'btn primary', i18n.t('Update password'));
    savePassword.type = 'submit';
    passwordForm.appendChild(savePassword);
    passwordForm.onsubmit = async (event) => {
      event.preventDefault();
      passwordError.hidden = true;
      if (next.value !== again.value) {
        passwordError.textContent = i18n.t(PASSWORDS_DIFFER);
        passwordError.hidden = false;
        again.focus();
        return;
      }
      const body = Object.fromEntries(new FormData(passwordForm).entries());
      try {
        await accountsApi.changePassword(body);
        this.toast(i18n.t('Password updated. Other devices were signed out.'));
        this.user = { ...this.user, must_change: false };
        this.onChange(this.user);
        this.render();
      } catch (exc) {
        passwordError.textContent = exc.message;
        passwordError.hidden = false;
      }
    };
    security.appendChild(passwordForm);
    card.appendChild(security);

    // --- taking over as administrator (only for a named successor) -----
    if (user.role === 'family') card.appendChild(takeoverSection(this.toast));

    // --- footer --------------------------------------------------------
    const foot = el('div', 'sheet-foot');
    const role = el('div', 'role-line');
    role.append(roleBadge(user.role, i18n.role(user.role_label)));
    if (user.scope) {
      role.appendChild(el('span', 'hint',
        i18n.t('Library scope: {scope}', { scope: user.scope })));
    }
    foot.appendChild(role);
    const footActions = el('div', 'row');
    // Both faces log out the same way. The gallery calls it "Log off" (it
    // used to say "Switch profile", which hid that it signs the person out);
    // the console keeps "Sign out".
    const leaving = el('button', 'btn ghost',
      this.face === 'admin' ? i18n.t('Sign out') : i18n.t('Log off'));
    leaving.type = 'button';
    leaving.onclick = async () => {
      try {
        await accountsApi.logout();
      } catch { /* already gone; reload lands on the sign-in screen anyway */ }
      window.location.reload();
    };
    footActions.appendChild(leaving);
    foot.appendChild(footActions);
    card.appendChild(foot);

    this.root.appendChild(card);
  }
}
