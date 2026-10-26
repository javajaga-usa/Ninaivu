/* The first day: what a new library needs, step by step, right after the
   administrator is made. Opens once (Config.first_day_done) and never again.

   It begins by asking how many computers will keep the library. One
   computer has the steps it always had. With two, this one is the Primary
   (it runs the library: the same steps, and pairing with the Secondary
   after the library folder) or the Secondary (its library stays empty: the
   originals folder on its big disk, how the originals first arrive, and
   pairing with the Primary). The Mugil page opens the same two-computer
   steps again later (openTwoComputers).

   Nothing here is new machinery. Each step calls the route the page behind it
   calls — the folder picker, People, the AI switches, the Server page's
   addresses — so finishing the walk-through leaves the console exactly as if
   the person had visited those pages themselves. Every step can be skipped. */

import * as i18n from './i18n.js';

const $ = (sel) => document.querySelector(sel);

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

const STEPS = {
  single: ['computers', 'library', 'people', 'ai', 'backup', 'phones'],
  primary: ['computers', 'library', 'partner', 'people', 'ai', 'backup', 'phones'],
  secondary: ['computers', 'originals', 'copying', 'primary'],
};

/* From the Mugil page, only the steps about the two computers. */
const TWO_COMPUTER_STEPS = {
  single: ['computers'],
  primary: ['computers', 'partner'],
  secondary: ['computers', 'originals', 'copying', 'primary'],
};

const STEP_LABEL = {
  computers: () => i18n.t('Computers'),
  library: () => i18n.t('Library'),
  partner: () => i18n.t('Secondary'),
  people: () => i18n.t('Household'),
  ai: () => 'AI',
  backup: () => i18n.t('Backup'),
  phones: () => i18n.t('Phones'),
  originals: () => i18n.t('Originals'),
  copying: () => i18n.t('First copy'),
  primary: () => i18n.t('Primary'),
};

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

export class FirstDay {
  constructor({ json, toast, pickFolder, openPage, refresh }) {
    this.json = json;
    this.toast = toast;
    this.pickFolder = pickFolder;
    this.openPage = openPage;
    this.refresh = refresh;
    this.state = null;
    this.step = 0;
    this.added = [];                       // people added during this walk-through
    this.wired = false;
    this.mugil = null;                     // /api/admin/mugil: the two computers
    this.onlyComputers = false;            // opened from the Mugil page
    this.poll = null;
    i18n.onChange(() => {
      const box = $('#first-day');
      if (this.state && box && !box.hidden) this.render();
    });
  }

  /* Open if it has not been finished. Called once the console has signed in. */
  async maybeOpen() {
    try {
      this.state = await this.json('/api/admin/first-day');
    } catch { return; }
    if (this.state.done) return;
    // An established library — a folder and a household already — has had
    // its first day. Say so and stay out of the way.
    if (this.state.library.chosen && this.state.people > 0) {
      try { await this.json('/api/admin/first-day', { method: 'POST', body: {} }); } catch { /* next time */ }
      return;
    }
    await this.reloadMugil();
    this.wire();
    this.onlyComputers = false;
    this.step = 0;
    $('#first-day').hidden = false;
    this.render();
  }

  /* From Mugil → Two computers: the same steps, any time. */
  async openTwoComputers() {
    try { this.state = await this.json('/api/admin/first-day'); } catch { return; }
    await this.reloadMugil();
    this.wire();
    this.onlyComputers = true;
    this.step = 0;
    $('#first-day').hidden = false;
    this.render();
  }

  steps() {
    const part = this.mugil?.role || 'single';
    return (this.onlyComputers ? TWO_COMPUTER_STEPS : STEPS)[part] || STEPS.single;
  }

  async reloadMugil() {
    try { this.mugil = await this.json('/api/admin/mugil'); } catch { this.mugil = this.mugil || { role: 'single' }; }
  }

  stopPolling() {
    clearTimeout(this.poll);
    this.poll = null;
  }

