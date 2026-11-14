/**
 * The console's Health page: how many photographs are in two places, what
 * went wrong, the copies of the index and the weekly photograph. Loaded when it is first opened (admin.js, openPage).
 * The drives, storage checks and the scrubber on the same page are still
 * drawn by admin.js and the panels it starts.
 */

import { admin, consoleJson as json } from '../../api.js';
import * as i18n from '../../i18n.js';
import { $, $$, bytesShort, consoleToast as toast, el, whenShort } from '../../ui.js';

const adminApi = {
  problems: (limit = 12) => admin.getAdminProblems({ query: { limit } }),
  copies: () => admin.getAdminCopies(),
  backups: () => admin.getAdminBackups(),
  backupNow: () => admin.postAdminBackupsNow(),
  verifyBackup: () => admin.postAdminBackupsVerify(),
  clearProblems: () => admin.postAdminProblemsClear(),
  drill: () => admin.getAdminBackupDrill(),
  drillRun: () => admin.postAdminBackupDrill(),
};

/** The console around the page (admin.js). */
let shell;

/* --- photographs in two places ------------------------------------------ */
/*
 * Every copy (the second disk, the Secondary, the off-site copy, Google
 * Drive) writes down, as it confirms a copy, which photographs it keeps
 * (ninaivu/copies). This reads that record: nothing is sent or checked by
 * opening the page.
 */

const COPY_NAMES = {
  mirror: i18n.key('Second disk'),
  secondary: i18n.key('The Secondary'),
  offsite: i18n.key('Off-site copy'),
  drive: i18n.key('Google Drive'),
};

function copyCard(dest) {
  const card = el('div', 'card');
  const name = COPY_NAMES[dest.name] ? i18n.t(COPY_NAMES[dest.name]) : dest.name;
  card.appendChild(el('div', 'card-label', name));
  const kept = Number(dest.ledger?.kept) || 0;
  card.appendChild(el('div', 'card-value', dest.on || kept ? kept.toLocaleString(i18n.locale()) : '—'));
  let said;
  if (!dest.on) {
    said = i18n.t('Off');
  } else if (dest.problems?.length) {
    said = dest.problems[0];
    card.classList.add('bad');
  } else if (dest.error) {
    said = i18n.t('The last attempt failed: {reason}', { reason: dest.error });
    card.classList.add('bad');
  } else if (dest.running) {
    said = i18n.t('Copying now');
  } else if (dest.last_finished) {
    said = i18n.t('Last done {when}', { when: whenShort(dest.last_finished) });
    card.classList.add('good');
  } else {
    said = i18n.t('Not done yet');
  }
  card.appendChild(el('div', 'card-sub', said));
  return card;
}

async function loadCopies() {
  let data;
  try {
    data = await adminApi.copies();
  } catch (exc) {
    return;                       // the panel is a convenience, never a blocker
  }
  const head = data.headline || {};
  const total = Number(head.total) || 0;
  const two = Math.min(Number(head.two_places) || 0, total);
  $('#copies-headline').textContent = total
    ? i18n.t('{two} of {total} photographs are in at least two places',
      { two: two.toLocaleString(i18n.locale()), total: total.toLocaleString(i18n.locale()) })
    : i18n.t('No photographs yet');
  const bar = $('#copies-bar');
  bar.hidden = !total;
  bar.max = Math.max(total, 1);
  bar.value = two;
  const bare = Number(head.unfingerprinted) || 0;
  const note = $('#copies-unfingerprinted');
  note.hidden = !bare;
  note.textContent = bare
    ? i18n.t('{count} are not fingerprinted yet: they are counted, but not as in two places until they are.',
      { count: bare.toLocaleString(i18n.locale()) })
    : '';
  const cards = $('#copies-destinations');
  cards.replaceChildren(...(data.destinations || []).map(copyCard));
}

/* --- the backup drill --------------------------------------------------- */
/*
 * One button: a few photographs restored from every copy into a temporary
 * folder and held to the library's fingerprints (ninaivu/copies/drill.py).
 * This only asks and tells; while it runs the page asks again every two
 * seconds.
 */

const DRILL_NAMES = {
  ...COPY_NAMES,
  index: i18n.key('Index backup'),
};

const DRILL_SAID = {
  passed: i18n.key('Passed'),
  failed: i18n.key('Failed'),
  attention: i18n.key('Could not be checked'),
  nothing: i18n.key('Nothing to check yet'),
  off: i18n.key('Off'),
};

