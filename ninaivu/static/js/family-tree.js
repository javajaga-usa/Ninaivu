/**
 * The family tree: generations as rows, each person a face and a name, lines
 * to their parents and between couples.
 *
 * Everybody in it comes from /api/family-tree, which is already cut to the
 * people this viewer may see on the People row (api_family_tree.py) — a
 * hidden person is simply not there, and nor are their lines. Choosing a
 * person in the tree opens their photographs; above the tree, one person's
 * parents, children and spouse can be added and taken away.
 */

import * as i18n from './i18n.js';

const SVG = 'http://www.w3.org/2000/svg';
const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
};

async function json(url, options = {}) {
  const response = await fetch(url, {
    credentials: 'same-origin',
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
    throw Object.assign(new Error(data?.error || response.statusText), { status: response.status });
  }
  return data;
}

function avatar(person) {
  if (person.cover_face_id) {
    const img = el('img', 'face');
    img.src = `/api/faces/thumb/${person.cover_face_id}`;
    img.alt = '';
    img.loading = 'lazy';
    return img;
  }
  const face = el('div', 'face initials');
  const words = String(person.name || '?').trim().split(/\s+/).filter(Boolean);
  face.textContent = words.slice(0, 2).map((w) => Array.from(w)[0]).join('').toUpperCase() || '?';
  return face;
}

/**
 * `onOpen(id)` shows that person's photographs; `focus` is the person whose
 * relations the editor starts on (the one the gallery is showing, if any).
 */
export function openFamilyTree({ toast = () => {}, focus = 0, onOpen = () => {} } = {}) {
  const modal = el('div', 'modal ask-modal');
  modal.setAttribute('role', 'dialog');
  modal.setAttribute('aria-modal', 'true');
  const card = el('div', 'modal-card ask-card tree-card');
  const head = el('div', 'ask-head');
  const title = el('h2', null, i18n.t('Family tree'));
  title.id = 'family-tree-title';
  modal.setAttribute('aria-labelledby', title.id);
  const close = el('button', 'btn ghost small', i18n.t('Close'));
  close.type = 'button';
  head.append(title, close);
  const editor = el('div', 'tree-editor');
  const stage = el('div', 'tree-stage');
  card.append(head, editor, stage);
  modal.appendChild(card);
  document.body.appendChild(modal);

  let resize = null;
  const shut = () => {
    resize?.disconnect();
    modal.remove();
    document.removeEventListener('keydown', onKey);
  };
  const onKey = (event) => { if (event.key === 'Escape') shut(); };
  document.addEventListener('keydown', onKey);
  close.onclick = shut;
  modal.addEventListener('click', (event) => { if (event.target === modal) shut(); });

  let data = { people: [], relations: [] };
  let selected = Number(focus) || 0;

  async function load() {
    data = await json('/api/family-tree');
    if (!data.people.some((p) => p.id === selected)) selected = data.people[0]?.id || 0;
    drawEditor();
    drawTree();
  }

  const byId = () => new Map(data.people.map((p) => [p.id, p]));
  const sorted = () => [...data.people].sort((a, b) => a.name.localeCompare(b.name, i18n.locale()));

  function personSelect(value, label, skip = 0) {
    const select = el('select', 'select input');
    select.setAttribute('aria-label', label);
    for (const person of sorted()) {
      if (person.id === skip) continue;
      const option = el('option', null, person.name);
      option.value = String(person.id);
      if (person.id === value) option.selected = true;
      select.appendChild(option);
    }
    return select;
  }

  function drawEditor() {
    editor.replaceChildren();
    if (!data.people.length) {
      editor.appendChild(el('p', 'hint', i18n.t('Nobody has been named yet. Name people first, then say how they are related.')));
      return;
    }
    const who = personSelect(selected, i18n.t('Person'));
    who.onchange = () => { selected = Number(who.value); drawEditor(); };
    const top = el('label', 'field');
    top.append(el('span', null, i18n.t('Relations of')), who);
    editor.appendChild(top);

    const people = byId();
    const groups = [
      [i18n.t('Parents'), data.relations.filter((r) => r.kind === 'parent' && r.person_b === selected), (r) => r.person_a],
      [i18n.t('Children'), data.relations.filter((r) => r.kind === 'parent' && r.person_a === selected), (r) => r.person_b],
      [i18n.t('Spouse'), data.relations.filter((r) => r.kind === 'spouse' && (r.person_a === selected || r.person_b === selected)),
        (r) => (r.person_a === selected ? r.person_b : r.person_a)],
    ];
    const list = el('div', 'tree-relations');
    for (const [label, rows, other] of groups) {
      const line = el('div', 'tree-relation-line');
      line.appendChild(el('span', 'hint', label));
      if (!rows.length) line.appendChild(el('span', 'hint', '—'));
      for (const row of rows) {
        const name = people.get(other(row))?.name || '';
        const chip = el('span', 'chip');
        chip.appendChild(document.createTextNode(name));
        const remove = el('button', 'tree-remove', '×');
        remove.type = 'button';
        remove.setAttribute('aria-label', i18n.t('Remove {name}', { name }));
        remove.onclick = async () => {
          try {
            await json(`/api/family-tree/relations/${row.id}`, { method: 'DELETE' });
            await load();
          } catch (err) { toast(err.message); }
        };
        chip.appendChild(remove);
        line.appendChild(chip);
      }
      list.appendChild(line);
    }
    editor.appendChild(list);

    if (data.people.length < 2) return;
    const kind = el('select', 'select input');
    kind.setAttribute('aria-label', i18n.t('Relation'));
    for (const [value, label] of [['parent', i18n.t('Parent')], ['child', i18n.t('Child')], ['spouse', i18n.t('Spouse')]]) {
      const option = el('option', null, label);
      option.value = value;
      kind.appendChild(option);
    }
    const other = personSelect(0, i18n.t('Who'), selected);
    const add = el('button', 'btn primary small', i18n.t('Add'));
    add.type = 'button';
    add.onclick = async () => {
      add.disabled = true;
      try {
        await json('/api/family-tree/relations', {
          method: 'POST',
          body: { person_id: selected, other_id: Number(other.value), relation: kind.value },
        });
        await load();
      } catch (err) {
        toast(i18n.t('Could not add that: {reason}', { reason: err.message }));
        add.disabled = false;
      }
    };
    const row = el('div', 'tree-add');
    row.append(kind, other, add);
    editor.appendChild(row);
  }

  function drawTree() {
    stage.replaceChildren();
    const rows = new Map();
    const loose = [];
    for (const person of sorted()) {
      if (person.generation == null) loose.push(person);
      else {
        if (!rows.has(person.generation)) rows.set(person.generation, []);
        rows.get(person.generation).push(person);
      }
    }
    const lines = document.createElementNS(SVG, 'svg');
    lines.setAttribute('class', 'tree-lines');
    lines.setAttribute('aria-hidden', 'true');
    stage.appendChild(lines);
    const nodes = new Map();
    const button = (person) => {
      const b = el('button', 'person-chip');
      b.type = 'button';
      b.title = i18n.t('See the photographs of {name}', { name: person.name });
      b.appendChild(avatar(person));
      b.appendChild(el('span', null, person.name));
      b.onclick = () => { shut(); onOpen(person.id); };
      nodes.set(person.id, b);
      return b;
    };
    if (!rows.size) {
      stage.appendChild(el('p', 'hint', i18n.t('No relations yet. Choose a person above and add their parents, children or spouse.')));
    }
    for (const generation of [...rows.keys()].sort((a, b) => a - b)) {
      const row = el('div', 'tree-row');
      // Couples side by side: each spouse straight after their partner.
      const placed = [];
      for (const person of rows.get(generation)) {
        if (placed.includes(person)) continue;
        placed.push(person);
        for (const r of data.relations) {
          if (r.kind !== 'spouse') continue;
          const partner = r.person_a === person.id ? r.person_b : r.person_b === person.id ? r.person_a : 0;
          const found = rows.get(generation).find((p) => p.id === partner);
          if (found && !placed.includes(found)) placed.push(found);
        }
      }
      for (const person of placed) row.appendChild(button(person));
      stage.appendChild(row);
    }
    if (loose.length) {
      stage.appendChild(el('h3', 'side-title', i18n.t('Not in the tree yet')));
      const row = el('div', 'tree-row');
      for (const person of loose) row.appendChild(button(person));
      stage.appendChild(row);
    }

    const draw = () => {
      const box = stage.getBoundingClientRect();
      lines.setAttribute('width', String(stage.scrollWidth));
      lines.setAttribute('height', String(stage.scrollHeight));
      lines.replaceChildren();
      const at = (id) => {
        const node = nodes.get(id);
        const face = node?.querySelector('.face');
        if (!face) return null;
        const r = face.getBoundingClientRect();
        // A line down leaves from under the name, not through it.
        const whole = node.getBoundingClientRect();
        return { x: r.left - box.left + stage.scrollLeft + r.width / 2,
          top: r.top - box.top + stage.scrollTop, bottom: whole.bottom - box.top + stage.scrollTop,
          mid: r.top - box.top + stage.scrollTop + r.height / 2 };
      };
      for (const r of data.relations) {
        const a = at(r.person_a);
        const b = at(r.person_b);
        if (!a || !b) continue;
        const path = document.createElementNS(SVG, 'path');
        if (r.kind === 'spouse') {
          path.setAttribute('d', `M${a.x} ${a.mid} L${b.x} ${b.mid}`);
          path.setAttribute('class', 'tree-spouse');
        } else {
          const bend = (a.bottom + b.top) / 2;
          path.setAttribute('d', `M${a.x} ${a.bottom} C${a.x} ${bend} ${b.x} ${bend} ${b.x} ${b.top}`);
        }
        lines.appendChild(path);
      }
    };
    requestAnimationFrame(draw);
    resize?.disconnect();
    if (typeof ResizeObserver !== 'undefined') {
      resize = new ResizeObserver(() => draw());
      resize.observe(stage);
    }
  }

  load().catch((err) => {
    stage.replaceChildren(el('p', 'hint', i18n.t('Could not load this: {reason}', { reason: err.message })));
  });
  close.focus();
  return { close: shut };
}
