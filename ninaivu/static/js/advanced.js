/**
 * The Advanced settings page: every setting in its group, from
 * /api/admin/settings/all, each with what it means and its default.
 *
 * Nothing is saved on keystroke. A field saves when it is left (blur) or
 * Enter is pressed; a switch saves at once. A setting that only takes effect
 * after a restart says so in the toast. Settings that describe how this
 * process was started are shown greyed and cannot be changed here.
 */

import { reportUnauthorized } from './api.js';
import * as i18n from './i18n.js';

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
    throw new Error(data?.error || i18n.t('Request failed ({status})', { status: response.status }));
  }
  return data;
}

const show = (value, kind, lines = false) => {
  if (value == null) return '';
  if (kind === 'list') return Array.isArray(value) ? value.join(lines ? '\n' : ', ') : String(value);
  return String(value);
};

export class AdvancedPanel {
  constructor({ toast } = {}) {
    this.toast = toast || (() => {});
    this.data = null;
    this.filter = '';
    // Redrawn in the new language — but not under somebody's cursor, for the
    // same reason save() holds back.
    i18n.onChange(() => {
      const active = document.activeElement;
      if (!active || !active.closest('#adv-first, #adv-groups') || active === document.body) this.render();
    });
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
      first.append(el('h3', null, i18n.t('The ones a household changes')));
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
      const changed = rows.filter((s) => s.changed).length;
      const counted = [rows.length === 1 ? i18n.t('1 setting') : i18n.t('{count} settings', { count: rows.length })];
      if (changed) counted.push(i18n.t('{count} changed', { count: changed }));
      summary.append(el('h2', null, i18n.t(group.name)), el('span', 'hint', counted.join(' · ')));
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
    if (setting.runtime || setting.managed_by) row.classList.add('runtime');
    const head = el('div', 'adv-head');
    head.append(el('code', null, setting.name));
    if (setting.default != null && setting.default !== '' && !setting.secret) {
      head.append(el('span', 'hint', i18n.t('default {value}', { value: show(setting.default, setting.kind) })));
    }
    if (setting.runtime) head.append(el('span', 'hint', i18n.t('set when Ninaivu starts')));
    if (setting.managed_by) head.append(el('span', 'hint', i18n.t('changed on {page}', { page: i18n.t(setting.managed_by) })));
    row.append(head);
    if (setting.doc) row.append(el('p', 'hint', setting.doc));
    row.append(this.control(setting));
    return row;
  }

  control(setting) {
    const wrap = el('div', 'adv-control');
    const fixed = !!(setting.runtime || setting.managed_by);
    if (setting.choices?.length && !fixed) {
      const pick = el('select', 'input');
      for (const choice of setting.choices) {
        const o = el('option', null, choice); o.value = choice;
        if (String(setting.value) === choice) o.selected = true;
        pick.append(o);
      }
      let last = pick.value;
      pick.onchange = () => this.save(setting, pick.value, () => { pick.value = last; }, () => { last = pick.value; });
      wrap.append(pick);
      return wrap;
    }
    if (setting.kind === 'bool') {
      const label = el('label', 'toggle');
      const box = el('input'); box.type = 'checkbox'; box.checked = !!setting.value;
      box.disabled = fixed;
      box.onchange = () => this.save(setting, box.checked, () => { box.checked = !box.checked; });
      label.append(box, el('span', null, setting.value ? i18n.t('On') : i18n.t('Off')));
      box.addEventListener('change', () => { label.lastChild.textContent = box.checked ? i18n.t('On') : i18n.t('Off'); });
      wrap.append(label);
      return wrap;
    }
    // A list is one entry per line: a comma is a legal character in a folder name.
    const input = setting.kind === 'list' ? el('textarea', 'input') : el('input', 'input');
    if (setting.kind === 'list') input.rows = Math.min(6, Math.max(2, (setting.value || []).length + 1));
    input.disabled = fixed;
    if (setting.secret) {
      input.type = 'password';
      input.placeholder = setting.value ? i18n.t('Set — type to replace') : i18n.t('Not set');
    } else {
      if (setting.kind !== 'list') {
        input.type = (setting.kind === 'int' || setting.kind === 'float') ? 'number' : 'text';
        if (setting.kind === 'float') input.step = 'any';
      }
      input.value = show(setting.value, setting.kind, true);
    }
    let last = input.value;
    const commit = () => {
      if (input.value === last) return;
      const raw = setting.kind === 'list'
        ? input.value.split('\n').map((p) => p.trim()).filter(Boolean)
        : input.value;
      this.save(setting, raw, () => { input.value = last; }, () => { last = input.value; });
    };
    input.addEventListener('blur', commit);
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && setting.kind !== 'list') { event.preventDefault(); input.blur(); }
    });
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
        this.toast(i18n.t('{name} saved — it takes effect when Ninaivu next starts.', { name: setting.name }));
      } else {
        this.toast(i18n.t('{name} saved.', { name: setting.name }));
      }
      // Re-read so "changed from the default" and the other pages agree —
      // but not under somebody's cursor: tabbing on to the next field and
      // having it redrawn away was the same as losing what they typed.
      this.data = await json('/api/admin/settings/all');
      const active = document.activeElement;
      if (!active || !active.closest('#adv-first, #adv-groups') || active === document.body) this.render();
    } catch (exc) {
      revert?.();
      this.toast(exc.message, true);
    }
  }
}