let drillTimer = null;

function drillName(name) {
  return DRILL_NAMES[name] ? i18n.t(DRILL_NAMES[name]) : name;
}

function drillCard(copy) {
  const card = el('div', 'card');
  card.appendChild(el('div', 'card-label', drillName(copy.name)));
  const value = copy.checked
    ? i18n.t('{passed} of {checked}', { passed: copy.passed, checked: copy.checked })
    : '—';
  card.appendChild(el('div', 'card-value', value));
  let said = DRILL_SAID[copy.status] ? i18n.t(DRILL_SAID[copy.status]) : copy.status;
  if (copy.status === 'passed' && copy.name !== 'index') {
    said = i18n.t('Passed: {count} matched the library fingerprint', { count: copy.fingerprinted || 0 });
  } else if (copy.reason) {
    said = `${said}: ${copy.reason}`;
  }
  card.appendChild(el('div', 'card-sub', said));
  if (copy.status === 'passed') card.classList.add('good');
  if (copy.status === 'failed') card.classList.add('bad');
  return card;
}

function showDrill(data) {
  const button = $('#drill-run');
  const head = $('#drill-headline');
  const cards = $('#drill-copies');
  const list = $('#drill-files');
  if (!button || !head) return;
  button.disabled = Boolean(data.running);
  head.classList.remove('good', 'bad');
  if (data.running) {
    const step = data.progress?.step;
    head.textContent = step
      ? i18n.t('Checking {copy}…', { copy: drillName(step) })
      : i18n.t('Checking…');
    return;
  }
  const last = data.last;
  if (!last) {
    head.textContent = i18n.t('No drill yet. It takes a few minutes, most of it downloading.');
    cards.replaceChildren();
    list.hidden = true;
    return;
  }
  const when = whenShort(last.ended_at);
  const verdict = {
    passed: i18n.t('Passed {when}: every copy that was checked brought its photographs back intact.', { when }),
    failed: i18n.t('FAILED {when}: a copy did not bring its photographs back intact. The files are listed below.', { when }),
    attention: i18n.t('Not finished {when}: a copy could not be checked. Nothing was found damaged.', { when }),
    nothing: i18n.t('Nothing to check {when}: no copy is set up yet.', { when }),
  };
  head.textContent = verdict[last.status] || last.status;
  if (last.status === 'passed') head.classList.add('good');
  if (last.status === 'failed') head.classList.add('bad');
  const copies = (last.copies || []).filter((c) => c.status !== 'off');
  cards.replaceChildren(...copies.map(drillCard));
  const bad = [];
  for (const copy of copies) {
    for (const file of copy.files || []) {
      if (file.ok) continue;
      bad.push(el('li', '', `${drillName(copy.name)} · ${file.path}${file.why ? ` — ${file.why}` : ''}`));
    }
  }
  list.replaceChildren(...bad);
  list.hidden = !bad.length;
}

async function loadDrill() {
  clearTimeout(drillTimer);
  let data;
  try {
    data = await adminApi.drill();
  } catch (exc) {
    return;                       // the panel is a convenience, never a blocker
  }
  showDrill(data);
  if (data.running) drillTimer = setTimeout(loadDrill, 2000);
}

/** Restore a few photographs from every copy and check them (Health). */
export async function runDrill() {
  const button = $('#drill-run');
  if (button) button.disabled = true;
  try {
    const data = await adminApi.drillRun();
    if (!data.started) toast(i18n.t('A drill is already running.'));
    showDrill(data);
  } catch (exc) {
    toast(exc.message, true);
  }
  loadDrill();
}

/* --- anything that went wrong ------------------------------------------- */
/*
 * The log file has everything and nobody will open it. This is the short
 * version: what has gone wrong since the machine was last started, in the one
 * place an administrator already looks when something feels off.
 */

async function loadProblems() {
  let data;
  try {
    data = await adminApi.problems(12);
  } catch (exc) {
    return;                       // the panel is a convenience, never a blocker
  }

  const list = $('#problems');
  const rows = data.problems || [];
  list.innerHTML = '';

  // The whole count, as Needs you and the Health tab give it, not the
  // twelve listed: the two used to disagree once there were more.
  const total = data.total ?? rows.length;
  const pill = $('#problem-count');
  pill.hidden = total === 0;
  pill.textContent = String(total);
  $('#problems-empty').hidden = rows.length > 0;
  $('#problem-file').textContent = data.file || i18n.t('the log file');

  for (const row of rows) {
    const line = el('div', `problem ${row.level === 'error' || row.level === 'critical' ? 'error' : ''}`);
    line.appendChild(el('span', 'where', row.where || ''));
    line.appendChild(el('span', 'what', row.what || ''));
    line.appendChild(el('span', 'when', whenShort(row.at)));
    if (row.detail) line.appendChild(el('pre', 'detail', row.detail));
    list.appendChild(line);
  }
}

