/**
 * The develop engine: one recipe in, one photograph out.
 *
 * Both editors call this — the Photo Studio from its worker and Sudar from its
 * own — so a slider means the same thing wherever it is moved, and Node's tests
 * can hold it to its promises without a browser. It is pure arithmetic on typed
 * arrays: nothing is generated, nothing leaves the machine.
 *
 * The work is done where each kind of work is right:
 *
 *   - light is multiplied in *linear* light, which is what light is, so exposure
 *     and white balance behave like a camera and not like a photocopier;
 *   - tone (highlights, shadows, contrast) is judged on a perceptual lightness,
 *     and applied back to the pixel as a ratio, so a face whose shadows are lifted
 *     keeps its colour rather than going grey;
 *   - highlights and shadows are judged by the *neighbourhood* a pixel is in — an
 *     edge-aware base from a guided filter, worked out once at low resolution —
 *     so lifting the shadows on a backlit face lifts the face and not a halo round
 *     it, and the texture of the skin comes through unchanged;
 *   - colour (vibrance, saturation, the mixer, black and white, toning) is done
 *     in OKLab, where hue, colour and lightness are independent, and vibrance
 *     knows where skin sits on the hue circle and leaves it nearly alone;
 *   - curves act on the encoded values, the way everybody who has used one expects;
 *   - sharpening acts on lightness alone, so edges never grow coloured fringes.
 *
 * A 24-megapixel photograph is worked in strips a hundred rows tall, each with the
 * rows its neighbourhood needs above and below. Nothing the size of the whole
 * photograph is ever held in floating point, which is what used to take four
 * hundred megabytes for one blur.
 */

import { SRGB_TO_LINEAR } from './colour.mjs';
import { BANDS, CHANNELS, bandKey, effective, isIdentity, isStraight } from './recipe.mjs';

const LIN = SRGB_TO_LINEAR;

/* -- encoding, both ways, by table ------------------------------------------ */

const STEPS = 4096;
const TO_LINEAR = new Float32Array(STEPS + 2);
const TO_DISPLAY = new Float32Array(STEPS + 2);
for (let i = 0; i <= STEPS + 1; i++) {
  const v = Math.min(1, i / STEPS);
  TO_LINEAR[i] = v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
  TO_DISPLAY[i] = v <= 0.0031308 ? v * 12.92 : 1.055 * v ** (1 / 2.4) - 0.055;
}
const lookup = (table, v) => {
  if (!(v > 0)) return 0;
  if (v >= 1) return table[STEPS];
  const f = v * STEPS, i = f | 0;
  return table[i] + (table[i + 1] - table[i]) * (f - i);
};
/** Linear light → the encoded value, 0..1, extended smoothly past white for the tone work. */
export const toDisplay = (v) => (v <= 1 ? lookup(TO_DISPLAY, v) : 1.055 * v ** (1 / 2.4) - 0.055);
/** The encoded value → linear light. */
export const toLinear = (v) => (v <= 1 ? lookup(TO_LINEAR, v) : ((v + 0.055) / 1.055) ** 2.4);

const YR = 0.2126, YG = 0.7152, YB = 0.0722;
const clamp01 = (v) => (v < 0 ? 0 : v > 1 ? 1 : v);
const smooth = (e0, e1, x) => {
  const t = clamp01((x - e0) / (e1 - e0));
  return t * t * (3 - 2 * t);
};

/** Mid-grey, encoded: where contrast pivots. */
const PIVOT = 0.46;

/* -- white balance ------------------------------------------------------------ */

/**
 * The gains warmth and tint put on linear red, green and blue. Warmth trades blue
 * for red, tint trades green for magenta (positive is magenta, as the label says),
 * and the three are scaled together so a grey keeps its brightness: white balance
 * changes the colour of the light, not how much of it there is.
 */
export function whiteBalance(warmth = 0, tint = 0) {
  const w = warmth / 100, t = tint / 100;
  const r = 2 ** (0.9 * w + 0.3 * t), g = 2 ** (-0.6 * t), b = 2 ** (-0.9 * w + 0.3 * t);
  const y = YR * r + YG * g + YB * b;
  return [r / y, g / y, b / y];
}

/* -- OKLab ---------------------------------------------------------------------- */

/** Linear sRGB → OKLab, into `out` ([L, a, b]). */
export function oklab(r, g, b, out = [0, 0, 0]) {
  const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
  const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
  const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);
  out[0] = 0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s;
  out[1] = 1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s;
  out[2] = 0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s;
  return out;
}

/** OKLab → linear sRGB, into `out`. */
export function fromOklab(L, a, b, out = [0, 0, 0]) {
  const l_ = L + 0.3963377774 * a + 0.2158037573 * b;
  const m_ = L - 0.1055613458 * a - 0.0638541728 * b;
  const s_ = L - 0.0894841775 * a - 1.2914855480 * b;
  const l = l_ * l_ * l_, m = m_ * m_ * m_, s = s_ * s_ * s_;
  out[0] = 4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s;
  out[1] = -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s;
  out[2] = -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s;
  return out;
}

/**
 * Where each band of the mixer sits on OKLab's hue circle, in degrees: the hue of
 * a pure red, orange, yellow and so on. Brown skin of every complexion falls
 * between red and orange, nearest orange.
 */
export const BAND_HUES = [29, 55, 101, 142, 195, 262, 298, 341];
/** The centre of skin on the same circle, and how far either side of it skin goes. */
export const SKIN_HUE = 52, SKIN_SPREAD = 20;

const RAD = Math.PI / 180;

