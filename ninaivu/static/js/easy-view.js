/**
 * Easy view (`easy_view`): the family app made calmer and larger for a
 * grandparent, per profile.
 *
 * While a profile has it on (their own profile sheet, or an administrator on
 * the console's People page), the page carries `html.easy-view`: bigger
 * words and buttons, the largest photographs in the timeline, and the
 * buttons a grandparent never needs (adding, making, the view and size
 * controls) out of the way. In the viewer, under every photograph, it says
 * in large letters who is in it, where and when it was taken, and how long
 * ago that was (ninaivu/features/easy_view/media.py); "Tell me" reads it
 * aloud in the calm voice letters are read in, or the device's own voice
 * when the home computer has none. "Tell me each one" reads each photograph
 * as it is opened, and is remembered on this device.
 */

import * as i18n from './i18n.js';
import { store } from './store.js';
import { toast } from './ui.js';

async function call(url, options = {}) {
  const response = await fetch(url, { credentials: 'same-origin', ...options });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || data.message || data.description || '');
  return data;
}

/** The largest step of the timeline's size slider (layout.js ZOOM_STEPS). */
const LARGEST = 4;

const SPEAKER = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 9.5h3.5L12 5.5v13l-4.5-4H4z"/>'
  + '<path d="M15.5 9a4 4 0 0 1 0 6M18 6.5a7.5 7.5 0 0 1 0 11"/></svg>';

export class EasyView {
  /**
   * @param {{ viewer: object, grid: object, zoomInput: HTMLInputElement }} parts
   */
  constructor({ viewer, grid, zoomInput }) {
    this.viewer = viewer;
    this.grid = grid;
    this.zoomInput = zoomInput;
    this.on = false;
    this.voices = {};
    this.item = null;
    this.told = new Map();       // `${id}:${language}` → answer
    this.audio = null;
    this.asked = 0;
    this.zoomBefore = null;
    this.eachOne = !!store.get('easyViewEachOne', false);
    this.build();
    viewer.easy = this;
  }

  build() {
    const strip = document.createElement('div');
    strip.className = 'easy-told';
    strip.hidden = true;
    strip.setAttribute('aria-live', 'polite');
    this.words = document.createElement('p');
    this.words.className = 'easy-words';
    const row = document.createElement('div');
    row.className = 'easy-told-row';
    this.tell = document.createElement('button');
    this.tell.type = 'button';
    this.tell.className = 'btn easy-tell';
    this.tell.innerHTML = `${SPEAKER}<span></span>`;
    this.tell.onclick = () => (this.audio || this.speaking ? this.stop() : this.say(this.item, true));
    this.each = document.createElement('label');
    this.each.className = 'easy-each';
    const box = document.createElement('input');
    box.type = 'checkbox';
    box.checked = this.eachOne;
    box.onchange = () => {
      this.eachOne = box.checked;
      try { store.set('easyViewEachOne', this.eachOne); } catch { /* private window */ }
      if (this.eachOne) this.say(this.item, true);
    };
    this.eachLabel = document.createElement('span');
    this.each.append(box, this.eachLabel);
    row.append(this.tell, this.each);
    strip.append(this.words, row);
    this.strip = strip;
    this.viewer.root.appendChild(strip);
    this.label();
    i18n.onChange?.(() => { this.label(); this.told.clear(); if (this.item) this.show(this.item); });
  }

  label() {
    this.tell.querySelector('span').textContent = this.audio || this.speaking
      ? i18n.t('Stop') : i18n.t('Tell me');
    this.eachLabel.textContent = i18n.t('Tell me each one');
  }

  /** Whether the person signed in has easy view on; asked by the app when
   *  the feature is switched on for a family member or administrator. */
  async refresh() {
    try {
      const mine = await call('/api/easy-view');
      this.voices = mine.voices || {};
      this.apply(!!mine.on);
    } catch {
      this.apply(false);
    }
  }

