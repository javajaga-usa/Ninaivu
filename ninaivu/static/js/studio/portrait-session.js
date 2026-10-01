/**
 * Everything a retouching tool needs to know about the people in one photograph.
 *
 * The session asks the server where the faces are, keeps what comes back, keeps
 * the brush work done on top of it, keeps what has been asked of each person, and
 * hands the worker the maps and the settings in the shape it reads them.
 *
 * It does not draw a panel and it does not do any pixel work; the two studios that
 * use it — the Photo Studio and the AI studio — each show it their own way. What
 * they have in common is here:
 *
 *   - The maps belong to the *photograph*, not to what is on screen. They are
 *     made once, and drawn through whatever crop, turn and straightening the
 *     photograph has at the moment, which is what `geometry` is for. For a
 *     studio that does not crop, geometry is the identity.
 *   - The faces the server found are *offered*, never imposed. Nothing changes
 *     until a slider moves or somebody presses "Improve faces", and then only as
 *     each person's own settings, which can be undone like any other edit.
 */

import { reportUnauthorized } from '../api.js';
import { Layer } from './layer.js';
import { blank, copy, applySuggestions } from './portrait-params.mjs';

export const PAINT = ['skinAdd', 'skinErase', 'hairAdd', 'hairErase', 'hairGrow'];

/** The longest edge of the picture sent to be looked at. Faces are small in a group photograph; this is where they stop being findable. */
const SEND_LONGEST = 2400;

/** The longest edge of the layers that hold what has been painted by hand. */
const LAYER_LONGEST = 1600;

const canvasOf = (w, h) => Object.assign(document.createElement('canvas'), { width: w, height: h });

export class PortraitSession {
  /**
   * @param source    the photograph the maps describe: a canvas or an image, before any crop or turn
   * @param geometry  how it sits in what is shown —
   *                    `key()`                        a string that changes when the crop, turn or straightening does
   *                    `size()`                       `{ w, h }`, the shown picture at its own resolution
   *                    `matrix(pw, ph, outW, outH)`   a DOMMatrix taking a picture laid over the photograph, `pw × ph` pixels,
   *                                                   to an `outW × outH` view of the shown picture
   * @param onChange  called with no arguments whenever what a worker would be sent has changed
   */
  constructor({ source, geometry, onChange = () => {} }) {
    this.source = source;
    this.geometry = geometry || identity(source);
    this.onChange = onChange;
    this.state = 'idle';           // idle | finding | ready | none | unavailable | failed
    this.message = '';
    this.analysis = null;
    this.images = null;
    this.portrait = blank();
    this.generation = 0;           // which analysis the maps are from
    const scale = Math.min(1, LAYER_LONGEST / Math.max(source.width, source.height));
    this.layerWidth = Math.max(1, Math.round(source.width * scale));
    this.layerHeight = Math.max(1, Math.round(source.height * scale));
    this.paint = Object.fromEntries(PAINT.map((name) => [name, new Layer(this.layerWidth, this.layerHeight)]));
    this.cache = null;
    this.selected = 'all';         // which person the panels are showing: 'all', or a face's id
    this.watchers = new Set();
  }

  /** Be told when anything a panel shows has changed. Returns the function that stops it. */
  watch(fn) {
    this.watchers.add(fn);
    return () => this.watchers.delete(fn);
  }

  touch() {
    for (const fn of this.watchers) fn();
  }

  // -- asking --------------------------------------------------------------

  /** Find the faces. Safe to call again; it does nothing while it is already looking or has already looked. */
  async analyse(force = false) {
    if (this.state === 'finding') return this.pending;
    if (this.state === 'ready' && !force) return undefined;
    this.state = 'finding';
    this.message = '';
    this.touch();
    this.pending = this.look().finally(() => { this.pending = null; });
    return this.pending;
  }

