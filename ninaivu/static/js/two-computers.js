/**
 * Mugil → Two computers: which part this computer plays, and pairing it.
 *
 * A single computer sees one choice and nothing else. The Secondary chooses
 * the folder on its big disk and waits for its Primary; the Primary looks for
 * it on the house network and the tailnet by itself, lists what it finds with
 * the number both screens show, and pairs with one tap (mugil/meeting.py).
 * A code typed with an address is still there for when it cannot be found.
 * The server does the checking (api/mugil_api.py): this only says what it
 * answers.
 *
 * Owned by the Mugil panel, shown and hidden with it; it polls only while it
 * is on screen: often while it is looking or sending, rarely otherwise.
 */

import { reportUnauthorized } from './api.js';
import * as i18n from './i18n.js';

const $ = (sel) => document.querySelector(sel);

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
  const data = text ? JSON.parse(text) : null;
  if (!response.ok) {
    if (response.status === 401) reportUnauthorized(url);
    throw Object.assign(new Error(i18n.fromServer(data, response.statusText)),
      { status: response.status, data });
  }
  return data;
}

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

const when = (seconds) => (seconds
  ? new Date(seconds * 1000).toLocaleString(i18n.locale(),
    { dateStyle: 'medium', timeStyle: 'short' })
  : '');

const ROLE_STATE = {
  single: () => i18n.t('One computer'),
  primary: () => i18n.t('Primary'),
  secondary: () => i18n.t('Secondary'),
};

export class TwoComputers {
  constructor({ toast }) {
    this.toast = toast || (() => {});
    this.timer = null;
    this.visible = false;
    this.status = null;
    this.found = null;
    this.looking = false;
    i18n.onChange(() => { if (this.status) this.render(this.status); });
  }

  wire() {
    if (!$('#tc-block')) return;
    $('#tc-save-role').onclick = () => this.act(
      () => json('/api/admin/mugil/role', { method: 'POST', body: { role: $('#tc-role').value } }),
      i18n.t('Saved'));
    $('#tc-save-folder').onclick = () => this.act(
      () => json('/api/admin/mugil/role', {
        method: 'POST', body: { role: 'secondary', originals_dir: $('#tc-folder').value.trim() },
      }),
      i18n.t('The originals will be kept in that folder'));
    $('#tc-wait').onclick = () => this.act(
      () => json('/api/admin/mugil/welcome', { method: 'POST' }),
      i18n.t('Waiting for the Primary'));
    $('#tc-code').onclick = () => this.showCode();
    $('#tc-pair').onclick = () => this.act(
      () => json('/api/admin/mugil/pair', {
        method: 'POST',
        body: { url: $('#tc-url').value.trim(), code: $('#tc-pair-code').value.trim() },
      }),
      i18n.t('The two computers are paired'));
    $('#tc-every').onchange = (event) => this.act(
      () => json('/api/admin/mugil/role', {
        method: 'POST', body: { role: 'primary', copy_every_hours: Number(event.target.value) },
      }),
      i18n.t('Saved'));
    $('#tc-send').onclick = () => this.act(
      () => json('/api/admin/mugil/send', { method: 'POST' }),
      i18n.t('Sending the originals to the Secondary'));
    $('#tc-stop').onclick = () => this.act(() => json('/api/admin/mugil/stop', { method: 'POST' }));
    $('#tc-unpair').onclick = () => {
      if (!window.confirm(i18n.t('Unpair the two computers? They can find each other and pair again afterwards.'))) return;
      this.act(() => json('/api/admin/mugil/unpair', { method: 'POST' }),
        i18n.t('Unpaired. Unpair the other computer too.'));
    };
  }

  show() { this.visible = true; this.refresh(); }

  hide() { this.visible = false; clearTimeout(this.timer); this.timer = null; }

  async act(call, done) {
    const buttons = [...document.querySelectorAll('#tc-block button')];
    buttons.forEach((b) => { b.disabled = true; });
    try {
      this.render(await call());
      if (done) this.toast(done);
    } catch (exc) {
      this.toast(exc.message, true);
    } finally {
      buttons.forEach((b) => { b.disabled = false; });
    }
    this.render(this.status);
    this.schedule();
  }

