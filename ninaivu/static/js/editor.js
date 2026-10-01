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
 *   a worker. The one request that touches the server asks where the faces in
 *   the photograph are, and it is answered by the model the household already
 *   downloaded, on the household's own computer.
 *
 *   *Nothing is invented.* Every adjustment is arithmetic on pixels that were
 *   photographed. Softening takes a blemish and keeps the skin; cover grey
 *   darkens hair that is there and does not grow hair that is not.
 *
 * The edit is a stack, not a sequence of destructive steps:
 *
 *   source → geometry (crop, straighten, quarter turns) → retouch ops
 *          → stage → worker(settings, brushes, faces) → what you see
 *
 * `stage` is rebuilt from the source whenever geometry or retouching changes,
 * which is what makes undo cheap: history stores the *description* (settings,
 * geometry, the list of retouch ops, what has been painted, what has been asked
 * of each face) and never a bitmap of the result.
 *
 * Everything painted, and everything known about the faces, belongs to the
 * photograph and not to the window: it is kept in the source's own space and
 * drawn through whatever crop and turn the photograph has, so turning or
 * cropping afterwards carries the work with the picture.
 */

import { reportUnauthorized } from './api.js';
import * as i18n from './i18n.js';
import { initPalette, studioCommands } from './palette.js';
import { Layer } from './studio/layer.js';
import { PortraitSession } from './studio/portrait-session.js';
import { PortraitPanel } from './studio/portrait-panel.js';
import { isNeutral } from './studio/portrait-params.mjs';
import { naturalTone } from './studio/natural-tone.mjs';
import { BANDS, CHANNELS, LOOKS, RANGES, bandKey, blank, cleanCurve } from './studio/recipe.mjs';
import { curveTable, develop } from './studio/develop.mjs';

/**
 * Everything the sliders say: the engine's recipe (studio/recipe.mjs) — light,
 * colour, curves, detail and a look — and the strengths of the painted brushes,
 * which are this editor's own.
 */
const defaults = () => ({ ...blank(), dodgeAmount: 0, burnAmount: 0, softenAmount: 0 });

/** What each band of the colour mixer is called, and the colour its swatch is drawn in. */
const BAND_NAMES = {
  red: [i18n.key('Red'), '#d8343a'], orange: [i18n.key('Orange'), '#e5822b'], yellow: [i18n.key('Yellow'), '#e3c62c'],
  green: [i18n.key('Green'), '#3fa34d'], aqua: [i18n.key('Aqua'), '#2fb7b3'], blue: [i18n.key('Blue'), '#3569d6'],
  purple: [i18n.key('Purple'), '#8446c9'], magenta: [i18n.key('Magenta'), '#cc3ea7'],
};
/** The long edges an export may be reduced to; 0 is the photograph's own size. */
const EXPORT_SIZES = [0, 3840, 2048, 1080];

// Skin and hair are not sliders of the photograph's but of the people in it, and
// live in the portrait session (studio/portrait-session.js); these are the
// brushes, which are.
const BRUSHES = ['dodge', 'burn', 'soften'];
const PAINTED = { dodge: 'dodge', burn: 'burn', soften: 'soften' };
/** The longest edge of the layers that hold what has been painted: the photograph's, not the window's. */
const LAYER_LONGEST = 1600;
/** The longest edge of the face maps sent to a render of the whole photograph. */
const MAPS_LONGEST = 2400;
const ASPECTS = { free: null, '1:1': 1, '4:5': 0.8, '3:2': 1.5, '2:3': 2 / 3, '16:9': 16 / 9 };

const canvas = (w, h) => Object.assign(document.createElement('canvas'), { width: w, height: h });
const clamp01 = (v) => Math.max(0, Math.min(1, v));

/**
 * A picture made smaller for export, halving at most at each step: one big jump
 * drops pixels instead of averaging them, which is what makes a reduced
 * photograph shimmer on fine patterns like silk and hair.
 */
function shrinkTo(picture, edge) {
  const scale = edge / Math.max(picture.width, picture.height);
  const goalW = Math.max(1, Math.round(picture.width * scale)), goalH = Math.max(1, Math.round(picture.height * scale));
  let from = picture;
  while (from.width !== goalW || from.height !== goalH) {
    const w = Math.max(goalW, Math.round(from.width / 2)), h = Math.max(goalH, Math.round(from.height / 2));
    const to = canvas(w, h), ctx = to.getContext('2d');
    ctx.imageSmoothingQuality = 'high';
    ctx.drawImage(from, 0, 0, w, h);
    from = to;
  }
  return from;
}