  async look() {
    try {
      const longest = Math.max(this.source.width, this.source.height);
      const scale = Math.min(1, SEND_LONGEST / longest);
      const sent = canvasOf(Math.max(1, Math.round(this.source.width * scale)), Math.max(1, Math.round(this.source.height * scale)));
      sent.getContext('2d').drawImage(this.source, 0, 0, sent.width, sent.height);
      const blob = await new Promise((resolve) => sent.toBlob(resolve, 'image/jpeg', 0.92));
      if (!blob) throw new Error('unreadable');
      const response = await fetch('/api/portrait/analyse', { method: 'POST', headers: { 'Content-Type': 'image/jpeg' }, body: blob });
      if (response.status === 401) reportUnauthorized();
      const body = await response.json().catch(() => ({}));
      if (response.status === 503 || response.status === 501) {
        this.state = 'unavailable';
        this.message = body.error || '';
        this.needs = body.needs || null;
      } else if (!response.ok) {
        this.state = 'failed';
        this.message = body.error || '';
      } else if (!body.maps || !(body.faces || []).some((f) => f.usable)) {
        this.state = 'none';
        this.message = body.reason || '';
        this.analysis = body;
      } else {
        const images = {};
        for (const name of ['a', 'b', 'c', 'ids']) {
          if (!body.maps[name]) continue;                // an answer from before `c` existed has no body map; colour then stays on the face
          const image = new Image();
          image.src = `data:image/png;base64,${body.maps[name]}`;
          await image.decode();
          images[name] = image;
        }
        this.images = images;                            // only once all of them are in: half an answer is no answer
        this.analysis = body;
        this.generation++;
        this.state = 'ready';
      }
    } catch (error) {
      this.state = 'failed';
      this.message = '';
    }
    this.cache = null;
    this.onChange();
    this.touch();
  }

  // -- the people ----------------------------------------------------------

  /**
   * The faces as the shown picture has them: each with its own axes in that
   * picture's terms (`frame`, in fractions of its width and height, `d` as a
   * fraction of its width), and whether it is inside the crop at all.
   */
  faces() {
    if (!this.analysis || !this.images) return [];
    const { w: sw, h: sh } = this.geometry.size();
    const aw = this.images.a.width, ah = this.images.a.height;
    const M = this.geometry.matrix(aw, ah, sw, sh);
    const out = [];
    for (const face of this.analysis.faces || []) {
      if (!face.usable || !face.frame) continue;
      const f = face.frame;
      const centre = M.transformPoint({ x: f.x * aw, y: f.y * ah });
      const along = M.transformPoint({ x: f.x * aw + f.cos * f.d * aw, y: f.y * ah + f.sin * f.d * aw });
      const length = Math.hypot(along.x - centre.x, along.y - centre.y) || 1;
      const frame = { x: centre.x / sw, y: centre.y / sh, d: length / sw, cos: (along.x - centre.x) / length, sin: (along.y - centre.y) / length };
      const inside = centre.x >= 0 && centre.x <= sw && centre.y >= 0 && centre.y <= sh;
      out.push({ ...face, frame, inside });
    }
    return out;
  }

  /** A square picture of one face, cut from *from* (a canvas showing the picture at any size). */
  thumb(id, from, size = 44) {
    const face = this.faces().find((f) => f.id === id);
    const out = canvasOf(size, size);
    if (!face) return out;
    const side = 3.1 * face.frame.d * from.width;
    const cx = face.frame.x * from.width, cy = face.frame.y * from.height + 0.3 * face.frame.d * from.width;
    out.getContext('2d').drawImage(from, cx - side / 2, cy - side / 2, side, side, 0, 0, size, size);
    return out;
  }

  // -- what has been asked -------------------------------------------------

  /** Give each face what was measured to stand out about it, and only that. Returns how many faces were given something. */
  improve() {
    const given = applySuggestions(this.portrait, this.faces().filter((f) => f.inside));
    this.bump();
    this.touch();
    return given;
  }

  /** Something the worker reads has changed. */
  bump() {
    this.cache = null;
    this.onChange();
  }

  /** History: what to keep, and how to put it back. */
  snapshot() {
    return {
      portrait: copy(this.portrait),
      paint: Object.fromEntries(PAINT.map((name) => [name, this.paint[name].snapshot()])),
    };
  }

  restore(state) {
    if (!state) return;
    this.portrait = copy(state.portrait);
    for (const name of PAINT) this.paint[name].restore(state.paint[name]);
    this.bump();
    this.touch();
  }

  reset() {
    this.portrait = blank();
    for (const name of PAINT) this.paint[name].clear();
    this.bump();
    this.touch();
  }

  get hasPaint() {
    return PAINT.some((name) => this.paint[name].any());
  }

  // -- what the worker reads -----------------------------------------------

