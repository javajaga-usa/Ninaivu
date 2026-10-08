/**
 * The Faces tab — grouping the people in the library.
 *
 * Front end for the face endpoints in `ninaivu/api.py`. Same shape as the
 * Archive and Cloud panels: self-contained, given only a toast function by
 * the console.
 *
 * The interface is built around one idea. The matcher is deliberately
 * cautious, so it produces two kinds of thing: groups it is sure enough about
 * to show you, and guesses it is not. Naming a group is one click. Answering
 * a guess is one click. Nothing else is asked of anybody, and nothing the
 * machine decided on its own is ever presented as settled — an automatic
 * match says so, and can be undone.
 */

import { reportUnauthorized } from './api.js';
import * as i18n from './i18n.js';
import { openAskFamily } from './ask-family.js';

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

const faceThumb = (id) => `/api/faces/thumb/${id}`;

export class FacesPanel {
  constructor({ toast, familyUrl } = {}) {
    this.toast = toast || (() => {});
    this.familyUrl = familyUrl || null;
    this.visible = false;
    this.state = { status: null, clusters: [], people: [], reviewing: null };
  }

  wire() {
    $('#faces-scan')?.addEventListener('click', () => this.scan());
    $('#faces-regroup')?.addEventListener('click', () => this.regroup());
    $('#faces-download')?.addEventListener('click', () => this.download());
    $('#faces-refresh')?.addEventListener('click', () => this.refresh());
    // "Who is this?" links: the answers come back here and in the family app.
    $('#faces-ask')?.addEventListener('click', () => openAskFamily({
      toast: this.toast, familyUrl: this.familyUrl,
      onClose: () => { if (this.visible) this.refresh(); },
    }));
    // The cards are built here, so a change of language rebuilds them.
    i18n.onChange(() => { if (this.state.status) this.render(); });
  }

  show() { this.visible = true; this.refresh(); }
  hide() { this.visible = false; }

  async refresh() {
    try {
      const [status, clusters, people] = await Promise.all([
        json('/api/faces/status'),
        json('/api/faces/clusters').catch(() => ({ clusters: [] })),
        json('/api/faces/people').catch(() => ({ people: [] })),
      ]);
      this.state.status = status;
      this.state.clusters = clusters.clusters || [];
      this.state.people = people.people || [];
      this.render();
    } catch (err) {
      this.toast(i18n.t('Could not load faces: {reason}', { reason: err.message }));
    }
  }

  /* -- rendering -------------------------------------------------------- */

  render() {
    this.renderStatus();
    this.renderPeople();
    this.renderClusters();
  }

  renderStatus() {
    const status = this.state.status;
    const box = $('#faces-status');
    if (!box || !status) return;
    box.innerHTML = '';

    const engine = status.engine || {};
    if (!engine.available) {
      const warn = el('div', 'notice');
      warn.appendChild(el('strong', null, i18n.t('Face grouping is not running.')));
      warn.appendChild(el('p', 'hint', engine.reason || i18n.t('Unavailable.')));
      if ((engine.reason || '').includes('not downloaded')) {
        $('#faces-download').hidden = false;
      }
      box.appendChild(warn);
      $('#faces-scan').disabled = true;
      return;
    }
    $('#faces-scan').disabled = false;
    $('#faces-download').hidden = true;

    const stats = [
      [i18n.t('People named'), status.people],
      [i18n.t('Faces found'), status.faces],
      [i18n.t('Confirmed by you'), status.confirmed],
      [i18n.t('Photographs with faces'), status.photos_with_faces],
      [i18n.t('Still to look at'), status.photos_pending],
    ];
    const grid = el('div', 'stat-grid');
    for (const [label, value] of stats) {
      const cell = el('div', 'stat');
      cell.appendChild(el('div', 'stat-value', String(value ?? 0)));
      cell.appendChild(el('div', 'stat-label', label));
      grid.appendChild(cell);
    }
    box.appendChild(grid);
  }

  renderPeople() {
    const list = $('#faces-people');
    if (!list) return;
    list.innerHTML = '';
    this.updateKnownNames();
    if (!this.state.people.length) {
      list.appendChild(el('p', 'hint',
        i18n.t('Nobody has been named yet. Name a group below and Ninaivu will find that person across the rest of the library.')));
      return;
    }
    for (const person of this.state.people) {
      list.appendChild(this.personCard(person));
    }
  }

  /** Every named person, for the datalist that backs both name inputs —
   * the one below and the one on each unnamed group. Autocomplete is what
   * makes an existing name easy to find and spell exactly, which is what
   * turns "type a name" into "join this person" instead of a duplicate. */
  updateKnownNames() {
    const datalist = $('#faces-known-people');
    if (!datalist) return;
    datalist.innerHTML = '';
    for (const person of this.state.people) {
      const option = document.createElement('option');
      option.value = person.name;
      datalist.appendChild(option);
    }
  }