/* --- copies of the index ------------------------------------------------- */

async function loadBackups() {
  let data;
  try {
    data = await adminApi.backups();
  } catch (exc) {
    return;
  }

  const last = data.last;
  $('#backup-when').textContent = last ? whenShort(last.at) : i18n.t('never');
  $('#backup-size').textContent = last ? bytesShort(last.bytes) : i18n.t('no copy yet');
  $('#backup-count').textContent = String(data.count || 0);
  $('#backup-keep').textContent = data.keep ? i18n.t('newest {count} kept', { count: data.keep }) : '';
  $('#backup-every').textContent = data.every_hours
    ? (data.every_hours === 24 ? i18n.t('day') : i18n.t('{hours} h', { hours: data.every_hours }))
    : i18n.t('off');
  const sharedWith = data.same_drive_as || [];
  const driveWarning = $('#backup-drive-warning');
  if (driveWarning) {
    driveWarning.hidden = !sharedWith.length;
    driveWarning.textContent = sharedWith.length
      ? i18n.t('These copies are on the same drive as {places}. They protect against mistakes, not against that drive failing. Set NINAIVU_BACKUP_DIR, or "backup_dir" in config.json in the state folder, to a folder on another drive.',
        { places: sharedWith.join(', ') })
      : '';
  }
  $('#backup-where').textContent = data.error
    ? i18n.t('The last attempt failed: {reason}', { reason: data.error })
    : i18n.t('Copies are written to {folder}. To put one back, stop Ninaivu and run ninaivu restore <file>.',
      { folder: data.folder });
  $('#backup-now').disabled = Boolean(data.running);
  // Every new backup is test-restored as soon as it is written; this is the
  // line that says whether the last one would actually come back.
  const check = $('#backup-check');
  if (check) {
    const verified = data.verified;
    check.hidden = !verified;
    check.classList.toggle('bad', Boolean(verified && !verified.ok));
    if (verified) {
      check.textContent = verified.ok
        ? i18n.t('Restore check passed {when}: the newest backup unpacks, matches its checksums and opens as a library of {items}.',
          { when: whenShort(verified.at), items: i18n.items(verified.assets) })
        : i18n.t('Restore check FAILED {when}: {reason}. Make a new backup and check again; if it fails again, the index itself may be damaged.',
          { when: whenShort(verified.at), reason: verified.error });
    }
  }
}

/* --- the weekly photograph ---------------------------------------------
 *
 * Its own section rather than another switch under Notifications, because it
 * is a different promise: those go to whoever looks after the machine and
 * never contain a photograph, and this goes to the family and is one. It
 * shares the mail server and nothing else.
 */

const digestApi = {
  get: () => json('/api/admin/digest'),
  save: (body) => json('/api/admin/digest', { method: 'POST', body }),
  preview: () => json('/api/admin/digest/preview'),
  send: () => json('/api/admin/digest/send', { method: 'POST' }),
};

function describeDigest(settings) {
  const state = $('#digest-state');
  if (state) {
    if (!settings.enabled) state.textContent = i18n.t('Not being sent.');
    else if (!settings.ready) state.textContent = i18n.t('On, but there is nobody to send it to.');
    else if (settings.last_sent) state.textContent = i18n.t('On. Last sent {when}.', { when: settings.last_sent });
    else state.textContent = i18n.t('On. Nothing sent yet.');
    if (settings.last_error) {
      state.textContent += ` ${i18n.t('Last attempt failed: {reason}', { reason: settings.last_error })}`;
    }
  }
  const warning = $('#digest-no-mail');
  if (warning) warning.hidden = Boolean(settings.has_mail_server);
}

