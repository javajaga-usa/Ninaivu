/**
 * A blurred preview from its few dozen bytes (media/placeholders.py makes
 * them; the ThumbHash format, by Evan Wallace, MIT licence:
 * https://github.com/evanw/thumbhash), drawn as a tiny PNG the browser can
 * show as a tile's background. Only opaque previews, which is all the server
 * makes. No imports and nothing of the page's: the same file runs in the
 * preview worker (placeholder-worker.js) and, where there is no worker, on
 * the page itself.
 */

/** Pixels along the longer side. The browser stretches it over the tile,
 *  which is the blur; more would only cost memory. */
export const SIDE = 20;

/** The red, green and blue of a preview, *side* pixels along its longer side. */
export function decodePreview(hash, side = SIDE) {
  if (!hash || hash.length < 5) return null;
  const h24 = hash[0] | (hash[1] << 8) | (hash[2] << 16);
  const h16 = hash[3] | (hash[4] << 8);
  if (h24 >> 23) return null;                 // with transparency: never made here
  const lDc = (h24 & 63) / 63;
  const pDc = ((h24 >> 6) & 63) / 31.5 - 1;
  const qDc = ((h24 >> 12) & 63) / 31.5 - 1;
  const lScale = ((h24 >> 18) & 31) / 31;
  const pScale = ((h16 >> 3) & 63) / 63;
  const qScale = ((h16 >> 9) & 63) / 63;
  const landscape = h16 >> 15;
  const short = h16 & 7;
  const lx = Math.max(3, landscape ? 7 : short);
  const ly = Math.max(3, landscape ? short : 7);

  let index = 0;
  const channel = (nx, ny, scale) => {
    const ac = [];
    for (let cy = 0; cy < ny; cy += 1) {
      for (let cx = cy ? 0 : 1; cx * ny < nx * (ny - cy); cx += 1) {
        const byte = hash[5 + (index >> 1)] || 0;
        ac.push((((byte >> ((index & 1) << 2)) & 15) / 7.5 - 1) * scale);
        index += 1;
      }
    }
    return ac;
  };
  const lAc = channel(lx, ly, lScale);
  // A little more colour than was measured, as the format's own decoder gives.
  const pAc = channel(3, 3, pScale * 1.25);
  const qAc = channel(3, 3, qScale * 1.25);

  const across = landscape ? 7 : (short || 7);
  const down = landscape ? (short || 7) : 7;
  const ratio = across / down;
  const w = ratio > 1 ? side : Math.max(1, Math.round(side * ratio));
  const h = ratio > 1 ? Math.max(1, Math.round(side / ratio)) : side;
  const rgb = new Uint8Array(w * h * 3);
  const nx = Math.max(lx, 3);
  const ny = Math.max(ly, 3);
  const fx = new Float64Array(w * nx);
  for (let x = 0; x < w; x += 1) {
    for (let cx = 0; cx < nx; cx += 1) fx[x * nx + cx] = Math.cos(Math.PI / w * (x + 0.5) * cx);
  }
  const fy = new Float64Array(ny);
  for (let y = 0, at = 0; y < h; y += 1) {
    for (let cy = 0; cy < ny; cy += 1) fy[cy] = Math.cos(Math.PI / h * (y + 0.5) * cy) * 2;
    for (let x = 0; x < w; x += 1) {
      const row = x * nx;
      let l = lDc;
      let p = pDc;
      let q = qDc;
      for (let cy = 0, j = 0; cy < ly; cy += 1) {
        const f = fy[cy];
        for (let cx = cy ? 0 : 1; cx * ly < lx * (ly - cy); cx += 1, j += 1) l += lAc[j] * fx[row + cx] * f;
      }
      for (let cy = 0, j = 0; cy < 3; cy += 1) {
        const f = fy[cy];
        for (let cx = cy ? 0 : 1; cx < 3 - cy; cx += 1, j += 1) {
          const k = fx[row + cx] * f;
          p += pAc[j] * k;
          q += qAc[j] * k;
        }
      }
      const b = l - (2 / 3) * p;
      const r = (3 * l - b + q) / 2;
      const g = r - q;
      rgb[at++] = Math.max(0, Math.min(255, Math.round(255 * r)));
      rgb[at++] = Math.max(0, Math.min(255, Math.round(255 * g)));
      rgb[at++] = Math.max(0, Math.min(255, Math.round(255 * b)));
    }
  }
  return { w, h, rgb };
}

let crcTable = null;
function crc32(bytes, start, end) {
  if (!crcTable) {
    crcTable = new Uint32Array(256);
    for (let n = 0; n < 256; n += 1) {
      let c = n;
      for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
      crcTable[n] = c >>> 0;
    }
  }
  let c = 0xffffffff;
  for (let i = start; i < end; i += 1) c = crcTable[(c ^ bytes[i]) & 255] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

/** An uncompressed PNG of the pixels, as a data: URL. A few hundred bytes of
 *  pixels do not repay compressing, and leaving them stored keeps this quick. */
export function pngURL({ w, h, rgb }) {
  const row = w * 3 + 1;
  const raw = row * h;                       // under 65,535: one stored block
  const idat = 2 + 5 + raw + 4;
  const size = 8 + (12 + 13) + (12 + idat) + 12;
  const out = new Uint8Array(size);
  let at = 0;
  const put32 = (v) => {
    out[at++] = (v >>> 24) & 255; out[at++] = (v >>> 16) & 255;
    out[at++] = (v >>> 8) & 255; out[at++] = v & 255;
  };
  const chunk = (type, length, write) => {
    put32(length);
    const from = at;
    for (let i = 0; i < 4; i += 1) out[at++] = type.charCodeAt(i);
    write();
    put32(crc32(out, from, at));
  };
  [137, 80, 78, 71, 13, 10, 26, 10].forEach((b) => { out[at++] = b; });
  chunk('IHDR', 13, () => {
    put32(w); put32(h);
    out[at++] = 8; out[at++] = 2;            // 8 bits, RGB
    out[at++] = 0; out[at++] = 0; out[at++] = 0;
  });
  chunk('IDAT', idat, () => {
    out[at++] = 0x78; out[at++] = 0x01;
    out[at++] = 1;                           // the last block, stored
    out[at++] = raw & 255; out[at++] = raw >> 8;
    out[at++] = ~raw & 255; out[at++] = (~raw >> 8) & 255;
    let a = 1;
    let b = 0;
    for (let y = 0, from = 0; y < h; y += 1) {
      const bytes = [0];                     // no filter
      for (let i = 0; i < w * 3; i += 1) bytes.push(rgb[from++]);
      for (const v of bytes) {
        out[at++] = v;
        a = (a + v) % 65521;
        b = (b + a) % 65521;
      }
    }
    put32(((b << 16) | a) >>> 0);
  });
  chunk('IEND', 0, () => {});
  let text = '';
  for (let i = 0; i < out.length; i += 1) text += String.fromCharCode(out[i]);
  return `data:image/png;base64,${btoa(text)}`;
}

/** The answer of /api/placeholders, read: for each id asked for, in order,
 *  its preview's data: URL, or null for none. */
export function readAnswer(ids, buffer, side = SIDE) {
  const bytes = new Uint8Array(buffer);
  const urls = new Array(ids.length).fill(null);
  for (let i = 0, at = 0; i < ids.length && at < bytes.length; i += 1) {
    const length = bytes[at++];
    if (length) {
      const decoded = decodePreview(bytes.subarray(at, at + length), side);
      if (decoded) urls[i] = pngURL(decoded);
    }
    at += length;
  }
  return urls;
}