/**
 * Which two bands of the mixer a hue lies between, and how much of the lower one
 * it belongs to (the shares add to one), for every tenth of a degree: packed as
 * `lower << 16 | share·65535`, so a pixel reads one number from a table.
 */
const BAND_TABLE = (() => {
  const n = BAND_HUES.length, table = new Int32Array(3600);
  for (let i = 0; i < 3600; i++) {
    const h = i / 10;
    let lower = n - 1;
    for (let k = 0; k < n; k++) if (h >= BAND_HUES[k]) lower = k;
    const from = BAND_HUES[lower];
    const to = lower + 1 < n ? BAND_HUES[lower + 1] : BAND_HUES[0] + 360;
    const t = ((h - from + 360) % 360) / (to - from);
    table[i] = (lower << 16) | Math.round((0.5 + 0.5 * Math.cos(Math.PI * t)) * 0xffff);
  }
  return table;
})();
const bandAt = (hueDeg) => BAND_TABLE[((((hueDeg % 360) + 360) % 360) * 10) | 0];

/** How much of each band of the mixer a hue belongs to. */
export function bandWeights(hueDeg) {
  const packed = bandAt(hueDeg), lower = packed >> 16, w = (packed & 0xffff) / 0xffff;
  const out = new Array(BAND_HUES.length).fill(0);
  out[lower] = w; out[(lower + 1) % BAND_HUES.length] += 1 - w;
  return out;
}

/** How much a colour looks like skin, 0..1, from its OKLab hue and colourfulness. */
export function skinness(hueDeg, chroma) {
  let d = Math.abs(hueDeg - SKIN_HUE) % 360;
  if (d > 180) d = 360 - d;
  const q = d / SKIN_SPREAD, byHue = Math.exp(-q * q);
  // Skin is never grey and never neon.
  return byHue * smooth(0.012, 0.035, chroma) * (1 - smooth(0.16, 0.24, chroma));
}

const SKIN_A = Math.cos(SKIN_HUE * RAD), SKIN_B = Math.sin(SKIN_HUE * RAD);
const SKIN_WIDTH = 2 / ((SKIN_SPREAD * RAD) ** 2);
/** `skinness`, from a and b directly: 1 − cos of the angle is half its square, near enough, where it matters. */
function skinAB(a, b, chroma) {
  if (chroma < 0.012 || chroma > 0.24) return 0;
  const cos = (a * SKIN_A + b * SKIN_B) / chroma;
  return Math.exp(-(1 - cos) * SKIN_WIDTH) * smooth(0.012, 0.035, chroma) * (1 - smooth(0.16, 0.24, chroma));
}

/* -- curves --------------------------------------------------------------------- */

const LUT_SIZE = 1024;

/** A monotone cubic through the points (Fritsch–Carlson): smooth, and never overshooting between them. */
export function curveTable(points) {
  const n = points.length, xs = points.map((p) => p[0]), ys = points.map((p) => p[1]);
  const d = [], m = new Array(n);
  for (let k = 0; k < n - 1; k++) d.push((ys[k + 1] - ys[k]) / Math.max(1e-6, xs[k + 1] - xs[k]));
  m[0] = d[0]; m[n - 1] = d[n - 2];
  for (let k = 1; k < n - 1; k++) m[k] = d[k - 1] * d[k] <= 0 ? 0 : (d[k - 1] + d[k]) / 2;
  for (let k = 0; k < n - 1; k++) {
    if (d[k] === 0) { m[k] = 0; m[k + 1] = 0; continue; }
    const a = m[k] / d[k], b = m[k + 1] / d[k], s = a * a + b * b;
    if (s > 9) { const t = 3 / Math.sqrt(s); m[k] = t * a * d[k]; m[k + 1] = t * b * d[k]; }
  }
  const table = new Float32Array(LUT_SIZE + 1);
  let k = 0;
  for (let i = 0; i <= LUT_SIZE; i++) {
    const x = i / LUT_SIZE;
    if (x <= xs[0]) { table[i] = ys[0]; continue; }
    if (x >= xs[n - 1]) { table[i] = ys[n - 1]; continue; }
    while (k < n - 2 && x > xs[k + 1]) k++;
    const h = xs[k + 1] - xs[k], t = (x - xs[k]) / h, t2 = t * t, t3 = t2 * t;
    table[i] = clamp01((2 * t3 - 3 * t2 + 1) * ys[k] + (t3 - 2 * t2 + t) * h * m[k]
      + (-2 * t3 + 3 * t2) * ys[k + 1] + (t3 - t2) * h * m[k + 1]);
  }
  return table;
}

const readTable = (table, v) => {
  if (!(v > 0)) return table[0];
  if (v >= 1) return table[LUT_SIZE];
  const f = v * LUT_SIZE, i = f | 0;
  return table[i] + (table[i + 1] - table[i]) * (f - i);
};

/** One table per channel for everything the curves ask: the look's (master, then channel), then the person's. */
function curveTables(r) {
  const stages = [r.lookCurve, r.curve].filter((c) => c && !CHANNELS.every((ch) => isStraight(c[ch])));
  if (!stages.length) return null;
  const tables = stages.map((c) => Object.fromEntries(CHANNELS.map((ch) => [ch, isStraight(c[ch]) ? null : curveTable(c[ch])])));
  return ['r', 'g', 'b'].map((ch) => {
    const out = new Float32Array(LUT_SIZE + 1);
    for (let i = 0; i <= LUT_SIZE; i++) {
      let v = i / LUT_SIZE;
      for (const t of tables) {
        if (t.rgb) v = readTable(t.rgb, v);
        if (t[ch]) v = readTable(t[ch], v);
      }
      out[i] = v;
    }
    return out;
  });
}

