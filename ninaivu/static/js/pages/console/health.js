/**
 * The console's Health page: what went wrong, the copies of the index and
 * the weekly photograph. Loaded when it is first opened (admin.js, openPage).
 * The drives, storage checks and the scrubber on the same page are still
 * drawn by admin.js and the panels it starts.
 */

import { admin, consoleJson as json } from '../../api.js';
import * as i18n from '../../i18n.js';
import { $, $$, bytesShort, consoleToast as toast, el, whenShort } from '../../ui.js';

const adminApi = {
  problems: (limit = 12) => admin.getAdminProblems({ query: { limit } }),
  backups: () => admin.getAdminBackups(),
  backupNow: () => admin.postAdminBackupsNow(),
  verifyBackup: () => admin.postAdminBackupsVerify(),
  clearProblems: () => admin.postAdminProblemsClear(),
};

/** The console around the page (admin.js). */
let shell;

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
  loadProblems();
  loadBackups();
  loadDigest();
}
