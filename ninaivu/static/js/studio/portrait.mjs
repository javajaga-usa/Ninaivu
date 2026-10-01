/**
 * Retouching faces the way a photographer would, and never the way a filter does.
 *
 * What this reads: a picture, and the maps the server made of it — for each
 * pixel, how much of it is skin, how much is hair, how much is a bindi — and a
 * list of what to do for whom (see `portrait-params.mjs`). What it does is a
 * handful of small, honest operations, each confined to the people and the parts
 * it was asked for:
 *
 *   light      exposure on linear light, rolled off before white, so a face in
 *              shadow gets what a fill flash would have given it;
 *   tone       one colour cast turned back towards skin, lightness untouched;
 *   balance    one side of the face brighter than the other, evened;
 *   shine      highlights eased back towards the skin's own light and colour;
 *   evenness   patches of colour at the scale of a blotch, never of a feature;
 *   hollows    the dark under the eyes, lifted towards the cheek beside them;
 *   softness   blemishes, but not pores: frequency separation, with real edges
 *              (a fold, an eyelid, a mole) left standing;
 *   richness   a little more of the skin's own colour;
 *   hair       strand detail, fuller hair, covering grey, recolouring — and the beard.
 *
 * Bindis, kumkum, sindoor, sacred ash and sandal paste are in the *protect* map
 * and are excluded from every operation that could change them, and from every
 * average that could smear them into the skin beside them.
 *
 * It works a face at a time, on a window of the picture, in CIE Lab, so that the
 * cost follows the faces and not the photograph: a group portrait of twelve
 * people and a single one at the same size take the same time a face.
 */

import { SRGB_TO_LINEAR, encode, linearToLab, labToLinear, luminance, smoothstep, parseHex, rgbToLab } from './colour.mjs';
import { blur, maskedBlur, weightedQuantile } from './plane.mjs';
import { effective, asksForSkin, asksForHair, LIMITS, NATURAL_DARK } from './portrait-params.mjs';

const clamp01 = (v) => (v < 0 ? 0 : v > 1 ? 1 : v);

/** What to assume about the size of a face the analysis did not find: painted by hand, it has none. */
const FREE_FACE_FRACTION = 0.045;

// ---------------------------------------------------------------------------
// The maps
// ---------------------------------------------------------------------------

const CHANNELS = {
  skin: ['a', 0], zone: ['a', 1], protect: ['a', 2],
  hair: ['b', 0], beard: ['b', 1], under: ['b', 2],
  body: ['c', 0],
  skinId: ['ids', 0], hairId: ['ids', 1],
};

/**
 * The server's three packed pictures, plus whatever has been painted by hand, at
 * one size — which need not be the picture's. Everything is asked for by window
 * and is resampled to the picture's pixels as it is read.
 */
export class Maps {
  /**
   * @param a,b,c,ids  RGBA byte arrays, width × height × 4 (alpha is not used); `c` may be absent
   * @param paint    `{ skinAdd, skinErase, hairAdd, hairErase }`: one byte a pixel each, any may be absent
   */
  constructor({ width, height, a, b, c = null, ids, paint = {} }) {
    Object.assign(this, { width, height, a, b, c, ids, paint });
    this.cache = null;
  }

