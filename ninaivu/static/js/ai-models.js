/**
 * The AI models tab — optional models downloaded onto this machine.
 *
 * Self-contained like the other panels: the console hands it a toast function
 * and it owns everything under `[data-panel="ai-models"]`. It polls only while
 * it is on screen and a download is running.
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
  const response = await fetch(url, { headers: { Accept: 'application/json' }, ...options });
  const text = await response.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = null; }
  if (!response.ok) {
    if (response.status === 401) reportUnauthorized(url);
    throw new Error(data?.error || response.statusText);
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
      this.toast(`Could not load the AI models: ${error.message}`, true);
    }
  }

  // One call for all three things somebody can ask for: a model they have
  // not got, a newer version of one they have, and the same one again when
  // the file on disk is the right size and the wrong bytes.
  async download(model, { force = false } = {}) {
    const size = megabytes(model.bytes);
    const asking = force
      ? `Download ${model.label} again (${size})?\n\nThe files on disk will be replaced.`
      : model.update_available
        ? `Update ${model.label} to version ${model.version} (${size}) from ${model.source}?`
        : `Download ${model.label} (${size}) from ${model.source}?\n\nLicence: ${model.licence}`;
    if (!window.confirm(asking)) return;
    try {
      this.render(await json(`/api/admin/ai-models/${encodeURIComponent(model.id)}/download`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ force }),
      }));
      this.toast(`Downloading ${model.label}…`);
    } catch (error) {
      this.toast(error.message, true);
    }
  }

  render(data) {
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
        if (state.status === 'installed') this.toast(`${model.label} is installed.`);
        if (state.status === 'failed') this.toast(`${model.label} could not be downloaded: ${state.error}`, true);
      }
      if (busy) this.wasDownloading.add(model.id);

      const row = el('div', 'am-model');
      const what = el('div', 'what');
      const name = el('strong', null, model.label);
      if (model.essential) {
        // What the gallery's own features need — faces, straightening — as
        // opposed to the Playground's editing models. A household that never
        // opens the editor still wants these.
        name.append(el('span', 'am-essential', 'Needed by the gallery'));
      }
      what.append(name, el('div', 'hint subtle', model.used_for));
      const side = el('div', 'am-status');
      if (model.update_available && !busy) {
        const button = el('button', 'btn small primary',
          `Update to version ${model.version}`);
        button.onclick = () => this.download(model);
        side.append(button);
      } else if (model.installed && !busy) {
        side.classList.add('good');
        side.append(el('div', null, 'Installed'));
        // Quiet, because it is rarely the answer — but it is the only way out
        // of a model whose file is the right length and the wrong bytes.
        const again = el('button', 'btn small ghost', 'Download again');
        again.onclick = () => this.download(model, { force: true });
        side.append(again);
      } else if (busy) {
        const bar = el('progress');
        bar.max = state.total_bytes || model.bytes;
        bar.value = state.done_bytes || 0;
        side.append(bar, el('div', null, `${megabytes(state.done_bytes || 0)} of ${megabytes(bar.max)}`));
      } else {
        const button = el('button', 'btn small', `Download · ${megabytes(model.bytes)}`);
        button.onclick = () => this.download(model);
        side.append(button);
      }
      row.append(what, side);
      const meta = `Licence: ${model.licence}. Source: ${model.source}. Runs on: ${model.runs_on}.`;
      row.append(el('div', 'meta', model.installed && model.installed_version
        ? `${meta} Version ${model.installed_version} installed.` : meta));
      if (state.status === 'failed' && !model.installed) {
        row.append(el('div', 'meta bad', `The last download failed: ${state.error}`));
      }
      if (model.missing_packages.length) {
        const note = el('div', 'meta bad', `Needs the Python package${model.missing_packages.length > 1 ? 's' : ''} `
          + `${model.missing_packages.join(', ')}. On this machine, run: `);
        model.install_hints.forEach((hint, index) => {
          if (index) note.append(' and ');
          note.append(el('code', null, hint));
        });
        row.append(note);
      }
      list.append(row);
    }
    $('#am-summary').textContent = `${installed} of ${data.models.length} installed`
      + (data.packages.graphics_card ? ' · graphics card available' : '');
    $('#am-folder').textContent = `Models are kept in ${data.folder}.`;
    $('#am-restart').hidden = !data.restart_for_search;
    if (downloading && this.visible) this.timer = setTimeout(() => this.refresh(), 1500);
  }
}
