/**
 * Ask the family: pick unnamed faces, send a link, review what comes back.
 *
 * Opened from the People row of the family app and from Faces on the
 * console, so it brings its own window rather than markup in either page,
 * built from the classes both pages already style (.modal, .btn, .input,
 * .chip, .hint). The server decides everything that matters — which faces
 * this person may ask about, what the link shows, whose answers they may
 * review — in api_ask_family.py; this only draws it.
 */

import * as i18n from './i18n.js';

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

const MAX_FACES = 20;
const faceThumb = (id) => `/api/faces/thumb/${id}`;

/**
 * Open the window. `familyUrl()` gives the address of the family app when
 * this is the console: a link has to open where somebody without an account
 * is let in, and the console is not that place.
 */
export function openAskFamily({ toast = () => {}, familyUrl = null, tab = 'ask', onClose = null } = {}) {
  const modal = el('div', 'modal ask-modal');
  modal.setAttribute('role', 'dialog');
  modal.setAttribute('aria-modal', 'true');
  const card = el('div', 'modal-card ask-card');
  const head = el('div', 'ask-head');
  const title = el('h2', null, i18n.t('Ask the family'));
  title.id = 'ask-family-title';
  modal.setAttribute('aria-labelledby', title.id);
  const close = el('button', 'btn ghost small', i18n.t('Close'));
  close.type = 'button';
  head.append(title, close);

  const tabs = el('div', 'ask-tabs');
  tabs.setAttribute('role', 'tablist');
  const body = el('div', 'ask-body');
  card.append(head, tabs, body);
  modal.appendChild(card);
  document.body.appendChild(modal);

  const shut = () => {
    modal.remove();
    document.removeEventListener('keydown', onKey);
    onClose?.();
  };
  const onKey = (event) => { if (event.key === 'Escape') shut(); };
  document.addEventListener('keydown', onKey);
  close.onclick = shut;
  modal.addEventListener('click', (event) => { if (event.target === modal) shut(); });

  const linkFor = (path) => {
    const base = (familyUrl ? familyUrl() : window.location.origin) || window.location.origin;
    return `${base.replace(/\/+$/, '')}${path}`;
  };

  const pages = {
    ask: { label: i18n.t('Choose faces'), draw: drawAsk },
    answers: { label: i18n.t('Answers'), draw: drawAnswers },
    links: { label: i18n.t('Links sent'), draw: drawLinks },
  };
  let current = pages[tab] ? tab : 'ask';
  function show(which) {
    current = which;
    tabs.replaceChildren();
    for (const [key, page] of Object.entries(pages)) {
      const button = el('button', `chip${key === current ? ' active' : ''}`, page.label);
      button.type = 'button';
      button.setAttribute('role', 'tab');
      button.setAttribute('aria-selected', String(key === current));
      button.onclick = () => show(key);
      tabs.appendChild(button);
    }
    body.replaceChildren(el('p', 'hint', i18n.t('Loading…')));
    pages[which].draw().catch((err) => {
      body.replaceChildren(el('p', 'hint', i18n.t('Could not load this: {reason}', { reason: err.message })));
    });
  }

  /* -- choosing faces --------------------------------------------------- */

  async function drawAsk() {
    const chosen = new Set();
    let offset = 0;
    const intro = el('p', 'hint', i18n.t('Choose up to 20 faces nobody has named. Ninaivu makes a link to send by WhatsApp or e-mail: whoever opens it sees only these faces, with a box under each for a name.'));
    const grid = el('div', 'ask-faces');
    const more = el('button', 'btn ghost small', i18n.t('Show more faces'));
    more.type = 'button';
    more.hidden = true;
    const photo = el('label', 'ask-check');
    const photoBox = document.createElement('input');
    photoBox.type = 'checkbox';
    photo.append(photoBox, el('span', null, i18n.t('Show the whole photograph too (never for photographs only administrators may see)')));
    const count = el('span', 'hint');
    const make = el('button', 'btn primary', i18n.t('Make the link'));
    make.type = 'button';
    make.disabled = true;
    const foot = el('div', 'modal-foot');
    foot.append(count, el('div', 'spacer'), make);
    const result = el('div', 'ask-result');
    body.replaceChildren(intro, grid, more, photo, foot, result);

    const update = () => {
      make.disabled = chosen.size === 0;
      count.textContent = chosen.size
        ? i18n.t('{count} of 20 chosen', { count: chosen.size }) : '';
    };
    async function page() {
      const data = await json(`/api/ask-family/faces?limit=60&offset=${offset}`);
      offset += data.faces.length;
      more.hidden = !data.more;
      if (!offset) {
        grid.replaceWith(el('p', 'hint', i18n.t('Every face Ninaivu has found has a name. Nothing to ask about.')));
        make.hidden = true;
        photo.hidden = true;
        return;
      }
      for (const face of data.faces) {
        const button = el('button', 'ask-face');
        button.type = 'button';
        button.setAttribute('aria-pressed', 'false');
        const img = el('img');
        img.src = faceThumb(face.face_id);
        img.alt = face.admin_only ? i18n.t('Unnamed face, in a photograph only administrators may see')
          : i18n.t('Unnamed face');
        img.loading = 'lazy';
        button.appendChild(img);
        if (face.admin_only) button.appendChild(el('span', 'ask-badge', i18n.t('Admin only')));
        button.onclick = () => {
          if (chosen.has(face.face_id)) chosen.delete(face.face_id);
          else if (chosen.size < MAX_FACES) chosen.add(face.face_id);
          else { toast(i18n.t('A link can ask about 20 faces at most.')); return; }
          button.setAttribute('aria-pressed', String(chosen.has(face.face_id)));
          update();
        };
        grid.appendChild(button);
      }
    }
    more.onclick = () => page().catch((err) => toast(err.message));
    make.onclick = async () => {
      make.disabled = true;
      try {
        const made = await json('/api/ask-family/questions', {
          method: 'POST', body: { face_ids: [...chosen], show_photo: photoBox.checked },
        });
        showLink(result, linkFor(made.url));
        result.scrollIntoView({ block: 'nearest' });
      } catch (err) {
        toast(i18n.t('Could not make the link: {reason}', { reason: err.message }));
        make.disabled = false;
      }
    };
    update();
    await page();
  }

  function showLink(box, url) {
    box.replaceChildren();
    box.appendChild(el('p', 'hint', i18n.t('Send this link. It works for 30 days, and you can withdraw it under Links sent.')));
    const row = el('div', 'share-link-box');
    const input = el('input', 'input share-url-input');
    input.readOnly = true;
    input.value = url;
    input.setAttribute('aria-label', i18n.t('Link to send'));
    const copy = el('button', 'btn primary small', i18n.t('Copy'));
    copy.type = 'button';
    copy.onclick = async () => {
      try { await navigator.clipboard.writeText(url); toast(i18n.t('Link copied.')); } catch {
        input.select();
      }
    };
    row.append(input, copy);
    box.appendChild(row);
    const message = i18n.t('Do you know who these people are? Please tell us here: {url}', { url });
    const actions = el('div', 'modal-foot');
    if (navigator.share) {
      const share = el('button', 'btn ghost small', i18n.t('Share…'));
      share.type = 'button';
      share.onclick = () => navigator.share({ text: message }).catch(() => {});
      actions.appendChild(share);
    }
    const whatsapp = el('a', 'btn ghost small', i18n.t('Send by WhatsApp'));
    whatsapp.href = `https://wa.me/?text=${encodeURIComponent(message)}`;
    whatsapp.target = '_blank';
    whatsapp.rel = 'noopener noreferrer';
    actions.appendChild(whatsapp);
    box.appendChild(actions);
  }

  /* -- reviewing answers ------------------------------------------------ */

  async function drawAnswers() {
    const data = await json('/api/ask-family/answers');
    const list = el('div', 'ask-answers');
    if (!data.answers.length) {
      body.replaceChildren(el('p', 'hint', i18n.t('No answers waiting. They appear here when somebody answers a link you sent.')));
      return;
    }
    for (const answer of data.answers) list.appendChild(answerRow(answer));
    body.replaceChildren(list);
  }

  function answerRow(answer) {
    const row = el('div', 'ask-answer');
    const img = el('img', 'ask-answer-face');
    img.src = faceThumb(answer.face_id);
    img.alt = '';
    row.appendChild(img);
    const text = el('div', 'ask-answer-text');
    const said = answer.answered_by
      ? i18n.t('{who} says this is {name}', { who: answer.answered_by, name: answer.name })
      : i18n.t('Someone says this is {name}', { name: answer.name });
    text.appendChild(el('strong', null, said));
    if (answer.note) text.appendChild(el('p', 'ask-note', answer.note));
    const when = new Date(answer.created_at * 1000);
    text.appendChild(el('span', 'hint', when.toLocaleString(i18n.locale())));
    if (answer.named) text.appendChild(el('span', 'hint warn', i18n.t('This face has been given a name since.')));

    const actions = el('div', 'ask-actions');
    const settle = async (url, body, done) => {
      for (const b of actions.querySelectorAll('button, input')) b.disabled = true;
      try {
        const reply = await json(url, { method: 'POST', body });
        toast(done(reply));
        row.remove();
      } catch (err) {
        toast(i18n.t('Could not save that: {reason}', { reason: err.message }));
        for (const b of actions.querySelectorAll('button, input')) b.disabled = false;
      }
    };
    for (const person of answer.matches || []) {
      const join = el('button', 'btn primary small', i18n.t('Yes, it is {name}', { name: person.name }));
      join.type = 'button';
      join.onclick = () => settle(`/api/ask-family/answers/${answer.id}/accept`,
        { person_id: person.id }, (r) => i18n.t('Named {name}.', { name: r.person.name }));
      actions.appendChild(join);
    }
    const name = el('input', 'input small');
    name.value = answer.name;
    name.maxLength = 80;
    name.setAttribute('aria-label', i18n.t('Name for this face'));
    const fresh = el('button', `btn ${answer.matches?.length ? 'ghost' : 'primary'} small`);
    fresh.type = 'button';
    const label = () => {
      const typed = name.value.trim().toLocaleLowerCase();
      const same = (answer.matches || []).some((p) => p.name.toLocaleLowerCase() === typed);
      fresh.textContent = same ? i18n.t('Add to that person') : i18n.t('Accept as a new person');
      fresh.disabled = !typed;
    };
    name.addEventListener('input', label);
    label();
    fresh.onclick = () => settle(`/api/ask-family/answers/${answer.id}/accept`,
      { name: name.value.trim() }, (r) => i18n.t('Named {name}.', { name: r.person.name }));
    const dismiss = el('button', 'btn ghost small', i18n.t('Dismiss'));
    dismiss.type = 'button';
    dismiss.onclick = () => settle(`/api/ask-family/answers/${answer.id}/reject`, {},
      () => i18n.t('Answer dismissed.'));
    const nameRow = el('div', 'ask-name-row');
    nameRow.append(name, fresh);
    actions.append(nameRow, dismiss);
    text.appendChild(actions);
    row.appendChild(text);
    return row;
  }

  /* -- the links -------------------------------------------------------- */

  async function drawLinks() {
    const data = await json('/api/ask-family/questions');
    if (!data.questions.length) {
      body.replaceChildren(el('p', 'hint', i18n.t('No links yet.')));
      return;
    }
    const list = el('div', 'ask-answers');
    for (const q of data.questions) {
      const row = el('div', 'ask-link');
      const made = new Date(q.created_at * 1000).toLocaleDateString(i18n.locale());
      row.appendChild(el('strong', null, i18n.t('{faces} faces, made {date}', { faces: q.face_count, date: made })));
      const state = q.revoked ? i18n.t('Withdrawn')
        : q.expired ? i18n.t('Ended')
          : i18n.t('{answers} answers, {pending} waiting', { answers: q.answer_count, pending: q.pending_count });
      row.appendChild(el('span', 'hint', state));
      if (q.open) {
        const actions = el('div', 'ask-actions');
        const copy = el('button', 'btn ghost small', i18n.t('Copy link'));
        copy.type = 'button';
        copy.onclick = async () => {
          try { await navigator.clipboard.writeText(linkFor(q.url)); toast(i18n.t('Link copied.')); } catch {
            toast(linkFor(q.url));
          }
        };
        const withdraw = el('button', 'btn ghost small', i18n.t('Withdraw'));
        withdraw.type = 'button';
        withdraw.onclick = async () => {
          withdraw.disabled = true;
          try {
            await json(`/api/ask-family/questions/${encodeURIComponent(q.token)}`, { method: 'DELETE' });
            toast(i18n.t('Link withdrawn. Answers it collected stay to be reviewed.'));
            show('links');
          } catch (err) {
            toast(err.message);
            withdraw.disabled = false;
          }
        };
        actions.append(copy, withdraw);
        row.appendChild(actions);
      }
      list.appendChild(row);
    }
    body.replaceChildren(list);
  }

  show(current);
  close.focus();
  return { close: shut };
}