/* -- planes: box filters, guided filters, sampling ----------------------------- */

/** The mean over a (2r+1)² window, the window clipped at the edges. */
function box(src, w, h, r) {
  const tmp = new Float32Array(w * h), out = new Float32Array(w * h);
  for (let y = 0; y < h; y++) {
    const row = y * w;
    let sum = 0, count = 0;
    for (let x = 0; x <= Math.min(r, w - 1); x++) { sum += src[row + x]; count++; }
    for (let x = 0; x < w; x++) {
      tmp[row + x] = sum / count;
      const add = x + r + 1, drop = x - r;
      if (add < w) { sum += src[row + add]; count++; }
      if (drop >= 0) { sum -= src[row + drop]; count--; }
    }
  }
  for (let x = 0; x < w; x++) {
    let sum = 0, count = 0;
    for (let y = 0; y <= Math.min(r, h - 1); y++) { sum += tmp[y * w + x]; count++; }
    for (let y = 0; y < h; y++) {
      out[y * w + x] = sum / count;
      const add = y + r + 1, drop = y - r;
      if (add < h) { sum += tmp[add * w + x]; count++; }
      if (drop >= 0) { sum -= tmp[drop * w + x]; count--; }
    }
  }
  return out;
}

/**
 * He's guided filter, returning its coefficient planes: the filtered value of a
 * pixel is `a·guide + b`. Kept as coefficients so they can be worked out small
 * and applied large ("fast guided filter"): the edges come from the full-size
 * guide, and only the smooth coefficients were ever at low resolution.
 */
function guided(I, p, w, h, r, eps) {
  const n = w * h;
  const II = new Float32Array(n), Ip = new Float32Array(n);
  for (let i = 0; i < n; i++) { II[i] = I[i] * I[i]; Ip[i] = I[i] * p[i]; }
  const mI = box(I, w, h, r), mp = I === p ? mI : box(p, w, h, r);
  const mII = box(II, w, h, r), mIp = I === p ? mII : box(Ip, w, h, r);
  const a = new Float32Array(n), b = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    const varI = mII[i] - mI[i] * mI[i], cov = mIp[i] - mI[i] * mp[i];
    a[i] = cov / (varI + eps);
    b[i] = mp[i] - a[i] * mI[i];
  }
  return { a: box(a, w, h, r), b: box(b, w, h, r) };
}

/** A running minimum over a (2r+1)² window. */
function minFilter(src, w, h, r) {
  const tmp = new Float32Array(w * h), out = new Float32Array(w * h);
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      let m = Infinity;
      for (let k = Math.max(0, x - r); k <= Math.min(w - 1, x + r); k++) m = Math.min(m, src[y * w + k]);
      tmp[y * w + x] = m;
    }
  }
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      let m = Infinity;
      for (let k = Math.max(0, y - r); k <= Math.min(h - 1, y + r); k++) m = Math.min(m, tmp[k * w + x]);
      out[y * w + x] = m;
    }
  }
  return out;
}

/** Where each full-size column falls on the small grid, for bilinear sampling. */
function axis(full, small) {
  const i0 = new Int32Array(full), i1 = new Int32Array(full), t = new Float32Array(full);
  const f = small / full;
  for (let x = 0; x < full; x++) {
    const s = Math.min(small - 1, Math.max(0, (x + 0.5) * f - 0.5));
    i0[x] = Math.floor(s); i1[x] = Math.min(small - 1, i0[x] + 1); t[x] = s - i0[x];
  }
  return { i0, i1, t };
}

/* -- what the recipe asks, worked out once --------------------------------------- */

function plan(recipe, W, H, opts) {
  const r = effective(recipe);
  const fullWidth = opts.fullWidth || W;
  const scale = Math.min(1, W / fullWidth);                    // this render's pixels per full-size pixel
  const [wr, wg, wb] = whiteBalance(r.warmth, r.tint);
  const ev = 2 ** (r.exposure / 40);                            // ±2.5 stops
  const hsl = BANDS.map((b) => [r[bandKey('hue', b)] / 100, r[bandKey('sat', b)] / 100, r[bandKey('lum', b)] / 100]);
  const mono = clamp01(r.mono);
  const toning = r.toneHiSat > 0 || r.toneShSat > 0;
  const p = {
    r, W, H, scale,
    gain: [wr * ev, wg * ev, wb * ev],
    dehaze: r.dehaze / 100,
    shadows: r.shadows / 100, highlights: r.highlights / 100,
    whites: r.whites / 100, blacks: r.blacks / 100, contrast: r.contrast / 100,
    clarity: r.clarity / 100,
    vibrance: r.vibrance / 100, saturation: r.saturation / 100,
    hsl, anyHsl: hsl.some(([a, b, c]) => a || b || c), mono,
    toning,
    toneHi: [Math.cos(r.toneHiHue * RAD) * r.toneHiSat / 100 * 0.07, Math.sin(r.toneHiHue * RAD) * r.toneHiSat / 100 * 0.07],
    toneSh: [Math.cos(r.toneShHue * RAD) * r.toneShSat / 100 * 0.07, Math.sin(r.toneShHue * RAD) * r.toneShSat / 100 * 0.07],
    toneBalance: r.toneBalance / 100,
    curves: curveTables(r),
    vignette: r.vignette / 100,
    vignetteStart: 0.2 + r.vignetteMidpoint / 100 * 0.6,
    vignetteWidth: 0.1 + r.vignetteFeather / 100 * 0.8,
    grain: r.grain / 100 * 0.1,
    grainCell: Math.max(0.5, (0.7 + r.grainSize / 100 * 2.3) * Math.max(W, H) / 2000),
    sharpen: r.sharpen / 100 * 1.4,
    sharpenSigma: Math.max(0.5, r.sharpenRadius / 10 * scale),
    sharpenMask: r.sharpenMasking / 100 * 0.12,
    noise: r.noise / 100, colourNoise: r.colourNoise / 100,
  };
  p.tone = !!(p.shadows || p.highlights || p.whites || p.blacks || p.contrast || p.clarity);
  p.colour = !!(p.vibrance || p.saturation || p.anyHsl || p.mono || p.toning);
  p.maps = !!(p.shadows || p.highlights || p.clarity || p.dehaze);
  p.noiseRadius = Math.max(1, Math.round(2 * scale));
  p.colourRadius = Math.max(1, Math.round((2 + 6 * p.colourNoise) * scale));
  p.nrHalo = (p.noise ? 2 * p.noiseRadius : 0) + (p.colourNoise ? 2 * p.colourRadius : 0) + 1;
  p.sharpHalo = Math.ceil(p.sharpenSigma * 3) + 2;
  return p;
}

