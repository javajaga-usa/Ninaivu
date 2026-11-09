/**
 * The Health page's notifications: what is sent, and where to. The settings
 * are asked for at sign-in (admin.js, loadNotifications); this module draws
 * them, once, when the page is first opened (admin.js, openPage).
 */

import { admin } from '../../api.js';
import * as i18n from '../../i18n.js';
import { $, $$, consoleToast as toast, el } from '../../ui.js';

let shell = null;
// The last answer, so a change of language can say it again without asking.
let lastNotifications = null;

function describeNotifications(data) {
  lastNotifications = data;
  const state = $('#notify-state');
  if (!state) return;
  state.textContent = data.configured
    ? i18n.t('Notifications are on.')
    : i18n.t('Nothing is being sent — add a webhook or an email address.');
}

/** The settings the server sent: the events to tick, the form and the line. */
export function render(settings) {
  const box = $('#notify-events');
  if (!box || !settings) return;

  const chosen = new Set(settings.events || []);
  box.innerHTML = '';
  for (const [key, description] of Object.entries(settings.known_events || {})) {
    const row = el('label', 'toggle');
    const input = el('input');
    input.type = 'checkbox';
    input.value = key;
    input.checked = chosen.has(key);
    // Kept as the English key too, so a change of language retranslates it.
    const words = el('span', null, i18n.t(description));
    words.dataset.i18n = description;
    row.append(input, words);
    box.appendChild(row);
  }
  fillNotificationForm(settings.form);
  describeNotifications(settings);
}

// What was saved, in the fields. Opened empty, the form sent empty strings
// for everything on Save, so changing one tick box wiped the webhook and the
// email setup. The password never comes back; a blank one keeps it.
function fillNotificationForm(form) {
  if (!form) return;              // a server from before it said
  const set = (id, value) => { const input = $(id); if (input) input.value = value ?? ''; };
  set('#notify-webhook', form.webhook);
  set('#notify-smtp-host', form.smtp_host);
  set('#notify-smtp-port', form.smtp_port || 587);
  set('#notify-smtp-user', form.smtp_user);
  set('#notify-smtp-to', form.smtp_to);
  const password = $('#notify-smtp-password');
  if (password) {
    password.value = '';
    password.placeholder = form.smtp_password_saved ? i18n.t('saved — leave blank to keep') : i18n.t('password');
  }
}

/** The language changed: say the line again in it. */
export function relabel() {
  if (lastNotifications) describeNotifications(lastNotifications);
}

/** Bind Save and Send a test, and draw what sign-in fetched: once, when first opened. */
export function wire(context) {
  shell = context;
  $$('#notify-save').onclick = async () => {
    const events = [...document.querySelectorAll('#notify-events input:checked')].map((i) => i.value);
    try {
      const result = await admin.postAdminNotifications({ body: {
        webhook: $('#notify-webhook')?.value || '',
        smtp_host: $('#notify-smtp-host')?.value || '',
        smtp_port: Number($('#notify-smtp-port')?.value || 587),
        smtp_user: $('#notify-smtp-user')?.value || '',
        smtp_password: $('#notify-smtp-password')?.value || '',
        smtp_to: $('#notify-smtp-to')?.value || '',
        events,
      } });
      describeNotifications(result.settings);
      toast(i18n.t('Saved.'));
    } catch (exc) {
      toast(exc.message, true);
    }
  };

  $$('#notify-test').onclick = async () => {
    try {
      const result = await admin.postAdminNotificationsTest();
      toast(result.sent
        ? i18n.t('Sent. Check wherever you pointed it.')
        : i18n.t('Nothing was sent: {reason}', { reason: result.reason || result.webhook || result.email }),
        !result.sent);
    } catch (exc) {
      toast(exc.message, true);
    }
  };
  render(shell.notifications());
}
