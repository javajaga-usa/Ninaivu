/**
 * The Tuning page: how hard Ninaivu works the computer it runs on.
 *
 * The server measures the machine, picks a profile for it and sizes every
 * knob from that (ninaivu/server/tuning.py). Here the administrator sees the
 * measurements, what the numbers are expected to use, and can choose another
 * profile, set any knob outright, or put everything back to automatic.
 *
 * Self-contained, like the Performance page: the console hands it a toast and
 * the Server page's restart, for the few knobs that wait for one.
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
    throw Object.assign(new Error(data?.error || response.statusText), { status: response.status, data });
  }
  return data;
}

const api = {
  report: () => json('/api/admin/tuning'),
  save: (body) => json('/api/admin/tuning', { method: 'POST', body }),
};

const GB = 1024 ** 3;
const size = (bytes) => {
  if (bytes == null) return '—';
  return bytes >= 10 * GB ? `${Math.round(bytes / GB)} GB` : `${(bytes / GB).toFixed(1)} GB`;
};

const SOURCE_WORDS = {
  auto: i18n.key('Automatic'),
  set: i18n.key('Set by you'),
  startup: i18n.key('From the start command'),
};
const APPLIES_WORDS = {
  now: i18n.key('Applies now'),
  'next-scan': i18n.key('Applies at the next scan'),
  restart: i18n.key('Applies after a restart'),
};
const HOW_WORDS = {
  measured: i18n.key('measured on this computer'),
  set: i18n.key('chosen by you'),
  mode: i18n.key('from the resource mode Ninaivu was started in'),
};
const word = (table, value) => (table[value] ? i18n.t(table[value]) : value);

export class TuningPanel {
  constructor({ toast, restart }) {
    this.toast = toast || (() => {});
    this.restart = restart || (() => {});
    this.state = null;
    this.busy = false;
    i18n.onChange(() => { if (this.state) this.render(this.state); });
  }

  wire() {
    $('#tn-use-profile')?.addEventListener('click', () => this.useProfile());
    $('#tn-save')?.addEventListener('click', () => this.saveValues());
    $('#tn-reset')?.addEventListener('click', () => this.resetAll());
    $('#tn-restart')?.addEventListener('click', () => this.restart());
  }

  show() { this.refresh(); }

  hide() {}

  async refresh() {
    try {
      this.render(await api.report());
    } catch (error) {
      $('#tn-summary').textContent = i18n.t('Could not measure this computer: {reason}', { reason: error.message });
    }
  }

  async send(body, done) {
    if (this.busy) return;
    this.busy = true;
    for (const id of ['#tn-use-profile', '#tn-save', '#tn-reset']) { const b = $(id); if (b) b.disabled = true; }
    try {
      this.render(await api.save(body));
      this.toast(done);
    } catch (error) {
      this.toast(i18n.t('Could not save that: {reason}', { reason: error.message }), true);
    } finally {
      this.busy = false;
      for (const id of ['#tn-use-profile', '#tn-save', '#tn-reset']) { const b = $(id); if (b) b.disabled = false; }
    }
  }

  useProfile() {
    const picked = document.querySelector('input[name="tn-profile"]:checked');
    if (!picked) return;
    this.send({ profile: picked.value }, i18n.t('Saved.'));
  }

  saveValues() {
    const values = {};
    for (const input of document.querySelectorAll('#tn-knobs input[data-knob]')) {
      const knob = this.state.knobs.find((k) => k.name === input.dataset.knob);
      if (!knob || input.value === '') continue;
      const value = Number(input.value);
      if (value !== knob.value) values[knob.name] = value;
    }
    if (!Object.keys(values).length) { this.toast(i18n.t('Nothing has changed.')); return; }
    this.send({ values }, i18n.t('Saved.'));
  }

  resetAll() {
    this.send({ reset: true }, i18n.t('Everything is automatic again.'));
  }

  render(state) {
    this.state = state;
    this.renderSummary(state);
    this.renderProfiles(state);
    this.renderUsage(state);
    this.renderKnobs(state);
    const restart = $('#tn-restart-row');
    if (restart) {
      const waiting = state.restart_needed || [];
      restart.hidden = !waiting.length;
      $('#tn-restart').hidden = !state.can_restart;
      $('#tn-restart-text').textContent = i18n.t('{count} changes wait for a restart: {names}.', {
        count: waiting.length,
        names: waiting.map((name) => i18n.t(state.knobs.find((k) => k.name === name)?.label || name)).join(', '),
      });
    }
  }

  profileLabel(id) {
    const profile = (this.state?.profiles || []).find((p) => p.id === id);
    return profile ? i18n.t(profile.label) : id;
  }

  renderSummary(state) {
    const m = state.machine || {};
    const why = state.why_key ? i18n.t(state.why_key, state.why_params) : state.why;
    $('#tn-summary').textContent = i18n.t('Tuned as {profile}, {how}. This computer measures as {measured} ({why}).', {
      profile: this.profileLabel(state.profile),
      how: word(HOW_WORDS, state.profile_source),
      measured: this.profileLabel(state.measured),
      why,
    });
    const facts = [
      i18n.t('{cores} cores', { cores: m.cores || '?' }),
      i18n.t('{memory} memory', { memory: size(m.memory_bytes) }),
      m.gpu || m.apple_silicon ? i18n.t('graphics processor') : i18n.t('no graphics processor'),
      m.library_spinning === true ? i18n.t('library on a spinning disk')
        : m.library_spinning === false ? i18n.t('library on a solid-state drive') : null,
      m.ffmpeg ? 'ffmpeg' : i18n.t('no ffmpeg'),
      m.ai_enabled ? i18n.t('image model on') : i18n.t('image model off'),
    ].filter(Boolean);
    const chips = $('#tn-facts');
    chips.replaceChildren(...facts.map((text) => el('span', 'pf-chip', text)));
  }

  renderProfiles(state) {
    const box = $('#tn-profiles');
    box.replaceChildren();
    const option = (id, title, detail) => {
      const label = el('label', 'choice');
      const input = el('input');
      input.type = 'radio';
      input.name = 'tn-profile';
      input.value = id;
      input.checked = (state.setting || 'auto') === id;
      const text = el('span');
      text.append(el('strong', null, title), document.createTextNode(` — ${detail}`));
      label.append(input, text);
      box.append(label);
    };
    option('auto', i18n.t('Automatic'), i18n.t('Measured at every start; this computer is {profile}.', {
      profile: this.profileLabel(state.measured) }));
    for (const p of state.profiles || []) {
      option(p.id, i18n.t(p.label), i18n.t(p.description));
    }
  }

  renderUsage(state) {
    const u = state.usage || {};
    const cards = $('#tn-usage');
    cards.replaceChildren();
    const card = (label, value, sub, bar, limit = u.ceiling_percent) => {
      const c = el('div', 'card');
      c.append(el('div', 'card-label', label), el('div', 'card-value', value));
      if (bar != null) {
        const track = el('div', 'sv-bar');
        const fill = el('i');
        fill.style.width = `${Math.max(0, Math.min(100, bar))}%`;
        if (bar > (limit || 95)) fill.className = 'hot';
        else if (bar > 70) fill.className = 'warm';
        track.append(fill);
        c.append(track);
      }
      c.append(el('div', 'card-sub', sub));
      cards.append(c);
    };
    card(i18n.t('Processor'), `${u.cpu_percent ?? '—'}%`,
      i18n.t('{used} of {cores} cores, at most', { used: u.cores, cores: u.of_cores }), u.cpu_percent);
    card(i18n.t('Memory'), u.memory_percent == null ? '—' : `${u.memory_percent}%`,
      i18n.t('about {used} of {total}, at most', { used: size(u.memory_bytes), total: size(u.of_memory) }),
      u.memory_percent, u.memory_ceiling_percent);
    card(i18n.t('Drive readers'), String(u.disk_readers ?? '—'), i18n.t('files read at once during a scan'));
    card(i18n.t('Uploads at once'), String(u.uploads ?? '—'), i18n.t('files sent to the cloud at the same time'));
    const ceiling = $('#tn-ceiling');
    ceiling.textContent = u.over
      ? i18n.t('These numbers go past the {percent}% of the processor and {memory}% of the memory this profile allows. That is your choice to make; the computer may slow down for everything else.', { percent: u.ceiling_percent, memory: u.memory_ceiling_percent })
      : i18n.t('Within the {percent}% of the processor and {memory}% of the memory this profile allows. The estimate is for a scan and the analysis running together, with every cache full.', { percent: u.ceiling_percent, memory: u.memory_ceiling_percent });
    ceiling.classList.toggle('tn-over', !!u.over);
  }

  renderKnobs(state) {
    const list = $('#tn-knobs');
    list.replaceChildren();
    for (const knob of state.knobs || []) {
      const row = el('div', 'tn-knob');
      const text = el('div', 'tn-knob-text');
      const id = `tn-knob-${knob.name}`;
      const title = el('label', 'tn-knob-title', i18n.t(knob.label));
      title.htmlFor = id;
      text.append(title, el('p', 'pf-detail', i18n.t(knob.help)));
      const chips = el('div', 'pf-chips');
      chips.append(
        el('span', `pf-chip ${knob.source === 'set' ? 'warn' : ''}`, word(SOURCE_WORDS, knob.source)),
        el('span', 'pf-chip', word(APPLIES_WORDS, knob.applies)),
      );
      if (knob.source !== 'auto') {
        chips.append(el('span', 'pf-chip', i18n.t('automatic would be {value}', { value: knob.auto })));
      }
      text.append(chips);
      if (knob.note) text.append(el('p', 'hint', i18n.t(knob.note, knob.note_params || {})));

      const controls = el('div', 'tn-knob-controls');
      const input = el('input', 'input tn-number');
      input.type = 'number';
      input.id = id;
      input.min = knob.min;
      input.max = knob.max;
      input.step = 1;
      input.value = knob.value;
      input.dataset.knob = knob.name;
      controls.append(input);
      if (knob.unit) controls.append(el('span', 'hint', knob.unit));
      if (knob.source === 'set') {
        const back = el('button', 'btn ghost small', i18n.t('Automatic'));
        back.type = 'button';
        back.title = i18n.t('Put this back to the automatic value');
        back.onclick = () => this.send({ values: { [knob.name]: null } }, i18n.t('Saved.'));
        controls.append(back);
      }
      row.append(text, controls);
      list.append(row);
    }
  }
}
