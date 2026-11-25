/**
 * The AI models tab — optional models downloaded onto this machine.
 *
 * Self-contained like the other panels: the console hands it a toast function
 * and it owns everything under `[data-panel="ai-models"]`. It polls only while
 * it is on screen and a download is running.
 */

import { reportUnauthorized } from './api.js';
import * as i18n from './i18n.js';

const $ = (sel) => document.querySelector(sel);

const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
};

async function json(url, options = {}) {
  const response = await fetch(url, { headers: { Accept: 'application/json' }, ...options });
  const text = await response.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = null; }
  if (!response.ok) {
    if (response.status === 401) reportUnauthorized(url);
    throw new Error(i18n.fromServer(data, response.statusText));
  }
  return data;
}

const megabytes = (bytes) => `${(bytes / 1024 / 1024).toLocaleString(undefined, { maximumFractionDigits: 1 })} MB`;

export class AIModelsPanel {
  constructor({ toast }) {
    this.toast = toast || (() => {});
    this.timer = null;
    this.visible = false;
    this.wasDownloading = new Set();
    this.data = null;
    i18n.onChange(() => {
      if (this.visible && this.data) { clearTimeout(this.timer); this.render(this.data); }
    });
  }

  async show() {
    this.visible = true;
    await this.refresh();
  }

  hide() {
    this.visible = false;
    clearTimeout(this.timer);
    this.timer = null;
  }

  async refresh() {
    clearTimeout(this.timer);
    try {
      this.render(await json('/api/admin/ai-models'));
    } catch (error) {
      this.toast(i18n.t('Could not load the AI models: {reason}', { reason: error.message }), true);
    }
  }