export class PhotoEditor {
  constructor(item, saved) {
    this.item = item; this.saved = saved; this.settings = defaults();
    this.geometry = { crop: null, straighten: 0, quarter: 0, aspect: 'free', flipH: false, flipV: false };
    this.retouchOps = [];
    this.history = []; this.future = []; this.tool = 'skin'; this.panel = 'light';
    this.serial = 0; this.jobs = new Map(); this.dirty = false; this.busy = false;
    this.selected = {};                 // which brushes have anything painted (see updateSelections)
    this.paintKind = null;              // 'skin' or 'hair' while painting by hand in a portrait panel
    this.paintMode = null;              // 'add' or 'erase' with it
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
      curve: '<rect x="3.2" y="3.2" width="17.6" height="17.6" rx="2.6"/><path d="M5.6 18.2C9.4 17.8 9.8 6.4 18.4 5.8"/>',
      looks: '<rect x="3.2" y="3.2" width="7.6" height="7.6" rx="1.8"/><rect x="13.2" y="3.2" width="7.6" height="7.6" rx="1.8"/><rect x="3.2" y="13.2" width="7.6" height="7.6" rx="1.8"/><rect x="13.2" y="13.2" width="7.6" height="7.6" rx="1.8"/>',
    }[name] || '';
    return `<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">${d}</svg>`;
  }

  /** A rail entry. `kind` is 'panel' or 'tool' — the wiring reads both. */
  rail(kind, key, label) {
    return `<button type="button" data-${kind}="${key}" aria-pressed="false">`
      + `${this.icon(key)}<span>${i18n.t(label)}</span></button>`;
  }

  /** A slider for one setting. Its range and resting value are the recipe's, where the recipe has one. */
  slider(key, label, min, max, swatch = '') {
    const range = RANGES[key] || [min ?? -100, max ?? 100, 0];
    const lo = min ?? range[0], hi = max ?? range[1], value = range[2];
    const dot = swatch ? `<i class="pe-swatch" style="background:${swatch}"></i>` : '';
    return `<label class="pe-slider"><span>${dot}${i18n.t(label)}</span><output data-value="${key}">${this.shown(key, value)}</output>`
      + `<input type="range" data-setting="${key}" min="${lo}" max="${hi}" value="${value}"></label>`;
  }

  /** A setting as a person reads it: a radius in pixels, a hue in degrees, everything else as it is. */
  shown(key, value) {
    if (key === 'sharpenRadius') return (Number(value) / 10).toFixed(1);
    if (key === 'toneHiHue' || key === 'toneShHue') return `${value}°`;
    return String(value);
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
            ${r('panel', 'light', i18n.key('Light'))}${r('panel', 'colour', i18n.key('Colour'))}${r('panel', 'curve', i18n.key('Curve'))}
            ${r('panel', 'detail', i18n.key('Detail'))}${r('panel', 'looks', i18n.key('Looks'))}
            <p class="pe-group">Frame</p>
            ${r('panel', 'crop', i18n.key('Crop'))}
            <p class="pe-group">Portrait</p>
            ${r('panel', 'retouch', i18n.key('Retouch'))}${r('tool', 'skin', i18n.key('Skin'))}${r('tool', 'hair', i18n.key('Hair'))}
            <p class="pe-group">Paint</p>
            ${r('panel', 'brush', i18n.key('Brush'))}
          </nav>

          <div class="pe-controls">
            <fieldset data-area="light"><legend>${i18n.t('Light')}</legend>
              <canvas class="pe-histogram" width="512" height="120" role="img" aria-label="${i18n.t('Histogram')}"></canvas>
              <p class="pe-lede">${i18n.t('Balance the exposure across the whole photograph.')}</p>
              <button data-preset="auto">✦ ${i18n.t('Balance this photograph')}</button>
              ${s('exposure', i18n.key('Exposure'))}${s('contrast', i18n.key('Contrast'))}
              ${s('highlights', i18n.key('Highlights'))}${s('shadows', i18n.key('Shadows'))}
              ${s('whites', i18n.key('Whites'))}${s('blacks', i18n.key('Blacks'))}
              ${s('dehaze', i18n.key('Dehaze'))}
              ${this.note(i18n.key('Highlights and Shadows judge each part of the photograph by its surroundings, so a face in front of a bright window can be opened up without a halo round it or a grey sky above it. Exposure works like a camera: 40 is one stop.'))}</fieldset>

            <fieldset data-area="colour" hidden><legend>${i18n.t('Colour')}</legend>
              <p class="pe-lede">${i18n.t('White balance, vibrance and the colour mixer, across the whole photograph.')}</p>
              <button type="button" data-action="natural-tone">✦ ${i18n.t('Natural skin tone')}</button>
              ${s('warmth', i18n.key('Warmth'))}${s('tint', i18n.key('Tint (green ↔ magenta)'))}
              ${s('vibrance', i18n.key('Vibrance'))}${s('saturation', i18n.key('Saturation'))}
              <p class="pe-label">${i18n.t('Colour mixer')}</p>
              <div class="pe-seg">
                <button type="button" data-mixer="hue" aria-pressed="true">${i18n.t('Hue')}</button>
                <button type="button" data-mixer="sat" aria-pressed="false">${i18n.t('Saturation')}</button>
                <button type="button" data-mixer="lum" aria-pressed="false">${i18n.t('Luminance')}</button></div>
              ${['hue', 'sat', 'lum'].map((kind) => `<div data-mixer-group="${kind}"${kind === 'hue' ? '' : ' hidden'}>${
                BANDS.map((band) => this.slider(bandKey(kind, band), BAND_NAMES[band][0], undefined, undefined, BAND_NAMES[band][1])).join('')}</div>`).join('')}
              <label class="pe-check"><input type="checkbox" data-toggle="mono"> ${i18n.t('Black and white')}</label>
              <p class="pe-hint" id="pe-mono-hint" hidden>${i18n.t('In black and white, the mixer’s Luminance sliders set how light each colour turns out.')}</p>
              <details class="pe-note pe-toning"><summary>${i18n.t('Split toning')}</summary><div>
                ${s('toneHiHue', i18n.key('Highlight hue'))}${s('toneHiSat', i18n.key('Highlight strength'))}
                ${s('toneShHue', i18n.key('Shadow hue'))}${s('toneShSat', i18n.key('Shadow strength'))}
                ${s('toneBalance', i18n.key('Balance'))}</div></details>
              ${this.note(i18n.key('Vibrance lifts muted colour and leaves colour that is already strong alone, and it knows where skin sits on the colour wheel, so skin keeps its own colour. Saturation moves every colour alike.'))}</fieldset>

            <fieldset data-area="curve" hidden><legend>${i18n.t('Curve')}</legend>
              <p class="pe-lede">${i18n.t('Drag the line to reshape the tones. Click to add a point; drag a point off the square to remove it.')}</p>
              <div class="pe-seg">${CHANNELS.map((c) => `<button type="button" data-channel="${c}" aria-pressed="${c === 'rgb'}">${
                i18n.t({ rgb: i18n.key('RGB'), r: i18n.key('Red'), g: i18n.key('Green'), b: i18n.key('Blue') }[c])}</button>`).join('')}</div>
              <div class="pe-curve"><canvas id="pe-curve" width="512" height="512" role="img"
                aria-label="${i18n.t('Tone curve, drawn over the histogram')}"></canvas></div>
              <button type="button" data-action="curve-reset" class="pe-quiet">${i18n.t('Reset this curve')}</button>
              ${this.note(i18n.key('The bottom left is black and the top right is white. Lift the line to lighten those tones, lower it to darken them; an S shape adds contrast. The shape behind the line is the histogram of the edited photograph.'))}</fieldset>

            <fieldset data-area="detail" hidden><legend>${i18n.t('Detail')}</legend>
              <p class="pe-lede">${i18n.t('Definition, sharpness and noise, across the whole photograph.')}</p>
              ${s('clarity', i18n.key('Clarity'))}
              <p class="pe-label">${i18n.t('Sharpening')}</p>
              ${s('sharpen', i18n.key('Amount'))}${s('sharpenRadius', i18n.key('Radius'))}${s('sharpenMasking', i18n.key('Masking'))}
              <p class="pe-label">${i18n.t('Noise reduction')}</p>
              ${s('noise', i18n.key('Luminance noise'))}${s('colourNoise', i18n.key('Colour noise'))}
              <p class="pe-label">${i18n.t('Effects')}</p>
              ${s('grain', i18n.key('Grain'))}${s('grainSize', i18n.key('Grain size'))}
              ${s('vignette', i18n.key('Vignette'))}${s('vignetteMidpoint', i18n.key('Vignette midpoint'))}${s('vignetteFeather', i18n.key('Vignette feather'))}
              ${this.note(i18n.key('Sharpening works on lightness only, so edges never grow coloured fringes; Masking keeps it to real edges and off smooth skin and sky. Sharpening and noise are sized to the full photograph, so judge them at 200%.'))}</fieldset>

            <fieldset data-area="looks" hidden><legend>${i18n.t('Looks')}</legend>
              <p class="pe-lede">${i18n.t('Start from a look, then adjust. Your own sliders are kept, and the look is added on top.')}</p>
              <div class="pe-looks">
                <button type="button" data-look="" aria-pressed="true"><canvas width="96" height="72" aria-hidden="true"></canvas><span>${i18n.t('None')}</span></button>
                ${LOOKS.map((look) => `<button type="button" data-look="${look.id}" aria-pressed="false" title="${i18n.t(look.about)}">`
                  + `<canvas width="96" height="72" aria-hidden="true"></canvas><span>${i18n.t(look.name)}</span></button>`).join('')}</div>
              <p class="pe-hint" id="pe-look-about"></p>
              ${s('lookAmount', i18n.key('Amount of the look'))}
              ${this.note(i18n.key('Every look is tuned for family photographs. None of them lightens skin or drains its colour, and the colourful ones add colour through vibrance, which leaves skin nearly as it was.'))}</fieldset>

            <fieldset data-area="crop" hidden><legend>Crop &amp; straighten</legend>
              <p class="pe-lede">Drag a corner on the photograph to set the crop.</p>
              <div class="pe-row"><button data-turn="-90">↺ Rotate left</button><button data-turn="90">↻ Rotate right</button></div>
              <div class="pe-row"><button type="button" data-flip="h" aria-pressed="false">⇋ ${i18n.t('Flip across')}</button><button type="button" data-flip="v" aria-pressed="false">⇵ ${i18n.t('Flip upside down')}</button></div>
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
              <p id="pe-selection-state" class="pe-hint" aria-live="polite">No area painted yet.</p>
              ${this.note(`<label class="pe-check"><input type="checkbox" id="pe-erase"> Erase selection</label>
                <label class="pe-check"><input type="checkbox" id="pe-edge" checked> Colour-aware brush</label>
                <label class="pe-slider"><span>Brush size</span><input id="pe-size" type="range" min="5" max="180" value="55"></label>
                <label class="pe-check"><input type="checkbox" id="pe-overlay"> Show painted area</label>
                <button data-action="clear" class="pe-quiet">Clear this selection</button>
                <p class="pe-hint">Colour-aware mode follows colours near the start of each stroke. Turn it off to select freely.</p>`, 'Brush settings')}</fieldset>

            <fieldset data-area="skin" hidden><legend>${i18n.t('Skin')}</legend>
              <p class="pe-lede">${i18n.t('Each person is judged against their own skin. Choose a person, or everyone, and move a slider.')}</p>
              <div data-portrait="skin"></div></fieldset>

            <fieldset data-area="hair" hidden><legend>${i18n.t('Hair')}</legend>
              <p class="pe-lede">${i18n.t('Strands, grey and colour, for the hair that can be told from what is behind it. Paint the rest in.')}</p>
              <div data-portrait="hair"></div></fieldset>

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
          <label class="pe-export-field"><span>${i18n.t('Size')}</span>
            <select id="pe-export-size" aria-label="${i18n.t('Export size')}">${EXPORT_SIZES.map((edge) => `<option value="${edge}">${
              edge ? i18n.t('{pixels} px', { pixels: edge }) : i18n.t('Full size')}</option>`).join('')}</select></label>
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
      this.worker = new Worker(new URL('./editor-worker.js', import.meta.url), { type: 'module' });
      this.worker.onmessage = ({ data }) => {
        const job = this.jobs.get(data.id);
        this.jobs.delete(data.id);
        if (data.error) job?.reject(new Error(data.error)); else job?.resolve(data);
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
      // What is painted and what is known about the faces belongs to the
      // photograph, so it is made before the stage and outlives every rebuild of it.
      this.session = new PortraitSession({
        source: this.source,
        geometry: { key: () => JSON.stringify(this.geometry), size: () => ({ w: this.stage.width, h: this.stage.height }),
                    matrix: (pw, ph, outW, outH) => this.placement(pw, ph, outW, outH) },
        onChange: () => this.faceWork(),
      });
      this.layers = Object.fromEntries(BRUSHES.map((name) => [name, new Layer(this.session.layerWidth, this.session.layerHeight)]));
      this.buildStage();
      this.mountPortrait();
      this.dialog.querySelector('#pe-size-note').textContent =
        `${this.source.width} × ${this.source.height} • ${i18n.t('Saved to the same folder')} • ${scale < 1 ? i18n.t('Reduced to 24 MP for editing') : i18n.t('Original resolution')}. ${i18n.t('Camera, lens, date and place are kept in the copy.')}`;
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

  /** Where the photograph sits once it has been turned and cropped: the numbers both the stage and everything painted on it are built from. */
  stageFrame() {
    const { quarter, straighten, crop } = this.geometry;
    const turned = quarter % 180 !== 0;
    const sw = turned ? this.source.height : this.source.width;
    const sh = turned ? this.source.width : this.source.height;
    // Straightening rotates the frame, so the usable rectangle shrinks; the
    // alternative is blank corners, which is never what anybody wanted.
    const radians = Math.abs(straighten * Math.PI / 180);
    const shrink = radians ? (Math.cos(radians) + Math.sin(radians) * sh / sw) : 1;
    const box = crop || { x: 0, y: 0, w: 1, h: 1 };
    return {
      sw, sh, shrink, degrees: quarter + straighten, straighten,
      fx: this.geometry.flipH ? -1 : 1, fy: this.geometry.flipV ? -1 : 1,
      x: Math.round(sw * box.x), y: Math.round(sh * box.y),
      cw: Math.max(8, Math.round(sw * box.w)), ch: Math.max(8, Math.round(sh * box.h)),
    };
  }

  /**
   * The transform that takes a picture laid over the photograph — the photograph
   * itself, or a layer of paint, or a map of the faces, `pw × ph` pixels — onto
   * the stage, drawn `outW × outH`. It is the same turn, straighten and crop the
   * stage is built with, which is what keeps paint on the face it was put on.
   */
  placement(pw, ph, outW, outH) {
    const f = this.stageFrame();
    return new DOMMatrix()
      .scale(outW / f.cw, outH / f.ch)
      .translate(-f.x, -f.y)
      .translate(f.sw / 2, f.sh / 2)
      .scale(f.shrink)
      .rotate(f.degrees)
      .scale(f.fx, f.fy)
      .translate(-this.source.width / 2, -this.source.height / 2)
      .scale(this.source.width / pw, this.source.height / ph);
  }

  /** Rebuild everything downstream of the source: geometry, then retouch ops. */
  buildStage() {
    const f = this.stageFrame();
    const full = canvas(f.sw, f.sh);
    const fctx = full.getContext('2d');
    fctx.save();
    fctx.translate(f.sw / 2, f.sh / 2);
    fctx.rotate(f.degrees * Math.PI / 180);
    if (f.straighten) fctx.scale(f.shrink, f.shrink);
    // A flip is of the photograph itself, so it is the innermost step: a turn after it turns the flipped picture.
    fctx.scale(f.fx, f.fy);
    fctx.drawImage(this.source, -this.source.width / 2, -this.source.height / 2);
    fctx.restore();

    this.stage = canvas(f.cw, f.ch);
    this.stage.getContext('2d').drawImage(full, f.x, f.y, f.cw, f.ch, 0, 0, f.cw, f.ch);

    for (const op of this.retouchOps) this.applyOp(this.stage, op, 1);

    const ratio = Math.min(1, 1400 / Math.max(f.cw, f.ch));
    this.preview = canvas(Math.max(1, Math.round(f.cw * ratio)), Math.max(1, Math.round(f.ch * ratio)));
    this.preview.getContext('2d').drawImage(this.stage, 0, 0, this.preview.width, this.preview.height);
    this.original = this.preview.getContext('2d').getImageData(0, 0, this.preview.width, this.preview.height);

    for (const c of [this.photo, this.overlay]) { c.width = this.preview.width; c.height = this.preview.height; }
    this.dialog.querySelector('.pe-picture').style.aspectRatio = `${this.preview.width}/${this.preview.height}`;
    this.result = null;
    this.fit?.();
    this.session?.touch();            // the people have moved in the frame: their faces, and what is drawn over them, follow
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
      settings: structuredClone(this.settings),
      geometry: JSON.parse(JSON.stringify(this.geometry)),
      retouchOps: this.retouchOps.map((o) => ({ ...o })),
      layers: Object.fromEntries(BRUSHES.map((name) => [name, this.layers[name].snapshot()])),
      portrait: this.session.snapshot(),
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
    for (const name of BRUSHES) this.layers[name].restore(state.layers[name]);
    this.session.restore(state.portrait);
    this.syncSettings();
    this.dirty = true; this.buttons(); this.updateSelections();
    this.editedPreview(); this.render();
  }

  syncSettings() {
    const q = (s) => this.dialog.querySelector(s);
    this.dialog.querySelectorAll('[data-setting]').forEach((input) => {
      input.value = this.settings[input.dataset.setting];
      q(`[data-value="${input.dataset.setting}"]`).value = this.shown(input.dataset.setting, input.value);
    });
    q('[data-toggle="mono"]').checked = this.settings.mono >= 1;
    q('#pe-mono-hint').hidden = !(this.settings.mono >= 1);
    this.dialog.querySelectorAll('[data-look]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.look === this.settings.look)));
    const look = LOOKS.find((l) => l.id === this.settings.look);
    q('#pe-look-about').textContent = look ? i18n.t(look.about) : '';
    this.dialog.querySelectorAll('[data-flip]').forEach((b) =>
      b.setAttribute('aria-pressed', String(!!this.geometry[b.dataset.flip === 'h' ? 'flipH' : 'flipV'])));
    this.drawCurve();
    q('#pe-straighten').value = this.geometry.straighten;
    q('[data-value="straighten"]').value = `${Number(this.geometry.straighten).toFixed(1)}°`;
    q('#pe-retouch-count').textContent = this.retouchOps.length
      ? `${this.retouchOps.length} spot${this.retouchOps.length === 1 ? '' : 's'} retouched.`
      : 'Nothing retouched yet.';
  }

  /* -- panels ------------------------------------------------------------ */

  selectPanel(panel) {
    this.panel = panel;
    // What is shown over the picture belongs to the panel that asked for it.
    this.showSelection = false;
    this.dialog.querySelector('#pe-overlay').checked = false;
    this.dialog.querySelectorAll('[data-pp="show"]').forEach((box) => { box.checked = false; });
    const painting = !!PAINTED[panel];
    if (painting) this.tool = panel;
    const portrait = panel === 'skin' || panel === 'hair';
    // Painting by hand belongs to the portrait panel it was started in.
    if (this.paintKind && this.paintKind !== panel) this.panels?.[this.paintKind]?.refine(null);
    this.dialog.querySelectorAll('[data-area]').forEach((el) => {
      el.hidden = el.dataset.area !== (painting ? 'brush' : panel);
    });
    this.dialog.querySelector('#pe-selection').hidden = !painting;
    this.dialog.querySelectorAll('.pe-tools button').forEach((el) => {
      const key = el.dataset.tool || el.dataset.panel;
      el.setAttribute('aria-pressed', String(key === panel || (key === 'brush' && painting)));
    });
    this.dialog.querySelectorAll('[data-area="brush"] [data-tool]').forEach((el) =>
      el.setAttribute('aria-pressed', String(el.dataset.tool === this.tool)));
    this.dialog.querySelector('#pe-crop').hidden = panel !== 'crop';
    if (panel === 'looks') this.drawLooks();
    if (panel === 'curve') this.drawCurve();
    this.overlay.style.pointerEvents = (painting || panel === 'retouch' || (portrait && this.paintKind === panel)) ? '' : 'none';
    this.dialog.querySelector('aside').scrollTop = 0;
    if (panel === 'crop') this.drawCrop();
    // The first time somebody opens Skin or Hair the photograph is looked at for
    // faces, so that what they find there is already about the people in it.
    if (portrait && this.session.state === 'idle') this.session.analyse();
    this.showMask();
  }

  /**
   * Turn the colour cast of the whole photograph back, judging by the skin in it.
   * It moves the two colour sliders and nothing else, and only as far as the edge
   * of what skin of any complexion looks like: see studio/natural-tone.mjs.
   */
  async naturalTone() {
    if (this.busy) return;
    this.status(i18n.t('Looking at the skin in this photograph…'));
    await this.session.analyse();
    if (this.session.state === 'unavailable') { this.status(i18n.t('Finding faces needs the face model. Download it under AI models.')); return; }
    const faces = this.session.faces().filter((face) => face.inside);
    if (this.session.state !== 'ready' || !faces.length) { this.status(i18n.t('No face was found to judge the colour by.')); return; }
    const found = naturalTone(faces);
    if (!found) { this.status(i18n.t('The skin already looks natural. Nothing was changed.')); return; }
    this.remember();
    Object.assign(this.settings, found);
    this.syncSettings(); this.editedPreview(); this.render();
    this.status(i18n.t('Turned the colour cast on the skin back towards natural: warmth {warmth}, tint {tint}.', found));
  }

  /** Put the Skin and Hair panels into the page. */
  mountPortrait() {
    const host = {
      picture: () => this.preview,
      remember: () => this.remember(),
      changed: () => { this.dirty = true; this.editedPreview(); this.render(); },
      say: (text) => this.status(text),
      select: (on) => { this.showSelection = on; this.showMask(); },
      refine: (mode, kind) => this.startRefining(mode, kind),
    };
    this.panels = {};
    for (const kind of ['skin', 'hair']) {
      this.panels[kind] = new PortraitPanel({ session: this.session, kind, host });
      this.dialog.querySelector(`[data-portrait="${kind}"]`).append(this.panels[kind].element);
    }
    this.session.watch(() => this.showMask());      // a different person chosen, or a different answer: the overlay follows
  }

  /** Start or stop painting skin or hair by hand. */
  startRefining(mode, kind) {
    this.paintMode = mode;
    this.paintKind = mode ? kind : null;
    if (mode) this.showSelection = true;
    this.overlay.style.pointerEvents = (mode || this.panel === 'retouch' || PAINTED[this.panel]) ? '' : 'none';
    if (mode) {
      this.status(mode === 'add'
        ? i18n.t('Paint over what the tools missed.')
        : i18n.t('Paint over what the tools took by mistake.'));
    }
    this.showMask();
  }

  /** Is there a brush at work, and on which layer? */
  paintTarget() {
    if (this.paintKind && this.paintMode && (this.panel === this.paintKind)) {
      const brush = this.panels[this.paintKind].brush;
      const add = this.session.paint[`${this.paintKind}Add`], erase = this.session.paint[`${this.paintKind}Erase`];
      return this.paintMode === 'add'
        ? { layer: add, opposite: erase, erase: false, size: brush.size, edge: brush.edge }
        : { layer: erase, opposite: add, erase: false, size: brush.size, edge: false };
    }
    if (PAINTED[this.panel]) {
      const erase = this.dialog.querySelector('#pe-erase').checked;
      return { layer: this.layers[this.tool], opposite: null, erase, size: Number(this.dialog.querySelector('#pe-size').value),
               edge: this.dialog.querySelector('#pe-edge').checked };
    }
    return null;
  }

  updateSelections() {
    this.selected = Object.fromEntries(BRUSHES.map((name) => [name, this.layers[name].any()]));
    const label = { dodge: i18n.t('Lighten'), burn: i18n.t('Darken'), soften: i18n.t('Soften') }[this.tool];
    const state = this.dialog.querySelector('#pe-selection-state');
    if (state && label) {
      state.textContent = this.selected[this.tool]
        ? i18n.t('{tool} area ready. Set its strength on the Brush panel.', { tool: label })
        : i18n.t('No {tool} area yet. Drag over the photo to paint it.', { tool: label.toLowerCase() });
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
      this.settings = defaults(); this.geometry = { crop: null, straighten: 0, quarter: 0, aspect: 'free', flipH: false, flipV: false };
      this.retouchOps = [];
      for (const name of BRUSHES) this.layers[name].clear();
      this.session.reset();
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
          curve: i18n.t('Drag the line to reshape the tones.'),
          looks: i18n.t('Pick a look, then set how much of it you want.'),
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
        this.setCrop(box); this.buildStage(); this.editedPreview(); this.render();
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
      straightening = false; this.buildStage(); this.editedPreview(); this.render();
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
          this.buildStage(); this.editedPreview(); this.render();
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
      this.buildStage(); this.syncSettings(); this.editedPreview(); this.render();
    };

    q('[data-action="natural-tone"]').onclick = () => this.naturalTone();

    // brushes
    q('#pe-overlay').onchange = () => { this.showSelection = q('#pe-overlay').checked; this.showMask(); };
    q('[data-action="clear"]').onclick = () => {
      this.remember();
      this.layers[this.tool].clear();
      this.updateSelections(); this.editedPreview(); this.render();
    };
    this.dialog.querySelectorAll('[data-preset]').forEach((button) => {
      button.onclick = async () => {
        this.remember();
        let values = {};
        if (button.dataset.preset === 'auto') {
          // Ask what *this* photograph needs. The fixed numbers below are the
          // fallback for an older server or a file the server cannot read —
          // a reasonable guess for an average photograph, which is exactly
          // what asking is meant to avoid.
          values = { exposure: 4, contrast: 10, shadows: 18, highlights: -14, clarity: 10, vibrance: 10 };
          try {
            const response = await fetch(`/api/asset/${this.item.id}/enhance`);
            if (response.ok) {
              const body = await response.json();
              if (body.settings && Object.keys(body.settings).length) values = body.settings;
            }
          } catch (err) { /* keep the fallback */ }
        }
        Object.assign(this.settings, values);
        this.syncSettings(); this.editedPreview(); this.render();
      };
    });
    this.dialog.querySelectorAll('[data-setting]').forEach((input) => {
      let started = false;
      input.oninput = () => {
        if (!started) { this.remember(); started = true; }
        this.settings[input.dataset.setting] = Number(input.value);
        q(`[data-value="${input.dataset.setting}"]`).value = this.shown(input.dataset.setting, input.value);
        this.editedPreview(); this.render();
      };
      input.onchange = () => { started = false; };
      // A double click puts one slider back where it rests, and nothing else.
      input.ondblclick = () => {
        const resting = defaults()[input.dataset.setting];
        if (this.settings[input.dataset.setting] === resting) return;
        this.remember(); this.settings[input.dataset.setting] = resting;
        this.syncSettings(); this.editedPreview(); this.render();
      };
    });

    // colour: the mixer's three views, and black and white
    this.dialog.querySelectorAll('[data-mixer]').forEach((button) => {
      button.onclick = () => {
        this.dialog.querySelectorAll('[data-mixer]').forEach((b) => b.setAttribute('aria-pressed', String(b === button)));
        this.dialog.querySelectorAll('[data-mixer-group]').forEach((g) => { g.hidden = g.dataset.mixerGroup !== button.dataset.mixer; });
      };
    });
    q('[data-toggle="mono"]').onchange = (e) => {
      this.remember(); this.settings.mono = e.target.checked ? 1 : 0;
      this.syncSettings(); this.editedPreview(); this.render();
    };

    // looks
    this.dialog.querySelectorAll('[data-look]').forEach((button) => {
      button.onclick = () => {
        if (this.settings.look === button.dataset.look) return;
        this.remember();
        this.settings.look = button.dataset.look;
        if (button.dataset.look && !this.settings.lookAmount) this.settings.lookAmount = 100;
        this.syncSettings(); this.editedPreview(); this.render();
      };
    });

    // curve
    this.dialog.querySelectorAll('[data-channel]').forEach((button) => {
      button.onclick = () => {
        this.channel = button.dataset.channel;
        this.dialog.querySelectorAll('[data-channel]').forEach((b) => b.setAttribute('aria-pressed', String(b === button)));
        this.drawCurve();
      };
    });
    q('[data-action="curve-reset"]').onclick = () => {
      this.remember(); this.settings.curve[this.channel] = [[0, 0], [1, 1]];
      this.drawCurve(); this.editedPreview(); this.render();
    };
    this.wireCurve(q('#pe-curve'));

    // flips
    this.dialog.querySelectorAll('[data-flip]').forEach((button) => {
      button.onclick = () => {
        this.remember();
        const key = button.dataset.flip === 'h' ? 'flipH' : 'flipV';
        this.geometry[key] = !this.geometry[key];
        this.geometry.crop = null;
        this.buildStage(); this.syncSettings(); this.editedPreview(); this.render();
      };
    });

    // painting and clicking on the picture
    this.overlay.onpointerdown = (e) => {
      if (this.busy || e.button !== 0) return;
      e.preventDefault();
      const p = this.point(e);
      if (this.panel === 'retouch') { this.retouchAt(p); return; }
      const target = this.paintTarget();
      if (!target) return;
      this.editedPreview(); this.remember();
      this.overlay.setPointerCapture(e.pointerId); this.stroke = e.pointerId;
      this.last = p;
      const i = (Math.floor(p.y) * this.preview.width + Math.floor(p.x)) * 4;
      this.sample = this.original.data.slice(i, i + 3);
      this.brush(p, target);
      this.showMask();
    };
    this.overlay.onpointermove = (e) => {
      if (this.stroke !== e.pointerId) return;
      const p = this.point(e), dx = p.x - this.last.x, dy = p.y - this.last.y;
      const target = this.paintTarget();
      const steps = Math.max(1, Math.ceil(Math.hypot(dx, dy) / 3));
      for (let i = 1; i <= steps; i++) this.brush({ x: this.last.x + dx * i / steps, y: this.last.y + dy * i / steps }, target);
      this.last = p;
      this.showMask();                  // once for the whole move: drawing the layer is the dear part
    };
    const finish = (e) => {
      if (this.stroke === e.pointerId) {
        this.stroke = null; this.showMask(); this.updateSelections();
        this.session.bump(); this.session.touch();           // what was painted is now part of what the worker is sent
        const state = this.dialog.querySelector('#pe-selection-state');
        if (!this.paintKind && state) this.status(state.textContent);
        this.render();
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
    this.buildStage(); this.syncSettings(); this.editedPreview(); this.render();
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

  /**
   * One dab. The brush is a circle on the picture, but what it paints into is a
   * layer in the photograph's own space, so the circle is taken there — it is
   * still a circle: a turn, a crop and a straighten only ever scale and rotate —
   * and a colour-aware brush reads the picture's colour back through the same
   * transform.
   */
  brush(p, target) {
    if (!target) return;
    const { layer, opposite, erase, size, edge } = target;
    const forward = this.placement(layer.w, layer.h, this.preview.width, this.preview.height);
    const inverse = forward.inverse();
    const centre = inverse.transformPoint({ x: p.x, y: p.y });
    const radius = size / 2 * Math.hypot(inverse.a, inverse.b);
    let weight = null;
    if (edge && !erase) {
      const data = this.original.data, w = this.preview.width, h = this.preview.height;
      weight = (lx, ly) => {
        const at = forward.transformPoint({ x: lx + 0.5, y: ly + 0.5 });
        const x = Math.floor(at.x), y = Math.floor(at.y);
        if (x < 0 || y < 0 || x >= w || y >= h) return 0;
        const j = (y * w + x) * 4;
        const diff = Math.hypot(data[j] - this.sample[0], data[j + 1] - this.sample[1], data[j + 2] - this.sample[2]);
        return Math.exp(-diff * diff / 5000);
      };
    }
    layer.stamp(centre.x, centre.y, radius, { erase, weight });
    // Painting over something that was painted out, or out of something painted in,
    // takes the opposite away: the last word about a place is the latest.
    if (opposite) opposite.stamp(centre.x, centre.y, radius, { erase: true });
  }

  /**
   * What is laid over the photograph: the area being painted, or what the skin and
   * hair tools would reach. Drawn through the same transform as the picture.
   */
  showMask() {
    if (!this.overlay || !this.preview) return;
    const ctx = this.overlay.getContext('2d');
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, this.overlay.width, this.overlay.height);
    const w = this.preview.width, h = this.preview.height;
    const shown = this.stroke != null || this.showSelection;
    if (PAINTED[this.panel] && shown) {
      const layer = this.layers[this.tool];
      ctx.save();
      ctx.globalAlpha = 0.4;
      ctx.setTransform(this.placement(layer.w, layer.h, w, h));
      ctx.drawImage(layer.canvas(), 0, 0);
      ctx.restore();
    }
    const portrait = this.panel === 'skin' || this.panel === 'hair';
    if (portrait && this.session && (this.showSelection || this.paintKind === this.panel) && this.session.state === 'ready') {
      // A soft picture of where the tools reach does not need every pixel of it.
      const ratio = Math.min(1, 700 / Math.max(w, h));
      const small = this.session.overlay(Math.max(1, Math.round(w * ratio)), Math.max(1, Math.round(h * ratio)), this.session.selected);
      const scratch = canvas(small.width, small.height);
      scratch.getContext('2d').putImageData(small, 0, 0);
      ctx.drawImage(scratch, 0, 0, w, h);
    }
  }

  /* -- histogram, curve and looks ----------------------------------------- */

  /** The colours the drawings below are made in: the editor's own, so light and dark themes both hold. */
  inks() {
    const style = getComputedStyle(this.dialog);
    const read = (name, fallback) => style.getPropertyValue(name).trim() || fallback;
    return { line: read('--pe-line-bright', 'rgba(128,128,128,.4)'), text: read('--pe-text', '#888'),
             muted: read('--pe-dim', '#888'), accent: read('--pe-accent', '#b07d39') };
  }

  /** The histogram of the edited preview: red, green and blue laid over each other, lightness in the ink colour. */
  drawHistograms() {
    const h = this.histogram;
    for (const c of this.dialog.querySelectorAll('.pe-histogram')) {
      const ctx = c.getContext('2d');
      ctx.clearRect(0, 0, c.width, c.height);
      if (!h) continue;
      this.plotHistogram(ctx, c.width, c.height, ['r', 'g', 'b', 'l']);
    }
  }

  plotHistogram(ctx, W, H, channels) {
    const h = this.histogram, ink = this.inks();
    // Scaled to the second-tallest level rather than the tallest, so a patch of
    // pure black or blown sky does not flatten everything else to nothing.
    const peak = Math.max(1, ...channels.map((c) => [...h[c].slice(1, 255)].sort((a, b) => b - a)[1] || 1));
    const colours = { r: 'rgba(220,60,60,.45)', g: 'rgba(60,180,80,.45)', b: 'rgba(70,110,230,.45)', l: ink.muted };
    ctx.save();
    ctx.globalCompositeOperation = 'source-over';
    for (const c of channels) {
      ctx.fillStyle = colours[c];
      ctx.globalAlpha = c === 'l' ? 0.35 : 1;
      ctx.beginPath(); ctx.moveTo(0, H);
      for (let i = 0; i < 256; i++) ctx.lineTo(i / 255 * W, H - Math.min(1, h[c][i] / peak) * H * 0.95);
      ctx.lineTo(W, H); ctx.closePath(); ctx.fill();
    }
    ctx.restore();
  }

  /** The curve being edited, over the histogram of the channel it belongs to. */
  drawCurve() {
    const c = this.dialog?.querySelector('#pe-curve');
    if (!c) return;
    const ctx = c.getContext('2d'), S = c.width, ink = this.inks();
    const channel = this.channel || 'rgb';
    ctx.clearRect(0, 0, S, S);
    if (this.histogram) this.plotHistogram(ctx, S, S, channel === 'rgb' ? ['l'] : [channel]);
    ctx.strokeStyle = ink.line; ctx.lineWidth = 1;
    for (let i = 1; i < 4; i++) {
      ctx.beginPath(); ctx.moveTo(i * S / 4, 0); ctx.lineTo(i * S / 4, S); ctx.moveTo(0, i * S / 4); ctx.lineTo(S, i * S / 4); ctx.stroke();
    }
    ctx.setLineDash([6, 6]); ctx.beginPath(); ctx.moveTo(0, S); ctx.lineTo(S, 0); ctx.stroke(); ctx.setLineDash([]);
    const points = this.settings.curve[channel];
    const table = curveTable(points);
    ctx.strokeStyle = { rgb: ink.text, r: '#d8343a', g: '#3fa34d', b: '#3569d6' }[channel];
    ctx.lineWidth = 3;
    ctx.beginPath();
    for (let i = 0; i < table.length; i += 4) {
      const x = i / (table.length - 1) * S, y = (1 - table[i]) * S;
      if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y);
    }
    ctx.stroke();
    for (const [x, y] of points) {
      ctx.beginPath(); ctx.arc(x * S, (1 - y) * S, 9, 0, Math.PI * 2);
      ctx.fillStyle = ink.accent; ctx.fill();
    }
  }

  /**
   * The curve editor: press on the line to add a point there, drag a point to
   * move it, drag it out of the square to take it away. The two ends stay at the
   * ends and move only up and down.
   */
  wireCurve(c) {
    this.channel = 'rgb';
    const at = (e) => {
      const r = c.getBoundingClientRect();
      return { x: (e.clientX - r.left) / r.width, y: 1 - (e.clientY - r.top) / r.height };
    };
    let dragging = null;
    c.onpointerdown = (e) => {
      if (!this.ready || this.busy || e.button !== 0) return;
      e.preventDefault();
      const p = at(e), points = this.settings.curve[this.channel];
      const reach = 18 / c.getBoundingClientRect().width;
      let index = points.findIndex(([x, y]) => Math.hypot(x - p.x, y - p.y) < reach);
      this.remember();
      if (index < 0) {
        if (points.length >= 16) return;
        const x = clamp01(p.x);
        points.push([x, curveTable(points)[Math.round(x * 1024)]]);
        points.sort((a, b) => a[0] - b[0]);
        index = points.findIndex(([px]) => px === x);
      }
      dragging = index;
      c.setPointerCapture(e.pointerId);
      this.drawCurve(); this.editedPreview(); this.render();
    };
    c.onpointermove = (e) => {
      if (dragging == null) return;
      const p = at(e), points = this.settings.curve[this.channel];
      const last = points.length - 1;
      const outside = p.y < -0.08 || p.y > 1.08 || p.x < -0.08 || p.x > 1.08;
      if (dragging > 0 && dragging < last && outside) {
        points.splice(dragging, 1); dragging = null;
      } else {
        // An end stays an end; a point in the middle stays between its neighbours.
        const x = dragging === 0 ? 0 : dragging === last ? 1
          : Math.min(points[dragging + 1][0] - 0.01, Math.max(points[dragging - 1][0] + 0.01, p.x));
        points[dragging] = [x, clamp01(p.y)];
      }
      this.settings.curve[this.channel] = cleanCurve(points);
      this.drawCurve(); this.render();
    };
    const done = () => { dragging = null; };
    c.onpointerup = done; c.onpointercancel = done;
  }

  /** Small pictures of each look, made from the preview as it now stands, so the choice is a choice between pictures. */
  drawLooks() {
    const buttons = [...this.dialog.querySelectorAll('[data-look] canvas')];
    if (!buttons.length || !this.preview) return;
    const W = buttons[0].width, H = buttons[0].height;
    const thumb = canvas(W, H), ctx = thumb.getContext('2d', { willReadFrequently: true });
    const k = Math.max(W / this.preview.width, H / this.preview.height);
    ctx.drawImage(this.preview, (W - this.preview.width * k) / 2, (H - this.preview.height * k) / 2, this.preview.width * k, this.preview.height * k);
    const base = ctx.getImageData(0, 0, W, H);
    for (const c of buttons) {
      const id = c.closest('[data-look]').dataset.look;
      const recipe = { ...this.settings, look: id, lookAmount: id ? 100 : 0 };
      const out = develop(base.data, W, H, recipe, { fullWidth: this.stage.width });
      c.getContext('2d').putImageData(new ImageData(out, W, H), 0, 0);
    }
  }

  /* -- rendering --------------------------------------------------------- */

  /**
   * A brush's area as one byte a pixel — the colour is decoration — drawn through
   * the same transform the photograph has, at the size of the preview. (The
   * worker stretches it to a full-size export; a brush's edge is soft already.)
   */
  maskBytes(layer) {
    const w = this.preview.width, h = this.preview.height;
    const scratch = canvas(w, h), ctx = scratch.getContext('2d', { willReadFrequently: true });
    ctx.imageSmoothingQuality = 'high';
    ctx.setTransform(this.placement(layer.w, layer.h, w, h));
    ctx.drawImage(layer.canvas(), 0, 0);
    const data = ctx.getImageData(0, 0, w, h).data;
    const alpha = new Uint8Array(w * h);
    for (let i = 0, j = 3; i < alpha.length; i++, j += 4) alpha[i] = data[j];
    return alpha;
  }

  /** What has been asked of the people in the photograph, and the maps that say where they are, for a render of *c*. */
  faceRequest(c) {
    const portrait = this.session.portrait;
    if (isNeutral(portrait)) return null;
    let w = this.preview.width, h = this.preview.height;
    if (c === this.stage) {
      // A render of the whole photograph asks for maps at working size; they are sampled up to it.
      const k = Math.min(1, MAPS_LONGEST / Math.max(c.width, c.height));
      w = Math.max(1, Math.round(c.width * k)); h = Math.max(1, Math.round(c.height * k));
    }
    const { key, maps, frames } = this.session.payload(w, h);
    if (this.mapsSent !== key) {
      this.worker.postMessage({ type: 'maps', key, maps });
      this.mapsSent = key;
    }
    return { params: portrait, frames, mapsKey: key };
  }

  /** Something about the faces changed — the answer came in, or a brush was lifted: show it, if there is anything to show. */
  faceWork() {
    if (!this.ready || this.busy) return;
    if (!isNeutral(this.session.portrait) || this.session.hasPaint) this.render();
  }

  process(c, wantHistogram = false) {
    if (this.workerFailed) return Promise.reject(new Error('Photo processing is unavailable. Close the editor and reload the app.'));
    const id = ++this.serial;
    const pixels = c.getContext('2d').getImageData(0, 0, c.width, c.height).data.buffer;
    const masks = {}; const transfer = [pixels];
    for (const name of BRUSHES) {
      if (!this.selected?.[name]) continue;      // an empty mask is not worth sending
      const bytes = this.maskBytes(this.layers[name]);
      masks[name] = bytes.buffer; transfer.push(bytes.buffer);
    }
    const portrait = this.faceRequest(c);
    return new Promise((resolve, reject) => {
      this.jobs.set(id, { resolve, reject });
      this.worker.postMessage({
        id, pixels, width: c.width, height: c.height, masks, portrait, wantHistogram,
        // Sharpening and noise are sized to the whole photograph, so the preview is told how big that is.
        fullWidth: this.stage.width,
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
        const { pixels, histogram } = await this.process(this.preview, true);
        this.result = new ImageData(new Uint8ClampedArray(pixels), this.preview.width, this.preview.height);
        this.histogram = histogram;
        this.paintPreview(); this.drawHistograms(); this.drawCurve();
        if (this.panel === 'looks') this.drawLooks();          // the small pictures follow the sliders
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

  /** The finished picture, encoded as the person chose. (Not `render`: that is
   *  the live preview's, and two methods of one name left only this one.) */
  async renderExport() {
    // The stage already carries the geometry and every retouch, applied at
    // full resolution; only the slider work is left for the worker.
    const { pixels } = await this.process(this.stage);
    const type = this.dialog.querySelector('#pe-format').value;
    const quality = Number(this.dialog.querySelector('#pe-quality').value) / 100;
    let out = canvas(this.stage.width, this.stage.height);
    out.getContext('2d').putImageData(new ImageData(new Uint8ClampedArray(pixels), out.width, out.height), 0, 0);
    const edge = Number(this.dialog.querySelector('#pe-export-size').value);
    if (edge && Math.max(out.width, out.height) > edge) out = shrinkTo(out, edge);
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
      const { blob, type } = await this.renderExport();
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
      const { blob, type } = await this.renderExport();
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