async function loadDigest() {
  const to = $('#digest-to');
  if (!to) return;
  const hours = $('#digest-hour');
  // Drawn again on every load rather than once, so a change of language
  // reaches them; the choice survives because it is set from settings below.
  if (hours) {
    const chosen = hours.value;
    hours.replaceChildren();
    for (let h = 0; h < 24; h += 1) {
      const option = document.createElement('option');
      option.value = String(h);
      // Plain words: "9 in the morning" is what somebody means by it.
      option.textContent = h === 0 ? i18n.t('midnight') : h === 12 ? i18n.t('midday')
        : h < 12 ? i18n.t('{hour} in the morning', { hour: h })
          : i18n.t('{hour} in the afternoon', { hour: h - 12 });
      hours.append(option);
    }
    if (chosen) hours.value = chosen;
  }

  let settings;
  try {
    settings = await digestApi.get();
  } catch {
    return;                       // not an admin, or the console is older
  }
  to.value = settings.to || '';
  if ($('#digest-link')) $('#digest-link').value = settings.link || '';
  if ($('#digest-enabled')) $('#digest-enabled').checked = Boolean(settings.enabled);
  if ($('#digest-weekday')) $('#digest-weekday').value = String(settings.weekday);
  if (hours) hours.value = String(settings.hour);
  describeDigest(settings);

  // What would go out today, so nobody has to wait until Sunday to find out
  // whether this works.
  try {
    const seen = await digestApi.preview();
    const line = $('#digest-preview');
    if (!line) return;
    const pick = { subject: seen.subject, year: seen.year, count: seen.faces,
      from: (seen.others + 1).toLocaleString() };
    line.textContent = !seen.ready
      ? i18n.t('Today it would send nothing: {reason}.', { reason: seen.reason })
      : seen.faces === 1
        ? i18n.t('Today it would send “{subject}” — {year}, 1 person in it, chosen from {from} from this day.', pick)
        : i18n.t('Today it would send “{subject}” — {year}, {count} people in it, chosen from {from} from this day.', pick);
  } catch { /* the preview is a nicety, not the feature */ }
}

function wireDigest() {
  $$('#digest-save').onclick = async () => {
    try {
      const result = await digestApi.save({
        enabled: $('#digest-enabled')?.checked || false,
        to: $('#digest-to')?.value || '',
        link: $('#digest-link')?.value || '',
        weekday: Number($('#digest-weekday')?.value ?? 6),
        hour: Number($('#digest-hour')?.value ?? 9),
      });
      describeDigest(result.settings);
      toast(i18n.t('Saved.'));
    } catch (exc) {
      toast(exc.message, true);
    }
  };

  $$('#digest-send').onclick = async () => {
    const button = $('#digest-send');
    if (button) button.disabled = true;
    try {
      const result = await digestApi.send();
      toast(result.sent
        ? i18n.t('Sent: {subject}', { subject: result.subject })
        : i18n.t('Nothing was sent: {reason}', { reason: result.reason }), !result.sent);
      await loadDigest();
    } catch (exc) {
      toast(exc.message, true);
    } finally {
      if (button) button.disabled = false;
    }
  };
}

/** Keep a copy of the index now (Health, and the command bar). */
export async function backupNow() {
  const button = $('#backup-now');
  button.disabled = true;
  const label = button.textContent;
  button.textContent = i18n.t('Copying…');
  try {
    await adminApi.backupNow();
    toast(i18n.t('A copy of the index has been kept.'));
    await loadBackups();
  } catch (exc) {
    toast(exc.message, true);
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

/** Check that the newest copy of the index would restore. */
export async function verifyBackup() {
  const button = $('#backup-verify');
  button.disabled = true;
  const label = button.textContent;
  button.textContent = i18n.t('Checking…');
  try {
    const result = await adminApi.verifyBackup();
    toast(result.verified.ok ? i18n.t('The newest backup restores cleanly.')
      : i18n.t('The newest backup would not restore: {reason}', { reason: result.verified.error }), !result.verified.ok);
    await loadBackups();
  } catch (exc) {
    toast(exc.message, true);
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

/** Bind the page's controls: once, when it is first opened. */
export function wire(context) {
  shell = context;
  $$('#problem-refresh').onclick = () => loadProblems();
  $$('#drill-run').onclick = () => runDrill();
  $$('#problem-clear').onclick = async () => {
    try {
      await adminApi.clearProblems();
      await loadProblems();
      shell.loadAttention();
    } catch (exc) { toast(exc.message, true); }
  };
  wireDigest();
}

/** The page has been opened, or the language changed while it is open. */
export function show() {
  loadCopies();
  loadDrill();
  loadProblems();
  loadBackups();
  loadDigest();
}
