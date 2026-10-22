/**
 * Full-screen viewer: zoom/pan, swipe, slideshow, filmstrip, EXIF panel and
 * CLIP "find similar". Neighbouring items are prefetched so paging feels
 * instant.
 */

import { api, thumbUrl } from './api.js';
import { blurhashUrl } from './blurhash.js';
import { PhotoEditor } from './editor.js';
import { StoryPanel } from './stories.js';
import { ColourPanel } from './colour.js';
import * as i18n from './i18n.js';
import { LIVE_ICON, liveVideo, mayAutoplay, releaseVideo } from './live.js';
import { calm, contentBox, fitInside, growFrom, shrinkTo } from './motion.js';

const MAX_ZOOM = 8;
/** How long a photograph is held before its Live Photo plays. */
const LIVE_HOLD_MS = 260;

// Only ever given to textContent, which needs no escaping — so the name
// says what it does and no more. It used to be called escapeText, and a
// name like that invites reuse in a template literal, where it would
// escape nothing.
const asText = (value) => String(value ?? '');

function formatBytes(n) {
  if (!n) return '';
  const units = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  let value = n;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i++;
  }
  return `${i === 0 ? value : value.toFixed(1)} ${units[i]}`;
}

/** Plain English for where a rotation came from. */
const ROTATION_WHY = {
  exif: 'from the camera',
  faces: 'worked out from the people in it',
  ai: 'worked out from the picture',
  manual: 'you set this',
};

function formatClock(seconds) {
  if (!seconds) return '';
  const s = Math.round(seconds);
  const m = Math.floor(s / 60);
  return `${m}:${String(s % 60).padStart(2, '0')}`;
}

/**
 * How much to trust the date beside a photograph.
 *
 * Only `exif` is the camera's own answer. Everything else is Ninaivu working
 * it out from the folder it sits in or the timestamps on the file, and a
 * worked-out date that looks identical to a real one is how a photograph from
 * 2014 comes to sit under this month without anybody noticing.
 */
function dateCaveat(source) {
  switch (source) {
    case 'exif': return '';
    case 'container': return '';
    case 'takeout-json': return ' ' + i18n.t('(from Google Takeout)');
    case 'filename': return ' ' + i18n.t('(from the file name)');
    case 'path': return ' ' + i18n.t('(from the folder)');
    case 'created': return ' ' + i18n.t('(file created)');
    case 'mtime': return ' ' + i18n.t('(file modified)');
    default: return source ? ` (${source})` : '';
  }
}


export class Viewer extends EventTarget {
  constructor(root) {
    super();
    this.root = root;
    this.stage = root.querySelector('#viewer-stage');
    this.info = root.querySelector('#viewer-info');
    this.infoList = root.querySelector('#info-list');
    this.infoTags = root.querySelector('#info-tags');
    this.filmstrip = root.querySelector('#filmstrip');
    this.similarStrip = root.querySelector('#similar-strip');
    this.similarRow = root.querySelector('#similar-row');
    this.ids = [];
    this.index = -1;
    this.item = null;
    this.cache = new Map();
    // The neighbours' pictures being fetched ahead (id -> Image), so the ones
    // paged past can be let go of.
    this.warming = new Map();
    // Neighbours whose full picture has arrived, the most recent last: not
    // asked for again while they are here. A few dozen, then the oldest go.
    this.warmed = new Set();
    this.slideshow = null;
    this.slideshowDelay = 4000;
    this.slideshowLoop = true;
    try {
      const settings = JSON.parse(localStorage.getItem('mv.slideshow') || '{}');
      if ([2000,4000,8000,15000].includes(settings.delay)) this.slideshowDelay = settings.delay;
      if (typeof settings.loop === 'boolean') this.slideshowLoop = settings.loop;
    } catch { /* Browser storage is optional. */ }

    this.transform = { scale: 1, x: 0, y: 0, rotate: 0 };
    this.pointers = new Map();
    this.pinchStart = null;
    // Everything one rendering of the stage listens for, let go of together
    // when the stage is drawn again or the viewer closes (see clearStage).
    this.staged = null;

    /** Set by the app: whether this viewer may save a rotation, and how to
     *  report it. Without them the rotate button is a look, not a change. */
    this.canRotate = false;
    this.canDownload = false;
    this.toast = null;
    this.onChange = null;
    /** Set by the app: the grid's tile for an id, for the photograph to
     *  shrink back into when the viewer closes (static/js/motion.js). */
    this.tileFor = null;
    /** The picture that grew out of its tile, until the stage shows its own. */
    this.flyer = null;
    /** Photograph id → the turn last asked for, while a save is in flight. */
    this.turnsWanted = new Map();

    this.wire();
    /** Voice stories: the Stories button and its panel (stories.js). */
    this.stories = new StoryPanel(this);
    /** Colour for black-and-white photographs (colour.js), while
     *  `colour_photos` is on: the app sets colourOn. */
    this.colourOn = false;
    this.colour = new ColourPanel(this);

    // A window that changes shape changes how much a turned photograph has to
    // shrink to keep fitting. Cheap, and only ever does anything while the
    // viewer is open on a quarter-turned picture.
    window.addEventListener('resize', () => {
      if (!this.root.hidden) this.applyTransform();
    });
  }

  /* ---------------------------------------------------------------- */