  /** Where every face's skin and hair are, in this map's own pixels: `{ faces: Map(id → {skin, hair}), free: {skin, hair} }`. */
  bounds() {
    if (this.cache) return this.cache;
    const { width: w, height: h, a, b, ids, paint } = this;
    const faces = new Map();
    const free = { skin: null, hair: null };
    const grow = (box, x, y) => {
      if (!box) return [x, y, x, y];
      if (x < box[0]) box[0] = x;
      if (y < box[1]) box[1] = y;
      if (x > box[2]) box[2] = x;
      if (y > box[3]) box[3] = y;
      return box;
    };
    const entry = (id) => {
      if (!faces.has(id)) faces.set(id, { skin: null, hair: null });
      return faces.get(id);
    };
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        const i = y * w + x, k = i * 4;
        const skinId = ids[k], hairId = ids[k + 1];
        if (skinId && (a[k] > 5 || a[k + 1] > 5 || (paint.skinAdd && paint.skinAdd[i] > 5))) {
          const e = entry(skinId);
          e.skin = grow(e.skin, x, y);
        } else if (!skinId && paint.skinAdd && paint.skinAdd[i] > 5) {
          free.skin = grow(free.skin, x, y);
        }
        const painted = (paint.hairAdd && paint.hairAdd[i] > 5) || (paint.hairGrow && paint.hairGrow[i] > 5);
        if (hairId && (b[k] > 5 || b[k + 1] > 5 || painted)) {
          const e = entry(hairId);
          e.hair = grow(e.hair, x, y);
        } else if (!hairId && painted) {
          free.hair = grow(free.hair, x, y);
        }
      }
    }
    this.cache = { faces, free };
    return this.cache;
  }

  /**
   * Planes for a window of a picture `imageWidth × imageHeight`, as floats 0..1
   * (ids as whole numbers). `rect` is `{x0, y0, w, h}` in the picture's pixels.
   */
  sample(names, rect, imageWidth, imageHeight) {
    const { width: mw, height: mh } = this;
    const fx = mw / imageWidth, fy = mh / imageHeight;
    const cols = { i0: new Int32Array(rect.w), i1: new Int32Array(rect.w), t: new Float32Array(rect.w), n: new Int32Array(rect.w) };
    for (let x = 0; x < rect.w; x++) {
      const s = Math.min(mw - 1, Math.max(0, (rect.x0 + x + 0.5) * fx - 0.5));
      cols.i0[x] = Math.floor(s);
      cols.i1[x] = Math.min(mw - 1, cols.i0[x] + 1);
      cols.t[x] = s - cols.i0[x];
      cols.n[x] = Math.min(mw - 1, Math.round(s));
    }
    const rows = { i0: new Int32Array(rect.h), i1: new Int32Array(rect.h), t: new Float32Array(rect.h), n: new Int32Array(rect.h) };
    for (let y = 0; y < rect.h; y++) {
      const s = Math.min(mh - 1, Math.max(0, (rect.y0 + y + 0.5) * fy - 0.5));
      rows.i0[y] = Math.floor(s);
      rows.i1[y] = Math.min(mh - 1, rows.i0[y] + 1);
      rows.t[y] = s - rows.i0[y];
      rows.n[y] = Math.min(mh - 1, Math.round(s));
    }
    const out = {};
    for (const name of names) {
      const plane = new Float32Array(rect.w * rect.h);
      let source, stride, offset, nearest = false, scale = 1 / 255;
      if (CHANNELS[name]) {
        const [which, channel] = CHANNELS[name];
        source = this[which]; stride = 4; offset = channel;
        nearest = name === 'skinId' || name === 'hairId';
        if (nearest) scale = 1;
      } else {
        source = this.paint[name]; stride = 1; offset = 0;
      }
      if (!source) { out[name] = plane; continue; }
      for (let y = 0; y < rect.h; y++) {
        const r0 = rows.i0[y] * mw, r1 = rows.i1[y] * mw, ty = rows.t[y], rn = rows.n[y] * mw;
        for (let x = 0; x < rect.w; x++) {
          if (nearest) {
            plane[y * rect.w + x] = source[(rn + cols.n[x]) * stride + offset];
          } else {
            const c0 = cols.i0[x], c1 = cols.i1[x], tx = cols.t[x];
            const top = source[(r0 + c0) * stride + offset] * (1 - tx) + source[(r0 + c1) * stride + offset] * tx;
            const bottom = source[(r1 + c0) * stride + offset] * (1 - tx) + source[(r1 + c1) * stride + offset] * tx;
            plane[y * rect.w + x] = (top * (1 - ty) + bottom * ty) * scale;
          }
        }
      }
      out[name] = plane;
    }
    return out;
  }
}

/** A box in map pixels as a window in picture pixels, with room round it, inside the picture. */
function windowOf(box, maps, width, height, pad) {
  const sx = width / maps.width, sy = height / maps.height;
  const x0 = Math.max(0, Math.floor(box[0] * sx - pad)), y0 = Math.max(0, Math.floor(box[1] * sy - pad));
  const x1 = Math.min(width, Math.ceil((box[2] + 1) * sx + pad)), y1 = Math.min(height, Math.ceil((box[3] + 1) * sy + pad));
  return { x0, y0, w: Math.max(0, x1 - x0), h: Math.max(0, y1 - y0) };
}

// ---------------------------------------------------------------------------
// Reading and writing a window of the picture
// ---------------------------------------------------------------------------

function readLinear(pixels, width, rect) {
  const n = rect.w * rect.h;
  const R = new Float32Array(n), G = new Float32Array(n), B = new Float32Array(n);
  for (let y = 0; y < rect.h; y++) {
    let src = ((rect.y0 + y) * width + rect.x0) * 4, dst = y * rect.w;
    for (let x = 0; x < rect.w; x++, src += 4, dst++) {
      R[dst] = SRGB_TO_LINEAR[pixels[src]];
      G[dst] = SRGB_TO_LINEAR[pixels[src + 1]];
      B[dst] = SRGB_TO_LINEAR[pixels[src + 2]];
    }
  }
  return { R, G, B };
}

/** Put the window back, but only where the operation reached: every other pixel stays exactly as it was. */
function writeBack(pixels, width, rect, R, G, B, touched) {
  for (let y = 0; y < rect.h; y++) {
    let dst = ((rect.y0 + y) * width + rect.x0) * 4, src = y * rect.w;
    for (let x = 0; x < rect.w; x++, dst += 4, src++) {
      if (!touched[src]) continue;
      pixels[dst] = encode(R[src]);
      pixels[dst + 1] = encode(G[src]);
      pixels[dst + 2] = encode(B[src]);
    }
  }
}

