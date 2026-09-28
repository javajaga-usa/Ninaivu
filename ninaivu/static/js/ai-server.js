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
      this.toast(`Could not load the AI server settings: ${error.message}`, true);
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
    $('#ai-test-note').textContent = 'Contacting the server…';
    $('#ai-test-result').hidden = true;
    const result = await this.run($('#ai-test'), () => api.test(url));
    if (!result) { $('#ai-test-note').textContent = ''; return; }
    this.showNetwork(result.network);
    if (!result.ok) {
      $('#ai-test-note').textContent = result.error;
      return;
    }
    const device = (result.server.devices || [])[0];
    $('#ai-gpu').textContent = device ? device.name.replace(/^cuda:\d+\s*/, '') : 'No GPU reported';
    $('#ai-vram').textContent = device && device.vram_total
      ? `${gib(device.vram_total)} memory, ${gib(device.vram_free)} free` : '';
    $('#ai-version').textContent = result.server.comfyui_version || 'Reachable';
    $('#ai-nodes').textContent = `${result.node_count} node types installed`;
    $('#ai-test-result').hidden = false;
    const missing = (result.workflows || []).filter((w) => (w.missing_nodes || []).length);
    $('#ai-test-note').textContent = missing.length
      ? `Connected. ${missing.length} saved workflow(s) use nodes this server does not have — see below.`
      : 'Connected.';
  }

  async saveUrl() {
    const url = $('#ai-url').value.trim();
    this.urlEdited = false;
    const saved = await this.run($('#ai-save-url'), () => api.save({ url }), url ? 'Address saved' : 'Address cleared');
    if (!saved) this.urlEdited = true;
  }

  async saveEnabled() {
    const box = $('#ai-enabled');
    const wanted = box.checked;
    const result = await this.run(null, () => api.save({ enabled: wanted }),
      wanted ? 'AI server on' : 'AI server off');
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
    await this.run($('#ai-save-jobs'), () => api.save(body), 'Jobs saved');
  }

  async readFile(file) {
    if (!file) return;
    if (file.size > 2 * 1024 * 1024) {
      this.toast('That workflow is larger than Ninaivu accepts (2 MB).', true);
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
    const result = await this.run($('#ai-workflow-save'), () => api.saveWorkflow(body), 'Workflow saved');
    if (result) {
      $('#ai-workflow-json').value = '';
      $('#ai-workflow-name').value = '';
      $('#ai-workflow-file').value = '';
      $('#ai-add-workflow').open = false;
    }
  }

  async remove(workflow) {
    if (!window.confirm(`Delete the workflow “${workflow.name}”? Jobs using it go back to this machine.`)) return;
    await this.run(null, () => api.deleteWorkflow(workflow.id), 'Workflow deleted');
  }

  /* -- drawing ------------------------------------------------------------ */

  showNetwork(scope) {
    const labels = { home: 'home network', public: 'outside your home network', unknown: 'network not recognised' };
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

    const active = Object.entries(settings.active).filter(([, on]) => on).map(([key]) => data.purposes[key].label);
    $('#ai-state').textContent = !settings.url ? 'Not set up'
      : !settings.enabled ? 'Off'
        : active.length ? `On · ${active.join(', ')}` : 'On · no jobs assigned';

    this.drawJobs(data);
    const purpose = $('#ai-workflow-purpose');
    if (!purpose.options.length) {
      for (const key of data.purpose_order) {
        const value = data.purposes[key];
        const option = el('option', null, value.label);
        option.value = key;
        purpose.append(option);
      }
    }

    const list = $('#ai-workflows');
    list.replaceChildren();
    $('#ai-workflow-count').textContent = data.workflows.length
      ? `${data.workflows.length} saved` : 'none yet';
    if (!data.workflows.length) {
      list.append(el('p', 'hint subtle', 'No workflows yet. Add one exported from ComfyUI below.'));
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
      const label = el('label', null, info.label);
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
    if (!chosen) return 'Runs on this machine.';
    if (!settings.enabled) return 'Assigned, but the AI server is off.';
    return settings.active[purpose] ? 'Runs on the AI server.' : 'Workflow missing — runs on this machine.';
  }

  fillSelect(select, workflowsList, purpose, chosen) {
    select.replaceChildren(el('option', null, 'This machine (no AI server)'));
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
      problems.push(`Missing ${workflow.missing_placeholders.map((name) => `{{${name}}}`).join(', ')}.`);
    }
    if (workflow.missing_nodes && workflow.missing_nodes.length) {
      problems.push(`The server does not have: ${workflow.missing_nodes.join(', ')}.`);
    }
    const row = el('div', `ai-workflow${problems.length ? ' problem-row' : ''}`);
    const what = el('div', 'what');
    what.append(el('strong', null, workflow.name));
    const used = Object.values(settings.jobs).includes(workflow.id) ? ' · assigned' : '';
    what.append(el('div', 'ai-note', `${workflow.purpose_label} · ${workflow.nodes} nodes${used}`));
    const remove = el('button', 'btn ghost small', 'Delete');
    remove.onclick = () => this.remove(workflow);
    row.append(what, remove);
    row.append(el('div', 'detail', `Fills ${workflow.placeholders.map((p) => `{{${p}}}`).join(', ') || 'nothing'}.`));
    for (const problem of problems) row.append(el('div', 'detail bad', problem));
    if (workflow.missing_nodes === null) {
      row.append(el('div', 'detail', 'Test the connection to check this workflow’s nodes are installed on the server.'));
    }
    return row;
  }
}