  async showCode() {
    try {
      const answer = await json('/api/admin/mugil/code', { method: 'POST' });
      const out = $('#tc-code-out');
      out.textContent = answer.code;
      const note = document.createElement('small');
      note.textContent = i18n.t('Type this on the Primary within {minutes} minutes. It works once.',
        { minutes: Math.round((answer.expires_in || 600) / 60) });
      out.append(note);
      out.hidden = false;
      clearTimeout(this.codeTimer);
      this.codeTimer = setTimeout(() => { out.hidden = true; }, (answer.expires_in || 600) * 1000);
    } catch (exc) {
      this.toast(exc.message, true);
    }
  }

  async refresh() {
    clearTimeout(this.timer);
    try {
      this.render(await json('/api/admin/mugil'));
    } catch (exc) {
      if (exc.status === 404) { $('#tc-block').hidden = true; return; }
      if (exc.status !== 401 && exc.status !== 423) this.toast(exc.message, true);
    }
    if (this.visible && this.status?.role === 'primary' && !this.status.paired) await this.look();
    this.schedule();
  }

  /** The Primary, not paired: what answers on the network, introduced to. */
  async look() {
    if (this.looking) return;
    this.looking = true;
    try {
      this.found = (await json('/api/admin/mugil/look', { method: 'POST' })).found || [];
    } catch (exc) {
      if (exc.status !== 409) this.toast(exc.message, true);
    } finally {
      this.looking = false;
    }
    if (this.status) this.renderFound();
  }

  async pairWith(item) {
    await this.act(() => json('/api/admin/mugil/pair', { method: 'POST', body: { found: item.id } }),
      i18n.t('The two computers are paired'));
    this.found = null;
  }

  schedule() {
    clearTimeout(this.timer);
    if (!this.visible || !this.status) return;
    const status = this.status;
    const busy = Boolean(status.sending?.running)
      || (status.role === 'primary' && !status.paired)
      || (status.role === 'secondary' && status.welcome?.waiting);
    this.timer = setTimeout(() => this.refresh(), busy ? 4000 : 60000);
  }

  render(status) {
    if (!status) return;
    this.status = status;
    const part = status.role || 'single';
    $('#tc-block').hidden = false;
    $('#tc-role').value = part;
    $('#tc-state').textContent = (ROLE_STATE[part] || ROLE_STATE.single)();
    $('#tc-secondary').hidden = part !== 'secondary';
    $('#tc-primary').hidden = part !== 'primary';
    $('#tc-network').hidden = part === 'single' || status.on_the_network !== false;

    const pairing = status.pairing || {};
    $('#tc-paired').hidden = !status.paired;
    $('#tc-paired-with').textContent = !status.paired ? ''
      : pairing.number
        ? i18n.t('Paired with {name} since {when}. Both screens showed {number}.', {
          name: pairing.partner_name || pairing.partner_url || pairing.partner_address || '—',
          when: when(pairing.paired_at), number: pairing.number,
        })
        : i18n.t('Paired with {name} since {when}.', {
          name: pairing.partner_name || pairing.partner_url || pairing.partner_address || '—',
          when: when(pairing.paired_at),
        });
    // A computer's part is changed only while it is not paired.
    $('#tc-role').disabled = Boolean(status.paired);
    $('#tc-save-role').disabled = Boolean(status.paired);

    if (part === 'secondary') this.renderSecondary(status);
    if (part === 'primary') this.renderPrimary(status);
  }

  renderSecondary(status) {
    const folder = $('#tc-folder');
    if (document.activeElement !== folder) folder.value = status.originals_dir || '';
    const keeping = status.keeping || {};
    $('#tc-keeping').textContent = !status.originals_dir
      ? i18n.t('Choose the folder on the big disk the originals are kept in.')
      : i18n.t('{files} originals kept, {size}; {free} free on that disk.', {
        files: count(keeping.files), size: size(keeping.bytes),
        free: keeping.free_bytes == null ? '—' : size(keeping.free_bytes),
      });
    const unpaired = !status.paired && Boolean(status.originals_dir);
    $('#tc-waiting-row').hidden = !unpaired;
    $('#tc-code-row').hidden = !unpaired;
    if (!unpaired) return;
    const welcome = status.welcome || {};
    $('#tc-waiting').textContent = welcome.waiting
      ? i18n.t('Waiting for the Primary until {when}. Choose Primary on the other computer’s console: it finds this one by itself.',
        { when: when(welcome.until) })
      : i18n.t('Not waiting for a Primary now.');
    $('#tc-wait').hidden = Boolean(welcome.waiting);
    const list = $('#tc-introduced');
    list.replaceChildren(...(welcome.introduced || []).map((item) => {
      const row = document.createElement('li');
      const name = document.createElement('span');
      name.className = 'tc-name';
      name.textContent = item.name || item.address;
      const number = document.createElement('span');
      number.className = 'tc-number';
      number.textContent = item.number;
      const where = document.createElement('span');
      where.className = 'tc-where';
      where.textContent = i18n.t('found this computer. If its screen shows the same number, tap Pair there.');
      row.append(name, number, where);
      return row;
    }));
  }

