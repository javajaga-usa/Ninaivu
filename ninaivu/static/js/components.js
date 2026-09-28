/**
 * The Extras tab — what Ninaivu can run without, installed from here.
 *
 * Self-contained like the other panels: the console hands it a toast function
 * and it owns everything under `[data-panel="extras"]`. It polls only while it
 * is on screen and an install is running, and it shows the installer's own
 * output, because "it failed" without the reason is one more thing to go and
 * find out.
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
    throw Object.assign(new Error(data?.error || response.statusText), { data });
  }
  return data;
}

export class ComponentsPanel {
  constructor({ toast }) {
    this.toast = toast || (() => {});
    this.timer = null;
    this.visible = false;
    this.wasInstalling = new Set();
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
      this.render(await json('/api/admin/components'));
    } catch (error) {
      this.toast(`Could not read what is installed: ${error.message}`, true);
    }
  }

  async install(item) {
    if (!window.confirm(`Install ${item.label} on this computer?\n\n${item.command}`)) return;
    try {
      await json(`/api/admin/components/${encodeURIComponent(item.id)}/install`,
                 { method: 'POST' });
      this.toast(`Installing ${item.label}…`);
    } catch (error) {
      this.toast(error.message, true);
    }
    await this.refresh();
  }

  render(data) {
    const list = $('#ex-list');
    if (!list) return;
    list.replaceChildren();
    let busy = false;
    let missing = 0;

    for (const item of data.components || []) {
      busy ||= item.installing;
      if (!item.installed) missing += 1;
      // Say how it ended, once, rather than leaving the row to be compared
      // with what it said a moment ago.
      if (this.wasInstalling.has(item.id) && !item.installing) {
        this.wasInstalling.delete(item.id);
        if (item.installed || item.status === 'installed') {
          this.toast(item.needs_restart
            ? `${item.label} is installed — restart Ninaivu to use it.`
            : `${item.label} is installed.`);
        } else if (item.status === 'failed') {
          this.toast(`${item.label} could not be installed: ${item.error}`, true);
        }
      }
      if (item.installing) this.wasInstalling.add(item.id);

      const row = el('div', 'am-model');
      const what = el('div', 'what');
      what.append(el('strong', null, item.label),
                  el('div', 'hint subtle', item.used_for));
      const side = el('div', 'am-status');
      if (item.installed) {
        side.classList.add('good');
        side.textContent = 'Installed';
      } else if (item.installing) {
        side.append(el('span', null, 'Installing…'));
      } else if (item.cannot) {
        side.append(el('span', 'meta bad', 'Not from here'));
      } else {
        const button = el('button', 'btn small', 'Install');
        button.onclick = () => this.install(item);
        side.append(button);
      }
      row.append(what, side);

      if (item.cannot) {
        const note = el('div', 'meta bad', `${item.cannot} `);
        if (item.manual) {
          const link = el('a', null, item.manual);
          link.href = item.manual;
          link.target = '_blank';
          link.rel = 'noopener';
          note.append(link);
        }
        row.append(note);
      } else if (!item.installed) {
        row.append(el('div', 'meta', `Runs: ${item.command}`));
      }
      if (item.needs_restart && item.status === 'installed') {
        row.append(el('div', 'meta', 'Installed — it is used after the next restart.'));
      }
      if (item.error && !item.installed && !item.installing) {
        row.append(el('div', 'meta bad', `The last attempt failed: ${item.error}`));
      }
      if ((item.log || []).length && (item.installing || item.error)) {
        const output = el('pre', 'ex-log', item.log.slice(-12).join('\n'));
        row.append(output);
      }
      list.append(row);
    }

    const summary = $('#ex-summary');
    if (summary) {
      summary.textContent = missing
        ? `${missing} of ${(data.components || []).length} not installed`
        : 'Everything optional is installed';
    }
    const manager = $('#ex-manager');
    if (manager) {
      manager.textContent = data.manager_label
        ? `Tools are installed with ${data.manager_label}.`
        : 'This computer has no package manager Ninaivu can use, so a tool has '
          + 'to be installed by hand.';
    }

    // Only while something is happening, and only while the tab is open.
    if (busy && this.visible) {
      this.timer = setTimeout(() => this.refresh(), 2000);
    }
  }
}
