/**
 * Where a new hairstyle may be drawn, and what must stay exactly as it is.
 *
 * A hairstyle is drawn by an image model, and an image model that is handed a
 * whole photograph hands back a whole new one: the face it "kept" has been
 * through the model too, and a person's face is the one thing in a family
 * photograph that must not drift. So Sudar does it the other way round. The
 * face analysis the retouch studio already makes says, for each pixel, how much
 * is this person's skin and how much their hair; from that and the face's own
 * axes this module makes two masks:
 *
 *   change  this person's hair, and the room around the head the style needs —
 *           the sides of the shoulders for long hair, above the head for a bun,
 *           the lower face for a beard. Only this is sent to be redrawn.
 *   keep    this person's face, every bindi, sindoor or sacred ash mark, and
 *           every other person's skin and hair. These pixels are put back from
 *           the photograph after the drawing, whatever came back from the server
 *           and whatever was painted by hand.
 *
 * Distances are in eye distances from the middle of the eyes, `u` across the
 * face and `v` down it, as the analysis measures them (ninaivu/media/portrait.py
 * `Frame`): the brows are near v = -0.36, the mouth near 1.0, the chin near 1.9.
 *
 * Nothing here touches the network or the DOM, so the Node tests can hold it to
 * that promise pixel by pixel.
 */

import { blur, resize } from './plane.mjs';

/**
 * The room each kind of style needs beyond the hair that is there, and how much
 * of the face it may reach. `face` is the top of the part of the face that is
 * kept, in v: a fringe falls to the brows, a beard grows below the nose.
 */
export const REACH = {
  head: { face: -1.3 },        // short crop, bob: the hair that is there and around the head
  long: { face: -1.3 },        // long, wavy, curly, braids: down past the shoulders too
  up: { face: -1.3 },          // a bun or plait gathered up: above and behind the head
  fringe: { face: -0.3 },      // across the forehead to the brows
  beard: { face: null },       // the lower face; only the eyes, nose and above are kept
};

const SKIN = 76;      // 0.3 of the map's 255: this much skin is skin
const HAIR = 64;      // 0.25: this much hair is hair
const MARK = 40;      // a bindi or sindoor counts from very little: missing one is the worse mistake

const inEllipse = (u, v, cu, cv, ru, rv) => ((u - cu) / ru) ** 2 + ((v - cv) / rv) ** 2 <= 1;

/** Room for the style, beyond this person's own hair. */
function room(kind, u, v) {
  const head = inEllipse(u, v, 0, -0.35, 2.05, 2.3);   // to the chin at the sides, for a bob
  if (kind === 'head') return head;
  if (kind === 'long') {
    // Down each side of the face and over the shoulders, never across the
    // front of the neck and chest, where a thali, a collar or a child on a lap is.
    const side = Math.abs(u) >= 0.95 && Math.abs(u) <= 3.1 && v >= 0 && v <= 5.6;
    return head || side;
  }
  if (kind === 'up') return head || inEllipse(u, v, 0, -1.2, 2.1, 2.3);
  if (kind === 'fringe') return head && v <= 0.05;
  if (kind === 'beard') return inEllipse(u, v, 0, 1.35, 1.35, 1.05);
  return head;
}

/** The face that is kept whatever the style: an oval from the hairline to the chin. */
function faceOval(u, v) {
  return inEllipse(u, v, 0, 0.45, 1.15, 1.62);
}

/**
 * The two masks for a picture `width × height`, one byte a pixel, 255 where set.
 *
 * @param maps    `{a, b, ids}`: the analysis maps at this size, RGBA bytes
 *                (see PortraitSession.payload): a = skin, zone, marks;
 *                b = hair, beard; ids = whose skin, whose hair
 * @param faces   the faces as PortraitSession.faces() gives them: `{id, frame}`,
 *                frame in fractions of the picture (x, y) and of its width (d)
 * @param target  the ids of the people to restyle
 * @param kind    one of the REACH keys
 */
