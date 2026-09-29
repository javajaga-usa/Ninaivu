/**
 * The Archive tab — consolidating drives into one hash-verified archive.
 *
 * This is the front end for `ninaivu/archive_api.py`. It is a module rather
 * than part of admin.js because it is a self-contained tool: it owns its own
 * form state, its own event stream and its own polling, and the console hands
 * it only the three things it cannot supply itself — a toast function, the
 * shared folder picker, and a way to tell the console the library changed.
 *
 * The form is the source of truth for a job. Nothing is saved as you type;
 * pressing Start (or Dry run) is what sends sources and destination to the
 * server, which then remembers them for next time.
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
  const data = text ? JSON.parse(text) : null;
  if (!response.ok) {
    if (response.status === 401) reportUnauthorized(url);
    throw Object.assign(new Error(data?.error || response.statusText),
      { status: response.status, data });
  }
  return data;
}

export const archiveApi = {
  status: () => json('/api/archive/status'),
  version: () => json('/api/archive/version'),
  settings: () => json('/api/archive/settings'),
  validate: (body) => json('/api/archive/validate', { method: 'POST', body }),
  capacity: (body) => json('/api/archive/capacity', { method: 'POST', body }),
  decideCourseFolders: (decisions) =>
    json('/api/archive/course-folders/decision', { method: 'POST', body: { decisions } }),
  capacityProgress: (token) =>
    json(`/api/archive/capacity/progress?token=${encodeURIComponent(token)}`),
  capacityCancel: (token) =>
    json('/api/archive/capacity/cancel', { method: 'POST', body: { token } }),
  start: (body) => json('/api/archive/start', { method: 'POST', body }),
  stop: () => json('/api/archive/stop', { method: 'POST' }),
  pause: () => json('/api/archive/pause', { method: 'POST' }),
  resume: () => json('/api/archive/resume', { method: 'POST' }),
  recent: (status, limit = 60) => json(
    `/api/archive/recent?limit=${limit}${status && status !== 'all' ? `&status=${status}` : ''}`),
  years: () => json('/api/archive/years'),
  retryErrors: () => json('/api/archive/retry-errors', { method: 'POST' }),
  guardian: () => json('/api/archive/guardian/run', { method: 'POST' }),
  reset: () => json('/api/archive/reset', { method: 'POST' }),
  adopt: (path) => json('/api/archive/adopt', { method: 'POST', body: { path } }),
  takeoutAlbums: () => json('/api/archive/takeout-albums'),
  recreateTakeoutAlbums: () => json('/api/archive/takeout-albums', { method: 'POST', body: {} }),
};

const KINDS = [
  ['image', 'Photos'],
  ['video', 'Video'],
  ['audio', 'Audio'],
];

const PLAN_STATES = new Set(['planned', 'plan-duplicate', 'plan-skip']);

/* ======================================================================== */

export class ArchivePanel {
  constructor({ toast, pickFolder, onLibraryChanged, getDevices }) {
    this.toast = toast;
    this.pickFolder = pickFolder;
    this.onLibraryChanged = onLibraryChanged || (() => {});
    this.getDevices = getDevices || (() => fetch('/api/devices').then((r) => r.json()));

    /** @type {{path: string, types: string[]}[]} */
    this.sources = [];
    this.filter = 'all';
    this.last = null;
    this.stream = null;
    this.poll = null;
    this.loaded = false;
    this.validateTimer = null;
    /** The walk currently being watched, and the poll watching it. */
    this.estimateToken = null;
    this.estimateTimer = null;
    /** The tab title before a run borrowed it, and the run last announced. */
    this.plainTitle = null;
    this.announced = null;
    /** Guardian notifications are emitted only when this durable id changes. */
    this.healthChangeId = null;
  }

  /* -- lifecycle -------------------------------------------------------- */

