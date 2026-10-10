/**
 * The handover plan: who looks after the library when its administrator
 * cannot, written down, printed, and a safe way for that person to take over.
 *
 * Three pieces, used in three places:
 *
 *   HandoverPanel     the console page (Backup & health → Handover)
 *   claimBanner       a line at the top of the console and the family app,
 *                     for every administrator, while somebody's claim waits
 *   takeoverSection   the entry on a successor's own profile sheet
 *
 * The server decides everything (api/api_handover.py); this only shows it.
 * Nothing on this page ever asks for a password or a key to be typed in —
 * it asks where they are kept, and says so.
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
  const response = await fetch(url, {
    headers: {
      Accept: 'application/json',
      ...(options.body ? { 'Content-Type': 'application/json' } : {}),
    },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const text = await response.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = null; }
  if (!response.ok) {
    if (response.status === 401) reportUnauthorized(url);
    throw Object.assign(new Error(data?.error || response.statusText), { status: response.status });
  }
  return data;
}

export const handoverApi = {
  page: () => json('/api/admin/handover'),
  save: (body) => json('/api/admin/handover', { method: 'POST', body }),
  makeCode: () => json('/api/admin/handover/code', { method: 'POST' }),
  waiting: () => json('/api/handover/claims'),
  cancel: (id) => json(`/api/handover/claims/${encodeURIComponent(id)}/cancel`, { method: 'POST' }),
  me: () => json('/api/handover/me'),
  claim: (body) => json('/api/handover/claim', { method: 'POST', body }),
};

const day = (seconds) => (seconds
  ? new Date(seconds * 1000).toLocaleDateString(i18n.locale(), { dateStyle: 'long' }) : '');

/** Open the printable sheet. With a code, it is sent as a form (never in an
 *  address) and printed only if it is the current one. */
function openSheet(lang, code = '') {
  if (!code) {
    window.open(`/api/admin/handover/sheet?lang=${encodeURIComponent(lang)}`, '_blank', 'noopener');
    return;
  }
  const form = el('form');
  form.method = 'post';
  form.action = '/api/admin/handover/sheet';
  form.target = '_blank';
  form.hidden = true;
  for (const [name, value] of Object.entries({ code, lang })) {
    const input = el('input');
    input.type = 'hidden';
    input.name = name;
    input.value = value;
    form.appendChild(input);
  }
  document.body.appendChild(form);
  form.submit();
  form.remove();
}

/* What each nudge from the server means, in words. */
const NUDGES = {
  none: i18n.key('Nobody has written down yet who looks after the library if you cannot.'),
  old: i18n.key('It has not been reviewed for six months.'),
  backups: i18n.key('The backups have changed since the plan was last reviewed.'),
};

const CLAIM_STATES = {
  waiting: i18n.key('waiting'),
  cancelled: i18n.key('stopped'),
  done: i18n.key('now an administrator'),
  void: i18n.key('ended without a change'),
};

/* The fields the administrator writes, in the order they are asked. */
const FIELDS = [
  ['message', i18n.key('A message to them'),
    i18n.key('In your own words: what the photographs mean, what to look after first.'), 6],
  ['backups_where', i18n.key('Where the backups are'),
    i18n.key('The disks, the other house, the accounts — where each one is.'), 3],
  ['secrets_where', i18n.key('Where the backup key’s recovery file and passphrase are kept'),
    i18n.key('For example: the blue folder in the steel almirah.'), 2],
  ['password_where', i18n.key('Where the computer’s password is kept'),
    i18n.key('Where it is written down, or who knows it — not the password.'), 2],
  ['machine_access', i18n.key('How to reach the computer'),
    i18n.key('Where it is, how to switch it on, which screen or address to use.'), 3],
  ['hardware', i18n.key('The machine and its disks'),
    i18n.key('What it is, and what each disk is for.'), 3],
];

