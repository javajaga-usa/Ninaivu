/**
 * Voice stories in the photo viewer: hear what the family said about a
 * photograph, and say something yourself.
 *
 * Recording is the browser's own MediaRecorder: WebM or Ogg on most browsers,
 * MP4 on an iPhone, kept as it was made (ninaivu/storage/stories.py). The
 * microphone is asked for when Record is pressed and let go of when it stops,
 * never before — somebody opening a photograph has not agreed to be listened
 * to. A file already on the device can be uploaded instead, which is also the
 * way in where recording is not offered: a browser lets a page use the
 * microphone only over HTTPS.
 *
 * Nothing is sent anywhere but Ninaivu, and Ninaivu sends it nowhere else.
 */

import { api } from './api.js';
import * as i18n from './i18n.js';

/** The longest recording, in seconds, and the largest file, as the server has them. */
const MAX_SECONDS = 300;
const MAX_BYTES = 25 * 1024 * 1024;

/** Containers in the order to ask for them. An iPhone records only MP4. */
const RECORD_TYPES = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg;codecs=opus'];

function clock(seconds) {
  const s = Math.max(0, Math.round(Number(seconds) || 0));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

function recordType() {
  if (typeof MediaRecorder === 'undefined' || !MediaRecorder.isTypeSupported) return '';
  return RECORD_TYPES.find((type) => MediaRecorder.isTypeSupported(type)) || '';
}

function fileName(type) {
  if (/mp4|m4a|aac/.test(type)) return 'story.m4a';
  if (/ogg/.test(type)) return 'story.ogg';
  return 'story.webm';
}

export class StoryPanel {
  constructor(viewer) {
    this.viewer = viewer;
    const q = (sel) => viewer.root.querySelector(sel);
    this.panel = q('#viewer-stories');
    this.button = q('#v-stories');
    if (!this.panel || !this.button) return;
    this.count = q('#v-stories-count');
    this.list = q('#stories-list');
    this.empty = q('#stories-empty');
    this.actions = q('#stories-actions');
    this.form = q('#story-recorder');
    this.timer = q('#story-timer');
    this.dot = q('#story-rec-dot');
    this.stopBtn = q('#story-stop');
    this.redoBtn = q('#story-redo');
    this.preview = q('#story-preview');
    this.text = q('#story-text');
    this.speaker = q('#story-speaker');
    this.status = q('#story-status');
    this.save = q('#story-save');
    this.file = q('#story-file');

    /** Set by the app: the signed-in person's name, the speaker by default,
     *  and whether they may tell a story at all (a guest only listens). */
    this.userName = '';
    this.canTell = true;
    this.item = null;
    /** What is being recorded or waits to be saved, and for which item. */
    this.draft = null;
    this.recorder = null;
    this.stream = null;
    this.ticker = null;

    this.button.onclick = () => this.toggle();
    q('#v-stories-close').onclick = () => this.toggle(false, true);
    q('#story-record-start').onclick = () => this.record();
    q('#story-upload').onclick = () => this.file.click();
    this.file.onchange = () => this.takeFile(this.file.files?.[0]);
    this.stopBtn.onclick = () => this.stop();
    this.redoBtn.onclick = () => this.record();
    q('#story-cancel').onclick = () => this.discard();
    this.form.onsubmit = (event) => { event.preventDefault(); this.send(); };
    this.preview.addEventListener('loadedmetadata', () => {
      if (this.draft && !this.draft.duration && Number.isFinite(this.preview.duration)) {
        this.draft.duration = this.preview.duration;
      }
    });
    // What is typed or pressed in here is for the panel, not a shortcut for
    // the photograph behind it — an arrow key in the words is not "next".
    this.panel.addEventListener('keydown', (event) => {
      event.stopPropagation();
      if (event.key === 'Escape') { event.preventDefault(); this.toggle(false, true); }
    });
  }

  get isOpen() {
    return !!this.panel && !this.panel.hidden;
  }

  /** The viewer moved to *item*: its count, and its stories if open. */
  show(item) {
    if (!this.panel) return;
    if (this.item?.id !== item.id && this.recorder) this.stop();
    this.item = item;
    this.setCount(item.stories || 0);
    this.renderDraft();
    if (this.isOpen) this.load();
  }

  setCount(n) {
    if (!this.count) return;
    this.count.textContent = n ? String(n) : '';
    this.count.hidden = !n;
    // A guest only listens, so for them the button is there only when there
    // is something to hear.
    this.button.hidden = !n && !this.canTell;
    if (this.button.hidden && this.isOpen) this.toggle(false);
    const label = n ? i18n.t('Stories ({count})', { count: n }) : i18n.t('Stories');
    this.button.title = label;
    this.button.setAttribute('aria-label', label);
  }

  toggle(force, returnFocus = false) {
    if (!this.panel) return;
    const open = (force === undefined ? this.panel.hidden : !!force) && !this.button.hidden;
    if (open) this.viewer.toggleInfo(false);
    this.panel.hidden = !open;
    this.button.classList.toggle('on', open);
    this.button.setAttribute('aria-expanded', String(open));
    this.viewer.root.classList.toggle('info-open', open || !this.viewer.info.hidden);
    if (open) {
      this.load();
      this.panel.querySelector('#v-stories-close').focus({ preventScroll: true });
    } else if (returnFocus) {
      this.button.focus();
    }
  }

  /** The viewer closed: nothing keeps the microphone. */
  close() {
    if (!this.panel) return;
    this.discard();
    this.panel.hidden = true;
    this.button.classList.remove('on');
    this.button.setAttribute('aria-expanded', 'false');
  }

  async load() {
    const item = this.item;
    if (!item) return;
    let answer;
    try {
      answer = await api.stories(item.id);
    } catch {
      if (this.item === item) this.say(i18n.t('The stories could not be loaded.'));
      return;
    }
    if (this.item !== item) return;                  // moved on meanwhile
    this.canAdd = !!answer.can_add;
    this.actions.hidden = !this.canAdd || !!this.draftHere();
    this.render(answer.stories || []);
  }

  render(stories) {
    this.setCount(stories.length);
    this.remember(stories.length);
    this.list.replaceChildren();
    this.empty.hidden = stories.length > 0;
    for (const story of stories) {
      const row = document.createElement('li');
      row.className = 'story';
      const head = document.createElement('div');
      head.className = 'story-head';
      const who = document.createElement('strong');
      who.textContent = story.speaker || i18n.t('Someone in the family');
      const when = document.createElement('span');
      when.className = 'hint';
      const day = story.created_at
        ? new Date(story.created_at * 1000).toLocaleDateString(i18n.locale(),
          { day: 'numeric', month: 'short', year: 'numeric' })
        : '';
      when.textContent = [day, story.duration ? clock(story.duration) : ''].filter(Boolean).join(' · ');
      head.append(who, when);
      const audio = document.createElement('audio');
      audio.controls = true;
      audio.preload = 'none';
      audio.src = story.src;
      audio.setAttribute('aria-label', i18n.t('Story told by {name}', { name: who.textContent }));
      row.append(head, audio);
      if (story.text) {
        const words = document.createElement('p');
        words.className = 'story-text';
        words.textContent = story.text;
        row.appendChild(words);
      }
      if (story.can_delete) {
        const remove = document.createElement('button');
        remove.type = 'button';
        remove.className = 'btn ghost small danger';
        remove.textContent = i18n.t('Delete');
        remove.setAttribute('aria-label', i18n.t('Delete the story told by {name}', { name: who.textContent }));
        remove.onclick = () => this.remove(story, remove);
        row.appendChild(remove);
      }
      this.list.appendChild(row);
    }
  }

  /** Keep the viewer's copy of the item in step, so the count is right when
   *  somebody comes back to it. */
  remember(n) {
    if (!this.item) return;
    this.item.stories = n;
    const cached = this.viewer.cache?.get(this.item.id);
    if (cached) cached.stories = n;
  }

  async remove(story, button) {
    if (!confirm(i18n.t('Delete this story? It cannot be undone.'))) return;
    button.disabled = true;
    try {
      await api.deleteStory(story.id);
      this.viewer.toast?.(i18n.t('Story deleted.'));
      await this.load();
    } catch (err) {
      button.disabled = false;
      this.viewer.toast?.(err.message || i18n.t('The story could not be deleted.'), true);
    }
  }

  /* -- recording ------------------------------------------------------- */

  draftHere() {
    return this.draft && this.item && this.draft.itemId === this.item.id ? this.draft : null;
  }

  say(text) {
    if (this.status) this.status.textContent = text || '';
  }

  /** Show the recorder for this item's draft, or the buttons that start one. */
  renderDraft() {
    const draft = this.draftHere();
    this.form.hidden = !draft;
    this.actions.hidden = !this.canAdd || !!draft;
    this.empty.hidden = !!draft || this.list.childElementCount > 0;
    if (!draft) return;
    const recording = !!this.recorder;
    this.stopBtn.hidden = !recording;
    this.redoBtn.hidden = recording || draft.uploaded;
    this.dot.classList.toggle('live', recording);
    this.preview.hidden = recording || !draft.blob;
    this.save.disabled = recording || !draft.blob;
    this.timer.textContent = clock(draft.duration || 0);
  }

  async record() {
    if (!this.item) return;
    this.release();
    const type = recordType();
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
      // A page reached over plain http is not allowed the microphone at all.
      this.startDraft({ uploaded: false });
      this.say(window.isSecureContext === false
        ? i18n.t('This browser only lets Ninaivu use the microphone over a secure (https) address. Upload a recording instead.')
        : i18n.t('This browser cannot record sound. Upload a recording instead.'));
      return;
    }
    this.startDraft({ uploaded: false });
    this.say(i18n.t('Asking to use the microphone…'));
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      this.say(i18n.t('The microphone was not allowed. Allow it in the browser and try again, or upload a recording.'));
      return;
    }
    const draft = this.draft;
    // The viewer may have moved to another photograph while the browser asked
    // for the microphone: recording then would leave it on with no Stop button.
    if (!draft || draft !== this.draftHere()) {
      stream.getTracks().forEach((track) => track.stop());
      return;
    }
    this.stream = stream;
    const chunks = [];
    const recorder = type ? new MediaRecorder(stream, { mimeType: type }) : new MediaRecorder(stream);
    this.recorder = recorder;
    recorder.ondataavailable = (event) => { if (event.data?.size) chunks.push(event.data); };
    recorder.onstop = () => {
      const blob = new Blob(chunks, { type: (recorder.mimeType || type || chunks[0]?.type || 'audio/webm') });
      this.release();
      if (this.draft !== draft) return;               // discarded meanwhile
      draft.blob = blob;
      draft.duration = Math.min(MAX_SECONDS, (Date.now() - draft.started) / 1000);
      this.setPreview(blob);
      this.say(i18n.t('Listen back, add the words if you like, then save.'));
      this.renderDraft();
    };
    draft.started = Date.now();
    recorder.start(1000);
    this.say(i18n.t('Recording… press Stop when you have finished.'));
    this.ticker = setInterval(() => {
      const seconds = (Date.now() - draft.started) / 1000;
      this.timer.textContent = clock(seconds);
      if (seconds >= MAX_SECONDS) this.stop();
    }, 250);
    this.renderDraft();
    this.stopBtn.focus({ preventScroll: true });
  }

  startDraft({ uploaded }) {
    const keep = this.draftHere();
    this.draft = {
      itemId: this.item.id, blob: null, duration: 0, uploaded,
      text: keep ? this.text.value : '', speaker: keep ? this.speaker.value : '',
    };
    if (!keep) {
      this.text.value = '';
      this.speaker.value = this.userName || '';
    }
    this.setPreview(null);
    this.say('');
    this.renderDraft();
  }

  stop() {
    clearInterval(this.ticker);
    this.ticker = null;
    if (this.recorder && this.recorder.state !== 'inactive') this.recorder.stop();
    else this.release();
  }

  /** Let go of the microphone. */
  release() {
    clearInterval(this.ticker);
    this.ticker = null;
    this.stream?.getTracks().forEach((track) => track.stop());
    this.stream = null;
    this.recorder = null;
  }

  setPreview(blob) {
    if (this.preview.src) URL.revokeObjectURL(this.preview.src);
    this.preview.removeAttribute('src');
    if (blob) this.preview.src = URL.createObjectURL(blob);
    else this.preview.load?.();
  }

  takeFile(file) {
    this.file.value = '';
    if (!file || !this.item) return;
    this.release();
    this.startDraft({ uploaded: true });
    if (file.type && !file.type.startsWith('audio/')) {
      this.say(i18n.t('That file is not a sound recording.'));
      return;
    }
    if (file.size > MAX_BYTES) {
      this.say(i18n.t('A story can be at most 25 MB.'));
      return;
    }
    this.draft.blob = file;
    this.draft.name = file.name;
    this.setPreview(file);
    this.say(i18n.t('Listen back, add the words if you like, then save.'));
    this.renderDraft();
  }

  discard() {
    if (this.recorder && this.recorder.state !== 'inactive') {
      this.recorder.onstop = null;
      this.recorder.stop();
    }
    this.release();
    this.draft = null;
    this.setPreview(null);
    this.say('');
    if (this.form) this.renderDraft();
  }

  async send() {
    const draft = this.draftHere();
    if (!draft?.blob || this.save.disabled) return;
    const form = new FormData();
    form.append('audio', draft.blob, draft.name || fileName(draft.blob.type || ''));
    form.append('text', this.text.value.trim());
    form.append('speaker', this.speaker.value.trim());
    if (draft.duration) form.append('duration', String(Math.round(draft.duration * 10) / 10));
    this.save.disabled = true;
    this.say(i18n.t('Saving…'));
    try {
      await api.addStory(draft.itemId, form);
    } catch (err) {
      this.save.disabled = false;
      this.say(err.message || i18n.t('The story could not be saved.'));
      return;
    }
    this.discard();
    this.viewer.toast?.(i18n.t('Story saved.'));
    await this.load();
    this.panel.querySelector('#story-record-start')?.focus({ preventScroll: true });
  }
}