  /* Ask again every few seconds while a step waits on the other computer. */
  pollWhile(step, ms = 3000) {
    this.stopPolling();
    this.poll = setTimeout(async () => {
      if ($('#first-day').hidden || this.steps()[this.step] !== step) return;
      const before = JSON.stringify(this.mugil);
      await this.reloadMugil();
      if (JSON.stringify(this.mugil) !== before) this.render();
      else this.pollWhile(step, ms);
    }, ms);
  }

  wire() {
    if (this.wired) return;
    this.wired = true;
    $('#fd-next').onclick = () => this.next();
    $('#fd-back').onclick = () => { if (this.step > 0) { this.step -= 1; this.render(); } };
    $('#fd-skip-all').onclick = () => this.finish(true);
  }

  async reload() {
    try { this.state = await this.json('/api/admin/first-day'); } catch { /* keep what we have */ }
  }

  async next() {
    if (this.step >= this.steps().length - 1) { await this.finish(false); return; }
    this.step += 1;
    await this.reload();
    await this.reloadMugil();
    this.render();
  }

  async finish(skipped) {
    this.stopPolling();
    if (this.onlyComputers) {
      $('#first-day').hidden = true;
      this.refresh?.();
      return;
    }
    try { await this.json('/api/admin/first-day', { method: 'POST', body: {} }); } catch { /* it will ask again */ }
    $('#first-day').hidden = true;
    this.toast(skipped ? i18n.t('You can do all of this from the pages on the left, any time.')
      : i18n.t('That is the first day done. The Overview says what needs you next.'));
    this.refresh?.();
  }

  render() {
    this.stopPolling();
    const steps = this.steps();
    this.step = Math.min(this.step, steps.length - 1);
    const name = steps[this.step];
    const list = $('#fd-steps');
    list.replaceChildren(...steps.map((step, i) => {
      const li = el('li', null, STEP_LABEL[step]());
      li.dataset.step = step;
      li.classList.toggle('current', i === this.step);
      li.classList.toggle('done', i < this.step);
      return li;
    }));
    $('#fd-back').hidden = this.step === 0;
    $('#fd-skip-all').hidden = this.onlyComputers;
    $('#fd-next').textContent = this.step === steps.length - 1
      ? (this.onlyComputers ? i18n.t('Done') : i18n.t('Open the console')) : i18n.t('Next');
    $('#fd-note').textContent = '';
    const body = $('#fd-body');
    body.replaceChildren();
    this[`render_${name}`](body);
  }

  /* -- 1. the library folder -------------------------------------------- */

  render_library(body) {
    $('#fd-title').textContent = i18n.t('Where are the photographs?');
    body.append(el('p', 'lede',
      i18n.t('Choose the folder that holds them. Ninaivu only ever reads it; nothing in it is moved, renamed or changed. You can add more folders later on Library settings.')));
    const chosen = el('div', 'fd-chosen', this.state.library.chosen
      ? this.state.library.root : i18n.t('No folder chosen yet'));
    body.append(chosen);
    const row = el('div', 'row');
    const pick = el('button', 'btn primary', this.state.library.chosen
      ? i18n.t('Choose a different folder') : i18n.t('Choose a folder'));
    pick.type = 'button';
    pick.onclick = () => this.pickFolder({
      title: i18n.t('Choose the folder that holds the photographs'),
      cta: i18n.t('Use this folder'),
      pick: async (path) => {
        try {
          await this.json('/api/library/root', { method: 'POST', body: { path } });
          this.toast(i18n.t('Indexing the library…'));
          await this.reload();
          this.render();
        } catch (exc) {
          this.toast(exc.message, true);
        }
      },
    });
    row.append(pick);
    body.append(row);
    if (!this.state.library.chosen) {
      $('#fd-note').textContent = i18n.t('Skipping leaves the library empty until a folder is chosen.');
    }
  }

  /* -- 2. the household ------------------------------------------------- */