export class HandoverPanel {
  constructor({ toast, root } = {}) {
    this.toast = toast || (() => {});
    this.root = root || document.querySelector('#handover-root');
    this.visible = false;
    this.data = null;
    this.draft = null;
    this.code = '';
    i18n.onChange(() => { if (this.visible && this.data) this.render(); });
  }

  async show() {
    this.visible = true;
    if (!this.root) return;
    if (!this.data) this.root.replaceChildren(el('p', 'hint', i18n.t('Loading…')));
    try {
      this.data = await handoverApi.page();
    } catch (exc) {
      this.root.replaceChildren(el('p', 'hint', exc.message));
      return;
    }
    this.draft = structuredClone(this.data.plan);
    this.render();
  }

  hide() { this.visible = false; this.code = ''; }

  render() {
    const data = this.data;
    const root = this.root;
    root.replaceChildren();

    for (const reason of data.nudges || []) {
      const note = el('div', 'ar-note ar-note-fix');
      note.appendChild(el('strong', null, i18n.t('The handover plan wants a look')));
      note.appendChild(el('p', null, NUDGES[reason] ? i18n.t(NUDGES[reason]) : reason));
      root.appendChild(note);
    }
    root.appendChild(this.renderClaims());
    root.appendChild(this.renderForm());
    root.appendChild(this.renderCode());
    root.appendChild(this.renderFacts());
    root.appendChild(this.renderPrint());
  }

  /* -- what the administrator writes ------------------------------------ */

