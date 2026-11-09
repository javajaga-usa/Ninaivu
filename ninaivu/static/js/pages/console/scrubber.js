/**
 * The Health page's storage check: its counts, what it found and how far a
 * running check has got. The status is asked for at sign-in and polled while
 * a check runs (admin.js, pollScrubber), and the start and stop buttons stay
 * there for the command bar; this module only draws, once the page is opened.
 */

import * as i18n from '../../i18n.js';
import { $ } from '../../ui.js';

/** How far a running check has got, and the buttons that go with it. */
function showScrubberProgress(progress) {
  const running = !!progress?.running;
  const startBtn = $('#scrubber-start-btn');
  const stopBtn = $('#scrubber-stop-btn');
  if (startBtn) {
    startBtn.disabled = running;
    startBtn.textContent = !running && Number(progress?.stopped_at) > 0
      ? i18n.t('Carry on checking') : i18n.t('Check every file');
  }
  if (stopBtn) {
    stopBtn.hidden = !running;
    if (!running) stopBtn.disabled = false;
  }
  const box = $('#scrub-progress');
  if (!box) return;
  box.hidden = !running;
  if (!running) return;
  const total = Number(progress.total) || 0;
  const done = Math.min(Number(progress.processed) || 0, total || Infinity);
  const bar = $('#scrub-progress-bar');
  if (bar) { bar.max = Math.max(total, 1); bar.value = done; }
  const text = $('#scrub-progress-text');
  if (text) {
    text.textContent = total
      ? i18n.t('Checked {done} of {total} files', { done: done.toLocaleString(), total: total.toLocaleString() })
      : i18n.t('Getting ready…');
  }
}

// What the storage check found, in the words the server uses for it.
const SCRUB_STATES = {
  corrupt: i18n.key('corrupt'),
  missing: i18n.key('missing'),
  unreadable: i18n.key('unreadable'),
};

function showScrubberStatus(data) {
  const set = (id, value) => {
    const node = $(id);
    if (node) node.textContent = String(value || 0);
  };
  set('#scrub-total', data.total_assets);
  set('#scrub-verified', data.verified);
  set('#scrub-baseline', data.baseline);
  set('#scrub-changed', data.changed);
  set('#scrub-corrupt', data.corrupt);
  set('#scrub-missing', data.missing);
  set('#scrub-unreadable', data.unreadable);

  // Only the three that need somebody to go and look turn red. "Edited" and
  // "fingerprinted" are ordinary states, not problems.
  const alarm = (id, n) => {
    const card = $(id);
    if (card) card.classList.toggle('bad', (n || 0) > 0);
  };
  alarm('#scrub-corrupt-card', data.corrupt);
  alarm('#scrub-missing-card', data.missing);
  alarm('#scrub-unreadable-card', data.unreadable);

  const issues = data.recent_issues || [];
  const wrap = $('#scrubber-issues-wrap');
  const tbody = $('#scrubber-issues-tbody');
  if (issues.length && wrap && tbody) {
    wrap.hidden = false;
    tbody.innerHTML = '';
    // Built as nodes rather than with innerHTML: these strings are
    // filenames off the disk, and a photograph called
    // `<img src=x onerror=...>.jpg` would otherwise run in the console.
    issues.forEach((iss) => {
      const tr = document.createElement('tr');

      const pathCell = document.createElement('td');
      const path = document.createElement('code');
      path.textContent = iss.rel_path || '';
      pathCell.appendChild(path);

      const statusCell = document.createElement('td');
      const tag = document.createElement('span');
      tag.className = `tag ${iss.status === 'corrupt' ? 'danger' : 'warn'}`;
      tag.textContent = SCRUB_STATES[iss.status] ? i18n.t(SCRUB_STATES[iss.status]) : (iss.status || '');
      statusCell.appendChild(tag);

      const whenCell = document.createElement('td');
      whenCell.textContent = iss.checked_at
        ? new Date(iss.checked_at * 1000).toLocaleString(i18n.locale())
        : i18n.t('Just now');

      tr.append(pathCell, statusCell, whenCell);
      tbody.appendChild(tr);
    });
  } else if (wrap) {
    wrap.hidden = true;
  }
}

/** The status the server last gave (undefined when it could not be asked). */
export function show(data) {
  if (data) showScrubberStatus(data);
  showScrubberProgress(data?.progress);
}

/** Draw what sign-in fetched: once, when the page is first opened. */
export function wire(shell) {
  const data = shell.scrubber();
  if (data) show(data);
}
