/**
 * Colour, done in the space it is meant to be done in.
 *
 * The Photo Studio used to do its sums on the numbers in the file, which are not
 * light: they are light after a curve that makes dark tones roomy and bright
 * tones cramped. Adding "a stop" to them lifts shadows too little and
 * highlights too much; averaging them darkens every blend; and "saturation"
 * means something different in a shadow than in a sky. The fixes are old:
 *
 *   - exposure is multiplication, and multiplication is only right on *linear*
 *     light — so decode, multiply, re-encode;
 *   - hue, chroma and lightness are only independent in a space built for that
 *     — CIE Lab — so retouching skin (whose lightness, colour and texture each
 *     need their own treatment) is done there.
 *
 * Everything in this file is pure arithmetic on typed arrays, with no DOM, so the
 * same code runs in the editor's worker, in the AI studio's, and under Node for
 * the tests. The Lab it computes agrees with OpenCV's, which is what the server
 * measured the faces in, to a tenth of a unit: a threshold set there means the
 * same thing here.
 */

/** 8-bit sRGB → linear light, as 0..1. */
export const SRGB_TO_LINEAR = (() => {
  const table = new Float32Array(256);
  for (let i = 0; i < 256; i++) {
    const c = i / 255;
    table[i] = c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  }
  return table;
})();

const ENCODE_STEPS = 16384;
/** Linear light → 8-bit sRGB. A table, because `Math.pow` per channel per pixel is the slow part of a retouch. */
const LINEAR_TO_SRGB = (() => {
  const table = new Uint8Array(ENCODE_STEPS + 1);
  for (let i = 0; i <= ENCODE_STEPS; i++) {
    const v = i / ENCODE_STEPS;
    const c = v <= 0.0031308 ? v * 12.92 : 1.055 * v ** (1 / 2.4) - 0.055;
    table[i] = Math.round(c * 255);
  }
  return table;
})();

/** One linear channel, 0..1 (values outside are clipped), to an 8-bit sRGB value. */
export function encode(v) {
  if (v <= 0) return 0;
  if (v >= 1) return 255;
  return LINEAR_TO_SRGB[(v * ENCODE_STEPS + 0.5) | 0];
}

// sRGB → XYZ (D65) and the reference white, as OpenCV writes them, so the Lab
// here and the Lab the server measured in are the same Lab.
const XR = 0.412453, XG = 0.35758, XB = 0.180423;
const YR = 0.212671, YG = 0.71516, YB = 0.072169;
const ZR = 0.019334, ZG = 0.119193, ZB = 0.950227;
const WHITE_X = 0.950456, WHITE_Z = 1.088754;
const EPSILON = 216 / 24389, KAPPA = 24389 / 27;

const fwd = (t) => (t > EPSILON ? Math.cbrt(t) : (KAPPA * t + 16) / 116);
const inv = (f) => {
  const cube = f * f * f;
  return cube > EPSILON ? cube : (116 * f - 16) / KAPPA;
};

/** Relative luminance (0..1) of linear RGB. */
export const luminance = (r, g, b) => YR * r + YG * g + YB * b;

/** CIE lightness (0..100) of a relative luminance. */
export const lightnessOf = (y) => 116 * fwd(y) - 16;

/** The relative luminance that has lightness *l*. */
export const luminanceOf = (l) => inv((l + 16) / 116);

/**
 * Linear RGB planes → Lab planes, for the *n* pixels that have them.
 * `lab` is three Float32Arrays of length n: L in 0..100, a and b roughly ±110.
 */
export function linearToLab(r, g, b, L, A, B, n = r.length) {
  for (let i = 0; i < n; i++) {
    const x = (XR * r[i] + XG * g[i] + XB * b[i]) / WHITE_X;
    const y = YR * r[i] + YG * g[i] + YB * b[i];
    const z = (ZR * r[i] + ZG * g[i] + ZB * b[i]) / WHITE_Z;
    const fx = fwd(x), fy = fwd(y), fz = fwd(z);
    L[i] = 116 * fy - 16;
    A[i] = 500 * (fx - fy);
    B[i] = 200 * (fy - fz);
  }
}

/** Lab planes → linear RGB planes (not clipped: the caller decides what an out-of-gamut colour becomes). */
export function labToLinear(L, A, B, r, g, b, n = L.length) {
  for (let i = 0; i < n; i++) {
    const fy = (L[i] + 16) / 116;
    const x = inv(fy + A[i] / 500) * WHITE_X;
    const y = inv(fy);
    const z = inv(fy - B[i] / 200) * WHITE_Z;
    r[i] = 3.240479 * x - 1.53715 * y - 0.498535 * z;
    g[i] = -0.969256 * x + 1.875991 * y + 0.041556 * z;
    b[i] = 0.055648 * x - 0.204043 * y + 1.057311 * z;
  }
}

/** One sRGB colour (0..255 each) as [L, a, b]. */
export function rgbToLab(red, green, blue) {
  const r = [SRGB_TO_LINEAR[red]], g = [SRGB_TO_LINEAR[green]], b = [SRGB_TO_LINEAR[blue]];
  const L = [0], A = [0], B = [0];
  linearToLab(r, g, b, L, A, B, 1);
  return [L[0], A[0], B[0]];
}

/** One [L, a, b] as sRGB bytes. */
export function labToRgb(l, a, b) {
  const r = [0], g = [0], bl = [0];
  labToLinear([l], [a], [b], r, g, bl, 1);
  return [encode(r[0]), encode(g[0]), encode(bl[0])];
}

/** `#rrggbb` → [r, g, b] bytes, or null if it is not a colour. */
export function parseHex(text) {
  const found = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(String(text || '').trim());
  return found ? [parseInt(found[1], 16), parseInt(found[2], 16), parseInt(found[3], 16)] : null;
}

export const smoothstep = (edge0, edge1, x) => {
  const span = edge1 - edge0;
  if (Math.abs(span) < 1e-9) return x >= edge1 ? 1 : 0;
  const t = Math.min(1, Math.max(0, (x - edge0) / span));
  return t * t * (3 - 2 * t);
};