  /**
   * The maps, the paint and the faces' axes for a picture of `outW × outH` — the
   * shown picture at that size — in the form the worker takes. Made once for each
   * size and kept until something changes.
   */
  payload(outW, outH) {
    const key = `${this.generation}|${this.geometry.key()}|${outW}x${outH}|${PAINT.map((n) => this.paint[n].version).join('.')}`;
    if (this.cache && this.cache.key === key) return this.cache;
    const maps = { width: outW, height: outH, a: null, b: null, c: null, ids: null, paint: {} };
    if (this.images) {
      const through = (image, smooth) => {
        const canvas = canvasOf(outW, outH);
        const ctx = canvas.getContext('2d', { willReadFrequently: true });
        ctx.imageSmoothingEnabled = smooth;
        ctx.imageSmoothingQuality = 'high';
        ctx.setTransform(this.geometry.matrix(image.width, image.height, outW, outH));
        ctx.drawImage(image, 0, 0);
        return ctx.getImageData(0, 0, outW, outH).data;
      };
      maps.a = through(this.images.a, true);
      maps.b = through(this.images.b, true);
      maps.c = this.images.c ? through(this.images.c, true) : new Uint8ClampedArray(outW * outH * 4);
      maps.ids = through(this.images.ids, false);
    } else {
      const none = new Uint8ClampedArray(outW * outH * 4);
      maps.a = none; maps.b = none; maps.c = none; maps.ids = none;
    }
    for (const name of PAINT) {
      const layer = this.paint[name];
      if (!layer.any()) continue;
      const canvas = canvasOf(outW, outH);
      const ctx = canvas.getContext('2d', { willReadFrequently: true });
      ctx.imageSmoothingQuality = 'high';
      ctx.setTransform(this.geometry.matrix(layer.w, layer.h, outW, outH));
      ctx.drawImage(layer.canvas(), 0, 0);
      const rgba = ctx.getImageData(0, 0, outW, outH).data;
      const bytes = new Uint8Array(outW * outH);
      for (let i = 0, j = 3; i < bytes.length; i++, j += 4) bytes[i] = rgba[j];
      maps.paint[name] = bytes;
    }
    const frames = {};
    for (const face of this.faces()) frames[face.id] = face.frame;
    this.cache = { key, maps, frames };
    return this.cache;
  }

  /** A translucent picture of what the tools would reach, for laying over the photograph. `selected` is a face's id, or 'all'. */
  overlay(outW, outH, selected = 'all') {
    const { maps } = this.payload(outW, outH);
    const image = new ImageData(outW, outH);
    const out = image.data, { a, b, ids, paint: brushed } = maps;
    const paint = new Float32Array(4);            // what has been laid over this pixel so far: r, g, b, alpha
    const pick = selected === 'all' ? 0 : Number(selected);
    for (let i = 0, k = 0; i < outW * outH; i++, k += 4) {
      const owner = ids[k], hairOwner = ids[k + 1];
      const dim = pick && owner !== pick ? 0.35 : 1, hairDim = pick && hairOwner !== pick ? 0.35 : 1;
      let skin = a[k] / 255, hair = b[k] / 255, beard = b[k + 1] / 255;
      if (brushed.skinAdd) skin = Math.max(skin, brushed.skinAdd[i] / 255);
      if (brushed.skinErase) skin *= 1 - brushed.skinErase[i] / 255;
      if (brushed.hairAdd) hair = Math.max(hair, brushed.hairAdd[i] / 255);
      if (brushed.hairErase) { hair *= 1 - brushed.hairErase[i] / 255; beard *= 1 - brushed.hairErase[i] / 255; }
      const grown = brushed.hairGrow ? (brushed.hairGrow[i] / 255) * (brushed.hairErase ? 1 - brushed.hairErase[i] / 255 : 1) : 0;
      const marks = (a[k + 2] / 255) * (owner ? 1 : 0);
      paint.fill(0);
      if (skin > 0.02) over(paint, 40, 205, 178, 0.5 * skin * dim);
      if (hair > 0.02) over(paint, 150, 120, 255, 0.55 * hair * hairDim);
      if (grown > 0.02) over(paint, 90, 225, 120, 0.6 * grown);
      if (beard > 0.02) over(paint, 255, 170, 70, 0.5 * beard * hairDim);
      if (marks > 0.05) over(paint, 255, 214, 64, 0.85 * marks * dim);
      const r = paint[0], g = paint[1], bl = paint[2], alpha = paint[3];
      out[k] = r; out[k + 1] = g; out[k + 2] = bl; out[k + 3] = Math.round(alpha * 255);
    }
    return image;
  }
}

/** For a studio that does not crop or turn: the photograph is the picture. */
function identity(source) {
  return {
    key: () => 'identity',
    size: () => ({ w: source.width, h: source.height }),
    matrix: (pw, ph, outW, outH) => new DOMMatrix().scale(outW / pw, outH / ph),
  };
}

/** Lay one translucent colour over what is in *paint* (r, g, b, alpha), in place. */
function over(paint, r, g, b, alpha) {
  const total = paint[3] + alpha * (1 - paint[3]);
  if (total <= 0) return;
  const weight = alpha * (1 - paint[3]);
  paint[0] = (paint[0] * paint[3] + r * weight) / total;
  paint[1] = (paint[1] * paint[3] + g * weight) / total;
  paint[2] = (paint[2] * paint[3] + b * weight) / total;
  paint[3] = total;
}
