/**
 * Moments inside videos (`video_moments`): above the results of a search, the
 * seconds inside long videos that match it, each opening the clip there.
 *
 * The server keeps every long clip's scenes as picture-search vectors
 * (ninaivu/storage/video_moments.py) and answers a search with the clips and
 * the seconds of their best scenes, already narrowed to what this viewer may
 * see (ninaivu/api/api_video_moments.py). This draws them in the same strip
 * the On This Day memories use, and hands the viewer the second to start at.
 */
import * as i18n from './i18n.js';

/** The most scenes shown in the strip. */
const MOST = 16;

function clock(seconds) {
  const s = Math.max(0, Math.round(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const rest = String(s % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${rest}` : `${m}:${rest}`;
}

export class VideoMoments {
  /**
   * @param {HTMLElement} banner  the strip's section (#moments-banner)
   * @param {{open: (ids: number[], index: number, at: number) => void}} options
   */
  constructor(banner, { open }) {
    this.banner = banner;
    this.carousel = banner?.querySelector('#moments-carousel');
    this.open = open;
    this.controller = null;
    this.asked = '';
  }

  hide() {
    this.controller?.abort();
    this.asked = '';
    if (this.banner) this.banner.hidden = true;
  }

  /** Look for *query* inside the videos; the strip shows only when found.
   *  True when it is showing. */
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
      const response = await fetch(`/api/moments?q=${encodeURIComponent(text)}`, {
        signal: this.controller.signal, headers: { Accept: 'application/json' },
      });
      if (!response.ok) throw new Error(String(response.status));
      data = await response.json();
    } catch (error) {
      if (error.name !== 'AbortError') this.banner.hidden = true;
      return false;
    }
    if (text !== this.asked) return false;
    this.render(data.clips || []);
    return !this.banner.hidden;
  }

  render(clips) {
    const cards = [];
    clips.forEach((clip) => {
      (clip.moments || []).forEach((moment) => cards.push({ clip, moment }));
    });
    // Best scene of each clip first, then the second-best of each, so one
    // long video does not fill the strip on its own.
    const order = [];
    for (let round = 0; order.length < Math.min(MOST, cards.length); round += 1) {
      const before = order.length;
      clips.forEach((clip) => {
        const moment = clip.moments?.[round];
        if (moment && order.length < MOST) order.push({ clip, moment });
      });
      if (order.length === before) break;
    }
    this.carousel.innerHTML = '';
    if (!order.length) {
      this.banner.hidden = true;
      return;
    }
    const ids = clips.map((clip) => clip.id);
    order.forEach(({ clip, moment }) => {
      const card = document.createElement('button');
      card.type = 'button';
      card.className = 'memory-card moment-card';
      card.dataset.id = String(clip.id);
      card.dataset.at = String(moment.at);
      const img = document.createElement('img');
      img.src = `/api/moments/${clip.id}/frame?at=${moment.at}`;
      img.alt = '';
      img.loading = 'lazy';
      const badge = document.createElement('span');
      badge.className = 'memory-badge';
      badge.textContent = clock(moment.at);
      const meta = document.createElement('div');
      meta.className = 'memory-meta';
      meta.textContent = [clip.date, clip.name].filter(Boolean).join(' · ');
      card.setAttribute('aria-label', i18n.t('Open {name} at {time}', {
        name: clip.name || '', time: clock(moment.at),
      }));
      card.append(img, badge, meta);
      card.onclick = () => this.open(ids, ids.indexOf(clip.id), moment.at);
      this.carousel.appendChild(card);
    });
    this.banner.hidden = false;
  }
}