  render_people(body) {
    $('#fd-title').textContent = i18n.t('Who is in the household?');
    body.append(el('p', 'lede',
      i18n.t('A family member taps their name and enters a PIN. A guest signs in with a password and sees only what you make public. Add the people you know of now; the rest can come later on People.')));
    const list = el('ul', 'fd-people');
    for (const person of this.added) {
      const li = el('li');
      li.append(el('strong', null, person.name), el('span', 'hint', person.role === 'guest'
        ? i18n.t('guest · password') : i18n.t('family · PIN {pin}', { pin: person.pin })));
      list.append(li);
    }
    if (!this.added.length && this.state.people) {
      list.append(el('li', 'hint', i18n.t('{count} already added.', { count: this.state.people })));
    }
    body.append(list);
    const form = el('form', 'row');
    const name = el('input', 'input'); name.placeholder = i18n.t('Name'); name.required = true; name.maxLength = 60;
    const role = el('select', 'input');
    for (const [v, t] of [['family', i18n.t('Family member')], ['guest', i18n.t('Guest')]]) {
      const o = el('option', null, t); o.value = v; role.append(o);
    }
    const secret = el('input', 'input'); secret.placeholder = i18n.t('PIN (4–8 digits)'); secret.inputMode = 'numeric';
    role.onchange = () => {
      secret.placeholder = role.value === 'guest' ? i18n.t('Password') : i18n.t('PIN (4–8 digits)');
      secret.type = role.value === 'guest' ? 'password' : 'text';
    };
    const add = el('button', 'btn', i18n.t('Add')); add.type = 'submit';
    form.append(name, role, secret, add);
    form.onsubmit = async (event) => {
      event.preventDefault();
      const username = name.value.trim().toLowerCase().replace(/[^a-z0-9]+/g, '') || `person${Date.now() % 10000}`;
      const body = { name: name.value.trim(), username, role: role.value };
      if (role.value === 'guest') body.password = secret.value; else body.pin = secret.value;
      add.disabled = true;
      try {
        await this.json('/api/people', { method: 'POST', body });
        this.added.push({ name: body.name, role: role.value, pin: body.pin });
        name.value = ''; secret.value = '';
        this.render();
      } catch (exc) {
        this.toast(exc.message, true);
      } finally {
        add.disabled = false;
      }
    };
    body.append(form);
    body.append(el('p', 'hint',
      i18n.t('A family member without a PIN can be opened from any phone or computer on your home network. Give a PIN to anyone whose photographs should stay theirs.')));
  }

  /* -- 3. what the scan does with AI ----------------------------------- */

  render_ai(body) {
    $('#fd-title').textContent = i18n.t('What should Ninaivu work out by itself?');
    body.append(el('p', 'lede',
      i18n.t('Everything here runs on this computer; no photograph leaves it. Each pass takes time on a large library and can be turned off part-way. You can change your mind on AI models.')));
    const switches = [
      ['faces_enabled', i18n.t('Find the people in photographs'), this.state.faces_model
        ? i18n.t('needs a 37 MB model — installed')
        : i18n.t('needs a 37 MB model, downloaded when you turn this on')],
      ['place_names', i18n.t('Name the places photographs were taken'), i18n.t('an 11 MB list of places, downloaded once')],
      ['ocr_enabled', i18n.t('Read the words in photographs'), i18n.t('needs the text reader from Extras; slow on a processor')],
    ];
    for (const [key, label, hint] of switches) {
      const row = el('label', 'toggle');
      const box = el('input'); box.type = 'checkbox'; box.checked = !!this.state.ai[key];
      box.onchange = async () => {
        try {
          await this.json('/api/admin/settings', { method: 'POST', body: { [key]: box.checked } });
          if (key === 'faces_enabled' && box.checked && !this.state.faces_model) {
            await this.json('/api/admin/ai-models/faces/download', { method: 'POST', body: {} });
            this.toast(i18n.t('Downloading the faces model in the background.'));
          }
        } catch (exc) {
          box.checked = !box.checked;
          this.toast(exc.message, true);
        }
      };
      row.append(box, el('span', null, label));
      body.append(row, el('p', 'hint', hint));
    }

    // The image model is not installed at the first start any more: it is
    // about 2 GB, so it is offered here, sized, and fetched only on a press.
    const image = this.state.image_model || {};
    const box = el('div', 'fd-image-model');
    box.append(el('strong', null, i18n.t('Search by description')),
      el('p', 'hint', image.present
        ? i18n.t('Installed. Photographs are tagged and can be found by what is in them.')
        : i18n.t('Find “the beach at sunset” without anybody tagging it, and get tags and descriptions. About 2 GB, then the model itself at the next start. Worth it on a computer with 8 GB of memory or a graphics card; smaller ones keep the light search.')));
    if (!image.present) {
      const add = el('button', 'btn', image.installing ? i18n.t('Installing…') : i18n.t('Add search by description'));
      add.type = 'button';
      add.disabled = !!image.installing;
      add.onclick = async () => {
        add.disabled = true;
        try {
          await this.json('/api/admin/components/image-model/install', { method: 'POST', body: {} });
          add.textContent = i18n.t('Installing…');
          this.toast(i18n.t('Installing in the background. Restart Ninaivu when Home & extensions → Extras says it is done.'));
        } catch (exc) {
          add.disabled = false;
          this.toast(exc.message, true);
        }
      };
      box.append(add);
    }
    body.append(box);
  }