  /** One person's card. The name is renamed in place — typing a name that
   * already belongs to somebody else merges the two rather than leaving two
   * people who happen to share a name (see db.rename_person_cluster). */
  personCard(person) {
    const card = el('div', 'person-card');
    if (person.cover_face_id) {
      const img = el('img', 'person-face');
      img.src = faceThumb(person.cover_face_id);
      img.alt = person.name;
      img.loading = 'lazy';
      card.appendChild(img);
    }

    const nameRow = el('div', 'name-row');
    const nameText = el('div', 'person-name', person.name);
    const editBtn = el('button', 'btn ghost small', i18n.t('Rename'));
    editBtn.setAttribute('aria-label', i18n.t('Rename {name}', { name: person.name }));
    nameRow.append(nameText, editBtn);
    card.appendChild(nameRow);

    editBtn.onclick = () => {
      nameRow.innerHTML = '';
      // Input + Save + Cancel rarely fit on one line inside the narrow
      // person card, so this row wraps — see .name-row.editing in admin.css.
      nameRow.classList.add('editing');
      const input = el('input', 'input small');
      input.value = person.name;
      input.setAttribute('list', 'faces-known-people');
      input.setAttribute('aria-label', i18n.t('New name for {name}', { name: person.name }));
      const save = el('button', 'btn primary small', i18n.t('Save'));
      const cancel = el('button', 'btn ghost small', i18n.t('Cancel'));
      const restore = () => this.renderPeople();
      const commit = async () => {
        const name = input.value.trim();
        if (!name || name === person.name) { restore(); return; }
        save.disabled = true;
        cancel.disabled = true;
        try {
          const result = await json(`/api/faces/person/${person.id}/rename`, {
            method: 'POST', body: { name },
          });
          this.toast(result.merged
            ? i18n.t('"{old}" is the same person as "{name}" — merged.', { old: person.name, name: result.person.name })
            : i18n.t('Renamed to "{name}".', { name: result.person.name }));
          await this.refresh();
        } catch (err) {
          this.toast(i18n.t('Could not rename: {reason}', { reason: err.message }));
          restore();
        }
      };
      save.onclick = commit;
      cancel.onclick = restore;
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') commit();
        if (e.key === 'Escape') restore();
      });
      nameRow.append(input, save, cancel);
      input.focus();
      input.select();
    };

    card.appendChild(el('div', 'hint', person.photo_count === 1
      ? i18n.t('1 photograph')
      : i18n.t('{count} photographs', { count: person.photo_count.toLocaleString(i18n.locale()) })));

    const review = el('button', 'btn ghost small', i18n.t('Review suggestions'));
    review.onclick = () => this.review(person);
    card.appendChild(review);
    return card;
  }

  renderClusters() {
    const list = $('#faces-clusters');
    if (!list) return;
    list.innerHTML = '';
    if (!this.state.clusters.length) {
      list.appendChild(el('p', 'hint',
        i18n.t('No unnamed groups. Run a scan if you have added photographs since the last one.')));
      return;
    }
    for (const cluster of this.state.clusters) {
      const card = el('div', 'person-card');
      if (cluster.cover?.id) {
        const img = el('img', 'person-face');
        img.src = faceThumb(cluster.cover.id);
        img.alt = i18n.t('Unnamed person');
        img.loading = 'lazy';
        card.appendChild(img);
      }
      card.appendChild(el('div', 'hint',
        i18n.t('{faces} faces in {count} photographs', { faces: cluster.size, count: cluster.photo_count })));

      const row = el('div', 'name-row');
      const input = el('input', 'input small');
      input.placeholder = i18n.t('Who is this?');
      input.setAttribute('aria-label', i18n.t('Name for this group'));
      // Typing an existing name here is not a mistake — it is how you tell
      // Ninaivu these faces are somebody already named, and the datalist is
      // what makes that name easy to find and spell exactly.
      input.setAttribute('list', 'faces-known-people');
      const save = el('button', 'btn primary small', i18n.t('Name'));
      const commit = async () => {
        const name = input.value.trim();
        if (!name) return;
        save.disabled = true;
        try {
          const result = await json('/api/faces/clusters/name', {
            method: 'POST',
            body: { cluster_key: cluster.cluster_key, name },
          });
          this.toast(result.joined_existing
            ? i18n.t('{count} faces added to {name}.', { count: result.faces, name: result.person.name })
            : i18n.t('{name}: {count} faces confirmed.', { count: result.faces, name: result.person.name }));
          await this.regroup(true);
        } catch (err) {
          this.toast(i18n.t('Could not save that name: {reason}', { reason: err.message }));
          save.disabled = false;
          // Most often the group was replaced by the regroup another naming
          // started. The groups on screen are stale, so show the current ones.
          await this.refresh();
        }
      };
      save.onclick = commit;
      input.addEventListener('keydown', (e) => { if (e.key === 'Enter') commit(); });
      row.append(input, save);
      card.appendChild(row);
      list.appendChild(card);
    }
  }

  /* -- the review queue -------------------------------------------------- */

  async review(person) {
    const modal = $('#faces-review');
    const body = $('#faces-review-body');
    if (!modal || !body) return;
    $('#faces-review-title').textContent = i18n.t('Is this {name}?', { name: person.name });
    body.innerHTML = '';
    body.appendChild(el('p', 'hint', i18n.t('Loading suggestions…')));
    modal.hidden = false;

    let data;
    try {
      data = await json(`/api/faces/suggestions/${person.id}`);
    } catch (err) {
      body.innerHTML = '';
      body.appendChild(el('p', 'hint', i18n.t('Could not load suggestions: {reason}', { reason: err.message })));
      return;
    }

    const queue = data.suggestions || [];
    body.innerHTML = '';
    if (!queue.length) {
      body.appendChild(el('p', 'hint',
        i18n.t('Nothing to review — every face Ninaivu is unsure about has been answered.')));
      return;
    }

    const hint = el('p', 'hint', queue.length === 1
      ? i18n.t('1 face Ninaivu thinks may be {name}. Each answer makes the next guess better.', { name: person.name })
      : i18n.t('{count} faces Ninaivu thinks may be {name}. Each answer makes the next guess better.', { count: queue.length, name: person.name }));
    body.appendChild(hint);

    const grid = el('div', 'face-grid');
    for (const item of queue) {
      const cell = el('div', 'face-cell');
      const img = el('img', 'person-face');
      img.src = faceThumb(item.face_id);
      img.alt = i18n.t('Face awaiting review');
      img.loading = 'lazy';
      cell.appendChild(img);
      cell.appendChild(el('div', 'hint', i18n.t('{percent}% alike', { percent: Math.round(item.score * 100) })));

      const actions = el('div', 'face-actions');
      const yes = el('button', 'btn primary small', i18n.t('Yes'));
      const no = el('button', 'btn ghost small', i18n.t('No'));
      const answer = async (confirmed) => {
        yes.disabled = no.disabled = true;
        try {
          await json(confirmed ? '/api/faces/confirm' : '/api/faces/reject', {
            method: 'POST',
            body: { face_id: item.face_id, person_id: person.id },
          });
          cell.classList.add(confirmed ? 'answered-yes' : 'answered-no');
          cell.querySelector('.face-actions').replaceChildren(
            el('span', 'hint', confirmed ? i18n.t('Confirmed') : i18n.t('Not them')));
        } catch (err) {
          this.toast(i18n.t('Could not save that: {reason}', { reason: err.message }));
          yes.disabled = no.disabled = false;
        }
      };
      yes.onclick = () => answer(true);
      no.onclick = () => answer(false);
      actions.append(yes, no);
      cell.appendChild(actions);
      grid.appendChild(cell);
    }
    body.appendChild(grid);
  }

  /* -- actions ----------------------------------------------------------- */

  async download() {
    const button = $('#faces-download');
    button.disabled = true;
    button.textContent = i18n.t('Downloading…');
    try {
      const result = await json('/api/faces/models', { method: 'POST' });
      if (result.errors?.length) this.toast(result.errors.join('; '));
      else this.toast(i18n.t('Face models downloaded.'));
      await this.refresh();
    } catch (err) {
      this.toast(i18n.t('Download failed: {reason}', { reason: err.message }));
    } finally {
      button.disabled = false;
      button.textContent = i18n.t('Download face models');
    }
  }

  async scan() {
    const button = $('#faces-scan');
    button.disabled = true;
    button.textContent = i18n.t('Looking for faces…');
    try {
      const result = await json('/api/faces/scan', { method: 'POST' });
      if (!result.ok) this.toast(result.reason || i18n.t('Face scan could not run.'));
      else if (result.background) {
        // The scan does it now, in the background — the answer is only that
        // it has begun, and the progress is on Activity like any other job.
        const n = (result.pending || 0).toLocaleString(i18n.locale());
        this.toast(!result.pending
          ? i18n.t('Every photograph has already been looked at.')
          : result.already_running
            ? i18n.t('A scan is already running; it looks at the {count} photographs still to do when it gets there. Progress is on Activity.', { count: n })
            : i18n.t('Looking for faces in {count} photographs in the background. Progress is on Activity.', { count: n }));
      } else {
        const grouped = result.grouping || {};
        this.toast(i18n.t('Looked at {count} photographs, found {faces} faces. {matched} matched somebody already named.',
          { count: result.scanned, faces: result.faces, matched: grouped.auto_assigned || 0 }));
      }
      await this.refresh();
    } catch (err) {
      this.toast(i18n.t('Face scan failed: {reason}', { reason: err.message }));
    } finally {
      button.disabled = false;
      button.textContent = i18n.t('Look for faces');
    }
  }

  async regroup(quiet = false) {
    try {
      const result = await json('/api/faces/regroup', { method: 'POST' });
      if (!quiet) {
        this.toast(i18n.t('{groups} groups, {matched} matched to someone named.',
          { groups: result.clusters, matched: result.auto_assigned }));
      }
      await this.refresh();
    } catch (err) {
      this.toast(i18n.t('Could not regroup: {reason}', { reason: err.message }));
    }
  }
}
