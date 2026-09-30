/**
 * The Photo Studio.
 *
 * Three promises hold the whole thing together, and every feature below is
 * shaped by them:
 *
 *   *Your original is never written to.* Editing produces a new file beside
 *   it. Everything here is a description of an edit until you press Save.
 *
 *   *Nothing leaves the machine.* All pixel work happens in this browser, in
 *   a worker. The one request that touches the server asks where the face in
 *   the photograph is, and it is answered by the model the household already
 *   downloaded, on the household's own computer.
 *
 *   *Nothing is invented.* Every adjustment is arithmetic on pixels that were
 *   photographed. Hair density thickens hair that is there; it does not grow
 *   hair that is not, and the panel says so rather than letting you find out.
 *
 * The edit is a stack, not a sequence of destructive steps:
 *
 *   source → geometry (crop, straighten, quarter turns) → retouch ops
 *          → stage → worker(settings, masks) → what you see
 *
 * `stage` is rebuilt from the source whenever geometry or retouching changes,
 * which is what makes undo cheap: history stores the *description* (settings,
 * geometry, the list of retouch ops, the masks) and never a bitmap of the
 * result.
 */

import { reportUnauthorized } from './api.js';
import * as i18n from './i18n.js';
import { initPalette, studioCommands } from './palette.js';

const defaults = () => ({
  // light
  exposure: 0, contrast: 0, highlights: 0, shadows: 0, whites: 0, blacks: 0,
  // colour
  warmth: 0, tint: 0, saturation: 0,
  // detail
  clarity: 0, sharpen: 0, vignette: 0,
  // skin
  skinWarm: 0, redness: 0, smooth: 0,
  // hair
  hairDensity: 0, hairRoots: 0, hairAmount: 0, hairLight: 0, hairColor: '#684331',
  // painted brushes
  dodgeAmount: 0, burnAmount: 0, softenAmount: 0,
});

const MASKS = ['skin', 'hair', 'dodge', 'burn', 'soften'];
const PAINTED = { skin: 'skin', hair: 'hair', dodge: 'dodge', burn: 'burn', soften: 'soften' };
const ASPECTS = { free: null, '1:1': 1, '4:5': 0.8, '3:2': 1.5, '2:3': 2 / 3, '16:9': 16 / 9 };

const canvas = (w, h) => Object.assign(document.createElement('canvas'), { width: w, height: h });
const clamp01 = (v) => Math.max(0, Math.min(1, v));

export class PhotoEditor {
  constructor(item, saved) {
    this.item = item; this.saved = saved; this.settings = defaults();
    this.geometry = { crop: null, straighten: 0, quarter: 0, aspect: 'free' };
    this.retouchOps = [];
    this.history = []; this.future = []; this.tool = 'skin'; this.panel = 'light';
    this.serial = 0; this.jobs = new Map(); this.dirty = false; this.busy = false;
  }

  /* -- markup ------------------------------------------------------------ */

  /** One line-art icon set for the tool rail, so the rail reads as one thing. */
  icon(name) {
    const d = {
      light: '<circle cx="12" cy="12" r="4.2"/><path d="M12 2.6v2.2M12 19.2v2.2M2.6 12h2.2M19.2 12h2.2M5.3 5.3l1.6 1.6M17.1 17.1l1.6 1.6M18.7 5.3l-1.6 1.6M6.9 17.1l-1.6 1.6"/>',
      colour: '<circle cx="9.6" cy="10.2" r="5.6"/><circle cx="14.4" cy="13.8" r="5.6"/>',
      detail: '<path d="M10.6 3.2l1.9 4.7 4.7 1.9-4.7 1.9-1.9 4.7-1.9-4.7-4.7-1.9 4.7-1.9z"/><path d="M18.1 15.2l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8z"/>',
      crop: '<path d="M6.8 2.5v14.7H21.5"/><path d="M2.5 6.8h14.7V21.5"/>',
      retouch: '<path d="M3.2 20.8l6.4-6.4"/><path d="M13.2 3.6l3.2 3.2-6.4 6.4-3.2-3.2z"/><path d="M18.6 9.4l.8 1.9 1.9.8-1.9.8-.8 1.9-.8-1.9-1.9-.8 1.9-.8z"/>',
      skin: '<circle cx="12" cy="12" r="8.8"/><path d="M9.2 10.2h.02M14.8 10.2h.02"/><path d="M8.6 14.6a4.6 4.6 0 0 0 6.8 0"/>',
      hair: '<path d="M3.9 13.6a8.1 8.1 0 0 1 16.2 0"/><path d="M3.9 13.6v6.2M20.1 13.6v6.2"/><path d="M8.1 11.4c1.5-2 2.7-3 3.9-3s2.4 1 3.9 3"/>',
      brush: '<path d="M9.6 14.4L3 21"/><path d="M20.4 3.6a2.2 2.2 0 0 0-3.1 0L8.8 12.1l3.1 3.1 8.5-8.5a2.2 2.2 0 0 0 0-3.1z"/>',
    }[name] || '';
    return `<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">${d}</svg>`;
  }

  /** A rail entry. `kind` is 'panel' or 'tool' — the wiring reads both. */
  rail(kind, key, label) {
    return `<button type="button" data-${kind}="${key}" aria-pressed="false">`
      + `${this.icon(key)}<span>${i18n.t(label)}</span></button>`;
  }

  slider(key, label, min = -100) {
    return `<label class="pe-slider"><span>${i18n.t(label)}</span><output data-value="${key}">0</output>`
      + `<input type="range" data-setting="${key}" min="${min}" max="100" value="0"></label>`;
  }

  /** The long explanations are worth keeping, but not worth shouting. */
  note(text, summary = i18n.key('How this works')) {
    return `<details class="pe-note"><summary>${i18n.t(summary)}</summary>`
      + `<div>${i18n.t(text)}</div></details>`;
  }

