/* Pixel work stays off the UI thread.
 *
 * Everything here is arithmetic on the pixels you can see. Nothing is
 * generated, nothing is invented, and no image leaves the machine — the
 * selections arrive as masks the person painted (or accepted from the
 * automatic proposal) and each adjustment is confined to one of them.
 *
 * The order below is the order a darkroom works in and it matters:
 *
 *   1. tone      exposure, then the recovery sliders, then contrast
 *   2. colour    white balance, then vibrance/saturation
 *   3. detail    clarity and sharpening, which want final tone underneath
 *   4. local     skin, hair, and the painted dodge/burn/soften brushes
 *
 * Doing colour before tone leaves the white balance fighting the exposure;
 * sharpening before contrast sharpens noise that contrast then amplifies.
 */

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
  const { id, pixels, width, height, masks, mw, mh, settings: s } = data;
  const src = new Uint8ClampedArray(pixels);
  const out = new Uint8ClampedArray(src.length);
  const count = width * height;

  /* Masks arrive as one byte per pixel at preview size; sampled by nearest
   * neighbour because they are already feathered and a second interpolation
   * only softens edges the person deliberately drew. */
  const mask = (name) => (masks[name] ? new Uint8Array(masks[name]) : null);
  const skin = mask('skin'), hair = mask('hair');
  const dodge = mask('dodge'), burn = mask('burn'), soften = mask('soften');
  const at = (name, x, y) => {
    const m = name;
    if (!m) return 0;
    const i = Math.min(mh - 1, (y * mh / height) | 0) * mw
            + Math.min(mw - 1, (x * mw / width) | 0);
    return m[i] / 255;
  };

  const tint = (s.hairColor || '#684331').match(/\w\w/g).map((v) => parseInt(v, 16));
  const tintLum = Math.max(1, luma(tint[0], tint[1], tint[2]));
  const scale = Math.max(1, Math.round(width / mw));

  /* Detail work needs a blurred copy of the whole frame, so it is built once
   * rather than per pixel. Clarity is midtone local contrast (a wide radius),
   * sharpening is edge acutance (a narrow one), and hair density leans on the
   * narrow one too. */
  const wantsWide = s.clarity || s.hairDensity;
  const wantsFine = s.sharpen || s.hairDensity;
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

      /* -- 4. local ---------------------------------------------------- */
      const sk = at(skin, x, y);
      if (sk) {
        if (s.smooth) {
          // Edge-aware neighbourhood: keeps pores and lashes, loses blotches.
          let sums = [0, 0, 0], weights = 0;
          for (let dy = -1; dy <= 1; dy++) {
            for (let dx = -1; dx <= 1; dx++) {
              const j = (Math.max(0, Math.min(height - 1, y + dy * scale)) * width
                       + Math.max(0, Math.min(width - 1, x + dx * scale))) * 4;
              const d = (src[j] - r) ** 2 + (src[j + 1] - g) ** 2 + (src[j + 2] - b) ** 2;
              const w = Math.exp(-d / 1800);
              weights += w;
              sums[0] += src[j] * w; sums[1] += src[j + 1] * w; sums[2] += src[j + 2] * w;
            }
          }
          const k = sk * s.smooth / 100;
          r += (sums[0] / weights - r) * k;
          g += (sums[1] / weights - g) * k;
          b += (sums[2] / weights - b) * k;
        }
        r += sk * (s.skinWarm * 0.25 - s.redness * 0.18);
        g += sk * s.redness * 0.05;
        b -= sk * s.skinWarm * 0.25;
      }

      const ha = at(hair, x, y);
      if (ha) {
        if (s.hairDensity && fine && wide) {
          /* Density, the way a retoucher means it.
           *
           * You cannot add hair that was never photographed. What reads as
           * thin hair is scalp showing through between strands, and what
           * reads as thick hair is strand contrast. So: darken the bright
           * gaps in proportion to how much brighter than their surroundings
           * they are, and raise the fine detail that separates one strand
           * from the next. On genuinely bald scalp there are no gaps between
           * strands and nothing happens, which is the honest outcome. */
          const k = s.hairDensity / 100 * ha;
          const localGap = Math.max(0, luma(r, g, b) - luma(wide[i], wide[i + 1], wide[i + 2]));
          const fill = Math.min(1, localGap / 40) * k * 26;
          r -= fill; g -= fill; b -= fill;
          const strand = k * 0.85;
          r += (r - fine[i]) * strand;
          g += (g - fine[i + 1]) * strand;
          b += (b - fine[i + 2]) * strand;
        }
        if (s.hairRoots) {
          // Burn the roots and the parting: the shading that makes hair sit
          // on a head rather than float above one.
          const depth = Math.max(0, 1 - luma(r, g, b) / 200);
          const k = ha * s.hairRoots / 100 * depth * 34;
          r -= k; g -= k; b -= k;
        }
        if (s.hairAmount) {
          const l = luma(r, g, b);
          const k = ha * s.hairAmount / 100;
          r += (tint[0] * l / tintLum - r) * k;
          g += (tint[1] * l / tintLum - g) * k;
          b += (tint[2] * l / tintLum - b) * k;
        }
        if (s.hairLight) {
          const k = ha * s.hairLight * 0.45;
          r += k; g += k; b += k;
        }
      }

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
};
