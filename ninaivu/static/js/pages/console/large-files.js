/**
 * The console's Large files page, loaded when it is first opened (admin.js,
 * openPage).
 */

import { hideSecret } from '../../accounts.js';
import { admin, thumbUrl } from '../../api.js';
import * as i18n from '../../i18n.js';
import {
  $, $$, askPasswordToDelete, bytesShort, consoleToast as toast, el, lfDescribe, plural, span,
} from '../../ui.js';
import { revealItem } from './common.js';

const adminApi = {
  largeFiles: (minMb) => admin.getAdminLargeFiles({ query: { min_mb: minMb } }),
  keepLargeFiles: (ids) => admin.postAdminLargeFilesKeep({ body: { ids } }),
  compressVideo: (id, mode, password) =>
    admin.postAdminLargeFilesCompress({ body: { id, mode, password } }),
  compressJobs: () => admin.getAdminLargeFilesCompress(),
  cancelCompress: (jobId) => admin.postAdminLargeFilesCompressByJobIdCancel(jobId),
  extraCopies: () => admin.getAdminLargeFilesExtraCopies(),
  tidyExtraCopies: (password) => admin.postAdminLargeFilesExtraCopies({ body: { password } }),
};

/* --- Large files ---------------------------------------------------------
 *
 * A worklist, not a filter: everything here still shows up everywhere else
 * in the library exactly as before. "Keep" only ever means "stop asking" —
 * see db.mark_large_files_reviewed — and "Delete" is the ordinary
 * password-gated /api/delete, the same one the gallery itself uses, so a
 * deleted file lands in the same recoverable _deleted folder either way.
 *
 * A video has one more answer: Compress & Replace makes a smaller MP4 and
 * swaps it in for the original in one step, with the original kept in the
 * bin first and the password asked for as Delete does. A copy left beside a
 * video by the old Compress button is used instead of encoding again (and
 * Tidy copies still clears extra ones). Videos run one at a time on the
 * server (media/video_compress.py) and this page polls while any is going. */

const lfCompress = { unavailable: null, preset: '', jobs: new Map(), timer: null };
const LF_ACTIVE = ['queued', 'running', 'checking'];

/* One set of buttons above the list acts on the rows that are ticked, rather
 * than four buttons on every row. What is ticked survives a reload of the
 * list (the poll reloads it when a compression finishes), so long as the file
 * is still on it. */
const lfPick = { items: new Map(), chosen: new Set() };