  renderPrimary(status) {
    $('#tc-look-row').hidden = Boolean(status.paired);
    $('#tc-send-row').hidden = !status.paired;
    if (!status.paired) this.renderFound();
    const every = String(Math.round(status.copy_every_hours || 0));
    const select = $('#tc-every');
    if ([...select.options].some((o) => o.value === every)) select.value = every;

    const sending = status.sending || {};
    const running = Boolean(sending.running);
    $('#tc-send').hidden = running;
    $('#tc-stop').hidden = !running;

    const lines = [];
    const progress = sending.progress || {};
    if (running) {
      lines.push(i18n.t('Sending: {done} of {files} files looked at, {sent} sent ({size}), {already} already there.', {
        done: count(progress.done), files: count(progress.files), sent: count(progress.sent),
        size: size(progress.bytes), already: count(progress.already),
      }));
      if (progress.waiting) lines.push(i18n.t('Waiting: {reason}', { reason: progress.waiting }));
      if (progress.secondary_away) {
        lines.push(i18n.t('The Secondary has not answered since {when}. Sending carries on by itself when it is back.', { when: when(progress.secondary_away) }));
      }
    } else if (sending.last && sending.last.finished) {
      const last = sending.last;
      lines.push(i18n.t('Last run {when}: {sent} sent ({size}), {already} already there.', {
        when: when(last.finished), sent: count(last.sent), size: size(last.bytes),
        already: count(last.already),
      }));
      if (last.changed) lines.push(i18n.t('{count} changed while being sent; they go next time.', { count: count(last.changed) }));
      if (last.failed) lines.push(i18n.t('{count} could not be sent; the log says why.', { count: count(last.failed) }));
      if (last.error) lines.push(i18n.t('It stopped: {reason}', { reason: last.error }));
    }
    const confirmed = Object.values(status.confirmed || {});
    if (confirmed.length) {
      const files = confirmed.reduce((sum, c) => sum + (c.files || 0), 0);
      const bytes = confirmed.reduce((sum, c) => sum + (c.bytes || 0), 0);
      lines.push(i18n.t('The Secondary has confirmed {files} originals, {size}.', {
        files: count(files), size: size(bytes),
      }));
    }
    $('#tc-progress').textContent = lines.join(' ');
  }

  renderFound() {
    const found = this.found;
    const list = $('#tc-found');
    $('#tc-looking').textContent = found == null || !found.length
      ? i18n.t('Looking for the Secondary on the house network and on Tailscale…')
      : i18n.t('Found on the network. Check that the Secondary’s screen shows the same number, then tap Pair.');
    list.replaceChildren(...(found || []).map((item) => {
      const row = document.createElement('li');
      const name = document.createElement('span');
      name.className = 'tc-name';
      name.textContent = item.name || item.address;
      row.append(name);
      if (item.waiting && item.number) {
        const number = document.createElement('span');
        number.className = 'tc-number';
        number.textContent = item.number;
        const button = document.createElement('button');
        button.className = 'btn primary small';
        button.type = 'button';
        button.textContent = i18n.t('Pair');
        button.onclick = () => this.pairWith(item);
        row.append(number, button);
      } else {
        const where = document.createElement('span');
        where.className = 'tc-where';
        where.textContent = item.unreachable
          ? i18n.t('answers, but its family app cannot be reached. Turn on network access on its Server page.')
          : i18n.t('is not waiting. On its console, tap Wait for the Primary again.');
        row.append(where);
      }
      return row;
    }));
  }
}