  renderForm() {
    const data = this.data;
    const draft = this.draft;
    const block = el('div', 'block');
    const head = el('div', 'block-head');
    head.appendChild(el('h2', null, i18n.t('Who takes over, and how')));
    block.appendChild(head);
    block.appendChild(el('p', 'hint', data.reviewed_at
      ? i18n.t('Last reviewed {date} by {name}.', { date: day(data.reviewed_at), name: data.reviewed_by || '' })
      : i18n.t('Not written yet.')));

    const warn = el('div', 'ar-note ar-note-bad');
    warn.appendChild(el('strong', null, i18n.t('Never type a password or the backup key here.')));
    warn.appendChild(el('p', null, i18n.t(
      'Write where each one is kept — “the blue folder in the steel almirah” — so the sheet is safe to keep with the family’s papers. Text that looks like the key itself is refused.')));
    block.appendChild(warn);

    // Successors, in order.
    const who = el('div', 'setting-field');
    who.appendChild(el('h3', null, i18n.t('Successors, in order')));
    who.appendChild(el('p', 'hint', i18n.t(
      'Family members with their own profile. The first one who can, takes over.')));
    const people = new Map(data.people.map((p) => [p.id, p]));
    const list = el('ol', 'handover-successors');
    draft.successors = draft.successors.filter((id) => people.has(id));
    draft.successors.forEach((id, index) => {
      const person = people.get(id);
      const item = el('li', 'row');
      const label = el('span', 'handover-name', person.name);
      item.appendChild(label);
      // The claim needs something of the successor's own to check: their
      // password, or failing that the PIN their profile opens with. A
      // tap-to-enter profile has neither, and the server refuses its claim.
      if (!person.has_password && !person.has_pin) {
        item.appendChild(el('span', 'hint subtle',
          i18n.t('no password or PIN yet: give them one before they can take over')));
      } else if (!person.has_password) {
        item.appendChild(el('span', 'hint subtle',
          i18n.t('opens with a PIN: they give it and choose a password when they take over')));
      }
      const move = (by) => {
        const next = [...draft.successors];
        [next[index], next[index + by]] = [next[index + by], next[index]];
        draft.successors = next;
        this.render();
      };
      const up = el('button', 'btn ghost small', '↑');
      up.type = 'button';
      up.disabled = index === 0;
      up.setAttribute('aria-label', i18n.t('Move up'));
      up.onclick = () => move(-1);
      const down = el('button', 'btn ghost small', '↓');
      down.type = 'button';
      down.disabled = index === draft.successors.length - 1;
      down.setAttribute('aria-label', i18n.t('Move down'));
      down.onclick = () => move(1);
      const remove = el('button', 'btn ghost small', i18n.t('Remove'));
      remove.type = 'button';
      remove.onclick = () => { draft.successors.splice(index, 1); this.render(); };
      item.append(up, down, remove);
      list.appendChild(item);
    });
    if (!draft.successors.length) list.appendChild(el('li', 'hint', i18n.t('Nobody is named yet.')));
    who.appendChild(list);
    const others = data.people.filter((p) => !draft.successors.includes(p.id));
    if (others.length) {
      const row = el('div', 'row');
      const pick = el('select', 'select');
      pick.setAttribute('aria-label', i18n.t('Add a successor'));
      pick.appendChild(new Option(i18n.t('Add a successor…'), ''));
      for (const person of others) pick.appendChild(new Option(person.name, String(person.id)));
      pick.onchange = () => {
        if (!pick.value) return;
        draft.successors.push(Number(pick.value));
        this.render();
      };
      row.appendChild(pick);
      who.appendChild(row);
    } else if (!data.people.length) {
      who.appendChild(el('p', 'hint', i18n.t('Add a family member on the People page first.')));
    }
    block.appendChild(who);

    // The waiting period.
    const wait = el('div', 'setting-field');
    const waitLabel = el('label', null, i18n.t('Days to wait before they become an administrator'));
    waitLabel.htmlFor = 'handover-wait';
    const waitInput = el('input', 'input');
    waitInput.id = 'handover-wait';
    waitInput.type = 'number';
    waitInput.min = '0';
    waitInput.max = String(data.max_wait_days);
    waitInput.value = String(this.waitDays ?? data.wait_days);
    waitInput.style.maxWidth = '7rem';
    waitInput.oninput = () => { this.waitDays = waitInput.value; };
    wait.append(waitLabel, waitInput, el('p', 'hint', i18n.t(
      'While they wait, every administrator sees it at the top of every page and can stop it. 0 makes them an administrator at once.')));
    block.appendChild(wait);

    // What only the administrator can say.
    for (const [name, label, hint, rows] of FIELDS) {
      const field = el('div', 'setting-field');
      const title = el('label', null, i18n.t(label));
      title.htmlFor = `handover-${name}`;
      const area = el('textarea', 'input handover-text');
      area.id = `handover-${name}`;
      area.rows = rows;
      area.value = draft[name] || '';
      area.spellcheck = true;
      area.oninput = () => { draft[name] = area.value; };
      field.append(title, area, el('p', 'hint', i18n.t(hint)));
      block.appendChild(field);
    }

    // Who can help.
    const help = el('div', 'setting-field');
    help.appendChild(el('h3', null, i18n.t('Who can help')));
    help.appendChild(el('p', 'hint', i18n.t('Somebody who knows computers, or knows this one.')));
    draft.helpers.forEach((helper, index) => {
      const row = el('div', 'row');
      const name = el('input', 'input');
      name.value = helper.name || '';
      name.maxLength = 80;
      name.placeholder = i18n.t('Name');
      name.setAttribute('aria-label', i18n.t('Name'));
      name.oninput = () => { helper.name = name.value; };
      const phone = el('input', 'input');
      phone.type = 'tel';
      phone.value = helper.phone || '';
      phone.maxLength = 40;
      phone.placeholder = i18n.t('Phone');
      phone.setAttribute('aria-label', i18n.t('Phone'));
      phone.oninput = () => { helper.phone = phone.value; };
      const remove = el('button', 'btn ghost small', i18n.t('Remove'));
      remove.type = 'button';
      remove.onclick = () => { draft.helpers.splice(index, 1); this.render(); };
      row.append(name, phone, remove);
      help.appendChild(row);
    });
    if (draft.helpers.length < 6) {
      const add = el('button', 'btn ghost small', i18n.t('Add someone'));
      add.type = 'button';
      add.onclick = () => { draft.helpers.push({ name: '', phone: '' }); this.render(); };
      help.appendChild(add);
    }
    block.appendChild(help);

    const error = el('p', 'gate-error');
    error.hidden = true;
    const save = el('button', 'btn primary', i18n.t('Save, and mark it reviewed today'));
    save.type = 'button';
    save.onclick = async () => {
      error.hidden = true;
      save.disabled = true;
      try {
        const waitDays = Number(this.waitDays ?? data.wait_days);
        this.data = await handoverApi.save({ plan: this.draft, wait_days: waitDays });
        this.draft = structuredClone(this.data.plan);
        this.waitDays = undefined;
        this.toast(i18n.t('The handover plan is saved.'));
        this.render();
      } catch (exc) {
        error.textContent = exc.message;
        error.hidden = false;
        save.disabled = false;
      }
    };
    block.append(error, save);
    return block;
  }