  wire() {
    $('#ar-add-source').onclick = () => this.addSource($('#ar-source-input').value);
    $('#ar-source-input').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        this.addSource($('#ar-source-input').value);
      }
    });
    $('#ar-browse-source').onclick = () => this.pickFolder({
      title: 'Choose a folder to sweep',
      cta: 'Add as a source',
      anyFolder: true,
      start: this.sources.at(-1)?.path || '',
      pick: (path) => this.addSource(path),
    });
    $('#ar-browse-dest').onclick = () => this.pickFolder({
      title: 'Choose where the archive is built',
      cta: 'Use this folder',
      anyFolder: true,
      start: $('#ar-dest-input').value.trim(),
      pick: (path) => {
        $('#ar-dest-input').value = path;
        this.revalidate();
      },
    });
    $('#ar-dest-input').addEventListener('input', () => this.revalidate());
    $('#ar-deep').onchange = () => this.revalidate();

    // Ticking a kind here has to change the job. It used to set the default for
    // folders added *later* only, so unticking Video left every folder already
    // on the list still scanning for video — the estimate then correctly
    // reported a job the user had not knowingly asked for.
    for (const [kind] of KINDS) {
      $(`#ar-type-${kind}`).onchange = (event) => {
        const on = event.target.checked;
        for (const source of this.sources) {
          const has = source.types.includes(kind);
          if (on && !has) source.types = [...source.types, kind];
          if (!on && has) source.types = source.types.filter((t) => t !== kind);
        }
        this.renderSources();
        this.revalidate();
      };
    }

    $('#ar-apply-all').onclick = () => {
      const types = this.defaultTypes();
      if (!types.length) {
        this.toast('Pick at least one kind of file first.', true);
        return;
      }
      this.sources = this.sources.map((s) => ({ ...s, types: [...types] }));
      this.renderSources();
      this.revalidate();
    };

    $('#ar-start').onclick = () => this.run('copy');
    $('#ar-dry').onclick = () => this.run('dry-run');
    $('#ar-audit').onclick = () => this.run('verify');
    $('#ar-stop').onclick = () => this.control('stop', 'Stopping…');
    $('#ar-pause').onclick = () => {
      const paused = this.last?.is_paused;
      this.control(paused ? 'resume' : 'pause', paused ? 'Resuming…' : 'Pausing…');
    };
    $('#ar-retry').onclick = () => this.simple('retryErrors');
    $('#ar-reset').onclick = () => this.simple('reset');
    $('#ar-export').onclick = () => { window.location.href = '/api/archive/manifest.csv'; };
    $('#ar-recovery').onclick = () => { window.location.href = '/api/archive/recovery.json'; };
    $('#ar-health-run').onclick = () => this.simple('guardian');
    $('#ar-adopt').onclick = () => this.adopt();
    $('#ar-takeout-make').onclick = () => this.makeTakeoutAlbums();

    for (const button of document.querySelectorAll('#ar-filters button')) {
      button.onclick = () => {
        this.filter = button.dataset.status;
        document.querySelectorAll('#ar-filters button').forEach(
          (b) => b.classList.toggle('active', b === button));
        this.loadRecent();
      };
    }
    // Deliberately not `checkDevices()` here. `wire()` runs while the console
    // is still building itself, before anybody has signed in, so the answer
    // was a 401 that the console reported as a session ending. `show()` runs
    // when the page is opened, which is when the question is worth asking.
  }

  /** Called when the Archive tab becomes visible. */
  async show() {
    if (!this.loaded) {
      this.loaded = true;
      await this.loadSettings();
      this.loadVersion();
    }
    await this.tick();
    this.checkDevices();
    this.loadRecent();
    this.loadYears();
    this.listen();
  }

  /** Called when the admin navigates away — stop the stream, keep the state. */
  hide() {
    if (this.stream) { this.stream.close(); this.stream = null; }
    if (this.poll) { clearInterval(this.poll); this.poll = null; }
    // Nobody is looking at the estimate any more, so stop reading the drive
    // for it. Coming back to the tab starts a fresh one.
    this.cancelEstimate();
  }

  listen() {
    if (this.stream || this.poll) return;
    if (window.EventSource) {
      const source = new EventSource('/api/archive/stream');
      source.onmessage = (event) => {
        try { this.render(JSON.parse(event.data)); } catch { /* keep listening */ }
      };
      source.onerror = () => {
        // A dropped stream must not leave the page frozen on stale numbers.
        source.close();
        this.stream = null;
        if (!this.poll) this.poll = setInterval(() => this.tick(), 2000);
      };
      this.stream = source;
      return;
    }
    this.poll = setInterval(() => this.tick(), 2000);
  }

  async tick() {
    try {
      this.render(await archiveApi.status());
    } catch { /* the console's own error handling covers a dead server */ }
  }

  /* -- form state ------------------------------------------------------- */

  defaultTypes() {
    return KINDS.map(([kind]) => kind).filter((kind) => $(`#ar-type-${kind}`).checked);
  }

  async loadSettings() {
    try {
      const saved = await archiveApi.settings();
      this.sources = (saved.source_dirs || []).map((entry) => ({
        path: entry.path,
        types: (entry.types && entry.types.length) ? [...entry.types]
          : KINDS.map(([k]) => k),
      }));
      $('#ar-dest-input').value = saved.destination_dir || '';
    } catch { /* first run, nothing saved */ }
    this.renderSources();
  }

  async loadVersion() {
    try {
      const info = await archiveApi.version();
      $('#archive-stamp').textContent = `engine v${info.version} · ${info.build}`;
      $('#archive-stamp').title = `Archive database: ${info.state?.db || ''}`;
    } catch { /* cosmetic */ }
  }

  addSource(raw) {
    const path = (raw || '').trim();
    if (!path) {
      this.toast('Type a folder, or press Browse.', true);
      return;
    }
    if (this.sources.some((s) => samePath(s.path, path))) {
      this.toast('That folder is already a source.', true);
      return;
    }
    const types = this.defaultTypes();
    this.sources.push({ path, types: types.length ? types : KINDS.map(([k]) => k) });
    $('#ar-source-input').value = '';
    this.renderSources();
    this.revalidate();
  }

  /** Make the master checkboxes tell the truth about the folder list. */
  syncTypeBoxes() {
    for (const [kind] of KINDS) {
      const box = $(`#ar-type-${kind}`);
      if (!this.sources.length) { box.indeterminate = false; continue; }
      const on = this.sources.filter((s) => s.types.includes(kind)).length;
      box.checked = on > 0;
      // Some folders yes, some no: neither ticked nor clear is honest.
      box.indeterminate = on > 0 && on < this.sources.length;
    }
  }

  renderSources() {
    const list = $('#ar-sources');
    list.innerHTML = '';
    $('#ar-sources-empty').hidden = this.sources.length > 0;
    this.syncTypeBoxes();

    this.sources.forEach((source, index) => {
      const row = el('div', 'ar-source');
      row.dataset.path = source.path;

      const path = el('span', 'ar-source-path', source.path);
      path.title = source.path;
      row.appendChild(path);

      const kinds = el('div', 'ar-kinds');
      for (const [kind, label] of KINDS) {
        const on = source.types.includes(kind);
        const chip = el('button', `ar-kind${on ? ' on' : ''}`, label);
        chip.type = 'button';
        chip.dataset.kind = kind;
        chip.title = on
          ? `${label} from this folder will be archived`
          : `${label} in this folder will be left alone`;
        chip.onclick = () => {
          source.types = on ? source.types.filter((t) => t !== kind)
            : [...source.types, kind];
          this.renderSources();
          this.revalidate();
        };
        kinds.appendChild(chip);
      }
      row.appendChild(kinds);

      const remove = el('button', 'btn ghost small', 'Remove');
      remove.type = 'button';
      remove.onclick = () => {
        this.sources.splice(index, 1);
        this.renderSources();
        this.revalidate();
      };
      row.appendChild(remove);

      list.appendChild(row);
    });
    this.checkDevices();
  }

  async checkDevices() {
    const container = $('#ar-devices');
    if (!container) return;
    try {
      const data = await this.getDevices();
      if (!data?.available || !data?.devices?.length) {
        container.hidden = true;
        container.innerHTML = '';
        return;
      }
      container.innerHTML = '';
      container.hidden = false;
      for (const dev of data.devices) {
        const card = el('div', 'ar-device-card');
        const info = el('div', 'ar-device-info');
        const badge = el('span', 'ar-device-badge', `📱 ${dev.name}`);
        info.appendChild(badge);
        const hint = el('span', 'hint', 'Connected via USB');
        info.appendChild(hint);
        card.appendChild(info);

        const actions = el('div', 'ar-device-actions');
        const alreadyAdded = this.sources.some((s) => s.path === dev.path || s.path.startsWith(dev.path + '\\'));
        const addBtn = el('button', `btn small ${alreadyAdded ? 'ghost' : 'primary'}`, alreadyAdded ? 'Added' : `Add ${dev.name}`);
        addBtn.type = 'button';
        addBtn.disabled = alreadyAdded;
        addBtn.onclick = () => {
          this.addSource(dev.path);
        };
        actions.appendChild(addBtn);

        const browseBtn = el('button', 'btn small ghost', 'Browse device…');
        browseBtn.type = 'button';
        browseBtn.onclick = () => this.pickFolder({
          title: `Browse ${dev.name}`,
          cta: 'Add as a source',
          anyFolder: true,
          start: dev.path,
          pick: (path) => this.addSource(path),
        });
        actions.appendChild(browseBtn);

        card.appendChild(actions);
        container.appendChild(card);
      }
    } catch (exc) {
      console.warn('devices check error:', exc);
      container.hidden = true;
    }
  }

  job(mode) {
    return {
      source_dirs: this.sources.map((s) => ({ path: s.path, types: s.types })),
      destination_dir: $('#ar-dest-input').value.trim(),
      deep_scan: $('#ar-deep').checked,
      media_types: this.defaultTypes(),
      ...(mode ? { mode } : {}),
    };
  }

  /* -- validation ------------------------------------------------------- */

  revalidate() {
    clearTimeout(this.validateTimer);
    this.validateTimer = setTimeout(() => this.validate(), 350);
  }

  async validate() {
    const job = this.job();
    if (!job.source_dirs.length || !job.destination_dir) {
      this.cancelEstimate();
      this.showNotes({ problems: [], notices: [], resolution: null });
      $('#ar-capacity').hidden = true;
      return;
    }
    // Reaching a sleeping external drive takes seconds on its own, so say so
    // rather than leaving the panel looking as though nothing was pressed.
    this.sayWorking('Checking that folder…');
    try {
      const result = await archiveApi.validate(job);
      this.showNotes(result);
      if (result.ok) this.estimate(job);
      else { this.cancelEstimate(); $('#ar-capacity').hidden = true; }
    } catch {
      // The Start button reports the real refusal; do not leave a spinner
      // running for something that has already stopped.
      this.cancelEstimate();
      $('#ar-capacity').hidden = true;
    }
  }

  /** Put the capacity line into its working state, with a live clock. */
  sayWorking(text) {
    const line = $('#ar-capacity');
    line.hidden = false;
    line.classList.remove('tight');
    line.classList.add('working');
    line.innerHTML = '';
    line.append(el('span', 'ar-spinner'), el('span', 'ar-working-text', text));
  }

  /**
   * Call off the walk that is running, if any, and stop watching it.
   *
   * Every edit to the form starts a new estimate. Without this, removing a
   * drive left the old walk reading it for minutes, and — worse — a slow
   * answer for a job the user had already changed could land on top of a fast
   * answer for the job they actually have.
   */
  cancelEstimate() {
    clearInterval(this.estimateTimer);
    this.estimateTimer = null;
    const token = this.estimateToken;
    this.estimateToken = null;
    if (token) archiveApi.capacityCancel(token).catch(() => {});
  }

  /** "1m 12s" — short enough for a status line, honest about a long wait. */
  static clock(seconds) {
    const whole = Math.max(0, Math.round(seconds || 0));
    if (whole < 60) return `${whole}s`;
    const mins = Math.floor(whole / 60);
    if (mins < 60) return `${mins}m ${String(whole % 60).padStart(2, '0')}s`;
    return `${Math.floor(mins / 60)}h ${String(mins % 60).padStart(2, '0')}m`;
  }

  /** Repaint the working line from a progress snapshot. */
  showEstimateProgress(snapshot) {
    const line = $('#ar-capacity');
    if (!line.classList.contains('working')) return;
    const text = line.querySelector('.ar-working-text');
    if (!text) return;
    text.innerHTML = '';

    if (!snapshot || !snapshot.known) {
      text.append(document.createTextNode('Looking through the folder…'));
      return;
    }
    text.append(
      document.createTextNode('Counting… '),
      strong(Number(snapshot.files || 0).toLocaleString()),
      document.createTextNode(` files so far · ${bytes(snapshot.bytes || 0)}`),
      document.createTextNode(` · ${ArchivePanel.clock(snapshot.elapsed)}`),
    );
    if (snapshot.folder) {
      const where = el('span', 'ar-working-where', snapshot.folder);
      where.title = snapshot.folder;
      text.append(document.createTextNode(' · now in '), where);
    }
  }

  async estimate(job) {
    const line = $('#ar-capacity');
    this.cancelEstimate();

    const token = `est-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
    this.estimateToken = token;
    this.sayWorking('Looking through the folder…');

    // Poll rather than stream: an estimate is watched by one tab for seconds
    // to minutes, and a second SSE connection would cost more than it saves.
    const ask = async () => {
      if (this.estimateToken !== token) return;
      try {
        const snapshot = await archiveApi.capacityProgress(token);
        if (this.estimateToken === token) this.showEstimateProgress(snapshot);
      } catch { /* a dropped poll is not worth reporting */ }
    };
    // Straight away, then steadily. Waiting a whole interval before the first
    // reading is what made a short job flash a placeholder and vanish.
    ask();
    this.estimateTimer = setInterval(ask, 500);

    try {
      const result = await archiveApi.capacity({ ...job, progress_token: token });
      // A newer estimate started while this one was walking: its answer is the
      // right one, and this one must not overwrite it.
      if (this.estimateToken !== token) return;
      clearInterval(this.estimateTimer);
      this.estimateTimer = null;
      this.estimateToken = null;
      if (!result.ok) { line.hidden = true; this.showCourseFolders([], job, []); return; }
      line.classList.remove('working');
      line.innerHTML = '';
      line.append(
        document.createTextNode(result.truncated ? 'At least ' : 'About '),
        strong(result.files.toLocaleString()),
        document.createTextNode(' files, '),
        strong(bytes(result.bytes)),
      );

      // What those files actually are. A single total can hide a selection the
      // user did not mean to make; "44,545 video" cannot.
      const parts = KINDS
        .filter(([kind]) => result.by_kind?.[kind])
        .map(([kind, label]) =>
          `${result.by_kind[kind].toLocaleString()} ${label.toLowerCase()}`
          + (result.bytes_by_kind?.[kind]
            ? ` (${bytes(result.bytes_by_kind[kind])})` : ''));
      if (parts.length > 1) {
        line.append(document.createTextNode(` — ${parts.join(' · ')}`));
      }

      line.append(
        document.createTextNode('. Needs '),
        strong(bytes(result.needed)),
        document.createTextNode(' free; the destination has '),
        strong(bytes(result.free)),
        document.createTextNode(result.fits ? '.' : ' — not enough room.'),
      );
      if (result.truncated) {
        line.append(document.createTextNode(
          ' Counting stopped at 200,000 files — the real job is larger.'));
      }
      line.classList.toggle('tight', !result.fits);
      this.showCourseFolders(result.course_folders, job, result.entertainment);
    } catch {
      if (this.estimateToken === token) {
        clearInterval(this.estimateTimer);
        this.estimateTimer = null;
        this.estimateToken = null;
        line.classList.remove('working');
        line.hidden = true;
      }
    }
  }

  /**
   * What the estimate set aside: downloaded course folders, and the films, TV
   * and music found file by file.
   *
   * Nothing here is excluded by guesswork: a held folder or file is simply not
   * copied until somebody picks Exclude or Keep, and the choice is remembered
   * for that drive. The reasons are shown beside each one because "we think
   * this is a course" or "a song" is a claim a person should be able to check
   * at a glance. Files that looked like entertainment but seem personal are
   * copied and only listed, with no button that could exclude them.
   */
  showCourseFolders(courses, job, entertainment = []) {
    const box = $('#ar-courses');
    if (!box) return;
    const folders = [
      ...(courses || []).map((f) => ({ ...f, category: 'course' })),
      ...(entertainment || []),
    ];
    if (!folders.length) { box.hidden = true; box.replaceChildren(); return; }
    box.hidden = false;
    box.replaceChildren();

    const by = (state) => folders.filter((f) => f.state === state);
    const held = by('held');
    const sizeOf = (list) => list.reduce((sum, f) => sum + (Number(f.bytes) || 0), 0);
    const count = (category) => folders.filter((f) => f.category === category);

    const head = el('div', 'ar-courses-head');
    const title = el('div', 'ar-courses-title');
    const files = (list) => list.reduce((sum, f) => sum + (Number(f.files) || 0), 0);
    const what = [
      count('course').length && `${count('course').length} ${count('course').length === 1 ? 'course folder' : 'course folders'}`,
      files(count('film')) && `${files(count('film')).toLocaleString()} film or TV ${files(count('film')) === 1 ? 'file' : 'files'}`,
      files(count('music')) && `${files(count('music')).toLocaleString()} music ${files(count('music')) === 1 ? 'file' : 'files'}`,
    ].filter(Boolean);
    title.append(strong(what.length ? `Set aside: ${what.join(', ')}` : 'Files that looked like films or music, but seem personal'));
    const summary = [
      held.length && `${held.length} held back (${bytes(sizeOf(held))})`,
      by('excluded').length && `${by('excluded').length} excluded`,
      by('kept').length && `${by('kept').length} kept`,
      by('review').length && `${by('review').length} worth a look`,
    ].filter(Boolean).join(' · ');
    title.append(el('span', 'hint', summary));
    head.append(title);

    if (held.length) {
      const actions = el('div', 'ar-courses-actions');
      const excludeAll = el('button', 'btn small', `Exclude all ${held.length}`);
      const keepAll = el('button', 'btn small ghost', 'Keep all');
      excludeAll.type = keepAll.type = 'button';
      excludeAll.onclick = () => this.decideCourseFolders(held.map((f) => [f.path, 'exclude', f.category]), job);
      keepAll.onclick = () => this.decideCourseFolders(held.map((f) => [f.path, 'include', f.category]), job);
      actions.append(excludeAll, keepAll);
      head.append(actions);
    }
    box.append(head);
    box.append(el('p', 'hint',
      'Held folders and files are not copied until you choose. Your choice is remembered for this drive, '
      + 'even if it comes back under another letter. Films and music are judged file by file: anything '
      + 'that seems personal — filmed on a phone, a voice recording, speech — is always copied. '
      + 'Nothing on the drive itself is changed.'));

    const KIND_LABEL = { course: 'Course', film: 'Film / TV', music: 'Music' };
    const list = el('ul', 'ar-courses-list');
    for (const folder of folders) {
      const course = folder.category === 'course';
      const row = el('li', `ar-course ar-course-${folder.state}`);
      const name = el('div', 'ar-course-name');
      name.append(strong(folderName(folder.path)));
      name.title = folder.path;
      name.append(el('span', 'ar-course-kind', KIND_LABEL[folder.category] || folder.category));
      const badge = {
        held: 'held back', excluded: 'excluded', kept: 'kept',
        review: course ? 'copied — has camera photos' : 'copied — seems personal',
      }[folder.state] || folder.state;
      name.append(el('span', 'ar-course-badge', badge));
      row.append(name);

      const facts = [];
      if (folder.files && (course || folder.state !== 'review')) {
        facts.push(`${folder.measurement_truncated ? 'At least ' : ''}${Number(folder.files).toLocaleString()} ${course ? 'files' : (folder.files === 1 ? 'file' : 'files')}, ${bytes(folder.bytes)}`);
      }
      facts.push(folder.path);
      row.append(el('div', 'hint ar-course-path', facts.join(' · ')));
      if (folder.examples?.length) {
        row.append(el('div', 'hint ar-course-path', `For example: ${folder.examples.join(' · ')}`));
      }
      row.append(el('div', 'hint ar-course-why', `Why: ${(folder.reasons || []).join('; ')}`));
      if (folder.state === 'review' && course) {
        row.append(el('div', 'hint ar-course-warn',
          (folder.camera_check_incomplete ? 'The camera-photo check is incomplete, ' :
          `Holds ${folder.camera_photos} photograph${folder.camera_photos === 1 ? '' : 's'} from a real camera, `)
          + 'so it is copied unless you exclude it.'));
      }
      if (folder.spared) {
        row.append(el('div', 'hint ar-course-spared',
          `${folder.spared} ${folder.spared === 1 ? 'file looks' : 'files look'} personal and ${folder.spared === 1 ? 'is' : 'are'} always copied`
          + ` (${(folder.spared_examples || []).join(' · ')}): ${(folder.spared_reasons || []).join('; ')}`));
      }

      const buttons = el('div', 'ar-course-actions');
      const choice = (label, decision, cls = 'btn small ghost') => {
        const button = el('button', cls, label);
        button.type = 'button';
        button.onclick = () => this.decideCourseFolders([[folder.path, decision, folder.category]], job);
        return button;
      };
      // Nothing held means only personal files were found: nothing to decide.
      if (!course && folder.state === 'review') { list.append(row); continue; }
      if (folder.state !== 'excluded') buttons.append(choice('Exclude', 'exclude', 'btn small'));
      if (folder.state !== 'kept') buttons.append(choice('Keep', 'include'));
      if (folder.state === 'excluded' || folder.state === 'kept') buttons.append(choice('Ask again', null));
      row.append(buttons);
      list.append(row);
    }
    box.append(list);
  }

  async decideCourseFolders(pairs, job) {
    const box = $('#ar-courses');
    box?.querySelectorAll('button').forEach((b) => { b.disabled = true; });
    try {
      await archiveApi.decideCourseFolders(pairs.map(([path, decision, category = 'course']) => ({ path, decision, category })));
      // Re-estimate rather than patching the numbers here: which files are
      // counted is the server's decision, and this keeps the two from drifting.
      this.estimate(job);
    } catch (exc) {
      this.toast(exc.message, true);
      box?.querySelectorAll('button').forEach((b) => { b.disabled = false; });
    }
  }

  showNotes({ problems, notices, resolution }) {
    const fix = $('#ar-resolution');
    if (resolution?.corrected) {
      fix.hidden = false;
      $('#ar-resolution-text').textContent =
        `${resolution.reason || 'That folder is inside an existing archive.'} `
        + `Using “${resolution.destination}” instead.`;
    } else {
      fix.hidden = true;
    }

    fillList('#ar-notices', '#ar-notice-list', notices);
    fillList('#ar-problems', '#ar-problem-list', problems);
  }

  /* -- running ---------------------------------------------------------- */

  async run(mode) {
    const button = { copy: $('#ar-start'), 'dry-run': $('#ar-dry'), verify: $('#ar-audit') }[mode];
    const label = button.textContent;
    button.disabled = true;
    button.textContent = 'Starting…';
    try {
      const result = await archiveApi.start(this.job(mode));
      this.showNotes({ problems: [], notices: [], resolution: result.resolution });
      if (result.destination) $('#ar-dest-input').value = result.destination;
      this.toast(result.message);
      this.loadRecent();
      // Now is the moment to ask: they have just started something that will
      // run for hours, so being told when it ends is obviously the point.
      // Asking on page load gets dismissed by reflex.
      this.askToNotify();
    } catch (exc) {
      this.showNotes({
        problems: exc.data?.problems || [exc.message],
        notices: [],
        resolution: exc.data?.resolution,
      });
      this.toast(exc.message, true);
    } finally {
      button.disabled = false;
      button.textContent = label;
      this.tick();
    }
  }

  async control(action, busy) {
    try {
      this.toast(busy);
      const result = await archiveApi[action]();
      if (result?.message) this.toast(result.message);
    } catch (exc) {
      this.toast(exc.message, true);
    }
    this.tick();
  }

  async simple(action) {
    try {
      const result = await archiveApi[action]();
      this.toast(result.message);
      this.loadRecent();
      this.loadYears();
      this.tick();
    } catch (exc) {
      this.toast(exc.message, true);
    }
  }

  async adopt() {
    const button = $('#ar-adopt');
    button.disabled = true;
    const label = button.textContent;
    button.textContent = 'Adding…';
    try {
      const result = await archiveApi.adopt(this.last?.handoff?.destination || '');
      this.toast(result.message);
      await this.onLibraryChanged();
      this.tick();
    } catch (exc) {
      this.toast(exc.message, true);
    } finally {
      button.disabled = false;
      button.textContent = label;
    }
  }

  /* A Google Photos export keeps its albums as folders. Once the import has
     run, the archive knows where every member went, so the albums can be made
     in the library without reading the export again. */
  async loadTakeoutAlbums() {
    const box = $('#ar-takeout');
    let found;
    try {
      found = (await archiveApi.takeoutAlbums()).albums || [];
    } catch { box.hidden = true; return; }
    if (!found.length) { box.hidden = true; return; }
    box.hidden = false;
    const files = found.reduce((n, a) => n + a.files, 0);
    $('#ar-takeout-title').textContent = found.length === 1
      ? `Google Photos album found: ${found[0].title}`
      : `${found.length} Google Photos albums found in the sources`;
    $('#ar-takeout-sub').textContent = `${files.toLocaleString()} photographs across `
      + found.slice(0, 4).map((a) => a.title).join(', ') + (found.length > 4 ? '…' : '')
      + '. Make them in the library, once the archive has been added and indexed.';
  }

  async makeTakeoutAlbums() {
    const button = $('#ar-takeout-make');
    button.disabled = true;
    try {
      const result = await archiveApi.recreateTakeoutAlbums();
      const added = result.albums.reduce((n, a) => n + a.added, 0);
      const left = result.unmatched
        ? ` ${result.unmatched.toLocaleString()} not indexed yet — run this again after the scan.` : '';
      this.toast(result.albums.length
        ? `${result.albums.length} albums, ${added.toLocaleString()} photographs.${left}`
        : `Nothing to add yet.${left}`, !result.albums.length);
    } catch (exc) {
      this.toast(exc.message, true);
    } finally {
      button.disabled = false;
    }
  }

  /* -- rendering -------------------------------------------------------- */

  render(data) {
    const wasRunning = this.last?.is_scanning;
    this.last = data;

    const running = data.is_scanning;
    const dry = data.job_mode === 'dry-run';
    const verifying = data.job_mode === 'verify';

    // Status line
    const status = $('#ar-status-text').parentElement;
    status.classList.toggle('busy', running);
    status.classList.toggle('done', !running && data.phase === 'done');
    status.classList.toggle('bad', !running && data.phase === 'error');
    $('#ar-status-text').textContent = statusLine(data);

    const indexer = $('#ar-indexer');
    if (data.indexer?.deferred) {
      indexer.hidden = false;
      indexer.textContent = `Library indexing paused — ${data.indexer.deferred}`;
    } else {
      indexer.hidden = true;
    }

    // Before Start as well as during a run: a job started unplugged is slowed
    // on purpose, and the time to say so is before it has begun.
    const batteryBanner = $('#ar-battery-banner');
    if (batteryBanner) {
      const onBattery = data.battery?.on_battery === true;
      batteryBanner.hidden = !onBattery;
      if (onBattery) {
        const charge = data.battery.percent != null ? ` (${data.battery.percent}%)` : '';
        $('#ar-battery-text').textContent = running
          ? `Archiving is slowed on battery${charge} and pauses at 15%. Plug in and it speeds up straight away — nothing needs restarting.`
          : `Archiving runs much slower on battery${charge} and pauses at 15%. Plug in before starting a large job.`;
      }
    }

    const resource = $('#ar-resource');
    const pacing = data.pacing || {};
    if (running && pacing.mode && pacing.mode !== 'full-speed') {
      resource.hidden = false;
      resource.textContent = pacing.mode === 'paused'
        ? `Safety pause — ${pacing.reason}` : `Power-aware pacing — ${pacing.reason}`;
    } else if (data.power?.managed) {
      resource.hidden = false;
      resource.textContent = data.power.mode === 'performance'
        ? 'Performance mode' : 'Efficient serving';
    } else {
      resource.hidden = true;
    }

    const driveBanner = $('#ar-drive-banner');
    const waiting = data.waiting_for_drives || [];
    driveBanner.hidden = waiting.length === 0;
    if (waiting.length) {
      $('#ar-drive-text').textContent = waiting.join(' · ')
        + ' — reconnect the same drive and Ninaivu will resume automatically.';
    }

    // Controls
    $('#ar-start').hidden = running;
    $('#ar-dry').hidden = running;
    $('#ar-audit').hidden = running;
    $('#ar-stop').hidden = !running;
    $('#ar-pause').hidden = !running;
    $('#ar-pause').textContent = data.is_paused ? 'Resume' : 'Pause';

    // Progress
    const progress = $('#ar-progress');
    const total = data.total_files || 0;
    const done = data.processed || 0;
    progress.hidden = !running && !total;
    if (!progress.hidden) {
      const pct = total ? Math.min(100, (done / total) * 100) : 0;
      $('#ar-fill').style.width = `${pct}%`;
      // On a resume the walk meets finished files mixed in with new ones.
      // One number for both made a correct resume look like the archive
      // being copied again, so the two are told apart.
      const stepped = Number(data.stepped_over || 0);
      const fresh = Math.max(0, done - stepped);
      $('#ar-count').textContent = stepped
        ? `${done.toLocaleString()} of ${total.toLocaleString()} files checked · `
          + `${fresh.toLocaleString()} new this run · `
          + `${stepped.toLocaleString()} already done, stepped over`
        : `${done.toLocaleString()} of ${total.toLocaleString()} files`;
      $('#ar-eta').textContent = data.eta_seconds != null
        ? `about ${duration(data.eta_seconds)} left` : '';
      $('#ar-bytes').textContent = data.bytes_copied
        ? `${bytes(data.bytes_copied)} copied` : '';
    }

    // Resuming.
    //
    // Start has always been Resume — progress lives in the archive's database,
    // so pressing Start after a power cut continues rather than starting over.
    // Saying so is the whole point of this banner: without it, a run picking
    // up three hundred thousand files in looks exactly like one beginning, and
    // the only clue is a counter moving impossibly fast for a while.
    const resume = $('#ar-resume-banner');
    // Only while it is actually running. The engine remembers the last job's
    // resume count long after it ended, and a banner still announcing a resume
    // over a finished run is a stale claim about the present.
    resume.hidden = !(running && data.is_resume && !dry);
    if (!resume.hidden) {
      // `resumed_from` is the whole archive — other sources, files since
      // deleted — so it can exceed this run's total. The run's own count, when
      // the engine took one, is the number that belongs next to the total.
      const own = data.finished_in_run;
      const before = Number(own != null ? own : data.resumed_from || 0);
      const all = total || 0;
      $('#ar-resume-text').textContent = all && before <= all
        ? `${before.toLocaleString()} of ${all.toLocaleString()} files were `
          + 'already done by an earlier run. They are checked and stepped over, '
          + 'which is why the count moves quickly at first.'
        : `${before.toLocaleString()} files were already done by an earlier `
          + 'run and will be stepped over.';
    }

    $('#ar-dry-banner').hidden = !dry;

    // A dry run predicts rather than does, so the cards must not claim files
    // were verified when nothing has been written.
    $('#ar-label-verified').textContent = dry ? 'Would archive'
      : verifying ? 'Verified' : 'Verified';
    $('#ar-verified').textContent = (dry ? data.planned : data.verified || 0).toLocaleString();
    $('#ar-duplicates').textContent =
      (dry ? data.plan_duplicates : data.duplicates || 0).toLocaleString();
    $('#ar-skipped').textContent =
      (dry ? data.plan_skipped : data.skipped || 0).toLocaleString();
    const errors = data.errors || 0;
    $('#ar-errors').textContent = errors.toLocaleString();
    // Amber only when there is something to be amber about: a standing "0"
    // in a warning colour teaches people to ignore the colour.
    $('#ar-errors-card').classList.toggle('warn', errors > 0);

    this.renderGuardian(data.guardian || {}, running);

    this.renderHandoff(data.handoff);
    if (data.handoff?.available && !this._takeoutChecked) {
      this._takeoutChecked = true;
      this.loadTakeoutAlbums();
    }

    this.showInTitle(data);

    // A run that just finished has new files and new years to show.
    if (wasRunning && !running) {
      this.loadRecent();
      this.loadYears();
      this.toast(dry ? 'Dry run finished.' : 'Run finished.');
      this.announceFinish(data);
    }
  }

  renderGuardian(health, archiveRunning) {
    const panel = $('#ar-health');
    const state = health.running ? 'checking' : (health.status || 'no-archive');
    panel.dataset.status = state;
    $('#ar-health-status').textContent = {
      healthy: 'Healthy', warning: 'Attention', critical: 'Action needed',
      checking: 'Checking…', 'no-archive': 'Not checked',
    }[state] || state;
    $('#ar-health-checked').textContent = health.checked != null
      ? `${Number(health.matched || 0).toLocaleString()} of ${Number(health.checked).toLocaleString()} matched`
      : 'No sample yet';
    $('#ar-health-space').textContent = health.free_bytes != null
      ? `${bytes(health.free_bytes)} free · ${health.free_percent}%` : 'Storage unavailable';
    $('#ar-health-last').textContent = health.checked_at
      ? new Date(health.checked_at * 1000).toLocaleString() : 'Never';
    $('#ar-health-message').textContent = health.message ||
      'A rotating daily sample catches missing, changed, or unreadable archive files.';
    $('#ar-health-run').disabled = Boolean(health.running || archiveRunning);

    const change = Number(health.change_id || 0);
    if (this.healthChangeId == null) {
      this.healthChangeId = change;
    } else if (change !== this.healthChangeId) {
      this.healthChangeId = change;
      this.toast(`Archive health changed: ${health.message || state}`,
        state === 'critical');
      if (typeof Notification !== 'undefined' && Notification.permission === 'granted') {
        try {
          new Notification('Ninaivu — archive health changed', {
            body: health.message || state,
            tag: 'ninaivu-archive-health',
          });
        } catch { /* advisory only */ }
      }
    }
  }

  /* -- telling somebody who is not looking ------------------------------ */

  /**
   * Put the run in the tab title.
   *
   * A consolidation runs for hours behind eleven other tabs. The favicon and
   * the first few characters of the title are all that is visible of it, so
   * that is where the percentage belongs. Restored exactly when the run ends,
   * because a title stuck at 96% is worse than no title at all.
   */
  showInTitle(data) {
    if (this.plainTitle == null) this.plainTitle = document.title;

    if (!data.is_scanning) {
      document.title = this.plainTitle;
      return;
    }
    const total = data.total_files || 0;
    const done = data.processed || 0;
    const pct = total ? Math.min(100, Math.floor((done / total) * 100)) : 0;
    const what = { 'dry-run': 'dry run', verify: 'audit' }[data.job_mode] || 'archiving';
    document.title = data.is_paused
      ? `Paused · ${this.plainTitle}`
      : `${pct}% ${what} · ${this.plainTitle}`;
  }

  /**
   * Raise a browser notification when a run ends.
   *
   * Permission is asked for when a run is *started*, not on page load: a
   * prompt that arrives before you have done anything gets dismissed out of
   * reflex, and then the one thing it was for cannot happen. Nothing here
   * fails if permission was refused — the toast and the panel still say
   * everything this does.
   */
  announceFinish(data) {
    if (typeof Notification === 'undefined') return;
    if (Notification.permission !== 'granted') return;
    // The run that just ended, so a second poll of the same finished job
    // cannot notify twice.
    const stamp = `${data.phase}:${data.processed}:${data.verified}:${data.errors}`;
    if (stamp === this.announced) return;
    this.announced = stamp;

    const dry = data.job_mode === 'dry-run';
    const bad = data.phase === 'error' || (data.errors || 0) > 0;
    const counted = (n) => Number(n || 0).toLocaleString();

    let body;
    if (bad) {
      body = `${counted(data.errors)} files could not be archived. `
        + 'Open the console to see which.';
    } else if (dry) {
      body = `${counted(data.planned)} files would be archived, `
        + `${counted(data.plan_duplicates)} are duplicates. Nothing was written.`;
    } else {
      body = `${counted(data.verified)} files archived and verified`
        + (data.duplicates ? `, ${counted(data.duplicates)} duplicates skipped` : '')
        + '.';
    }

    try {
      new Notification(
        bad ? 'Ninaivu — the run stopped with errors' : 'Ninaivu — the run has finished',
        // One tag, so a machine left alone all weekend does not build a stack
        // of these; each replaces the last.
        { body, tag: 'ninaivu-archive-run' },
      );
    } catch { /* a refused or unavailable notification is not a failure */ }
  }

  /** Ask once, when a run starts — never on page load. */
  askToNotify() {
    if (typeof Notification === 'undefined') return;
    if (Notification.permission !== 'default') return;
    try {
      Notification.requestPermission().catch(() => {});
    } catch { /* older browsers took a callback; not worth the branch */ }
  }

  renderHandoff(handoff) {
    const box = $('#ar-handoff');
    if (!handoff || !handoff.available) { box.hidden = true; return; }

    box.hidden = false;
    const settled = handoff.in_library;
    box.classList.toggle('settled', settled);
    $('#ar-handoff-title').textContent = settled
      ? 'This archive is in your library'
      : 'Add this archive to your library?';
    $('#ar-handoff-sub').textContent = settled
      ? `${handoff.destination} — Ninaivu indexes it, so the household can browse it.`
      : `${handoff.destination} holds ${handoff.verified.toLocaleString()} verified files. `
        + 'Adding it lets Ninaivu index it so the household can browse it.';
    $('#ar-adopt').hidden = settled;
  }

  async loadRecent() {
    const body = $('#ar-tbody');
    let rows;
    try {
      rows = await archiveApi.recent(this.filter);
    } catch { return; }

    body.innerHTML = '';
    if (!rows.length) {
      const empty = el('tr');
      const cell = el('td', 'ar-empty', 'Nothing here yet');
      cell.colSpan = 3;
      empty.appendChild(cell);
      body.appendChild(empty);
      return;
    }

    for (const row of rows) {
      const tr = el('tr');
      const name = el('td', null, row.filename || row.source_path || '');
      name.title = row.source_path || '';
      tr.appendChild(name);

      const state = el('td');
      state.appendChild(el('span', `ar-state ${row.status}`, stateLabel(row.status)));
      tr.appendChild(state);

      tr.appendChild(el('td', null, detail(row)));
      body.appendChild(tr);
    }
  }

  async loadYears() {
    let years;
    try {
      years = await archiveApi.years();
    } catch { return; }

    const box = $('#ar-years');
    const chart = $('#ar-years-chart');
    if (!years.length) { box.hidden = true; return; }

    box.hidden = false;
    chart.innerHTML = '';
    const peak = Math.max(...years.map((y) => y.count), 1);
    for (const year of years) {
      const column = el('div', 'ar-year');
      column.title = `${year.year}: ${year.count.toLocaleString()} files`;
      const bar = el('i');
      bar.style.height = `${Math.max(3, (year.count / peak) * 100)}%`;
      column.appendChild(bar);
      column.appendChild(el('span', null, year.year));
      chart.appendChild(column);
    }
  }
}

/* -- helpers ------------------------------------------------------------- */

/** The last part of a path, whichever separator the drive uses. */
function folderName(path) {
  const text = String(path || '').replace(/[\\/]+$/, '');
  const cut = Math.max(text.lastIndexOf('/'), text.lastIndexOf('\\'));
  return cut >= 0 ? text.slice(cut + 1) : text;
}

function strong(text) {
  const node = document.createElement('strong');
  node.textContent = text;
  return node;
}

function fillList(boxSel, listSel, items) {
  const box = $(boxSel);
  const list = $(listSel);
  if (!items || !items.length) { box.hidden = true; return; }
  box.hidden = false;
  list.innerHTML = '';
  for (const item of items) list.appendChild(el('li', null, item));
}

function statusLine(data) {
  if (data.waiting_for_drives?.length) return 'Drive disconnected — waiting safely';
  if (data.pacing?.mode === 'paused') return `Safety pause — ${data.pacing.reason}`;
  if (data.is_paused) return 'Paused';
  if (data.is_scanning) {
    const what = { 'dry-run': 'Dry run', verify: 'Auditing the archive' }[data.job_mode]
      || 'Consolidating';
    return data.job_message ? `${what} — ${data.job_message}` : `${what}…`;
  }
  if (data.phase === 'done') return data.job_message || 'Finished';
  if (data.phase === 'error') return data.job_message || 'Stopped with errors';
  if (data.total_scanned) return 'Ready — Start also resumes';
  return 'Ready';
}

function stateLabel(status) {
  return {
    verified: 'verified', duplicate: 'duplicate', skipped: 'skipped',
    error: 'error', pending: 'queued', copying: 'copying', copied: 'copied',
    planned: 'would copy', 'plan-duplicate': 'would skip', 'plan-skip': 'would skip',
  }[status] || status;
}

function detail(row) {
  if (row.error) return row.error;
  if (row.duplicate_of) return `same bytes as ${row.duplicate_of}`;
  if (row.destination_path) {
    return PLAN_STATES.has(row.status)
      ? `→ ${row.destination_path}` : row.destination_path;
  }
  if (row.exif_date) return `${row.exif_date} (${row.date_source || 'date'})`;
  return '';
}

function bytes(n) {
  const value = Number(n) || 0;
  if (value < 1024) return `${value} B`;
  const units = ['KB', 'MB', 'GB', 'TB', 'PB'];
  let size = value / 1024;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
  return `${size < 10 ? size.toFixed(1) : Math.round(size)} ${units[unit]}`;
}

function duration(seconds) {
  const s = Math.max(0, Math.round(Number(seconds) || 0));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.round(s / 60)} min`;
  const hours = Math.floor(s / 3600);
  const minutes = Math.round((s % 3600) / 60);
  return minutes ? `${hours}h ${minutes}m` : `${hours}h`;
}

/** Windows is case-insensitive and both separators reach the same folder. */
function samePath(a, b) {
  const key = (p) => String(p).replace(/\\/g, '/').replace(/\/+$/, '');
  const norm = (p) => (/^[a-zA-Z]:/.test(key(p)) ? key(p).toLowerCase() : key(p));
  return norm(a) === norm(b);
}