function lfDuration(seconds) {
  if (!seconds) return '';
  const s = Math.round(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = String(Math.floor(s % 60)).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${sec}` : `${m}:${sec}`;
}

async function loadLargeFiles() {
  const list = $('#lf-list');
  const summary = $('#lf-summary');
  const empty = $('#lf-empty');
  const minMb = $('#lf-floor')?.value || 500;
  summary.textContent = i18n.t('Looking…');
  let data;
  try {
    data = await adminApi.largeFiles(minMb);
  } catch (exc) {
    toast(exc.message, true);
    summary.textContent = '';
    return;
  }
  await lfReadJobs();
  lfCompressNote(data.items.some((item) => item.kind === 'video'));
  list.innerHTML = '';
  lfPick.items = new Map(data.items.map((item) => [item.id, item]));
  lfPick.chosen = new Set([...lfPick.chosen].filter((id) => lfPick.items.has(id)));
  $('#lf-bar').hidden = !data.items.length;
  if (!data.items.length) {
    summary.textContent = '';
    empty.textContent = i18n.t('Nothing over {size} MB is waiting to be looked at.', { size: data.floor_mb });
    empty.hidden = false;
    lfRenderBar();
    return;
  }
  empty.hidden = true;
  summary.textContent =
    i18n.t('{files} over {size} MB, largest first.', {
      files: plural(data.items.length, i18n.key('1 file'), i18n.key('{count} files')), size: data.floor_mb });
  for (const item of data.items) list.appendChild(lfRow(item));
  lfRenderBar();
  lfWatch();
}

/** What the server is compressing, and whether it can — one ask per load. */
async function lfReadJobs() {
  try {
    const data = await adminApi.compressJobs();
    lfCompress.unavailable = data.unavailable || null;
    lfCompress.preset = data.preset || '';
    lfCompress.jobs = new Map();
    for (const job of data.jobs || []) lfCompress.jobs.set(job.asset_id, job);
  } catch {
    // The list is still worth showing without the compress buttons' state.
  }
}

function lfCompressNote(hasVideos) {
  const note = $('#lf-compress-note');
  if (!note) return;
  note.hidden = !hasVideos;
  note.classList.toggle('warn', Boolean(lfCompress.unavailable));
  note.textContent = lfCompress.unavailable
    ? i18n.t(lfCompress.unavailable)
    : i18n.t('For a video, Compress & Replace makes a smaller copy ({preset}), checks that it plays, and puts it in place of the original in one step. The original is kept in the bin.',
      { preset: i18n.t(lfCompress.preset || 'MP4 (HEVC), at its own size') });
}

/** The ticked rows, in the order the list shows them (largest first). */
function lfChosen() {
  return [...lfPick.items.values()].filter((item) => lfPick.chosen.has(item.id));
}

/** Ticked videos that are not already being compressed, and not damaged at
 * their source (those can only be kept or deleted). */
function lfCompressible() {
  return lfChosen().filter((item) => item.kind === 'video' && !item.damaged
    && !LF_ACTIVE.includes(lfCompress.jobs.get(item.id)?.state));
}

function lfRenderBar() {
  const chosen = lfChosen();
  const n = chosen.length;
  const total = lfPick.items.size;
  $('#lf-selected').textContent = n
    ? plural(n, i18n.key('1 file chosen'), i18n.key('{count} files chosen'))
    : i18n.t('Nothing chosen');
  const all = $('#lf-all');
  all.checked = total > 0 && n === total;
  all.indeterminate = n > 0 && n < total;

  const videos = lfCompressible().length;
  const replace = $('#lf-replace');
  replace.disabled = Boolean(lfCompress.unavailable) || videos === 0;
  if (lfCompress.unavailable) {
    replace.title = i18n.t(lfCompress.unavailable);
  } else if (n && !videos) {
    replace.title = i18n.t('Only a video that is not damaged and not already being compressed can be compressed.');
  } else {
    replace.title = i18n.t('Compress the video and put the smaller copy in place of the original. The original is kept in the bin.');
  }
  $('#lf-keep').disabled = n === 0;
  $('#lf-keep').title = i18n.t('Stop asking about these files');
  $('#lf-delete').disabled = n === 0;

  for (const row of document.querySelectorAll('.lf-row')) {
    const on = lfPick.chosen.has(Number(row.dataset.id));
    row.classList.toggle('chosen', on);
    const box = row.querySelector('.lf-check');
    if (box) box.checked = on;
  }
}

function lfChoose(id, on) {
  if (on) lfPick.chosen.add(id);
  else lfPick.chosen.delete(id);
  lfRenderBar();
}

function lfChooseAll() {
  const every = lfPick.items.size > 0 && lfPick.chosen.size === lfPick.items.size;
  lfPick.chosen = every ? new Set() : new Set(lfPick.items.keys());
  lfRenderBar();
}

/** "IMG_3165.MOV" (5.7 GB) for one file, "3 files, 17.5 GB together" for more. */
async function lfStartCompress(item, mode, password) {
  try {
    const { job } = await adminApi.compressVideo(item.id, mode, password);
    lfCompress.jobs.set(item.id, job);
    const row = document.querySelector(`.lf-row[data-id="${item.id}"]`);
    if (row) lfShowJob(row, job);
    lfWatch();
    return true;
  } catch (exc) {
    if ((exc.status === 401 && exc.data?.needs_password) || exc.status === 429) throw exc;
    toast(`${item.filename}: ${i18n.t(exc.message)}`, true);
    return false;
  }
}

function lfShowJob(row, job) {
  let line = row.querySelector('.lf-job');
  if (!job) { line?.remove(); return; }
  if (!line) {
    line = el('div', 'lf-job');
    line.setAttribute('role', 'status');
    row.querySelector('.lf-info').appendChild(line);
  }
  line.innerHTML = '';
  line.classList.toggle('bad', job.state === 'error');
  const busy = LF_ACTIVE.includes(job.state);
  let text;
  if (job.state === 'queued') {
    text = job.ahead
      ? i18n.t('Waiting to compress ({count} ahead)…', { count: job.ahead })
      : i18n.t('Starting to compress…');
  } else if (job.state === 'running') {
    text = i18n.t('Compressing… {percent}%', { percent: Math.round((job.progress || 0) * 100) });
  } else if (job.state === 'checking') {
    text = i18n.t('Checking the smaller copy…');
  } else if (job.state === 'done' && job.result) {
    const sizes = { from: bytesShort(job.result.old_size), to: bytesShort(job.result.new_size) };
    text = job.result.mode === 'replace'
      ? i18n.t('Replaced: {from} → {to}. The original is kept in the bin.', sizes)
      : i18n.t('Smaller copy saved as {name}: {from} → {to}.', { ...sizes, name: job.result.name });
  } else if (job.state === 'cancelled') {
    text = i18n.t('Stopped.');
  } else {
    text = i18n.t(job.error || 'Could not compress this video.');
  }
  line.appendChild(span(text));
  if (busy) {
    if (job.state === 'running') {
      const bar = document.createElement('progress');
      bar.max = 100;
      bar.value = Math.round((job.progress || 0) * 100);
      line.appendChild(bar);
    }
    const stop = el('button', 'btn ghost small', i18n.t('Stop'));
    stop.onclick = async () => {
      stop.disabled = true;
      try { await adminApi.cancelCompress(job.id); } catch (exc) { toast(exc.message, true); }
      lfWatch();
    };
    line.appendChild(stop);
  }
}

/** Poll while anything is compressing; stop when nothing is. */
function lfWatch() {
  clearTimeout(lfCompress.timer);
  const going = [...lfCompress.jobs.values()].some((job) => LF_ACTIVE.includes(job.state));
  if (!going) return;
  lfCompress.timer = setTimeout(async () => {
    const before = new Map(lfCompress.jobs);
    await lfReadJobs();
    let finished = false;
    for (const [assetId, job] of lfCompress.jobs) {
      const row = document.querySelector(`.lf-row[data-id="${assetId}"]`);
      if (row) lfShowJob(row, job);
      const was = before.get(assetId);
      if (was && LF_ACTIVE.includes(was.state) && !LF_ACTIVE.includes(job.state)) {
        finished = true;
        if (job.state === 'done' && job.result) {
          toast(job.result.mode === 'replace'
            ? i18n.t('“{name}” is now {size}. The original is kept in the bin.',
              { name: job.result.name, size: bytesShort(job.result.new_size) })
            : i18n.t('Smaller copy saved as {name}.', { name: job.result.name }));
        } else if (job.state === 'error') {
          toast(i18n.t(job.error || 'Could not compress this video.'), true);
        }
      }
    }
    // A replaced video's size changed, and a new copy may itself be large:
    // the list is read again so it says what is on disk now.
    if (finished) await loadLargeFiles();
    else { lfRenderBar(); lfWatch(); }
  }, 1500);
}

/** Compress & Replace every ticked video: one password for all of them. The first
 * request checks it; the rest follow one after another with the same one. */
function lfReplaceChosen() {
  const items = lfCompressible();
  if (!items.length) return;
  const modal = $('#lf-replace-modal');
  const input = $('#lf-replace-password');
  const error = $('#lf-replace-error');
  const confirm = $('#lf-replace-confirm');

  $('#lf-replace-title').textContent = items.length === 1
    ? i18n.t('Compress and replace this video?')
    : i18n.t('Compress and replace these videos?');
  $('#lf-replace-what').textContent = lfDescribe(items);
  error.hidden = true;
  input.value = '';
  hideSecret(input);
  modal.hidden = false;
  input.focus();

  const close = () => {
    modal.hidden = true;
    input.onkeydown = null;
    confirm.onclick = null;
    $('#lf-replace-cancel').onclick = null;
  };

  const attempt = async () => {
    if (!input.value) { input.focus(); return; }
    const password = input.value;
    confirm.disabled = true;
    try {
      let checked = false;
      for (const item of items) {
        try {
          if (await lfStartCompress(item, 'replace', password)) lfPick.chosen.delete(item.id);
          checked = true;
        } catch (exc) {
          // A wrong password is said in the dialog, before anything started.
          if (!checked) {
            error.textContent = i18n.fromServer(exc.data, exc.message);
            error.hidden = false;
            input.value = '';
            input.focus();
            return;
          }
          toast(`${item.filename}: ${i18n.t(exc.message)}`, true);
        }
      }
      close();
      lfRenderBar();
    } finally {
      confirm.disabled = false;
    }
  };

  confirm.onclick = attempt;
  $('#lf-replace-cancel').onclick = close;
  input.onkeydown = (event) => {
    if (event.key === 'Enter') { event.preventDefault(); attempt(); }
    if (event.key === 'Escape') close();
  };
}

async function lfKeepChosen() {
  const items = lfChosen();
  if (!items.length) return;
  const keep = $('#lf-keep');
  keep.disabled = true;
  try {
    await adminApi.keepLargeFiles(items.map((item) => item.id));
    for (const item of items) lfPick.chosen.delete(item.id);
    toast(items.length === 1
      ? i18n.t('Won’t ask about “{name}” again.', { name: items[0].filename })
      : i18n.t('Won’t ask about these {count} files again.', { count: items.length }));
    await loadLargeFiles();
  } catch (exc) {
    toast(exc.message, true);
    lfRenderBar();
  }
}

function lfRow(item) {
  const row = el('div', 'lf-row');
  row.dataset.id = item.id;

  const box = document.createElement('input');
  box.type = 'checkbox';
  box.className = 'lf-check';
  box.checked = lfPick.chosen.has(item.id);
  box.setAttribute('aria-label', item.filename);
  box.onchange = () => lfChoose(item.id, box.checked);
  row.appendChild(box);
  row.classList.toggle('chosen', box.checked);

  if (item.thumb) {
    const img = document.createElement('img');
    img.className = 'lf-thumb';
    img.src = thumbUrl(item.id, 160, item.thumb_v);
    img.alt = '';
    img.loading = 'lazy';
    img.title = i18n.t('Show this file where it lives');
    img.onclick = () => revealItem(item);
    row.appendChild(img);
  } else {
    const placeholder = el('div', 'lf-thumb lf-thumb-empty', item.kind === 'video' ? '▶' : '');
    row.appendChild(placeholder);
  }

  const info = el('div', 'lf-info');
  info.appendChild(el('div', 'lf-name', item.filename));
  const meta = el('div', 'lf-meta');
  meta.appendChild(span(bytesShort(item.size)));
  if (item.duration) meta.appendChild(span(lfDuration(item.duration)));
  meta.appendChild(span(item.folder || i18n.t('(library root)')));
  if (item.no_camera) meta.appendChild(el('span', 'tag warn', i18n.t('no camera info')));
  if (item.damaged) meta.appendChild(el('span', 'tag warn', i18n.t('damaged')));
  info.appendChild(meta);
  if (item.damaged) {
    info.appendChild(el('div', 'lf-job bad', i18n.t('This video is damaged at its source: the file was cut short, so its index is missing and it cannot be played or compressed. Keep it or delete it; only an older copy from elsewhere can bring it back.')));
  }
  row.appendChild(info);

  // Anywhere on the row ticks it, except the picture (which opens the file)
  // and the job line's Stop button.
  row.addEventListener('click', (event) => {
    if (event.target.closest('.lf-thumb, button, input, a')) return;
    lfChoose(item.id, !lfPick.chosen.has(item.id));
  });

  if (item.kind === 'video' && !item.damaged && lfCompress.jobs.has(item.id)) {
    lfShowJob(row, lfCompress.jobs.get(item.id));
  }

  return row;
}

/** Move the extra compressed copies to the bin: what the server says is
 * extra (all but the newest copy of a video, and every copy of a video
 * already replaced), shown first, then one password. */
async function lfTidyCopies() {
  let plan;
  try {
    plan = await adminApi.extraCopies();
  } catch (exc) {
    toast(exc.message, true);
    return;
  }
  if (!plan.items.length) {
    toast(i18n.t('No extra compressed copies to tidy.'));
    return;
  }
  const modal = $('#lf-delete-modal');
  const input = $('#lf-delete-password');
  const error = $('#lf-delete-error');
  const confirm = $('#lf-delete-confirm');

  $('#lf-delete-title').textContent = i18n.t('Move the extra copies to the bin?');
  $('#lf-delete-what').textContent = lfDescribe(plan.items);
  error.hidden = true;
  input.value = '';
  modal.hidden = false;
  input.focus();

  const close = () => {
    modal.hidden = true;
    input.onkeydown = null;
    confirm.onclick = null;
    $('#lf-delete-cancel').onclick = null;
  };

  const attempt = async () => {
    if (!input.value) { input.focus(); return; }
    confirm.disabled = true;
    try {
      const result = await adminApi.tidyExtraCopies(input.value);
      close();
      toast(i18n.t('{files} moved to the bin.', {
        files: plural(result.deleted, i18n.key('1 file'), i18n.key('{count} files')) }));
      await loadLargeFiles();
    } catch (exc) {
      if (exc.status === 401 && exc.data?.needs_password) {
        error.textContent = i18n.fromServer(exc.data, exc.message);
        error.hidden = false;
        input.value = '';
        input.focus();
        return;
      }
      close();
      toast(exc.message, true);
    } finally {
      confirm.disabled = false;
    }
  };

  confirm.onclick = attempt;
  $('#lf-delete-cancel').onclick = close;
  input.onkeydown = (event) => {
    if (event.key === 'Enter') { event.preventDefault(); attempt(); }
    if (event.key === 'Escape') close();
  };
}

/** Delete every ticked file: one password, one request, all into the bin. */
function lfDeleteChosen() {
  const items = lfChosen();
  if (!items.length) return;
  askPasswordToDelete(items, async (result) => {
    for (const item of items) lfPick.chosen.delete(item.id);
    const failed = (result.failed || []).length;
    if (failed) {
      toast(i18n.t('{count} could not be moved to the bin.', { count: failed }), true);
    } else {
      toast(items.length === 1
        ? i18n.t('“{name}” moved to the bin.', { name: items[0].filename })
        : i18n.t('{count} files moved to the bin.', { count: items.length }));
    }
    await loadLargeFiles();
  });
}

/** Ask for the password, then move *items* to the bin; *done* gets the
 *  server's answer. Shared by Large files and Folders. With `password` off
 *  (the Folders screen) it only asks to confirm: the files go to the bin,
 *  where Recently deleted puts them back. */

/** Bind the page's controls: once, when it is first opened. */
export function wire() {
  $$('#lf-refresh').onclick = () => loadLargeFiles();
  $$('#lf-floor').onchange = () => loadLargeFiles();
  $$('#lf-all').onchange = () => lfChooseAll();
  $$('#lf-replace').onclick = () => lfReplaceChosen();
  $$('#lf-keep').onclick = () => lfKeepChosen();
  $$('#lf-tidy').onclick = () => lfTidyCopies();
  $$('#lf-delete').onclick = () => lfDeleteChosen();
  $$('#lf-replace-modal').addEventListener('click', (event) => {
    if (event.target === $('#lf-replace-modal')) $('#lf-replace-cancel')?.click();
  });
}

/** The page has been opened, or the language changed while it is open. */
export function show() {
  return loadLargeFiles();
}
