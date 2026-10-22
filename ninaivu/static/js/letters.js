/**
 * Letters read aloud, in the viewer (`letters_aloud`).
 *
 * The viewer's menu offers Read aloud on any photograph. It opens the
 * photograph beside its words: read off the photograph by the text reader
 * already on the home computer, or typed by the family, and always theirs to
 * correct. Kept words are read aloud in a calm Tamil or English voice
 * (ninaivu/api/api_letters.py, Meta's MMS voices, never anybody's own), and
 * the sentence being read lights up; pressing a sentence plays from there.
 */

import { reportUnauthorized } from './api.js';
import * as i18n from './i18n.js';

async function call(url, options = {}) {
  const response = await fetch(url, { credentials: 'same-origin', ...options });
  if (response.status === 401) reportUnauthorized(url);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || data.message || data.description || '');
  return data;
}

const LANGUAGES = [['ta', 'தமிழ்'], ['en', 'English']];

export class LetterPanel {
  constructor(viewer) {
    this.viewer = viewer;
    this.button = viewer.root.querySelector('#v-letter');
    this.item = null;
    this.status = null;
    this.overlay = null;
    this.audio = null;
    this.editing = false;
    this.draft = null;          // { words, language, source } not yet kept
    this.asked = 0;
    if (this.button) this.button.onclick = () => this.open();
    this.onKey = (event) => this.key(event);
  }

  /** Called for every item the viewer shows. */
  show(item) {
    this.close();
    this.item = item;
    if (!this.button) return;
    this.button.hidden = !(this.viewer.lettersOn && item?.kind === 'picture');
  }

  async open() {
    const item = this.item;
    if (!item) return;
    this.viewer.closeMoreMenu?.();
    this.viewer.stopSlideshow?.();
    this.build(item);
    this.busy(i18n.t('Opening…'));
    const asked = ++this.asked;
    try {
      const status = await call(`/api/asset/${item.id}/letter`);
      if (asked !== this.asked || !this.overlay) return;
      this.status = status;
      this.editing = !status.words;
      this.draft = null;
      this.render();
    } catch (error) {
      this.close();
      this.viewer.toast?.(i18n.t(error.message || i18n.key('This photograph could not be opened.')), true);
    }
  }

  build(item) {
    const overlay = document.createElement('div');
    overlay.className = 'letter-read';
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-label', i18n.t('Read aloud'));
    overlay.innerHTML = `
      <figure class="lr-photo"><img alt=""></figure>
      <section class="lr-paper">
        <header class="lr-head">
          <h2 class="lr-title"></h2>
          <div class="lr-langs" role="radiogroup"></div>
          <button type="button" class="icon-btn lr-close"><svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6 6 18"/></svg></button>
        </header>
        <div class="lr-words" aria-live="off"></div>
        <textarea class="lr-edit input" rows="10" spellcheck="false" hidden></textarea>
        <p class="lr-note" role="status"></p>
        <footer class="lr-bar">
          <button type="button" class="btn ghost small lr-scan" hidden></button>
          <button type="button" class="btn ghost small lr-change" hidden></button>
          <button type="button" class="btn ghost small lr-cancel" hidden></button>
          <button type="button" class="btn primary small lr-save" hidden></button>
          <button type="button" class="btn primary lr-play" hidden>
            <svg class="lr-play-icon" viewBox="0 0 24 24"><path d="m8 5 11 7-11 7Z"/></svg>
            <svg class="lr-pause-icon" viewBox="0 0 24 24"><path d="M8 5v14M16 5v14"/></svg>
            <span></span>
          </button>
        </footer>
        <div class="lr-progress" hidden><span></span></div>
      </section>`;
    const img = overlay.querySelector('.lr-photo img');
    img.src = item.view || item.src || item.thumb || '';
    img.alt = item.title || item.name || '';
    overlay.querySelector('.lr-title').textContent = i18n.t('Read aloud');
    const close = overlay.querySelector('.lr-close');
    close.title = i18n.t('Close');
    close.setAttribute('aria-label', i18n.t('Close'));
    close.onclick = () => this.close();
    overlay.querySelector('.lr-scan').textContent = i18n.t('Read the words from the photo');
    overlay.querySelector('.lr-scan').onclick = () => this.scan();
    overlay.querySelector('.lr-change').textContent = i18n.t('Change the words');
    overlay.querySelector('.lr-change').onclick = () => this.edit();
    overlay.querySelector('.lr-cancel').textContent = i18n.t('Cancel');
    overlay.querySelector('.lr-cancel').onclick = () => { this.editing = false; this.draft = null; this.render(); };
    overlay.querySelector('.lr-save').textContent = i18n.t('Keep the words');
    overlay.querySelector('.lr-save').onclick = () => this.save();
    overlay.querySelector('.lr-play').onclick = () => this.play();
    overlay.querySelector('.lr-edit').placeholder = i18n.t('Type the words of the letter here, line by line as they are written.');
    overlay.querySelector('.lr-edit').setAttribute('aria-label', i18n.t('The words of the letter'));
    const langs = overlay.querySelector('.lr-langs');
    langs.setAttribute('aria-label', i18n.t('Read in'));
    for (const [code, name] of LANGUAGES) {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'lr-lang';
      chip.dataset.lang = code;
      chip.setAttribute('role', 'radio');
      chip.textContent = name;
      chip.onclick = () => this.choose(code);
      langs.appendChild(chip);
    }
    this.viewer.root.appendChild(overlay);
    this.overlay = overlay;
    document.addEventListener('keydown', this.onKey, true);
  }