  /* -- 4. backup -------------------------------------------------------- */

  render_backup(body) {
    $('#fd-title').textContent = i18n.t('A copy outside the house');
    body.append(el('p', 'lede',
      i18n.t('A fire, a theft or a failing disk would take every photograph. Mugil keeps an encrypted copy on Google Drive and checks once a week that it restores. It needs a Google account, which takes a few minutes on the Mugil page — the Overview will remind you until it is done.')));
    body.append(el('p', 'hint', this.state.backup.enabled
      ? i18n.t('Mugil is on.') : i18n.t('Nothing is backed up yet.')));
    const row = el('div', 'row');
    const go = el('button', 'btn', i18n.t('Set up Mugil now')); go.type = 'button';
    go.onclick = () => { this.finish(true); this.openPage('cloud'); };
    row.append(go);
    body.append(row);
  }

  /* -- 5. phones -------------------------------------------------------- */

  async render_phones(body) {
    $('#fd-title').textContent = i18n.t('On the household’s phones');
    body.append(el('p', 'lede',
      i18n.t('Open this address on a phone on the home Wi-Fi, then add it to the home screen: it opens like an app, and photographs the phone takes can back themselves up here.')));
    const box = el('div', 'fd-addresses');
    box.append(el('p', 'hint', i18n.t('Reading the addresses…')));
    body.append(box);
    try {
      const state = await this.json('/api/admin/server');
      // A phone cannot use this computer's own "localhost"; only an address
      // on the home network counts here.
      const urls = (state.endpoints?.family_urls || []).filter((u) => !/\/\/(localhost|127\.0\.0\.1|\[::1\])[:/]/.test(u));
      box.replaceChildren();
      if (!urls.length) {
        box.append(el('p', 'hint', i18n.t('Ninaivu is on this computer only. Turn on Network access on the Server page (and restart) to reach it from a phone.')));
      }
      for (const href of urls) {
        const a = el('a', null, href); a.href = href; a.target = '_blank'; a.rel = 'noopener';
        box.append(a);
      }
    } catch {
      box.replaceChildren(el('p', 'hint', i18n.t('The addresses are on the Server page.')));
    }
  }

  /* -- 0. one computer or two ------------------------------------------- */

