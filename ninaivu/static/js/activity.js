/**
 * The activity strip — everything Ninaivu is doing, on whichever page you are on.
 *
 * There used to be one bar, and it was the library scan. A consolidation
 * walking a USB disk, a cloud upload, a straightening pass, a storage check
 * hashing every file, a model still downloading: each had a page that knew
 * about it and nowhere else did. A machine with its fan up and its disk light
 * on had no answer on screen for why.
 *
 * So both faces render the same list from the same endpoint, and the server
 * composes the words (see ninaivu/server/activity.py) — the gallery and the
 * console had their own copy of the phase names before this, and a phase
 * added to the scanner turned up in neither.
 *
 * What this module will not do is guess. A job that cannot say how far along
 * it is gets a bar that paces rather than a percentage nobody can stand
 * behind, and a paused job says what it is waiting for instead of sitting at
 * a number that never moves and looking hung.
 */

import { api, timeLeft, SCAN_COUNTS } from './api.js';
import * as i18n from './i18n.js';

const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
};

/* -- asking ---------------------------------------------------------------- */

// Scripts update the moment they change on disk; the server only on a restart.
// A page with this script and a server without the endpoint falls back to the
// scan alone — the way it was — rather than to an empty strip that would say
// the machine is idle while it is indexing.
let activityMissing = false;

// What each phase is called, for that fallback only. The server names the
// phases everywhere else; this is the copy that stops a page served by an
// older server from showing a blank line.
const FALLBACK_PHASES = {
  walking: i18n.key('Looking for files'),
  indexing: i18n.key('Indexing'),
  tagging: i18n.key('Analysing with AI'),
  videos: i18n.key('Describing videos'),
  naming: i18n.key('Naming places'),
  reading: i18n.key('Reading text in photos'),
  faces: i18n.key('Finding faces'),
  covers: i18n.key('Drawing pictures for sound files'),
  finishing: i18n.key('Finishing up'),
  paused: i18n.key('Indexing'),
};

function jobFromScan(scan) {
  if (!scan?.running) return null;
  const counter = scan.tag_total ? SCAN_COUNTS[scan.status] : null;
  const paused = scan.status === 'paused';
  const detail = counter
    ? counter(scan.tagged.toLocaleString(), scan.tag_total.toLocaleString())
    : scan.message || `${(scan.processed || 0).toLocaleString()} of ${(scan.total || 0).toLocaleString()}`;
  return {
    id: 'indexing',
    title: i18n.t(FALLBACK_PHASES[scan.status] || i18n.key('Indexing')),
    detail: scan.folder && !paused ? `${detail} · ${scan.folder}` : detail,
    percent: paused || scan.status === 'walking' ? null : scan.percent,
    eta: scan.eta || null,
    paused,
    page: 'library',
    uses: ['disk'],
  };
}

/** Everything running now, plus the scan snapshot the callers still key off. */
export async function fetchActivity() {
  if (!activityMissing) {
    try {
      return await api.activity();
    } catch (err) {
      // A network failure is the caller's to handle; only a route that is not
      // there means this server is older than this script.
      if (err instanceof TypeError || (err.status && err.status !== 404)) throw err;
      activityMissing = true;
    }
  }
  const scan = await api.scanProgress();
  const job = jobFromScan(scan);
  return { jobs: job ? [job] : [], running: Boolean(job), uses: job ? job.uses : [], scan };
}

/* -- drawing --------------------------------------------------------------- */

// One row per job, reused between ticks and keyed by id. Rebuilding the list
// every two seconds would restart the bar's transition on every tick, so a bar
// at 41% would never appear to move — it would jump.
function row(job, onOpen) {
  const node = el(onOpen ? 'button' : 'div', 'job-row');
  if (onOpen) {
    node.type = 'button';
    node.addEventListener('click', () => onOpen(job.page, job));
  }
  const head = el('span', 'job-head');
  head.appendChild(el('span', 'job-dot'));
  head.appendChild(el('span', 'job-title'));
  head.appendChild(el('span', 'job-percent'));
  node.appendChild(head);
  const bar = el('span', 'job-bar');
  bar.appendChild(el('i'));
  node.appendChild(bar);
  node.appendChild(el('span', 'job-text'));
  return node;
}

