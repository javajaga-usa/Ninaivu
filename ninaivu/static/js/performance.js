/**
 * The Performance page: what this computer can do for Ninaivu, and what would
 * help it do more.
 *
 * Self-contained, like the Server panel: the console hands it a toast, a way
 * to open another page, and the Server page's restart (which asks first and
 * waits for Ninaivu to come back). It measures when it is opened and when asked
 * again, and not otherwise — measuring is a second of the machine's time.
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
    throw Object.assign(new Error(data?.error || response.statusText), { status: response.status, data });
  }
  return data;
}

const api = {
  report: () => json('/api/admin/performance'),
  setting: (key, value) => json('/api/admin/settings', { method: 'POST', body: { [key]: value } }),
};

const GB = 1024 ** 3;
const size = (bytes) => {
  if (bytes == null) return '—';
  return bytes >= 10 * GB ? `${Math.round(bytes / GB)} GB` : `${(bytes / GB).toFixed(1)} GB`;
};
const pct = (value) => (value == null ? '—' : `${Math.round(value)}%`);

const LEVEL_WORDS = { act: 'Needs attention', consider: 'Worth doing', good: 'Fine' };
const ROLE_WORDS = { library: 'Library', index: 'Index and thumbnails' };
const SCHEDULE_WORDS = {
  balanced: 'Balanced — alongside the household',
  quiet: 'Quiet — waits while anyone uses Ninaivu',
  overnight: 'Overnight — analysis waits for the night',
};
const DEVICE_WORDS = { mps: 'the graphics processor (Metal)', cuda: 'the graphics card (CUDA)', cpu: 'the processor' };

export class PerformancePanel {
  constructor({ toast, openPage, restart }) {
    this.toast = toast || (() => {});
    this.openPage = openPage || (() => {});
    this.restart = restart || (() => {});
    this.visible = false;
    this.loading = false;
  }

  wire() {
    $('#pf-refresh')?.addEventListener('click', () => this.refresh());
  }

  show() {
    this.visible = true;
    this.refresh();
  }

  hide() {
    this.visible = false;
  }

  async refresh() {
    if (this.loading) return;
    this.loading = true;
    const button = $('#pf-refresh');
    if (button) { button.disabled = true; button.textContent = 'Measuring…'; }
    try {
      this.render(await api.report());
    } catch (error) {
      $('#pf-summary').textContent = `Could not measure this computer: ${error.message}`;
    } finally {
      this.loading = false;
      if (button) { button.disabled = false; button.textContent = 'Check again'; }
    }
  }

  render(facts) {
    this.renderSummary(facts);
    this.renderAdvice(facts.recommendations || []);
    this.renderMachine(facts);
    this.renderNinaivu(facts);
    this.renderDrives(facts);
  }

  renderSummary(facts) {
    const m = facts.machine || {};
    const advice = facts.recommendations || [];
    const count = (level) => advice.filter((a) => a.level === level).length;
    const act = count('act');
    const consider = count('consider');
    const verdict = act || consider
      ? [act && `${act} need${act === 1 ? 's' : ''} attention`,
        consider && `${consider} worth doing`].filter(Boolean).join(', ')
      : 'Nothing to change';
    $('#pf-summary').textContent = `${m.processor || 'This computer'} · ${m.logical_cores || '?'} cores · `
      + `${size(m.memory_bytes)} · ${m.system || ''} — ${verdict}.`;
    const when = facts.measured_at ? new Date(facts.measured_at * 1000) : null;
    $('#pf-when').textContent = when ? `Measured ${when.toLocaleTimeString()}` : '—';
  }

  renderAdvice(advice) {
    const list = $('#pf-advice');
    list.replaceChildren();
    for (const item of advice) {
      const row = el('li', `pf-item ${item.level}`);
      const head = el('div', 'pf-item-head');
      head.append(el('span', `pf-level ${item.level}`, LEVEL_WORDS[item.level] || item.level),
        el('strong', 'pf-title', item.title));
      row.append(head, el('p', 'pf-detail', item.detail));
      if (item.action) row.append(this.actionButton(item.action));
      list.append(row);
    }
  }

  actionButton(action) {
    const button = el('button', 'btn small pf-action', action.label || 'Do it');
    button.type = 'button';
    button.onclick = async () => {
      if (action.kind === 'tab') { this.openPage(action.tab); return; }
      if (action.kind === 'restart') { this.restart(); return; }
      if (action.kind === 'setting') {
        button.disabled = true;
        try {
          await api.setting(action.key, action.value);
          this.toast('Saved.');
          if (action.restart) this.restart();
          else this.refresh();
        } catch (error) {
          this.toast(`Could not save that: ${error.message}`, true);
        } finally {
          button.disabled = false;
        }
      }
    };
    return button;
  }

  card(label, value, sub) {
    const card = el('div', 'card');
    card.append(el('div', 'card-label', label), el('div', 'card-value pf-value', value));
    if (sub) card.append(el('div', 'card-sub', sub));
    return card;
  }

  renderMachine(facts) {
    const m = facts.machine || {};
    const load = facts.load || {};
    const gpu = facts.graphics || {};
    const kinds = (m.core_kinds || []).map((k) => `${k.cores} ${k.name}`).join(' · ');
    const gpuSub = gpu.in_use ? 'The image model runs on it'
      : gpu.available ? 'Not used by the image model' : 'None Ninaivu can use';
    const battery = load.battery
      ? ` · battery ${pct(load.battery.percent)}${load.battery.plugged ? ', charging' : ''}` : '';
    const tier = facts.tier || {};
    const tierName = tier.tier === 'full' ? 'Full' : tier.tier === 'basic' ? 'Basic' : '—';
    $('#pf-machine').replaceChildren(
      this.card('Kind of computer', tierName, tier.why
        ? `${tier.why}${tier.tier === 'basic' ? ' — the image model, descriptions and text reading are not attempted here' : ''}`
        : ''),
      this.card('Processor', m.processor || '—',
        `${m.logical_cores || '?'} cores${kinds ? ` — ${kinds}` : ''}`),
      this.card('Memory', size(m.memory_bytes),
        `${size(load.memory_available_bytes)} free now · Ninaivu uses ${size(load.ninaivu_memory_bytes)}`),
      this.card('Graphics', gpu.name || (gpu.available ? gpu.available.toUpperCase() : 'None'), gpuSub),
      this.card('Busy now', pct(load.cpu_percent),
        `processor in use${battery}${load.swap_used_bytes ? ` · ${size(load.swap_used_bytes)} swapped` : ''}`),
    );
  }

  renderNinaivu(facts) {
    const h = facts.ninaivu || {};
    const gpu = facts.graphics || {};
    const waiting = facts.backlog || {};
    const night = (h.night || []).join('–');
    const schedule = SCHEDULE_WORDS[h.schedule] || h.schedule || '—';
    const rows = [
      ['Resource mode', `${h.mode || '—'} — ${h.workers} workers, ${h.compute_threads} AI threads, `
        + `${h.server_threads} web threads`],
      ['Background work', h.schedule === 'overnight' && night ? `${schedule} (${night})` : schedule],
      ['Image model', !h.ai_enabled ? 'Off'
        : `${h.ai_model || 'not loaded'} on ${DEVICE_WORDS[gpu.ai_device] || gpu.ai_device || '—'}`
          + (h.ai_gpu ? '' : ' (graphics processor switched off)')],
      ['Waiting for analysis', `${(waiting.analysis || 0).toLocaleString()} photographs and videos`
        + (waiting.faces ? `, ${waiting.faces.toLocaleString()} for faces` : '')],
      ['ffmpeg', h.ffmpeg ? 'Installed' : 'Not installed'],
    ];
    const list = $('#pf-ninaivu');
    list.replaceChildren();
    for (const [term, value] of rows) list.append(el('dt', null, term), el('dd', null, value));
  }

  renderDrives(facts) {
    const box = $('#pf-drives');
    box.replaceChildren();
    for (const drive of facts.storage || []) {
      const card = el('div', 'pf-drive');
      const head = el('div', 'pf-drive-head');
      head.append(el('strong', null, ROLE_WORDS[drive.role] || drive.role),
        el('code', 'pf-path', drive.path));
      card.append(head);
      const chips = el('div', 'pf-chips');
      const chip = (text, tone) => chips.append(el('span', `pf-chip ${tone || ''}`, text));
      if (!drive.present) {
        chip('Not connected', 'bad');
      } else {
        if (drive.filesystem) chip(drive.filesystem.toUpperCase());
        chip(drive.internal ? 'Internal' : (drive.bus || 'External'));
        if (drive.solid_state === true) chip('Solid-state', 'good');
        else if (drive.solid_state === false) chip('Spinning disk', 'warn');
        chip(drive.read_only ? 'Read-only' : 'Writable', drive.read_only ? 'bad' : 'good');
        if (drive.free_bytes != null) chip(`${size(drive.free_bytes)} free of ${size(drive.total_bytes)}`);
      }
      card.append(chips);
      const scan = (facts.scans || {})[drive.path];
      if (scan) {
        const minutes = Math.floor(scan.seconds / 60);
        const took = minutes ? `${minutes} min ${Math.round(scan.seconds % 60)} s` : `${Math.round(scan.seconds)} s`;
        card.append(el('p', 'hint', `The last scan that found nothing new took ${took} for `
          + `${scan.files.toLocaleString()} files — ${(scan.files_per_second || 0).toLocaleString()} a second.`));
      }
      box.append(card);
    }
  }
}