// ---------------------------------------------------------------------------
// The operations
// ---------------------------------------------------------------------------

/** Linear light to linear light, rolled off before white so that nothing is pushed past it. */
const shoulder = (x) => (x <= 0.72 ? x : 0.72 + 0.28 * (1 - Math.exp(-(x - 0.72) / 0.28)));

/**
 * A fill flash for a face in shadow: exposure, on linear light, with the lift
 * spent where there is room for it. Colour is carried along with the light —
 * every channel is scaled by the same factor — so skin keeps the colour it has.
 * It is light, so it reaches a bindi as it reaches the skin beside it: a mark
 * left in shadow on a face brought into the light would look stuck on.
 */
export function lightenFace(R, G, B, weight, amount) {
  const gain = 2 ** (LIMITS.faceLight * amount);
  for (let i = 0; i < R.length; i++) {
    const z = weight[i];
    if (z <= 0.002) continue;
    const y = luminance(R[i], G[i], B[i]);
    const ratio = y > 1e-5 ? shoulder(y * gain) / y : gain;
    const k = 1 + z * (ratio - 1);
    R[i] *= k; G[i] *= k; B[i] *= k;
  }
}

/**
 * One side of the face brighter than the other. Fits how lightness changes from
 * one side of the face to the other — across the line between the eyes, which is
 * what a window light or a low sun does — and takes away the part beyond what a
 * face's own modelling explains.
 */
export function balanceSides(L, weight, reach, w, h, frame, amount) {
  if (!frame) return;
  const n = w * h, u = new Float32Array(n);
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) u[y * w + x] = ((x - frame.cx) * frame.cos + (y - frame.cy) * frame.sin) / frame.d;
  }
  // A line through lightness against position across the face: fitted by least
  // squares, then twice more with whatever sits far from the line counting for
  // less, so a patch of shadow or a highlight does not tilt it.
  let slope = 0, intercept = 0, centre = 0;
  for (let pass = 0; pass < 3; pass++) {
    const closeness = (i) => (pass === 0 ? 1 : 1 / (1 + ((L[i] - intercept - slope * u[i]) / 6) ** 2));
    let total = 0, sumU = 0, sumL = 0;
    for (let i = 0; i < n; i++) {
      if (weight[i] < 0.5) continue;
      const c = closeness(i);
      total += c; sumU += c * u[i]; sumL += c * L[i];
    }
    if (total < 30) return;
    const meanU = sumU / total, meanL = sumL / total;
    let cross = 0, spread = 0;
    for (let i = 0; i < n; i++) {
      if (weight[i] < 0.5) continue;
      const c = closeness(i);
      cross += c * (u[i] - meanU) * (L[i] - meanL);
      spread += c * (u[i] - meanU) ** 2;
    }
    if (spread < 1e-6) return;
    slope = cross / spread;
    intercept = meanL - slope * meanU;
    centre = meanU;
  }
  // A face has some modelling of its own, a few units of lightness from one side
  // to the other; only what goes beyond that is the light's doing.
  const beyond = Math.sign(slope) * Math.max(0, Math.abs(slope) - 3);
  if (beyond === 0) return;
  const cap = LIMITS.balance * amount;
  for (let i = 0; i < n; i++) {
    if (reach[i] <= 0.002) continue;
    const change = -amount * beyond * (u[i] - centre) * reach[i];
    L[i] += Math.max(-cap, Math.min(cap, change));
  }
}

/**
 * The dark under the eyes, lifted towards the skin beside it.
 * It only ever moves pixels *up* to the cheek's own lightness: it cannot make
 * the hollow brighter than the face around it.
 */
export function liftHollows(L, A, B, weight, hollows, protect, w, h, d, amount) {
  const n = w * h, around = new Float32Array(n);
  for (let i = 0; i < n; i++) around[i] = weight[i] * (1 - Math.min(1, 2 * hollows[i]));
  const sigma = Math.max(2, 0.35 * d);
  const Lr = maskedBlur(L, around, w, h, sigma), Ar = maskedBlur(A, around, w, h, sigma), Br = maskedBlur(B, around, w, h, sigma);
  for (let i = 0; i < n; i++) {
    const u = hollows[i] * (1 - protect[i]);
    if (u <= 0.01) continue;
    const gap = Lr[i] - L[i];
    if (gap <= 0) continue;
    L[i] += Math.min(gap, 14) * 0.8 * amount * u;
    const pull = 0.5 * amount * u * smoothstep(1, 5, gap);
    A[i] += (Ar[i] - A[i]) * pull;
    B[i] += (Br[i] - B[i]) * pull;
  }
}

/**
 * Shine: what is brighter than the skin round it, pulled back towards that skin,
 * its colour returned. The reference is the skin's own light — taken with the
 * highlights clipped out of it, so a forehead's glare does not lift its own
 * standard — and whatever is far brighter than skin could be (an earring,
 * a stud, a tikka) is left alone.
 */
