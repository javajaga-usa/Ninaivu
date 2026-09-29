/**
 * The Server page — the desktop control panel, in the console.
 *
 * Self-contained, like the Cloud and AI server panels: the console hands it a
 * toast function and it owns everything under `[data-panel="server"]`.
 *
 * It polls only while it is the page on screen: readings every two seconds, the
 * log alongside them. A restart is the one thing that keeps it asking after
 * the server has gone, because the answer it is waiting for is the new server.
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
  state: () => json('/api/admin/server'),
  log: (after) => json(`/api/admin/server/log${after != null ? `?after=${after}` : ''}`),
  restart: (mode) => json('/api/admin/server/restart', { method: 'POST', body: mode ? { mode } : {} }),
  stop: () => json('/api/admin/server/stop', { method: 'POST', body: {} }),
  network: (enabled) => json('/api/admin/server/network', { method: 'POST', body: { enabled } }),
  consoleNetwork: (enabled) => json('/api/admin/server/console-network', { method: 'POST', body: { enabled } }),
  overview: () => json('/api/admin/overview'),
  settings: (body) => json('/api/admin/settings', { method: 'POST', body }),
};

const POLL_MS = 2000;
const HISTORY = 60;                 // two minutes at POLL_MS
const LOG_LIMIT = 400_000;          // characters kept in the view
const BACK_TIMEOUT_MS = 25 * 60 * 1000;

const gb = (bytes) => {
  if (bytes == null) return '—';
  const g = bytes / 1024 ** 3;
  return g >= 100 ? `${Math.round(g)} GB` : `${g.toFixed(1)} GB`;
};
const mb = (bytes) => (bytes == null ? '—' : bytes >= 1024 ** 3 ? gb(bytes) : `${Math.round(bytes / 1024 ** 2)} MB`);
const pct = (value) => (value == null ? '—' : `${Math.round(value)}%`);

function uptime(seconds) {
  if (seconds == null || seconds < 0) return '—';
  const m = Math.floor(seconds / 60);
  if (m < 1) return i18n.t('under a minute');
  const d = Math.floor(m / 1440);
  const h = Math.floor((m % 1440) / 60);
  const mm = m % 60;
  if (d) return i18n.t('{days} d {hours} h', { days: d, hours: h });
  if (h) return i18n.t('{hours} h {minutes} min', { hours: h, minutes: mm });
  return i18n.t('{minutes} min', { minutes: mm });
}

export class ServerPanel {
  constructor({ toast }) {
    this.toast = toast || (() => {});
    this.visible = false;
    this.timer = null;
    this.state = null;
    this.cpu = [];                  // [{machine, ninaivu}]
    this.logPosition = null;
    this.follow = true;
    this.selectedMode = null;
    this.phase = 'running';         // running | restarting | stopped
    this.waitingSince = 0;
    this.oldPid = null;
  }

  wire() {
    $('#sv-restart')?.addEventListener('click', () => this.confirmRestart(null));
    $('#sv-stop')?.addEventListener('click', () => this.confirmStop());
    $('#sv-apply-mode')?.addEventListener('click', () => this.confirmRestart(this.selectedMode));
    $('#sv-net-toggle')?.addEventListener('change', (event) => {
      // The switch shows what is saved; it only moves once the change is confirmed.
      const wanted = event.target.checked;
      event.target.checked = !wanted;
      this.confirmNetwork(wanted);
    });
    $('#sv-console-toggle')?.addEventListener('change', async (event) => {
      const wanted = event.target.checked;
      event.target.disabled = true;
      try {
        await api.consoleNetwork(wanted);
        this.toast?.(wanted ? i18n.t('Opening the console to the home network — Ninaivu is restarting.')
          : i18n.t('Keeping the console to this computer — Ninaivu is restarting.'));
      } catch (exc) {
        event.target.checked = !wanted;
        this.toast?.(exc.message, true);
      } finally {
        event.target.disabled = false;
      }
    });
    $('#sv-log-follow')?.addEventListener('click', () => this.setFollow(!this.follow));
    $('#sv-log-copy')?.addEventListener('click', () => this.copyLog());
    $('#sv-log-clear')?.addEventListener('click', () => { $('#sv-log').textContent = ''; this.findMatches(); });
    $('#sv-log-find')?.addEventListener('input', () => this.findMatches());
    $('#sv-log-find')?.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') { event.preventDefault(); this.findNext(event.shiftKey); }
    });
    // Scrolling up to read stops the view jumping back to the end.
    $('#sv-log')?.addEventListener('scroll', () => {
      const log = $('#sv-log');
      const atEnd = log.scrollHeight - log.scrollTop - log.clientHeight < 24;
      if (atEnd !== this.follow) this.setFollow(atEnd, false);
    });
    const cert = $('#sv-cert');
    if (cert) cert.href = this.familyUrl('/cert');
    this.wireRemote();
    // What this panel drew itself is redrawn in the new language; the cached
    // pieces (modes, addresses) are only redrawn when their signature changes.
    i18n.onChange(() => {
      this.modesSignature = null;
      this.urlsSignature = null;
      if (this.state && this.phase === 'running') {
        // A redraw, not a reading: the CPU chart must not gain a sample.
        this.relabelling = true;
        try { this.render(this.state); } finally { this.relabelling = false; }
      }
      if ($('#sv-log-follow')) this.setFollow(this.follow, false);
      if (this.matches && $('#sv-log-matches')) this.paintMatches();
    });
    $('#sv-update-toggle')?.addEventListener('change', async (event) => {
      try {
        await api.settings({ update_check: event.target.checked });
        this.toast(event.target.checked ? i18n.t('Ninaivu will ask GitHub once a day.') : i18n.t('Ninaivu will not ask.'));
      } catch (exc) {
        event.target.checked = !event.target.checked;
        this.toast(exc.message, true);
      }
    });
  }

  renderUpdate(update) {
    const note = $('#sv-update');
    const box = $('#sv-update-toggle');
    if (!note || !box) return;
    if (update && document.activeElement !== box) box.checked = update.enabled !== false;
    if (!update || !update.available) { note.hidden = true; return; }
    note.replaceChildren();
    note.append(`${i18n.t('Ninaivu {latest} is out (this is {current}).', { latest: update.latest, current: update.current })} `);
    if (update.url) {
      const a = document.createElement('a');
      a.href = update.url; a.target = '_blank'; a.rel = 'noopener';
      a.textContent = i18n.t('See what changed');
      note.append(a, '.');
    }
    note.hidden = false;
  }

  /* -- Away from home: the remote-access provider ------------------------ */

  wireRemote() {
    const select = $('#remote-provider');
    if (!select || select.dataset.wired) return;
    select.dataset.wired = '1';
    const showFields = () => {
      $('#remote-networks-field').hidden = select.value !== 'wireguard';
      $('#remote-hostname-field').hidden = !['tunnel', 'proxy'].includes(select.value);
    };
    select.addEventListener('change', showFields);
    $('#remote-save')?.addEventListener('click', async () => {
      const body = { remote_access: select.value };
      if (select.value === 'wireguard') body.remote_networks = $('#remote-networks').value;
      if (['tunnel', 'proxy'].includes(select.value)) body.remote_hostname = $('#remote-hostname').value;
      const button = $('#remote-save');
      button.disabled = true;
      try {
        await api.settings(body);
        this.toast(i18n.t('Saved. The addresses above follow the new choice.'));
        this.urlsSignature = null;               // redraw the address list
        await this.loadRemote();
      } catch (exc) {
        this.toast(exc.message, true);
      } finally {
        button.disabled = false;
      }
    });
    this.loadRemote();
  }

  async loadRemote() {
    let settings;
    try {
      settings = (await api.overview()).app || {};
    } catch { return; }
    const select = $('#remote-provider');
    if (!select) return;
    if (document.activeElement !== select) select.value = settings.remote_access || 'auto';
    const nets = $('#remote-networks');
    if (nets && document.activeElement !== nets) nets.value = (settings.remote_networks || []).join(', ');
    const host = $('#remote-hostname');
    if (host && document.activeElement !== host) host.value = settings.remote_hostname || '';
    select.dispatchEvent(new Event('change'));
    this.renderRemote(this.state?.endpoints?.remote_access);
  }

  renderRemote(access) {
    const summary = $('#remote-summary');
    const problems = $('#remote-problems');
    if (!summary || !problems) return;
    problems.replaceChildren();
    if (!access) { summary.textContent = i18n.t('Reading…'); return; }
    const names = [...(access.hostnames || []), ...(access.addresses || [])];
    summary.textContent = access.name === 'none'
      ? i18n.t('Nothing is set up: Ninaivu answers on the home network only.')
      : `${access.title}${names.length ? ` — ${names.join(', ')}` : ''}.`;
    for (const problem of access.problems || []) {
      const li = document.createElement('li');
      li.className = 'hint';
      li.textContent = problem;
      problems.append(li);
    }
  }

  show() {
    this.visible = true;
    this.tick();
  }

  hide() {
    this.visible = false;
    if (this.phase === 'running') this.stopTimer();
  }

  stopTimer() {
    clearTimeout(this.timer);
    this.timer = null;
  }

  schedule(ms = POLL_MS) {
    this.stopTimer();
    if (this.visible || this.phase === 'restarting') this.timer = setTimeout(() => this.tick(), ms);
  }

  async tick() {
    if (this.phase === 'stopped') return;
    try {
      const state = await api.state();
      if (this.phase === 'restarting') {
        if (state.pid === this.oldPid) {
          this.renderWaiting(i18n.t('Ninaivu is finishing active work before it restarts…'));
          this.schedule();
          return;
        } else {
          this.phase = 'running';
          this.cpu = [];
          this.logPosition = null;
          this.toast(i18n.t('Ninaivu has restarted.'));
        }
      }
      this.state = state;
      this.render(state);
      if (this.visible) await this.pullLog();
    } catch (error) {
      if (this.phase === 'restarting') {
        this.renderWaiting(i18n.t('Ninaivu is starting again…'));
        if (Date.now() - this.waitingSince > BACK_TIMEOUT_MS) {
          this.phase = 'running';
          this.renderWaiting(i18n.t('Ninaivu has not come back. The restart log is {path} on the Ninaivu computer.', { path: '.ninaivu-control\\restart.log' }));
          this.toast(i18n.t('Ninaivu has not come back after a restart.'), true);
          return;
        }
      } else if (error.status !== 401) {
        $('#sv-state').textContent = i18n.t('Not answering');
        $('#sv-state').className = 'sv-state bad';
        $('#sv-summary').textContent = i18n.t('Could not read the server: {reason}', { reason: error.message });
      }
    }
    this.schedule();
  }

  /* -- drawing ------------------------------------------------------------- */

  familyUrl(path = '') {
    const e = this.state?.endpoints;
    if (e?.family) return `${e.scheme}://${e.family}${path}`;
    const host = location.hostname.replace(/-admin(?=\.local$)/, '');
    return `${location.protocol}//${host}${path}`;
  }

  render(state) {
    const m = state.metrics;
    const started = m?.process?.started_at;
    const since = started ? (Date.now() / 1000) - started : null;

    const pill = $('#sv-state');
    if (state.restarting) {
      pill.textContent = i18n.t('Restarting'); pill.className = 'sv-state busy';
    } else {
      pill.textContent = i18n.t('Running'); pill.className = 'sv-state good';
    }
    const modeLabel = state.modes.find((x) => x.id === state.mode)?.label || state.mode;
    $('#sv-summary').textContent = state.mode_chosen
      ? i18n.t('Up {uptime} · {mode} mode', { uptime: uptime(since), mode: modeLabel })
      : i18n.t('Up {uptime} · {mode} mode (default — no mode was chosen when it started)', { uptime: uptime(since), mode: modeLabel });

    const details = $('#sv-details');
    details.innerHTML = '';
    const row = (label, value, href) => {
      details.append(el('dt', '', label));
      const dd = el('dd');
      if (href) {
        const a = el('a', '', value);
        a.href = href; a.target = '_blank'; a.rel = 'noopener';
        dd.append(a);
      } else {
        dd.textContent = value;
      }
      details.append(dd);
    };
    const e = state.endpoints || {};
    row(i18n.t('Ports'), i18n.t('{family} family · {console} console', { family: e.port ?? '—', console: e.admin_port ?? '—' }));
    const b = state.budget || {};
    row(i18n.t('Threads'), i18n.t('{workers} scan workers · {compute} AI/video · {requests} requests', { workers: b.workers, compute: b.compute_threads, requests: b.server_threads }));
    row(i18n.t('Process'), `PID ${state.pid} · Python ${state.python}`);

    const busy = $('#sv-busy');
    busy.innerHTML = '';
    (state.busy || []).forEach((line) => busy.append(el('li', '', line)));
    busy.hidden = !(state.busy || []).length;

    const idle = this.phase === 'running' && !state.restarting;
    $('#sv-restart').disabled = !idle || !state.can_restart;
    $('#sv-restart').title = state.restart_problem || '';
    $('#sv-stop').disabled = !idle || !state.can_stop;

    this.renderModes(state, idle);
    this.renderNetwork(state, idle);
    this.renderUpdate(state.update);
    if (m) this.renderMetrics(state, m);
    const cert = $('#sv-cert');
    if (cert) cert.href = this.familyUrl('/cert');
  }

  renderWaiting(message) {
    const pill = $('#sv-state');
    pill.textContent = this.phase === 'stopped' ? i18n.t('Stopped') : i18n.t('Restarting');
    pill.className = `sv-state ${this.phase === 'stopped' ? 'bad' : 'busy'}`;
    $('#sv-summary').textContent = message;
    $('#sv-restart').disabled = true;
    $('#sv-stop').disabled = true;
    $('#sv-apply-mode').disabled = true;
    const toggle = $('#sv-net-toggle');
    if (toggle) toggle.disabled = true;
    this.modesSignature = null;       // so the mode picker is redrawn when it is back
  }

  renderNetwork(state, idle) {
    const n = state.network;
    const toggle = $('#sv-net-toggle');
    if (!n || !toggle) return;
    toggle.checked = n.enabled;
    toggle.disabled = !idle;
    $('#sv-net-label').textContent = n.enabled ? i18n.t('On') : i18n.t('Off');
    const consoleToggle = $('#sv-console-toggle');
    if (consoleToggle && consoleToggle !== document.activeElement) {
      consoleToggle.checked = !!n.console_setting;
      consoleToggle.disabled = !idle || !n.enabled;
    }

    let summary;
    let note = '';
    if (n.enabled && n.family_on_network) {
      summary = i18n.t('Every device on your home network can open Ninaivu: phones, tablets and other computers.');
    } else if (!n.enabled && !n.family_on_network) {
      summary = i18n.t('Only this computer can open Ninaivu. Other devices on the network cannot reach it.');
    } else if (n.enabled) {
      summary = i18n.t('Switched on, but Ninaivu is still only on this computer.');
      note = i18n.t('It has not restarted since the switch was turned on, or it was started with {flag}. Restart Ninaivu to put it on the network.', { flag: '--local-only' });
    } else {
      summary = i18n.t('Switched off, but Ninaivu is still on the network.');
      note = i18n.t('The change takes effect when Ninaivu restarts.');
    }
    if (n.enabled && n.family_on_network && !n.console_on_network) {
      note = i18n.t('The admin console is kept to this computer, so only the family app is on the network.');
    }
    $('#sv-net-summary').textContent = summary;
    const noteEl = $('#sv-net-note');
    noteEl.textContent = note;
    noteEl.hidden = !note;

    // Only redrawn when the addresses change, so a link keeps its focus.
    const e = state.endpoints || {};
    const signature = JSON.stringify([e.family_urls, e.admin_urls,
      e.tailnet_family_urls, e.tailnet_admin_urls]);
    if (signature === this.urlsSignature) return;
    this.urlsSignature = signature;
    const list = $('#sv-net-urls');
    list.innerHTML = '';
    const links = (label, urls) => {
      if (!urls?.length) return;
      list.append(el('dt', '', label));
      const dd = el('dd', 'sv-urls');
      urls.forEach((href) => {
        const a = el('a', '', href);
        a.href = href; a.target = '_blank'; a.rel = 'noopener';
        dd.append(a);
      });
      list.append(dd);
    };
    links(i18n.t('Family app'), e.family_urls);
    links(i18n.t('Admin console'), e.admin_urls);
    // What the remote-access provider says works from outside the house: the
    // ones to give the household for when they are away.
    const via = e.remote_access?.title ? ` (${e.remote_access.title})` : '';
    links(`${i18n.t('Family app, away from home')}${via}`, e.tailnet_family_urls);
    links(`${i18n.t('Admin console, away from home')}${via}`, e.tailnet_admin_urls);
    this.renderRemote(e.remote_access);
  }

  async confirmNetwork(on) {
    const n = this.state?.network || {};
    const remote = !n.request_is_local;
    const yes = await this.ask(on ? {
      title: i18n.t('Put Ninaivu on your network?'),
      text: i18n.t('Ninaivu restarts, then answers every device on your home network — phones, tablets and other computers — by name and by this computer’s address. Everyone still signs in as before.'),
      confirm: i18n.t('Turn on and restart'),
    } : {
      title: i18n.t('Keep Ninaivu to this computer?'),
      text: i18n.t('Ninaivu restarts, then answers only on this computer, at localhost. Phones, tablets and other computers lose access until this is turned back on.')
        + (remote ? ` ${i18n.t('You are using the console from another device, so this page stops working here, and turning it back on has to be done on the Ninaivu computer itself.')}` : ''),
      confirm: i18n.t('Turn off and restart'),
      danger: true,
    });
    if (!yes) return;
    let reply;
    try {
      reply = await api.network(on);
    } catch (error) {
      this.toast(i18n.t('Could not change network access: {reason}', { reason: error.message }), true);
      return;
    }
    if (!reply.applied) {
      this.toast(reply.error
        ? i18n.t('Saved. It takes effect the next time Ninaivu starts ({reason}).', { reason: reply.error })
        : i18n.t('Saved. It takes effect the next time Ninaivu starts.'));
      this.tick();
      return;
    }
    this.oldPid = reply.pid;
    this.waitingSince = Date.now();
    if (!on && remote) {
      // Nothing to wait for from here: the new server will not answer this device.
      this.phase = 'stopped';
      this.stopTimer();
      this.renderWaiting(i18n.t('Ninaivu is restarting for its own computer only, so this page no longer reaches it. To put it back on the network, open the console on the Ninaivu computer: {url}',
        { url: `https://localhost:${this.state?.endpoints?.admin_port ?? 3000}` }));
      $('#sv-state').textContent = i18n.t('Local only');
      return;
    }
    this.phase = 'restarting';
    this.renderWaiting(on ? i18n.t('Ninaivu is restarting to join the network…')
      : i18n.t('Ninaivu is restarting for this computer only…'));
    this.schedule(3000);
  }

  renderModes(state, idle) {
    const box = $('#sv-modes');
    if (!this.selectedMode) this.selectedMode = state.mode;
    // Redrawn only when something on it changed: rebuilding the radios on every
    // reading would take keyboard focus away from them every two seconds.
    const signature = JSON.stringify([state.mode, this.selectedMode, idle, state.can_restart]);
    if (signature === this.modesSignature) return;
    this.modesSignature = signature;
    box.innerHTML = '';
    state.modes.forEach((mode) => {
      const card = el('label', `sv-mode${mode.id === this.selectedMode ? ' selected' : ''}`);
      const input = el('input');
      input.type = 'radio'; input.name = 'sv-mode'; input.value = mode.id;
      input.checked = mode.id === this.selectedMode;
      input.onchange = () => {
        this.selectedMode = mode.id;
        this.renderModes(state, idle);
        box.querySelector('input:checked')?.focus();
      };
      const head = el('span', 'sv-mode-head');
      head.append(input, el('strong', '', mode.label));
      if (mode.id === state.mode) head.append(el('span', 'sv-mode-now', i18n.t('Running')));
      card.append(head, el('span', 'sv-mode-text', mode.description),
        el('span', 'sv-mode-budget',
          i18n.t('{workers} scan workers · {compute} AI/video threads · {requests} requests', { workers: mode.workers, compute: mode.compute_threads, requests: mode.server_threads })));
      box.append(card);
    });
    const apply = $('#sv-apply-mode');
    apply.disabled = !idle || !state.can_restart || this.selectedMode === state.mode;
    apply.textContent = this.selectedMode === state.mode ? i18n.t('Apply and restart')
      : i18n.t('Restart in {mode} mode', { mode: state.modes.find((x) => x.id === this.selectedMode)?.label || '' });
  }

  renderMetrics(state, m) {
    const cards = $('#sv-cards');
    cards.innerHTML = '';
    const card = (label, value, sub, bar, tone) => {
      const c = el('div', `card${tone ? ` ${tone}` : ''}`);
      c.append(el('div', 'card-label', label), el('div', 'card-value', value));
      if (bar != null) {
        const track = el('div', 'sv-bar');
        const fill = el('i');
        fill.style.width = `${Math.max(0, Math.min(100, bar))}%`;
        if (bar > 85) fill.className = 'hot'; else if (bar > 70) fill.className = 'warm';
        track.append(fill);
        c.append(track);
      }
      c.append(el('div', 'card-sub', sub));
      cards.append(c);
      return c;
    };
    card(i18n.t('Computer CPU'), pct(m.cpu), i18n.t('{cores} logical cores, all applications', { cores: m.cpus }), m.cpu);
    card(i18n.t('Computer memory'), pct(m.memory.percent),
      i18n.t('{used} of {total} in use', { used: gb(m.memory.used), total: gb(m.memory.total) }), m.memory.percent);
    const p = m.process;
    if (p) {
      card(i18n.t('Ninaivu'), p.cpu == null ? '…' : `${p.cpu.toFixed(1)}%`,
        p.processes > 1
          ? i18n.t('{memory} · {threads} threads · {processes} processes', { memory: mb(p.memory), threads: p.threads, processes: p.processes })
          : i18n.t('{memory} · {threads} threads', { memory: mb(p.memory), threads: p.threads }));
    }
    (m.disks || []).forEach((disk) => {
      const tone = disk.free < 20 * 1024 ** 3 ? 'warn' : '';
      const c = card(disk.role === 'library' ? i18n.t('Library disk free') : i18n.t('Install disk free'), gb(disk.free),
        i18n.t('{drive} · {percent}% of {total} used', { drive: disk.drive, percent: Math.round(disk.percent), total: gb(disk.total) }), null, tone);
      c.title = disk.path;
    });
    if (m.battery) {
      card(i18n.t('Battery'), pct(m.battery.percent),
        m.battery.plugged == null ? i18n.t('Charging state unknown')
          : m.battery.plugged ? i18n.t('Connected to power') : i18n.t('Running on battery'),
        null, !m.battery.plugged && m.battery.percent < 25 ? 'warn' : '');
    }
    if (m.power_watts != null) {
      card(i18n.t('Power draw'), `${m.power_watts.toFixed(1)} W`, i18n.t('Whole computer, from the battery'));
    }

    $('#sv-updated').textContent = i18n.t('Updated {time}', { time: new Date(m.at * 1000).toLocaleTimeString(i18n.locale()) });

    if (!this.relabelling) this.cpu.push({ machine: m.cpu ?? 0, ninaivu: p?.cpu ?? 0 });
    if (this.cpu.length > HISTORY) this.cpu.splice(0, this.cpu.length - HISTORY);
    this.drawChart();
  }

  drawChart() {
    const svg = $('#sv-chart');
    if (!svg) return;
    const W = 600; const H = 90;
    const x = (i) => (HISTORY === 1 ? 0 : (i + (HISTORY - this.cpu.length)) * (W / (HISTORY - 1)));
    const y = (v) => H - 2 - (Math.max(0, Math.min(100, v)) * (H - 4)) / 100;
    const line = (key) => this.cpu.map((s, i) => `${x(i).toFixed(1)},${y(s[key]).toFixed(1)}`).join(' ');
    const grid = [25, 50, 75].map((v) => `<line class="grid" x1="0" x2="${W}" y1="${y(v)}" y2="${y(v)}"/>`).join('');
    let body = grid;
    if (this.cpu.length > 1) {
      const machine = line('machine');
      body += `<polygon class="area" points="${x(0)},${H} ${machine} ${x(this.cpu.length - 1)},${H}"/>`
        + `<polyline class="machine" points="${machine}"/>`
        + `<polyline class="ninaivu" points="${line('ninaivu')}"/>`;
    }
    svg.innerHTML = body;
    const last = this.cpu[this.cpu.length - 1];
    const peak = Math.max(...this.cpu.map((s) => s.machine));
    $('#sv-chart-title').textContent = last
      ? i18n.t('CPU activity · last two minutes · now {now}% · peak {peak}%', { now: Math.round(last.machine), peak: Math.round(peak) })
      : i18n.t('CPU activity');
  }

  /* -- the log ------------------------------------------------------------- */

  async pullLog() {
    let data;
    try {
      data = await api.log(this.logPosition);
    } catch {
      return;
    }
    const log = $('#sv-log');
    const where = $('#sv-log-where');
    if (!data.available) {
      where.textContent = i18n.t('No server log yet. Ninaivu writes one when it is started from the Ninaivu Control Panel or restarted from this page.');
      if (this.logPosition !== 0) log.textContent = '';
      this.logPosition = 0;
      return;
    }
    const modified = new Date(data.modified * 1000);
    where.textContent = i18n.t('{path} · last written {when}', { path: data.path, when: modified.toLocaleString(i18n.locale()) });
    if (data.reset) {
      log.textContent = data.truncated ? `[${i18n.t('Showing the most recent part of the log')}]\n` : '';
      this.matchIndex = -1;
      if (!data.text && $('#sv-log-find').value) this.findMatches();
    }
    if (data.text) {
      log.append(document.createTextNode(data.text));
      if (log.textContent.length > LOG_LIMIT) {
        log.textContent = log.textContent.slice(-LOG_LIMIT);
      }
      if ($('#sv-log-find').value) this.findMatches(true);
    }
    this.logPosition = data.position;
    if (this.follow) log.scrollTop = log.scrollHeight;
  }

  setFollow(on, scroll = true) {
    this.follow = on;
    const button = $('#sv-log-follow');
    button.textContent = on ? i18n.t('Following') : i18n.t('Follow');
    button.setAttribute('aria-pressed', String(on));
    if (on && scroll) { const log = $('#sv-log'); log.scrollTop = log.scrollHeight; }
  }

  async copyLog() {
    const text = $('#sv-log').textContent;
    try {
      await navigator.clipboard.writeText(text);
      this.toast(i18n.t('Log copied.'));
    } catch {
      // Clipboard needs a secure context; select it for a manual copy instead.
      const range = document.createRange();
      range.selectNodeContents($('#sv-log'));
      const selection = getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      this.toast(i18n.t('Selected the log — press Ctrl+C to copy it.'));
    }
  }

  /**
   * Find in the log. Matches are painted with the CSS Custom Highlight API
   * rather than selected: a selection fights the focused search box for the
   * caret, and every new piece of log would otherwise throw the place away.
   * `keep` holds the current match across a refresh.
   */
  findMatches(keep = false) {
    const log = $('#sv-log');
    log.normalize();                 // appended pieces into one text node
    const query = $('#sv-log-find').value.trim().toLowerCase();
    const text = log.textContent.toLowerCase();
    const previous = keep && this.matchIndex >= 0 ? this.matches?.[this.matchIndex] : null;
    this.matches = [];
    if (query) {
      for (let i = text.indexOf(query); i >= 0; i = text.indexOf(query, i + query.length)) {
        this.matches.push(i);
        if (this.matches.length >= 2000) break;
      }
    }
    this.matchIndex = previous != null ? this.matches.indexOf(previous) : -1;
    this.paintMatches();
  }

  paintMatches() {
    const query = $('#sv-log-find').value.trim();
    const label = $('#sv-log-matches');
    const n = this.matches?.length || 0;
    if (!query) label.textContent = '';
    else if (!n) label.textContent = i18n.t('Not found');
    else if (this.matchIndex >= 0) label.textContent = i18n.t('{index} of {count}', { index: this.matchIndex + 1, count: `${n}${n >= 2000 ? '+' : ''}` });
    else label.textContent = i18n.t('{count} found · Enter for next', { count: `${n}${n >= 2000 ? '+' : ''}` });

    if (typeof CSS === 'undefined' || !CSS.highlights || typeof Highlight === 'undefined') return;
    const node = $('#sv-log').firstChild;
    const all = new Highlight();
    const current = new Highlight();
    if (node && query) {
      this.matches.forEach((start, index) => {
        const range = new Range();
        range.setStart(node, start);
        range.setEnd(node, start + query.length);
        (index === this.matchIndex ? current : all).add(range);
      });
    }
    CSS.highlights.set('sv-find', all);
    CSS.highlights.set('sv-find-current', current);
  }

  findNext(backwards) {
    if (!this.matches?.length) return;
    const n = this.matches.length;
    this.matchIndex = this.matchIndex < 0
      ? (backwards ? n - 1 : 0)
      : (backwards ? this.matchIndex - 1 + n : this.matchIndex + 1) % n;
    this.paintMatches();
    // Reading back through the log: stop following the end while doing it.
    this.setFollow(false, false);
    const log = $('#sv-log');
    const node = log.firstChild;
    if (!node) return;
    const range = new Range();
    range.setStart(node, this.matches[this.matchIndex]);
    range.setEnd(node, this.matches[this.matchIndex] + 1);
    const rect = range.getBoundingClientRect();
    const box = log.getBoundingClientRect();
    log.scrollTop += rect.top - box.top - log.clientHeight / 2;
  }

  /* -- restart and stop ---------------------------------------------------- */

  ask({ title, text, confirm, danger }) {
    return new Promise((resolve) => {
      const modal = $('#server-modal');
      $('#server-modal-title').textContent = title;
      $('#server-modal-text').textContent = text;
      const busy = $('#server-modal-busy');
      busy.innerHTML = '';
      (this.state?.busy || []).forEach((line) => busy.append(el('li', '', line)));
      busy.hidden = !(this.state?.busy || []).length;
      const ok = $('#server-modal-confirm');
      ok.textContent = confirm;
      ok.className = `btn ${danger ? 'danger' : 'primary'}`;
      modal.hidden = false;
      const close = (answer) => {
        modal.hidden = true;
        ok.onclick = null;
        $('#server-modal-cancel').onclick = null;
        resolve(answer);
      };
      ok.onclick = () => close(true);
      $('#server-modal-cancel').onclick = () => close(false);
    });
  }

  async confirmRestart(mode) {
    const label = mode ? this.state?.modes.find((x) => x.id === mode)?.label : null;
    const yes = await this.ask({
      title: label ? i18n.t('Restart Ninaivu in {mode} mode?', { mode: label }) : i18n.t('Restart Ninaivu?'),
      text: i18n.t('Everyone using Ninaivu is disconnected for a minute or so. Files being copied or indexed are finished first, which can take a while on a slow disk. This page reconnects by itself.'),
      confirm: label ? i18n.t('Restart in {mode} mode', { mode: label }) : i18n.t('Restart'),
    });
    if (!yes) return;
    try {
      const reply = await api.restart(mode);
      this.oldPid = reply.pid;
      this.phase = 'restarting';
      this.waitingSince = Date.now();
      if (mode) this.selectedMode = mode;
      this.renderWaiting(i18n.t('Ninaivu is finishing active work before it restarts…'));
      this.schedule(3000);
    } catch (error) {
      this.toast(i18n.t('Could not restart: {reason}', { reason: error.message }), true);
    }
  }

  async confirmStop() {
    const yes = await this.ask({
      title: i18n.t('Stop Ninaivu?'),
      text: i18n.t('The family app and this console both go offline. There is no Start button here to bring them back: start Ninaivu again from the Ninaivu tray or start.cmd on the computer it runs on.'),
      confirm: i18n.t('Stop Ninaivu'),
      danger: true,
    });
    if (!yes) return;
    try {
      await api.stop();
      this.phase = 'stopped';
      this.stopTimer();
      this.renderWaiting(i18n.t('Ninaivu is stopping. Start it again from the Ninaivu tray or start.cmd on the computer it runs on.'));
    } catch (error) {
      this.toast(i18n.t('Could not stop: {reason}', { reason: error.message }), true);
    }
  }
}