  /** Set it for the person signed in (their profile sheet). */
  async set(on) {
    const mine = await call('/api/easy-view', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ on }),
    });
    this.voices = mine.voices || {};
    this.apply(!!mine.on);
    return this.on;
  }

  apply(on) {
    if (on === this.on) return;
    this.on = on;
    document.documentElement.classList.toggle('easy-view', on);
    if (on) {
      this.zoomBefore = this.grid?.zoom ?? null;
      this.grid?.setZoom(LARGEST);
    } else {
      this.stop();
      if (this.zoomBefore !== null) this.grid?.setZoom(this.zoomBefore);
      this.zoomBefore = null;
    }
    if (this.zoomInput && this.grid) this.zoomInput.value = String(this.grid.zoom);
    if (this.item && this.viewer.isOpen) this.show(this.item);
    else this.strip.hidden = true;
  }

  /** Turned off for everyone (the switch) or a guest signed in. */
  off() { this.apply(false); }

  language() {
    return i18n.language() === 'en' ? 'en' : 'ta';
  }

  /** Called by the viewer for every item it shows. */
  show(item) {
    this.stop();
    this.item = item;
    if (!this.on || !item) { this.strip.hidden = true; return; }
    this.strip.hidden = false;
    this.words.textContent = '';
    this.fetch(item).then((answer) => {
      if (!answer || this.item !== item) return;
      this.words.textContent = answer.sentences.join(' ');
      if (this.eachOne && item.kind !== 'video') this.say(item, false);
    });
  }

  close() {
    this.stop();
    this.item = null;
    this.strip.hidden = true;
  }

  async fetch(item) {
    const language = this.language();
    const key = `${item.id}:${language}`;
    if (this.told.has(key)) return this.told.get(key);
    try {
      const answer = await call(`/api/asset/${item.id}/told?language=${language}`);
      this.told.set(key, answer);
      return answer;
    } catch {
      return null;
    }
  }

  async say(item, pressed) {
    if (!item || !this.on) return;
    this.stop();
    const asked = ++this.asked;
    const answer = await this.fetch(item);
    if (!answer || asked !== this.asked || this.item !== item) return;
    let audio = answer.audio;
    if (!audio && answer.voice?.ready) {
      this.busy(true);
      try {
        const made = await call(`/api/asset/${item.id}/told/voice`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ language: answer.language }),
        });
        this.told.set(`${item.id}:${answer.language}`, made);
        audio = made.audio;
      } catch (err) {
        if (pressed && err.message) toast(err.message, true);
      } finally {
        this.busy(false);
      }
      if (asked !== this.asked || this.item !== item) return;
    }
    if (audio) this.play(audio);
    else this.device(answer, pressed);
  }

  busy(on) {
    this.tell.disabled = on;
    this.tell.classList.toggle('busy', on);
  }

  play(url) {
    const audio = new Audio(url);
    this.audio = audio;
    audio.onended = () => { if (this.audio === audio) { this.audio = null; this.label(); } };
    audio.play().catch(() => { this.audio = null; this.label(); });
    this.label();
  }

  /** The device's own voice, when the home computer has none. */
  device(answer, pressed) {
    const synth = globalThis.speechSynthesis;
    if (!synth || typeof SpeechSynthesisUtterance === 'undefined') {
      if (pressed) toast(i18n.t('This device cannot read aloud.'), true);
      return;
    }
    const lang = answer.language === 'en' ? 'en' : 'ta';
    const voice = synth.getVoices().find((v) => v.lang?.toLowerCase().startsWith(lang));
    if (!voice && lang === 'ta' && synth.getVoices().length) {
      if (pressed) toast(i18n.t('This device has no Tamil voice. The words are written under the photograph.'), true);
      return;
    }
    const utterance = new SpeechSynthesisUtterance(answer.sentences.join(' '));
    utterance.lang = lang === 'ta' ? 'ta-IN' : 'en-IN';
    if (voice) utterance.voice = voice;
    utterance.rate = 0.9;
    utterance.onend = () => { this.speaking = false; this.label(); };
    this.speaking = true;
    synth.speak(utterance);
    this.label();
  }

  stop() {
    this.asked += 1;
    if (this.audio) {
      this.audio.pause();
      this.audio = null;
    }
    if (this.speaking) {
      globalThis.speechSynthesis?.cancel();
      this.speaking = false;
    }
    this.busy(false);
    this.label();
  }
}