export function easeShine(L, A, B, weight, w, h, d, amount) {
  const n = w * h;
  const Lb0 = maskedBlur(L, weight, w, h, Math.max(2, 0.3 * d));
  const clipped = new Float32Array(n);
  for (let i = 0; i < n; i++) clipped[i] = Math.min(L[i], Lb0[i] + 3);
  const Lb = maskedBlur(clipped, weight, w, h, Math.max(2, 0.22 * d));
  const plain = new Float32Array(n);
  for (let i = 0; i < n; i++) plain[i] = weight[i] * (1 - smoothstep(3, 10, L[i] - Lb[i]));
  const Ab = maskedBlur(A, plain, w, h, Math.max(2, 0.3 * d)), Bb = maskedBlur(B, plain, w, h, Math.max(2, 0.3 * d));
  for (let i = 0; i < n; i++) {
    const wt = weight[i];
    if (wt <= 0.002) continue;
    const excess = L[i] - Lb[i] - 2;
    if (excess <= 0) continue;
    const g = amount * wt * (1 - smoothstep(18, 30, excess));
    L[i] -= 0.85 * excess * g;
    const pull = 0.7 * g * smoothstep(2, 10, excess);
    A[i] += (Ab[i] - A[i]) * pull;
    B[i] += (Bb[i] - B[i]) * pull;
  }
}

/**
 * Patches of colour, at the scale of a blotch: bigger than a pore, smaller than
 * the shading of a face. Each pixel is drawn part of the way towards what the
 * skin a little further off averages to, which takes the edge off a patch and
 * leaves the face its form.
 */
export function evenTone(L, A, B, weight, w, h, d, amount) {
  const fine = Math.max(1, 0.06 * d), wide = Math.max(3, 0.3 * d);
  const denominatorFine = blur(weight, w, h, fine), denominatorWide = blur(weight, w, h, wide);
  const pass = (plane, strength, cap) => {
    const near = maskedBlur(plane, weight, w, h, fine, denominatorFine);
    const far = maskedBlur(plane, weight, w, h, wide, denominatorWide);
    for (let i = 0; i < plane.length; i++) {
      if (weight[i] <= 0.002) continue;
      plane[i] += Math.max(-cap, Math.min(cap, (far[i] - near[i]) * amount * strength)) * weight[i];
    }
  };
  pass(A, 0.9, 7); pass(B, 0.9, 7); pass(L, 0.5, 5);
}

/**
 * Soften blemishes and leave the skin skin. Frequency separation: the picture of
 * a face is a broad part (the form), a middle part (blemishes, patchiness) and a
 * fine part (pores, grain). Softening takes most of the middle and a little of
 * the fine, so nothing goes to wax. And a middle-sized difference that is strong
 * is not a flaw but a fold, an eyelid, a mole, and is kept.
 */
export function softenSkin(L, A, B, weight, w, h, d, amount) {
  const s1 = Math.max(0.8, 0.014 * d), s2 = Math.max(1.6, 0.05 * d);
  const d1 = blur(weight, w, h, s1), d2 = blur(weight, w, h, s2);
  const one = (plane, cutFine, edgeLow, edgeHigh) => {
    const b1 = maskedBlur(plane, weight, w, h, s1, d1), b2 = maskedBlur(plane, weight, w, h, s2, d2);
    for (let i = 0; i < plane.length; i++) {
      const wt = weight[i];
      if (wt <= 0.002) continue;
      const fineBand = plane[i] - b1[i], mid = b1[i] - b2[i];
      const structure = smoothstep(edgeLow, edgeHigh, Math.abs(mid));
      plane[i] -= wt * (mid * amount * 0.85 * (1 - 0.8 * structure) + fineBand * amount * cutFine);
    }
  };
  one(L, 0.3, 5, 14);
  one(A, 0.6, 3, 9);
  one(B, 0.6, 3, 9);
}

/** Turn the colour of skin round the a-b plane: a cast's worth of hue, with lightness and depth of colour as they were. */
export function turnTone(A, B, weight, amount) {
  const theta = amount * LIMITS.tone * Math.PI / 180;
  for (let i = 0; i < A.length; i++) {
    const wt = weight[i];
    if (wt <= 0.002) continue;
    const t = theta * wt, c = Math.cos(t), s = Math.sin(t);
    const a = A[i], b = B[i];
    A[i] = a * c - b * s;
    B[i] = a * s + b * c;
  }
}

/** A little more of the skin's own colour. Never more than a third more, and never a hue the skin did not have. */
export function enrichSkin(A, B, weight, amount) {
  for (let i = 0; i < A.length; i++) {
    const wt = weight[i];
    if (wt <= 0.002) continue;
    const k = 1 + LIMITS.richness * amount * wt;
    A[i] *= k; B[i] *= k;
  }
}

// ---------------------------------------------------------------------------
// A face's skin
// ---------------------------------------------------------------------------