  /* -- the code ---------------------------------------------------------- */

  renderCode() {
    const data = this.data;
    const block = el('div', 'block');
    block.appendChild(el('h2', null, i18n.t('Handover code')));
    block.appendChild(el('p', 'hint', i18n.t(
      'A successor types this into their own profile, with their own password, to take over. It works once, and only for somebody named above.')));
    if (this.code) {
      const shown = el('p', 'handover-code', this.code);
      shown.setAttribute('aria-live', 'polite');
      block.appendChild(shown);
      block.appendChild(el('p', 'hint warn', i18n.t(
        'Shown only this once. Print it on the sheet now, or write it on the sheet by hand.')));
      const print = el('button', 'btn primary', i18n.t('Print the sheet with this code'));
      print.type = 'button';
      print.onclick = () => openSheet(this.language(), this.code);
      block.appendChild(print);
    } else {
      block.appendChild(el('p', null, data.has_code
        ? i18n.t('A code was made on {date}.', { date: day(data.code_made_at) })
        : i18n.t('No code has been made.')));
    }
    const make = el('button', 'btn ghost', data.has_code || this.code
      ? i18n.t('Make a new code') : i18n.t('Make a code'));
    make.type = 'button';
    make.onclick = async () => {
      if ((data.has_code || this.code)
          && !window.confirm(i18n.t('The code made before stops working. Make a new one?'))) return;
      make.disabled = true;
      try {
        const made = await handoverApi.makeCode();
        this.code = made.code;
        this.data = { ...this.data, has_code: true, code_made_at: made.made_at };
        this.render();
      } catch (exc) {
        this.toast(exc.message, true);
        make.disabled = false;
      }
    };
    block.appendChild(make);
    return block;
  }

  /* -- what Ninaivu fills in ---------------------------------------------- */

  renderFacts() {
    const facts = this.data.facts;
    const block = el('div', 'block');
    block.appendChild(el('h2', null, i18n.t('What Ninaivu fills in')));
    block.appendChild(el('p', 'hint', i18n.t(
      'Read from this computer every time the page or the sheet is opened, so it is never out of date. The backup key is named by its fingerprint, never the key.')));
    const table = el('table', 'ar-table');
    const add = (label, value) => {
      const row = el('tr');
      row.append(el('th', null, label), el('td', null, value || '—'));
      table.appendChild(row);
    };
    add(i18n.t('Computer name'), facts.machine);
    add(i18n.t('Ninaivu version'), facts.version);
    add(i18n.t('Ninaivu’s own folder'), facts.state_dir);
    add(i18n.t('Library folders'), facts.libraries.join('\n'));
    const remote = facts.remote || {};
    add(i18n.t('Reached from outside the house with'),
      [remote.title || i18n.t('Nothing set up'), ...(remote.hostnames || [])].join(' · '));
    add(i18n.t('Backup encryption key fingerprint'),
      facts.key_fingerprint || i18n.t('No backup encryption key has been made.'));
    for (const place of facts.destinations) {
      add(i18n.t(place.title), `${place.where || '—'}\n${i18n.t('Last good copy')}: ${
        place.last_ok ? day(place.last_ok) : i18n.t('never')}`);
    }
    table.querySelectorAll('td').forEach((cell) => { cell.style.whiteSpace = 'pre-wrap'; });
    const wrap = el('div', 'ar-table-wrap');
    wrap.appendChild(table);
    block.appendChild(wrap);
    return block;
  }