  // One call for all three things somebody can ask for: a model they have
  // not got, a newer version of one they have, and the same one again when
  // the file on disk is the right size and the wrong bytes.
  async download(model, { force = false } = {}) {
    const size = megabytes(model.bytes);
    const asking = force
      ? `${i18n.t('Download {name} again ({size})?', { name: i18n.t(model.label), size })}\n\n${i18n.t('The files on disk will be replaced.')}`
      : model.update_available
        ? i18n.t('Update {name} to version {version} ({size}) from {source}?', {
          name: i18n.t(model.label), version: model.version, size, source: model.source })
        : `${i18n.t('Download {name} ({size}) from {source}?', { name: i18n.t(model.label), size, source: model.source })}`
          + `\n\n${i18n.t('Licence: {licence}', { licence: model.licence })}`;
    if (!window.confirm(asking)) return;
    try {
      this.render(await json(`/api/admin/ai-models/${encodeURIComponent(model.id)}/download`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ force }),
      }));
      this.toast(i18n.t('Downloading {name}…', { name: i18n.t(model.label) }));
    } catch (error) {
      this.toast(error.message, true);
    }
  }

  // Everything the features that are on need, one download after another:
  // the Extras (packages and tools) they run on first in line, then the models.
  async downloadNeeded() {
    const needed = this.data?.needed;
    const extras = needed?.extras || { ids: [], labels: {} };
    if (!needed?.ids.length && !extras.ids.length) return;
    const names = needed.ids.map((id) => {
      const model = this.data.models.find((m) => m.id === id);
      return `• ${i18n.t(model ? model.label : id)}`;
    }).join('\n');
    const more = extras.ids.map((id) => `• ${i18n.t(extras.labels[id] || id)}`).join('\n');
    let asking = needed.ids.length
      ? `${i18n.t('Download {count} models ({size}), one after another?', {
        count: needed.ids.length, size: megabytes(needed.bytes) })}\n\n${names}`
      : i18n.t('Install the extras the models run on, one after another?');
    if (more) {
      asking += `\n\n${i18n.t('And install these extras from Home & extensions first, one after another:')}\n${more}`;
    }
    if (!window.confirm(asking)) return;
    try {
      this.render(await json('/api/admin/ai-models/download-needed', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
      }));
      this.toast(i18n.t('Downloading the needed models, one after another…'));
    } catch (error) {
      this.toast(error.message, true);
    }
  }

  // Deleting a model leaves everything else as it was: the feature it served
  // says it is not installed until it is downloaded again.
  async remove(model, { waiting = false } = {}) {
    const asking = `${i18n.t('Delete {name} from this computer ({size})?', {
      name: i18n.t(model.label), size: megabytes(model.on_disk_bytes || model.bytes) })}\n\n${
      model.essential
        ? i18n.t('The gallery keeps working; this one feature stops until the model is downloaded again.')
        : i18n.t('Everything else keeps working; this feature stops until the model is downloaded again.')}`;
    // Taking a model out of the queue deletes nothing worth asking about.
    if (!waiting && !window.confirm(asking)) return;
    try {
      this.render(await json(`/api/admin/ai-models/${encodeURIComponent(model.id)}`, {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
      }));
      this.toast(waiting
        ? i18n.t('{name} will not be downloaded.', { name: i18n.t(model.label) })
        : i18n.t('{name} was deleted.', { name: i18n.t(model.label) }));
    } catch (error) {
      this.toast(error.message, true);
      this.refresh();
    }
  }

  renderNeeded(data) {
    const box = $('#am-needed');
    if (!box) return;
    const needed = data.needed || { ids: [], bytes: 0, skipped: [] };
    const waiting = (data.queued || []).length
      + data.models.filter((m) => (m.download || {}).status === 'downloading').length;
    const button = $('#am-needed-go');
    const hint = $('#am-needed-hint');
    const pending = needed.ids.filter((id) => !(data.queued || []).includes(id)
      && (data.models.find((m) => m.id === id)?.download || {}).status !== 'downloading');
    const extras = (needed.extras || { ids: [] }).ids;
    box.hidden = !needed.ids.length && !waiting && !needed.skipped.length && !extras.length;
    button.hidden = !pending.length && !extras.length;
    button.onclick = () => this.downloadNeeded();
    const lines = [];
    if (pending.length) {
      lines.push(i18n.t('{count} models for the features that are on ({size}), downloaded one after another.', {
        count: pending.length, size: megabytes(needed.bytes) }));
    }
    if (extras.length) {
      lines.push(i18n.t('{count} extras from Home & extensions come with them, installed first.', {
        count: extras.length }));
    }
    if (!pending.length && !extras.length) {
      lines.push(waiting
        ? i18n.t('Downloading, one after another: {count} to go.', { count: waiting })
        : i18n.t('Every model the features that are on need is here.'));
    }
    if (needed.skipped.length) {
      lines.push(i18n.t('Not included, because Python packages are missing: {names}.', {
        names: needed.skipped.map((s) => i18n.t(s.label)).join(', ') }));
    }
    hint.textContent = lines.join(' ');
  }

  render(data) {
    this.data = data;
    const list = $('#am-models');
    list.replaceChildren();
    let downloading = false;
    let installed = 0;
    for (const model of data.models) {
      const state = model.download || {};
      const busy = state.status === 'downloading';
      downloading ||= busy;
      if (model.installed) installed += 1;
      if (this.wasDownloading.has(model.id) && !busy) {
        this.wasDownloading.delete(model.id);
        if (state.status === 'installed') this.toast(i18n.t('{name} is installed.', { name: i18n.t(model.label) }));
        if (state.status === 'failed') this.toast(i18n.t('{name} could not be downloaded: {reason}', { name: i18n.t(model.label), reason: state.error }), true);
      }
      if (busy) this.wasDownloading.add(model.id);

      const row = el('div', 'am-model');
      row.dataset.modelId = model.id;           // so a switch above can point here
      const what = el('div', 'what');
      const name = el('strong', null, i18n.t(model.label));
      if (model.essential) {
        // What the gallery's own features need — faces, straightening — as
        // opposed to the Playground's editing models. A household that never
        // opens the editor still wants these.
        name.append(el('span', 'am-essential', i18n.t('Needed by the gallery')));
      }
      what.append(name, el('div', 'hint subtle', i18n.t(model.used_for)));
      const side = el('div', 'am-status');
      const queued = state.status === 'queued';
      const deleteButton = () => {
        const button = el('button', 'btn small ghost danger', i18n.t('Delete'));
        button.onclick = () => this.remove(model);
        return button;
      };
      if (model.update_available && !busy) {
        const actions = el('div', 'am-actions');
        const button = el('button', 'btn small primary',
          i18n.t('Update to version {version}', { version: model.version }));
        button.onclick = () => this.download(model);
        actions.append(button, deleteButton());
        side.append(actions);
      } else if (model.installed && !busy) {
        side.classList.add('good');
        side.append(el('div', null, i18n.t('Installed')));
        // Quiet, because it is rarely the answer — but it is the only way out
        // of a model whose file is the right length and the wrong bytes.
        const actions = el('div', 'am-actions');
        const again = el('button', 'btn small ghost', i18n.t('Download again'));
        again.onclick = () => this.download(model, { force: true });
        actions.append(again, deleteButton());
        side.append(actions);
      } else if (queued) {
        side.append(el('div', null, i18n.t('Waiting to download')));
        const cancel = el('button', 'btn small ghost', i18n.t('Cancel'));
        cancel.onclick = () => this.remove(model, { waiting: true });
        side.append(cancel);
      } else if (busy) {
        const bar = el('progress');
        bar.max = state.total_bytes || model.bytes;
        bar.value = state.done_bytes || 0;
        side.append(bar, el('div', null, i18n.t('{done} of {total}', { done: megabytes(state.done_bytes || 0), total: megabytes(bar.max) })));
      } else {
        const actions = el('div', 'am-actions');
        const button = el('button', 'btn small', i18n.t('Download · {size}', { size: megabytes(model.bytes) }));
        button.onclick = () => this.download(model);
        actions.append(button);
        // Part of it on disk (a download stopped half way): that can go too.
        if (model.on_disk_bytes > 0) actions.append(deleteButton());
        side.append(actions);
      }
      row.append(what, side);
      const meta = i18n.t('Licence: {licence}. Source: {source}. Runs on: {runs_on}.', {
        licence: model.licence, source: model.source, runs_on: i18n.t(model.runs_on) });
      row.append(el('div', 'meta', model.installed && model.installed_version
        ? `${meta} ${i18n.t('Version {version} installed.', { version: model.installed_version })}` : meta));
      if (state.status === 'failed' && !model.installed) {
        row.append(el('div', 'meta bad', i18n.t('The last download failed: {reason}', { reason: state.error })));
      }
      if (model.missing_packages.length) {
        const packages = model.missing_packages.join(', ');
        const note = el('div', 'meta bad', `${model.missing_packages.length > 1
          ? i18n.t('Needs the Python packages {packages}. On this machine, run:', { packages })
          : i18n.t('Needs the Python package {packages}. On this machine, run:', { packages })} `);
        model.install_hints.forEach((hint, index) => {
          if (index) note.append(` ${i18n.t('and')} `);
          note.append(el('code', null, hint));
        });
        row.append(note);
      }
      list.append(row);
    }
    const summary = [i18n.t('{installed} of {total} installed', { installed, total: data.models.length })];
    if (data.packages.graphics_card) summary.push(i18n.t('graphics card available'));
    $('#am-summary').textContent = summary.join(' · ');
    $('#am-folder').textContent = i18n.t('Models are kept in {folder}.', { folder: data.folder });
    $('#am-restart').hidden = !data.restart_for_search;
    this.renderNeeded(data);
    if ((downloading || (data.queued || []).length) && this.visible) this.timer = setTimeout(() => this.refresh(), 1500);
  }
}