  render_computers(body) {
    $('#fd-title').textContent = i18n.t('How many computers will keep the library?');
    body.append(el('p', 'lede',
      i18n.t('Most homes need one. With two, the fast one runs the library, and one that is always on with a big disk keeps every original exactly as it was taken.')));
    const part = this.mugil?.role || 'single';
    const choices = [
      ['single', i18n.t('One computer'),
        i18n.t('This computer keeps everything: the library, the albums and the originals.')],
      ['primary', i18n.t('Two computers: this one is the Primary'),
        i18n.t('The fast one, a Mac mini for example. It runs the library the family browses and sends every original to the Secondary.')],
      ['secondary', i18n.t('Two computers: this one is the Secondary'),
        i18n.t('The one that is always on with the big disk, a Linux PC for example. It keeps the originals, never changes them, and has no library of its own.')],
    ];
    const group = el('div', 'fd-choices');
    group.setAttribute('role', 'radiogroup');
    for (const [value, label, hint] of choices) {
      const row = el('label', 'fd-choice');
      const box = el('input'); box.type = 'radio'; box.name = 'fd-part'; box.value = value;
      box.checked = part === value;
      box.onchange = () => this.choosePart(value);
      const words = el('span');
      words.append(el('strong', null, label), el('span', 'hint', hint));
      row.append(box, words);
      group.append(row);
    }
    body.append(group);
    if (this.mugil?.paired) {
      body.append(el('p', 'hint', i18n.t('Paired with {name}. To change this computer’s part, unpair the two on Mugil → Two computers first.', {
        name: this.mugil.pairing?.partner_name || i18n.t('the other computer') })));
    }
  }

  async choosePart(value) {
    try {
      this.mugil = await this.json('/api/admin/mugil/role', { method: 'POST', body: { role: value } });
    } catch (exc) {
      this.toast(exc.message, true);
    }
    this.render();
  }

  /* -- Secondary: the originals folder ----------------------------------- */

  render_originals(body) {
    $('#fd-title').textContent = i18n.t('Where should the originals be kept?');
    body.append(el('p', 'lede',
      i18n.t('Choose a folder on the big disk. This computer has no library of its own: leave Library settings empty here, and never add this disk as a library.')));
    const folder = this.mugil?.originals_dir || '';
    body.append(el('div', 'fd-chosen', folder || i18n.t('No folder chosen yet')));
    const keeping = this.mugil?.keeping || {};
    if (folder && keeping.free_bytes != null) {
      body.append(el('p', 'hint', i18n.t('{free} free on this disk.', { free: size(keeping.free_bytes) })));
    }
    if (folder && keeping.files) {
      // A disk that kept the originals for a computer that was replaced.
      body.append(el('p', 'hint', i18n.t('This disk already keeps {files} originals ({size}). They are used as they are; nothing is sent again that is already here.', {
        files: count(keeping.files), size: size(keeping.bytes) })));
    }
    const row = el('div', 'row');
    const pick = el('button', 'btn primary', folder ? i18n.t('Choose a different folder') : i18n.t('Choose a folder'));
    pick.type = 'button';
    pick.disabled = !!this.mugil?.paired;
    pick.onclick = () => this.pickFolder({
      title: i18n.t('Choose the folder on the big disk for the originals'),
      cta: i18n.t('Use this folder'),
      pick: async (path) => {
        try {
          this.mugil = await this.json('/api/admin/mugil/role', {
            method: 'POST', body: { role: 'secondary', originals_dir: path } });
        } catch (exc) {
          this.toast(exc.message, true);
        }
        this.render();
      },
    });
    row.append(pick);
    body.append(row);
    if (!folder) $('#fd-note').textContent = i18n.t('The Primary cannot pair until a folder is chosen.');
  }

  /* -- Secondary: how the originals first arrive -------------------------- */

  render_copying(body) {
    $('#fd-title').textContent = i18n.t('How should the originals first arrive?');
    const mode = this.mugil?.copy_mode || 'automatic';
    const choices = [
      ['automatic', i18n.t('Automatically'),
        i18n.t('The Primary sends every original over the network as soon as the two are paired, each one checked. It carries on by itself after a restart, a power cut or the network going down. A large library takes days.')],
      ['manual', i18n.t('I will copy them myself'),
        i18n.t('Quicker for a large library: plug the Primary’s library disk into this computer, or bring the files on another disk, and copy them into the originals folder. Ninaivu checks what you copied and the Primary sends only what is missing or different.')],
    ];
    const group = el('div', 'fd-choices');
    group.setAttribute('role', 'radiogroup');
    for (const [value, label, hint] of choices) {
      const row = el('label', 'fd-choice');
      const box = el('input'); box.type = 'radio'; box.name = 'fd-copy'; box.value = value;
      box.checked = mode === value;
      box.onchange = async () => {
        try {
          this.mugil = await this.json('/api/admin/mugil/role', {
            method: 'POST', body: { role: 'secondary', copy_mode: value } });
        } catch (exc) {
          this.toast(exc.message, true);
        }
        this.render();
      };
      const words = el('span');
      words.append(el('strong', null, label), el('span', 'hint', hint));
      row.append(box, words);
      group.append(row);
    }
    body.append(group);
    if (mode === 'manual') this.renderManual(body);
  }