/**
 * Everything the skin tools do for one face, on the window of the picture it
 * occupies. `frame` is the face's own axes (`{cx, cy, d, cos, sin}` in the window's
 * pixels), or null for skin that was painted by hand, which has none.
 */
export function retouchSkinWindow(pixels, width, rect, planes, frame, d, p) {
  const n = rect.w * rect.h;
  const { S, Z, P, U, C } = planes;
  const { R, G, B } = readLinear(pixels, width, rect);
  const amount = (key) => Math.max(-1, Math.min(1, p[key] / 100));

  if (p.faceLight) lightenFace(R, G, B, Z, amount('faceLight'));

  const needsLab = ['balance', 'shine', 'even', 'underEye', 'smooth', 'tone', 'richness'].some((key) => p[key]);
  // Only the pixels an operation could reach are written back, so that a mark the
  // skin tools leave alone is not so much as rounded differently on its way through.
  const touched = new Uint8Array(n);
  const weight = new Float32Array(n), colour = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    weight[i] = S[i] * (1 - P[i]);
    // A colour that stopped at the jaw would leave a line round the face: it goes
    // on all the skin that goes with it, the ears and the neck too.
    colour[i] = Math.max(S[i], C[i]) * (1 - P[i]);
  }
  for (let i = 0; i < n; i++) {
    const light = (p.faceLight || p.balance) && Z[i] > 0.002;
    const skin = (p.shine || p.even || p.smooth) && weight[i] > 0.002;
    const tint = (p.tone || p.richness) && colour[i] > 0.002;
    const hollow = p.underEye && U[i] * (1 - P[i]) > 0.002;
    touched[i] = (light || skin || tint || hollow) ? 1 : 0;
  }

  if (needsLab) {
    const L = new Float32Array(n), A = new Float32Array(n), Bc = new Float32Array(n);
    linearToLab(R, G, B, L, A, Bc, n);

    if (p.balance) {
      const reach = new Float32Array(n);
      for (let i = 0; i < n; i++) reach[i] = Math.max(weight[i], 0.85 * Z[i]);
      balanceSides(L, weight, reach, rect.w, rect.h, frame, amount('balance'));
    }
    if (p.underEye) liftHollows(L, A, Bc, weight, U, P, rect.w, rect.h, d, amount('underEye'));
    if (p.shine) easeShine(L, A, Bc, weight, rect.w, rect.h, d, amount('shine'));
    if (p.even) evenTone(L, A, Bc, weight, rect.w, rect.h, d, amount('even'));
    if (p.smooth) softenSkin(L, A, Bc, weight, rect.w, rect.h, d, amount('smooth'));
    if (p.tone) turnTone(A, Bc, colour, amount('tone'));
    if (p.richness) enrichSkin(A, Bc, colour, amount('richness'));
    for (let i = 0; i < n; i++) if (L[i] < 0) L[i] = 0;
    labToLinear(L, A, Bc, R, G, B, n);
  }
  writeBack(pixels, width, rect, R, G, B, touched);
}

// ---------------------------------------------------------------------------
// A face's hair
// ---------------------------------------------------------------------------

/** The colour grey is covered with: the darkest strands this person has, or a natural dark if there are none. */
function coverColour(L, A, B, weight, hex) {
  const chosen = parseHex(hex);
  if (chosen) return rgbToLab(...chosen);
  const dark = weightedQuantile(L, weight, 0.2, 0, 100);
  if (dark !== null && dark < 40) {
    let sa = 0, sb = 0, count = 0;
    for (let i = 0; i < L.length; i++) {
      if (weight[i] > 0.5 && L[i] <= dark + 4) { sa += A[i]; sb += B[i]; count++; }
    }
    if (count > 20) return [dark, sa / count, sb / count];
  }
  return rgbToLab(...parseHex(NATURAL_DARK));
}

/**
 * Hair and beard: strands, grey, colour. The weights come from the hair map and
 * the beard map, with the parting and anything else in the protect map left out.
 */