/**
 * The low-resolution maps: the neighbourhood lightness for highlights and shadows,
 * a finer one for clarity, and the haze. Made from a copy about four hundred
 * pixels across, whatever size the photograph is, so the preview and the export
 * judge the same neighbourhoods.
 */
function buildMaps(src, W, H, p) {
  const f = Math.max(1, Math.floor(Math.max(W, H) / 400));
  const w = Math.ceil(W / f), h = Math.ceil(H / f), n = w * h;
  const R = new Float32Array(n), G = new Float32Array(n), B = new Float32Array(n), count = new Float32Array(n);
  for (let y = 0; y < H; y++) {
    const sy = (y / f) | 0;
    for (let x = 0; x < W; x++) {
      const i = (y * W + x) * 4, j = sy * w + ((x / f) | 0);
      R[j] += LIN[src[i]]; G[j] += LIN[src[i + 1]]; B[j] += LIN[src[i + 2]]; count[j]++;
    }
  }
  const [gr, gg, gb] = p.gain;
  const Y = new Float32Array(n);
  for (let j = 0; j < n; j++) {
    R[j] = R[j] / count[j] * gr; G[j] = G[j] / count[j] * gg; B[j] = B[j] / count[j] * gb;
    Y[j] = YR * R[j] + YG * G[j] + YB * B[j];
  }
  const maps = { w, h, xs: axis(W, w), ys: axis(H, h) };
  const long = Math.max(w, h);

  if (p.dehaze) {
    // Dark-channel haze (He, Sun and Tang): in a clear photograph almost every
    // neighbourhood has something nearly black in some channel; haze is what
    // lifts that floor. The air's own colour is taken from the haziest places.
    const rd = Math.max(2, Math.round(long / 70));
    const dark = new Float32Array(n);
    for (let j = 0; j < n; j++) dark[j] = Math.min(R[j], G[j], B[j]);
    const dc = minFilter(dark, w, h, rd);
    const order = Array.from(dc.keys()).sort((a, b) => dc[b] - dc[a]).slice(0, Math.max(1, Math.round(n * 0.002)));
    const A = [0, 0, 0];
    for (const j of order) { A[0] += R[j]; A[1] += G[j]; A[2] += B[j]; }
    for (let c = 0; c < 3; c++) A[c] = Math.min(1, Math.max(0.05, A[c] / order.length));
    for (let j = 0; j < n; j++) dark[j] = Math.min(R[j] / A[0], G[j] / A[1], B[j] / A[2]);
    const dn = minFilter(dark, w, h, rd);
    const t = new Float32Array(n);
    for (let j = 0; j < n; j++) t[j] = 1 - 0.9 * dn[j];
    maps.air = A;
    maps.haze = guided(Y, t, w, h, rd * 2, 1e-3);
    // The small copy is dehazed too, so the tone maps see the photograph the tone work will be given.
    for (let j = 0; j < n; j++) {
      const tt = maps.haze.a[j] * Y[j] + maps.haze.b[j];
      const v = dehazed(R[j], G[j], B[j], tt, A, p.dehaze, [0, 0, 0]);
      R[j] = Math.max(0, v[0]); G[j] = Math.max(0, v[1]); B[j] = Math.max(0, v[2]);
      Y[j] = YR * R[j] + YG * G[j] + YB * B[j];
    }
  }
  if (p.shadows || p.highlights || p.clarity) {
    const L = new Float32Array(n);
    for (let j = 0; j < n; j++) L[j] = toDisplay(Math.max(0, Y[j]));
    // Wide and edge-aware for highlights and shadows: a window a twentieth of
    // the photograph, that stops at any edge with more than about a seventh of
    // the tonal range across it — a face against a window, say.
    if (p.shadows || p.highlights) maps.base = guided(L, L, w, h, Math.max(2, Math.round(long / 20)), 0.02);
    // Finer and softer-edged for clarity, which is about texture and form.
    if (p.clarity) maps.detail = guided(L, L, w, h, Math.max(1, Math.round(long / 70)), 0.006);
  }
  return maps;
}

/** Haze taken out of one linear colour (k > 0) or laid over it (k < 0), into `out`. */
function dehazed(r, g, b, t, A, k, out) {
  if (k > 0) {
    const tt = Math.max(0.2, Math.min(1, t));
    out[0] = r + k * ((r - A[0]) / tt + A[0] - r);
    out[1] = g + k * ((g - A[1]) / tt + A[1] - g);
    out[2] = b + k * ((b - A[2]) / tt + A[2] - b);
    return out;
  }
  const v = -k * 0.55;
  out[0] = r + v * (A[0] - r); out[1] = g + v * (A[1] - g); out[2] = b + v * (A[2] - b);
  return out;
}