  renderManual(body) {
    const folder = this.mugil?.originals_dir || i18n.t('the originals folder');
    const manual = this.mugil?.manual || {};
    const box = el('div', 'fd-manual');
    box.append(el('p', null, i18n.t('Copy each library folder of the Primary into {folder}, keeping its name and every folder inside it. Copy the files as they are: nothing needs converting or renaming.', { folder })));
    const libraries = this.mugil?.primary?.libraries || [];
    if (libraries.length) {
      const list = el('ul', 'fd-people');
      for (const lib of libraries) {
        const li = el('li');
        li.append(el('code', null, `${lib.root || lib.name} → ${folder}/${lib.name}`),
          el('span', 'hint', i18n.t('{files} files, {size}', { files: count(lib.files), size: size(lib.bytes) })));
        list.append(li);
      }
      box.append(list);
    } else {
      box.append(el('p', 'hint', i18n.t('Once the two are paired, the Primary’s library folders and their sizes are listed here.')));
    }
    box.append(el('p', 'hint', i18n.t('A copy that changed the files’ dates is fine: the Primary’s dates are put back. A file cut short or missing is sent over the network.')));
    const row = el('div', 'row');
    const state = manual.state || 'copying';
    const progress = manual.progress || {};
    if (state === 'copying') {
      box.append(el('p', 'hint', i18n.t('Until you say the copy is finished, the Primary waits and sends nothing.')));
      const done = el('button', 'btn primary', i18n.t('I have finished copying'));
      done.type = 'button';
      done.onclick = () => this.manualDone(true);
      row.append(done);
    } else if (state === 'checking') {
      box.append(el('p', 'hint', progress.files_total
        ? i18n.t('Checking what was copied: {done} of {total} files ({size} of {all}).', {
          done: count(progress.files_done), total: count(progress.files_total),
          size: size(progress.bytes_done), all: size(progress.bytes_total) })
        : i18n.t('Looking at what was copied…')));
      this.pollWhile('copying', 2000);
    } else {
      box.append(el('p', 'hint', i18n.t('Checked: {files} files ({size}). The Primary now sends only what is missing or different.', {
        files: count(manual.files), size: size(manual.bytes) })));
      const more = el('button', 'btn ghost', i18n.t('Copy more first'));
      more.type = 'button';
      more.onclick = () => this.manualDone(false);
      row.append(more);
    }
    if (manual.error) box.append(el('p', 'hint error', manual.error));
    box.append(row);
    body.append(box);
  }

  async manualDone(done) {
    try {
      this.mugil = await this.json('/api/admin/mugil/manual', { method: 'POST', body: { done } });
    } catch (exc) {
      this.toast(exc.message, true);
    }
    this.render();
  }

  /* -- Secondary: pairing with the Primary --------------------------------- */