export function hairstyleMasks({ width, height, maps, faces, target, kind = 'head' }) {
  const n = width * height;
  const change = new Uint8Array(n), keep = new Uint8Array(n);
  const { a, b, ids } = maps;
  const chosen = new Set(target);
  const reach = REACH[kind] || REACH.head;
  const people = faces.filter((f) => f.frame && f.frame.d > 0).map((f) => ({
    id: f.id,
    mine: chosen.has(f.id),
    x: f.frame.x * width, y: f.frame.y * height, d: f.frame.d * width,
    cos: f.frame.cos, sin: f.frame.sin,
  }));
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      const i = y * width + x, k = i * 4;
      const skinId = ids[k], hairId = ids[k + 1];
      const skin = a[k] >= SKIN, mark = a[k + 2] >= MARK;
      const hair = b[k] >= HAIR, beard = b[k + 1] >= HAIR;
      let wanted = false, kept = mark;
      // Somebody else's face or hair is theirs.
      if (skin && skinId && !chosen.has(skinId)) kept = true;
      if ((hair || beard) && hairId && !chosen.has(hairId)) kept = true;
      for (const p of people) {
        const dx = x - p.x, dy = y - p.y;
        const u = (dx * p.cos + dy * p.sin) / p.d, v = (-dx * p.sin + dy * p.cos) / p.d;
        if (!p.mine) {
          // Another person's face is kept even where the maps missed it.
          if (faceOval(u, v)) kept = true;
          continue;
        }
        const ownHair = hairId === p.id && hair;
        const ownBeard = hairId === p.id && beard;
        const near = Math.abs(u) < 3.5 && v > -3 && v < 6;
        if (kind === 'beard') {
          if (ownBeard || room(kind, u, v)) wanted = true;
          // The eyes, the nose and everything above them stay.
          if (v < 0.62 && (faceOval(u, v) || (skin && skinId === p.id))) kept = true;
        } else {
          if ((ownHair && near) || room(kind, u, v)) wanted = true;
          // The face from the brows (a fringe) or the hairline down: its skin
          // always, and the oval except where this person's own hair lies on it.
          const top = reach.face;
          const ownSkin = skin && skinId === p.id && Math.abs(u) < 1.25 && v < 2.4;
          if (v >= top && ((faceOval(u, v) && !ownHair) || ownSkin)) kept = true;
          if (ownBeard) kept = true;   // a beard is not a hairstyle; it stays as it is
        }
      }
      if (kept) keep[i] = 255;
      else if (wanted) change[i] = 255;
    }
  }
  return { change, keep };
}

/**
 * How much of the drawn picture to take at each pixel, 0..1: the painted area
 * with a soft edge so the new hair melts into what is around it, and nothing at
 * all inside the kept area or on its very edge — the soft edge is pushed out of
 * the face, never into it.
 */
export function blendWeights(painted, keep, width, height, feather = 3) {
  const n = width * height;
  const paint = new Float32Array(n), kept = new Float32Array(n);
  for (let i = 0; i < n; i++) { paint[i] = painted[i] > 24 ? 1 : 0; kept[i] = keep[i] ? 1 : 0; }
  const soft = blur(paint, width, height, feather);
  // Grow the kept area by the feather, then soften it outwards only.
  const grown = blur(kept, width, height, feather);
  for (let i = 0; i < n; i++) grown[i] = grown[i] > 0.02 ? 1 : 0;
  const guard = blur(grown, width, height, feather);
  const out = new Float32Array(n);
  for (let i = 0; i < n; i++) out[i] = kept[i] ? 0 : Math.max(soft[i], paint[i]) * (1 - guard[i]);
  return out;
}

/**
 * The drawn picture laid onto the photograph through *weights* (at `ww × wh`,
 * resampled to the picture's size): RGBA bytes, a new array. Where the weight is
 * nothing — the kept face, everything away from the hair — the photograph's own
 * bytes are copied, so those pixels are exactly what they were. *keep*, at the
 * picture's size (see keepAt), makes sure of that where resampling the weights
 * would otherwise let a sliver of the drawing onto the edge of a face.
 */
export function blendOnto(base, drawn, width, height, weights, ww, wh, keep = null) {
  const w = ww === width && wh === height ? weights.slice() : resize(weights, ww, wh, width, height);
  if (keep) for (let i = 0; i < w.length; i++) if (keep[i]) w[i] = 0;
  const out = new Uint8ClampedArray(base.length);
  for (let i = 0, k = 0; i < width * height; i++, k += 4) {
    const t = w[i];
    if (t <= 0.0005) {
      out[k] = base[k]; out[k + 1] = base[k + 1]; out[k + 2] = base[k + 2]; out[k + 3] = base[k + 3];
      continue;
    }
    out[k] = base[k] + (drawn[k] - base[k]) * t;
    out[k + 1] = base[k + 1] + (drawn[k + 1] - base[k + 1]) * t;
    out[k + 2] = base[k + 2] + (drawn[k + 2] - base[k + 2]) * t;
    out[k + 3] = 255;
  }
  return out;
}

/** The kept area at a picture's full size, from the mask at `ww × wh`: a pixel is kept if any of it was. */
export function keepAt(keep, ww, wh, width, height) {
  if (ww === width && wh === height) return keep;
  const out = new Uint8Array(width * height);
  const fx = ww / width, fy = wh / height;
  for (let y = 0; y < height; y++) {
    const y0 = Math.floor(y * fy), y1 = Math.min(wh - 1, Math.floor((y + 1) * fy - 1e-6));
    for (let x = 0; x < width; x++) {
      const x0 = Math.floor(x * fx), x1 = Math.min(ww - 1, Math.floor((x + 1) * fx - 1e-6));
      let any = 0;
      for (let yy = y0; yy <= Math.max(y0, y1) && !any; yy++)
        for (let xx = x0; xx <= Math.max(x0, x1); xx++) if (keep[yy * ww + xx]) { any = 255; break; }
      out[y * width + x] = any;
    }
  }
  return out;
}