// What a job is spending, in words a household uses. The point of the whole
// strip is answering "what is using my disk?", so the answer is spelled out
// rather than left as three lowercase keys.
const SPEND = { disk: 'the disk', cpu: 'the processor', network: 'the network' };

export function spendWords(uses) {
  const words = (uses || []).map((use) => SPEND[use]).filter(Boolean);
  if (!words.length) return '';
  if (words.length === 1) return words[0];
  return `${words.slice(0, -1).join(', ')} and ${words[words.length - 1]}`;
}


function fill(node, job, openable) {
  node.dataset.id = job.id;
  node.classList.toggle('paused', Boolean(job.paused));
  // No percentage to show is not the same as 0%. A bar left empty reads as a
  // job that has not started; this one paces instead, which reads as working.
  const unknown = job.percent == null;
  node.classList.toggle('unknown', unknown && !job.paused);
  node.querySelector('.job-title').textContent = job.title;
  node.querySelector('.job-percent').textContent =
    unknown || job.paused ? '' : `${job.percent}%`;
  node.querySelector('.job-bar i').style.width =
    unknown || job.paused ? '' : `${job.percent}%`;
  const left = job.paused ? '' : timeLeft(job.eta);
  const text = left ? `${job.detail} · ${left}` : job.detail;
  node.querySelector('.job-text').textContent = text;
  // The row is one line and the detail is often elided, so the whole of it
  // — and what this job is spending — lives in the tooltip.
  const spending = job.paused ? '' : spendWords(job.uses);
  const lines = [`${job.title} — ${text}`];
  if (spending) lines.push(`Using ${spending}`);
  if (openable) lines.push(i18n.t('Open the page that can stop it'));
  node.title = lines.join('\n');
}

/**
 * Draw `jobs` into `container`, in order. Returns how many are running.
 *
 * `onOpen(page, job)` makes each row a button through to the console page that
 * owns that job — the console passes it, the gallery does not, because a
 * family member has no console to open.
 */
export function renderActivity(container, jobs, onOpen = null) {
  if (!container) return 0;
  const wanted = jobs || [];
  const have = new Map(
    [...container.children].map((node) => [node.dataset.id, node]));
  wanted.forEach((job, index) => {
    let node = have.get(job.id);
    if (node) have.delete(job.id);
    else node = row(job, onOpen);
    fill(node, job, Boolean(onOpen));
    // Order can change — a consolidation starting moves above the indexer —
    // and moving an existing node keeps its bar where it was.
    if (container.children[index] !== node) {
      container.insertBefore(node, container.children[index] || null);
    }
  });
  have.forEach((node) => node.remove());
  return wanted.length;
}

/**
 * Poll the strip for as long as the tab is being looked at.
 *
 * Two cadences: quick while something is running, slow otherwise, so work
 * started somewhere else still turns up here within a few seconds. Both stop
 * when the tab is hidden — a household leaves this open on five devices and
 * none of them should be asking about a library nobody is watching — and
 * coming back to the tab catches up at once.
 */
export function subscribeActivity(onPayload, { onError } = {}) {
  let timer = null;
  let stopped = false;

  const tick = async () => {
    if (stopped) return;
    let payload = null;
    try {
      payload = await fetchActivity();
    } catch (err) {
      if (onError?.(err) === false) return;   // the caller has taken it over
    }
    if (stopped) return;
    if (payload) onPayload(payload);
    clearTimeout(timer);
    if (!document.hidden) {
      timer = setTimeout(tick, payload?.running ? 2000 : 10000);
    }
  };

  const wake = () => { if (!document.hidden) tick(); };
  document.addEventListener('visibilitychange', wake);
  tick();

  return () => {
    stopped = true;
    clearTimeout(timer);
    document.removeEventListener('visibilitychange', wake);
  };
}
