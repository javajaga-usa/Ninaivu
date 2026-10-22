/**
 * Talk about this photo, in the viewer (`photo_talk`).
 *
 * The viewer's menu offers Talk about this photo on a photograph the AI may
 * read. A side panel, like the stories', holds a short talk: a question
 * typed (or one of the suggestions tapped), and the home computer's writing
 * model looks at the photograph and answers in a few sentences, in the
 * language the question was asked in (ninaivu/api/api_photo_talk.py).
 *
 * Nothing is kept on the server. The talk about each photograph stays here
 * while the viewer is open, so going back to a photograph shows what was
 * said about it; closing the viewer lets all of it go.
 */

import { api } from './api.js';
import * as i18n from './i18n.js';

/** Questions offered before anything is asked, as keys. */
const SUGGESTIONS = [
  i18n.key('What is written here?'),
  i18n.key('Where might this be?'),
  i18n.key('What is happening here?'),
  i18n.key('Tell me about this photo'),
];
/** Earlier turns sent with a question; the server keeps as many. */
const TURNS = 4;

export class TalkPanel {
  constructor(viewer) {
    this.viewer = viewer;
    const q = (sel) => viewer.root.querySelector(sel);
    this.panel = q('#viewer-talk');
    this.button = q('#v-talk');
    if (!this.panel || !this.button) return;
    this.list = q('#talk-list');
    this.chips = q('#talk-chips');
    this.form = q('#talk-form');
    this.input = q('#talk-input');
    this.send = q('#talk-send');
    this.note = q('#talk-note');
    /** Set by the app while `photo_talk` is on, for the family. */
    this.on = false;
    this.item = null;
    this.status = null;
    /** Photograph id → [{ q, a, error }], while the viewer is open. */
    this.talks = new Map();
    /** The question on its way: { id, token }. */
    this.asking = null;
    this.looked = 0;

    this.button.onclick = () => { this.viewer.closeMoreMenu?.(); this.toggle(); };
    q('#v-talk-close').onclick = () => this.toggle(false, true);
    this.form.onsubmit = (event) => { event.preventDefault(); this.ask(this.input.value); };
    this.input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        this.ask(this.input.value);
      }
    });
    this.input.addEventListener('input', () => this.fit());
    // What is typed in here is for the panel, not a shortcut for the
    // photograph behind it.
    this.panel.addEventListener('keydown', (event) => {
      event.stopPropagation();
      if (event.key === 'Escape') { event.preventDefault(); this.toggle(false, true); }
    });
    this.chips.replaceChildren(...SUGGESTIONS.map((key) => {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'talk-chip';
      chip.dataset.key = key;
      chip.textContent = i18n.t(key);
      chip.onclick = () => this.ask(i18n.t(key));
      return chip;
    }));
  }

  get isOpen() {
    return !!this.panel && !this.panel.hidden;
  }

  /** The viewer moved to *item*: is it one to talk about? */
  async show(item) {
    if (!this.panel) return;
    this.item = item;
    this.status = null;
    const hide = () => {
      this.button.hidden = true;
      if (this.isOpen) this.toggle(false);
    };
    if (!this.on || item?.kind !== 'picture') { hide(); return; }
    const looked = ++this.looked;
    let status;
    try {
      status = await api.talkStatus(item.id);
    } catch { if (looked === this.looked) hide(); return; }
    if (looked !== this.looked || this.viewer.item?.id !== item.id) return;
    this.status = status;
    // A hidden or explicit photograph is never shown to the model: the
    // button is not offered for it at all.
    if (!status.allowed) { hide(); return; }
    this.button.hidden = false;
    if (this.isOpen) this.render();
  }

  toggle(force, returnFocus = false) {
    if (!this.panel) return;
    const open = (force === undefined ? this.panel.hidden : !!force) && !this.button.hidden;
    if (open) {
      this.viewer.toggleInfo(false);
      if (this.viewer.stories?.isOpen) this.viewer.stories.toggle(false);
    }
    this.panel.hidden = !open;
    this.button.classList.toggle('on', open);
    this.button.setAttribute('aria-expanded', String(open));
    this.viewer.root.classList.toggle('info-open',
      open || !this.viewer.info.hidden || !!this.viewer.stories?.isOpen);
    if (open) {
      this.viewer.stopSlideshow?.();
      this.render();
      if (this.status?.ready) this.input.focus({ preventScroll: true });
      else this.panel.querySelector('#v-talk-close').focus({ preventScroll: true });
    } else if (returnFocus) {
      this.viewer.root.querySelector('#v-more')?.focus();
    }
  }

  /** The viewer closed: every talk goes with it. */
  close() {
    if (!this.panel) return;
    this.panel.hidden = true;
    this.button.classList.remove('on');
    this.button.setAttribute('aria-expanded', 'false');
    this.talks.clear();
    this.asking = null;
    this.item = null;
  }

  turns(id) {
    if (!this.talks.has(id)) this.talks.set(id, []);
    return this.talks.get(id);
  }

  render() {
    const item = this.item;
    if (!item || !this.status) return;
    const ready = !!this.status.ready;
    const turns = this.turns(item.id);
    const waiting = this.asking?.id === item.id;
    this.form.hidden = !ready;
    this.chips.hidden = !ready || turns.length > 0 || waiting;
    this.note.textContent = ready
      ? i18n.t('Answered on this computer. Nothing is kept.')
      : i18n.t('Not ready yet: {why}', { why: i18n.fromServer(this.status.why) });
    this.note.classList.toggle('talk-warn', !ready);
    this.input.maxLength = this.status.max_question || 300;
    this.input.disabled = waiting;
    this.send.disabled = waiting;
    const rows = [];
    for (const turn of turns) {
      rows.push(this.bubble('talk-q', turn.q));
      if (turn.a) rows.push(this.bubble('talk-a', turn.a));
      else if (turn.error) rows.push(this.bubble('talk-a talk-error', turn.error));
    }
    if (waiting) {
      const thinking = this.bubble('talk-a talk-thinking', i18n.t('Looking at the photo…'));
      thinking.prepend(Object.assign(document.createElement('span'), {
        className: 'talk-dots', ariaHidden: 'true',
      }));
      rows.push(thinking);
    }
    this.list.replaceChildren(...rows);
    this.list.hidden = !rows.length;
    this.list.lastElementChild?.scrollIntoView?.({ block: 'nearest' });
  }

  bubble(className, text) {
    const li = document.createElement('li');
    li.className = className;
    li.textContent = text;
    return li;
  }

  fit() {
    this.input.style.height = 'auto';
    this.input.style.height = `${Math.min(this.input.scrollHeight, 120)}px`;
  }

  async ask(text) {
    const question = String(text || '').trim();
    const item = this.item;
    if (!question || !item || this.asking || !this.status?.ready) return;
    const turns = this.turns(item.id);
    const earlier = turns.filter((t) => t.a).slice(-TURNS).map((t) => ({ q: t.q, a: t.a }));
    const turn = { q: question, a: '', error: '' };
    turns.push(turn);
    const token = {};
    this.asking = { id: item.id, token };
    this.input.value = '';
    this.fit();
    this.render();
    try {
      const answer = await api.talk(item.id, question, earlier);
      turn.a = answer.answer || '';
      if (!turn.a) turn.error = i18n.t('No answer came this time. Ask again in a moment.');
    } catch (error) {
      turn.error = error.message || i18n.t('No answer came this time. Ask again in a moment.');
    }
    if (this.asking?.token !== token) return;          // the viewer closed meanwhile
    this.asking = null;
    if (this.item?.id === item.id && this.isOpen) {
      this.render();
      this.input.focus({ preventScroll: true });
    }
  }
}