  render_primary(body) {
    $('#fd-title').textContent = i18n.t('Pair with the Primary');
    const m = this.mugil || {};
    if (m.paired) {
      this.renderPaired(body, m.pairing?.partner_name);
      const libraries = m.primary?.libraries || [];
      const wanted = libraries.reduce((sum, lib) => sum + (Number(lib.bytes) || 0), 0);
      const free = m.keeping?.free_bytes;
      if (wanted && free != null && m.copy_mode !== 'manual' && wanted > free) {
        body.append(el('p', 'hint error', i18n.t('The Primary’s library is {wanted} and this disk has {free} free. What fits is kept; give this computer a bigger disk for the rest.', {
          wanted: size(wanted), free: size(free) })));
      }
      body.append(el('p', 'hint', m.copy_mode === 'manual'
        ? i18n.t('The Primary waits until the copy you make by hand is checked.')
        : i18n.t('The first copy starts about a minute after pairing and carries on by itself. You can close this; the Mugil page shows how far it has got.')));
      return;
    }
    if (!m.originals_dir) {
      body.append(el('p', 'lede', i18n.t('Choose the originals folder first.')));
      return;
    }
    body.append(el('p', 'lede',
      i18n.t('On the Primary, open Mugil → Two computers and press Show a code for the Secondary. Then type its address and the code here.')));
    const form = el('form', 'row');
    const url = el('input', 'input'); url.placeholder = i18n.t('mac-mini or 100.64.1.2'); url.required = true;
    url.autocomplete = 'off'; url.spellcheck = false;
    url.setAttribute('aria-label', i18n.t('The Primary’s address'));
    const code = el('input', 'input'); code.placeholder = 'ABCD-EFGH'; code.required = true;
    code.autocomplete = 'off'; code.spellcheck = false;
    code.setAttribute('aria-label', i18n.t('Pairing code'));
    const go = el('button', 'btn primary', i18n.t('Pair')); go.type = 'submit';
    form.append(url, code, go);
    form.onsubmit = async (event) => {
      event.preventDefault();
      go.disabled = true;
      try {
        this.mugil = await this.json('/api/admin/mugil/join', {
          method: 'POST', body: { url: url.value.trim(), code: code.value.trim() } });
        this.toast(i18n.t('The two computers are paired'));
        this.render();
      } catch (exc) {
        go.disabled = false;
        this.toast(exc.message, true);
        await this.reloadMugil();
      }
    };
    body.append(form);
    // Or the Primary finds this one by itself and is tapped there.
    const welcome = m.welcome || {};
    const waiting = el('div', 'fd-waiting');
    if (welcome.waiting) {
      waiting.append(el('p', 'hint', i18n.t('Or let the Primary find this computer: it is waiting now. On the Primary, open Mugil → Two computers and tap the computer showing the same number.')));
      const found = el('ul', 'tc-found');
      for (const met of welcome.introduced || []) {
        const li = el('li');
        li.append(el('span', 'tc-name', met.name || met.address), el('span', 'tc-number', met.number));
        found.append(li);
      }
      waiting.append(found);
    } else {
      const again = el('button', 'btn ghost', i18n.t('Wait for the Primary again'));
      again.type = 'button';
      again.onclick = async () => {
        try { this.mugil = await this.json('/api/admin/mugil/welcome', { method: 'POST' }); } catch (exc) { this.toast(exc.message, true); }
        this.render();
      };
      waiting.append(el('p', 'hint', i18n.t('The Primary can also find this computer, while it waits.')), again);
    }
    if (m.on_the_network === false) {
      waiting.append(el('p', 'hint error', i18n.t('The family app answers this computer only, so the other computer cannot reach it. Turn on network access on the Server page.')));
    }
    body.append(waiting);
    $('#fd-note').textContent = i18n.t('No Primary yet? Skip: this computer keeps waiting, and the two pair later.');
    this.pollWhile('primary');
  }

  renderPaired(body, name) {
    const m = this.mugil || {};
    body.append(el('p', 'lede', i18n.t('Paired with {name}.', { name: name || i18n.t('the other computer') })));
    if (m.same_version === false) {
      body.append(el('p', 'hint error', i18n.t('This computer runs Ninaivu {mine} and the other runs {theirs}. Bring the older one up to date; a Linux Secondary can be updated from the Primary’s Mugil page.', {
        mine: m.version, theirs: m.partner_version })));
    }
  }

  /* -- Primary: pairing with the Secondary ---------------------------------- */

