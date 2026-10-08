/* The "who is this?" page: what somebody with no account sees when a family
   member sends them a link (api_ask_family.py). Like the share page it is its
   own small thing, and every address it uses carries the token, which is the
   only authority here. It is shown faces by number and nothing else: no names
   the family already uses, no dates, no places. */
import * as i18n from './i18n.js';

const TOKEN = document.body.dataset.askToken;
const APP = document.body.dataset.appName || 'Ninaivu';
const main = document.getElementById('main');
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};

let data = null;
let sent = false;
/** What has been typed, by face number, so a change of language keeps it. */
const typed = new Map();
let who = '';

function languageButtons() {
  const box = document.getElementById('langs');
  box.replaceChildren();
  for (const lang of i18n.LANGUAGES) {
    const button = el('button', null, lang.name);
    button.type = 'button';
    button.lang = lang.code;
    button.setAttribute('aria-pressed', String(i18n.language() === lang.code));
    button.onclick = async () => { await i18n.use(lang.code); draw(); };
    box.appendChild(button);
  }
}

function field(labelText, node) {
  const label = el('label');
  label.append(el('span', null, labelText), node);
  return label;
}

function faceCard(face) {
  const card = el('section', 'face');
  const crop = document.createElement('img');
  crop.className = 'crop';
  crop.src = face.face;
  crop.alt = i18n.t('Face {n}', { n: face.n });
  card.appendChild(crop);

  const fields = el('div', 'fields');
  const answer = typed.get(face.n) || { name: '', note: '' };
  const name = document.createElement('input');
  name.type = 'text';
  name.maxLength = data.limits?.name || 80;
  name.autocomplete = 'off';
  name.value = answer.name;
  name.placeholder = i18n.t('Who is this?');
  name.oninput = () => typed.set(face.n, { ...answer, ...typed.get(face.n), name: name.value });
  const note = document.createElement('textarea');
  note.maxLength = data.limits?.note || 500;
  note.value = answer.note;
  note.placeholder = i18n.t('For example: my uncle Raman, at Chennai, around 1975');
  note.oninput = () => typed.set(face.n, { ...answer, ...typed.get(face.n), note: note.value });
  fields.append(field(i18n.t('Name'), name), field(i18n.t('Anything else you remember (optional)'), note));
  card.appendChild(fields);

  if (face.photo) {
    const holder = el('div', 'photo');
    const show = el('button', 'link', i18n.t('See the whole photograph'));
    show.type = 'button';
    show.onclick = () => {
      const img = document.createElement('img');
      img.src = face.photo;
      img.alt = i18n.t('The photograph face {n} is in', { n: face.n });
      holder.replaceChildren(img);
    };
    holder.appendChild(show);
    card.appendChild(holder);
  }
  return card;
}

function draw() {
  languageButtons();
  document.title = i18n.t('Who is this? · {name}', { name: APP });
  document.getElementById('title').textContent = i18n.t('Who is this?');
  if (!data) return;
  if (sent) {
    main.replaceChildren(el('p', 'empty',
      i18n.t('Thank you. Your answers have been sent to the family.')));
    return;
  }
  const form = document.createElement('form');
  form.noValidate = true;
  form.appendChild(el('p', 'lead', data.faces.length === 1
    ? i18n.t('Do you know who this is? Write the name under the face, and anything you remember about them.')
    : i18n.t('Do you know who these people are? Write a name under each face you know — leave the others empty.')));
  for (const face of data.faces) form.appendChild(faceCard(face));

  const me = document.createElement('input');
  me.type = 'text';
  me.maxLength = data.limits?.who || 60;
  me.autocomplete = 'name';
  me.value = who;
  me.oninput = () => { who = me.value; };
  const whoField = field(i18n.t('Your name, so the family knows who answered (optional)'), me);
  whoField.classList.add('who');
  const error = el('p', 'error');
  error.setAttribute('role', 'alert');
  const button = el('button', 'send', i18n.t('Send answers'));
  button.type = 'submit';
  form.append(whoField, button, error);
  form.onsubmit = async (event) => {
    event.preventDefault();
    const answers = [...typed.entries()]
      .map(([n, a]) => ({ n, name: (a.name || '').trim(), note: (a.note || '').trim() }))
      .filter((a) => a.name);
    if (!answers.length) {
      error.textContent = i18n.t('Write a name under at least one face.');
      return;
    }
    button.disabled = true;
    error.textContent = '';
    try {
      const res = await fetch(`/api/ask/${TOKEN}/answer`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ answers, who: who.trim() }),
      });
      if (res.status === 429) {
        error.textContent = i18n.t('Thank you — that is a lot of answers at once. Wait a little and send the rest.');
        return;
      }
      if (!res.ok) {
        error.textContent = i18n.t('That could not be sent. Check the answers and try again.');
        return;
      }
      sent = true;
      draw();
    } catch {
      error.textContent = i18n.t('Could not reach Ninaivu. Try opening the link again.');
    } finally {
      button.disabled = false;
    }
  };
  main.replaceChildren(form);
}

async function load() {
  try {
    const res = await fetch(`/api/ask/${TOKEN}`, { headers: { Accept: 'application/json' } });
    if (!res.ok) {
      main.replaceChildren(el('p', 'empty', res.status === 410
        ? i18n.t('This link has ended. Ask the person who sent it for a new one.')
        : i18n.t('This link is no longer available.')));
      return;
    }
    data = await res.json();
    draw();
  } catch {
    main.replaceChildren(el('p', 'empty', i18n.t('This link could not be opened.')));
  }
}

// The browser's language to begin with, as on the share page; the buttons at
// the top change it, and remember it on this device.
await i18n.start();
draw();
load();
