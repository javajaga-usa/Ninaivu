/**
 * The AI server tab — heavy image edits on a ComfyUI server in the house.
 *
 * Self-contained, like the Cloud panel: the console hands it a toast function
 * and it owns everything under `[data-panel="ai-server"]`.
 *
 * It never polls. Nothing here changes on its own; the state is loaded when the
 * tab is shown and replaced by whatever each save or test returns.
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
  const response = await fetch(url, {
    headers: {
      Accept: 'application/json',
      ...(options.body ? { 'Content-Type': 'application/json' } : {}),
    },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const text = await response.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = null; }
  if (!response.ok) {
    if (response.status === 401) reportUnauthorized(url);
    throw Object.assign(new Error(data?.error || response.statusText), { status: response.status, data });
  }
  return data;
}

const api = {
  state: () => json('/api/admin/ai-server'),
  save: (body) => json('/api/admin/ai-server', { method: 'POST', body }),
  test: (url) => json('/api/admin/ai-server/test', { method: 'POST', body: { url } }),
  saveWorkflow: (body) => json('/api/admin/ai-server/workflows', { method: 'POST', body }),
  deleteWorkflow: (id) => json(`/api/admin/ai-server/workflows/${encodeURIComponent(id)}`, { method: 'DELETE' }),
};

const gib = (bytes) => (bytes ? `${(bytes / 1024 ** 3).toFixed(1)} GB` : '');

export class AIServerPanel {
  constructor({ toast }) {
    this.toast = toast || (() => {});
    this.state = null;
    this.busy = false;
    // True while the address box holds something typed but not yet saved, so a
    // test (which redraws the panel) does not put the saved address back.
    this.urlEdited = false;
    i18n.onChange(() => { if (this.state) this.render(this.state); });
  }

  wire() {
    $('#ai-test').onclick = () => this.test();
    $('#ai-save-url').onclick = () => this.saveUrl();
    $('#ai-enabled').onchange = () => this.saveEnabled();
    $('#ai-save-jobs').onclick = () => this.saveJobs();
    $('#ai-workflow-save').onclick = () => this.saveWorkflow();
    $('#ai-url').oninput = () => { this.urlEdited = true; $('#ai-public-warning').hidden = true; };
    $('#ai-workflow-file').onchange = (event) => this.readFile(event.target.files?.[0]);
  }

  async show() {
    try {
      this.render(await api.state());
    } catch (error) {
      this.toast(i18n.t('Could not load the AI server settings: {reason}', { reason: error.message }), true);
    }
  }

  hide() {}

  /* -- actions ------------------------------------------------------------ */

  async run(button, work, done) {
    if (this.busy) return;
    this.busy = true;
    if (button) button.disabled = true;
    try {
      const result = await work();
      if (result && result.settings) this.render(result);
      if (done) this.toast(done);
      return result;
    } catch (error) {
      this.toast(error.message, true);
      return null;
    } finally {
      this.busy = false;
      if (button) button.disabled = false;
    }
  }

  async test() {
    const url = $('#ai-url').value.trim();
    $('#ai-test-note').textContent = i18n.t('Contacting the server…');
    $('#ai-test-result').hidden = true;
    const result = await this.run($('#ai-test'), () => api.test(url));
    if (!result) { $('#ai-test-note').textContent = ''; return; }
    this.showNetwork(result.network);
    if (!result.ok) {
      $('#ai-test-note').textContent = result.error;
      return;
    }
    const device = (result.server.devices || [])[0];
    $('#ai-gpu').textContent = device ? device.name.replace(/^cuda:\d+\s*/, '') : i18n.t('No GPU reported');
    $('#ai-vram').textContent = device && device.vram_total
      ? i18n.t('{total} memory, {free} free', { total: gib(device.vram_total), free: gib(device.vram_free) }) : '';
    $('#ai-version').textContent = result.server.comfyui_version || i18n.t('Reachable');
    $('#ai-nodes').textContent = i18n.t('{count} node types installed', { count: result.node_count });
    $('#ai-test-result').hidden = false;
    const missing = (result.workflows || []).filter((w) => (w.missing_nodes || []).length);
    $('#ai-test-note').textContent = missing.length
      ? i18n.t('Connected. {count} saved workflow(s) use nodes this server does not have — see below.', { count: missing.length })
      : i18n.t('Connected.');
  }

  async saveUrl() {
    const url = $('#ai-url').value.trim();
    this.urlEdited = false;
    const saved = await this.run($('#ai-save-url'), () => api.save({ url }), url ? i18n.t('Address saved') : i18n.t('Address cleared'));
    if (!saved) this.urlEdited = true;
  }

  async saveEnabled() {
    const box = $('#ai-enabled');
    const wanted = box.checked;
    const result = await this.run(null, () => api.save({ enabled: wanted }),
      wanted ? i18n.t('AI server on') : i18n.t('AI server off'));
    if (!result) box.checked = !wanted;
  }

  async saveJobs() {
    const jobs = {};
    document.querySelectorAll('#ai-job-rows select[data-job]').forEach((select) => {
      jobs[select.dataset.job] = select.value;
    });
    const body = {
      jobs,
      max_side: Number($('#ai-max-side').value),
      timeout: Number($('#ai-timeout').value),
    };
    await this.run($('#ai-save-jobs'), () => api.save(body), i18n.t('Jobs saved'));
  }

  async readFile(file) {
    if (!file) return;
    if (file.size > 2 * 1024 * 1024) {
      this.toast(i18n.t('That workflow is larger than Ninaivu accepts (2 MB).'), true);
      return;
    }
    $('#ai-workflow-json').value = await file.text();
    if (!$('#ai-workflow-name').value) {
      $('#ai-workflow-name').value = file.name.replace(/\.json$/i, '').replace(/[_-]+/g, ' ');
    }
  }

  async saveWorkflow() {
    const body = {
      name: $('#ai-workflow-name').value.trim(),
      purpose: $('#ai-workflow-purpose').value,
      workflow: $('#ai-workflow-json').value,
    };
    const result = await this.run($('#ai-workflow-save'), () => api.saveWorkflow(body), i18n.t('Workflow saved'));
    if (result) {
      $('#ai-workflow-json').value = '';
      $('#ai-workflow-name').value = '';
      $('#ai-workflow-file').value = '';
      $('#ai-add-workflow').open = false;
    }
  }

  async remove(workflow) {
    if (!window.confirm(i18n.t('Delete the workflow “{name}”? Jobs using it go back to this machine.', { name: workflow.name }))) return;
    await this.run(null, () => api.deleteWorkflow(workflow.id), i18n.t('Workflow deleted'));
  }

  /* -- drawing ------------------------------------------------------------ */

  showNetwork(scope) {
    const labels = {
      home: i18n.t('home network'),
      public: i18n.t('outside your home network'),
      unknown: i18n.t('network not recognised'),
    };
    $('#ai-network').textContent = scope ? labels[scope] || '' : '';
    $('#ai-public-warning').hidden = scope !== 'public';
  }

  render(data) {
    this.state = data;
    const { settings } = data;
    if (!this.urlEdited) $('#ai-url').value = settings.url || '';
    $('#ai-enabled').checked = settings.enabled;
    $('#ai-max-side').value = settings.max_side;
    $('#ai-timeout').value = settings.timeout;
    this.showNetwork(settings.network);

    const active = Object.entries(settings.active).filter(([, on]) => on).map(([key]) => i18n.t(data.purposes[key].label));
    $('#ai-state').textContent = !settings.url ? i18n.t('Not set up')
      : !settings.enabled ? i18n.t('Off')
        : active.length ? i18n.t('On · {jobs}', { jobs: active.join(', ') }) : i18n.t('On · no jobs assigned');

    this.drawJobs(data);
    const purpose = $('#ai-workflow-purpose');
    if (!purpose.options.length) {
      for (const key of data.purpose_order) {
        const option = el('option');
        option.value = key;
        purpose.append(option);
      }
    }
    // Named on every draw, not only the first, so a change of language reaches them.
    for (const option of purpose.options) {
      const value = data.purposes[option.value];
      if (value) option.textContent = i18n.t(value.label);
    }

    const list = $('#ai-workflows');
    list.replaceChildren();
    $('#ai-workflow-count').textContent = data.workflows.length
      ? i18n.t('{count} saved', { count: data.workflows.length }) : i18n.t('none yet');
    if (!data.workflows.length) {
      list.append(el('p', 'hint subtle', i18n.t('No workflows yet. Add one exported from ComfyUI below.')));
    }
    for (const workflow of data.workflows) list.append(this.workflowRow(workflow, settings));
  }

  /** One label, workflow picker and status line per job, in the grid's columns. */
  drawJobs(data) {
    const rows = $('#ai-job-rows');
    rows.replaceChildren();
    for (const purpose of data.purpose_order) {
      const info = data.purposes[purpose];
      const id = `ai-job-${purpose}`;
      const label = el('label', null, i18n.t(info.label));
      label.htmlFor = id;
      const select = el('select', 'select');
      select.id = id;
      select.dataset.job = purpose;
      this.fillSelect(select, data.workflows, purpose, data.settings.jobs[purpose]);
      rows.append(label, select, el('span', 'ai-note', this.jobNote(data.settings, purpose)));
    }
  }

  jobNote(settings, purpose) {
    const chosen = settings.jobs[purpose];
    if (!chosen) return i18n.t('Runs on this machine.');
    if (!settings.enabled) return i18n.t('Assigned, but the AI server is off.');
    return settings.active[purpose] ? i18n.t('Runs on the AI server.') : i18n.t('Workflow missing — runs on this machine.');
  }

  fillSelect(select, workflowsList, purpose, chosen) {
    select.replaceChildren(el('option', null, i18n.t('This machine (no AI server)')));
    select.firstChild.value = '';
    for (const workflow of workflowsList.filter((w) => w.purpose === purpose)) {
      const option = el('option', null, workflow.name);
      option.value = workflow.id;
      select.append(option);
    }
    select.value = chosen || '';
  }

  workflowRow(workflow, settings) {
    const problems = [];
    if (workflow.problem) problems.push(workflow.problem);
    if (workflow.missing_placeholders.length) {
      problems.push(i18n.t('Missing {names}.', { names: workflow.missing_placeholders.map((name) => `{{${name}}}`).join(', ') }));
    }
    if (workflow.missing_nodes && workflow.missing_nodes.length) {
      problems.push(i18n.t('The server does not have: {nodes}.', { nodes: workflow.missing_nodes.join(', ') }));
    }
    const row = el('div', `ai-workflow${problems.length ? ' problem-row' : ''}`);
    const what = el('div', 'what');
    what.append(el('strong', null, workflow.name));
    const note = [i18n.t(workflow.purpose_label), i18n.t('{count} nodes', { count: workflow.nodes })];
    if (Object.values(settings.jobs).includes(workflow.id)) note.push(i18n.t('assigned'));
    what.append(el('div', 'ai-note', note.join(' · ')));
    const remove = el('button', 'btn ghost small', i18n.t('Delete'));
    remove.onclick = () => this.remove(workflow);
    row.append(what, remove);
    const fills = workflow.placeholders.map((p) => `{{${p}}}`).join(', ');
    row.append(el('div', 'detail', fills ? i18n.t('Fills {names}.', { names: fills }) : i18n.t('Fills nothing.')));
    for (const problem of problems) row.append(el('div', 'detail bad', problem));
    if (workflow.missing_nodes === null) {
      row.append(el('div', 'detail', i18n.t('Test the connection to check this workflow’s nodes are installed on the server.')));
    }
    return row;
  }
}