  wire() {
    const q = (sel) => this.root.querySelector(sel);
    q('#v-close').onclick = () => this.close();
    q('#v-prev').onclick = () => this.step(-1);
    q('#v-next').onclick = () => this.step(1);
    q('#v-zoom-in').onclick = () => this.zoomBy(1.5);
    q('#v-zoom-out').onclick = () => this.zoomBy(1 / 1.5);
    q('#v-rotate').onclick = () => this.rotate();
    q('#v-edit').onclick = () => {
      this.stopSlideshow();
      if (this.isKiosk) this.toggleKiosk();
      new PhotoEditor(this.item, (copy) => {
        this.toast?.(i18n.t('Edited copy saved — original kept'));
        this.onChange?.(copy);
        this.open([copy.id], 0);
      }).open();
    };
    q('#v-info').onclick = () => this.toggleInfo();
    if (q('#v-ai-playground')) q('#v-ai-playground').onclick = async () => {
      if (!this.canDownload || this.item?.kind !== 'picture') return;
      const item = {...this.item, rotation: this.transform.rotate};
      this.stopSlideshow();
      if (this.isKiosk) this.toggleKiosk();
      try {
        const {openPlayground} = await import('./ai-playground/components/playground.js');
        openPlayground({item, returnFocus: q('#v-ai-playground'), canSave: this.canRotate, onSaved: copy => {
          this.toast?.(i18n.t('Edited copy saved — original kept'));
          this.onChange?.(copy);
          this.open([copy.id], 0);
        }});
      } catch { this.toast?.(i18n.t('Sudar could not load. Please refresh and try again.')); }
    };
    // A card, a collage or a calendar of this photograph, straight from the
    // viewer rather than by way of Sudar's Magic tools.
    if (q('#v-create')) q('#v-create').onclick = async () => {
      if (!this.canDownload || this.item?.kind !== 'picture') return;
      const item = this.item;
      this.stopSlideshow();
      if (this.isKiosk) this.toggleKiosk();
      try {
        const {openCreativeStudio} = await import('./creative-studio.js');
        await openCreativeStudio({ids: [item.id], returnFocus: q('#v-create'), sourceId: item.id, canSave: this.canRotate, onSaved: copy => {
          this.toast?.(i18n.t('Edited copy saved — original kept'));
          this.onChange?.(copy);
          this.open([copy.id], 0);
        }});
      } catch { this.toast?.(i18n.t('Creative Studio could not open. Please refresh and try again.')); }
    };
    if (q('#v-info-close')) q('#v-info-close').onclick = () => this.toggleInfo(false);
    q('#v-similar').onclick = () => this.loadSimilar();
    // The button asks how to play before it plays; Space starts at once with
    // what was chosen last. While it is playing the button stops it.
    q('#v-slideshow').onclick = () => (this.slideshow ? this.stopSlideshow() : this.toggleSlidePop());
    const pop = q('#slide-pop');
    if (pop) {
      q('#v-slide-start').onclick = () => this.startSlideshow();
      q('#v-slide-cancel').onclick = () => this.toggleSlidePop(false);
      pop.addEventListener('keydown', (event) => {
        // Nothing typed in the question is a shortcut for the picture behind it.
        event.stopPropagation();
        if (event.key === 'Escape') { event.preventDefault(); this.toggleSlidePop(false, true); }
      });
      document.addEventListener('click', (event) => {
        if (pop.hidden || pop.contains(event.target) || event.target.closest('#v-slideshow')) return;
        this.toggleSlidePop(false);
      });
    }
    const delay = q('#v-slide-delay'), loop = q('#v-slide-loop');
    if (delay && loop) {
      delay.value = String(this.slideshowDelay);loop.checked = this.slideshowLoop;
      const update = () => {
        this.slideshowDelay = Number(delay.value);this.slideshowLoop = loop.checked;
        try { localStorage.setItem('mv.slideshow', JSON.stringify({delay:this.slideshowDelay,loop:this.slideshowLoop})); } catch {}
        if (this.slideshow) this.scheduleSlideshow();
      };
      delay.onchange = loop.onchange = update;
    }
    q('#v-fav').onclick = () => this.toggleFavorite();
    if (q('#v-kiosk')) q('#v-kiosk').onclick = () => this.toggleKiosk();
    if (q('#v-album')) q('#v-album').onclick = () => {
      if (!this.item) return;
      this.dispatchEvent(new CustomEvent('album', { detail: { item: this.item } }));
    };
    if (q('#v-share')) q('#v-share').onclick = () => {
      this.dispatchEvent(new CustomEvent('share', { detail: { item: this.item } }));
    };
    // Where it was taken, on Ninaivu's own map, and the photographs taken near
    // it. Offered only for a photograph with a location (renderChrome), which
    // a guest is never sent.
    if (q('#v-map')) q('#v-map').onclick = () => {
      if (this.item?.gps) this.dispatchEvent(new CustomEvent('map', { detail: { item: this.item } }));
    };
    if (q('#v-nearby')) q('#v-nearby').onclick = () => {
      if (this.item?.gps) this.dispatchEvent(new CustomEvent('nearby', { detail: { item: this.item } }));
    };
    // Confirmation and the password prompt live in the app shell, not here —
    // this just says which item was looking at when the button was pressed.
    if (q('#v-delete')) q('#v-delete').onclick = () => {
      if (!this.item) return;
      this.dispatchEvent(new CustomEvent('delete', { detail: { id: this.item.id } }));
    };

    // The viewer's "More" menu, at every width: everything but the everyday
    // tools, each with its name. The same open / close-on-outside-click /
    // close-on-pick pattern as the topbar's menus, anchored under this
    // toolbar. Opening it from the keyboard puts focus on the first item, and
    // the arrow keys move between items rather than between photographs.
    const moreBtn = q('#v-more');
    const moreMenu = q('#viewer-tools-more');
    const menuItems = () => [...moreMenu.querySelectorAll('button')]
      .filter((button) => !button.hidden && !button.closest('[hidden]'));
    moreBtn.onclick = (event) => {
      event.stopPropagation();
      const open = moreMenu.classList.toggle('open');
      moreBtn.setAttribute('aria-expanded', String(open));
      // The slideshow question opens in the same corner; one at a time.
      if (open) this.toggleSlidePop(false);
      if (open && event.detail === 0) menuItems()[0]?.focus();
    };
    moreMenu.addEventListener('keydown', (event) => {
      if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
      event.preventDefault();
      event.stopPropagation();
      const items = menuItems();
      const at = items.indexOf(document.activeElement);
      const next = at < 0 ? 0 : (at + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
      items[next]?.focus();
    });
    // A button inside the menu is a one-shot action — close the menu once
    // it's picked rather than leaving it sitting open over the photo.
    moreMenu.addEventListener('click', (event) => {
      if (event.target.closest('button')) this.closeMoreMenu();
    });
    document.addEventListener('click', (event) => {
      if (!moreMenu.classList.contains('open')) return;
      if (moreMenu.contains(event.target) || event.target === moreBtn) return;
      this.closeMoreMenu();
    });

    this.stage.addEventListener('wheel', (event) => this.onWheel(event), { passive: false });
    this.stage.addEventListener('pointerdown', (event) => this.onPointerDown(event));
    this.stage.addEventListener('pointermove', (event) => this.onPointerMove(event));
    this.stage.addEventListener('pointerup', (event) => this.onPointerUp(event));
    this.stage.addEventListener('pointercancel', (event) => this.onPointerUp(event));
    this.root.addEventListener('click', (event) => {
      if (!this.isKiosk || event.target.closest('#v-kiosk')) return;
      event.preventDefault();
      event.stopPropagation();
      this.toggleKiosk();
    }, true);
    this.stage.addEventListener('dblclick', (event) => this.onDoubleClick(event));

    this.filmstrip.addEventListener('click', (event) => {
      const img = event.target.closest('img');
      if (img) this.goTo(Number(img.dataset.index));
    });

    this.similarRow.addEventListener('click', (event) => {
      const img = event.target.closest('img');
      if (!img) return;
      this.dispatchEvent(new CustomEvent('jump', { detail: { id: Number(img.dataset.id) } }));
    });
  }

  toggleKiosk() {
    this.stopSlideshow();
    this.isKiosk = !this.isKiosk;
    const hud = this.root.querySelector('#kiosk-hud');
    const topBar = this.root.querySelector('.viewer-top');
    const filmstrip = this.root.querySelector('#filmstrip');
    const navPrev = this.root.querySelector('#v-prev');
    const navNext = this.root.querySelector('#v-next');

    if (this.isKiosk) {
      if (hud) hud.hidden = false;
      if (topBar) topBar.style.opacity = '0';
      if (filmstrip) filmstrip.style.opacity = '0';
      if (navPrev) navPrev.style.opacity = '0';
      if (navNext) navNext.style.opacity = '0';
      if (this.media) this.media.classList.add('ken-burns');

      this.updateKioskClock();
      this.kioskClockTimer = setInterval(() => this.updateKioskClock(), 1000);
      this.kioskTimer = setInterval(() => this.goTo((this.index + 1) % this.ids.length), 8000);
      try { document.documentElement.requestFullscreen?.()?.catch(() => {}); } catch {}
      this.toast?.(i18n.t('Ambient Frame mode active — click anywhere or Esc to exit'));
    } else {
      if (hud) hud.hidden = true;
      if (topBar) topBar.style.opacity = '';
      if (filmstrip) filmstrip.style.opacity = '';
      if (navPrev) navPrev.style.opacity = '';
      if (navNext) navNext.style.opacity = '';
      if (this.media) this.media.classList.remove('ken-burns');

      clearInterval(this.kioskClockTimer);
      clearInterval(this.kioskTimer);
      this.kioskClockTimer = null;
      this.kioskTimer = null;
      try { if (document.fullscreenElement) document.exitFullscreen?.()?.catch(() => {}); } catch {}
    }
  }

  updateKioskClock() {
    const clock = this.root.querySelector('#kiosk-clock');
    const date = this.root.querySelector('#kiosk-date');
    if (!clock || !date) return;
    const now = new Date();
    clock.textContent = now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    date.textContent = now.toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric' });
  }

  /** `at` opens the video at *index* that many seconds in (a moment a
   *  search found inside it, static/js/video-moments.js); with `words` its
   *  spoken words show under it from the start (static/js/video-words.js). */
  async open(ids, index, thumbed = null, { at = null, words = false, from = null } = {}) {
    this.ids = ids;
    this.thumbed = thumbed;
    this.startAt = at != null && ids[index] != null
      ? { id: ids[index], at: Number(at), words: Boolean(words) } : null;
    // Opened from a tile in the grid: the photograph grows out of it
    // (static/js/motion.js). The stage waits, unseen, under the picture in
    // flight, and the backdrop darkens as it comes.
    this.dropFlyer();
    this.root.classList?.remove('grown');
    const growing = Boolean(from) && !calm();
    this.root.classList?.toggle('growing', growing);
    this.root.hidden = false;
    document.body.style.overflow = 'hidden';
    const flight = growing ? growFrom({ tile: from, stage: this.stage, viewer: this.root }) : null;
    await this.goTo(index);
    this.root.focus?.();
    if (flight) this.land(await flight);
  }

  /** The picture in flight has arrived: the stage comes out from under it,
   *  and it goes once the stage's own picture is on screen. */
  land(flyer) {
    if (!this.isOpen || !this.root.classList.contains('growing')) { flyer?.remove(); return; }
    // `grown` keeps the viewer's own fade-in from playing now, after the fact.
    this.root.classList.toggle('grown', Boolean(flyer));
    this.root.classList.remove('growing');
    if (!flyer) return;
    this.flyer = flyer;
    const shown = this.stage.querySelector('img.placeholder, img, video');
    const drawn = !shown || shown.tagName === 'VIDEO' || (shown.complete && shown.naturalWidth > 0);
    if (drawn) { requestAnimationFrame(() => this.dropFlyer()); return; }
    const go = () => this.dropFlyer();
    shown.addEventListener('load', go, { once: true });
    shown.addEventListener('error', go, { once: true });
    // A picture that is slow to come is not waited for long.
    setTimeout(go, 600);
  }

  dropFlyer() {
    this.flyer?.remove();
    this.flyer = null;
  }

  /** Where the photograph on screen is drawn, and a picture of it the browser
   *  already has, so that it can shrink back into its tile after the viewer
   *  has gone. Nothing for a zoomed or panned photograph, the ambient frame,
   *  a sound, or anyone who has asked for less motion. */
  flightBack() {
    const item = this.item;
    if (calm() || this.isKiosk || !item || item.kind === 'audio' || !item.has_thumb) return null;
    const { scale = 1, x = 0, y = 0, rotate = 0 } = this.transform || {};
    if (scale > 1.02 || x || y) return null;
    const media = this.media;
    if (!media?.isConnected) return null;
    let w = media.naturalWidth || media.videoWidth || item.width;
    let h = media.naturalHeight || media.videoHeight || item.height;
    if (Math.abs(rotate % 180) === 90) [w, h] = [h, w];
    const from = fitInside(contentBox(this.stage), w, h);
    return from ? { id: item.id, src: thumbUrl(item.id, 640, item.thumb_v), from } : null;
  }

  close() {
    const back = this.flightBack();
    this.dropFlyer();
    this.root.classList?.remove('growing', 'grown');
    this.previousIndex = null;
    if (this.isKiosk) this.toggleKiosk();
    this.stopSlideshow();
    this.toggleSlidePop(false);
    this.closeMoreMenu();
    this.stories?.close();
    this.colour?.close();
    this.root.hidden = true;
    this.clearStage();
    this.coolDown();
    document.body.style.overflow = '';
    this.dispatchEvent(new CustomEvent('close', { detail: { id: this.item?.id } }));
    // After the close event, which scrolls the grid to the photograph, so its
    // tile is where it will be drawn.
    if (back) shrinkTo({ ...back, tile: this.tileFor?.(back.id) });
  }

  /** Close the viewer's "More" menu, if it's open. Cheap and safe to call
   * unconditionally — leaving it open across a close or a swipe to the next
   * photo is the kind of thing that only shows up on someone's phone.
   * `returnFocus` is for Escape: focus goes back to the button that opened
   * it rather than being lost with the menu. */
  closeMoreMenu(returnFocus = false) {
    const menu = this.root.querySelector('#viewer-tools-more');
    const btn = this.root.querySelector('#v-more');
    const hadFocus = menu.contains(document.activeElement);
    menu.classList.remove('open');
    btn.setAttribute('aria-expanded', 'false');
    if (returnFocus && hadFocus) btn.focus();
  }

  get isOpen() {
    return !this.root.hidden;
  }

  step(delta) {
    const next = this.index + delta;
    if (next < 0 || next >= this.ids.length) return;
    this.goTo(next);
  }

  async fetchItem(id) {
    if (this.cache.has(id)) return this.cache.get(id);
    const item = await api.asset(id);
    if (this.cache.size > 300) this.cache.clear();
    this.cache.set(id, item);
    return item;
  }

  async goTo(index) {
    if (index < 0 || index >= this.ids.length) return;
    this.index = index;
    const id = this.ids[index];

    let item;
    try {
      item = await this.fetchItem(id);
    } catch {
      return;
    }
    if (!this.isOpen || this.ids[this.index] !== id) return; // user moved on while loading
    this.item = item;

    // Stage first: resetTransform needs the media element to exist, and
    // applying the photograph's own rotation to nothing was why a corrected
    // photograph still opened sideways in the viewer while looking right in
    // the grid.
    this.renderStage(item);
    this.resetTransform();
    this.renderChrome(item);
    // The title is in now, and its height decides where the Live button goes.
    this.placeLiveBadge();
    this.stories?.show(item);
    this.colour?.show(item);
    if (this.slideshow) this.scheduleSlideshow();
    this.renderFilmstrip();
    if (!this.info.hidden) this.renderInfo(item);
    this.similarStrip.hidden = true;

    if (this.isKiosk) {
      const kTitle = this.root.querySelector('#kiosk-title');
      const kSub = this.root.querySelector('#kiosk-sub');
      if (kTitle) kTitle.textContent = item.caption || item.name || '';
      if (kSub) kSub.textContent = [item.date, item.city, item.country].filter(Boolean).join(' · ');
      if (this.media) this.media.classList.add('ken-burns');
    }

    // Warm the neighbours so paging is instant — more of them the way the
    // person is going, since that is where the next swipe lands. Stepping
    // backwards used to find only one photograph ready.
    let moved = this.previousIndex == null ? 0 : index - this.previousIndex;
    // The slideshow wrapping from the last photograph to the first is forwards.
    if (Math.abs(moved) > this.ids.length / 2) moved -= Math.sign(moved) * this.ids.length;
    const going = Math.sign(moved);
    this.previousIndex = index;
    const ahead = going < 0 ? [-1, -2, -3, 1] : going > 0 ? [1, 2, 3, -1] : [1, -1, 2];
    // A neighbour paged past is let go of: holding the arrow key through
    // forty photographs left forty originals downloading, ahead of the one on
    // screen, on the few connections a browser allows.
    const near = new Set([id, ...ahead.map((offset) => this.ids[index + offset])]);
    this.coolDown(near);
    for (const offset of ahead) {
      const neighbour = this.ids[index + offset];
      // Already looked up is not the same as its picture being ready: one
      // paged past was let go of (coolDown), and coming back to it found
      // nothing warming. So the picture is asked for whenever it is not on
      // its way or here already, whether or not its details are.
      if (neighbour != null && !this.warming.has(neighbour) && !this.warmed.has(neighbour)) {
        this.fetchItem(neighbour).then((data) => {
          if (data?.kind !== 'picture' || !this.isOpen || this.warming.has(neighbour)) return;
          if (this.ids[this.index] !== id) return;      // paged on meanwhile
          const image = new Image();
          const finished = () => {
            if (this.warming.get(neighbour) !== image) return;
            this.warming.delete(neighbour);
            if (image.complete && image.naturalWidth) this.remember(neighbour);
          };
          image.onload = finished;
          image.onerror = finished;
          this.warming.set(neighbour, image);
          image.src = data.view || data.src;
        }).catch(() => {});
      }
    }
    this.dispatchEvent(new CustomEvent('change', { detail: { id, index } }));
  }

  /* -- rendering ------------------------------------------------------ */

  /**
   * Empty the stage, and let go of what the last photograph left listening.
   *
   * A live photo's listeners are on the stage itself, which outlives every
   * photograph. They used to be dropped by the next pointer event to reach
   * the stage — and in a slideshow or the ambient frame, or paging with the
   * arrow keys, there is none, so each live photo shown kept its clip, its
   * picture and three listeners for as long as the viewer stayed open.
   */
  clearStage() {
    this.staged?.abort();
    this.staged = null;
    for (const clip of this.stage.querySelectorAll('.live-clip')) releaseVideo(clip);
    // A photograph paged past before it arrived: its download stops too.
    for (const img of this.stage.querySelectorAll('img')) {
      if (!img.complete) img.removeAttribute('src');
    }
    this.stage.innerHTML = '';
  }

  /**
   * Someone else is using the page now (a session ended and the picker came
   * up without a reload): nothing kept as the last person was shown it — the
   * camera, the place, the download link — is shown to the next.
   */
  forget() {
    if (this.isOpen) this.close();
    this.cache.clear();
    this.coolDown();
  }

  /** Stop fetching ahead the pictures of anything not in `keep`. */
  remember(id) {
    this.warmed.delete(id);
    this.warmed.add(id);
    while (this.warmed.size > 40) this.warmed.delete(this.warmed.values().next().value);
  }

  coolDown(keep = new Set()) {
    for (const [id, image] of this.warming || []) {
      if (keep.has(id)) continue;
      this.warming.delete(id);
      image.removeAttribute('src');
    }
  }

  renderStage(item) {
    this.clearStage();
    this.staged = new AbortController();

    if (item.kind === 'audio') {
      const hero = document.createElement('div');
      hero.className = 'audio-hero';
      const disc = document.createElement('div');
      disc.className = 'disc';
      // Its picture as the record's label: the cover, or the tile the grid
      // shows. Over the plain disc, so a picture that will not load leaves
      // that rather than a hole.
      if (item.has_thumb) {
        disc.classList.add('cover');
        disc.style.backgroundImage = `url("${thumbUrl(item.id, 256, item.thumb_v)}"), `
          + 'conic-gradient(from 0deg, var(--accent), var(--accent-2), var(--accent))';
      }
      const title = document.createElement('div');
      title.className = 'title';
      title.textContent = item.caption || item.name;
      const audio = document.createElement('audio');
      audio.src = item.src;
      audio.controls = true;
      audio.autoplay = true;
      audio.onplay = () => hero.classList.add('playing');
      audio.onpause = () => hero.classList.remove('playing');
      audio.onended = () => this.slideshow && this.advanceSlideshow();
      hero.append(disc, title, audio);
      this.stage.appendChild(hero);
      return;
    }

    if (item.kind === 'video') {
      if (!item.playable) {
        // The browser cannot open this container, but Ninaivu can convert a
        // copy. Ask for one and play that; the original is never touched.
        if (item.needs_proxy) {
          this.playViaProxy(item);
          return;
        }
        this.renderUnsupported(item);
        return;
      }
      const video = document.createElement('video');
      video.src = item.src;
      video.controls = true;
      video.autoplay = true;
      video.playsInline = true;
      video.preload = 'metadata';
      this.addSpokenWords(video, item);
      this.startVideoAt(video, item);
      video.onended = () => this.slideshow && this.advanceSlideshow();
      this.media = video;
      this.stage.appendChild(video);
      return;
    }

    // The gallery's own picture of it stands in until the full one arrives,
    // as a phone's photo app opens a photograph: sharp at once, then sharper.
    // It is usually in the browser already, from the grid. Only a photograph
    // with no thumbnail falls back to the blurred colour sketch. For a HEIC
    // or a raw, converted on first view, this is what covers the wait.
    const sharp = item.has_thumb !== false && item.thumb_v !== undefined;
    const placeholder = sharp ? thumbUrl(item.id, 640, item.thumb_v) : blurhashUrl(item.blurhash);
    if (placeholder) {
      const ghost = document.createElement('img');
      ghost.className = sharp ? 'placeholder sharp' : 'placeholder';
      ghost.src = placeholder;
      ghost.alt = '';
      if (!sharp) {
        // The colour sketch is drawn at the photograph's own shape; the
        // thumbnail already has it, and sits where the photograph will.
        ghost.style.width = `${Math.min(item.width || 800, window.innerWidth * 0.92)}px`;
        ghost.style.aspectRatio = `${item.width || 4} / ${item.height || 3}`;
      }
      this.stage.appendChild(ghost);
    }

    const img = document.createElement('img');
    img.alt = item.name;
    img.decoding = 'async';
    // `view` is the file itself for a JPEG and a converted copy for HEIC or
    // raw. Falling back to `src` keeps this working against an older server.
    img.src = item.view || item.src;
    img.onload = () => {
      this.stage.querySelector('.placeholder')?.remove();
      // The fit-after-turn factor is worked out from the picture's own
      // dimensions, and until it has loaded there are none — so a photograph
      // that opens already turned has to be measured again here, or it is
      // laid out at scale 1 and runs off the top and bottom of the screen.
      this.applyTransform();
    };
    img.onerror = () => {
      // The original could not be served — the file is gone from disk or
      // unreadable. Left alone, the blurhash placeholder would stay up
      // forever and read as an abstract picture that was never in the
      // library. Drop it and fall back to the generated preview instead.
      this.stage.querySelector('.placeholder')?.remove();
      img.remove();
      if (item.has_thumb) {
        const fallback = document.createElement('img');
        fallback.src = thumbUrl(item.id, 640, item.thumb_v);
        fallback.alt = item.name;
        this.media = fallback;
        this.stage.appendChild(fallback);
      }
      const note = document.createElement('div');
      note.className = 'badge';
      note.style.cssText = 'position:absolute;bottom:80px;left:50%;transform:translateX(-50%)';
      note.textContent = i18n.t('Original file is not available — showing the preview');
      this.stage.appendChild(note);
    };
    this.media = img;
    this.stage.appendChild(img);

    if (item.is_live && item.live_src) this.renderLive(item, img);
  }

  /**
   * A live photo: the photograph, and the moment around it.
   *
   * It plays once by itself when opened (not for anyone who asked their
   * system for less motion), plays again with its sound while the photograph
   * is held down, a finger or a mouse button, and from the Live button, and
   * goes back to the photograph when it ends or is let go. The clip is laid
   * exactly over the picture rather than over the whole screen, and fades in
   * and out, so it reads as the photograph moving.
   */
  renderLive(item, img) {
    const id = item.id;
    const { signal } = this.staged;
    const wanted = () => this.isOpen && this.item?.id === id;
    const clip = liveVideo({ src: item.live_src, convert: item.live_convert }, {
      wanted, className: 'live-clip',
    });
    const badge = document.createElement('button');
    badge.type = 'button';
    badge.className = 'live-badge';
    badge.title = i18n.t('Play the Live Photo (hold the photo to play it with sound)');
    badge.innerHTML = `${LIVE_ICON}<span>LIVE</span>`;
    let holding = false;

    // Over the picture as it is drawn: the same cell of the stage, and the
    // same zoom and turn. applyTransform keeps it there as the zoom changes.
    const place = () => {
      clip.style.transform = img.style.transform;
      clip.style.transition = img.style.transition;
    };
    const play = (withSound) => {
      // Nothing to play once neither the clip nor its copy would open.
      if (!wanted() || !img.isConnected || badge.disabled) return;
      place();
      clip.dataset.playing = '1';
      clip.currentTime = 0;
      clip.muted = !withSound;
      badge.classList.add('on');
      const started = clip.play();
      // A browser that will not start sound without a tap of its own still
      // plays the picture moving. Only that refusal: a play cut short by
      // letting go (pause() rejects it too) stays stopped, rather than
      // starting again, muted, after the hand is off the photograph.
      started?.catch((error) => {
        if (clip.muted || error?.name !== 'NotAllowedError' || clip.dataset.playing !== '1') return;
        clip.muted = true;
        clip.play().catch(() => {});
      });
    };
    const settle = () => {
      clip.dataset.playing = '';
      clip.classList.remove('playing');
      badge.classList.remove('on');
      clip.pause();
    };
    clip.addEventListener('playing', () => clip.classList.add('playing'));
    clip.addEventListener('ended', () => { if (!holding) settle(); });
    clip.addEventListener('live-unavailable', () => { settle(); badge.disabled = true; });

    // Held, not tapped: a tap is the toolbar's and a drag is a swipe or a pan.
    let timer = null;
    let origin = null;
    // What last pressed the picture: a mouse's right button keeps the
    // browser's own menu (Save image as…); a finger's long press does not,
    // because there the press is the Live Photo.
    let pressedBy = 'mouse';
    const release = () => {
      clearTimeout(timer);
      timer = null;
      if (holding) {
        holding = false;
        settle();
        // The click that ends a hold is not a click on the photograph.
        const swallow = (event) => { event.stopPropagation(); event.preventDefault(); };
        this.stage.addEventListener('click', swallow, { capture: true, once: true });
        setTimeout(() => this.stage.removeEventListener('click', swallow, { capture: true }), 400);
      }
    };
    img.addEventListener('pointerdown', (event) => {
      pressedBy = event.pointerType || 'mouse';
      // A second finger is a pinch starting, not a longer hold; the stage
      // hears it below and lets go of the first.
      if (event.button > 0 || this.transform.scale > 1.02 || this.pointers.size > 0) return;
      origin = { x: event.clientX, y: event.clientY };
      clearTimeout(timer);
      timer = setTimeout(() => {
        timer = null;
        holding = true;
        play(true);
      }, LIVE_HOLD_MS);
    }, { signal });
    // The rest of a press is heard on the stage, which captures the pointer
    // for swipes and pans: the picture itself is told the pointer has left
    // the moment it is pressed. Until the stage is drawn again (clearStage).
    const listen = (type, handler) => this.stage.addEventListener(type, handler, { signal });
    // The stage's own pointerdown has already counted this pointer: two down
    // is a pinch, and a pinch is not a hold, even with both fingers resting.
    listen('pointerdown', () => { if (this.pointers.size > 1) release(); });
    listen('pointermove', (event) => {
      if (!timer || !origin) return;
      if (Math.hypot(event.clientX - origin.x, event.clientY - origin.y) > 10) release();
    });
    listen('pointerup', release);
    listen('pointercancel', release);
    // Held on a phone, the browser offers to save the picture instead.
    img.addEventListener('contextmenu', (event) => {
      if (holding || timer || pressedBy !== 'mouse') event.preventDefault();
    }, { signal });
    img.classList.add('live-still');

    // The button: a resting mouse plays it quietly, as the grid does, and a
    // click or a tap plays it with its sound — or, with the sound already
    // on, stops it.
    badge.addEventListener('click', (event) => {
      event.stopPropagation();
      if (badge.classList.contains('on') && !clip.muted) settle();
      else play(true);
    });
    badge.addEventListener('pointerenter', (event) => {
      if (event.pointerType === 'mouse' && !badge.classList.contains('on')) play(false);
    });

    this.stage.append(clip, badge);
    this.placeLiveBadge();
    window.addEventListener('resize', () => this.placeLiveBadge(), { signal });
    if (mayAutoplay()) {
      const once = () => setTimeout(() => {
        if (!holding && !badge.classList.contains('on')) play(false);
      }, 250);
      if (img.complete && img.naturalWidth) once();
      else img.addEventListener('load', once, { once: true, signal });
    }
  }

  /**
   * The Live button, just below the title bar. A fixed offset sat under the
   * bar on a phone, where the name and its details wrap onto more lines, and
   * a tap on the button went to the title instead.
   */
  placeLiveBadge() {
    const badge = this.stage.querySelector('.live-badge');
    const bar = this.root.querySelector('.viewer-top');
    if (!badge || !bar || bar.hidden) return;
    badge.style.top = '';
    const below = bar.getBoundingClientRect().bottom - this.stage.getBoundingClientRect().top;
    if (below + 8 > badge.offsetTop) badge.style.top = `${Math.ceil(below + 8)}px`;
  }


  /* Play a video the browser cannot open, through a converted copy.
   *
   * /api/proxy/<id> answers 202 with a percentage while it is working and the
   * file itself once it is done, so this polls one address rather than
   * juggling a build endpoint and a fetch endpoint.
   */
  async playViaProxy(item) {
    const url = `/api/proxy/${item.id}`;
    const panel = document.createElement('div');
    panel.className = 'unsupported';
    const heading = document.createElement('h3');
    heading.textContent = i18n.t('Preparing this video');
    const detail = document.createElement('p');
    detail.textContent =
      `${(item.ext || '').toUpperCase()} is not a format browsers can play, `
      + i18n.t('so Ninaivu is making a copy it can. This happens once.');
    const bar = document.createElement('div');
    bar.className = 'proxy-bar';
    const fill = document.createElement('i');
    bar.appendChild(fill);
    panel.append(heading, detail, bar);
    this.stage.appendChild(panel);

    const stop = () => !this.isOpen || this.item?.id !== item.id || !panel.isConnected;
    this.currentProxyId = item.id;

    for (let attempt = 0; attempt < 900; attempt += 1) {
      if (stop()) return;                     // the viewer moved on
      let response;
      try {
        response = await fetch(url, { headers: { Accept: 'application/json' } });
      } catch {
        heading.textContent = i18n.t('Could not reach the server');
        bar.remove();
        return;
      }
      if (stop()) return;
      if (response.ok) {
        if (stop()) return;
        panel.remove();
        const video = document.createElement('video');
        video.src = url;
        video.controls = true;
        video.autoplay = true;
        video.playsInline = true;
        this.addSpokenWords(video, item);
        this.startVideoAt(video, item);
        video.onended = () => this.slideshow && this.advanceSlideshow();
        this.media = video;
        this.stage.appendChild(video);
        return;
      }
      let payload = {};
      try { payload = await response.json(); } catch { /* not JSON */ }
      if (response.status !== 202) {
        heading.textContent = payload.state === 'unavailable'
          ? i18n.t('This video cannot be converted here')
          : i18n.t('Could not prepare this video');
        detail.textContent = payload.message
          || 'ffmpeg is needed to convert video for the browser.';
        bar.remove();
        return;
      }
      fill.style.width = `${Math.max(2, payload.percent || 0)}%`;
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }
    bar.remove();
    heading.textContent = i18n.t('Video preparation timed out');
  }

  /**
   * What is said in *video*, as subtitles the player's own captions button
   * offers (`video_words`, ninaivu/api/api_video_words.py): the words as
   * they were said, and in English when they were not said in English.
   * Shown at once only when a search for the words opened the video.
   */
  async addSpokenWords(video, item) {
    if (!this.spokenWords?.()) return;
    const showing = this.startAt?.id === item.id && this.startAt.words;
    let data;
    try {
      const response = await fetch(`/api/video-words/${item.id}`, {
        headers: { Accept: 'application/json' },
      });
      if (!response.ok) return;
      data = await response.json();
    } catch {
      return;
    }
    if (!data.lines?.length || !video.isConnected || this.item?.id !== item.id) return;
    const spoken = (data.languages || []).find((code) => code !== 'en') || 'en';
    const tracks = [{ lang: spoken, which: 'spoken' }];
    if (spoken !== 'en' && data.english) tracks.push({ lang: 'en', which: 'english' });
    const names = { ta: 'தமிழ்', en: 'English' };
    tracks.forEach(({ lang, which }, index) => {
      const track = document.createElement('track');
      track.kind = 'subtitles';
      track.srclang = lang;
      track.label = names[lang] || lang;
      track.src = `/api/video-words/${item.id}.vtt?lang=${which}`;
      // The words in the page's own language first, when there are both.
      const wanted = tracks.length > 1 ? (i18n.language() === 'en' ? 'en' : spoken) : lang;
      if (showing && lang === wanted) track.default = true;
      track.dataset.index = String(index);
      video.appendChild(track);
    });
  }

  /** Start *video* where a search found the moment in it, once. */
  startVideoAt(video, item) {
    const start = this.startAt;
    if (!start || start.id !== item.id || !(start.at > 0)) return;
    this.startAt = null;
    const seek = () => {
      try { video.currentTime = start.at; } catch { /* not seekable yet */ }
    };
    if (video.readyState >= 1) seek();
    else video.addEventListener('loadedmetadata', seek, { once: true });
  }

  renderUnsupported(item) {
    const box = document.createElement('div');
    box.className = 'audio-hero';
    const title = document.createElement('div');
    title.className = 'title';
    title.textContent = `${item.name} cannot play in the browser`;
    const link = document.createElement('a');
    link.className = 'btn primary';
    link.href = item.download;
    link.textContent = i18n.t('Download to play');
    box.append(title, link);
    this.stage.appendChild(box);
  }

  renderChrome(item) {
    // Not gated on `playable` any more: the studio opens the converted copy,
    // so a phone's HEIC is as editable as anything else in the library.
    this.root.querySelector('#v-edit').hidden = !this.canRotate || item.kind !== 'picture';
    const playground = this.root.querySelector('#v-ai-playground');
    if (playground) playground.hidden = !this.canDownload || item.kind !== 'picture';
    const create = this.root.querySelector('#v-create');
    if (create) create.hidden = !this.canDownload || item.kind !== 'picture';
    this.root.querySelector('#v-name').textContent = item.name;
    const bits = [
      item.date,
      item.width ? `${item.width}×${item.height}` : '',
      item.size_h,
      item.duration ? formatClock(item.duration) : '',
      typeof item.score === 'number' ? `${Math.round(item.score * 100)}% match` : '',
    ].filter(Boolean);
    this.root.querySelector('#v-sub').textContent = bits.join('  ·  ');
    this.root.querySelector('#v-download').href = item.download;
    // The RAW shot with this JPEG, when there is one: it is not a photograph
    // of its own in the gallery, so this is where it is taken from.
    const raw = this.root.querySelector('#v-download-raw');
    if (raw) {
      raw.hidden = !item.raw;
      if (item.raw) {
        raw.href = item.raw.download;
        raw.querySelector('.viewer-tools-more-label').textContent =
          `${i18n.t('Download RAW')} (${item.raw.ext})`;
      } else {
        raw.removeAttribute('href');
      }
    }
    this.root.querySelector('#v-fav').classList.toggle('on', item.favorite);
    for (const id of ['#v-map', '#v-nearby']) {
      const button = this.root.querySelector(id);
      if (button) button.hidden = !item.gps;
    }
  }

  renderFilmstrip() {
    const window_ = 24;
    const from = Math.max(0, this.index - window_);
    const to = Math.min(this.ids.length, this.index + window_);
    this.filmstrip.innerHTML = '';
    for (let i = from; i < to; i++) {
      const img = document.createElement('img');
      img.loading = 'lazy';
      img.decoding = 'async';
      img.alt = '';
      img.dataset.index = String(i);
      if (i === this.index) img.className = 'current';
      // Audio (and anything else without a derivative) has no thumbnail, so
      // don't request one; the error handler covers stale index entries.
      if (this.thumbed && !this.thumbed.has(this.ids[i])) {
        img.classList.add('no-thumb');
      } else {
        img.addEventListener('error', () => {
          img.removeAttribute('src');
          img.classList.add('no-thumb');
        }, { once: true });
        img.src = thumbUrl(this.ids[i], 256, this.cache.get(this.ids[i])?.thumb_v);
      }
      this.filmstrip.appendChild(img);
    }
    this.centerFilmstrip(true);
    // Thumbnails arrive at different widths, so what was centred drifts as
    // the ones before it load: centre again as each settles.
    let queued = false;
    const again = () => {
      if (queued) return;
      queued = true;
      requestAnimationFrame(() => { queued = false; this.centerFilmstrip(false); });
    };
    for (const img of this.filmstrip.querySelectorAll('img')) {
      if (!img.complete) img.addEventListener('load', again, { once: true });
    }
  }

  /** Put the current frame in the middle of the strip, exactly. The strip has
   *  half its width of padding at each end (see .filmstrip), so even the first
   *  and last frame can sit in the middle. */
  centerFilmstrip(smooth) {
    const strip = this.filmstrip;
    const current = strip.querySelector('.current');
    if (!current) return;
    const left = current.offsetLeft + current.offsetWidth / 2 - strip.clientWidth / 2;
    strip.scrollTo({ left: Math.max(0, left), behavior: smooth ? 'smooth' : 'auto' });
  }

  toggleInfo(force) {
    this.info.hidden = force === undefined ? !this.info.hidden : !force;
    // One side panel at a time: the details or the stories.
    if (!this.info.hidden && this.stories?.isOpen) this.stories.toggle(false);
    this.root.classList.toggle('info-open', !this.info.hidden || !!this.stories?.isOpen);
    this.root.querySelector('#v-info').classList.toggle('on', !this.info.hidden);
    if (!this.info.hidden && this.item) this.renderInfo(this.item);
  }

  renderInfo(item) {
    const rows = [
      [i18n.t('Name'), item.name],
      [i18n.t('Folder'), item.folder || '—'],
      [i18n.t('Taken'), item.date + dateCaveat(item.date_source)],
      [i18n.t('Type'), `${item.kind} · ${item.ext.toUpperCase()}`],
      [i18n.t('Size'), item.size_h],
      item.raw ? ['RAW', `${item.raw.ext} · ${item.raw.size_h} · ${i18n.t('kept with this photo')}`] : null,
      item.live_src ? [i18n.t('Live Photo'), i18n.t('Hold the photo to play it')] : null,
      item.width ? [i18n.t('Dimensions'), `${item.width} × ${item.height}`] : null,
      item.duration ? [i18n.t('Duration'), formatClock(item.duration)] : null,
      item.camera ? [i18n.t('Camera'), item.camera] : null,
      item.lens ? [i18n.t('Lens'), item.lens] : null,
      item.f_number ? [i18n.t('Aperture'), `ƒ/${item.f_number}`] : null,
      item.exposure ? [i18n.t('Shutter'), item.exposure] : null,
      item.iso ? ['ISO', item.iso] : null,
      item.focal_length ? [i18n.t('Focal length'), `${item.focal_length} mm`] : null,
      item.gps ? ['Location', `${item.gps[0].toFixed(4)}, ${item.gps[1].toFixed(4)}`] : null,
      item.duplicate ? [i18n.t('Duplicate'), i18n.t('Has near-identical copies')] : null,
      // Only when Ninaivu turned it, and only saying *who* decided. A
      // photograph the camera tagged correctly needs no explanation; one that
      // came out sideways needs somebody to be able to tell a bad guess from a
      // bad tag, which is the difference between correcting the photograph and
      // correcting Ninaivu.
      item.rotation ? [i18n.t('Turned'), `${item.rotation}° · ${ROTATION_WHY[item.rotation_source]
        || item.rotation_source}`] : null,
    ].filter(Boolean);

    this.infoList.innerHTML = '';
    for (const [label, value] of rows) {
      const dt = document.createElement('dt');
      dt.textContent = label;
      const dd = document.createElement('dd');
      // Linking the location out to openstreetmap.org puts the coordinates
      // of a photograph in somebody else's request log, and a mis-click is
      // enough to do it. So the link exists only where the household has
      // already accepted that trade by turning tiles on; otherwise the
      // location is what it says it is, and stays here.
      if (label === 'Location' && document.body.dataset.mapTiles === '1') {
        const link = document.createElement('a');
        link.href = `https://www.openstreetmap.org/?mlat=${item.gps[0]}&mlon=${item.gps[1]}#map=15/${item.gps[0]}/${item.gps[1]}`;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        link.textContent = asText(value);
        dd.appendChild(link);
      } else {
        dd.textContent = asText(value);
      }
      this.infoList.append(dt, dd);
    }

    this.infoTags.innerHTML = '';
    for (const tag of item.tags || []) {
      const chip = document.createElement('button');
      chip.className = 'chip';
      chip.type = 'button';
      chip.textContent = tag;
      chip.onclick = () => this.dispatchEvent(
        new CustomEvent('tag', { detail: { tag } }),
      );
      this.infoTags.appendChild(chip);
    }
  }

  async loadSimilar() {
    if (!this.item) return;
    this.similarStrip.hidden = false;
    this.toggleInfo(true);
    this.similarRow.innerHTML = '<span style="color:var(--text-3);font-size:12px">Searching…</span>';
    try {
      const { items, reason } = await api.similar(this.item.id);
      this.similarRow.innerHTML = '';
      if (!items?.length) {
        const note = document.createElement('span');
        note.style.cssText = 'color:var(--text-3);font-size:12px';
        note.textContent = reason === 'no-embedding'
          ? i18n.t('This item has not been analysed yet.')
          : i18n.t('No visually similar items found.');
        this.similarRow.appendChild(note);
        return;
      }
      for (const item of items.slice(0, 12)) {
        const img = document.createElement('img');
        img.src = thumbUrl(item.id, 256, item.thumb_v);
        img.alt = item.name;
        img.title = `${item.name} · ${Math.round((item.score || 0) * 100)}%`;
        img.dataset.id = String(item.id);
        img.loading = 'lazy';
        this.similarRow.appendChild(img);
      }
    } catch {
      this.similarRow.textContent = i18n.t('Similarity search is unavailable.');
    }
  }

  /* -- mutations ------------------------------------------------------- */

  async toggleFavorite() {
    if (!this.item || this.favoritePending) return;
    const item = this.item;
    const previous = item.favorite;
    const next = !previous;
    this.favoritePending = true;
    item.favorite = next;
    this.root.querySelector('#v-fav').classList.toggle('on', next);
    try {
      await api.update(item.id, { favorite: next });
      this.dispatchEvent(new CustomEvent('mutated', {
        detail: { id: item.id, favorite: next },
      }));
    } catch (error) {
      item.favorite = previous;
      if (this.item === item) this.root.querySelector('#v-fav').classList.toggle('on', !!previous);
      this.toast?.(`Could not update favorite: ${error.message}`, true);
    } finally {
      this.favoritePending = false;
    }
  }

  /* -- transform ------------------------------------------------------- */

  resetTransform() {
    // The turn starts where the photograph says it should, not at zero.
    // Thumbnails are baked upright by the scanner, but /api/file serves the
    // untouched original — so a photograph Ninaivu worked out the orientation
    // of would open sideways here while looking fine in the grid.
    this.transform = {
      scale: 1, x: 0, y: 0, rotate: Number(this.item?.rotation || 0) % 360,
    };
    this.applyTransform();
  }

  /**
   * How much to shrink a quarter-turned picture so it still fits.
   *
   * `object-fit: contain` fits the picture to the stage *before* the CSS
   * rotation happens, and a rotation does not re-run it — so a landscape
   * photograph turned on its side is laid out at its landscape size and then
   * spun, and its now-vertical long edge runs straight off the top and bottom
   * of the screen. This is the factor that puts it back inside.
   */
  fitScale() {
    const el = this.media;
    const turn = Math.abs(this.transform.rotate % 180);
    if (!el || turn !== 90) return 1;

    const boxW = el.offsetWidth;
    const boxH = el.offsetHeight;
    // The element fills the stage; what is *painted* inside it is the picture
    // fitted to that box by object-fit, and that is the thing being turned.
    // Measuring the element box instead gives the wrong factor entirely,
    // because the box is the shape of the window rather than the photograph.
    const natW = el.naturalWidth || el.videoWidth || boxW;
    const natH = el.naturalHeight || el.videoHeight || boxH;
    if (!boxW || !boxH || !natW || !natH) return 1;

    const contain = Math.min(boxW / natW, boxH / natH);
    const paintedW = natW * contain;
    const paintedH = natH * contain;
    // A quarter turn swaps which side has to fit which.
    // A portrait turned landscape can use MORE of the stage. Capping this
    // at 1 leaves it unnecessarily small even though there is room to fit.
    return Math.min(boxW / paintedH, boxH / paintedW);
  }

  applyTransform() {
    const el = this.media;
    if (!el) return;
    const { scale, x, y, rotate } = this.transform;
    const fitted = scale * this.fitScale();
    el.style.transform = `translate3d(${x}px, ${y}px, 0) scale(${fitted}) rotate(${rotate}deg)`;
    el.style.transition = this.pointers.size ? 'none' : 'transform .18s cubic-bezier(.22,.61,.36,1)';
    // A live photo's clip is laid over the picture: it zooms and turns with it.
    const clip = this.stage.querySelector('.live-clip');
    if (clip) {
      clip.style.transform = el.style.transform;
      clip.style.transition = el.style.transition;
    }
    el.classList.toggle('zoomed', scale > 1.02);
    // Zooming out only makes sense once something has zoomed in — at 1:1
    // there is nowhere further out to go, so the button starts disabled and
    // wakes up the moment a zoom-in (wheel, pinch, double-click or the +
    // button) actually moves the scale past 1.
    const zoomOut = this.root.querySelector('#v-zoom-out');
    if (zoomOut) zoomOut.disabled = scale <= 1;
    const zoomIn = this.root.querySelector('#v-zoom-in');
    if (zoomIn) zoomIn.disabled = scale >= MAX_ZOOM;
  }

  zoomBy(factor, origin) {
    const previous = this.transform.scale;
    const next = Math.max(1, Math.min(MAX_ZOOM, previous * factor));
    if (next === previous) return;
    if (origin && this.media) {
      const rect = this.media.getBoundingClientRect();
      const dx = origin.x - (rect.left + rect.width / 2);
      const dy = origin.y - (rect.top + rect.height / 2);
      const ratio = next / previous;
      this.transform.x = this.transform.x - dx * (ratio - 1);
      this.transform.y = this.transform.y - dy * (ratio - 1);
    }
    this.transform.scale = next;
    if (next === 1) {
      this.transform.x = 0;
      this.transform.y = 0;
    }
    this.applyTransform();
  }

  /**
   * Turn the photograph, and remember it.
   *
   * The button used to be a CSS transform and nothing else, so a scanned print
   * that opened sideways opened sideways again tomorrow. For a household
   * profile the turn is now saved: the thumbnails everybody sees are rewritten,
   * and it is marked as a decision a person made so no later scan overrules it.
   * Guests still get a temporary look for this one viewing, because a rotation
   * changes what the whole household sees.
   */
  async rotate(step = 90) {
    this.transform.rotate = (((this.transform.rotate + step) % 360) + 360) % 360;
    this.transform.scale = 1;
    this.transform.x = 0;
    this.transform.y = 0;
    this.applyTransform();

    const item = this.item;
    if (!item || item.kind !== 'picture' || !this.canRotate) return;

    // One save per photograph at a time, and always the latest turn. Three
    // quick clicks used to send three requests at once; they raced on the
    // server, and whichever finished last decided the angle — not the last
    // click. Clicks during a save only update what is wanted, and the save
    // sends that when it lands.
    const pending = this.turnsWanted.has(item.id);
    this.turnsWanted.set(item.id, this.transform.rotate);
    if (pending) return;

    let sent;
    while (this.turnsWanted.get(item.id) !== sent) {
      sent = this.turnsWanted.get(item.id);
      try {
        const saved = await api.rotate(item.id, sent);
        item.rotation = saved.rotation;
        item.rotation_source = saved.rotation_source;
        item.width = saved.width;
        item.height = saved.height;
        // The rewritten thumbnail lives at a new URL; without this the gallery
        // redraws from the copy the browser cached before the turn.
        item.thumb_v = saved.thumb_v;
        if (this.item === item) {
          this.renderChrome(item);
          if (!this.info.hidden) this.renderInfo(item);
        }
        this.onChange?.(item);
        // Deliberately silent. The photograph turning in front of you is the
        // confirmation; a toast on top of it is noise on an action whose whole
        // result is visible.
      } catch (exc) {
        this.toast?.(exc.message || i18n.t('That could not be saved.'), true);
        break;
      }
    }
    this.turnsWanted.delete(item.id);
  }

  onWheel(event) {
    if (event.ctrlKey || event.metaKey || this.transform.scale > 1.02) {
      event.preventDefault();
      this.zoomBy(event.deltaY < 0 ? 1.15 : 1 / 1.15, { x: event.clientX, y: event.clientY });
    }
  }

  onDoubleClick(event) {
    if (this.item?.kind !== 'picture') return;
    // Two quick presses of the Live button are two presses of a button.
    if (event.target.closest('.live-badge')) return;
    if (this.transform.scale > 1.02) this.resetTransform();
    else this.zoomBy(2.6, { x: event.clientX, y: event.clientY });
  }

  onPointerDown(event) {
    // The Live button is a button: captured to the stage, its click went to
    // the stage instead, so a tap on it did nothing at all.
    if (event.target.closest('video, audio, .filmstrip, .viewer-info, .live-badge')) return;
    this.pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    this.stage.setPointerCapture?.(event.pointerId);
    this.dragStart = {
      x: event.clientX,
      y: event.clientY,
      tx: this.transform.x,
      ty: this.transform.y,
      t: performance.now(),
    };
    if (this.pointers.size === 2) {
      const [a, b] = [...this.pointers.values()];
      this.pinchStart = {
        distance: Math.hypot(a.x - b.x, a.y - b.y),
        scale: this.transform.scale,
      };
    }
  }

  onPointerMove(event) {
    if (!this.pointers.has(event.pointerId)) return;
    this.pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });

    if (this.pointers.size === 2 && this.pinchStart) {
      const [a, b] = [...this.pointers.values()];
      const distance = Math.hypot(a.x - b.x, a.y - b.y);
      this.transform.scale = Math.max(
        1, Math.min(MAX_ZOOM, this.pinchStart.scale * (distance / this.pinchStart.distance)),
      );
      this.applyTransform();
      return;
    }

    if (this.transform.scale > 1.02 && this.dragStart) {
      this.transform.x = this.dragStart.tx + (event.clientX - this.dragStart.x);
      this.transform.y = this.dragStart.ty + (event.clientY - this.dragStart.y);
      this.media?.classList.add('panning');
      this.applyTransform();
    }
  }

  onPointerUp(event) {
    const start = this.dragStart;
    this.pointers.delete(event.pointerId);
    this.media?.classList.remove('panning');
    if (this.pointers.size < 2) this.pinchStart = null;
    if (!start) return;
    this.dragStart = null;

    const dx = event.clientX - start.x;
    const dy = event.clientY - start.y;
    const elapsed = performance.now() - start.t;

    // A quick horizontal flick pages, but only when not zoomed in.
    if (this.transform.scale <= 1.02 && Math.abs(dx) > 60
        && Math.abs(dx) > Math.abs(dy) * 1.6 && elapsed < 600) {
      this.step(dx < 0 ? 1 : -1);
      return;
    }
    this.applyTransform();
  }

  /* -- slideshow ------------------------------------------------------- */

  toggleSlideshow() {
    if (this.slideshow) this.stopSlideshow();
    else this.startSlideshow();
  }

  /** The question that comes before a slideshow: how long, and whether to loop. */
  toggleSlidePop(force, returnFocus = false) {
    const pop = this.root.querySelector('#slide-pop');
    if (!pop) return;
    const show = force === undefined ? pop.hidden : force;
    pop.hidden = !show;
    if (show) {
      this.closeMoreMenu();
      pop.querySelector('#v-slide-start').focus();
    } else if (returnFocus) {
      this.root.querySelector('#v-slideshow').focus();
    }
  }

  startSlideshow() {
    if (!this.ids.length) return;
    this.toggleSlidePop(false);
    if (this.isKiosk) this.toggleKiosk();
    this.stopSlideshow();
    this.root.querySelector('#v-slideshow').classList.add('on');
    this.root.querySelector('#v-slideshow').setAttribute('aria-pressed', 'true');
    this.root.querySelector('#v-slideshow').setAttribute('aria-label', i18n.t('Stop slideshow'));
    this.scheduleSlideshow();
    this.toast?.(i18n.t('Slideshow started.'));
  }

  scheduleSlideshow() {
    if (this.slideshow) clearInterval(this.slideshow);
    const tick = () => {
      if (document.hidden) return;
      const media = this.stage.querySelector('video, audio');
      if (media && !media.ended && !media.error) return;
      // A proxy may still be loading; don't skip away while it is prepared.
      if (!media && this.stage.querySelector('.proxy-bar')) return;
      this.advanceSlideshow();
    };
    this.slideshow = setInterval(tick, this.slideshowDelay);
  }

  async advanceSlideshow() {
    if (!this.slideshow || this.slideAdvancing) return;
    if (this.index >= this.ids.length - 1 && !this.slideshowLoop) { this.stopSlideshow(); return; }
    this.slideAdvancing = true;
    try { await this.goTo((this.index + 1) % this.ids.length); }
    finally { this.slideAdvancing = false; }
  }

  stopSlideshow() {
    if (this.slideshow) clearInterval(this.slideshow);
    this.slideshow = null;
    this.root.querySelector('#v-slideshow').classList.remove('on');
    this.root.querySelector('#v-slideshow').setAttribute('aria-pressed', 'false');
    this.root.querySelector('#v-slideshow').setAttribute('aria-label', i18n.t('Start slideshow'));
  }

  /* -- keyboard -------------------------------------------------------- */

  handleKey(event) {
    if (event.ctrlKey || event.metaKey || event.altKey) return false;
    const key = event.key.toLowerCase();
    if (this.isKiosk && key === 'escape') { this.toggleKiosk(); return true; }
    switch (key) {
      case 'escape':
        if (this.stories?.isOpen) this.stories.toggle(false, true); else this.close();
        return true;
      case 'arrowright': this.step(1); return true;
      case 'arrowleft': this.step(-1); return true;
      case 'home': this.goTo(0); return true;
      case 'end': this.goTo(this.ids.length - 1); return true;
      case 'f': this.toggleFavorite(); return true;
      case 'i': this.toggleInfo(); return true;
      case 'v': this.stories?.toggle(); return true;
      case 's': this.loadSimilar(); return true;
      case 'r': this.rotate(event.shiftKey ? -90 : 90); return true;
      case 'd': this.root.querySelector('#v-download').click(); return true;
      case '+': case '=': this.zoomBy(1.4); return true;
      case '-': this.zoomBy(1 / 1.4); return true;
      case '0': this.resetTransform(); return true;
      case 'k': this.toggleKiosk(); return true;
      case ' ': this.toggleSlideshow(); return true;
      default: return false;
    }
  }
}