/** A small map, read bilinearly at a full-size pixel whose place on it has been worked out. */
function sample(plane, sy0, sy1, sty, sx0, sx1, stx) {
  const top = plane[sy0 + sx0] + (plane[sy0 + sx1] - plane[sy0 + sx0]) * stx;
  const bottom = plane[sy1 + sx0] + (plane[sy1 + sx1] - plane[sy1 + sx0]) * stx;
  return top + (bottom - top) * sty;
}

/* -- small helpers used per strip ----------------------------------------------- */

function hash(x, y, seed) {
  let h = (Math.imul(x, 374761393) + Math.imul(y, 668265263) + Math.imul(seed, 2147483647)) | 0;
  h = Math.imul(h ^ (h >>> 13), 1274126177);
  h ^= h >>> 16;
  return (h >>> 0) / 4294967296;
}

/** Film-like grain: smooth noise a cell or two across, the same in every channel. */
function grainAt(x, y, cell) {
  const gx = x / cell, gy = y / cell, x0 = Math.floor(gx), y0 = Math.floor(gy), tx = gx - x0, ty = gy - y0;
  const a = hash(x0, y0, 7), b = hash(x0 + 1, y0, 7), c = hash(x0, y0 + 1, 7), d = hash(x0 + 1, y0 + 1, 7);
  return (a + (b - a) * tx) * (1 - ty) + (c + (d - c) * tx) * ty - 0.5;
}

function gaussianKernel(sigma) {
  const r = Math.ceil(sigma * 3), k = new Float32Array(2 * r + 1);
  let sum = 0;
  for (let i = -r; i <= r; i++) { k[i + r] = Math.exp(-(i * i) / (2 * sigma * sigma)); sum += k[i + r]; }
  for (let i = 0; i < k.length; i++) k[i] /= sum;
  return k;
}

/**
 * Noise reduction on the encoded values of a strip. Lightness is smoothed by a
 * guided filter, which flattens grain in flat places and leaves edges; colour
 * noise — the red and green blotches of a phone in a dim room — is blurred more
 * widely, because the eye reads colour far more coarsely than lightness.
 */
function denoise(R, G, B, w, h, p) {
  const n = w * h, Y = new Float32Array(n);
  for (let i = 0; i < n; i++) Y[i] = YR * R[i] + YG * G[i] + YB * B[i];
  let Yn = Y;
  if (p.noise) {
    const eps = (0.01 + 0.05 * p.noise) ** 2;
    const { a, b } = guided(Y, Y, w, h, p.noiseRadius, eps);
    const k = Math.min(1, p.noise * 1.6);
    Yn = new Float32Array(n);
    for (let i = 0; i < n; i++) Yn[i] = Y[i] + k * (a[i] * Y[i] + b[i] - Y[i]);
  }
  let CR = null, CB = null;
  if (p.colourNoise) {
    const cr = new Float32Array(n), cb = new Float32Array(n);
    for (let i = 0; i < n; i++) { cr[i] = R[i] - Y[i]; cb[i] = B[i] - Y[i]; }
    const k = Math.min(1, p.colourNoise * 1.5);
    const br = box(box(cr, w, h, p.colourRadius), w, h, p.colourRadius);
    const bb = box(box(cb, w, h, p.colourRadius), w, h, p.colourRadius);
    CR = cr; CB = cb;
    for (let i = 0; i < n; i++) { CR[i] += k * (br[i] - cr[i]); CB[i] += k * (bb[i] - cb[i]); }
  }
  for (let i = 0; i < n; i++) {
    const y = Yn[i];
    const r = CR ? y + CR[i] : R[i] + (y - Y[i]);
    const b = CB ? y + CB[i] : B[i] + (y - Y[i]);
    R[i] = r; B[i] = b; G[i] = (y - YR * r - YB * b) / YG;
  }
}

/* -- the engine ----------------------------------------------------------------- */

const STRIP = 96;

/**
 * Develop, a strip at a time, yielding after each so a caller on a busy thread
 * can breathe. `develop()` and `developAsync()` below are the usual ways in.
 *
 * @param src     RGBA bytes
 * @param recipe  a recipe (recipe.mjs); a look in it is laid over the sliders
 * @param opts.fullWidth  the width of the full-size photograph this is a preview
 *                        of, so the fine-grained work (sharpening, noise) is sized
 *                        to the real photograph and not to the preview
 * @param opts.defer      leave the vignette and grain for `finish()`, when
 *                        something (the faces, a brush) is to be done in between
 */
export function* developSteps(src, W, H, recipe, opts = {}) {
  const out = new Uint8ClampedArray(src.length);
  if (isIdentity(recipe)) { out.set(src); return out; }
  const state = setup(src, W, H, recipe, opts, out);
  for (let y0 = 0; y0 < H; y0 += STRIP) {
    strip(state, y0);
    yield Math.min(H, y0 + STRIP) / H;
  }
  return out;
}