  /* -- printing ------------------------------------------------------------ */

  language() {
    return this.printLanguage || i18n.language();
  }

  renderPrint() {
    const block = el('div', 'block');
    block.appendChild(el('h2', null, i18n.t('Print the handover sheet')));
    block.appendChild(el('p', 'hint', i18n.t(
      'One or two pages of A4: everything above, and how to bring the photographs back. Keep it with the family’s important papers, and print it again whenever you change something.')));
    const row = el('div', 'row');
    const pick = el('select', 'select');
    pick.setAttribute('aria-label', i18n.t('Language'));
    for (const lang of i18n.LANGUAGES) pick.appendChild(new Option(lang.name, lang.code));
    pick.value = this.language();
    pick.onchange = () => { this.printLanguage = pick.value; };
    const print = el('button', 'btn', i18n.t('Print the handover sheet'));
    print.type = 'button';
    print.onclick = () => openSheet(this.language());
    row.append(pick, print);
    block.appendChild(row);
    return block;
  }

  /* -- claims ------------------------------------------------------------- */

  renderClaims() {
    const block = el('div');
    const claims = this.data.claims || [];
    if (!claims.length) return block;
    block.className = 'block';
    block.appendChild(el('h2', null, i18n.t('Takeovers')));
    const list = el('ul', 'handover-claims');
    for (const claim of claims) {
      list.appendChild(el('li', null, i18n.t('{name} asked on {date}: {state}', {
        name: claim.name, date: day(claim.requested_at), state: i18n.t(CLAIM_STATES[claim.state] || claim.state),
      })));
    }
    block.appendChild(list);
    return block;
  }
}

/* ========================================================================
   The banner every administrator sees while a claim waits
   ======================================================================== */

/**
 * Put the banner at the top of *mount* while somebody's takeover waits, and
 * look again every few minutes. Quiet for anybody who is not an administrator
 * (the server answers 403, and nothing is shown).
 */
export function claimBanner(mount, { toast } = {}) {
  if (!mount) return;
  const banner = el('div', 'offline-banner handover-banner');
  banner.setAttribute('role', 'status');
  banner.hidden = true;
  mount.prepend(banner);

  const check = async () => {
    let waiting = [];
    try {
      waiting = (await handoverApi.waiting()).waiting || [];
    } catch { waiting = []; }
    banner.replaceChildren();
    banner.hidden = !waiting.length;
    for (const claim of waiting.slice(0, 1)) {
      banner.appendChild(el('span', 'offline-banner-text', i18n.t(
        '{name} has asked to take over as administrator. Unless it is stopped, they become one on {date}.',
        { name: claim.name, date: day(claim.due_at) })));
      const stop = el('button', 'btn ghost small', i18n.t('Stop this handover'));
      stop.type = 'button';
      stop.onclick = async () => {
        if (!window.confirm(i18n.t('Stop {name} becoming an administrator? The handover code is used up; make a new one if the plan still stands.', { name: claim.name }))) return;
        stop.disabled = true;
        try {
          await handoverApi.cancel(claim.id);
          toast?.(i18n.t('The handover is stopped.'));
        } catch (exc) {
          toast?.(exc.message, true);
        }
        check();
      };
      banner.appendChild(stop);
    }
  };
  check();
  i18n.onChange(check);
  setInterval(check, 5 * 60 * 1000);
}