export function retouchHairWindow(pixels, width, rect, planes, d, p) {
  const n = rect.w * rect.h;
  const { H, Bd, P, Hall, Gr } = planes;
  const weight = new Float32Array(n), touched = new Uint8Array(n);
  for (let i = 0; i < n; i++) {
    weight[i] = clamp01(H[i] + (p.beard ? Bd[i] : 0)) * (1 - P[i]);
    touched[i] = weight[i] > 0.002 ? 1 : 0;
  }
  const { R, G, B } = readLinear(pixels, width, rect);
  const L = new Float32Array(n), A = new Float32Array(n), Bc = new Float32Array(n);
  linearToLab(R, G, B, L, A, Bc, n);
  const amount = (key) => Math.max(0, Math.min(1, p[key] / 100));

  if (p.hairGrow && Gr) {
    // Added hair: where the brush said there should be hair — a receding
    // hairline, a thin crown — hair is drawn from the strands beside it. Each
    // painted pixel finds the nearest pixel of real hair and takes the colour
    // of its mirror image across that edge, so the new hair runs on from the
    // old with the same strands, colour and shine; a pixel too far from any
    // hair to borrow from is left alone. What is grown is then hair for every
    // other tool here, and for nothing else: the skin map is not changed.
    const k = amount('hairGrow');
    const nearest = nearestHair(Hall, Gr, rect.w, rect.h);
    const L0 = L.slice(), A0 = A.slice(), B0 = Bc.slice();
    const reach = Math.max(4, 0.9 * d);
    for (let i = 0; i < n; i++) {
      const g = clamp01(Gr[i]) * (1 - P[i]);
      if (g <= 0.002 || nearest[i] < 0 || Hall[i] > 0.6) continue;
      const x = i % rect.w, y = (i - x) / rect.w;
      const j = nearest[i], nx = j % rect.w, ny = (j - nx) / rect.w;
      const dist = Math.hypot(x - nx, y - ny);
      if (dist > reach) continue;
      // The mirror image across the hairline; failing that, a point just inside it.
      let sx = Math.round(2 * nx - x), sy = Math.round(2 * ny - y);
      let src = inside(sx, sy, rect.w, rect.h) ? sy * rect.w + sx : -1;
      if (src < 0 || Hall[src] < 0.3) {
        sx = Math.round(nx + (nx - x) * 0.4); sy = Math.round(ny + (ny - y) * 0.4);
        src = inside(sx, sy, rect.w, rect.h) ? sy * rect.w + sx : -1;
        if (src < 0 || Hall[src] < 0.3) src = j;
      }
      const m = k * g * (1 - smoothstep(0.6 * reach, reach, dist));
      L[i] += (L0[src] - L[i]) * m;
      A[i] += (A0[src] - A[i]) * m;
      Bc[i] += (B0[src] - Bc[i]) * m;
      weight[i] = Math.max(weight[i], m);
      touched[i] = 1;
    }
  }

  if (p.greyCover) {
    const [Lt, At, Bt] = coverColour(L, A, Bc, weight, p.hairColor);
    const k = amount('greyCover');
    for (let i = 0; i < n; i++) {
      const wt = weight[i];
      if (wt <= 0.002) continue;
      const c = Math.hypot(A[i], Bc[i]);
      // Grey and white: a good deal lighter than the dark strands and all but colourless. Not henna, not blonde, not a highlight on black.
      const grey = smoothstep(Lt + 6, Lt + 20, L[i]) * (1 - smoothstep(14, 26, c));
      const g = k * wt * grey;
      if (g <= 0) continue;
      L[i] += (Lt + (L[i] - Lt) * 0.3 - L[i]) * g;
      A[i] += (At - A[i]) * g;
      Bc[i] += (Bt - Bc[i]) * g;
    }
  }

  if (p.hairAmount && parseHex(p.hairColor)) {
    const [Lc, Ac, Bc2] = rgbToLab(...parseHex(p.hairColor));
    const median = weightedQuantile(L, weight, 0.5, 0, 100);
    const k = amount('hairAmount') * 0.9;
    if (median !== null) {
      for (let i = 0; i < n; i++) {
        const wt = weight[i];
        if (wt <= 0.002) continue;
        const m = k * wt * (1 - smoothstep(58, 82, L[i]));      // the glints in hair stay white
        L[i] += (Lc - median) * m;
        A[i] += (Ac - A[i]) * m;
        Bc[i] += (Bc2 - Bc[i]) * m;
      }
    }
  }

  if (p.hairFill) {
    // Fuller hair. Thin hair is hair with light showing through it: where the
    // strands are sparse, the scalp or the wall behind lifts the lightness of a
    // whole patch. So two things are asked of each pixel before it is filled —
    // is the patch round it lighter than the hair's own dark strands (a thin
    // place, not a single bright hair), and is the pixel itself a gap rather
    // than a strand — and the gap is then pulled most of the way down to the
    // strands' tone and its colour towards theirs. The strands between keep
    // their own tone, the texture the gap had is kept and a little sharpened,
    // so the filled place reads as hair and not as paint; the glints are left;
    // and the skin and the wall, which are not in the hair map, are never
    // reached.
    const k = amount('hairFill');
    const dark = weightedQuantile(L, weight, 0.3, 0, 100);
    if (dark !== null) {
      let sa = 0, sb = 0, count = 0;
      for (let i = 0; i < n; i++) {
        if (weight[i] > 0.5 && L[i] <= dark + 4) { sa += A[i]; sb += Bc[i]; count++; }
      }
      const As = count ? sa / count : null, Bs = count ? sb / count : null;
      const base = maskedBlur(L, weight, rect.w, rect.h, Math.max(1.5, 0.07 * d));
      // Only where hair is all round, and only where there are strands: a stray
      // pixel the maps left inside the skin, a lone dab of the brush, or a smooth
      // patch of shadowed skin the brush strayed onto must not become a dark
      // spot. Thin hair always has strands crossing the light that shows
      // through it, so the texture round a pixel says whether it is hair.
      const support = blur(weight, rect.w, rect.h, Math.max(1.5, 0.1 * d));
      const deviation = new Float32Array(n);
      for (let i = 0; i < n; i++) deviation[i] = Math.abs(L[i] - base[i]);
      const texture = maskedBlur(deviation, weight, rect.w, rect.h, Math.max(1.5, 0.1 * d));
      for (let i = 0; i < n; i++) {
        const wt = weight[i];
        if (wt <= 0.002) continue;
        const thin = smoothstep(dark + 2, dark + 14, base[i]);
        const gap = smoothstep(dark + 2, dark + 18, L[i]) * (1 - smoothstep(62, 86, L[i]));
        const m = k * wt * thin * gap * smoothstep(0.35, 0.7, support[i]) * smoothstep(2, 5, texture[i]);
        if (m <= 0.001) continue;
        const detail = L[i] - base[i];
        L[i] += (dark + (L[i] - dark) * 0.25 - L[i]) * m + detail * 0.15 * m;
        if (L[i] < 0) L[i] = 0;
        if (As !== null) { A[i] += (As - A[i]) * m * 0.8; Bc[i] += (Bs - Bc[i]) * m * 0.8; }
      }
    }
  }

  if (p.hairDetail) {
    const k = amount('hairDetail');
    const base = maskedBlur(L, weight, rect.w, rect.h, Math.max(1, 0.035 * d));
    for (let i = 0; i < n; i++) {
      const wt = weight[i];
      if (wt <= 0.002) continue;
      const detail = L[i] - base[i];
      // Black hair is a blob until its strands are told apart: more contrast
      // between them, a little light into the deepest shadows, a glint on the
      // highlights. Not so much that black is no longer black.
      const lift = 6 * k * (1 - smoothstep(0, 28, base[i]));
      L[i] += wt * (detail * 1.2 * k + Math.max(0, detail) * 0.6 * k + lift);
      if (L[i] < 0) L[i] = 0;
    }
  }

  labToLinear(L, A, Bc, R, G, B, n);
  writeBack(pixels, width, rect, R, G, B, touched);
}

