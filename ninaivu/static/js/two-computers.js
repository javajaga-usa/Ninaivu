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
    // A 401 asking for the password again is not a session that ended.
    if (response.status === 401 && !data?.needs_password) reportUnauthorized(url);
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

/**
 * Choose the Secondary's originals folder. When this computer's own library
 * is inside it (a one-computer Ninaivu becoming the Secondary), the server
 * asks first: the library is kept as the originals only when confirmed.
 */
export async function keepAsOriginals(request, folder) {
  const body = { role: 'secondary', originals_dir: folder };
  try {
    return await request('/api/admin/mugil/role', { method: 'POST', body });
  } catch (exc) {
    const inside = exc.data?.library_inside;
    if (exc.status !== 409 || !inside?.length) throw exc;
    if (!window.confirm(i18n.t('Keep the library this computer shows ({paths}) as the originals, in {folder}? The photographs already there are checked once and marked as in sync, so the Primary sends only what is missing or different. This computer stops showing it as its library, and the Primary brings in what only it has. No file is moved, changed or removed. Turn on XMP sidecars first and let them be written, so the names, albums and stars go with the photographs.', { paths: inside.join(', '), folder: exc.data?.originals_dir || folder }))) throw exc;
    return request('/api/admin/mugil/role', {
      method: 'POST', body: { ...body, library_into_originals: true } });
  }
}

const when = (seconds) => (seconds
  ? new Date(seconds * 1000).toLocaleString(i18n.locale(),
    { dateStyle: 'medium', timeStyle: 'short' })
  : '');

