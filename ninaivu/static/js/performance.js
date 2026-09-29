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
  report: () => json('/api/admin/performance'),
  setting: (key, value) => json('/api/admin/settings', { method: 'POST', body: { [key]: value } }),
};

const GB = 1024 ** 3;
const size = (bytes) => {
  if (bytes == null) return '—';
  return bytes >= 10 * GB ? `${Math.round(bytes / GB)} GB` : `${(bytes / GB).toFixed(1)} GB`;
};
const pct = (value) => (value == null ? '—' : `${Math.round(value)}%`);

// Marked with key() and translated where they are shown: a table built as the
// file loads is built before any language has been fetched.
const LEVEL_WORDS = {
  act: i18n.key('Needs attention'),
  consider: i18n.key('Worth doing'),
  good: i18n.key('Fine'),
};
const ROLE_WORDS = { library: i18n.key('Library'), index: i18n.key('Index and thumbnails') };
const SCHEDULE_WORDS = {
  balanced: i18n.key('Balanced — alongside the household'),
  quiet: i18n.key('Quiet — waits while anyone uses Ninaivu'),
  overnight: i18n.key('Overnight — analysis waits for the night'),
};
const DEVICE_WORDS = {
  mps: i18n.key('the graphics processor (Metal)'),
  cuda: i18n.key('the graphics card (CUDA)'),
  cpu: i18n.key('the processor'),
};
/** A word from one of the tables above, in the current language — or the
 *  server's own word, untouched, when the table does not know it. */
const word = (table, value) => (table[value] ? i18n.t(table[value]) : value);

export class PerformancePanel {
  constructor({ toast, openPage, restart }) {
    this.toast = toast || (() => {});
    this.openPage = openPage || (() => {});
    this.restart = restart || (() => {});
    this.visible = false;
    this.loading = false;
    this.facts = null;
    i18n.onChange(() => { if (this.facts) this.render(this.facts); });
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
    if (button) { button.disabled = true; button.textContent = i18n.t('Measuring…'); }
    try {
      this.render(await api.report());
    } catch (error) {
      $('#pf-summary').textContent = i18n.t('Could not measure this computer: {reason}', { reason: error.message });
    } finally {
      this.loading = false;
      if (button) { button.disabled = false; button.textContent = i18n.t('Check again'); }
    }
  }

  render(facts) {
    this.facts = facts;
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
      ? [act && (act === 1 ? i18n.t('1 needs attention') : i18n.t('{count} need attention', { count: act })),
        consider && i18n.t('{count} worth doing', { count: consider })].filter(Boolean).join(', ')
      : i18n.t('Nothing to change');
    $('#pf-summary').textContent = i18n.t('{processor} · {cores} cores · {memory} · {system} — {verdict}.', {
      processor: m.processor || i18n.t('This computer'),
      cores: m.logical_cores || '?',
      memory: size(m.memory_bytes),
      system: m.system || '',
      verdict,
    });
    const when = facts.measured_at ? new Date(facts.measured_at * 1000) : null;
    $('#pf-when').textContent = when
      ? i18n.t('Measured {time}', { time: when.toLocaleTimeString(i18n.locale()) }) : '—';
  }

  renderAdvice(advice) {
    const list = $('#pf-advice');
    list.replaceChildren();
    for (const item of advice) {
      const row = el('li', `pf-item ${item.level}`);
      const head = el('div', 'pf-item-head');
      head.append(el('span', `pf-level ${item.level}`, word(LEVEL_WORDS, item.level)),
        el('strong', 'pf-title', i18n.t(item.title)));
      // The server marks its advice with said(); one with numbers in it comes
      // as a template and its values, to be translated before filling in.
      const detail = item.detail_key ? i18n.t(item.detail_key, item.detail_params) : i18n.t(item.detail);
      row.append(head, el('p', 'pf-detail', detail));
      if (item.action) row.append(this.actionButton(item.action));
      list.append(row);
    }
  }

