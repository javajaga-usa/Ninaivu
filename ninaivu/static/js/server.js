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
  if (m < 1) return 'under a minute';
  const d = Math.floor(m / 1440);
  const h = Math.floor((m % 1440) / 60);
  const mm = m % 60;
  if (d) return `${d} d ${h} h`;
  if (h) return `${h} h ${mm} min`;
  return `${mm} min`;
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
          this.renderWaiting('Ninaivu is finishing active work before it restarts…');
          this.schedule();
          return;
        } else {
          this.phase = 'running';
          this.cpu = [];
          this.logPosition = null;
          this.toast('Ninaivu has restarted.');
        }
      }
      this.state = state;
      this.render(state);
      if (this.visible) await this.pullLog();
    } catch (error) {
      if (this.phase === 'restarting') {
        this.renderWaiting('Ninaivu is starting again…');
        if (Date.now() - this.waitingSince > BACK_TIMEOUT_MS) {
          this.phase = 'running';
          this.renderWaiting('Ninaivu has not come back. The restart log is .ninaivu-control\\restart.log on the Ninaivu computer.');
          this.toast('Ninaivu has not come back after a restart.', true);
          return;
        }
      } else if (error.status !== 401) {
        $('#sv-state').textContent = 'Not answering';
        $('#sv-state').className = 'sv-state bad';
        $('#sv-summary').textContent = `Could not read the server: ${error.message}`;
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
      pill.textContent = 'Restarting'; pill.className = 'sv-state busy';
    } else {
      pill.textContent = 'Running'; pill.className = 'sv-state good';
    }
    const modeLabel = state.modes.find((x) => x.id === state.mode)?.label || state.mode;
    $('#sv-summary').textContent = `Up ${uptime(since)} · ${modeLabel} mode`
      + (state.mode_chosen ? '' : ' (default — no mode was chosen when it started)');

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
    row('Ports', `${e.port ?? '—'} family · ${e.admin_port ?? '—'} console`);
    const b = state.budget || {};
    row('Threads', `${b.workers} scan workers · ${b.compute_threads} AI/video · ${b.server_threads} requests`);
    row('Process', `PID ${state.pid} · Python ${state.python}`);

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
    if (m) this.renderMetrics(state, m);
    const cert = $('#sv-cert');
    if (cert) cert.href = this.familyUrl('/cert');
  }

  renderWaiting(message) {
    const pill = $('#sv-state');
    pill.textContent = this.phase === 'stopped' ? 'Stopped' : 'Restarting';
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
    $('#sv-net-label').textContent = n.enabled ? 'On' : 'Off';

    let summary;
    let note = '';
    if (n.enabled && n.family_on_network) {
      summary = 'Every device on your home network can open Ninaivu: phones, tablets and other computers.';
    } else if (!n.enabled && !n.family_on_network) {
      summary = 'Only this computer can open Ninaivu. Other devices on the network cannot reach it.';
    } else if (n.enabled) {
      summary = 'Switched on, but Ninaivu is still only on this computer.';
      note = 'It has not restarted since the switch was turned on, or it was started with '
        + '--local-only. Restart Ninaivu to put it on the network.';
    } else {
      summary = 'Switched off, but Ninaivu is still on the network.';
      note = 'The change takes effect when Ninaivu restarts.';
    }
    if (n.enabled && n.family_on_network && !n.console_on_network) {
      note = 'The admin console is kept to this computer (--admin-host), so only the family app is on the network.';
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
    links('Family app', e.family_urls);
    links('Admin console', e.admin_urls);
    // Tailscale's addresses work from anywhere, on devices signed in to the
    // tailnet: the ones to give the household for when they are away.
    links('Family app, away from home (Tailscale)', e.tailnet_family_urls);
    links('Admin console, away from home (Tailscale)', e.tailnet_admin_urls);
  }

  async confirmNetwork(on) {
    const n = this.state?.network || {};
    const remote = !n.request_is_local;
    const yes = await this.ask(on ? {
      title: 'Put Ninaivu on your network?',
      text: 'Ninaivu restarts, then answers every device on your home network — phones, '
        + 'tablets and other computers — by name and by this computer’s address. '
        + 'Everyone still signs in as before.',
      confirm: 'Turn on and restart',
    } : {
      title: 'Keep Ninaivu to this computer?',
      text: 'Ninaivu restarts, then answers only on this computer, at localhost. Phones, '
        + 'tablets and other computers lose access until this is turned back on.'
        + (remote ? ' You are using the console from another device, so this page stops '
          + 'working here, and turning it back on has to be done on the Ninaivu computer itself.' : ''),
      confirm: 'Turn off and restart',
      danger: true,
    });
    if (!yes) return;
    let reply;
    try {
      reply = await api.network(on);
    } catch (error) {
      this.toast(`Could not change network access: ${error.message}`, true);
      return;
    }
    if (!reply.applied) {
      this.toast(`Saved. It takes effect the next time Ninaivu starts${reply.error ? ` (${reply.error})` : ''}.`);
      this.tick();
      return;
    }
    this.oldPid = reply.pid;
    this.waitingSince = Date.now();
    if (!on && remote) {
      // Nothing to wait for from here: the new server will not answer this device.
      this.phase = 'stopped';
      this.stopTimer();
      this.renderWaiting('Ninaivu is restarting for its own computer only, so this page no '
        + 'longer reaches it. To put it back on the network, open the console on the '
        + `Ninaivu computer: https://localhost:${this.state?.endpoints?.admin_port ?? 3000}`);
      $('#sv-state').textContent = 'Local only';
      return;
    }
    this.phase = 'restarting';
    this.renderWaiting(on ? 'Ninaivu is restarting to join the network…'
      : 'Ninaivu is restarting for this computer only…');
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
      if (mode.id === state.mode) head.append(el('span', 'sv-mode-now', 'Running'));
      card.append(head, el('span', 'sv-mode-text', mode.description),
        el('span', 'sv-mode-budget',
          `${mode.workers} scan workers · ${mode.compute_threads} AI/video threads · ${mode.server_threads} requests`));
      box.append(card);
    });
    const apply = $('#sv-apply-mode');
    apply.disabled = !idle || !state.can_restart || this.selectedMode === state.mode;
    apply.textContent = this.selectedMode === state.mode ? 'Apply and restart'
      : `Restart in ${state.modes.find((x) => x.id === this.selectedMode)?.label || ''} mode`;
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
    card('Computer CPU', pct(m.cpu), `${m.cpus} logical cores, all applications`, m.cpu);
    card('Computer memory', pct(m.memory.percent),
      `${gb(m.memory.used)} of ${gb(m.memory.total)} in use`, m.memory.percent);
    const p = m.process;
    if (p) {
      card('Ninaivu', p.cpu == null ? '…' : `${p.cpu.toFixed(1)}%`,
        `${mb(p.memory)} · ${p.threads} threads${p.processes > 1 ? ` · ${p.processes} processes` : ''}`);
    }
    (m.disks || []).forEach((disk) => {
      const tone = disk.free < 20 * 1024 ** 3 ? 'warn' : '';
      const c = card(disk.role === 'library' ? 'Library disk free' : 'Install disk free', gb(disk.free),
        `${disk.drive} · ${Math.round(disk.percent)}% of ${gb(disk.total)} used`, null, tone);
      c.title = disk.path;
    });
    if (m.battery) {
      card('Battery', pct(m.battery.percent),
        m.battery.plugged == null ? 'Charging state unknown'
          : m.battery.plugged ? 'Connected to power' : 'Running on battery',
        null, !m.battery.plugged && m.battery.percent < 25 ? 'warn' : '');
    }
    if (m.power_watts != null) {
      card('Power draw', `${m.power_watts.toFixed(1)} W`, 'Whole computer, from the battery');
    }

    $('#sv-updated').textContent = `Updated ${new Date(m.at * 1000).toLocaleTimeString()}`;

    this.cpu.push({ machine: m.cpu ?? 0, ninaivu: p?.cpu ?? 0 });
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
      ? `CPU activity · last two minutes · now ${Math.round(last.machine)}% · peak ${Math.round(peak)}%`
      : 'CPU activity';
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
      where.textContent = 'No server log yet. Ninaivu writes one when it is started from the '
        + 'Ninaivu Control Panel or restarted from this page.';
      if (this.logPosition !== 0) log.textContent = '';
      this.logPosition = 0;
      return;
    }
    const modified = new Date(data.modified * 1000);
    where.textContent = `${data.path} · last written ${modified.toLocaleString()}`;
    if (data.reset) {
      log.textContent = data.truncated ? '[Showing the most recent part of the log]\n' : '';
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
    button.textContent = on ? 'Following' : 'Follow';
    button.setAttribute('aria-pressed', String(on));
    if (on && scroll) { const log = $('#sv-log'); log.scrollTop = log.scrollHeight; }
  }

  async copyLog() {
    const text = $('#sv-log').textContent;
    try {
      await navigator.clipboard.writeText(text);
      this.toast('Log copied.');
    } catch {
      // Clipboard needs a secure context; select it for a manual copy instead.
      const range = document.createRange();
      range.selectNodeContents($('#sv-log'));
      const selection = getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      this.toast('Selected the log — press Ctrl+C to copy it.');
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
    else if (!n) label.textContent = 'Not found';
    else if (this.matchIndex >= 0) label.textContent = `${this.matchIndex + 1} of ${n}${n >= 2000 ? '+' : ''}`;
    else label.textContent = `${n}${n >= 2000 ? '+' : ''} found · Enter for next`;

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
      title: label ? `Restart Ninaivu in ${label} mode?` : 'Restart Ninaivu?',
      text: 'Everyone using Ninaivu is disconnected for a minute or so. Files being '
        + 'copied or indexed are finished first, which can take a while on a slow '
        + 'disk. This page reconnects by itself.',
      confirm: label ? `Restart in ${label} mode` : 'Restart',
    });
    if (!yes) return;
    try {
      const reply = await api.restart(mode);
      this.oldPid = reply.pid;
      this.phase = 'restarting';
      this.waitingSince = Date.now();
      if (mode) this.selectedMode = mode;
      this.renderWaiting('Ninaivu is finishing active work before it restarts…');
      this.schedule(3000);
    } catch (error) {
      this.toast(`Could not restart: ${error.message}`, true);
    }
  }

  async confirmStop() {
    const yes = await this.ask({
      title: 'Stop Ninaivu?',
      text: 'The family app and this console both go offline. There is no Start '
        + 'button here to bring them back: start Ninaivu again from the Ninaivu '
        + 'Control Panel or start.bat on the computer it runs on.',
      confirm: 'Stop Ninaivu',
      danger: true,
    });
    if (!yes) return;
    try {
      await api.stop();
      this.phase = 'stopped';
      this.stopTimer();
      this.renderWaiting('Ninaivu is stopping. Start it again from the Ninaivu Control Panel '
        + 'or start.bat on the computer it runs on.');
    } catch (error) {
      this.toast(`Could not stop: ${error.message}`, true);
    }
  }
}