/** What happens to a file, as the plan says it (mugil/topology.OUTCOMES). */
const outcomeWords = (outcome) => ({
  both: () => i18n.t('on both'),
  brought_in: () => i18n.t('brought in'),
  beside: () => i18n.t('kept beside'),
  sent: () => i18n.t('sent to the Secondary'),
  left_alone: () => i18n.t('left alone'),
  kept: () => i18n.t('kept where it is'),
  carried: () => i18n.t('to carry to the new Primary'),
}[outcome] || (() => outcome))();

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
      () => keepAsOriginals(json, $('#tc-folder').value.trim()),
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
    $('#tc-copies-go').onclick = () => {
      const spare = this.copies || {};
      if (!window.confirm(i18n.t('Move {files} spare copies ({size}) to the recycle bin? One copy of each stays where it is, and they can be put back from the bin.', {
        files: count(spare.spare_files), size: size(spare.spare_bytes) }))) return;
      const password = $('#tc-copies-password').value;
      $('#tc-copies-password').value = '';
      this.act(() => json('/api/admin/exact-copies/set-aside', { method: 'POST', body: { password } })
        .then((done) => {
          this.copiesAt = 0;
          this.toast(i18n.t('{count} spare copies are in the recycle bin.', { count: count(done.deleted) }));
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
    $('#tc-only-look').onclick = () => this.act(
      () => json('/api/admin/mugil/only-on-secondary/look', { method: 'POST' })
        .then(() => { this.onlyAt = 0; return json('/api/admin/mugil'); }),
      i18n.t('Looking on the Secondary'));
    $('#tc-only-bring').onclick = () => {
      const found = this.only?.found?.counts || {};
      const files = (found.only?.files || 0) + (found.other_version?.files || 0);
      const bytes = (found.only?.bytes || 0) + (found.other_version?.bytes || 0);
      if (!window.confirm(i18n.t('Bring {files} files ({size}) from the Secondary into this computer\'s library? Each is checked as it arrives; nothing here is replaced, and nothing on the Secondary changes.', {
        files: count(files), size: size(bytes) }))) return;
      this.act(() => json('/api/admin/mugil/only-on-secondary/bring', { method: 'POST' })
        .then(() => { this.onlyAt = 0; return json('/api/admin/mugil'); }),
      i18n.t('Bringing them in'));
    };
    $('#tc-only-stop').onclick = () => this.act(
      () => json('/api/admin/mugil/only-on-secondary/stop', { method: 'POST' })
        .then(() => { this.onlyAt = 0; return json('/api/admin/mugil'); }));
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
    $('#tc-update-send').onclick = () => this.sendUpdate();
    $('#tc-unpair').onclick = () => {
      if (!window.confirm(i18n.t('Unpair the two computers? They can find each other and pair again afterwards.'))) return;
      this.act(() => json('/api/admin/mugil/unpair', { method: 'POST' }),
        i18n.t('Unpaired. Unpair the other computer too.'));
    };
    $('#tc-census-go').onclick = () => this.topologyAct(
      () => json('/api/admin/mugil/census', { method: 'POST', body: {} }));
    $('#tc-plan-go').onclick = () => this.topologyAct(
      () => json('/api/admin/mugil/topology/plan', {
        method: 'POST', body: { transition: $('#tc-plan-which').value || 'pair' } }));
    $('#tc-prove-go').onclick = () => this.topologyAct(
      () => json('/api/admin/mugil/topology/prove', { method: 'POST' }));
    $('#tc-plan-filter').onchange = () => this.loadPlan(true);
    $('#tc-plan-more').onclick = () => this.loadPlan(false);
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
    if (this.visible && this.status) await this.safe();
    if (this.visible && this.status?.paired && this.status.role === 'primary') {
      await this.optimize();
      await this.exactCopies();
      await this.rebuilding();
      await this.onlyOnSecondary();
    } else {
      $('#tc-optimize').hidden = true; $('#tc-free').hidden = true; $('#tc-rebuild').hidden = true;
      $('#tc-only').hidden = true;
      $('#tc-copies').hidden = true;
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
        if (item.status === 'off') {
          return [i18n.t('A copy is kept outside the house'),
            i18n.t('Sent by the Primary, or turn it on here: this computer is always on.')];
        }
        return [i18n.t('A copy is kept outside the house'), item.status === 'problem'
          ? i18n.t('The off-site copy has a problem. See Copy out of the house.')
          : done ? (item.sent_by === 'secondary' ? i18n.t('Sent by the Secondary, which is always on.')
            : i18n.t('Sent by this computer.'))
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
    const primaryHere = this.status?.role === 'primary';
    $('#tc-update-row').hidden = !(primaryHere && answer.same_version === false);
    if (primaryHere) await this.updating();
  }

  /** The Primary: a newer Ninaivu on its way to the Secondary (mugil/update.py). */
  async updating() {
    let answer;
    try {
      answer = await json('/api/admin/mugil/update');
    } catch {
      return;
    }
    const says = $('#tc-update-says');
    const sending = answer.sending || {};
    const there = answer.secondary || {};
    let text = '';
    let away = false;
    if (sending.running) {
      text = i18n.t('Sending Ninaivu {to} to the Secondary: {sent} of {size}.', {
        to: sending.to, sent: size(sending.sent), size: size(sending.size) });
    } else if (sending.stage === 'failed') {
      text = i18n.t('The Secondary did not take the update: {why}', { why: sending.message });
      away = true;
    } else if (there.stage && (!there.finished_at || Date.now() / 1000 - there.finished_at < 7 * 86400)) {
      const to = there.to || '?';
      switch (there.stage) {
        case 'receiving':
        case 'unpacking':
          text = i18n.t('The Secondary is unpacking Ninaivu {to}.', { to }); break;
        case 'waiting':
          text = i18n.t('The Secondary has Ninaivu {to} ready, and switches to it when no originals are arriving.', { to }); break;
        case 'switching':
        case 'starting':
          text = i18n.t('The Secondary is switching to Ninaivu {to}. It is away for a minute or two.', { to }); break;
        case 'done':
          text = i18n.t('The Secondary runs Ninaivu {to}.', { to }); break;
        case 'went_back':
          text = i18n.t('Ninaivu {to} could not start on the Secondary, so it went back to the version it had, as it was. Its logs say why.', { to });
          away = true; break;
        default:
          text = i18n.t('The Secondary did not take the update: {why}', { why: there.message || there.stage });
          away = true;
      }
    }
    says.textContent = text;
    says.className = away ? 'hint tc-away' : 'hint subtle';
    says.hidden = !text;
  }

  /** Send the Linux download the person chose; the Primary sends it on. */
  async sendUpdate() {
    const file = $('#tc-update-file').files?.[0];
    if (!file) {
      this.toast(i18n.t('Choose the Linux portable download of Ninaivu first.'), true);
      return;
    }
    if (!window.confirm(i18n.t('Update the Secondary with {name}? It switches when no originals are arriving, and goes back by itself if the new version cannot start.', { name: file.name }))) return;
    const button = $('#tc-update-send');
    button.disabled = true;
    try {
      const response = await fetch('/api/admin/mugil/update', {
        method: 'POST',
        headers: { Accept: 'application/json', 'Content-Type': 'application/octet-stream',
          'X-Requested-With': 'fetch' },
        body: file,
      });
      const data = await response.json().catch(() => null);
      if (!response.ok) throw new Error(i18n.fromServer(data, response.statusText));
      $('#tc-update-file').value = '';
      this.toast(i18n.t('Sending the update to the Secondary.'));
    } catch (exc) {
      this.toast(exc.message, true);
    } finally {
      button.disabled = false;
    }
    this.machinesAt = 0;
    await this.machines();
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
      || Boolean(this.only?.found?.running) || Boolean(this.only?.bring?.running)
      || (status.role === 'primary' && !status.paired)
      || (status.role === 'secondary' && status.welcome?.waiting)
      || Boolean(this.topology?.running);
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
    // A copy made by hand (mugil/adopt.py): the steps say what to do next.
    const manual = status.copy_mode === 'manual' ? (status.manual || {}) : null;
    if (manual && manual.state !== 'ready') {
      $('#tc-keeping').textContent += ` ${manual.state === 'checking'
        ? i18n.t('Checking the copy made by hand; the Primary waits.')
        : i18n.t('The library is being copied here by hand; the Primary waits until you say it is finished (Set up step by step).')}`;
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
      if (progress.secondary_preparing) {
        lines.push(i18n.t('The Secondary is taking in a copy made by hand. Sending carries on by itself once it is checked.'));
      }
      if (progress.short_of_room) {
        lines.push(i18n.t('{needed} still to send and {free} free on the Secondary: what fits is sent.', {
          needed: size(progress.short_of_room.needed), free: size(progress.short_of_room.free) }));
      }
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

  /** The Primary: exact copies, best set aside before the first copy (storage/exact_copies.py). */
  async exactCopies() {
    const now = Date.now();
    if (this.copiesAt && now - this.copiesAt < 300000) return;
    this.copiesAt = now;
    try {
      this.copies = await json('/api/admin/exact-copies?limit=0');
    } catch {
      return;
    }
    const spare = this.copies;
    $('#tc-copies').hidden = !spare.spare_files;
    if (!spare.spare_files) return;
    const lines = [i18n.t('{files} files are exact copies of others here and take {size}. Set the spare copies aside before the first copy, so they are not kept twice on the Secondary and off site.', {
      files: count(spare.spare_files), size: size(spare.spare_bytes) })];
    if (spare.left_alone) {
      lines.push(i18n.t('{count} groups are left for you: more than one copy is in an album or a favourite, or one is part of a live photo.', {
        count: count(spare.left_alone) }));
    }
    $('#tc-copies-says').textContent = lines.join(' ');
  }

  /** The Primary: what only the Secondary has, and bringing it in (mugil/bring_in.py). */
  async onlyOnSecondary() {
    const now = Date.now();
    const busy = this.only?.found?.running || this.only?.bring?.running;
    if (!busy && this.onlyAt && now - this.onlyAt < 120000) return;
    this.onlyAt = now;
    try {
      this.only = await json('/api/admin/mugil/only-on-secondary');
    } catch {
      return;
    }
    const answer = this.only;
    const found = answer.found || {};
    const bring = answer.bring || {};
    const counts = found.counts || {};
    const of = (kind) => counts[kind] || { files: 0, bytes: 0 };
    $('#tc-only').hidden = false;
    const lines = [];
    if (answer.why_not) lines.push(i18n.fromServer(answer.why_not));
    if (answer.away) lines.push(i18n.t('The Secondary is not answering.'));
    if (found.running) {
      lines.push(i18n.t('Looking on the Secondary: {files} files gone through, {size} read.', {
        files: count(found.files_seen), size: size(found.bytes_read) }));
    } else if (found.error) {
      lines.push(i18n.fromServer(found.error));
    } else if (found.finished && !bring.running && !bring.started) {
      const wanted = of('only').files + of('other_version').files;
      lines.push(wanted
        ? i18n.t('To bring in: {files} files ({size}), {versions} of them its own versions of photographs this computer also has, to go beside them.', {
          files: count(wanted), size: size(of('only').bytes + of('other_version').bytes),
          versions: count(of('other_version').files) })
        : i18n.t('The Secondary has nothing this computer does not.'));
      lines.push(i18n.t('Not brought in: {same} this computer has under another name; {binned} in its recycle bin; {stray} in folders that are no library here (move them into a library folder on the Secondary, then look again).', {
        same: count(of('duplicate').files), binned: count(of('deleted_folder').files),
        stray: count(of('not_a_library').files) }));
    }
    if (bring.started) {
      lines.push(i18n.t('{copied} brought in ({size}); {beside} beside this computer\'s own; {already} here already; {deleted} deleted here, not brought back; {bad} did not arrive whole.', {
        copied: count(bring.copied), size: size(bring.copied_bytes), beside: count(bring.beside),
        already: count(bring.already), deleted: count(bring.deleted_here), bad: count(bring.not_same) }));
      if (bring.not_a_library_here) {
        lines.push(i18n.t('{count} are in a library this computer does not have, or whose disk is away.', {
          count: count(bring.not_a_library_here) }));
      }
    }
    if (bring.running && bring.current) lines.push(i18n.t('Now: {path}', { path: bring.current }));
    if (bring.waiting) lines.push(i18n.t('Waiting: {reason}', { reason: i18n.fromServer(bring.waiting) }));
    if (bring.complete) lines.push(i18n.t('Done. The library is being indexed; the names, albums and stars come from the sidecars.'));
    if (bring.error) lines.push(i18n.fromServer(bring.error));
    $('#tc-only-says').textContent = lines.join(' ');
    $('#tc-only-look').hidden = Boolean(found.running || bring.running);
    $('#tc-only-look').disabled = Boolean(answer.why_not);
    const wanted = of('only').files + of('other_version').files;
    $('#tc-only-bring').hidden = !(answer.can_bring && wanted);
    $('#tc-only-stop').hidden = !bring.running;
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

  /** A census, plan or proof started: what it says now, and look again soon. */
  async topologyAct(call) {
    const buttons = [...document.querySelectorAll('#tc-safe button')];
    buttons.forEach((b) => { b.disabled = true; });
    try {
      this.renderSafe(await call());
    } catch (exc) {
      this.toast(exc.message, true);
    } finally {
      buttons.forEach((b) => { b.disabled = false; });
    }
    this.schedule();
  }

  /** Census, plan and proof (mugil/topology.py), on every computer. */
  async safe() {
    try {
      this.renderSafe(await json('/api/admin/mugil/topology'));
    } catch {
      $('#tc-safe').hidden = true;
    }
  }

  renderSafe(answer) {
    if (!answer) return;
    this.topology = answer;
    const here = answer.standing || {};
    const running = Boolean(answer.running);
    const record = answer.record || null;
    const censuses = answer.censuses || [];
    const last = censuses[censuses.length - 1] || null;
    $('#tc-safe').hidden = false;

    const part = (ROLE_STATE[here.role] || ROLE_STATE.single)();
    const standing = [here.paired ? i18n.t('Standing: {part}, paired.', { part })
      : i18n.t('Standing: {part}, not paired.', { part })];
    if (last) {
      const days = Math.floor((Date.now() / 1000 - (last.taken_at || 0)) / 86400);
      const about = { when: when(last.taken_at), files: count(last.files), size: size(last.bytes) };
      standing.push(days < 1 ? i18n.t('Last census {when}, today: {files} files, {size}.', about)
        : i18n.t('Last census {when}, {days} days ago: {files} files, {size}.', { ...about, days: count(days) }));
    } else {
      standing.push(i18n.t('No census taken on this computer yet.'));
    }
    $('#tc-safe-standing').textContent = standing.join(' ');
    $('#tc-safe-warn').hidden = Boolean(here.paired);

    $('#tc-census-go').hidden = Boolean(here.paired) || running;
    $('#tc-plan-go').hidden = !(here.role === 'primary' && here.paired) || running;
    $('#tc-plan-which').hidden = $('#tc-plan-go').hidden;
    $('#tc-prove-go').hidden = !answer.can_prove || running;

    const lines = [];
    const progress = answer.progress || {};
    if (running && answer.job === 'census') {
      lines.push(i18n.t('Taking a census: {files} files so far, {size}.', {
        files: count(progress.files), size: size(progress.bytes) }));
    } else if (running && answer.job === 'plan') {
      lines.push(i18n.t('Making the plan from the censuses…'));
    } else if (running) {
      lines.push(i18n.t('Proving: taking both censuses again and comparing them…'));
    }
    if (running && answer.partner_progress) {
      lines.push(i18n.t('The other computer: {files} files so far.', {
        files: count(answer.partner_progress.files) }));
    }
    if (answer.waiting) lines.push(i18n.t('Waiting: {reason}', { reason: i18n.fromServer(answer.waiting) }));
    if (!running && answer.error) lines.push(i18n.fromServer(answer.error));
    if (last?.away?.length) {
      lines.push(i18n.t('That census could not reach {folders}: plug the disk in and take it again.', {
        folders: last.away.join(', ') }));
    }
    if (answer.moving) {
      lines.push(i18n.t('The plan for moving the Primary is ready: export the index now, and the plan goes with it.'));
    }
    if (record?.state === 'planned' && record.transition === 'replace_secondary_disk') {
      lines.push(i18n.t('Bring in what only the Secondary\'s disk holds, put the new disk in, let the originals be sent again, then prove nothing was lost.'));
    } else if (record?.state === 'planned' && answer.can_prove) {
      lines.push(i18n.t('Carry the change out, then prove nothing was lost.'));
    } else if (record?.state === 'planned' && record.transition === 'pair') {
      lines.push(i18n.t('Bring them in, then prove nothing was lost.'));
    }
    if (record?.state === 'failed' && record.error) lines.push(i18n.fromServer(record.error));
    $('#tc-safe-says').textContent = lines.join(' ');
    this.renderPlan(record);
    this.renderDiskPlan(record);
    this.renderProof(record);
  }

  /** Replacing the Secondary's disk: what only that disk holds, which goes
   * with it unless it is brought in first (mugil/topology.py). */
  renderDiskPlan(record) {
    const plan = record?.transition === 'replace_secondary_disk' ? record.plan : null;
    const block = $('#tc-disk-plan');
    if (!plan || record.state === 'planning') { block.hidden = true; return; }
    block.hidden = false;
    const only = Number(plan.only_on_the_secondary) || 0;
    const says = $('#tc-disk-says');
    says.textContent = only
      ? i18n.t('{count} files are only on the Secondary\'s disk ({size}). Bring them in on this page before the disk is replaced, or they go with it.', {
        count: count(only), size: size(plan.only_bytes) })
      : i18n.t('Nothing is only on the Secondary\'s disk: everything it holds is here too. The disk can be replaced, and the originals sent again.');
    says.className = only ? 'hint tc-away' : 'hint';
    $('#tc-disk-only').replaceChildren(...(plan.named || []).map((gone) => {
      const row = document.createElement('li');
      const head = document.createElement('strong');
      head.textContent = gone.file;
      const line = document.createElement('span');
      line.className = 'hint subtle';
      line.textContent = size(gone.size);
      row.append(head, line);
      return row;
    }));
  }

  /** The plan's totals by outcome, and its files, a page at a time. */
  renderPlan(record) {
    const plan = record?.plan;
    const block = $('#tc-plan');
    if (!plan || !(plan.primary || plan.secondary)) { block.hidden = true; this.planFor = null; return; }
    block.hidden = false;
    const outcomes = new Set();
    const rows = [];
    for (const side of ['primary', 'secondary']) {
      for (const [outcome, bucket] of Object.entries(plan[side] || {})) {
        outcomes.add(outcome);
        const row = document.createElement('li');
        row.dataset.outcome = outcome;
        row.textContent = i18n.t('{where}: {outcome}, {files} files, {size}', {
          where: side === 'primary' ? i18n.t('On the Primary') : i18n.t('On the Secondary'),
          outcome: outcomeWords(outcome), files: count(bucket.files), size: size(bucket.bytes) });
        rows.push(row);
      }
    }
    $('#tc-plan-totals').replaceChildren(...rows);
    const key = `${record.id}:${record.planned || ''}`;
    if (this.planFor === key) return;
    this.planFor = key;
    const filter = $('#tc-plan-filter');
    const all = document.createElement('option');
    all.value = '';
    all.textContent = i18n.t('Every file');
    filter.replaceChildren(all, ...[...outcomes].map((outcome) => {
      const option = document.createElement('option');
      option.value = outcome;
      option.textContent = outcomeWords(outcome);
      return option;
    }));
    this.loadPlan(true);
  }

  async loadPlan(fresh) {
    if (fresh) { this.planNext = 0; $('#tc-plan-list').replaceChildren(); }
    const outcome = $('#tc-plan-filter').value;
    const query = new URLSearchParams({ limit: '100', after: String(this.planNext || 0) });
    if (outcome) query.set('outcome', outcome);
    let page;
    try {
      page = await json(`/api/admin/mugil/topology/plan?${query}`);
    } catch (exc) {
      this.toast(exc.message, true);
      return;
    }
    const reasons = page.reasons || {};
    $('#tc-plan-list').append(...(page.items || []).map((item) => {
      const row = document.createElement('li');
      row.dataset.outcome = item.outcome;
      const head = document.createElement('strong');
      head.textContent = item.file;
      const line = document.createElement('span');
      line.className = 'hint subtle';
      const said = [outcomeWords(item.outcome)];
      if (item.to) said.push(i18n.t('to {path}', { path: item.to }));
      if (item.reason && reasons[item.reason]) said.push(i18n.fromServer(reasons[item.reason]));
      line.textContent = said.join(' · ');
      row.append(head, line);
      return row;
    }));
    this.planNext = page.next;
    $('#tc-plan-more').hidden = !page.next;
  }

  /** The proof: passed, or failed with every missing file named. */
  renderProof(record) {
    const proof = record?.proof;
    const block = $('#tc-proof');
    if (!proof || record.state === 'proving') { block.hidden = true; return; }
    block.hidden = false;
    const says = $('#tc-proof-says');
    const lines = [];
    if (proof.passed) {
      lines.push(i18n.t('Passed: nothing was lost. All {files} files seen before ({size}) are still there. Proved {when}.', {
        files: count(proof.checked), size: size(proof.bytes), when: when(proof.proved_at) }));
    } else {
      lines.push(i18n.t('Failed: {count} files seen before are missing. Put them back, then prove again.', {
        count: count(proof.missing_count) }));
      if (proof.error) lines.push(i18n.fromServer(proof.error));
      if (proof.away_before?.length) {
        lines.push(i18n.t('The census before could not reach {folders}, so the proof cannot pass: take the censuses again with the disk plugged in.', {
          folders: proof.away_before.join(', ') }));
      }
    }
    if (record.transition === 'unpair') {
      lines.push(i18n.t('This proves this computer\'s side; prove the other computer on its own console.'));
    }
    says.textContent = lines.join(' ');
    says.className = proof.passed ? 'hint' : 'hint tc-away';
    $('#tc-proof-missing').replaceChildren(...(proof.missing || []).map((gone) => {
      const row = document.createElement('li');
      const head = document.createElement('strong');
      head.textContent = gone.file;
      const line = document.createElement('span');
      line.className = 'hint subtle';
      line.textContent = i18n.t('Was on {name}, {size}.', { name: gone.computer || '—', size: size(gone.size) });
      row.append(head, line);
      return row;
    }));
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
