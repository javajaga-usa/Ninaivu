/**
 * Single-channel pictures — "planes" — and the few things retouching needs to do to them.
 *
 * A plane is a `Float32Array` of width × height numbers. Everything here is a
 * straight loop over one, which is why the retouch can run on a face-sized window
 * of a twenty-four-megapixel photograph, several times, in less time than it
 * takes to move a slider.
 *
 * The one operation that matters most is the *masked* blur. Skin that is blurred
 * beside a bindi, an eye or a fringe of hair takes some of it in, and that is how
 * a smoothing tool leaves a red halo round a dot it was told to leave alone. A
 * masked blur averages only over the pixels it is allowed to: the value at each
 * place is the blur of (value × weight) divided by the blur of weight, so the
 * things that are not skin simply do not count.
 */

/** Box-blur radii whose three passes approximate a Gaussian of the given sigma. */
function boxSizes(sigma, passes = 3) {
  const ideal = Math.sqrt((12 * sigma * sigma) / passes + 1);
  let lower = Math.floor(ideal);
  if (lower % 2 === 0) lower--;
  const upper = lower + 2;
  const mid = Math.round((12 * sigma * sigma - passes * lower * lower - 4 * passes * lower - 3 * passes)
    / (-4 * lower - 4));
  const sizes = [];
  for (let i = 0; i < passes; i++) sizes.push((i < mid ? lower : upper - 0) | 0);
  return sizes.map((size) => Math.max(1, (size - 1) >> 1));
}

/** One horizontal box pass of radius *r*, edges clamped. */
function boxH(src, dst, w, h, r) {
  const span = 2 * r + 1;
  for (let y = 0; y < h; y++) {
    const row = y * w;
    let sum = src[row] * (r + 1);
    for (let x = 1; x < r; x++) sum += src[row + Math.min(x, w - 1)];
    for (let x = 0; x < w; x++) {
      sum += src[row + Math.min(w - 1, x + r)];
      dst[row + x] = sum / span;
      sum -= src[row + Math.max(0, x - r)];
    }
  }
}

/** One vertical box pass of radius *r*, edges clamped. */
function boxV(src, dst, w, h, r) {
  const span = 2 * r + 1;
  for (let x = 0; x < w; x++) {
    let sum = src[x] * (r + 1);
    for (let y = 1; y < r; y++) sum += src[Math.min(y, h - 1) * w + x];
    for (let y = 0; y < h; y++) {
      sum += src[Math.min(h - 1, y + r) * w + x];
      dst[y * w + x] = sum / span;
      sum -= src[Math.max(0, y - r) * w + x];
    }
  }
}

/** A direct Gaussian, for the small sigmas a box approximation cannot make. */
function gaussianSmall(src, w, h, sigma) {
  const radius = Math.max(1, Math.ceil(sigma * 3));
  const kernel = new Float32Array(2 * radius + 1);
  let total = 0;
  for (let i = -radius; i <= radius; i++) {
    kernel[i + radius] = Math.exp(-(i * i) / (2 * sigma * sigma));
    total += kernel[i + radius];
  }
  for (let i = 0; i < kernel.length; i++) kernel[i] /= total;
  const mid = new Float32Array(src.length), out = new Float32Array(src.length);
  for (let y = 0; y < h; y++) {
    const row = y * w;
    for (let x = 0; x < w; x++) {
      let sum = 0;
      for (let k = -radius; k <= radius; k++) sum += src[row + Math.min(w - 1, Math.max(0, x + k))] * kernel[k + radius];
      mid[row + x] = sum;
    }
  }
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      let sum = 0;
      for (let k = -radius; k <= radius; k++) sum += mid[Math.min(h - 1, Math.max(0, y + k)) * w + x] * kernel[k + radius];
      out[y * w + x] = sum;
    }
  }
  return out;
}

/** Shrink a plane by an integer factor, averaging, so a wide blur can be done on fewer pixels. */
function shrink(src, w, h, factor) {
  const sw = Math.max(1, Math.ceil(w / factor)), sh = Math.max(1, Math.ceil(h / factor));
  const out = new Float32Array(sw * sh);
  for (let y = 0; y < sh; y++) {
    for (let x = 0; x < sw; x++) {
      let sum = 0, count = 0;
      for (let dy = 0; dy < factor; dy++) {
        const yy = y * factor + dy;
        if (yy >= h) break;
        for (let dx = 0; dx < factor; dx++) {
          const xx = x * factor + dx;
          if (xx >= w) break;
          sum += src[yy * w + xx];
          count++;
        }
      }
      out[y * sw + x] = sum / count;
    }
  }
  return { data: out, w: sw, h: sh };
}

