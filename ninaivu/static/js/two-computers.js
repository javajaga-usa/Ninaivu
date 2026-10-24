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
    $('#tc-family-copy').onchange = (event) => this.act(
      () => json('/api/admin/mugil/role', {
        method: 'POST', body: { role: 'primary', family_copy: event.target.checked },
      }),
      i18n.t('Saved'));
    $('#tc-free-on').onchange = (event) => this.act(
      () => json('/api/admin/mugil/role', {
        method: 'POST', body: { role: 'primary', free_space: event.target.checked },
      }),
      i18n.t('Saved'));
    $('#tc-free-go').onclick = () => {
      const ready = this.plan?.ready || {};
      if (!window.confirm(i18n.t('Let {files} originals ({size}) go from this computer? Each is read again here and on the Secondary first. Afterwards they are on the Secondary and off site only, and are sent for when somebody opens one.', {
        files: count(ready.files), size: size(ready.bytes) }))) return;
      this.act(() => json('/api/admin/mugil/free', { method: 'POST', body: { confirm: true } })
        .then(() => json('/api/admin/mugil')), i18n.t('Freeing space'));
    };
    $('#tc-free-stop').onclick = () => this.act(
      () => json('/api/admin/mugil/free/stop', { method: 'POST' }).then(() => json('/api/admin/mugil')));
    $('#tc-bring-back').onclick = () => {
      if (!window.confirm(i18n.t('Bring every original back from the Secondary? This computer needs room for them.'))) return;
      this.act(() => json('/api/admin/mugil/bring-back', { method: 'POST', body: { all: true } })
        .then((answer) => {
          this.toast(i18n.t('{back} brought back; {missed} could not be.', {
            back: count(answer.back), missed: count(answer.missed) }), Boolean(answer.missed));
          return json('/api/admin/mugil');
        }));
    };
    $('#tc-rebuild-lib').onchange = () => {
      const lib = (this.rebuild?.libraries || []).find((l) => l.id === $('#tc-rebuild-lib').value);
      if (lib && lib.was_at) $('#tc-rebuild-folder').value = lib.was_at;
    };
    $('#tc-rebuild-go').onclick = () => {
      const folder = $('#tc-rebuild-folder').value.trim();
      if (!window.confirm(i18n.t('Copy this library\'s originals from the Secondary into {folder}? Files already there are left as they are.', { folder }))) return;
      this.act(() => json('/api/admin/mugil/rebuild', {
        method: 'POST', body: { library: $('#tc-rebuild-lib').value, folder },
      }).then(() => { this.rebuildAt = 0; return json('/api/admin/mugil'); }),
      i18n.t('Rebuilding the library'));
    };
    $('#tc-rebuild-stop').onclick = () => this.act(
      () => json('/api/admin/mugil/rebuild/stop', { method: 'POST' })
        .then(() => { this.rebuildAt = 0; return json('/api/admin/mugil'); }));
    $('#tc-wake-now').onclick = () => this.act(
      () => json('/api/admin/mugil/wake', { method: 'POST' }).then(() => {
        this.checklistAt = 0;
        return json('/api/admin/mugil');
      }),
      i18n.t('The wake was sent. The Primary should answer in a minute.'));
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
    if (this.visible && this.status?.paired) await this.machines();
    else $('#tc-machines-row').hidden = true;
    if (this.visible && this.status && this.status.role !== 'single') await this.checklist();
    else $('#tc-checklist-row').hidden = true;
    if (this.visible && this.status?.paired && this.status.role === 'primary') {
      await this.optimize();
      await this.rebuilding();
    } else {
      $('#tc-optimize').hidden = true; $('#tc-free').hidden = true; $('#tc-rebuild').hidden = true;
    }
    this.schedule();
  }

  /** The Primary: what Optimize Mac storage could free, read only. */
  async optimize() {
    const now = Date.now();
    if (this.optimizeAt && now - this.optimizeAt < 300000) return;
    this.optimizeAt = now;
    let plan;
    try {
      plan = await json('/api/admin/mugil/optimize');
    } catch {
      return;
    }
    this.plan = plan;
    const staying = plan.staying || {};
    const kept = (key) => staying[key] || { files: 0, bytes: 0 };
    const waiting = ['not_on_secondary', 'not_off_site', 'held', 'no_display_copy']
      .reduce((sum, key) => sum + kept(key).bytes, 0);
    const line = $('#tc-optimize');
    line.hidden = false;
    line.textContent = i18n.t('Optimize Mac storage: {ready} ({files} originals) could live on the Secondary only. {waiting} more once they are on the Secondary and off site for {days} days. {inuse} stay here in full: the last year and favourites. Nothing is freed until you say so.', {
      ready: size(plan.ready?.bytes), files: count(plan.ready?.files), waiting: size(waiting),
      days: plan.hold_days, inuse: size(kept('in_use').bytes + kept('favourite').bytes),
    });
  }

  /** The Mugil checklist, read from what is set up (mugil/checklist.py), once a minute. */
  async checklist() {
    const now = Date.now();
    if (this.checklistAt && now - this.checklistAt < 60000) return;
    this.checklistAt = now;
    let answer;
    try {
      answer = await json('/api/admin/mugil/checklist');
    } catch {
      return;
    }
    $('#tc-checklist-row').hidden = false;
    $('#tc-checklist-says').textContent = i18n.t('{done} of {of} in place.', {
      done: count(answer.done), of: count(answer.of) });
    const marks = { done: '✓', todo: '○', problem: '!', off: '–' };
    $('#tc-checklist').replaceChildren(...(answer.items || []).map((item) => {
      const [title, line] = this.checklistWords(item);
      const row = document.createElement('li');
      row.dataset.status = item.status;
      const mark = document.createElement('span');
      mark.className = 'tc-mark';
      mark.textContent = marks[item.status] || '?';
      const name = document.createElement('span');
      name.textContent = title;
      row.append(mark, name);
      if (line) {
        const says = document.createElement('span');
        says.className = 'tc-line';
        says.textContent = line;
        row.append(says);
      }
      return row;
    }));
    $('#tc-wake-row').hidden = !(answer.role === 'secondary'
      && (answer.items || []).some((i) => i.id === 'wake' && i.status !== 'todo'));
  }

  /** One checklist item's title, and its line for how it stands. */
  checklistWords(item) {
    const done = item.status === 'done';
    const free = () => i18n.t('{free} free of {total}.', {
      free: size(item.free_bytes), total: size(item.total_bytes) });
    switch (item.id) {
      case 'paired':
        return [i18n.t('The two computers are paired'),
          done ? '' : i18n.t('Pair them above.')];
      case 'secondary_seen':
        return [i18n.t('The Secondary is answering'), item.seen_at
          ? i18n.t('Last answered {when}.', { when: when(item.seen_at) })
          : i18n.t('It has not answered yet. Press Send now.')];
      case 'primary_seen':
        return [i18n.t('The Primary is sending'), item.seen_at
          ? i18n.t('Last heard from {when}.', { when: when(item.seen_at) })
          : i18n.t('The Primary has not sent anything yet.')];
      case 'same_version':
        return [i18n.t('Both run the same version'), item.theirs
          ? i18n.t('Here {mine}, there {theirs}.', { mine: item.mine, theirs: item.theirs })
          : i18n.t('Not known until the Secondary answers.')];
      case 'originals':
        return [i18n.t('Every original is on the Secondary'),
          i18n.t('{confirmed} of {total} confirmed.', {
            confirmed: count(item.confirmed), total: count(item.total) })];
      case 'secondary_disk':
      case 'originals_disk':
        return [i18n.t('The originals disk is healthy and has room'),
          item.unreachable ? i18n.t('The originals folder cannot be reached. Is the disk plugged in?')
            : item.smart === false ? i18n.t('The disk reports a failing health check.')
              : item.total_bytes ? free() : ''];
      case 'offsite':
        return [i18n.t('A copy is kept outside the house'), done ? ''
          : item.status === 'problem' ? i18n.t('The off-site copy has a problem. See its page.')
            : i18n.t('Set up the off-site copy (Backblaze B2 or another).')];
      case 'display_copies':
        return [i18n.t('Display copies are made'),
          i18n.t('{made} made, {owed} still to make.', { made: count(item.made), owed: count(item.owed) })];
      case 'catchup':
        return [i18n.t('The daily catch-up is on'),
          done ? '' : i18n.t('Turn it on under Advanced settings, so the heavy work runs at night.')];
      case 'wake':
        if (item.status === 'off') {
          return [i18n.t('The Secondary wakes the Primary at night'),
            i18n.t('Not needed while the daily catch-up is off.')];
        }
        if (item.network_wake === false) {
          return [i18n.t('The Secondary wakes the Primary at night'),
            i18n.t('Turn on Wake for network access in System Settings, Energy.')];
        }
        return [i18n.t('The Secondary wakes the Primary at night'), done
          ? i18n.t('At {at} each night.', { at: item.at })
          : i18n.t('Set after the next run sends the originals.')];
      case 'sign_in':
        return [i18n.t('Ninaivu starts when the computer signs in'), item.status === 'off'
          ? i18n.t('On Linux, the system service starts it.')
          : done ? '' : i18n.t('Tick Start Ninaivu when I sign in in the Control Panel.')];
      case 'originals_folder':
        return [i18n.t('The originals folder is ready'),
          done ? '' : i18n.t('Choose the folder on the big disk above.')];
      default:
        return [item.id, ''];
    }
  }

  /** Paired: the two computers side by side, asked at most twice a minute. */
  async machines() {
    const now = Date.now();
    if (this.machinesAt && now - this.machinesAt < 30000) return;
    this.machinesAt = now;
    let answer;
    try {
      answer = await json('/api/admin/mugil/machines');
    } catch {
      return;
    }
    const list = $('#tc-machines');
    list.textContent = '';
    const row = (machine, extra) => {
      const item = document.createElement('li');
      const head = document.createElement('strong');
      head.textContent = i18n.t('{name}, the {part}: Ninaivu {version} on {system}', {
        name: machine.name || machine.host || '—',
        part: (ROLE_STATE[machine.role] || ROLE_STATE.single)(),
        version: machine.version || '?', system: machine.system || '?',
      });
      item.append(head);
      for (const [text, away] of extra) {
        if (!text) continue;
        const line = document.createElement('span');
        line.className = away ? 'hint tc-away' : 'hint subtle';
        line.textContent = text;
        item.append(line);
      }
      list.append(item);
    };
    const mine = answer.this || {};
    row(mine, [[mine.state_disk ? i18n.t('{free} free of {total}.', {
      free: size(mine.state_disk.free_bytes), total: size(mine.state_disk.total_bytes) }) : '', false]]);
    const other = answer.other || {};
    const theirs = { ...(other.machine || {}) };
    if (!theirs.version && other.version) theirs.version = other.version;
    const health = other.health || {};
    const extra = [];
    if (answer.online === false) {
      extra.push([i18n.t('Not answering now. Last seen {when}.', { when: when(other.seen_at) || '—' }), true]);
    } else if (other.seen_at) {
      extra.push([i18n.t('Last seen {when}.', { when: when(other.seen_at) }), false]);
    }
    if (health.free_bytes !== undefined) {
      extra.push([i18n.t('Originals disk: {free} free of {total}, {files} originals kept.', {
        free: size(health.free_bytes), total: size(health.total_bytes), files: count(health.files),
      }), false]);
    }
    if (health.last_kept) extra.push([i18n.t('Last original kept {when}.', { when: when(health.last_kept) }), false]);
    if (health.smart) {
      extra.push(health.smart.passed
        ? [i18n.t('The disk says it is healthy.'), false]
        : [i18n.t('The disk reports a problem. Copy the originals to another disk soon.'), true]);
    } else if (theirs.role === 'secondary' && answer.online !== false) {
      extra.push([i18n.t('The disk\'s own health cannot be read on that computer.'), false]);
    }
    if (theirs.name || theirs.version) row(theirs, extra);
    $('#tc-machines-row').hidden = false;
    const version = $('#tc-version');
    version.hidden = answer.same_version !== false;
    version.textContent = answer.same_version === false
      ? i18n.t('The two computers run different versions of Ninaivu ({mine} and {theirs}). Update the older one so both understand the same things.', {
        mine: mine.version, theirs: theirs.version })
      : '';
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
    const busy = Boolean(status.sending?.running) || Boolean(status.freeing?.running)
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
    if (status.handover?.made_at) {
      $('#tc-keeping').textContent += ` ${i18n.t('It holds the family app\'s copy from {when}.', { when: when(status.handover.made_at) })}`;
    }
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
    $('#tc-family-copy').checked = Boolean(status.family_copy);
    const handover = status.handover || {};
    if (status.family_copy && handover.at) {
      lines.push(i18n.t('The family app\'s copy was last sent {when}.', { when: when(handover.at) }));
    } else if (status.family_copy && handover.error) {
      lines.push(i18n.t('The family app\'s copy could not be sent: {reason}', { reason: i18n.fromServer(handover.error) }));
    }
    const display = status.display || {};
    if (display.made || display.owed) {
      lines.push(i18n.t('Display copies: {made} made, {size} in place of {original}; {owed} still to make.', {
        made: count(display.made), size: size(display.bytes),
        original: size(display.original_bytes), owed: count(display.owed),
      }));
    }
    $('#tc-progress').textContent = lines.join(' ');
    this.renderFreeing(status);
  }

  /** The Primary: freeing space, off until it is turned on (mugil/free.py). */
  renderFreeing(status) {
    const block = $('#tc-free');
    if (status.role !== 'primary' || !status.paired) { block.hidden = true; return; }
    block.hidden = false;
    const freeing = status.freeing || {};
    const gone = freeing.let_go || {};
    $('#tc-free-on').checked = Boolean(status.free_space);
    $('#tc-free-actions').hidden = !status.free_space && !gone.files;
    $('#tc-free-go').hidden = !status.free_space || Boolean(freeing.running);
    $('#tc-free-stop').hidden = !freeing.running;
    $('#tc-bring-back').hidden = !gone.files || Boolean(freeing.running);
    const lines = [];
    if (gone.files) {
      lines.push(i18n.t('{files} originals ({size}) are on the Secondary only.', {
        files: count(gone.files), size: size(gone.bytes) }));
    }
    if (freeing.running && freeing.current) lines.push(i18n.t('Now: {path}', { path: freeing.current }));
    if (freeing.waiting) lines.push(i18n.t('Waiting: {reason}', { reason: i18n.fromServer(freeing.waiting) }));
    if (freeing.started) {
      const kept = Object.values(freeing.kept || {}).reduce((a, b) => a + b, 0);
      lines.push(i18n.t('This time: {freed} let go ({size}); {kept} kept because a check did not pass.', {
        freed: count(freeing.freed), size: size(freeing.freed_bytes), kept: count(kept) }));
    }
    if (freeing.error) lines.push(i18n.fromServer(freeing.error));
    $('#tc-free-says').textContent = lines.join(' ');
  }

  /** The Primary: a library copied back from the Secondary (mugil/rebuild.py). */
  async rebuilding() {
    const now = Date.now();
    if (!this.rebuild?.running && this.rebuildAt && now - this.rebuildAt < 300000) return;
    this.rebuildAt = now;
    try {
      this.rebuild = await json('/api/admin/mugil/rebuild');
    } catch {
      return;
    }
    const answer = this.rebuild;
    const block = $('#tc-rebuild');
    block.hidden = !answer.running && !(answer.libraries || []).length;
    const select = $('#tc-rebuild-lib');
    const chosen = select.value;
    select.replaceChildren(...(answer.libraries || []).map((lib) => {
      const option = document.createElement('option');
      option.value = lib.id;
      option.textContent = i18n.t('{name}: {files} originals, {size}', {
        name: lib.name || lib.id.slice(0, 8), files: count(lib.files), size: size(lib.bytes) });
      return option;
    }));
    if (chosen) select.value = chosen;
    if (!$('#tc-rebuild-folder').value) select.onchange();
    $('#tc-rebuild-go').hidden = Boolean(answer.running);
    $('#tc-rebuild-stop').hidden = !answer.running;
    const lines = [];
    if (answer.started) {
      lines.push(i18n.t('{copied} copied ({size}); {already} were there already; {left} left as they were; {bad} did not arrive whole.', {
        copied: count(answer.copied), size: size(answer.copied_bytes), already: count(answer.already),
        left: count(answer.left_alone), bad: count(answer.not_same) }));
    }
    if (answer.running && answer.current) lines.push(i18n.t('Now: {path}', { path: answer.current }));
    if (answer.waiting) lines.push(i18n.t('Waiting: {reason}', { reason: i18n.fromServer(answer.waiting) }));
    if (answer.complete) lines.push(i18n.t('Every original is back. The library is being indexed.'));
    if (answer.error) lines.push(i18n.fromServer(answer.error));
    $('#tc-rebuild-says').textContent = lines.join(' ');
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