  markup() {
    const s = (k, l, m) => this.slider(k, l, m);
    const r = (kind, key, label) => this.rail(kind, key, label);
    return `
      <header>
        <div class="pe-brand"><span class="pe-brand-mark" aria-hidden="true">✦</span>
          <h2 id="pe-title">Photo Studio<span class="pe-local">On-device</span></h2></div>

        <!-- Only this middle part scrolls when the window is narrow. The name
             stays where it is and, more to the point, so does the way out. -->
        <div class="pe-bar">
        <div class="pe-cluster">
          <button data-action="undo" disabled>Undo</button>
          <button data-action="redo" disabled>Redo</button>
          <button data-action="reset">Reset all</button>
        </div>
        <div class="pe-cluster">
          <button data-action="compare" aria-pressed="false">Show original</button>
          <button data-action="split" aria-pressed="false">Compare</button>
        </div>
        <span id="pe-preview-state" aria-live="polite">Edited preview</span>

        <label class="pe-zoom">Zoom
          <select id="pe-zoom"><option value="1">Fit</option><option value="1.5">150%</option><option value="2">200%</option></select></label>
        <button type="button" class="pe-theme-toggle" id="pe-palette-btn" title="Commands (Ctrl/⌘ K)" aria-label="Command palette">
          <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
        </button>
        <button type="button" class="pe-theme-toggle" id="pe-theme-btn" title="Theme (T)" aria-label="Toggle theme">
          <svg viewBox="0 0 24 24" class="ico-sun"><circle cx="12" cy="12" r="4.5"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M19.1 4.9l-1.4 1.4M6.3 17.7l-1.4 1.4"/></svg>
          <svg viewBox="0 0 24 24" class="ico-moon"><path d="M20 14.5A8.5 8.5 0 1 1 10.2 4a7 7 0 0 0 9.8 10.5Z"/></svg>
        </button>
        </div>
        <button type="button" data-action="close" aria-label="Close photo editor">✕</button>
      </header>

      <div class="pe-layout">
        <section class="pe-workspace" aria-label="Photo preview">
          <div class="pe-viewport"><div class="pe-picture">
            <canvas id="pe-photo" aria-label="Photo preview"></canvas>
            <canvas id="pe-mask" aria-label="Paint the selected area, or click to retouch" tabindex="0"></canvas>
            <div id="pe-crop" class="pe-crop" hidden aria-hidden="true"><div data-handle="nw"></div><div data-handle="ne"></div><div data-handle="sw"></div><div data-handle="se"></div></div>
            <div id="pe-divider" aria-hidden="true" hidden><span>↔</span></div>
            <div class="pe-compare-labels" hidden><span>Original</span><span>Edited</span></div>
          </div></div>

          <!-- Both of these are laid over the picture rather than taking a row
               of their own: the slider is only there in split view, and the
               status line is a sentence, not furniture. -->
          <label class="pe-comparison" hidden>Before <input id="pe-split-position" type="range" min="0" max="100" value="50" aria-label="Before and after comparison position"> After</label>
          <p class="pe-status" role="status" aria-live="polite">Loading your photograph…</p>
        </section>

        <aside>
          <nav class="pe-tools" aria-label="Editing tools">
            <p class="pe-group">Adjust</p>
            ${r('panel', 'light', i18n.key('Light'))}${r('panel', 'colour', i18n.key('Colour'))}${r('panel', 'detail', i18n.key('Detail'))}
            <p class="pe-group">Frame</p>
            ${r('panel', 'crop', i18n.key('Crop'))}
            <p class="pe-group">Portrait</p>
            ${r('panel', 'retouch', i18n.key('Retouch'))}${r('tool', 'skin', i18n.key('Skin'))}${r('tool', 'hair', i18n.key('Hair'))}
            <p class="pe-group">Paint</p>
            ${r('panel', 'brush', i18n.key('Brush'))}
          </nav>

          <div class="pe-controls">
            <fieldset data-area="light"><legend>Light</legend>
              <p class="pe-lede">Balance the exposure across the whole photograph.</p>
              <button data-preset="auto">✦ Balance this photograph</button>
              ${s('exposure', i18n.key('Exposure'))}${s('contrast', i18n.key('Contrast'))}
              ${s('highlights', i18n.key('Highlights'))}${s('shadows', i18n.key('Shadows'))}
              ${s('whites', i18n.key('Whites'))}${s('blacks', i18n.key('Blacks'))}
              ${this.note(i18n.key('Highlights and Shadows each hold one end of the range, so recovering a bright sky will not flatten the shadows.'))}</fieldset>

            <fieldset data-area="colour" hidden><legend>Colour</legend>
              <p class="pe-lede">Warmth, tint and vibrance, across the whole photograph.</p>
              ${s('warmth', i18n.key('Warmth'))}${s('tint', i18n.key('Tint (green ↔ magenta)'))}${s('saturation', i18n.key('Vibrance'))}
              ${this.note(i18n.key('Vibrance lifts muted colour and leaves colour that is already strong alone, which is what keeps skin from going orange.'))}</fieldset>

            <fieldset data-area="detail" hidden><legend>Detail</legend>
              <p class="pe-lede">Definition and edges, across the whole photograph.</p>
              ${s('clarity', i18n.key('Clarity'), 0)}${s('sharpen', i18n.key('Sharpen'), 0)}${s('vignette', i18n.key('Vignette'), 0)}
              ${this.note(i18n.key('Clarity is midtone contrast and is held back in the brightest and darkest areas, where it would only make halos and noise.'))}</fieldset>

            <fieldset data-area="crop" hidden><legend>Crop &amp; straighten</legend>
              <p class="pe-lede">Drag a corner on the photograph to set the crop.</p>
              <div class="pe-row"><button data-turn="-90">↺ Rotate left</button><button data-turn="90">↻ Rotate right</button></div>
              <label class="pe-slider"><span>Straighten</span><output data-value="straighten">0°</output>
                <input type="range" id="pe-straighten" min="-15" max="15" step="0.1" value="0"></label>
              <p class="pe-label">Shape</p>
              <div class="pe-seg pe-aspects">${Object.keys(ASPECTS).map((a) => `<button data-aspect="${a}"${a === 'free' ? ' aria-pressed="true"' : ''}>${a === 'free' ? 'Free' : a}</button>`).join('')}</div>
              <button data-action="crop-reset" class="pe-quiet">Reset crop</button>
              ${this.note(i18n.key('Straightening trims the edges that rotate out of frame, so the picture stays rectangular.'))}</fieldset>

            <fieldset data-area="retouch" hidden><legend>Retouch</legend>
              <p class="pe-lede">Click the mark on the photograph.</p>
              <div class="pe-seg"><button data-retouch="heal" aria-pressed="true">Blemish</button><button data-retouch="redeye" aria-pressed="false">Red-eye</button></div>
              <label class="pe-slider"><span>Spot size</span><input id="pe-spot" type="range" min="6" max="90" value="22"></label>
              <p id="pe-retouch-count" class="pe-hint">Nothing retouched yet.</p>
              <button data-action="undo-spot" class="pe-quiet">Undo last spot</button>
              ${this.note(i18n.key('Blemish borrows clean skin from just beside the mark; red-eye drains the red and keeps the catchlight.'))}</fieldset>

            <fieldset id="pe-selection" hidden><legend>Selection</legend>
              <p class="pe-lede">Drag over the photograph to paint where this tool applies.</p>
              <button data-action="auto-select" hidden>✦ Find the face for me</button>
              <p id="pe-selection-state" class="pe-hint" aria-live="polite">No skin area painted yet.</p>
              ${this.note(`<label class="pe-check"><input type="checkbox" id="pe-erase"> Erase selection</label>
                <label class="pe-check"><input type="checkbox" id="pe-edge" checked> Colour-aware brush</label>
                <label class="pe-slider"><span>Brush size</span><input id="pe-size" type="range" min="5" max="180" value="55"></label>
                <label class="pe-check"><input type="checkbox" id="pe-overlay"> Show painted area</label>
                <button data-action="clear" class="pe-quiet">Clear this selection</button>
                <p class="pe-hint">Colour-aware mode follows colours near the start of each stroke. Turn it off to select freely.</p>`, 'Brush settings')}</fieldset>

            <fieldset data-area="skin" hidden><legend>Skin finish</legend>
              <p class="pe-hint" data-area-hint="skin">Paint skin on the photo to enable these adjustments.</p>
              <button data-preset="skin">✦ Natural skin touch-up</button>
              ${s('skinWarm', i18n.key('Tone warmth'))}${s('redness', i18n.key('Reduce redness'), 0)}${s('smooth', i18n.key('Gentle smoothing'), 0)}
              ${this.note(i18n.key('Only your skin selection is affected, and nothing is lightened automatically.'))}</fieldset>

            <fieldset data-area="hair" hidden><legend>Hair</legend>
              <p class="pe-hint" data-area-hint="hair">Paint hair on the photo to enable these adjustments.</p>
              <button data-preset="hair">✦ Fuller hair</button>
              ${s('hairDensity', i18n.key('Density'), 0)}${s('hairRoots', i18n.key('Roots &amp; depth'), 0)}
              <label class="pe-slider"><span>Tint</span><input type="color" id="pe-hair-color" value="#684331"></label>
              ${s('hairAmount', i18n.key('Tint strength'), 0)}${s('hairLight', i18n.key('Shadows &amp; shine'))}
              ${this.note(`<p><strong>Density</strong> darkens the scalp showing between strands and lifts the contrast that separates one strand from the next — it makes the hair you photographed look fuller. It cannot add hair that is not in the picture, and it will do nothing on bare scalp.</p>
                <p>Try a local hair suggestion, then check and refine it with the brush. Only distinct, bounded dark hair can be suggested; for light hair or an uncertain background, paint by hand.</p>`)}</fieldset>

            <fieldset data-area="brush" hidden><legend>Paint an adjustment</legend>
              <p class="pe-lede">Pick a brush, paint the area, then set its strength.</p>
              <div class="pe-seg"><button data-tool="dodge" aria-pressed="false">Lighten</button><button data-tool="burn" aria-pressed="false">Darken</button><button data-tool="soften" aria-pressed="false">Soften</button></div>
              ${s('dodgeAmount', i18n.key('Lighten strength'), 0)}${s('burnAmount', i18n.key('Darken strength'), 0)}${s('softenAmount', i18n.key('Soften strength'), 0)}
              ${this.note(i18n.key('Each brush keeps its own painted area, so lightening a face and darkening a background do not fight over the same pixels.'))}</fieldset>
          </div>
        </aside>
      </div>

      <footer>
        <p><strong>Your original stays untouched.</strong>
          <small id="pe-size-note">A separate copy is added to the same folder.</small></p>
        <div class="pe-export">
          <label class="pe-export-field"><span>Format</span>
            <select id="pe-format" aria-label="Export format">
              <option value="image/png">PNG</option>
              <option value="image/jpeg">JPEG</option>
              <option value="image/webp">WebP</option>
            </select></label>
          <label class="pe-export-field" id="pe-quality-field"><span>Quality</span>
            <input type="range" id="pe-quality" min="50" max="100" value="92" aria-label="Export quality">
            <output id="pe-quality-out">92</output></label>
          <button data-action="download" class="pe-download" disabled>Download</button>
          <button data-action="save" class="pe-save" disabled>Save to library</button>
        </div>
      </footer>`;
  }

