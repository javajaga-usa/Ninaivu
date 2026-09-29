/**
 * The All settings page: every setting in its group, from
 * /api/admin/settings/all, each with what it means and its default.
 *
 * Nothing is saved on keystroke. A field saves when it is left (blur) or
 * Enter is pressed; a switch saves at once. A setting that only takes effect
 * after a restart says so in the toast. Settings that describe how this
 * process was started are shown greyed and cannot be changed here.
 */

import { reportUnauthorized } from './api.js';

const $ = (sel) => document.querySelector(sel);

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
    throw new Error(data?.error || `Request failed (${response.status})`);
  }
  return data;
}

const show = (value, kind) => {
  if (value == null) return '';
  if (kind === 'list') return Array.isArray(value) ? value.join(', ') : String(value);
  return String(value);
};

export class AdvancedPanel {
  constructor({ toast } = {}) {
    this.toast = toast || (() => {});
    this.data = null;
    this.filter = '';
  }

  wire() {
    $('#adv-filter')?.addEventListener('input', (event) => {
      this.filter = event.target.value.trim().toLowerCase();
      this.render();
    });
  }

  async show() {
    try {
      this.data = await json('/api/admin/settings/all');
      this.render();
    } catch (exc) {
      this.toast(exc.message, true);
    }
  }

  hide() {}

  matches(setting) {
    if (!this.filter) return true;
    return setting.name.includes(this.filter) || setting.doc.toLowerCase().includes(this.filter);
  }

  render() {
    if (!this.data) return;
    const first = $('#adv-first');
    const groups = $('#adv-groups');
    if (!first || !groups) return;
    first.replaceChildren();
    groups.replaceChildren();

    const all = this.data.groups.flatMap((g) => g.settings);
    const firstScreen = this.data.first_screen
      .map((name) => all.find((s) => s.name === name))
      .filter((s) => s && this.matches(s));
    if (firstScreen.length) {
      first.append(el('h3', null, 'The ones a household changes'));
      const list = el('div', 'adv-list');
      for (const setting of firstScreen) list.append(this.row(setting));
      first.append(list);
    }

    for (const group of this.data.groups) {
      const rows = group.settings.filter((s) => this.matches(s));
      if (!rows.length) continue;
      const block = el('details', 'block adv-group');
      block.open = !!this.filter;
      const summary = el('summary');
      summary.append(el('h2', null, group.name),
        el('span', 'hint', `${rows.length} settings` + (rows.some((s) => s.changed) ? ` · ${rows.filter((s) => s.changed).length} changed` : '')));
      block.append(summary);
      const list = el('div', 'adv-list');
      for (const setting of rows) list.append(this.row(setting));
      block.append(list);
      groups.append(block);
    }
  }

  row(setting) {
    const row = el('div', 'adv-row');
    if (setting.changed) row.classList.add('changed');
    if (setting.runtime) row.classList.add('runtime');
    const head = el('div', 'adv-head');
    head.append(el('code', null, setting.name));
    if (setting.default != null && setting.default !== '' && !setting.secret) {
      head.append(el('span', 'hint', `default ${show(setting.default, setting.kind)}`));
    }
    if (setting.runtime) head.append(el('span', 'hint', 'set when Ninaivu starts'));
    row.append(head);
    if (setting.doc) row.append(el('p', 'hint', setting.doc));
    row.append(this.control(setting));
    return row;
  }

  control(setting) {
    const wrap = el('div', 'adv-control');
    if (setting.kind === 'bool') {
      const label = el('label', 'toggle');
      const box = el('input'); box.type = 'checkbox'; box.checked = !!setting.value;
      box.disabled = !!setting.runtime;
      box.onchange = () => this.save(setting, box.checked, () => { box.checked = !box.checked; });
      label.append(box, el('span', null, setting.value ? 'On' : 'Off'));
      box.addEventListener('change', () => { label.lastChild.textContent = box.checked ? 'On' : 'Off'; });
      wrap.append(label);
      return wrap;
    }
    const input = el('input', 'input');
    input.disabled = !!setting.runtime;
    if (setting.secret) {
      input.type = 'password';
      input.placeholder = setting.value ? 'Set — type to replace' : 'Not set';
    } else {
      input.type = (setting.kind === 'int' || setting.kind === 'float') ? 'number' : 'text';
      if (setting.kind === 'float') input.step = 'any';
      input.value = show(setting.value, setting.kind);
    }
    let last = input.value;
    const commit = () => {
      if (input.value === last) return;
      const raw = setting.kind === 'list'
        ? input.value.split(',').map((p) => p.trim()).filter(Boolean)
        : input.value;
      this.save(setting, raw, () => { input.value = last; }, () => { last = input.value; });
    };
    input.addEventListener('blur', commit);
    input.addEventListener('keydown', (event) => { if (event.key === 'Enter') { event.preventDefault(); input.blur(); } });
    wrap.append(input);
    return wrap;
  }

  async save(setting, value, revert, done) {
    try {
      const result = await json('/api/admin/settings/all', {
        method: 'POST', body: { settings: { [setting.name]: value } },
      });
      done?.();
      if (result.restart?.length) {
        this.toast(`${setting.name} saved — it takes effect when Ninaivu next starts.`);
      } else {
        this.toast(`${setting.name} saved.`);
      }
      // Re-read so "changed from the default" and the other pages agree.
      this.data = await json('/api/admin/settings/all');
      this.render();
    } catch (exc) {
      revert?.();
      this.toast(exc.message, true);
    }
  }
}
