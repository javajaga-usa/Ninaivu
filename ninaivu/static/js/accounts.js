/**
 * Sign-in, profiles and the admin People panel.
 *
 * The server is the only authority on permissions — everything here is
 * presentation. Hiding a button the API would refuse anyway keeps the
 * interface honest about what each role can actually do.
 */

import { reportUnauthorized } from './api.js';
import * as i18n from './i18n.js';

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
  const headers = { Accept: 'application/json' };
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
    throw Object.assign(new Error(data?.error || response.statusText), { status: response.status, data });
  }
  return data;
}

export const accountsApi = {
  state: () => json('/api/auth/state'),
  setup: (body) => json('/api/auth/setup', { method: 'POST', body }),
  login: (body) => json('/api/auth/login', { method: 'POST', body }),
  logout: () => json('/api/auth/logout', { method: 'POST' }),
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
    wrap.style.background = person.color || 'var(--accent)';
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
    const name = this.state?.face === 'admin' ? 'Ninaivu' : (this.state?.house_name || 'Ninaivu');
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
      tile.appendChild(el('span', 'picker-role', person.role_label));
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
    form.appendChild(input);

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
      submit: i18n.t('Create profile'), busy: i18n.t('Creating…'),
      action: (body) => accountsApi.setup(body),
    });
  }

  renderLogin(card) {
    card.appendChild(el('p', 'gate-lede', this.state.face === 'admin'
      ? i18n.t('Sign in with your administrator account.')
      : i18n.t("Your family's media, at home.")));
    this.credentialForm(card, {
      submit: i18n.t('Sign in'), busy: i18n.t('Signing in…'),
      action: (body) => accountsApi.login(body),
    });

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

  credentialForm(card, { withName = false, submit: label, busy, action }) {
    const form = el('form', 'gate-form');
    form.autocomplete = 'on';
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

    const error = el('p', 'gate-error');
    error.hidden = true;
    form.appendChild(error);

    const button = el('button', 'btn primary gate-submit', label);
    button.type = 'submit';
    form.appendChild(button);

    form.onsubmit = async (event) => {
      event.preventDefault();
      error.hidden = true;
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
    wrap.appendChild(input);
    return wrap;
  }
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
      homeInput.placeholder = this.houseName || 'Ninaivu';
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
    for (const color of ['#0b7fd4', '#6d5efc', '#e05299', '#e8833a', '#2fbf71', '#00a3a3', '#d8353d', '#8b5cf6']) {
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

    const passwordError = el('p', 'gate-error');
    passwordError.hidden = true;
    passwordForm.appendChild(passwordError);

    const savePassword = el('button', 'btn primary', i18n.t('Update password'));
    savePassword.type = 'submit';
    passwordForm.appendChild(savePassword);
    passwordForm.onsubmit = async (event) => {
      event.preventDefault();
      passwordError.hidden = true;
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

    // --- footer --------------------------------------------------------
    const foot = el('div', 'sheet-foot');
    const role = el('div', 'role-line');
    role.append(roleBadge(user.role, user.role_label));
    if (user.scope) {
      role.appendChild(el('span', 'hint',
        i18n.t('Library scope: {scope}', { scope: user.scope })));
    }
    foot.appendChild(role);
    const footActions = el('div', 'row');
    // Both faces log out the same way, but they mean different things by it.
    // The gallery hands the tablet to somebody else; the console has no
    // profiles to switch between, so calling it "Switch profile" there sent
    // admins looking for a sign-out button that did not exist.
    const leaving = el('button', 'btn ghost',
      this.face === 'admin' ? i18n.t('Sign out') : i18n.t('Switch profile'));
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

/* ========================================================================
   People panel — admin only
   ======================================================================== */

export class PeoplePanel {
  constructor(root, { toast, currentUser }) {
    this.root = root;
    this.toast = toast;
    this.me = currentUser;
    this.folders = [];
  }

  async open() {
    this.root.hidden = false;
    this.root.innerHTML = '<div class="sheet-card"><p class="hint">Loading…</p></div>';
    try {
      const [people, folders] = await Promise.all([
        accountsApi.people(),
        accountsApi.scopeFolders().catch(() => ({ folders: [] })),
      ]);
      this.people = people.people;
      this.roles = people.roles;
      this.folders = folders.folders || [];
      this.render();
    } catch (exc) {
      this.toast(exc.message, true);
      this.close();
    }
  }

  close() {
    this.root.hidden = true;
  }

  async refresh() {
    const people = await accountsApi.people();
    this.people = people.people;
    this.render();
  }

  render() {
    this.root.innerHTML = '';
    const card = el('div', 'sheet-card wide');

    const head = el('div', 'sheet-head');
    head.appendChild(el('h2', null, 'People'));
    const close = el('button', 'icon-btn');
    close.type = 'button';
    close.setAttribute('aria-label', i18n.t('Close'));
    close.innerHTML = '<svg viewBox="0 0 24 24"><path d="M18 6 6 18M6 6l12 12"/></svg>';
    close.onclick = () => this.close();
    head.appendChild(close);
    card.appendChild(head);

    card.appendChild(el('p', 'hint',
      'Admins manage the library. Family members view, download and keep their own favourites. Guests see public media only.'));

    const list = el('div', 'people-list');
    for (const person of this.people) list.appendChild(this.personRow(person));
    card.appendChild(list);

    card.appendChild(this.addForm());
    this.root.appendChild(card);
  }

  personRow(person) {
    const row = el('div', `person${person.active ? '' : ' disabled'}`);
    row.appendChild(avatarNode(person, 40));

    const identity = el('div', 'person-identity');
    const line = el('div', 'person-name');
    line.appendChild(el('strong', null, person.name));
    line.appendChild(roleBadge(person.role, person.role_label));
    if (person.id === this.me.id) line.appendChild(el('span', 'you-tag', 'you'));
    if (!person.active) line.appendChild(el('span', 'off-tag', 'disabled'));
    identity.appendChild(line);

    const meta = [`@${person.username}`];
    if (person.scope) meta.push(`only ${person.scope}`);
    else if (person.role !== 'admin') meta.push('whole library');
    if (person.sessions) meta.push(`${person.sessions} signed-in device${person.sessions > 1 ? 's' : ''}`);
    if (person.must_change) meta.push('temporary password');
    identity.appendChild(el('div', 'person-meta', meta.join(' · ')));
    row.appendChild(identity);

    const controls = el('div', 'person-controls');

    const roleSelect = el('select', 'select small');
    for (const role of this.roles) {
      const option = el('option', null, role.label);
      option.value = role.value;
      if (role.value === person.role) option.selected = true;
      roleSelect.appendChild(option);
    }
    roleSelect.onchange = () => {
      const body = { role: roleSelect.value };
      // An administrator signs in with a password; one without could not.
      if (roleSelect.value === 'admin' && person.role !== 'admin' && !person.has_password) {
        const password = prompt(
          `${person.name} has no password yet. Temporary password for them.\nThey will be asked to choose their own when they sign in.`,
          randomPassword(),
        );
        if (!password) { roleSelect.value = person.role; return; }
        body.password = password;
      }
      this.patch(person.id, body);
    };
    controls.appendChild(roleSelect);

    // Scope only means something for non-admins.
    const scopeSelect = el('select', 'select small');
    const anyFolder = el('option', null, 'Whole library');
    anyFolder.value = '';
    scopeSelect.appendChild(anyFolder);
    for (const folder of this.folders) {
      const option = el('option', null,
        `${'  '.repeat(folder.depth)}${folder.path.split('/').pop()} (${folder.count})`);
      option.value = folder.path;
      if (folder.path === person.scope) option.selected = true;
      scopeSelect.appendChild(option);
    }
    scopeSelect.disabled = person.role === 'admin';
    scopeSelect.title = person.role === 'admin'
      ? 'Admins always see the whole library'
      : 'Folder this profile is limited to';
    scopeSelect.onchange = () => this.patch(person.id, { scope: scopeSelect.value });
    controls.appendChild(scopeSelect);

    const menu = el('div', 'person-actions');
    const toggle = el('button', 'btn small ghost', person.active ? 'Disable' : 'Enable');
    toggle.type = 'button';
    toggle.disabled = person.id === this.me.id;
    toggle.onclick = () => this.patch(person.id, { active: !person.active });
    menu.appendChild(toggle);

    const reset = el('button', 'btn small ghost', 'Reset password');
    reset.type = 'button';
    reset.onclick = () => this.resetPassword(person);
    menu.appendChild(reset);

    if (person.sessions) {
      const signout = el('button', 'btn small ghost', 'Sign out');
      signout.type = 'button';
      signout.onclick = async () => {
        await accountsApi.signOutPerson(person.id);
        this.toast(`${person.name} was signed out everywhere.`);
        this.refresh();
      };
      menu.appendChild(signout);
    }
    controls.appendChild(menu);
    row.appendChild(controls);
    return row;
  }

  async patch(id, body) {
    try {
      await accountsApi.updatePerson(id, body);
      await this.refresh();
      this.toast('Saved.');
    } catch (exc) {
      this.toast(exc.message, true);
      this.refresh();
    }
  }

  async resetPassword(person) {
    const password = prompt(
      `Temporary password for ${person.name}.\nThey will be asked to choose their own when they sign in.`,
      randomPassword(),
    );
    if (!password) return;
    try {
      await accountsApi.updatePerson(person.id, { password });
      this.toast(`New password set for ${person.name}. Tell them: ${password}`);
      this.refresh();
    } catch (exc) {
      this.toast(exc.message, true);
    }
  }

  addForm() {
    const section = el('form', 'add-person');
    section.appendChild(el('h3', null, 'Add someone'));

    const grid = el('div', 'add-grid');
    const name = el('input', 'input');
    name.placeholder = 'Name';
    name.name = 'name';
    const username = el('input', 'input');
    username.placeholder = 'username';
    username.name = 'username';
    username.autocapitalize = 'none';
    username.required = true;
    const password = el('input', 'input');
    password.placeholder = 'Temporary password';
    password.name = 'password';
    password.value = randomPassword();
    password.required = true;

    const role = el('select', 'select');
    role.name = 'role';
    for (const option of this.roles) {
      const node = el('option', null, option.label);
      node.value = option.value;
      if (option.value === 'family') node.selected = true;
      role.appendChild(node);
    }

    const scope = el('select', 'select');
    scope.name = 'scope';
    const whole = el('option', null, 'Whole library');
    whole.value = '';
    scope.appendChild(whole);
    for (const folder of this.folders) {
      const option = el('option', null,
        `${'  '.repeat(folder.depth)}${folder.path.split('/').pop()} (${folder.count})`);
      option.value = folder.path;
      scope.appendChild(option);
    }

    grid.append(name, username, password, role, scope);
    section.appendChild(grid);

    const error = el('p', 'gate-error');
    error.hidden = true;
    section.appendChild(error);

    const submit = el('button', 'btn primary', 'Create profile');
    submit.type = 'submit';
    section.appendChild(submit);

    section.onsubmit = async (event) => {
      event.preventDefault();
      error.hidden = true;
      const body = Object.fromEntries(new FormData(section).entries());
      try {
        await accountsApi.createPerson({ ...body, must_change: true });
        this.toast(`${body.name || body.username} added. Temporary password: ${body.password}`);
        await this.refresh();
      } catch (exc) {
        error.textContent = exc.message;
        error.hidden = false;
      }
    };
    return section;
  }
}

function randomPassword() {
  const words = ['river', 'maple', 'copper', 'lantern', 'harbour', 'meadow',
    'cedar', 'amber', 'summit', 'willow', 'cobalt', 'ember'];
  const pick = () => words[Math.floor(Math.random() * words.length)];
  return `${pick()}-${pick()}-${Math.floor(10 + Math.random() * 89)}`;
}