/** Everything a strip needs that does not change from strip to strip. */
function setup(src, W, H, recipe, opts, out) {
  const p = plan(recipe, W, H, opts);
  const maps = p.maps ? buildMaps(src, W, H, p) : null;
  const defer = !!opts.defer;
  const vignette = !defer && p.vignette, grain = !defer && p.grain;
  const [gr, gg, gb] = p.gain;
  const curves = p.curves;
  const nr = !!(p.noise || p.colourNoise);
  const hs = p.sharpen ? p.sharpHalo : 0, hn = nr ? p.nrHalo : 0;
  const cap = (STRIP + 2 * (hs + hn)) * W;
  const R = new Float32Array(cap), G = new Float32Array(cap), B = new Float32Array(cap);
  const lab = [0, 0, 0], rgb = [0, 0, 0];
  const kernel = p.sharpen ? gaussianKernel(p.sharpenSigma) : null;
  const seed = opts.seed | 0;
  return { src, out, W, H, p, maps, vignette, grain, gr, gg, gb, curves, nr, hs, hn, R, G, B, lab, rgb, kernel, seed };
}

/** One strip of rows, from bytes in to bytes out. A plain function, not the generator: V8 optimises it far better. */
function strip(s, y0) {
  const { src, W, H, p, nr, hs, hn, R, G, B } = s;
  const y1 = Math.min(H, y0 + STRIP);
  const a0 = Math.max(0, y0 - hs - hn), a1 = Math.min(H, y1 + hs + hn);
  const b0 = Math.max(0, y0 - hs), b1 = Math.min(H, y1 + hs);

  if (nr) {
    // Noise is taken out of the encoded values first, before any tone work can amplify it.
    for (let y = a0; y < a1; y++) {
      for (let x = 0, i = y * W * 4, j = (y - a0) * W; x < W; x++, i += 4, j++) {
        R[j] = src[i] / 255; G[j] = src[i + 1] / 255; B[j] = src[i + 2] / 255;
      }
    }
    denoise(R, G, B, W, a1 - a0, p);
  }

  developPixels(s, y1, a0, b0, b1);
  if (p.sharpen) sharpen(s, y0, y1, a0, b0, b1);
  write(s, y0, y1, a0);
}

