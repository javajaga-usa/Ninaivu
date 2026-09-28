/** Minimal BlurHash decoder — renders the placeholder while a full image loads. */

const DIGITS = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz#$%*+,-.:;=?@[]^_{|}~';
const LOOKUP = new Map([...DIGITS].map((ch, i) => [ch, i]));

function decode83(str) {
  let value = 0;
  for (const ch of str) {
    const digit = LOOKUP.get(ch);
    if (digit === undefined) return NaN;
    value = value * 83 + digit;
  }
  return value;
}

const srgbToLinear = (v) => {
  const x = v / 255;
  return x <= 0.04045 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4;
};

const linearToSrgb = (v) => {
  const x = Math.max(0, Math.min(1, v));
  return Math.round((x <= 0.0031308 ? x * 12.92 : 1.055 * x ** (1 / 2.4) - 0.055) * 255);
};

const signPow = (v, exp) => Math.sign(v) * Math.abs(v) ** exp;

export function decodeBlurhash(hash, width = 32, height = 32, punch = 1) {
  if (!hash || hash.length < 6) return null;
  const sizeFlag = decode83(hash[0]);
  const cx = (sizeFlag % 9) + 1;
  const cy = Math.floor(sizeFlag / 9) + 1;
  if (hash.length !== 4 + 2 * cx * cy + 2) return null;

  const maxValue = (decode83(hash[1]) + 1) / 166;
  const colors = new Array(cx * cy);

  const dc = decode83(hash.slice(2, 6));
  colors[0] = [
    srgbToLinear(dc >> 16),
    srgbToLinear((dc >> 8) & 255),
    srgbToLinear(dc & 255),
  ];

  for (let i = 1; i < cx * cy; i++) {
    const value = decode83(hash.slice(4 + i * 2, 6 + i * 2));
    const r = Math.floor(value / (19 * 19));
    const g = Math.floor(value / 19) % 19;
    const b = value % 19;
    colors[i] = [
      signPow((r - 9) / 9, 2) * maxValue * punch,
      signPow((g - 9) / 9, 2) * maxValue * punch,
      signPow((b - 9) / 9, 2) * maxValue * punch,
    ];
  }

  const pixels = new Uint8ClampedArray(width * height * 4);
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      let r = 0;
      let g = 0;
      let b = 0;
      for (let j = 0; j < cy; j++) {
        const cosY = Math.cos((Math.PI * y * j) / height);
        for (let i = 0; i < cx; i++) {
          const basis = Math.cos((Math.PI * x * i) / width) * cosY;
          const color = colors[j * cx + i];
          r += color[0] * basis;
          g += color[1] * basis;
          b += color[2] * basis;
        }
      }
      const p = (y * width + x) * 4;
      pixels[p] = linearToSrgb(r);
      pixels[p + 1] = linearToSrgb(g);
      pixels[p + 2] = linearToSrgb(b);
      pixels[p + 3] = 255;
    }
  }
  return new ImageData(pixels, width, height);
}

const cache = new Map();

/** Returns a data URL for a hash, memoised (hashes repeat across views). */
export function blurhashUrl(hash) {
  if (!hash) return null;
  if (cache.has(hash)) return cache.get(hash);
  let url = null;
  try {
    const data = decodeBlurhash(hash, 32, 32);
    if (data) {
      const canvas = document.createElement('canvas');
      canvas.width = 32;
      canvas.height = 32;
      canvas.getContext('2d').putImageData(data, 0, 0);
      url = canvas.toDataURL();
    }
  } catch { /* placeholder is optional */ }
  if (cache.size > 500) cache.clear();
  cache.set(hash, url);
  return url;
}