  /* -- lifecycle --------------------------------------------------------- */

  async open() {
    this.previousFocus = document.activeElement;
    this.dialog = document.createElement('dialog');
    this.dialog.className = 'photo-editor';
    this.dialog.setAttribute('aria-labelledby', 'pe-title');
    this.dialog.innerHTML = this.markup();
    document.body.append(this.dialog); this.dialog.showModal();
    // Focus the room, not its first button, so no control opens wearing a ring.
    this.dialog.tabIndex = -1; this.dialog.focus();
    this.dialog.addEventListener('cancel', (e) => { e.preventDefault(); this.close(); });
    const cycleTheme = () => {
      if (typeof window.cycleTheme === 'function') {
        window.cycleTheme();
      } else {
        const order = ['system', 'light', 'dark'];
        const cur = document.documentElement.dataset.theme || 'system';
        const next = order[(order.indexOf(cur) + 1) % order.length];
        document.documentElement.dataset.theme = next;
        try {
          localStorage.setItem('mv.theme', JSON.stringify(next));
          localStorage.setItem('ninaivu.theme', JSON.stringify(next));
        } catch {}
      }
    };
    this.dialog.addEventListener('keydown', (e) => {
      const typing = e.target.matches('input:not([type=range]), textarea, select') || e.target.isContentEditable;
      if (!typing && e.key.toLowerCase() === 't' && !e.ctrlKey && !e.metaKey && !e.altKey) {
        e.preventDefault();
        cycleTheme();
        return;
      }
      e.stopPropagation();
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') {
        e.preventDefault(); this.restore(e.shiftKey);
      }
    });
    this.dialog.querySelector('[data-action="close"]').onclick = () => this.close();
    const themeBtn = this.dialog.querySelector('#pe-theme-btn');
    if (themeBtn) themeBtn.onclick = cycleTheme;
    try {
      this.worker = new Worker(new URL('./editor-worker.js', import.meta.url));
      this.worker.onmessage = ({ data }) => {
        this.jobs.get(data.id)?.resolve(data.pixels); this.jobs.delete(data.id);
      };
      this.worker.onerror = () => {
        this.workerFailed = true;
        for (const job of this.jobs.values()) job.reject(new Error('Photo processing failed. Close the editor and try again.'));
        this.jobs.clear(); this.status('Photo processing failed. Close the editor and try again.');
      };

      // The viewable copy, so HEIC and raw open here too.
      const img = new Image(); img.src = this.item.view || this.item.src; await img.decode();
      if (!this.dialog.open) return;
      const scale = Math.min(1, Math.sqrt(24_000_000 / (img.naturalWidth * img.naturalHeight)));
      const w = Math.floor(img.naturalWidth * scale), h = Math.floor(img.naturalHeight * scale);
      const rotate = ((this.item.rotation || 0) % 360 + 360) % 360;
      this.source = canvas(rotate % 180 ? h : w, rotate % 180 ? w : h);
      const ctx = this.source.getContext('2d');
      ctx.translate(this.source.width / 2, this.source.height / 2);
      ctx.rotate(rotate * Math.PI / 180);
      ctx.drawImage(img, -w / 2, -h / 2, w, h);
      this.exportScale = scale;

      this.photo = this.dialog.querySelector('#pe-photo');
      this.overlay = this.dialog.querySelector('#pe-mask');
      this.buildStage();
      this.dialog.querySelector('#pe-size-note').textContent =
        `${this.source.width} × ${this.source.height} • Saved to the same folder • ${scale < 1 ? 'Reduced to 24 MP for editing' : 'Original resolution'}. Camera metadata is not embedded.`;
      this.wire(); this.ready = true; this.buttons();
      // Ctrl/⌘ K, inside the dialog: a modal dialog covers the page's own.
      const palette = initPalette({ commands: () => studioCommands(this.dialog), host: this.dialog,
                                    id: 'pe-palette', target: this.dialog });
      const paletteBtn = this.dialog.querySelector('#pe-palette-btn');
      if (paletteBtn && palette) paletteBtn.onclick = palette.open;
      this.selectPanel('light');
      this.updateSelections();
      this.resizeObserver = new ResizeObserver(() => this.fit());
      this.resizeObserver.observe(this.dialog.querySelector('.pe-viewport'));
      this.fit();
      this.photo.getContext('2d').putImageData(this.original, 0, 0);
      this.status('Adjust the light, or pick a tool. Nothing is saved until you say so.');
    } catch (error) { this.status(`Could not open this photo. ${error.message}`); }
  }

  /* -- geometry and retouching ------------------------------------------- */

  /** Rebuild everything downstream of the source: geometry, then retouch ops. */
  buildStage(keepMasks = false) {
    const { quarter, straighten, crop } = this.geometry;
    const turned = quarter % 180 !== 0;
    const sw = turned ? this.source.height : this.source.width;
    const sh = turned ? this.source.width : this.source.height;

    // Straightening rotates the frame, so the usable rectangle shrinks; the
    // alternative is blank corners, which is never what anybody wanted.
    const radians = Math.abs(straighten * Math.PI / 180);
    const shrink = radians ? (Math.cos(radians) + Math.sin(radians) * sh / sw) : 1;
    const box = crop || { x: 0, y: 0, w: 1, h: 1 };

    const full = canvas(sw, sh);
    const fctx = full.getContext('2d');
    fctx.save();
    fctx.translate(sw / 2, sh / 2);
    fctx.rotate((quarter + straighten) * Math.PI / 180);
    if (straighten) fctx.scale(shrink, shrink);
    fctx.drawImage(this.source, -this.source.width / 2, -this.source.height / 2);
    fctx.restore();

    const cw = Math.max(8, Math.round(sw * box.w));
    const ch = Math.max(8, Math.round(sh * box.h));
    this.stage = canvas(cw, ch);
    this.stage.getContext('2d').drawImage(
      full, Math.round(sw * box.x), Math.round(sh * box.y), cw, ch, 0, 0, cw, ch);

    for (const op of this.retouchOps) this.applyOp(this.stage, op, 1);

    const ratio = Math.min(1, 1400 / Math.max(cw, ch));
    this.preview = canvas(Math.max(1, Math.round(cw * ratio)), Math.max(1, Math.round(ch * ratio)));
    this.preview.getContext('2d').drawImage(this.stage, 0, 0, this.preview.width, this.preview.height);
    this.original = this.preview.getContext('2d').getImageData(0, 0, this.preview.width, this.preview.height);

    const old = keepMasks ? this.masks : null;
    this.masks = Object.fromEntries(MASKS.map((k) => [k, canvas(this.preview.width, this.preview.height)]));
    if (old) {
      for (const k of MASKS) {
        if (old[k]) this.masks[k].getContext('2d').drawImage(old[k], 0, 0, this.preview.width, this.preview.height);
      }
    }
    for (const c of [this.photo, this.overlay]) { c.width = this.preview.width; c.height = this.preview.height; }
    this.dialog.querySelector('.pe-picture').style.aspectRatio = `${this.preview.width}/${this.preview.height}`;
    this.result = null;
    this.fit?.();
  }

  /** One retouch operation, replayed at whatever scale the target is. */
  applyOp(target, op, scale) {
    const ctx = target.getContext('2d');
    const x = op.x * target.width, y = op.y * target.height;
    const r = Math.max(2, op.r * target.width);
    if (op.kind === 'redeye') {
      const x0 = Math.max(0, Math.round(x - r)), y0 = Math.max(0, Math.round(y - r));
      const w = Math.min(target.width - x0, Math.round(r * 2)), h = Math.min(target.height - y0, Math.round(r * 2));
      if (w < 1 || h < 1) return;
      const data = ctx.getImageData(x0, y0, w, h);
      const px = data.data;
      for (let iy = 0; iy < h; iy++) {
        for (let ix = 0; ix < w; ix++) {
          if (Math.hypot(ix + x0 - x, iy + y0 - y) > r) continue;
          const i = (iy * w + ix) * 4;
          const other = Math.max(px[i + 1], px[i + 2]);
          // Only the red that has no business being there. A catchlight is
          // near-white in all three channels and survives untouched.
          if (px[i] > other * 1.5 && px[i] > 60) {
            const level = (px[i + 1] + px[i + 2]) / 2;
            px[i] = level; px[i + 1] = level * 1.02; px[i + 2] = level * 1.02;
          }
        }
      }
      ctx.putImageData(data, x0, y0);
      return;
    }
    // Blemish: borrow the cleanest nearby patch and feather it in.
    const donor = op.donor || { x: op.x, y: op.y };
    const dx = donor.x * target.width, dy = donor.y * target.height;
    const patch = canvas(Math.round(r * 2), Math.round(r * 2));
    patch.getContext('2d').drawImage(target, Math.round(dx - r), Math.round(dy - r),
      Math.round(r * 2), Math.round(r * 2), 0, 0, patch.width, patch.height);
    const feather = patch.getContext('2d');
    feather.globalCompositeOperation = 'destination-in';
    const gradient = feather.createRadialGradient(patch.width / 2, patch.height / 2, r * 0.25,
      patch.width / 2, patch.height / 2, r);
    gradient.addColorStop(0, 'rgba(0,0,0,1)');
    gradient.addColorStop(1, 'rgba(0,0,0,0)');
    feather.fillStyle = gradient;
    feather.fillRect(0, 0, patch.width, patch.height);
    ctx.drawImage(patch, Math.round(x - r), Math.round(y - r));
  }

  /** Where to borrow from: the calmest of eight neighbours at the same distance. */
  findDonor(nx, ny, nr) {
    const data = this.original.data, W = this.preview.width, H = this.preview.height;
    const px = nx * W, py = ny * H, pr = Math.max(2, nr * W);
    const sample = (cx, cy) => {
      let sum = 0, sum2 = 0, n = 0;
      for (let y = -pr; y <= pr; y += Math.max(1, pr / 4)) {
        for (let x = -pr; x <= pr; x += Math.max(1, pr / 4)) {
          const ix = Math.round(cx + x), iy = Math.round(cy + y);
          if (ix < 0 || iy < 0 || ix >= W || iy >= H) return null;
          const i = (iy * W + ix) * 4;
          const l = 0.2126 * data[i] + 0.7152 * data[i + 1] + 0.0722 * data[i + 2];
          sum += l; sum2 += l * l; n++;
        }
      }
      return n ? { mean: sum / n, variance: sum2 / n - (sum / n) ** 2 } : null;
    };
    const here = sample(px, py);
    let best = null;
    for (let a = 0; a < 8; a++) {
      const angle = a * Math.PI / 4;
      const cx = px + Math.cos(angle) * pr * 2.6, cy = py + Math.sin(angle) * pr * 2.6;
      const stat = sample(cx, cy);
      if (!stat) continue;
      // Closest in tone, and as flat as possible — a donor with an eyebrow in
      // it just moves the problem somewhere more obvious.
      const cost = Math.abs(stat.mean - (here ? here.mean : stat.mean)) + Math.sqrt(Math.max(0, stat.variance)) * 1.4;
      if (!best || cost < best.cost) best = { cost, x: cx / W, y: cy / H };
    }
    return best ? { x: best.x, y: best.y } : { x: nx, y: ny };
  }

  /* -- state ------------------------------------------------------------- */

  status(text) { this.dialog.querySelector('.pe-status').textContent = text; }

  snapshot() {
    return {
      settings: { ...this.settings },
      geometry: JSON.parse(JSON.stringify(this.geometry)),
      retouchOps: this.retouchOps.map((o) => ({ ...o })),
      masks: Object.fromEntries(Object.entries(this.masks).map(([k, c]) =>
        [k, c.getContext('2d').getImageData(0, 0, c.width, c.height)])),
    };
  }

  remember() {
    this.history.push(this.snapshot());
    if (this.history.length > 12) this.history.shift();
    this.future = []; this.dirty = true; this.buttons();
  }

  buttons() {
    const q = (s) => this.dialog.querySelector(s);
    q('[data-action="undo"]').disabled = !this.history.length || this.busy;
    q('[data-action="redo"]').disabled = !this.future.length || this.busy;
    // Converting an untouched photograph (a raw file to JPEG, say) is a use in
    // itself, so both exports wait only for the editor to be ready.
    q('[data-action="save"]').disabled = !this.ready || this.busy;
    q('[data-action="download"]').disabled = !this.ready || this.busy;
  }

  restore(redo = false) {
    if (!this.ready || this.busy) return;
    const from = redo ? this.future : this.history, to = redo ? this.history : this.future;
    if (!from.length) return;
    to.push(this.snapshot());
    const state = from.pop();
    const geometryChanged = JSON.stringify(state.geometry) !== JSON.stringify(this.geometry)
      || state.retouchOps.length !== this.retouchOps.length;
    this.settings = state.settings;
    this.geometry = state.geometry;
    this.retouchOps = state.retouchOps;
    if (geometryChanged) this.buildStage();
    for (const [k, data] of Object.entries(state.masks)) {
      if (!this.masks[k]) continue;
      if (data.width === this.masks[k].width && data.height === this.masks[k].height) {
        this.masks[k].getContext('2d').putImageData(data, 0, 0);
      }
    }
    this.syncSettings();
    this.dirty = true; this.buttons(); this.updateSelections();
    this.editedPreview(); this.render();
  }

  syncSettings() {
    const q = (s) => this.dialog.querySelector(s);
    this.dialog.querySelectorAll('[data-setting]').forEach((input) => {
      input.value = this.settings[input.dataset.setting];
      q(`[data-value="${input.dataset.setting}"]`).value = input.value;
    });
    q('#pe-hair-color').value = this.settings.hairColor;
    q('#pe-straighten').value = this.geometry.straighten;
    q('[data-value="straighten"]').value = `${Number(this.geometry.straighten).toFixed(1)}°`;
    q('#pe-retouch-count').textContent = this.retouchOps.length
      ? `${this.retouchOps.length} spot${this.retouchOps.length === 1 ? '' : 's'} retouched.`
      : 'Nothing retouched yet.';
  }

  /* -- panels ------------------------------------------------------------ */

  selectPanel(panel) {
    this.panel = panel;
    const painting = !!PAINTED[panel];
    if (painting) this.tool = panel;
    this.dialog.querySelectorAll('[data-area]').forEach((el) => {
      el.hidden = el.dataset.area !== (panel === 'dodge' || panel === 'burn' || panel === 'soften' ? 'brush' : panel);
    });
    this.dialog.querySelector('#pe-selection').hidden = !painting;
    const autoButton = this.dialog.querySelector('[data-action="auto-select"]');
    autoButton.hidden = !['skin', 'hair'].includes(panel);
    autoButton.textContent = panel === 'hair' ? 'Suggest hair selection' : 'Find the face for me';
    this.dialog.querySelectorAll('.pe-tools button').forEach((el) => {
      const key = el.dataset.tool || el.dataset.panel;
      el.setAttribute('aria-pressed', String(key === panel
        || (key === 'brush' && ['dodge', 'burn', 'soften'].includes(panel))));
    });
    this.dialog.querySelectorAll('[data-area="brush"] [data-tool]').forEach((el) =>
      el.setAttribute('aria-pressed', String(el.dataset.tool === this.tool)));
    this.dialog.querySelector('#pe-crop').hidden = panel !== 'crop';
    this.overlay.style.pointerEvents = (painting || panel === 'retouch') ? '' : 'none';
    this.dialog.querySelector('aside').scrollTop = 0;
    if (panel === 'crop') this.drawCrop();
    this.showMask();
  }

  updateSelections() {
    this.selected = {};
    for (const [area, mask] of Object.entries(this.masks)) {
      const pixels = mask.getContext('2d').getImageData(0, 0, mask.width, mask.height).data;
      let any = false;
      for (let i = 3; i < pixels.length; i += 4) { if (pixels[i] > 0) { any = true; break; } }
      this.selected[area] = any;
      const field = this.dialog.querySelector(`[data-area="${area}"]`);
      if (field) field.disabled = !any;
      const hint = this.dialog.querySelector(`[data-area-hint="${area}"]`);
      if (hint) {
        hint.textContent = any ? `Adjustments apply only to your ${area} selection.`
          : (area === 'skin'
            ? 'Paint skin on the photo, or press "Find the face for me".'
            : `Paint ${area} on the photo to enable these adjustments.`);
      }
    }
    const label = { skin: 'Skin', hair: 'Hair', dodge: 'Lighten', burn: 'Darken', soften: 'Soften' }[this.tool] || this.tool;
    const state = this.dialog.querySelector('#pe-selection-state');
    if (state) {
      state.textContent = this.selected[this.tool]
        ? `${label} area ready. Use a preset or adjust its sliders below.`
        : (this.tool === 'skin'
          ? 'No skin area yet. Drag over the photo, or press "Find the face for me".'
          : `No ${label.toLowerCase()} area yet. Drag over the photo to select it.`);
    }
  }

  /* -- automatic selection ----------------------------------------------- */

  async autoSelect() {
    if (!['skin', 'hair'].includes(this.tool)) {
      this.status('Automatic selection only knows faces. Paint this one by hand.');
      return;
    }
    const area = this.tool;
    const button = this.dialog.querySelector('[data-action="auto-select"]');
    button.disabled = true;
    button.textContent = 'Looking…';
    this.status('Looking for the face in this photograph…');
    try {
      const response = await fetch(`/api/asset/${this.item.id}/portrait-masks`, { method: 'POST' });
      if (response.status === 401) reportUnauthorized();
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || `Could not look (${response.status}).`);
      if (!data.masks || !data.masks[area]) {
        this.status(data.faces ? 'Found a face, but could not separate that area. Paint it by hand.'
          : 'No face found in this photograph. Paint the area by hand.');
        return;
      }
      const image = new Image();
      image.src = `data:image/png;base64,${data.masks[area]}`;
      await image.decode();
      this.remember();
      // The proposal is a starting point, drawn as a selection the person can
      // erase, extend, or clear entirely. Nothing is applied by finding it.
      const ctx = this.masks[area].getContext('2d');
      const scratch = canvas(this.preview.width, this.preview.height);
      const sctx = scratch.getContext('2d');
      sctx.drawImage(image, 0, 0, scratch.width, scratch.height);
      const proposal = sctx.getImageData(0, 0, scratch.width, scratch.height);
      const target = ctx.getImageData(0, 0, scratch.width, scratch.height);
      for (let i = 0; i < proposal.data.length; i += 4) {
        const strength = proposal.data[i];          // greyscale PNG: any channel
        target.data[i] = area === 'skin' ? 255 : 100;
        target.data[i + 1] = 180; target.data[i + 2] = 255;
        target.data[i + 3] = Math.max(target.data[i + 3], strength);
      }
      ctx.putImageData(target, 0, 0);
      this.dialog.querySelector('#pe-overlay').checked = true;
      this.updateSelections(); this.showMask(); this.render();
      this.status(`Suggested ${area} selection. Check it, paint to extend it, or erase what it got wrong.`);
    } catch (error) {
      this.status(error.message);
    } finally {
      button.disabled = false;
      button.textContent = this.panel === 'hair' ? 'Suggest hair selection' : 'Find the face for me';
    }
  }

  /* -- comparison -------------------------------------------------------- */

  editedPreview() {
    this.comparing = false;
    this.setSplit(false);
    const compare = this.dialog.querySelector('[data-action="compare"]');
    compare.setAttribute('aria-pressed', 'false');
    compare.textContent = 'Show original';
    this.dialog.querySelector('#pe-preview-state').textContent = 'Edited preview';
    this.overlay.style.visibility = ''; this.showMask(); this.paintPreview();
  }

  setSplit(enabled) {
    this.split = enabled;
    for (const selector of ['#pe-divider', '.pe-compare-labels', '.pe-comparison']) {
      this.dialog.querySelector(selector).hidden = !enabled;
    }
    this.dialog.querySelector('[data-action="split"]').setAttribute('aria-pressed', String(enabled));
    this.overlay.style.visibility = enabled ? 'hidden' : '';
  }

  /* -- crop -------------------------------------------------------------- */

  drawCrop() {
    const box = this.geometry.crop || { x: 0, y: 0, w: 1, h: 1 };
    const el = this.dialog.querySelector('#pe-crop');
    el.style.left = `${box.x * 100}%`; el.style.top = `${box.y * 100}%`;
    el.style.width = `${box.w * 100}%`; el.style.height = `${box.h * 100}%`;
  }

  setCrop(box) {
    const ratio = ASPECTS[this.geometry.aspect];
    let { x, y, w, h } = box;
    if (ratio) {
      // Honour the chosen shape by shrinking the longer side, never by
      // growing past the frame.
      const frame = this.preview.width / this.preview.height;
      const want = ratio / frame;
      if (w / h > want) w = h * want; else h = w / want;
    }
    w = clamp01(w); h = clamp01(h);
    x = Math.max(0, Math.min(1 - w, x)); y = Math.max(0, Math.min(1 - h, y));
    this.geometry.crop = (x === 0 && y === 0 && w === 1 && h === 1) ? null : { x, y, w, h };
    this.drawCrop();
  }

  /* -- wiring ------------------------------------------------------------ */

  wire() {
    const q = (s) => this.dialog.querySelector(s);
    q('[data-action="undo"]').onclick = () => this.restore();
    q('[data-action="redo"]').onclick = () => this.restore(true);
    q('[data-action="save"]').onclick = () => this.save();
    q('[data-action="download"]').onclick = () => this.download();
    const format = q('#pe-format'), quality = q('#pe-quality');
    try {
      const saved = localStorage.getItem('ninaivu.export-format');
      if ([...format.options].some((o) => o.value === saved)) format.value = saved;
    } catch { /* private mode */ }
    const showFormat = () => {
      q('#pe-quality-field').hidden = format.value === 'image/png';
      q('#pe-quality-out').textContent = quality.value;
      try { localStorage.setItem('ninaivu.export-format', format.value); } catch { /* private mode */ }
    };
    format.onchange = quality.oninput = showFormat;
    showFormat();
    q('[data-action="reset"]').onclick = () => {
      this.remember();
      this.settings = defaults(); this.geometry = { crop: null, straighten: 0, quarter: 0, aspect: 'free' };
      this.retouchOps = [];
      this.buildStage();
      this.syncSettings(); this.updateSelections(); this.editedPreview(); this.render();
      this.status('Back to the photograph as it was.');
    };
    q('[data-action="compare"]').onclick = () => {
      this.setSplit(false);
      this.comparing = !this.comparing;
      q('[data-action="compare"]').setAttribute('aria-pressed', String(this.comparing));
      q('[data-action="compare"]').textContent = this.comparing ? 'Show edited' : 'Show original';
      q('#pe-preview-state').textContent = this.comparing ? 'Original • edits hidden' : 'Edited preview';
      this.overlay.style.visibility = this.comparing ? 'hidden' : ''; this.paintPreview();
    };
    q('[data-action="split"]').onclick = () => {
      const enabled = !this.split; this.editedPreview(); this.setSplit(enabled);
      q('#pe-preview-state').textContent = enabled ? 'Before / after' : 'Edited preview';
      this.paintPreview();
    };
    q('#pe-split-position').oninput = () => this.paintPreview();
    const divider = q('#pe-divider');
    divider.onpointerdown = (e) => { divider.setPointerCapture(e.pointerId); e.preventDefault(); };
    divider.onpointermove = (e) => {
      if (!divider.hasPointerCapture(e.pointerId)) return;
      const rect = this.photo.getBoundingClientRect();
      q('#pe-split-position').value = String(Math.max(0, Math.min(100, (e.clientX - rect.left) / rect.width * 100)));
      this.paintPreview();
    };
    q('#pe-zoom').onchange = () => this.fit();

    this.dialog.querySelectorAll('[data-panel]').forEach((button) => {
      button.onclick = () => {
        const panel = button.dataset.panel;
        this.selectPanel(panel === 'brush' ? (['dodge', 'burn', 'soften'].includes(this.tool) ? this.tool : 'dodge') : panel);
        this.editedPreview();
        this.status({
          light: 'Light adjustments apply to the whole photograph.',
          colour: 'Colour adjustments apply to the whole photograph.',
          detail: 'Detail adjustments apply to the whole photograph.',
          crop: 'Drag a corner to crop. Rotate and straighten are below.',
          retouch: 'Click a blemish to heal it, or a red eye to drain it.',
          brush: 'Pick Lighten, Darken or Soften, then paint where it should apply.',
        }[panel] || '');
      };
    });
    this.dialog.querySelectorAll('[data-tool]').forEach((button) => {
      button.onclick = () => {
        this.selectPanel(button.dataset.tool); this.editedPreview(); this.updateSelections();
      };
    });

    // crop
    this.dialog.querySelectorAll('[data-turn]').forEach((button) => {
      button.onclick = () => {
        this.remember();
        this.geometry.quarter = ((this.geometry.quarter + Number(button.dataset.turn)) % 360 + 360) % 360;
        this.geometry.crop = null;
        this.buildStage(); this.editedPreview(); this.render();
      };
    });
    this.dialog.querySelectorAll('[data-aspect]').forEach((button) => {
      button.onclick = () => {
        this.remember();
        this.geometry.aspect = button.dataset.aspect;
        this.dialog.querySelectorAll('[data-aspect]').forEach((b) =>
          b.setAttribute('aria-pressed', String(b === button)));
        const box = this.geometry.crop || { x: 0.05, y: 0.05, w: 0.9, h: 0.9 };
        this.setCrop(box); this.buildStage(true); this.editedPreview(); this.render();
      };
    });
    q('[data-action="crop-reset"]').onclick = () => {
      this.remember(); this.geometry.crop = null; this.geometry.straighten = 0;
      this.buildStage(); this.syncSettings(); this.editedPreview(); this.render();
    };
    let straightening = false;
    q('#pe-straighten').oninput = (e) => {
      if (!straightening) { this.remember(); straightening = true; }
      this.geometry.straighten = Number(e.target.value);
      q('[data-value="straighten"]').value = `${this.geometry.straighten.toFixed(1)}°`;
    };
    q('#pe-straighten').onchange = () => {
      straightening = false; this.buildStage(true); this.editedPreview(); this.render();
    };
    const crop = q('#pe-crop');
    crop.querySelectorAll('[data-handle]').forEach((handle) => {
      handle.onpointerdown = (e) => {
        e.preventDefault(); e.stopPropagation();
        handle.setPointerCapture(e.pointerId);
        this.remember();
        const move = (ev) => {
          const rect = this.photo.getBoundingClientRect();
          const px = clamp01((ev.clientX - rect.left) / rect.width);
          const py = clamp01((ev.clientY - rect.top) / rect.height);
          const box = this.geometry.crop || { x: 0, y: 0, w: 1, h: 1 };
          let { x, y, w, h } = box;
          const dir = handle.dataset.handle;
          if (dir.includes('w')) { w = x + w - px; x = px; }
          if (dir.includes('e')) { w = px - x; }
          if (dir.includes('n')) { h = y + h - py; y = py; }
          if (dir.includes('s')) { h = py - y; }
          if (w > 0.05 && h > 0.05) this.setCrop({ x, y, w, h });
        };
        handle.onpointermove = move;
        const done = () => {
          handle.onpointermove = null;
          this.buildStage(true); this.editedPreview(); this.render();
        };
        handle.onpointerup = done; handle.onpointercancel = done;
      };
    });

    // retouch
    this.dialog.querySelectorAll('[data-retouch]').forEach((button) => {
      button.onclick = () => {
        this.retouchMode = button.dataset.retouch;
        this.dialog.querySelectorAll('[data-retouch]').forEach((b) =>
          b.setAttribute('aria-pressed', String(b === button)));
      };
    });
    this.retouchMode = 'heal';
    q('[data-action="undo-spot"]').onclick = () => {
      if (!this.retouchOps.length) { this.status('No spots to undo.'); return; }
      this.remember(); this.retouchOps.pop();
      this.buildStage(true); this.syncSettings(); this.editedPreview(); this.render();
    };

    // selections
    q('[data-action="auto-select"]').onclick = () => this.autoSelect();
    q('#pe-overlay').onchange = () => this.showMask();
    q('[data-action="clear"]').onclick = () => {
      this.remember();
      this.masks[this.tool].getContext('2d').clearRect(0, 0, this.preview.width, this.preview.height);
      this.updateSelections(); this.editedPreview(); this.render();
    };
    this.dialog.querySelectorAll('[data-preset]').forEach((button) => {
      button.onclick = async () => {
        this.remember();
        let values = {
          skin: { redness: 25, smooth: 35 },
          hair: { hairDensity: 45, hairRoots: 20 },
        }[button.dataset.preset] || {};
        if (button.dataset.preset === 'auto') {
          // Ask what *this* photograph needs. The fixed numbers below are the
          // fallback for an older server or a file the server cannot read —
          // a reasonable guess for an average photograph, which is exactly
          // what asking is meant to avoid.
          values = { exposure: 6, contrast: 10, shadows: 18, highlights: -14, clarity: 12, saturation: 8 };
          try {
            const response = await fetch(`/api/asset/${this.item.id}/enhance`);
            if (response.ok) {
              const body = await response.json();
              if (body.settings && Object.keys(body.settings).length) values = body.settings;
            }
          } catch (err) { /* keep the fallback */ }
        }
        Object.assign(this.settings, values);
        this.syncSettings(); q('#pe-overlay').checked = false; this.editedPreview(); this.render();
      };
    });
    this.dialog.querySelectorAll('[data-setting]').forEach((input) => {
      let started = false;
      input.oninput = () => {
        if (!started) { this.remember(); started = true; }
        this.settings[input.dataset.setting] = Number(input.value);
        q(`[data-value="${input.dataset.setting}"]`).value = input.value;
        q('#pe-overlay').checked = false; this.editedPreview(); this.render();
      };
      input.onchange = () => { started = false; };
    });
    q('#pe-hair-color').onchange = (e) => {
      this.remember(); this.settings.hairColor = e.target.value;
      if (!this.settings.hairAmount) this.settings.hairAmount = 45;
      this.syncSettings(); q('#pe-overlay').checked = false; this.editedPreview(); this.render();
    };

    // painting and clicking on the picture
    this.overlay.onpointerdown = (e) => {
      if (this.busy || e.button !== 0) return;
      e.preventDefault();
      const p = this.point(e);
      if (this.panel === 'retouch') { this.retouchAt(p); return; }
      if (!PAINTED[this.panel]) return;
      this.editedPreview(); this.remember();
      this.overlay.setPointerCapture(e.pointerId); this.stroke = e.pointerId;
      this.last = p;
      const i = (Math.floor(p.y) * this.preview.width + Math.floor(p.x)) * 4;
      this.sample = this.original.data.slice(i, i + 3);
      this.brush(p);
    };
    this.overlay.onpointermove = (e) => {
      if (this.stroke !== e.pointerId) return;
      const p = this.point(e), dx = p.x - this.last.x, dy = p.y - this.last.y;
      const steps = Math.max(1, Math.ceil(Math.hypot(dx, dy) / 3));
      for (let i = 1; i <= steps; i++) this.brush({ x: this.last.x + dx * i / steps, y: this.last.y + dy * i / steps });
      this.last = p;
    };
    const finish = (e) => {
      if (this.stroke === e.pointerId) {
        this.stroke = null; this.showMask(); this.updateSelections();
        this.status(this.dialog.querySelector('#pe-selection-state').textContent); this.render();
      }
    };
    this.overlay.onpointerup = finish; this.overlay.onpointercancel = finish;
  }

  retouchAt(p) {
    this.remember();
    const radius = Number(this.dialog.querySelector('#pe-spot').value) / 2 / this.preview.width;
    const op = {
      kind: this.retouchMode,
      x: p.x / this.preview.width, y: p.y / this.preview.height, r: radius,
    };
    if (op.kind === 'heal') op.donor = this.findDonor(op.x, op.y, op.r);
    this.retouchOps.push(op);
    this.buildStage(true); this.syncSettings(); this.editedPreview(); this.render();
    this.status(op.kind === 'heal' ? 'Blemish healed from the skin beside it.' : 'Red drained, catchlight kept.');
  }

  point(e) {
    const r = this.overlay.getBoundingClientRect();
    return {
      x: Math.max(0, Math.min(this.preview.width - 1, (e.clientX - r.left) * this.preview.width / r.width)),
      y: Math.max(0, Math.min(this.preview.height - 1, (e.clientY - r.top) * this.preview.height / r.height)),
    };
  }

  fit() {
    const viewport = this.dialog.querySelector('.pe-viewport');
    if (!viewport || !this.preview) return;
    // Follows .pe-viewport's padding, doubled, plus room for the drop shadow.
    const padding = innerWidth <= 760 ? 16 : 28;
    const width = Math.max(1, Math.min(viewport.clientWidth - padding,
      (viewport.clientHeight - padding) * this.preview.width / this.preview.height));
    this.dialog.querySelector('.pe-picture').style.width =
      `${width * Number(this.dialog.querySelector('#pe-zoom').value)}px`;
  }

  brush(p) {
    const r = Number(this.dialog.querySelector('#pe-size').value) / 2;
    const erase = this.dialog.querySelector('#pe-erase').checked;
    const edge = this.dialog.querySelector('#pe-edge').checked;
    const ctx = this.masks[this.tool].getContext('2d');
    const w = this.preview.width, h = this.preview.height;
    const x0 = Math.max(0, Math.floor(p.x - r)), y0 = Math.max(0, Math.floor(p.y - r));
    const bw = Math.min(w - x0, Math.ceil(r * 2 + 1)), bh = Math.min(h - y0, Math.ceil(r * 2 + 1));
    if (bw < 1 || bh < 1) return;
    const data = ctx.getImageData(x0, y0, bw, bh);
    for (let y = 0; y < bh; y++) {
      for (let x = 0; x < bw; x++) {
        const dist = Math.hypot(x + x0 - p.x, y + y0 - p.y) / r;
        if (dist >= 1) continue;
        const j = ((y + y0) * w + x + x0) * 4, i = (y * bw + x) * 4;
        const diff = Math.hypot(...[0, 1, 2].map((c) => this.original.data[j + c] - this.sample[c]));
        const a = Math.min(1, (1 - dist) * 3) * (edge && !erase ? Math.exp(-diff * diff / 5000) : 1);
        data.data[i] = this.tool === 'skin' ? 255 : 100;
        data.data[i + 1] = 180; data.data[i + 2] = 255;
        data.data[i + 3] = erase ? data.data[i + 3] * (1 - a) : Math.max(data.data[i + 3], a * 255);
      }
    }
    ctx.putImageData(data, x0, y0); this.showMask();
  }

  showMask() {
    const ctx = this.overlay.getContext('2d');
    ctx.clearRect(0, 0, this.overlay.width, this.overlay.height);
    if (PAINTED[this.panel] && (this.stroke != null || this.dialog.querySelector('#pe-overlay').checked)) {
      ctx.globalAlpha = 0.4; ctx.drawImage(this.masks[this.tool], 0, 0); ctx.globalAlpha = 1;
    }
  }

  /* -- rendering --------------------------------------------------------- */

  /** Masks travel as one byte per pixel, not four: the colour is decoration. */
  maskBytes(mask) {
    const data = mask.getContext('2d').getImageData(0, 0, mask.width, mask.height).data;
    const alpha = new Uint8Array(data.length / 4);
    for (let i = 0, j = 3; i < alpha.length; i++, j += 4) alpha[i] = data[j];
    return alpha;
  }

  process(c) {
    if (this.workerFailed) return Promise.reject(new Error('Photo processing is unavailable. Close the editor and reload the app.'));
    const id = ++this.serial;
    const pixels = c.getContext('2d').getImageData(0, 0, c.width, c.height).data.buffer;
    const masks = {}; const transfer = [pixels];
    for (const name of MASKS) {
      if (!this.selected?.[name]) continue;      // an empty mask is not worth sending
      const bytes = this.maskBytes(this.masks[name]);
      masks[name] = bytes.buffer; transfer.push(bytes.buffer);
    }
    return new Promise((resolve, reject) => {
      this.jobs.set(id, { resolve, reject });
      this.worker.postMessage({
        id, pixels, width: c.width, height: c.height, masks,
        mw: this.preview.width, mh: this.preview.height, settings: this.settings,
      }, transfer);
    });
  }

  async render() {
    this.pending = true; if (this.rendering) return;
    this.rendering = true;
    try {
      while (this.pending && this.dialog.open) {
        this.pending = false;
        const bytes = await this.process(this.preview);
        this.result = new ImageData(new Uint8ClampedArray(bytes), this.preview.width, this.preview.height);
        this.paintPreview();
      }
    } catch (error) { this.status(error.message); } finally { this.rendering = false; }
  }

  paintPreview() {
    const ctx = this.photo.getContext('2d');
    ctx.putImageData(this.comparing ? this.original : (this.result || this.original), 0, 0);
    if (this.split) {
      const percent = Number(this.dialog.querySelector('#pe-split-position').value);
      const width = this.photo.width * percent / 100;
      ctx.save(); ctx.beginPath(); ctx.rect(0, 0, width, this.photo.height); ctx.clip();
      ctx.drawImage(this.preview, 0, 0); ctx.restore();
      this.dialog.querySelector('#pe-divider').style.left = `${percent}%`;
    }
  }

  /** The finished picture, encoded as the person chose. */
  async render() {
    // The stage already carries the geometry and every retouch, applied at
    // full resolution; only the slider work is left for the worker.
    const bytes = await this.process(this.stage);
    const type = this.dialog.querySelector('#pe-format').value;
    const quality = Number(this.dialog.querySelector('#pe-quality').value) / 100;
    let out = canvas(this.stage.width, this.stage.height);
    out.getContext('2d').putImageData(new ImageData(new Uint8ClampedArray(bytes), out.width, out.height), 0, 0);
    if (type === 'image/jpeg') {
      // JPEG has no transparency: without a ground it would turn black.
      const flat = canvas(out.width, out.height), ctx = flat.getContext('2d');
      ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, flat.width, flat.height); ctx.drawImage(out, 0, 0);
      out = flat;
    }
    const blob = await new Promise((resolve) => out.toBlob(resolve, type, quality));
    if (!blob || blob.type !== type) throw new Error('This browser cannot export that format. Choose another.');
    return { blob, type };
  }

  async save() {
    if (this.busy) return;
    this.busy = true; this.buttons(); this.dialog.querySelector('aside').inert = true;
    this.status('Rendering and saving your new copy…');
    try {
      const { blob, type } = await this.render();
      const response = await fetch(`/api/asset/${this.item.id}/edited-copy`, {
        method: 'POST', headers: { 'Content-Type': type }, body: blob,
      });
      if (response.status === 401) reportUnauthorized();
      const data = await response.json().catch(() => ({ error: `Save failed (${response.status}).` }));
      if (!response.ok) throw new Error(data.error || 'Could not save the copy.');
      this.dirty = false; this.busy = false; this.close(); this.saved(data);
    } catch (error) {
      this.status(error.message); this.busy = false;
      this.dialog.querySelector('aside').inert = false; this.buttons();
    }
  }

  /** The same picture to this device, leaving the library alone. */
  async download() {
    if (this.busy) return;
    this.busy = true; this.buttons(); this.dialog.querySelector('aside').inert = true;
    this.status('Rendering your download…');
    try {
      const { blob, type } = await this.render();
      const ext = { 'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp' }[type];
      const stem = String(this.item.filename || 'photo').replace(/\.[^./\\]+$/, '');
      const link = Object.assign(document.createElement('a'), {
        href: URL.createObjectURL(blob), download: `${stem}-edited.${ext}`,
      });
      document.body.append(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(link.href), 10_000);
      this.status(`Downloaded ${link.download}.`);
    } catch (error) {
      this.status(error.message);
    } finally {
      this.busy = false; this.dialog.querySelector('aside').inert = false; this.buttons();
    }
  }

  close() {
    if (this.busy) { this.status('Please wait while your copy is saving.'); return; }
    if (this.dirty && !window.confirm('Discard your unsaved photo edits?')) return;
    this.worker?.terminate();
    for (const job of this.jobs.values()) job.reject(new Error('Editor closed.'));
    this.jobs.clear(); this.resizeObserver?.disconnect();
    this.dialog.close(); this.dialog.remove(); this.previousFocus?.focus();
  }
}
