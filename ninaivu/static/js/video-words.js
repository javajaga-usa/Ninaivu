/**
 * Words spoken in videos (`video_words`): above the results of a search, the
 * lines said in the family's videos that hold the words, each opening the
 * video at that second with the words shown under it.
 *
 * The server keeps what was said in each video, line by line, as the
 * on-device speech model wrote it down (ninaivu/storage/video_words.py), and
 * answers a search with the videos and their matching lines, already
 * narrowed to what this viewer may see (ninaivu/api/api_video_words.py).
 * This draws them in the same strip the On This Day memories use.
 */
import * as i18n from './i18n.js';

/** The most lines shown in the strip. */
const MOST = 16;

function clock(seconds) {
  const s = Math.max(0, Math.round(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const rest = String(s % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${rest}` : `${m}:${rest}`;
}

/** *text* into *box*, every word of *words* found in it marked. */
function marked(box, text, words) {
  const lower = text.toLowerCase();
  const spans = [];
  words.forEach((word) => {
    for (let at = lower.indexOf(word); word && at >= 0; at = lower.indexOf(word, at + word.length)) {
      spans.push([at, at + word.length]);
    }
  });
  spans.sort((a, b) => a[0] - b[0]);
  let from = 0;
  spans.forEach(([start, end]) => {
    if (start < from) return;
    box.append(text.slice(from, start));
    const mark = document.createElement('mark');
    mark.textContent = text.slice(start, end);
    box.append(mark);
    from = end;
  });
  box.append(text.slice(from));
}

export class VideoWords {
  /**
   * @param {HTMLElement} banner  the strip's section (#words-banner)
   * @param {{open: (ids: number[], index: number, at: number) => void}} options
   */
  constructor(banner, { open }) {
    this.banner = banner;
    this.carousel = banner?.querySelector('#words-carousel');
    this.open = open;
    this.controller = null;
    this.asked = '';
  }

  hide() {
    this.controller?.abort();
    this.asked = '';
    if (this.banner) this.banner.hidden = true;
  }

  /** Look for *query* in what was said in the videos; the strip shows only
   *  when found. True when it is showing. */
  async show(query) {
    const text = String(query || '').trim();
    if (!this.banner || !this.carousel || !text) {
      this.hide();
      return false;
    }
    if (text === this.asked && !this.banner.hidden) return true;
    this.controller?.abort();
    this.controller = new AbortController();
    this.asked = text;
    let data;
    try {
      const response = await fetch(`/api/video-words?q=${encodeURIComponent(text)}`, {
        signal: this.controller.signal, headers: { Accept: 'application/json' },
      });
      if (!response.ok) throw new Error(String(response.status));
      data = await response.json();
    } catch (error) {
      if (error.name !== 'AbortError') this.banner.hidden = true;
      return false;
    }
    if (text !== this.asked) return false;
    this.render(data.clips || [], text);
    return !this.banner.hidden;
  }

  render(clips, query = '') {
    // The first line of each video first, then the second of each, so one
    // long video does not fill the strip on its own.
    const order = [];
    for (let round = 0; order.length < MOST; round += 1) {
      const before = order.length;
      clips.forEach((clip) => {
        const line = clip.lines?.[round];
        if (line && order.length < MOST) order.push({ clip, line });
      });
      if (order.length === before) break;
    }
    this.carousel.innerHTML = '';
    if (!order.length) {
      this.banner.hidden = true;
      return;
    }
    const words = query.toLowerCase().split(/\s+/)
      .map((w) => w.replace(/^[.,!?;:"'()[\]]+|[.,!?;:"'()[\]]+$/g, '')).filter(Boolean);
    const english = i18n.language() === 'en';
    const ids = clips.map((clip) => clip.id);
    order.forEach(({ clip, line }) => {
      const card = document.createElement('button');
      card.type = 'button';
      card.className = 'memory-card moment-card words-card';
      card.dataset.id = String(clip.id);
      card.dataset.at = String(line.at);
      const img = document.createElement('img');
      img.src = `/api/video-words/${clip.id}/frame?at=${line.at}`;
      img.alt = '';
      img.loading = 'lazy';
      const badge = document.createElement('span');
      badge.className = 'memory-badge';
      badge.textContent = clock(line.at);
      const quote = document.createElement('q');
      quote.className = 'words-quote';
      // What was said, as it was said; on an English page, its English when
      // the words searched for are there too.
      const said = line.text || line.english;
      const translated = english && line.english && line.english !== said
        && words.every((w) => line.english.toLowerCase().includes(w));
      marked(quote, translated ? line.english : said, words);
      quote.lang = translated ? 'en' : (line.language || '');
      const meta = document.createElement('div');
      meta.className = 'memory-meta';
      meta.textContent = [clip.date, clip.name].filter(Boolean).join(' · ');
      card.setAttribute('aria-label', i18n.t('Open {name} at {time}', {
        name: clip.name || '', time: clock(line.at),
      }));
      card.title = line.english && line.english !== said ? `${said}\n${line.english}` : said;
      card.append(img, badge, quote, meta);
      card.onclick = () => this.open(ids, ids.indexOf(clip.id), line.at);
      this.carousel.appendChild(card);
    });
    this.banner.hidden = false;
  }
}