// ---------------------------------------------------------------------------
// The whole picture
// ---------------------------------------------------------------------------

/**
 * Retouch every face in *pixels* (RGBA bytes, `width × height`, changed in place).
 *
 * @param maps     a {@link Maps}
 * @param portrait `{ params: {all, faces, beard, hairColor}, frames: {id: {x, y, d, cos, sin}} }`,
 *                 the frames in fractions of the picture's width and height
 */
export function portraitPass({ pixels, width, height, maps, portrait }) {
  const { faces, free } = maps.bounds();
  const params = portrait.params;
  const frames = portrait.frames || {};

  for (const [id, box] of faces) {
    const p = effective(params, id);
    const frame = frames[id];
    const d = frame ? frame.d * width : FREE_FACE_FRACTION * width;
    const pad = Math.ceil(0.25 * d);
    if (box.skin && asksForSkin(p)) {
      const rect = windowOf(box.skin, maps, width, height, pad);
      if (rect.w > 3 && rect.h > 3) {
        const planes = skinPlanes(maps, rect, width, height, id);
        const here = frame ? { cx: frame.x * width - rect.x0, cy: frame.y * height - rect.y0, d, cos: frame.cos, sin: frame.sin } : null;
        retouchSkinWindow(pixels, width, rect, planes, here, d, p);
      }
    }
    if (box.hair && asksForHair(p)) {
      const rect = windowOf(box.hair, maps, width, height, pad);
      if (rect.w > 3 && rect.h > 3) retouchHairWindow(pixels, width, rect, hairPlanes(maps, rect, width, height, id), d, p);
    }
  }

  // Painted by hand where no face was found: there is nobody's own settings, so everybody's.
  const shared = effective(params, 0);
  const d = FREE_FACE_FRACTION * width;
  if (free.skin && asksForSkin(shared)) {
    const rect = windowOf(free.skin, maps, width, height, Math.ceil(0.25 * d));
    if (rect.w > 3 && rect.h > 3) retouchSkinWindow(pixels, width, rect, skinPlanes(maps, rect, width, height, 0), null, d, shared);
  }
  if (free.hair && asksForHair(shared)) {
    // Hair painted where nobody's hair was found is still somebody's — added
    // hair most of all, which borrows from the hair beside it — so it is sized
    // to the largest face in the picture, and its window reaches that far.
    const sizes = Object.values(portrait.frames || {}).map((f) => f.d * width).filter((v) => v > 0);
    const dHair = sizes.length ? Math.max(...sizes) : d;
    const rect = windowOf(free.hair, maps, width, height, Math.ceil(maps.paint.hairGrow ? dHair : 0.25 * dHair));
    if (rect.w > 3 && rect.h > 3) retouchHairWindow(pixels, width, rect, hairPlanes(maps, rect, width, height, 0), dHair, shared);
  }
}