/** Every pixel of the strip by itself: light, haze, tone, colour, the lens, the gamut, the curves. */
function developPixels(s, y1, a0, b0, b1) {
  const { src, W, H, p, maps, vignette, gr, gg, gb, curves, nr, R, G, B, lab, rgb } = s;
  // Read once into locals: the loop below runs for every pixel of the photograph.
  const { anyHsl, blacks, clarity, colour, contrast, dehaze, highlights, hsl, mono, saturation, shadows, tone, toneBalance, toneHi, toneSh, toning, vibrance, vignetteStart, vignetteWidth, whites } = p;
  const vignetteAmount = p.vignette;
  for (let y = b0; y < b1; y++) {
    let sy0 = 0, sy1 = 0, sty = 0;
    if (maps) { sy0 = maps.ys.i0[y] * maps.w; sy1 = maps.ys.i1[y] * maps.w; sty = maps.ys.t[y]; }
    for (let x = 0, j = (y - a0) * W; x < W; x++, j++) {
      const i = (y * W + x) * 4;
      let r, g, b;
      if (nr) { r = toLinear(R[j]); g = toLinear(G[j]); b = toLinear(B[j]); }
      else { r = LIN[src[i]]; g = LIN[src[i + 1]]; b = LIN[src[i + 2]]; }

      // 1. the light: white balance and exposure, as a camera would.
      r *= gr; g *= gg; b *= gb;

      let sx0 = 0, sx1 = 0, stx = 0;
      if (maps) { sx0 = maps.xs.i0[x]; sx1 = maps.xs.i1[x]; stx = maps.xs.t[x]; }

      // 2. haze, taken out (or put in) against the colour of the air.
      if (dehaze && maps.haze) {
        const lum = YR * r + YG * g + YB * b;
        const t = sample(maps.haze.a, sy0, sy1, sty, sx0, sx1, stx) * lum + sample(maps.haze.b, sy0, sy1, sty, sx0, sx1, stx);
        dehazed(r, g, b, t, maps.air, dehaze, rgb);
        r = rgb[0] > 0 ? rgb[0] : 0; g = rgb[1] > 0 ? rgb[1] : 0; b = rgb[2] > 0 ? rgb[2] : 0;
      }

      // 3. tone, on lightness, handed back to the pixel as a ratio.
      if (tone) {
        const Y = YR * r + YG * g + YB * b;
        const L0 = toDisplay(Y);
        let L = L0;
        if (maps && maps.base) {
          const base = sample(maps.base.a, sy0, sy1, sty, sx0, sx1, stx) * L0 + sample(maps.base.b, sy0, sy1, sty, sx0, sx1, stx);
          // Shadows lift what lives in a dark neighbourhood and fade out by
          // the midtones; true black (a neighbourhood with nothing in it) stays black.
          if (shadows) L += shadows * 0.3 * (1 - smooth(0.05, 0.6, base)) * smooth(0, 0.06, base);
          if (highlights) L += highlights * 0.25 * smooth(0.4, 1.0, base);
        }
        if (whites) L += whites * 0.16 * smooth(0.5, 1.0, L);
        if (blacks) L += blacks * 0.1 * (1 - smooth(0, 0.45, L));
        if (contrast) {
          const c = clamp01(L);
          L += contrast * 1.6 * c * (1 - c) * (c - PIVOT);
        }
        if (clarity && maps && maps.detail) {
          const local = sample(maps.detail.a, sy0, sy1, sty, sx0, sx1, stx) * L0 + sample(maps.detail.b, sy0, sy1, sty, sx0, sx1, stx);
          const m2 = 2 * clamp01(L) - 1, mid = 1 - m2 * m2;            // held back at both ends: halos in the light, noise in the dark
          L += (clarity > 0 ? 0.9 : 1) * clarity * (L0 - local) * mid;
        }
        const target = toLinear(Math.max(0, L));
        if (Y > 1e-5) {
          const k = Math.min(64, target / Y);
          r *= k; g *= k; b *= k;
        } else {
          r = g = b = target;
        }
      }

      // 4. colour, in OKLab. Chroma is scaled on a and b directly; the hue
      //    angle is only worked out when the mixer needs to know it.
      if (colour) {
        oklab(r > 0 ? r : 0, g > 0 ? g : 0, b > 0 ? b : 0, lab);
        let Lk = lab[0], A = lab[1], Bb = lab[2];
        const C = Math.sqrt(A * A + Bb * Bb);
        let k = 1;
        if (vibrance) {
          // Muted colour is lifted most and strong colour hardly at all; skin
          // is held back in both directions, so it neither goes orange nor ashen.
          const skin = skinAB(A, Bb, C);
          const room = 1 - smooth(0, 0.2, C);
          k *= 1 + vibrance * (vibrance > 0 ? room : 0.8) * (1 - 0.95 * skin);
        }
        if (saturation) k *= 1 + saturation;
        if (anyHsl || mono) {
          const band = bandAt(Math.atan2(Bb, A) / RAD);
          const lower = band >> 16, upper = (lower + 1) % BANDS.length, w = (band & 0xffff) / 0xffff;
          const dh = w * hsl[lower][0] + (1 - w) * hsl[upper][0];
          const ds = w * hsl[lower][1] + (1 - w) * hsl[upper][1];
          const dl = w * hsl[lower][2] + (1 - w) * hsl[upper][2];
          if (mono) {
            // Black and white: the mixer's lightness sliders say how light
            // each colour turns out, in proportion to how much colour it had.
            Lk += mono * dl * 0.25 * Math.min(1, C / 0.1);
            k *= 1 - mono;
          }
          if (mono < 1) {
            const kept = 1 - mono;
            if (dh) {
              const turn = kept * dh * 30 * RAD, cs = Math.cos(turn), sn = Math.sin(turn);
              const a2 = A * cs - Bb * sn;
              Bb = A * sn + Bb * cs; A = a2;
            }
            k *= 1 + kept * ds;
            Lk += kept * dl * 0.12 * smooth(0.01, 0.06, C);
          }
        }
        if (k < 0) k = 0;
        A *= k; Bb *= k;
        if (toning) {
          const hi = smooth(0.25, 0.85, Lk - toneBalance * 0.3);
          A += toneHi[0] * hi + toneSh[0] * (1 - hi);
          Bb += toneHi[1] * hi + toneSh[1] * (1 - hi);
        }
        fromOklab(Lk > 0 ? Lk : 0, A, Bb, rgb);
        r = rgb[0]; g = rgb[1]; b = rgb[2];
      }

      // 5. the lens: a vignette, darkening (or lightening) the corners.
      if (vignette) {
        const vx = (x + 0.5) / W * 2 - 1, vy = (y + 0.5) / H * 2 - 1;
        const d = Math.sqrt((vx * vx + vy * vy) / 2);
        const k = smooth(vignetteStart - vignetteWidth / 2, vignetteStart + vignetteWidth / 2, d);
        const f = vignetteAmount > 0 ? 1 - 0.8 * vignetteAmount * k : 1 - 0.9 * vignetteAmount * k;
        r *= f; g *= f; b *= f;
      }

      // 6. into the gamut, keeping lightness and hue: a colour too bright or
      //    too strong to show gives up colour, never turns another colour.
      const Y = YR * r + YG * g + YB * b;
      const hi = r > g ? (r > b ? r : b) : (g > b ? g : b), lo = r < g ? (r < b ? r : b) : (g < b ? g : b);
      if (Y >= 1) { r = g = b = 1; }
      else if (Y <= 0) { r = g = b = 0; }
      else {
        let k = 1;
        if (hi > 1) k = Math.min(k, (1 - Y) / (hi - Y));
        if (lo < 0) k = Math.min(k, Y / (Y - lo));
        if (k < 1) { r = Y + (r - Y) * k; g = Y + (g - Y) * k; b = Y + (b - Y) * k; }
      }

      // 7. encoded, then the curves.
      let er = toDisplay(r), eg = toDisplay(g), eb = toDisplay(b);
      if (curves) { er = readTable(curves[0], er); eg = readTable(curves[1], eg); eb = readTable(curves[2], eb); }
      R[j] = er; G[j] = eg; B[j] = eb;
    }
  }

}