/** Bilinear resize of a plane, pixel centres aligned. */
export function resize(src, sw, sh, dw, dh) {
  if (sw === dw && sh === dh) return src.slice();
  const out = new Float32Array(dw * dh);
  const fx = sw / dw, fy = sh / dh;
  for (let y = 0; y < dh; y++) {
    const sy = Math.min(sh - 1, Math.max(0, (y + 0.5) * fy - 0.5));
    const y0 = Math.floor(sy), y1 = Math.min(sh - 1, y0 + 1), ty = sy - y0;
    for (let x = 0; x < dw; x++) {
      const sx = Math.min(sw - 1, Math.max(0, (x + 0.5) * fx - 0.5));
      const x0 = Math.floor(sx), x1 = Math.min(sw - 1, x0 + 1), tx = sx - x0;
      const top = src[y0 * sw + x0] * (1 - tx) + src[y0 * sw + x1] * tx;
      const bottom = src[y1 * sw + x0] * (1 - tx) + src[y1 * sw + x1] * tx;
      out[y * dw + x] = top * (1 - ty) + bottom * ty;
    }
  }
  return out;
}

/**
 * A Gaussian blur of *sigma* pixels. Small sigmas are done exactly; larger ones
 * with three box passes; very large ones on a shrunken copy, so a blur as wide as
 * a face costs the same at any resolution.
 */
export function blur(src, w, h, sigma) {
  if (!(sigma > 0.35)) return src.slice();
  if (sigma <= 2.2) return gaussianSmall(src, w, h, sigma);
  const factor = Math.floor(sigma / 5);
  if (factor >= 2 && w >= 2 * factor && h >= 2 * factor) {
    const small = shrink(src, w, h, factor);
    const blurred = blur(small.data, small.w, small.h, sigma / factor);
    return resize(blurred, small.w, small.h, w, h);
  }
  const across = new Float32Array(src.length), result = new Float32Array(src.length);
  let from = src;
  for (const r of boxSizes(sigma)) {
    boxH(from, across, w, h, r);
    boxV(across, result, w, h, r);
    from = result;                      // each pass finishes before the next reads it
  }
  return result;
}

/**
 * A blur that averages over where *weight* is and nowhere else. The value at
 * every pixel is what the weighted pixels around it average to, so a face blurred
 * this way never takes in the colour of the wall behind it.
 *
 * Where there is nothing to average — a pixel with no weighted neighbour within
 * reach — the pixel keeps its own value, which is what nothing-to-say looks like.
 */
export function maskedBlur(values, weight, w, h, sigma, denominator = null) {
  const top = new Float32Array(values.length);
  for (let i = 0; i < top.length; i++) top[i] = values[i] * weight[i];
  const numerator = blur(top, w, h, sigma);
  const bottom = denominator || blur(weight, w, h, sigma);
  const out = new Float32Array(values.length);
  for (let i = 0; i < out.length; i++) out[i] = bottom[i] > 1e-3 ? numerator[i] / bottom[i] : values[i];
  return out;
}

/** The value below which *fraction* of the weighted samples fall, from a histogram (so: fast, and exact to a bin). */
export function weightedQuantile(values, weight, fraction, low, high, bins = 256, threshold = 0.25) {
  const histogram = new Float64Array(bins);
  let total = 0;
  const scale = (bins - 1) / (high - low);
  for (let i = 0; i < values.length; i++) {
    if (weight[i] < threshold) continue;
    const bin = Math.min(bins - 1, Math.max(0, Math.round((values[i] - low) * scale)));
    histogram[bin] += weight[i];
    total += weight[i];
  }
  if (total <= 0) return null;
  let running = 0;
  const goal = total * fraction;
  for (let b = 0; b < bins; b++) {
    running += histogram[b];
    if (running >= goal) return low + b / scale;
  }
  return high;
}