/**
 * How protected a place is, from the protect map's soft edge: complete well before
 * the map reaches one, and beginning a little outside the mark. The map is blurred
 * so that it has no hard edge to be seen, but a mark is promised untouched, and a
 * weight of a hundredth is still a change.
 */
const protection = (value) => smoothstep(0.3, 0.85, value);

/** This face's skin weights, with the brush's additions and erasures, gated to the face that owns each pixel. */
function skinPlanes(maps, rect, width, height, id) {
  const read = maps.sample(['skin', 'zone', 'protect', 'under', 'body', 'skinId', 'skinAdd', 'skinErase'], rect, width, height);
  const n = rect.w * rect.h;
  const S = new Float32Array(n), Z = new Float32Array(n), P = new Float32Array(n), U = new Float32Array(n), C = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    const mine = read.skinId[i] === id;
    const erased = 1 - read.skinErase[i];
    const added = mine || (id === 0 && read.skinId[i] === 0) ? read.skinAdd[i] : 0;
    if (mine) {
      S[i] = clamp01(read.skin[i] * erased + added);
      Z[i] = Math.max(read.zone[i] * erased, S[i]);
      P[i] = protection(read.protect[i]);
      U[i] = read.under[i] * erased;
      C[i] = read.body[i] * erased;
    } else if (added > 0) {
      S[i] = added; Z[i] = added;
    }
  }
  return { S, Z, P, U, C };
}

const inside = (x, y, w, h) => x >= 0 && y >= 0 && x < w && y < h;

/**
 * For every pixel, the index of the nearest pixel of hair (`weight` above 0.45),
 * or -1 where there is none within the window: two passes of a chamfer sweep
 * that carry the nearest point along, which is exact enough for a brush's reach.
 * Only pixels the brush painted (`painted` above 0) are answered, to save the work.
 */
export function nearestHair(weight, painted, w, h) {
  const n = w * h, near = new Int32Array(n).fill(-1), dist = new Float32Array(n).fill(Infinity);
  for (let i = 0; i < n; i++) if (weight[i] > 0.45) { near[i] = i; dist[i] = 0; }
  const consider = (i, x, y, j) => {
    const c = near[j];
    if (c < 0) return;
    const cx = c % w, cy = (c - cx) / w, dd = Math.hypot(x - cx, y - cy);
    if (dd < dist[i]) { dist[i] = dd; near[i] = c; }
  };
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const i = y * w + x;
      if (dist[i] === 0) continue;
      if (x > 0) consider(i, x, y, i - 1);
      if (y > 0) { consider(i, x, y, i - w); if (x > 0) consider(i, x, y, i - w - 1); if (x < w - 1) consider(i, x, y, i - w + 1); }
    }
  }
  for (let y = h - 1; y >= 0; y--) {
    for (let x = w - 1; x >= 0; x--) {
      const i = y * w + x;
      if (dist[i] === 0) continue;
      if (x < w - 1) consider(i, x, y, i + 1);
      if (y < h - 1) { consider(i, x, y, i + w); if (x < w - 1) consider(i, x, y, i + w + 1); if (x > 0) consider(i, x, y, i + w - 1); }
    }
  }
  for (let i = 0; i < n; i++) if (!(painted[i] > 0)) near[i] = dist[i] === 0 ? i : -1;
  return near;
}

function hairPlanes(maps, rect, width, height, id) {
  const read = maps.sample(['hair', 'beard', 'protect', 'hairId', 'skinId', 'skin', 'body', 'hairAdd', 'hairErase', 'hairGrow'], rect, width, height);
  const n = rect.w * rect.h;
  const H = new Float32Array(n), Bd = new Float32Array(n), P = new Float32Array(n);
  // Everybody's hair, whoever it belongs to, and the hair the brush asked for:
  // added hair borrows from the hair beside it, and that is the person's own hair
  // even though the pixels it grows over — the forehead — belong to nobody's hair.
  const Hall = new Float32Array(n), Gr = new Float32Array(n);
  let grown = false;
  for (let i = 0; i < n; i++) {
    const mine = read.hairId[i] === id;
    const erased = 1 - read.hairErase[i];
    const added = mine || (id === 0 && read.hairId[i] === 0) ? read.hairAdd[i] : 0;
    Hall[i] = clamp01(read.hair[i] * erased);
    if (mine) {
      // Skin the tools know of is never hair, however the brush strayed onto it: the
      // face's skin and the body's, and anything more than a little of either.
      H[i] = clamp01((read.hair[i] * erased + added) * (1 - smoothstep(0.1, 0.5, Math.max(read.skin[i], read.body[i]))));
      Bd[i] = read.beard[i] * erased;
      P[i] = read.skinId[i] ? protection(read.protect[i]) : 0;
      Gr[i] = clamp01(read.hairGrow[i] * erased);
      if (Gr[i] > 0.002) grown = true;
    } else if (added > 0) {
      H[i] = added;
    }
  }
  return { H, Bd, P, Hall, Gr: grown ? Gr : null };
}