  async render_partner(body) {
    $('#fd-title').textContent = i18n.t('Pair with the Secondary');
    const m = this.mugil || {};
    if (m.paired) {
      this.renderPaired(body, m.pairing?.partner_name);
      const secondary = m.secondary || {};
      const wanted = (m.libraries || []).reduce((sum, lib) => sum + (Number(lib.bytes) || 0), 0);
      const free = secondary.health?.free_bytes;
      if (wanted && free != null && wanted > free) {
        body.append(el('p', 'hint error', i18n.t('The library is {wanted} and the Secondary has {free} free. What fits is sent; give it a bigger disk for the rest.', {
          wanted: size(wanted), free: size(free) })));
      }
      body.append(el('p', 'hint', secondary.copy_mode === 'manual'
        ? i18n.t('The Secondary is having the library copied to it by hand. Sending waits until that copy is checked, then sends only what is missing.')
        : i18n.t('The first copy starts about a minute after pairing, and carries on by itself after a restart or the Secondary going away.')));
      if (!(m.libraries || []).some((lib) => lib.files)) {
        // A new Mac in place of one that failed: the Secondary has it all.
        body.append(el('p', 'hint', i18n.t('Replacing a Primary that failed? Mugil → Two computers → Rebuild copies the library back from the Secondary.')));
      }
      return;
    }
    body.append(el('p', 'lede',
      i18n.t('Set the Secondary up as the Secondary first. This computer looks for it on the house network and on Tailscale; tap it when both screens show the same number.')));
    const found = el('ul', 'tc-found');
    found.append(el('li', 'hint', i18n.t('Looking for the Secondary on the house network and on Tailscale…')));
    body.append(found);
    const byCode = el('div', 'fd-waiting');
    const show = el('button', 'btn ghost', i18n.t('Show a code for the Secondary'));
    show.type = 'button';
    const out = el('p', 'tc-code');
    out.hidden = true;
    show.onclick = async () => {
      try {
        const answer = await this.json('/api/admin/mugil/code', { method: 'POST' });
        out.textContent = answer.code;
        out.hidden = false;
        let addresses = [];
        try {
          const server = await this.json('/api/admin/server');
          addresses = (server.endpoints?.family_urls || []).filter((u) => !/\/\/(localhost|127\.0\.0\.1|\[::1\])[:/]/.test(u));
        } catch { /* the Server page has them */ }
        if (addresses.length) {
          out.append(el('small', null, i18n.t('This computer’s address: {address}', { address: addresses.join('  ·  ') })));
        }
        out.append(el('small', null, i18n.t('Type both on the Secondary within ten minutes.')));
      } catch (exc) {
        this.toast(exc.message, true);
      }
    };
    byCode.append(el('p', 'hint', i18n.t('Cannot find it, or setting the Secondary up second? Show a code here and type it on the Secondary with this computer’s address.')), show, out);
    body.append(byCode);
    $('#fd-note').textContent = i18n.t('No Secondary yet? Skip: pair later on Mugil → Two computers.');
    const step = this.step;
    try {
      const answer = await this.json('/api/admin/mugil/look', { method: 'POST' });
      if (this.step !== step || $('#first-day').hidden) return;
      found.replaceChildren();
      const waiting = (answer.found || []).filter((f) => f.waiting && f.number);
      if (!waiting.length) {
        found.append(el('li', 'hint', i18n.t('No Secondary is waiting yet. Looking again in a moment.')));
      }
      for (const other of waiting) {
        const li = el('li');
        li.append(el('span', 'tc-name', other.name), el('span', 'tc-number', other.number),
          el('span', 'tc-where', other.address));
        const pair = el('button', 'btn primary small', i18n.t('Pair'));
        pair.type = 'button';
        pair.onclick = async () => {
          pair.disabled = true;
          try {
            this.mugil = await this.json('/api/admin/mugil/pair', { method: 'POST', body: { found: other.id } });
            this.toast(i18n.t('The two computers are paired'));
            this.render();
          } catch (exc) {
            pair.disabled = false;
            this.toast(exc.message, true);
          }
        };
        li.append(pair);
        found.append(li);
      }
    } catch (exc) {
      found.replaceChildren(el('li', 'hint', exc.message));
    }
    // Paired from the Secondary with the code, or nothing found yet: ask again.
    if (out.hidden) this.pollLook(step);
    else this.pollWhile('partner');
  }

  pollLook(step) {
    this.stopPolling();
    this.poll = setTimeout(async () => {
      if ($('#first-day').hidden || this.step !== step) return;
      await this.reloadMugil();
      this.render();
    }, 8000);
  }
}