/* ========================================================================
   A successor's entry, on their own profile sheet
   ======================================================================== */

/** A section for the profile sheet: empty and hidden unless this person is
 *  named in the plan. */
export function takeoverSection(toast) {
  const section = el('div', 'sheet-section');
  section.hidden = true;
  handoverApi.me().then((me) => {
    if (!me.successor) return;
    section.hidden = false;
    section.appendChild(el('h3', null, i18n.t('Take over as administrator')));
    const claim = me.claim;
    if (claim?.state === 'waiting') {
      section.appendChild(el('p', 'hint', i18n.t(
        'Your handover is waiting. You become an administrator on {date}, unless an administrator stops it.',
        { date: day(claim.due_at) })));
      return;
    }
    if (claim?.state === 'done') {
      section.appendChild(el('p', 'hint', i18n.t(
        'You are an administrator now. Sign in to the console with your password.')));
      return;
    }
    if (claim?.state === 'cancelled') {
      section.appendChild(el('p', 'hint warn', i18n.t('An administrator stopped the last handover.')));
    }
    section.appendChild(el('p', 'hint', i18n.t(
      'You are named in the family’s handover plan. If the administrator cannot look after the library any more, type the handover code from the printed sheet.')));
    if (!me.has_password && !me.has_pin) {
      // Nothing of their own to check against: the server refuses the claim,
      // so say why here rather than after they have typed the code.
      section.appendChild(el('p', 'hint warn', i18n.t(
        'Your profile opens with a tap, so there is nothing of your own for Ninaivu to check. Ask an administrator to give it a PIN or a password, then type the code.')));
      return;
    }
    const form = el('form', 'stack');
    const code = el('input', 'input');
    code.name = 'code';
    code.autocomplete = 'off';
    code.setAttribute('autocapitalize', 'characters');
    code.spellcheck = false;
    code.placeholder = i18n.t('Handover code');
    code.setAttribute('aria-label', i18n.t('Handover code'));
    // A profile with a PIN but no password proves itself with the PIN and
    // chooses the password it will use as administrator in the same step.
    const pin = me.has_password ? null : el('input', 'input');
    if (pin) {
      pin.type = 'password';
      pin.name = 'pin';
      pin.inputMode = 'numeric';
      pin.autocomplete = 'off';
      pin.placeholder = i18n.t('Your PIN');
      pin.setAttribute('aria-label', pin.placeholder);
    }
    const password = el('input', 'input');
    password.type = 'password';
    password.name = 'password';
    password.autocomplete = me.has_password ? 'current-password' : 'new-password';
    password.placeholder = me.has_password
      ? i18n.t('Your password') : i18n.t('Choose a password (8+ characters)');
    password.setAttribute('aria-label', password.placeholder);
    const error = el('p', 'gate-error');
    error.hidden = true;
    const go = el('button', 'btn', i18n.t('Take over as administrator'));
    go.type = 'submit';
    form.append(code, ...(pin ? [pin] : []), password, error, go);
    form.onsubmit = async (event) => {
      event.preventDefault();
      error.hidden = true;
      go.disabled = true;
      try {
        const body = { code: code.value, password: password.value };
        if (pin) body.pin = pin.value;
        const answer = await handoverApi.claim(body);
        section.replaceChildren(el('h3', null, i18n.t('Take over as administrator')),
          el('p', 'hint', answer.done
            ? i18n.t('You are an administrator now. Sign in to the console with your password.')
            : i18n.t('Your handover is waiting. You become an administrator on {date}, unless an administrator stops it.',
              { date: day(answer.claim.due_at) })));
        toast?.(i18n.t('Your request is recorded.'));
      } catch (exc) {
        error.textContent = exc.message;
        error.hidden = false;
        go.disabled = false;
      }
    };
    section.appendChild(form);
  }).catch(() => {});
  return section;
}