  actionButton(action) {
    const button = el('button', 'btn small pf-action', action.label ? i18n.t(action.label) : i18n.t('Do it'));
    button.type = 'button';
    button.onclick = async () => {
      if (action.kind === 'tab') { this.openPage(action.tab); return; }
      if (action.kind === 'restart') { this.restart(); return; }
      if (action.kind === 'setting') {
        button.disabled = true;
        try {
          await api.setting(action.key, action.value);
          this.toast(i18n.t('Saved.'));
          if (action.restart) this.restart();
          else this.refresh();
        } catch (error) {
          this.toast(i18n.t('Could not save that: {reason}', { reason: error.message }), true);
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
    const gpuSub = gpu.in_use ? i18n.t('The image model runs on it')
      : gpu.available ? i18n.t('Not used by the image model') : i18n.t('None Ninaivu can use');
    const busy = [i18n.t('processor in use')];
    if (load.battery) {
      busy.push(load.battery.plugged
        ? i18n.t('battery {percent}, charging', { percent: pct(load.battery.percent) })
        : i18n.t('battery {percent}', { percent: pct(load.battery.percent) }));
    }
    if (load.swap_used_bytes) busy.push(i18n.t('{size} swapped', { size: size(load.swap_used_bytes) }));
    const tier = facts.tier || {};
    const tierName = tier.tier === 'full' ? i18n.t('Full') : tier.tier === 'basic' ? i18n.t('Basic') : '—';
    const cores = m.logical_cores || '?';
    const why = tier.why_key ? i18n.t(tier.why_key, tier.why_params) : tier.why;
    $('#pf-machine').replaceChildren(
      this.card(i18n.t('Kind of computer'), tierName, why
        ? (tier.tier === 'basic'
          ? i18n.t('{why} — the image model, descriptions and text reading are not attempted here', { why })
          : why)
        : ''),
      this.card(i18n.t('Processor'), m.processor || '—', kinds
        ? i18n.t('{cores} cores — {kinds}', { cores, kinds })
        : i18n.t('{cores} cores', { cores })),
      this.card(i18n.t('Memory'), size(m.memory_bytes),
        i18n.t('{free} free now · Ninaivu uses {used}', {
          free: size(load.memory_available_bytes), used: size(load.ninaivu_memory_bytes) })),
      this.card(i18n.t('Graphics'), gpu.name || (gpu.available ? gpu.available.toUpperCase() : i18n.t('None')), gpuSub),
      this.card(i18n.t('Busy now'), pct(load.cpu_percent), busy.join(' · ')),
    );
  }

  renderNinaivu(facts) {
    const h = facts.ninaivu || {};
    const gpu = facts.graphics || {};
    const waiting = facts.backlog || {};
    const night = (h.night || []).join('–');
    const schedule = word(SCHEDULE_WORDS, h.schedule) || '—';
    const model = {
      model: h.ai_model || i18n.t('not loaded'),
      device: word(DEVICE_WORDS, gpu.ai_device) || '—',
    };
    const analysis = (waiting.analysis || 0).toLocaleString();
    const rows = [
      [i18n.t('Resource mode'), i18n.t('{mode} — {workers} workers, {ai} AI threads, {web} web threads', {
        mode: h.mode || '—', workers: h.workers, ai: h.compute_threads, web: h.server_threads })],
      [i18n.t('Background work'), h.schedule === 'overnight' && night ? `${schedule} (${night})` : schedule],
      [i18n.t('Image model'), !h.ai_enabled ? i18n.t('Off')
        : h.ai_gpu ? i18n.t('{model} on {device}', model)
          : i18n.t('{model} on {device} (graphics processor switched off)', model)],
      [i18n.t('Waiting for analysis'), waiting.faces
        ? i18n.t('{count} photographs and videos, {faces} for faces', {
          count: analysis, faces: waiting.faces.toLocaleString() })
        : i18n.t('{count} photographs and videos', { count: analysis })],
      ['ffmpeg', h.ffmpeg ? i18n.t('Installed') : i18n.t('Not installed')],
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
      head.append(el('strong', null, word(ROLE_WORDS, drive.role)),
        el('code', 'pf-path', drive.path));
      card.append(head);
      const chips = el('div', 'pf-chips');
      const chip = (text, tone) => chips.append(el('span', `pf-chip ${tone || ''}`, text));
      if (!drive.present) {
        chip(i18n.t('Not connected'), 'bad');
      } else {
        if (drive.filesystem) chip(drive.filesystem.toUpperCase());
        chip(drive.internal ? i18n.t('Internal') : (drive.bus || i18n.t('External')));
        if (drive.solid_state === true) chip(i18n.t('Solid-state'), 'good');
        else if (drive.solid_state === false) chip(i18n.t('Spinning disk'), 'warn');
        chip(drive.read_only ? i18n.t('Read-only') : i18n.t('Writable'), drive.read_only ? 'bad' : 'good');
        if (drive.free_bytes != null) {
          chip(i18n.t('{free} free of {total}', { free: size(drive.free_bytes), total: size(drive.total_bytes) }));
        }
      }
      card.append(chips);
      const scan = (facts.scans || {})[drive.path];
      if (scan) {
        const minutes = Math.floor(scan.seconds / 60);
        const took = minutes
          ? i18n.t('{minutes} min {seconds} s', { minutes, seconds: Math.round(scan.seconds % 60) })
          : i18n.t('{seconds} s', { seconds: Math.round(scan.seconds) });
        card.append(el('p', 'hint', i18n.t('The last scan that found nothing new took {took} for {files} files — {rate} a second.', {
          took, files: scan.files.toLocaleString(), rate: (scan.files_per_second || 0).toLocaleString() })));
      }
      box.append(card);
    }
  }
}