  $(selector) { return this.overlay?.querySelector(selector); }

  busy(note) {
    if (!this.overlay) return;
    this.overlay.classList.toggle('busy', !!note);
    if (note) this.$('.lr-note').textContent = note;
    for (const button of this.overlay.querySelectorAll('.lr-bar .btn, .lr-lang')) button.disabled = !!note;
  }

  /** The language shown chosen: the draft's, the kept words', else Tamil. */
  language() {
    return this.draft?.language || this.status?.language || 'ta';
  }

  render() {
    const status = this.status;
    if (!this.overlay || !status) return;
    this.busy('');
    const words = this.draft ? this.draft.words : status.words;
    const editing = this.editing;
    const language = this.language();
    for (const chip of this.overlay.querySelectorAll('.lr-lang')) {
      const on = chip.dataset.lang === language;
      chip.classList.toggle('on', on);
      chip.setAttribute('aria-checked', String(on));
    }
    const edit = this.$('.lr-edit');
    edit.hidden = !editing;
    if (editing && document.activeElement !== edit) edit.value = words || '';
    edit.lang = language;
    const shown = this.$('.lr-words');
    shown.hidden = editing;
    shown.lang = language;
    this.paint(shown, status.reading?.segments, status.words);
    this.$('.lr-scan').hidden = !editing || !status.reader;
    this.$('.lr-change').hidden = editing;
    this.$('.lr-cancel').hidden = !editing || !status.words;
    this.$('.lr-save').hidden = !editing;
    const voice = status.voices?.[language] || {};
    const play = this.$('.lr-play');
    play.hidden = editing || !status.words;
    play.disabled = !voice.ready;
    play.querySelector('span').textContent = this.playing() ? i18n.t('Pause') : i18n.t('Read aloud');
    play.classList.toggle('playing', this.playing());
    let note = '';
    if (editing && !words) {
      note = status.reader
        ? i18n.t('No words are kept for this photo yet. Read them from the photo, or type them.')
        : i18n.t('No words are kept for this photo yet. Type them, and they can be read aloud.');
    } else if (editing && this.draft?.source === 'read') {
      note = i18n.t('Check the words against the photo and correct them; handwriting is often misread.');
    } else if (!editing && !voice.ready) {
      note = i18n.t(voice.why || i18n.key('The voice is not downloaded (AI models page)'));
    } else if (!editing) {
      note = i18n.t('Press a sentence to hear it from there.');
    }
    this.$('.lr-note').textContent = note;
  }

  /** The kept words as sentences to light up, or as plain lines. */
  paint(box, segments, words) {
    box.replaceChildren();
    this.lines = [];
    if (segments?.length) {
      for (const [n, segment] of segments.entries()) {
        const span = document.createElement('span');
        span.className = 'lr-sentence';
        span.textContent = segment.text;
        span.tabIndex = 0;
        span.onclick = () => this.play(n);
        span.onkeydown = (event) => { if (event.key === 'Enter') { event.preventDefault(); this.play(n); } };
        box.append(span, ' ');
        if (segment.paragraph) box.append(Object.assign(document.createElement('span'), { className: 'lr-break' }));
        this.lines.push(span);
      }
      return;
    }
    for (const paragraph of String(words || '').split(/\n\s*\n/)) {
      const p = document.createElement('p');
      p.textContent = paragraph;
      box.appendChild(p);
    }
  }

  choose(code) {
    if (!this.status) return;
    if (this.editing) {
      this.draft = { words: this.$('.lr-edit').value, source: 'typed', ...this.draft, language: code };
      this.render();
      return;
    }
    if (code === this.status.language) return;
    // Kept words read in the other voice: kept again with that language.
    this.keep({ words: this.status.words, language: code, source: this.status.source });
  }