/** Sharpening: lightness against its own blur, where there are edges. */
function sharpen(s, y0, y1, a0, b0, b1) {
  const { W, p, R, G, B, kernel } = s;
  /* -- sharpening: lightness against its own blur, where there are edges ----- */
  if (p.sharpen) {
    const top = b0, count = b1 - b0, n = count * W, kr = (kernel.length - 1) >> 1;
    const Y = new Float32Array(n), across = new Float32Array(n), blurY = new Float32Array(n);
    for (let y = 0; y < count; y++) {
      for (let x = 0, j = (y + top - a0) * W, k = y * W; x < W; x++, j++, k++) Y[k] = YR * R[j] + YG * G[j] + YB * B[j];
    }
    for (let y = 0; y < count; y++) {
      for (let x = 0; x < W; x++) {
        let sum = 0;
        for (let t = -kr; t <= kr; t++) sum += kernel[t + kr] * Y[y * W + Math.min(W - 1, Math.max(0, x + t))];
        across[y * W + x] = sum;
      }
    }
    for (let y = y0 - top; y < y1 - top; y++) {
      for (let x = 0; x < W; x++) {
        let sum = 0;
        for (let t = -kr; t <= kr; t++) sum += kernel[t + kr] * across[Math.min(count - 1, Math.max(0, y + t)) * W + x];
        blurY[y * W + x] = sum;
      }
    }
    for (let y = y0 - top; y < y1 - top; y++) {
      const up = Math.max(y0 - top, y - 1), down = Math.min(y1 - top - 1, y + 1);
      for (let x = 0; x < W; x++) {
        const k = y * W + x;
        let mask = 1;
        if (p.sharpenMask) {
          const edge = Math.abs(blurY[k + (x < W - 1 ? 1 : 0)] - blurY[k - (x > 0 ? 1 : 0)])
                     + Math.abs(blurY[down * W + x] - blurY[up * W + x]);
          mask = smooth(p.sharpenMask, p.sharpenMask + 0.04, edge);
        }
        let delta = p.sharpen * (Y[k] - blurY[k]) * mask;
        delta = delta > 0.2 ? 0.2 : delta < -0.2 ? -0.2 : delta;        // no haloes past a fifth of the range
        const j = (y + top - a0) * W + x;
        R[j] += delta; G[j] += delta; B[j] += delta;
      }
    }
  }

}

/** Out, with grain and a whisper of dither against banding. */
function write(s, y0, y1, a0) {
  const { src, out, W, p, grain, R, G, B, seed } = s;
  /* -- out, with grain and a whisper of dither against banding ---------------- */
  for (let y = y0; y < y1; y++) {
    for (let x = 0, i = y * W * 4, j = (y - a0) * W; x < W; x++, i += 4, j++) {
      let er = R[j], eg = G[j], eb = B[j];
      if (grain) {
        const l = clamp01(YR * er + YG * eg + YB * eb);
        const n = grainAt(x, y, p.grainCell) * p.grain * (0.4 + 2.4 * l * (1 - l));
        er += n; eg += n; eb += n;
      }
      const d = (hash(x, y, seed + 1) - hash(x, y, seed + 2)) * 0.3;
      out[i] = er * 255 + d; out[i + 1] = eg * 255 + d; out[i + 2] = eb * 255 + d;
      out[i + 3] = src[i + 3];
    }
  }
}

/** Develop in one go. */
export function develop(src, W, H, recipe, opts = {}) {
  const steps = developSteps(src, W, H, recipe, opts);
  for (;;) {
    const step = steps.next();
    if (step.done) return step.value;
  }
}

/** Develop, giving the thread back between strips — for when there is no worker to hand it to. */
export async function developAsync(src, W, H, recipe, opts = {}) {
  const steps = developSteps(src, W, H, recipe, opts);
  for (;;) {
    const step = steps.next();
    if (step.done) return step.value;
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
}

/**
 * The vignette and grain `develop(…, { defer: true })` left out, done to the
 * finished bytes in place. They come last because both belong to the lens and
 * the film, not to anything done to the picture inside them.
 */
export function finish(pixels, W, H, recipe) {
  const p = plan(recipe, W, H, {});
  if (!p.vignette && !p.grain) return pixels;
  for (let y = 0; y < H; y++) {
    for (let x = 0, i = y * W * 4; x < W; x++, i += 4) {
      let r = LIN[pixels[i]], g = LIN[pixels[i + 1]], b = LIN[pixels[i + 2]];
      if (p.vignette) {
        const vx = (x + 0.5) / W * 2 - 1, vy = (y + 0.5) / H * 2 - 1;
        const d = Math.sqrt((vx * vx + vy * vy) / 2);
        const k = smooth(p.vignetteStart - p.vignetteWidth / 2, p.vignetteStart + p.vignetteWidth / 2, d);
        const f = p.vignette > 0 ? 1 - 0.8 * p.vignette * k : 1 - 0.9 * p.vignette * k;
        r *= f; g *= f; b *= f;
        const Y = YR * r + YG * g + YB * b, hi = Math.max(r, g, b);
        if (Y >= 1) r = g = b = 1;
        else if (hi > 1) { const k2 = (1 - Y) / (hi - Y); r = Y + (r - Y) * k2; g = Y + (g - Y) * k2; b = Y + (b - Y) * k2; }
      }
      let er = toDisplay(r), eg = toDisplay(g), eb = toDisplay(b);
      if (p.grain) {
        const l = clamp01(YR * er + YG * eg + YB * eb);
        const n = grainAt(x, y, p.grainCell) * p.grain * (0.4 + 2.4 * l * (1 - l));
        er += n; eg += n; eb += n;
      }
      const d = (hash(x, y, 3) - hash(x, y, 4)) * 0.3;
      pixels[i] = er * 255 + d; pixels[i + 1] = eg * 255 + d; pixels[i + 2] = eb * 255 + d;
    }
  }
  return pixels;
}

/** How many pixels sit at each level, for red, green, blue and lightness. Every `step`th pixel is counted. */
export function histogram(pixels, step = 1) {
  const r = new Uint32Array(256), g = new Uint32Array(256), b = new Uint32Array(256), l = new Uint32Array(256);
  const stride = 4 * Math.max(1, step | 0);
  for (let i = 0; i < pixels.length; i += stride) {
    if (pixels[i + 3] < 8) continue;
    r[pixels[i]]++; g[pixels[i + 1]]++; b[pixels[i + 2]]++;
    l[Math.round(YR * pixels[i] + YG * pixels[i + 1] + YB * pixels[i + 2])]++;
  }
  return { r, g, b, l };
}
