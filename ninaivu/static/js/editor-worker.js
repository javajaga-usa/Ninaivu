/* Pixel work stays off the UI thread.
 *
 * Everything here is arithmetic on the pixels you can see. Nothing is
 * generated, nothing is invented, and no image leaves the machine — the
 * brushes arrive as masks the person painted, and the faces as maps the
 * household's own face detector drew, and each adjustment is confined to its own.
 *
 * The order below is the order a darkroom works in and it matters:
 *
 *   1. tone      exposure, then the recovery sliders, then contrast
 *   2. colour    white balance, then vibrance/saturation
 *   3. detail    clarity and sharpening, which want final tone underneath
 *   4. brushes   the painted dodge, burn and soften
 *   5. faces     skin and hair, person by person (studio/portrait.mjs)
 *   6. vignette  last, because it belongs to the lens
 *
 * Doing colour before tone leaves the white balance fighting the exposure;
 * sharpening before contrast sharpens noise that contrast then amplifies; and
 * retouching faces last-but-one means it works on the look the photograph has
 * ended up with, which is the look being retouched.
 */

import { Maps, portraitPass } from './studio/portrait.mjs';

/** The maps of the faces, kept between renders: they change when the crop or the paint does, not when a slider does. */
let faces = null;

const clamp = (v) => (v < 0 ? 0 : v > 255 ? 255 : v);
const luma = (r, g, b) => 0.2126 * r + 0.7152 * g + 0.0722 * b;

/** A separable box blur, run twice — close enough to a Gaussian, far cheaper. */
function blurred(src, width, height, radius) {
  if (radius < 1) return src.slice();
  const pass = (input) => {
    const out = new Float32Array(input.length);
    const span = radius * 2 + 1;
    for (let y = 0; y < height; y++) {
      for (let c = 0; c < 3; c++) {
        let sum = 0;
        for (let x = -radius; x <= radius; x++) {
          sum += input[(y * width + Math.min(width - 1, Math.max(0, x))) * 4 + c];
        }
        for (let x = 0; x < width; x++) {
          out[(y * width + x) * 4 + c] = sum / span;
          const drop = input[(y * width + Math.max(0, x - radius)) * 4 + c];
          const add = input[(y * width + Math.min(width - 1, x + radius + 1)) * 4 + c];
          sum += add - drop;
        }
      }
    }
    const out2 = new Float32Array(input.length);
    for (let x = 0; x < width; x++) {
      for (let c = 0; c < 3; c++) {
        let sum = 0;
        for (let y = -radius; y <= radius; y++) {
          sum += out[(Math.min(height - 1, Math.max(0, y)) * width + x) * 4 + c];
        }
        for (let y = 0; y < height; y++) {
          out2[(y * width + x) * 4 + c] = sum / span;
          const drop = out[(Math.max(0, y - radius) * width + x) * 4 + c];
          const add = out[(Math.min(height - 1, y + radius + 1) * width + x) * 4 + c];
          sum += add - drop;
        }
      }
    }
    return out2;
  };
  return pass(src);
}

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