  edit() {
    this.stop();
    this.editing = true;
    this.draft = null;
    this.render();
    this.$('.lr-edit').focus();
  }

  async scan() {
    const item = this.item;
    this.busy(i18n.t('Reading the words on this computer…'));
    try {
      const found = await call(`/api/asset/${item.id}/letter/read`, { method: 'POST' });
      if (this.item !== item || !this.overlay) return;
      if (!found.words) {
        this.render();
        this.$('.lr-note').textContent = i18n.t('No words could be read on this photo. They can be typed instead.');
        return;
      }
      this.draft = { words: found.words, language: found.language, source: 'read' };
      this.$('.lr-edit').value = found.words;
      this.render();
    } catch (error) {
      this.render();
      this.viewer.toast?.(i18n.t(error.message || i18n.key('The words could not be read.')), true);
    }
  }

  save() {
    const words = this.$('.lr-edit').value;
    const draft = this.draft || {};
    // Words the reader found and nobody changed are kept as read.
    const source = draft.source === 'read' && draft.words === words ? 'read' : 'typed';
    this.keep({ words, language: draft.language || this.language(), source });
  }

  async keep(body) {
    const item = this.item;
    this.busy(i18n.t('Keeping the words…'));
    try {
      const status = await call(`/api/asset/${item.id}/letter`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      });
      if (this.item !== item || !this.overlay) return;
      this.stop();
      this.status = status;
      this.editing = false;
      this.draft = null;
      this.render();
    } catch (error) {
      this.render();
      this.viewer.toast?.(i18n.t(error.message || i18n.key('The words could not be kept.')), true);
    }
  }

  playing() { return !!this.audio && !this.audio.paused; }

  async play(from = null) {
    if (this.playing() && from === null) {
      this.audio.pause();
      this.render();
      return;
    }
    const item = this.item;
    if (!this.status?.reading) {
      this.busy(i18n.t('Reading aloud on this computer… a long letter takes a minute.'));
      try {
        const status = await call(`/api/asset/${item.id}/letter/voice`, { method: 'POST' });
        if (this.item !== item || !this.overlay) return;
        this.status = status;
        this.render();
      } catch (error) {
        this.render();
        this.viewer.toast?.(i18n.t(error.message || i18n.key('These words could not be read aloud.')), true);
        return;
      }
    }
    const reading = this.status.reading;
    if (!reading) return;
    if (!this.audio || this.audio.dataset.src !== reading.audio) {
      this.stop();
      const audio = new Audio(reading.audio);
      audio.dataset.src = reading.audio;
      audio.preload = 'auto';
      audio.ontimeupdate = () => this.follow();
      audio.onended = () => { this.light(-1); this.render(); };
      audio.onpause = () => this.render();
      audio.onerror = () => this.viewer.toast?.(i18n.t('This reading could not be played.'), true);
      this.audio = audio;
    }
    if (from !== null) this.audio.currentTime = reading.segments[from]?.start || 0;
    try { await this.audio.play(); } catch { /* the browser wants a press first */ }
    this.$('.lr-progress').hidden = false;
    this.render();
  }

  follow() {
    const audio = this.audio;
    const reading = this.status?.reading;
    if (!audio || !reading) return;
    const at = audio.currentTime;
    const index = reading.segments.findIndex((s) => at >= s.start && at < s.end + 0.3);
    this.light(index);
    const bar = this.$('.lr-progress span');
    if (bar && reading.duration) bar.style.width = `${Math.min(100, (at / reading.duration) * 100)}%`;
  }

  light(index) {
    if (this.lit === index) return;
    this.lit = index;
    for (const [n, line] of (this.lines || []).entries()) line.classList.toggle('now', n === index);
    const now = this.lines?.[index];
    now?.scrollIntoView?.({ block: 'nearest', behavior: 'smooth' });
  }

  stop() {
    if (this.audio) {
      this.audio.onpause = null;
      this.audio.pause();
      this.audio.removeAttribute('src');
      this.audio.load?.();
    }
    this.audio = null;
    this.lit = null;
  }

  key(event) {
    if (!this.overlay) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      if (this.editing && this.status?.words) {
        this.editing = false;
        this.draft = null;
        this.render();
      } else {
        this.close();
      }
    }
  }

  /** While open, the viewer's own keys (arrows, F, Space) are left alone:
   *  the viewer asks this before it handles one. */
  get isOpen() { return !!this.overlay; }

  close() {
    document.removeEventListener('keydown', this.onKey, true);
    this.stop();
    this.asked++;
    this.overlay?.remove();
    this.overlay = null;
    this.status = null;
    this.draft = null;
  }
}
