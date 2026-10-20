/**
 * Speaking to search: the microphone in the search box (`voice_search`).
 *
 * Press it and say what you remember, in Tamil or English; press again, or
 * stop talking for a moment, and what was said is written down on the home
 * computer (ninaivu/api/api_voice_search.py) and searched as if typed. The
 * microphone is asked for when the button is pressed and let go of as soon
 * as the recording stops; the recording goes to Ninaivu alone, which throws
 * it away once it has been read.
 *
 * A browser lets a page use the microphone only over HTTPS, so on the plain
 * address the button says that instead of recording.
 */

import { reportUnauthorized } from './api.js';
import * as i18n from './i18n.js';

/** The longest a spoken search records for, in seconds. */
const MAX_SECONDS = 12;
/** Seconds of quiet, after something was said, that end the recording. */
const QUIET_SECONDS = 1.4;
/** How loud (RMS of the samples, 0–1) counts as somebody talking. */
const SPEAKING = 0.02;
/** Containers in the order to ask for them. An iPhone records only MP4. */
const RECORD_TYPES = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg;codecs=opus'];

function recordType() {
  if (typeof MediaRecorder === 'undefined' || !MediaRecorder.isTypeSupported) return '';
  return RECORD_TYPES.find((type) => MediaRecorder.isTypeSupported(type)) || '';
}

export class VoiceSearch {
  /**
   * @param {HTMLButtonElement} button  the microphone in the search box
   * @param {{toast: Function, onHeard: (query: string, heard: object) => void}} hooks
   */
  constructor(button, { toast, onHeard }) {
    this.button = button;
    this.toast = toast || (() => {});
    this.onHeard = onHeard;
    this.recorder = null;
    this.stream = null;
    this.audio = null;
    this.busy = false;
    button.addEventListener('click', () => (this.recorder ? this.stop() : this.start()));
    button.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && this.recorder) { event.preventDefault(); this.cancel(); }
    });
  }

  show(on) {
    this.button.hidden = !on;
    this.button.closest('.searchbar')?.classList.toggle('has-voice', !!on);
    if (!on) this.cancel();
  }

  say(state) {
    this.button.classList.toggle('listening', state === 'listening');
    this.button.classList.toggle('hearing', state === 'hearing');
    const label = state === 'listening' ? i18n.t('Listening… press again when you have finished')
      : state === 'hearing' ? i18n.t('Writing down what you said…')
        : i18n.t('Search by speaking');
    this.button.title = label;
    this.button.setAttribute('aria-label', label);
    this.button.setAttribute('aria-pressed', String(state === 'listening'));
  }

  async start() {
    if (this.busy) return;
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
      this.toast(window.isSecureContext === false
        ? i18n.t('This browser only lets Ninaivu use the microphone over a secure (https) address.')
        : i18n.t('This browser cannot record sound.'), true);
      return;
    }
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      this.toast(i18n.t('The microphone was not allowed. Allow it in the browser and try again.'), true);
      return;
    }
    const type = recordType();
    const chunks = [];
    const recorder = type ? new MediaRecorder(stream, { mimeType: type }) : new MediaRecorder(stream);
    this.stream = stream;
    this.recorder = recorder;
    recorder.ondataavailable = (event) => { if (event.data?.size) chunks.push(event.data); };
    recorder.onstop = () => {
      const kept = !recorder.cancelled;
      this.release();
      if (!kept || !chunks.length) { this.say(''); return; }
      this.send(new Blob(chunks, { type: recorder.mimeType || type || chunks[0]?.type || 'audio/webm' }));
    };
    recorder.start(250);
    this.say('listening');
    this.watchForQuiet(stream);
    this.limit = setTimeout(() => this.stop(), MAX_SECONDS * 1000);
  }

  /** Stop by itself once somebody has spoken and then gone quiet. */
  watchForQuiet(stream) {
    const Context = window.AudioContext || window.webkitAudioContext;
    if (!Context) return;
    try {
      const audio = new Context();
      const meter = audio.createAnalyser();
      meter.fftSize = 1024;
      audio.createMediaStreamSource(stream).connect(meter);
      this.audio = audio;
      const samples = new Float32Array(meter.fftSize);
      let spoke = false;
      let quietSince = 0;
      const listen = () => {
        if (!this.recorder || this.audio !== audio) return;
        meter.getFloatTimeDomainData(samples);
        let sum = 0;
        for (const value of samples) sum += value * value;
        const loud = Math.sqrt(sum / samples.length) > SPEAKING;
        const now = performance.now();
        if (loud) { spoke = true; quietSince = 0; } else if (spoke && !quietSince) quietSince = now;
        if (spoke && quietSince && now - quietSince > QUIET_SECONDS * 1000) { this.stop(); return; }
        requestAnimationFrame(listen);
      };
      requestAnimationFrame(listen);
    } catch { /* the button still stops it */ }
  }

  stop() {
    clearTimeout(this.limit);
    if (this.recorder?.state === 'recording') this.recorder.stop();
  }

  cancel() {
    if (!this.recorder) return;
    this.recorder.cancelled = true;
    this.stop();
  }

  /** Let go of the microphone, so the browser's recording light goes out. */
  release() {
    clearTimeout(this.limit);
    this.stream?.getTracks().forEach((track) => track.stop());
    this.audio?.close?.().catch?.(() => {});
    this.stream = null;
    this.audio = null;
    this.recorder = null;
  }

  async send(blob) {
    this.busy = true;
    this.say('hearing');
    try {
      const response = await fetch('/api/voice-search', {
        method: 'POST', body: blob, credentials: 'same-origin',
        headers: { 'Content-Type': blob.type || 'application/octet-stream' },
      });
      if (response.status === 401) reportUnauthorized('/api/voice-search');
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || data.message || i18n.t('That could not be made out. Try again, a little closer.'));
      if (!data.query) {
        this.toast(i18n.t('Nothing was heard. Press the microphone and speak.'), true);
        return;
      }
      this.onHeard?.(data.query, data);
      // A Tamil search is searched in English too: say what was heard.
      if (data.text && data.text !== data.query) this.toast(i18n.t('Heard: {words}', { words: data.text }));
    } catch (error) {
      this.toast(i18n.t(error.message || 'That could not be made out. Try again, a little closer.'), true);
    } finally {
      this.busy = false;
      this.say('');
    }
  }
}
