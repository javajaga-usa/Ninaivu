/* Pixel work stays off the UI thread.
 *
 * Everything here is arithmetic on the pixels you can see. Nothing is
 * generated, nothing is invented, and no image leaves the machine — the
 * brushes arrive as masks the person painted, and the faces as maps the
 * household's own face detector drew, and each adjustment is confined to its own.
 *
 * The order below is the order a darkroom works in and it matters:
 *
 *   1. develop   light, tone, colour, curves and detail: the whole photograph,
 *                by the shared engine (studio/develop.mjs) Sudar uses too
 *   2. brushes   the painted lighten, darken and soften
 *   3. faces     skin and hair, person by person (studio/portrait.mjs)
 *   4. the lens  vignette and grain, last, because they belong to the lens and
 *                the film and not to anything done to the picture inside them
 *
 * Doing colour before tone leaves the white balance fighting the exposure;
 * sharpening before contrast sharpens noise that contrast then amplifies; and
 * retouching faces last-but-one means it works on the look the photograph has
 * ended up with, which is the look being retouched.
 */

import { Maps, portraitPass } from './studio/portrait.mjs';
import { develop, finish, histogram } from './studio/develop.mjs';

/** The maps of the faces, kept between renders: they change when the crop or the paint does, not when a slider does. */
let faces = null;

self.onmessage = ({ data }) => {
  if (data.type === 'maps') {
    const m = data.maps;
    faces = { key: data.key, maps: new Maps({ width: m.width, height: m.height, a: m.a, b: m.b, c: m.c, ids: m.ids, paint: m.paint || {} }) };
    return;
  }
  try {
    render(data);
  } catch (error) {
    self.postMessage({ id: data.id, error: String((error && error.message) || error) });
  }
};

/**
 * A box blur of one rectangle of the picture, run twice — close enough to a
 * Gaussian, and only as large as the rectangle: softening a cheek does not need
 * a blurred copy of the whole photograph.
 */
function blurRegion(px, width, x0, y0, x1, y1, radius) {
  const w = x1 - x0, h = y1 - y0;
  let from = new Uint8ClampedArray(w * h * 4);
  for (let y = 0; y < h; y++) from.set(px.subarray(((y0 + y) * width + x0) * 4, ((y0 + y) * width + x1) * 4), y * w * 4);
  const tmp = new Uint8ClampedArray(from.length), to = new Uint8ClampedArray(from.length);
  const pass = (input, output, horizontal) => {
    const lines = horizontal ? h : w, length = horizontal ? w : h;
    for (let line = 0; line < lines; line++) {
      const at = (k) => (horizontal ? (line * w + k) : (k * w + line)) * 4;
      for (let c = 0; c < 3; c++) {
        let sum = 0, count = 0;
        for (let k = 0; k <= Math.min(radius, length - 1); k++) { sum += input[at(k) + c]; count++; }
        for (let k = 0; k < length; k++) {
          output[at(k) + c] = sum / count;
          if (k + radius + 1 < length) { sum += input[at(k + radius + 1) + c]; count++; }
          if (k - radius >= 0) { sum -= input[at(k - radius) + c]; count--; }
        }
      }
    }
  };
  for (let round = 0; round < 2; round++) {
    pass(from, tmp, true); pass(tmp, to, false);
    from = to.slice();
  }
  return { data: to, w };
}

function render(data) {
  const { id, pixels, width, height, masks, mw, mh, settings: s, portrait, fullWidth, wantHistogram } = data;
  const src = new Uint8ClampedArray(pixels);

  /* Masks arrive as one byte per pixel at preview size; sampled by nearest
   * neighbour because they are already feathered and a second interpolation
   * only softens edges the person deliberately drew. */
  const mask = (name) => (masks[name] ? new Uint8Array(masks[name]) : null);
  const dodge = s.dodgeAmount ? mask('dodge') : null;
  const burn = s.burnAmount ? mask('burn') : null;
  const soften = s.softenAmount ? mask('soften') : null;
  const facesWanted = !!(portrait && faces && faces.key === portrait.mapsKey);
  const between = !!(dodge || burn || soften || facesWanted);

  /* -- 1. develop -------------------------------------------------------- */
  const out = develop(src, width, height, s, { fullWidth: fullWidth || width, defer: between });

  /* -- 2. brushes -------------------------------------------------------- */
  if (dodge || burn || soften) {
    const maskAt = (m, x, y) => m[Math.min(mh - 1, (y * mh / height) | 0) * mw + Math.min(mw - 1, (x * mw / width) | 0)] / 255;
    let blur = null, box = null;
    if (soften) {
      // Only the painted rectangle is blurred, and what is blurred is the
      // developed picture, so softening keeps every other adjustment.
      let mx0 = mw, my0 = mh, mx1 = -1, my1 = -1;
      for (let y = 0; y < mh; y++) {
        for (let x = 0; x < mw; x++) {
          if (!soften[y * mw + x]) continue;
          if (x < mx0) mx0 = x; if (x > mx1) mx1 = x;
          if (y < my0) my0 = y; if (y > my1) my1 = y;
        }
      }
      if (mx1 >= 0) {
        const radius = Math.max(2, Math.round(width / mw * 3));
        box = {
          x0: Math.max(0, Math.floor(mx0 * width / mw) - radius), y0: Math.max(0, Math.floor(my0 * height / mh) - radius),
          x1: Math.min(width, Math.ceil((mx1 + 1) * width / mw) + radius), y1: Math.min(height, Math.ceil((my1 + 1) * height / mh) + radius),
        };
        blur = blurRegion(out, width, box.x0, box.y0, box.x1, box.y1, radius);
      }
    }
    for (let y = 0; y < height; y++) {
      for (let x = 0; x < width; x++) {
        const i = (y * width + x) * 4;
        let r = out[i], g = out[i + 1], b = out[i + 2];
        if (dodge) {
          const k = maskAt(dodge, x, y) * s.dodgeAmount / 100;
          if (k) { r += (255 - r) * k * 0.45; g += (255 - g) * k * 0.45; b += (255 - b) * k * 0.45; }
        }
        if (burn) {
          const k = maskAt(burn, x, y) * s.burnAmount / 100;
          if (k) { r *= 1 - k * 0.45; g *= 1 - k * 0.45; b *= 1 - k * 0.45; }
        }
        if (blur && x >= box.x0 && x < box.x1 && y >= box.y0 && y < box.y1) {
          const k = maskAt(soften, x, y) * s.softenAmount / 100;
          if (k) {
            const j = ((y - box.y0) * blur.w + (x - box.x0)) * 4;
            r += (blur.data[j] - r) * k; g += (blur.data[j + 1] - g) * k; b += (blur.data[j + 2] - b) * k;
          }
        }
        out[i] = r; out[i + 1] = g; out[i + 2] = b;
      }
    }
  }

  /* -- 3. faces: each person's skin and hair, where the maps say they are. -- */
  if (facesWanted) portraitPass({ pixels: out, width, height, maps: faces.maps, portrait });

  /* -- 4. the lens ------------------------------------------------------- */
  if (between) finish(out, width, height, s);

  const counts = wantHistogram ? histogram(out, Math.max(1, Math.floor(width * height / 250_000))) : null;
  self.postMessage({ id, pixels: out.buffer, histogram: counts }, [out.buffer]);
}
