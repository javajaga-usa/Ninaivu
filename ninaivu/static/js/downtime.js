/**
 * The downtime notice — the family app while Maintenance mode is on.
 *
 * An administrator can give the whole computer to the library's own work for
 * a while (Activity → Sharing this computer → Maintenance, see
 * ninaivu/server/workload.py). The server then answers family members and
 * guests with 503 and `maintenance: true` instead of photographs; this covers
 * the page with a notice saying so, the first time any request is answered
 * that way, and looks every half a minute whether it is over. When it is, the
 * page is loaded again, so the gallery comes back by itself. Administrators
 * are not refused, so they never see it.
 */

import * as i18n from './i18n.js';

/** How often the notice asks whether maintenance is over. */
const LOOK_EVERY = 30000;

const nativeFetch = window.fetch.bind(window);

export class DowntimeNotice {
  constructor() {
    this.root = null;
    this.timer = null;
    this.since = null;
    this.watchFetch();
    // Redrawn in the language just chosen.
    i18n.onChange(() => { if (this.root) this.draw(); });
  }

  watchFetch() {
    if (window.__ninaivuDowntimeFetch) return;
    window.__ninaivuDowntimeFetch = true;
    const previous = window.fetch;
    window.fetch = async (...args) => {
      const response = await previous(...args);
      if (response.status === 503 && !this.root) {
        try {
          const data = await response.clone().json();
          if (data && data.maintenance) this.show();
        } catch { /* not ours: a 503 from something in between */ }
      }
      return response;
    };
  }

  async show() {
    if (this.root) return;
    document.body.classList.add('screen-down');
    this.root = document.createElement('dialog');
    this.root.className = 'lock-screen downtime-screen';
    this.root.setAttribute('aria-labelledby', 'downtime-title');
    // Escape must not be a way past it: there is nothing behind it to use.
    this.root.addEventListener('cancel', (event) => event.preventDefault());
    document.body.appendChild(this.root);
    this.draw();
    try { this.root.showModal(); } catch { /* an old browser: a plain cover */ }
    this.look();
  }

  draw() {
    const card = document.createElement('div');
    card.className = 'lock-card downtime-card';
    const mark = document.createElement('div');
    mark.className = 'downtime-mark';
    mark.setAttribute('aria-hidden', 'true');
    mark.innerHTML = '<svg viewBox="0 0 24 24" width="34" height="34" fill="none" '
      + 'stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">'
      + '<path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18l3 3 6.3-6.3a4 4 0 0 0 5.4-5.4l-2.6 2.6-2.4-.6-.6-2.4z"/></svg>';
    const title = document.createElement('h2');
    title.id = 'downtime-title';
    title.textContent = i18n.t('Down for maintenance');
    const body = document.createElement('p');
    body.className = 'lock-who';
    body.textContent = i18n.t('Ninaivu is busy looking after the photographs for a while. They are all safe, and the albums come back here by themselves when it is done.');
    card.append(mark, title, body);
    if (this.since) {
      const note = document.createElement('p');
      note.className = 'lock-note';
      const at = new Date(this.since * 1000);
      note.textContent = i18n.t('Since {time}', {
        time: at.toLocaleString(i18n.language(), { weekday: 'short', hour: 'numeric', minute: '2-digit' }),
      });
      card.append(note);
    }
    this.root.replaceChildren(card);
  }

  async look() {
    clearTimeout(this.timer);
    try {
      const response = await nativeFetch('/api/maintenance', {
        headers: { Accept: 'application/json', 'X-Ninaivu-Background': '1' },
        credentials: 'same-origin',
      });
      const data = response.ok ? await response.json() : null;
      if (data && !data.on) {
        window.location.reload();
        return;
      }
      if (data && data.since && data.since !== this.since) {
        this.since = data.since;
        this.draw();
      }
    } catch { /* the server is restarting: ask again */ }
    this.timer = setTimeout(() => this.look(), LOOK_EVERY);
  }
}