function render(data) {
  const { id, pixels, width, height, masks, mw, mh, settings: s, portrait } = data;
  const src = new Uint8ClampedArray(pixels);
  const out = new Uint8ClampedArray(src.length);
  const count = width * height;

  /* Masks arrive as one byte per pixel at preview size; sampled by nearest
   * neighbour because they are already feathered and a second interpolation
   * only softens edges the person deliberately drew. */
  const mask = (name) => (masks[name] ? new Uint8Array(masks[name]) : null);
  const dodge = mask('dodge'), burn = mask('burn'), soften = mask('soften');
  const at = (name, x, y) => {
    const m = name;
    if (!m) return 0;
    const i = Math.min(mh - 1, (y * mh / height) | 0) * mw
            + Math.min(mw - 1, (x * mw / width) | 0);
    return m[i] / 255;
  };

  const scale = Math.max(1, Math.round(width / mw));

  /* Detail work needs a blurred copy of the whole frame, so it is built once
   * rather than per pixel. Clarity is midtone local contrast (a wide radius),
   * sharpening is edge acutance (a narrow one), and hair density leans on the
   * narrow one too. */
  const wantsWide = s.clarity;
  const wantsFine = s.sharpen;
  const wide = wantsWide ? blurred(src, width, height, Math.max(2, Math.round(scale * 6))) : null;
  const fine = wantsFine ? blurred(src, width, height, Math.max(1, scale)) : null;
  const softBlur = soften ? blurred(src, width, height, Math.max(2, scale * 3)) : null;

  const exposure = Math.pow(2, s.exposure / 50);
  const contrast = 1 + s.contrast / 100;
  const warmth = s.warmth * 0.4;
  const tintGM = (s.tint || 0) * 0.35;

  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      const i = (y * width + x) * 4;
      let r = src[i], g = src[i + 1], b = src[i + 2];

      /* -- 1. tone ----------------------------------------------------- */
      r *= exposure; g *= exposure; b *= exposure;

      if (s.highlights || s.shadows || s.whites || s.blacks) {
        const l = luma(r, g, b) / 255;
        // Each recovery slider owns one end of the range and fades out before
        // it reaches the other, so pulling highlights back cannot grey the
        // shadows — the complaint about every naïve implementation of this.
        const hiW = Math.max(0, (l - 0.5) * 2) ** 1.4;
        const loW = Math.max(0, (0.5 - l) * 2) ** 1.4;
        const whiteW = Math.max(0, (l - 0.75) * 4);
        const blackW = Math.max(0, (0.25 - l) * 4);
        const lift = (s.highlights * hiW * 0.9 + s.shadows * loW * 1.1
                    + s.whites * whiteW * 0.8 + s.blacks * blackW * 0.8) * 1.1;
        r += lift; g += lift; b += lift;
      }

      r = (r - 128) * contrast + 128;
      g = (g - 128) * contrast + 128;
      b = (b - 128) * contrast + 128;

      /* -- 2. colour --------------------------------------------------- */
      r += warmth; b -= warmth;
      g += tintGM; r -= tintGM * 0.5; b -= tintGM * 0.5;

      if (s.saturation) {
        const gray = luma(r, g, b);
        // Vibrance, not saturation: the further a pixel already is from grey,
        // the less it is pushed. Skin stops going orange when the slider is
        // used to rescue a flat sky.
        const spread = Math.max(Math.abs(r - gray), Math.abs(g - gray), Math.abs(b - gray)) / 255;
        const amount = (s.saturation / 100) * (s.saturation > 0 ? 1 - spread * 0.7 : 1);
        r = gray + (r - gray) * (1 + amount);
        g = gray + (g - gray) * (1 + amount);
        b = gray + (b - gray) * (1 + amount);
      }

      /* -- 3. detail --------------------------------------------------- */
      if (s.clarity && wide) {
        const k = s.clarity / 130;
        // Held back at both ends: clarity on a blown highlight makes a halo,
        // and on a black shadow it makes noise.
        const l = luma(r, g, b) / 255;
        const guard = 1 - Math.abs(l - 0.5) * 1.6;
        if (guard > 0) {
          r += (r - wide[i]) * k * guard;
          g += (g - wide[i + 1]) * k * guard;
          b += (b - wide[i + 2]) * k * guard;
        }
      }
      if (s.sharpen && fine) {
        const k = s.sharpen / 90;
        r += (r - fine[i]) * k;
        g += (g - fine[i + 1]) * k;
        b += (b - fine[i + 2]) * k;
      }

      /* -- 4. brushes -------------------------------------------------- */
      if (dodge && s.dodgeAmount) {
        const k = at(dodge, x, y) * s.dodgeAmount / 100;
        if (k) { r += (255 - r) * k * 0.45; g += (255 - g) * k * 0.45; b += (255 - b) * k * 0.45; }
      }
      if (burn && s.burnAmount) {
        const k = at(burn, x, y) * s.burnAmount / 100;
        if (k) { r *= 1 - k * 0.45; g *= 1 - k * 0.45; b *= 1 - k * 0.45; }
      }
      if (softBlur && s.softenAmount) {
        const k = at(soften, x, y) * s.softenAmount / 100;
        if (k) {
          r += (softBlur[i] - r) * k;
          g += (softBlur[i + 1] - g) * k;
          b += (softBlur[i + 2] - b) * k;
        }
      }

      out[i] = clamp(r); out[i + 1] = clamp(g); out[i + 2] = clamp(b);
      out[i + 3] = src[i + 3];
    }
  }

  /* -- 5. faces: each person's skin and hair, where the maps say they are. -- */
  if (portrait && faces && faces.key === portrait.mapsKey) {
    portraitPass({ pixels: out, width, height, maps: faces.maps, portrait });
  }

  if (s.vignette) {
    // Last, over everything, because a vignette is a property of the lens and
    // not of any adjustment made to the picture inside it.
    const cx = width / 2, cy = height / 2;
    const far = Math.hypot(cx, cy);
    for (let y = 0; y < height; y++) {
      for (let x = 0; x < width; x++) {
        const i = (y * width + x) * 4;
        const d = Math.hypot(x - cx, y - cy) / far;
        const k = Math.max(0, d - 0.45) / 0.55;
        const f = 1 - (k * k) * (s.vignette / 100) * 0.85;
        out[i] = clamp(out[i] * f);
        out[i + 1] = clamp(out[i + 1] * f);
        out[i + 2] = clamp(out[i + 2] * f);
      }
    }
  }

  self.postMessage({ id, pixels: out.buffer }, [out.buffer]);
}
