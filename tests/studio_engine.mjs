/**
 * The retouching engine, without a browser.
 *
 * Run with `node --test tests/studio_engine.mjs`. Nothing here needs a face
 * detector or a server: a face is a few ellipses painted into an array, the maps
 * that say which ellipse is skin and which is a bindi are painted beside it, and
 * what is checked is what the engine does to the numbers.
 *
 * The promises worth a test are the ones a person would hold it to:
 *   - it touches only what it was asked to, and only the face it was asked to;
 *   - a bindi, sindoor or a streak of ash comes out byte for byte as it went in;
 *   - nothing it offers makes skin lighter except light on a face in shadow, and
 *     that keeps the colour;
 *   - a tool moved by a little does a little.
 */

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { rgbToLab, labToRgb, parseHex, smoothstep } from '../ninaivu/static/js/studio/colour.mjs';
import { blur, maskedBlur, resize } from '../ninaivu/static/js/studio/plane.mjs';
import { Maps, portraitPass } from '../ninaivu/static/js/studio/portrait.mjs';
import { KEYS, blank, effective, applySuggestions, isNeutral, copy } from '../ninaivu/static/js/studio/portrait-params.mjs';

// ---------------------------------------------------------------------------
// A face, painted
// ---------------------------------------------------------------------------

/** A small deterministic random, so a failure can be run again. */
function noise(seed) {
  let s = seed >>> 0;
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return (s / 4294967296 + (((s * 22695477) >>> 0) / 4294967296) - 1) * 1.7;
  };
}

const ellipse = (x, y, cx, cy, rx, ry) => ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2 <= 1;

/**
 * One or two faces in a picture of `W × H`. Each face: `cx, cy, d` (eye distance), its
 * `skin` colour, and what it wears or has. Returns the pixels, the maps, and the regions to look at.
 */
function scene(specs, { W = 360, H = 260, wall = [150, 152, 158], grain = 0, seed = 5, mapScale = 1 } = {}) {
  const pixels = new Uint8ClampedArray(W * H * 4);
  const rand = noise(seed);
  const mw = Math.round(W * mapScale), mh = Math.round(H * mapScale);
  const a = new Uint8ClampedArray(mw * mh * 4), b = new Uint8ClampedArray(mw * mh * 4), c = new Uint8ClampedArray(mw * mh * 4), ids = new Uint8ClampedArray(mw * mh * 4);
  const regions = specs.map(() => ({ skin: [], bindi: [], hollows: [], hair: [], zone: [], neck: [] }));
  for (let y = 0; y < H; y++) {
    for (let x = 0; x < W; x++) {
      const i = (y * W + x) * 4;
      let [r, g, bl] = wall;
      specs.forEach((f, k) => {
        const u = (x - f.cx) / f.d, v = (y - f.cy) / f.d;
        const inFace = ellipse(u, v, 0, 0.34, 1.0, 1.45);
        if (ellipse(u, v, 0, 2.0, 0.6, 0.75) && !inFace) { [r, g, bl] = f.skin.map((c) => c * 0.88); regions[k].neck.push(y * W + x); }
        if (f.hair && ellipse(u, v, 0, -0.95, 1.12, 1.0)) { [r, g, bl] = f.hair; if (!inFace) regions[k].hair.push(y * W + x); }
        if (inFace) {
          const shade = 1 + (f.side || 0) * Math.max(-1, Math.min(1, u));
          [r, g, bl] = f.skin.map((c) => c * shade);
          const eye = ellipse(u, v, -0.5, 0, 0.2, 0.09) || ellipse(u, v, 0.5, 0, 0.2, 0.09);
          const mouth = ellipse(u, v, 0, 1.0, 0.32, 0.075);
          const dot = f.bindi && ellipse(u, v, 0, -0.62, 0.06, 0.06);
          const hollow = ellipse(u, v, -0.5, 0.3, 0.38, 0.14) || ellipse(u, v, 0.5, 0.3, 0.38, 0.14);
          if (eye) { [r, g, bl] = [230, 230, 226]; }
          else if (mouth) { [r, g, bl] = [166, 66, 78]; }
          else if (dot) { [r, g, bl] = [188, 22, 32]; regions[k].bindi.push(y * W + x); }
          else {
            if (hollow && f.circles) { r *= 1 - f.circles; g *= 1 - f.circles; bl *= 1 - f.circles; regions[k].hollows.push(y * W + x); }
            if (f.spot && ellipse(u, v, f.spot[0], f.spot[1], 0.05, 0.05)) { [r, g, bl] = f.spot[2]; }
            if (f.glint && ellipse(u, v, f.glint[0], f.glint[1], 0.08, 0.08)) { [r, g, bl] = f.glint[2]; }
            // "Skin" is what the maps call skin: a ring round each feature is left out, as the server leaves it out.
            const nearEye = ellipse(u, v, -0.5, 0, 0.37, 0.22) || ellipse(u, v, 0.5, 0, 0.37, 0.22) || ellipse(u, v, 0, 1.0, 0.45, 0.22);
            const nearDot = f.bindi && ellipse(u, v, 0, -0.62, 0.13, 0.13);
            if (!nearEye && !nearDot) regions[k].skin.push(y * W + x);
          }
        }
      });
      const n = grain ? rand() * grain : 0;
      pixels[i] = Math.max(0, Math.min(255, r + n)); pixels[i + 1] = Math.max(0, Math.min(255, g + n)); pixels[i + 2] = Math.max(0, Math.min(255, bl + n)); pixels[i + 3] = 255;
    }
  }
  // The maps, at their own size.
  for (let y = 0; y < mh; y++) {
    for (let x = 0; x < mw; x++) {
      const k = (y * mw + x) * 4, px = (x + 0.5) / mapScale, py = (y + 0.5) / mapScale;
      specs.forEach((f, idx) => {
        const u = (px - f.cx) / f.d, v = (py - f.cy) / f.d;
        const face = ellipse(u, v, 0, 0.34, 1.0, 1.45);
        const eye = ellipse(u, v, -0.5, 0, 0.37, 0.22) || ellipse(u, v, 0.5, 0, 0.37, 0.22);
        const mouth = ellipse(u, v, 0, 1.0, 0.45, 0.22);
        const dot = f.bindi && ellipse(u, v, 0, -0.62, 0.13, 0.13);
        const zone = ellipse(u, v, 0, 0.3, 1.22, 1.72) || ellipse(u, v, 0, 2.0, 0.6, 0.75);     // the face, and the neck with it
        const hollow = (ellipse(u, v, -0.5, 0.3, 0.38, 0.17) || ellipse(u, v, 0.5, 0.3, 0.38, 0.17)) && !eye;
        if (zone) { a[k + 1] = 255; ids[k] = idx + 1; }
        if (face && !eye && !mouth) a[k] = 255;
        if ((face || ellipse(u, v, 0, 2.0, 0.6, 0.75)) && !eye && !mouth) c[k] = 255;     // all the skin that goes with the face: the neck too
        if (dot) a[k + 2] = 255;
        if (hollow) b[k + 2] = 255;
        if (f.hair && ellipse(u, v, 0, -0.95, 1.12, 1.0) && !face) { b[k] = 255; ids[k + 1] = idx + 1; }
      });
    }
  }
  return { W, H, pixels, maps: new Maps({ width: mw, height: mh, a, b, c, ids }), regions, specs };
}

const ask = (over = {}, faces = {}) => ({
  params: { all: over, faces, beard: true, hairColor: null },
  frames: {},
});

function frameFor(f, W, H) {
  return { x: f.cx / W, y: f.cy / H, d: f.d / W, cos: 1, sin: 0 };
}

function run(sc, portrait, { frames = true } = {}) {
  const pixels = new Uint8ClampedArray(sc.pixels);
  if (frames) portrait.frames = Object.fromEntries(sc.specs.map((f, k) => [k + 1, frameFor(f, sc.W, sc.H)]));
  portraitPass({ pixels, width: sc.W, height: sc.H, maps: sc.maps, portrait });
  return pixels;
}

const labOf = (pixels, index) => rgbToLab(pixels[index * 4], pixels[index * 4 + 1], pixels[index * 4 + 2]);
const meanLab = (pixels, indexes) => {
  const sum = [0, 0, 0];
  for (const i of indexes) { const lab = labOf(pixels, i); sum[0] += lab[0]; sum[1] += lab[1]; sum[2] += lab[2]; }
  return sum.map((v) => v / indexes.length);
};
const stdev = (pixels, indexes, channel = 0) => {
  const values = indexes.map((i) => labOf(pixels, i)[channel]);
  const mean = values.reduce((p, c) => p + c, 0) / values.length;
  return Math.sqrt(values.reduce((p, c) => p + (c - mean) ** 2, 0) / values.length);
};
const identical = (before, after, indexes) => indexes.every((i) => [0, 1, 2].every((c) => before[i * 4 + c] === after[i * 4 + c]));
const changed = (before, after, indexes) => indexes.filter((i) => [0, 1, 2].some((c) => before[i * 4 + c] !== after[i * 4 + c])).length;

const WHEATISH = [205, 152, 118];
const DEEP = [84, 54, 40];
const FACE = { cx: 180, cy: 110, d: 60 };

// ---------------------------------------------------------------------------
// Colour
// ---------------------------------------------------------------------------

test('Lab agrees with the Lab the server measured in', () => {
  const expected = [
    [[0, 0, 0], [0, 0, 0]], [[255, 255, 255], [100, 0, 0]], [[128, 128, 128], [53.583, 0, 0]],
    [[200, 150, 120], [66.04, 14.734, 23.297]], [[235, 190, 165], [80.2, 12.531, 18.656]],
    [[104, 66, 47], [31.793, 14.109, 18.25]], [[28, 24, 22], [8.618, 1.578, 1.984]],
    [[255, 0, 0], [53.241, 80.094, 67.203]], [[0, 0, 255], [32.294, 79.188, -107.859]],
  ];
  for (const [rgb, lab] of expected) {
    const got = rgbToLab(...rgb);
    lab.forEach((v, i) => assert.ok(Math.abs(got[i] - v) < 0.3, `${rgb} → ${got} is not ${lab}`));
  }
});

test('Lab and back gives the colour it started with', () => {
  for (const rgb of [[0, 0, 0], [12, 200, 90], [104, 66, 47], [235, 190, 165], [255, 255, 255], [3, 4, 5]]) {
    const back = labToRgb(...rgbToLab(...rgb));
    back.forEach((v, i) => assert.ok(Math.abs(v - rgb[i]) <= 1, `${rgb} came back ${back}`));
  }
});

test('hex colours are read, and nonsense is not', () => {
  assert.deepEqual(parseHex('#1c1714'), [28, 23, 20]);
  assert.equal(parseHex('not a colour'), null);
  assert.equal(parseHex(null), null);
});

// ---------------------------------------------------------------------------
// Planes
// ---------------------------------------------------------------------------

test('a blur keeps a flat picture flat and spreads a point to the width asked for', () => {
  const w = 120, h = 90, flat = new Float32Array(w * h).fill(5);
  assert.ok(Math.abs(blur(flat, w, h, 7)[40 * w + 40] - 5) < 1e-4);
  assert.ok(Math.abs(blur(flat, w, h, 1.4)[3 * w + 3] - 5) < 1e-4);
  for (const sigma of [1.5, 3, 6]) {
    const point = new Float32Array(w * h); point[45 * w + 60] = 1000;
    const out = blur(point, w, h, sigma);
    let mass = 0, spread = 0;
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) { mass += out[y * w + x]; spread += out[y * w + x] * (x - 60) ** 2; }
    assert.ok(Math.abs(mass - 1000) < 1, `mass ${mass}`);
    assert.ok(Math.abs(Math.sqrt(spread / mass) - sigma) < 0.2 * sigma, `sigma ${Math.sqrt(spread / mass)} for ${sigma}`);
  }
});

test('a masked blur takes in nothing it was told to ignore', () => {
  const w = 64, h = 48, values = new Float32Array(w * h).fill(10), weight = new Float32Array(w * h).fill(1);
  for (let y = 0; y < h; y++) for (let x = 30; x < 34; x++) { values[y * w + x] = 1000; weight[y * w + x] = 0; }
  const out = maskedBlur(values, weight, w, h, 4);
  assert.ok(Math.abs(out[20 * w + 28] - 10) < 1e-3);
  assert.ok(blur(values, w, h, 4)[20 * w + 28] > 100, 'the plain blur would have been full of it');
});

test('resizing a plane keeps its values', () => {
  const src = Float32Array.from({ length: 16 }, (_, i) => i % 4);
  const big = resize(src, 4, 4, 8, 8);
  assert.equal(big.length, 64);
  assert.ok(Math.abs(big[0] - 0) < 1e-6 && Math.abs(big[7] - 3) < 1e-6);
});

// ---------------------------------------------------------------------------
// The maps
// ---------------------------------------------------------------------------

test('maps made at a different size line up with the picture they describe', () => {
  const sc = scene([{ ...FACE, skin: WHEATISH }], { mapScale: 0.5 });
  assert.equal(sc.maps.width, 180);
  const rect = { x0: 100, y0: 40, w: 160, h: 140 };
  const read = sc.maps.sample(['skin', 'skinId'], rect, sc.W, sc.H);
  const at = (x, y) => read.skin[(y - rect.y0) * rect.w + (x - rect.x0)];
  assert.ok(at(150, 150) > 0.95, 'a cheek is skin');
  assert.ok(at(150 + 0, 110) < 0.05 || at(180 - 30, 110) < 0.05, 'the eye is not');
  assert.equal(read.skinId[(150 - rect.y0) * rect.w + (180 - rect.x0)], 1);
  assert.ok(Number.isInteger(read.skinId[0]), 'ids are not blended');
});

test('the bounds of each face are where that face is', () => {
  const sc = scene([{ cx: 90, cy: 110, d: 40, skin: WHEATISH }, { cx: 260, cy: 110, d: 40, skin: DEEP }]);
  const { faces } = sc.maps.bounds();
  assert.deepEqual([...faces.keys()].sort(), [1, 2]);
  assert.ok(faces.get(1).skin[2] < 180 && faces.get(2).skin[0] > 180);
});

// ---------------------------------------------------------------------------
// Only what was asked, only for whom
// ---------------------------------------------------------------------------

test('nothing asked, nothing changed', () => {
  const sc = scene([{ ...FACE, skin: WHEATISH }], { grain: 4 });
  const out = run(sc, ask());
  assert.ok(identical(sc.pixels, out, [...Array(sc.W * sc.H).keys()]));
});

test('a setting for one face changes that face and no other', () => {
  const sc = scene([{ cx: 90, cy: 110, d: 40, skin: WHEATISH }, { cx: 260, cy: 110, d: 40, skin: WHEATISH }], { grain: 5 });
  const out = run(sc, ask({}, { 2: { smooth: 100 } }));
  assert.equal(changed(sc.pixels, out, sc.regions[0].skin), 0, 'face 1 was left alone');
  assert.ok(changed(sc.pixels, out, sc.regions[1].skin) > 0.5 * sc.regions[1].skin.length, 'face 2 was softened');
});

test("a face's own setting replaces the shared one; it does not add to it", () => {
  const portrait = blank();
  portrait.all.smooth = 30;
  portrait.faces[2] = { smooth: 70 };
  assert.equal(effective(portrait, 1).smooth, 30);
  assert.equal(effective(portrait, 2).smooth, 70);
  assert.equal(effective(portrait, 2).shine, 0);
});

test('the background is never touched', () => {
  const sc = scene([{ ...FACE, skin: WHEATISH }], { grain: 4 });
  const p = {}; for (const k of KEYS) p[k] = 100; p.hairColor = '#7a3b22';
  const out = run(sc, ask(p));
  const wall = [];
  for (let y = 0; y < sc.H; y++) for (let x = 0; x < sc.W; x++) {
    const u = (x - FACE.cx) / FACE.d, v = (y - FACE.cy) / FACE.d;
    if (!ellipse(u, v, 0, 0.3, 1.4, 1.9) && !ellipse(u, v, 0, 2.0, 0.7, 0.85)) wall.push(y * sc.W + x);
  }
  assert.ok(identical(sc.pixels, out, wall));
});

// ---------------------------------------------------------------------------
// What is worn on purpose
// ---------------------------------------------------------------------------

test('a bindi comes out byte for byte as it went in, whatever is asked of the skin round it', () => {
  const sc = scene([{ ...FACE, skin: WHEATISH, bindi: true }], { grain: 5 });
  const p = {}; for (const k of KEYS) p[k] = 100;
  p.tone = -100;
  p.faceLight = 0; p.balance = 0;           // light is light: it reaches a mark as it reaches the skin beside it
  const out = run(sc, ask(p));
  const bindi = sc.regions[0].bindi;
  assert.ok(bindi.length > 20);
  assert.ok(identical(sc.pixels, out, bindi), 'the bindi was changed');
});

test('a mark whose map has a soft edge is still untouched wherever the map is all but solid', () => {
  // The server blurs the protect map so that it has no hard edge; a weight of a hundredth is still a change.
  const sc = scene([{ ...FACE, skin: WHEATISH, bindi: true }], { grain: 4 });
  const { width: mw, height: mh, a } = sc.maps;
  const plane = new Float32Array(mw * mh);
  for (let i = 0; i < plane.length; i++) plane[i] = a[i * 4 + 2] / 255;
  const soft = blur(plane, mw, mh, 3.5);
  for (let i = 0; i < plane.length; i++) a[i * 4 + 2] = Math.round(Math.min(1, soft[i] * 1.6) * 255);
  const p = {}; for (const k of KEYS) p[k] = 100;
  p.faceLight = 0; p.balance = 0;
  const out = run(sc, ask(p));
  const held = [];
  for (let i = 0; i < plane.length; i++) if (a[i * 4 + 2] >= 217) held.push(i);
  assert.ok(held.length > 30, 'there is a mark to hold');
  assert.ok(identical(sc.pixels, out, held), 'a pixel of the mark was changed');
});

test('the skin beside a bindi takes none of its red when it is softened, evened and calmed', () => {
  const sc = scene([{ ...FACE, skin: WHEATISH, bindi: true }], { grain: 4 });
  const out = run(sc, ask({ smooth: 100, even: 100, shine: 100 }));
  const beside = [];
  for (let y = 0; y < sc.H; y++) for (let x = 0; x < sc.W; x++) {
    const u = (x - FACE.cx) / FACE.d, v = (y - FACE.cy) / FACE.d;
    if (ellipse(u, v, 0, -0.62, 0.22, 0.22) && !ellipse(u, v, 0, -0.62, 0.15, 0.15)) beside.push(y * sc.W + x);
  }
  assert.ok(beside.length > 30);
  assert.ok(meanLab(out, beside)[1] < meanLab(sc.pixels, beside)[1] + 1.2,
    `a* went ${meanLab(sc.pixels, beside)[1]} → ${meanLab(out, beside)[1]}: red bled into the forehead`);
});

// ---------------------------------------------------------------------------
// Light: for a face in shadow, and only that
// ---------------------------------------------------------------------------

test('light that is brought to a face is brought to the mark on it too', () => {
  const sc = scene([{ ...FACE, skin: [70, 46, 34], bindi: true }]);
  const out = run(sc, ask({ faceLight: 100 }));
  const gain = (set) => meanLab(out, set)[0] - meanLab(sc.pixels, set)[0];
  assert.ok(gain(sc.regions[0].bindi) > 5, 'the bindi stayed in the shadow');
  assert.ok(gain(sc.regions[0].skin) > 10);
});

test('face light brings up a face by the stops asked for, in linear light', () => {
  const sc = scene([{ ...FACE, skin: [70, 46, 34] }]);
  const out = run(sc, ask({ faceLight: 100 }));
  const before = meanLab(sc.pixels, sc.regions[0].skin), after = meanLab(out, sc.regions[0].skin);
  assert.ok(after[0] > before[0] + 15, `L* went from ${before[0]} to ${after[0]}`);
  const luminance = (l) => ((l + 16) / 116) ** 3;
  const stops = Math.log2(luminance(after[0]) / luminance(before[0]));
  assert.ok(stops > 1.1 && stops < 1.6, `${stops} stops`);
});

test('face light carries the colour along: a face lifted is the same person, not a paler one', () => {
  const sc = scene([{ ...FACE, skin: [70, 46, 34] }]);
  const out = run(sc, ask({ faceLight: 60 }));
  const hue = ([, a, b]) => Math.atan2(b, a) * 180 / Math.PI;
  const before = meanLab(sc.pixels, sc.regions[0].skin), after = meanLab(out, sc.regions[0].skin);
  assert.ok(Math.abs(hue(after) - hue(before)) < 3, `hue ${hue(before)} → ${hue(after)}`);
  assert.ok(Math.hypot(after[1], after[2]) > 0.85 * Math.hypot(before[1], before[2]), 'the colour drained');
});

test('face light cannot push a bright face past white', () => {
  const sc = scene([{ ...FACE, skin: [245, 210, 190] }]);
  const out = run(sc, ask({ faceLight: 100 }));
  const red = sc.regions[0].skin.map((i) => out[i * 4]);
  assert.ok(Math.max(...red) <= 255);
  const after = meanLab(out, sc.regions[0].skin);
  assert.ok(after[0] < 99, `nothing went white: ${after[0]}`);
});

test('a dark face is lifted by the same stops as a pale one, so it stays darker', () => {
  const sc = scene([{ cx: 90, cy: 110, d: 40, skin: [80, 52, 38] }, { cx: 260, cy: 110, d: 40, skin: [200, 150, 125] }]);
  const out = run(sc, ask({ faceLight: 50 }));
  const l = (p, k) => meanLab(p, sc.regions[k].skin)[0];
  assert.ok(l(out, 0) < l(out, 1) - 25, 'the dark face did not become the pale one');
  assert.ok(l(out, 0) > l(sc.pixels, 0) + 4 && l(out, 1) > l(sc.pixels, 1) + 2);
});

test('nothing but face light ever makes skin lighter', () => {
  for (const skin of [[236, 192, 168], WHEATISH, [158, 104, 74], DEEP]) {
    const sc = scene([{ ...FACE, skin, circles: 0, bindi: true }], { grain: 3 });
    const p = {}; for (const k of KEYS) p[k] = 100;
    p.faceLight = 0;
    const out = run(sc, ask(p));
    const before = meanLab(sc.pixels, sc.regions[0].skin)[0], after = meanLab(out, sc.regions[0].skin)[0];
    assert.ok(after <= before + 1.5, `skin went from ${before} to ${after}`);
  }
});

// ---------------------------------------------------------------------------
// Colour
// ---------------------------------------------------------------------------

test('tone turns colour by the degrees it says and leaves lightness and depth of colour alone', () => {
  const sc = scene([{ ...FACE, skin: WHEATISH }]);
  const out = run(sc, ask({ tone: 100 }));
  const before = meanLab(sc.pixels, sc.regions[0].skin), after = meanLab(out, sc.regions[0].skin);
  const hue = ([, a, b]) => Math.atan2(b, a) * 180 / Math.PI;
  assert.ok(Math.abs(hue(after) - hue(before) - 12) < 1.2, `${hue(after) - hue(before)} degrees`);
  assert.ok(Math.abs(after[0] - before[0]) < 0.6);
  assert.ok(Math.abs(Math.hypot(after[1], after[2]) - Math.hypot(before[1], before[2])) < 0.6);
  const back = run(sc, ask({ tone: -100 }));
  assert.ok(hue(meanLab(back, sc.regions[0].skin)) < hue(before) - 10, 'and it runs the other way');
});

test('a colour goes on the neck as well as the face, so there is no line at the jaw', () => {
  const sc = scene([{ ...FACE, skin: WHEATISH }]);
  const out = run(sc, ask({ tone: 100 }));
  const hue = ([, a, b]) => Math.atan2(b, a) * 180 / Math.PI;
  const neck = sc.regions[0].neck.filter((i) => Math.floor(i / sc.W) > FACE.cy + 1.8 * FACE.d);
  assert.ok(neck.length > 100, 'there is a neck');
  const turned = hue(meanLab(out, neck)) - hue(meanLab(sc.pixels, neck));
  assert.ok(turned > 9, `the neck turned ${turned} degrees`);
  const faceTurned = hue(meanLab(out, sc.regions[0].skin)) - hue(meanLab(sc.pixels, sc.regions[0].skin));
  assert.ok(Math.abs(turned - faceTurned) < 2, 'by as much as the face did');
  // and what smoothing would do to a neck it was not asked to touch
  const soft = run(sc, ask({ smooth: 100 }));
  assert.equal(changed(sc.pixels, soft, neck), 0, 'softening stops at the jaw');
});

test('richness adds colour and never more than a third', () => {
  const sc = scene([{ ...FACE, skin: [150, 120, 105] }]);
  const out = run(sc, ask({ richness: 100 }));
  const before = meanLab(sc.pixels, sc.regions[0].skin), after = meanLab(out, sc.regions[0].skin);
  const chroma = ([, a, b]) => Math.hypot(a, b);
  assert.ok(chroma(after) > chroma(before) * 1.2 && chroma(after) < chroma(before) * 1.4);
  assert.ok(Math.abs(after[0] - before[0]) < 0.5);
});

// ---------------------------------------------------------------------------
// Softness, shine, evenness, hollows, sides
// ---------------------------------------------------------------------------

test('soften takes blemishes and leaves the grain of skin', () => {
  const sc = scene([{ ...FACE, skin: WHEATISH, spot: [0.3, 0.5, [150, 70, 60]] }], { grain: 7 });
  const plain = stdev(sc.pixels, sc.regions[0].skin);
  const half = stdev(run(sc, ask({ smooth: 50 })), sc.regions[0].skin);
  const full = stdev(run(sc, ask({ smooth: 100 })), sc.regions[0].skin);
  assert.ok(full < half && half < plain, `${plain} → ${half} → ${full}`);
  assert.ok(full > 0.25 * plain, 'it is not wax');
  // A spot is a red patch; it is less red after.
  const spot = sc.regions[0].skin.filter((i) => {
    const x = i % sc.W, y = Math.floor(i / sc.W);
    return ellipse((x - FACE.cx) / FACE.d, (y - FACE.cy) / FACE.d, 0.3, 0.5, 0.04, 0.04);
  });
  const redness = (p) => meanLab(p, spot)[1];
  assert.ok(redness(run(sc, ask({ smooth: 100 }))) < redness(sc.pixels) - 2);
});

test('soften keeps the edges of the features: the eye, the brow and the lip are not blurred', () => {
  const sc = scene([{ ...FACE, skin: WHEATISH }], { grain: 3 });
  const out = run(sc, ask({ smooth: 100 }));
  const eyes = [];
  for (let y = 0; y < sc.H; y++) for (let x = 0; x < sc.W; x++) {
    const u = (x - FACE.cx) / FACE.d, v = (y - FACE.cy) / FACE.d;
    if (ellipse(u, v, -0.5, 0, 0.2, 0.09) || ellipse(u, v, 0.5, 0, 0.2, 0.09) || ellipse(u, v, 0, 1.0, 0.32, 0.075)) eyes.push(y * sc.W + x);
  }
  assert.ok(identical(sc.pixels, out, eyes), 'an eye or a lip was smoothed');
});

test('shine is eased towards the skin round it, and a bright stud is not', () => {
  const sc = scene([{ ...FACE, skin: WHEATISH, spot: [0.3, 0.5, [232, 196, 178]], glint: [-0.45, 0.55, [255, 255, 255]] }], { grain: 1 });
  const spot = sc.regions[0].skin.filter((i) => {
    const x = i % sc.W, y = Math.floor(i / sc.W);
    return ellipse((x - FACE.cx) / FACE.d, (y - FACE.cy) / FACE.d, 0.3, 0.5, 0.03, 0.03);
  });
  const stud = sc.regions[0].skin.filter((i) => {
    const x = i % sc.W, y = Math.floor(i / sc.W);
    return ellipse((x - FACE.cx) / FACE.d, (y - FACE.cy) / FACE.d, -0.45, 0.55, 0.03, 0.03);
  });
  const out = run(sc, ask({ shine: 100 }));
  const l = (p, set) => meanLab(p, set)[0];
  assert.ok(l(out, spot) < l(sc.pixels, spot) - 8, `${l(sc.pixels, spot)} → ${l(out, spot)}`);
  assert.ok(l(out, stud) > l(sc.pixels, stud) - 8, 'a stud was dimmed as if it were shine');
});

test('hollows under the eyes are lifted towards the cheek and no higher', () => {
  const sc = scene([{ ...FACE, skin: WHEATISH, circles: 0.25 }], { grain: 2 });
  const out = run(sc, ask({ underEye: 100 }));
  const hollows = sc.regions[0].hollows;
  const cheek = sc.regions[0].skin.filter((i) => {
    const x = i % sc.W, y = Math.floor(i / sc.W), u = (x - FACE.cx) / FACE.d, v = (y - FACE.cy) / FACE.d;
    return Math.abs(Math.abs(u) - 0.5) < 0.2 && v > 0.55 && v < 0.7;
  });
  const before = meanLab(sc.pixels, hollows)[0], after = meanLab(out, hollows)[0], target = meanLab(sc.pixels, cheek)[0];
  assert.ok(after > before + 5, `${before} → ${after}`);
  assert.ok(after <= target + 1.5, `lifted past the cheek: ${after} vs ${target}`);
});

test('light from one side is balanced, and a face lit evenly is left alone', () => {
  const lit = scene([{ ...FACE, skin: WHEATISH, side: 0.22 }], { grain: 1 });
  const slope = (p, sc) => {
    const left = sc.regions[0].skin.filter((i) => i % sc.W < FACE.cx - 12), right = sc.regions[0].skin.filter((i) => i % sc.W > FACE.cx + 12);
    return meanLab(p, right)[0] - meanLab(p, left)[0];
  };
  const before = slope(lit.pixels, lit);
  const after = slope(run(lit, ask({ balance: 100 })), lit);
  assert.ok(before > 8 && Math.abs(after) < before * 0.5, `${before} → ${after}`);
  const even = scene([{ ...FACE, skin: WHEATISH }], { grain: 1 });
  const out = run(even, ask({ balance: 100 }));
  assert.ok(Math.abs(meanLab(out, even.regions[0].skin)[0] - meanLab(even.pixels, even.regions[0].skin)[0]) < 0.6);
});

// ---------------------------------------------------------------------------
// Hair
// ---------------------------------------------------------------------------

test('grey hair is covered and black hair is left black', () => {
  const grey = scene([{ ...FACE, skin: WHEATISH, hair: [150, 148, 146] }], { grain: 3 });
  const out = run(grey, ask({ greyCover: 100 }));
  const hair = grey.regions[0].hair.filter((i) => ellipse(((i % grey.W) - FACE.cx) / FACE.d, (Math.floor(i / grey.W) - FACE.cy) / FACE.d, 0, -1.3, 0.8, 0.5));
  assert.ok(meanLab(out, hair)[0] < meanLab(grey.pixels, hair)[0] - 15, 'grey stayed grey');

  const black = scene([{ ...FACE, skin: WHEATISH, hair: [28, 24, 22] }], { grain: 2 });
  const same = run(black, ask({ greyCover: 100 }));
  assert.ok(Math.abs(meanLab(same, hair)[0] - meanLab(black.pixels, hair)[0]) < 2);
});

test('a chosen hair colour moves the hair towards it, in proportion', () => {
  const sc = scene([{ ...FACE, skin: WHEATISH, hair: [40, 32, 28] }], { grain: 2 });
  const hair = sc.regions[0].hair.filter((i) => ellipse(((i % sc.W) - FACE.cx) / FACE.d, (Math.floor(i / sc.W) - FACE.cy) / FACE.d, 0, -1.3, 0.8, 0.5));
  const target = { all: { hairAmount: 100 }, faces: {}, beard: true, hairColor: '#7a3b22' };
  const full = run(sc, { params: target, frames: {} });
  const half = run(sc, { params: { ...target, all: { hairAmount: 50 } }, frames: {} });
  const a = (p) => meanLab(p, hair)[1];
  assert.ok(a(full) > a(sc.pixels) + 4, 'the hair did not warm');
  assert.ok(a(half) > a(sc.pixels) && a(half) < a(full));
});

test('hair strands gain detail and black stays black', () => {
  const sc = scene([{ ...FACE, skin: WHEATISH, hair: [28, 24, 22] }], { grain: 5 });
  const hair = sc.regions[0].hair.filter((i) => ellipse(((i % sc.W) - FACE.cx) / FACE.d, (Math.floor(i / sc.W) - FACE.cy) / FACE.d, 0, -1.3, 0.8, 0.5));
  const out = run(sc, ask({ hairDetail: 100 }));
  assert.ok(stdev(out, hair) > stdev(sc.pixels, hair) * 1.25);
  assert.ok(meanLab(out, hair)[0] < 22, 'black is no longer black');
});

// ---------------------------------------------------------------------------
// Painted by hand
// ---------------------------------------------------------------------------

test('skin painted by hand where no face was found is retouched with the shared settings', () => {
  const W = 200, H = 160, pixels = new Uint8ClampedArray(W * H * 4);
  const rand = noise(9);
  for (let i = 0; i < W * H; i++) { const n = rand() * 6; pixels.set([200 + n, 150 + n, 120 + n, 255], i * 4); }
  const empty = new Uint8ClampedArray(W * H * 4), add = new Uint8Array(W * H);
  for (let y = 40; y < 120; y++) for (let x = 60; x < 140; x++) add[y * W + x] = 255;
  const maps = new Maps({ width: W, height: H, a: empty, b: empty, ids: empty, paint: { skinAdd: add } });
  const out = new Uint8ClampedArray(pixels);
  portraitPass({ pixels: out, width: W, height: H, maps, portrait: ask({ smooth: 100 }) });
  const inside = [], outside = [];
  for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) ((x > 62 && x < 138 && y > 42 && y < 118) ? inside : outside).push(y * W + x);
  assert.ok(stdev(out, inside) < stdev(pixels, inside) * 0.75);
  assert.ok(identical(pixels, out, outside.filter((i) => { const x = i % W, y = Math.floor(i / W); return x < 50 || x > 150 || y < 30 || y > 130; })));
});

test('erasing with the brush takes skin out of the retouch', () => {
  const sc = scene([{ ...FACE, skin: WHEATISH }], { grain: 6 });
  const erase = new Uint8Array(sc.maps.width * sc.maps.height);
  for (let y = 0; y < sc.H; y++) for (let x = 0; x < sc.W; x++) if (x < FACE.cx) erase[y * sc.maps.width + x] = 255;
  sc.maps.paint = { skinErase: erase };
  sc.maps.cache = null;
  const out = run(sc, ask({ smooth: 100 }));
  const left = sc.regions[0].skin.filter((i) => i % sc.W < FACE.cx - 3), right = sc.regions[0].skin.filter((i) => i % sc.W > FACE.cx + 3);
  assert.equal(changed(sc.pixels, out, left), 0);
  assert.ok(changed(sc.pixels, out, right) > 100);
});

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

test('suggestions become each face\'s own settings, and only where something stood out', () => {
  const portrait = blank();
  const given = applySuggestions(portrait, [
    { id: 1, usable: true, suggest: { faceLight: 70, tone: -40, smooth: 999, nonsense: 5 } },
    { id: 2, usable: true, suggest: {} },
    { id: 3, usable: false, suggest: { shine: 50 } },
  ]);
  assert.equal(given, 1);
  assert.deepEqual(portrait.faces[1], { faceLight: 70, tone: -40, smooth: 100 });
  assert.equal(portrait.faces[2], undefined);
  assert.equal(portrait.faces[3], undefined);
  assert.equal(isNeutral(portrait), false);
  assert.equal(isNeutral(blank()), true);
});

test('history can hold a portrait without the sliders rewriting it afterwards', () => {
  const portrait = blank();
  portrait.all.smooth = 10;
  const saved = copy(portrait);
  portrait.all.smooth = 90;
  portrait.faces[1] = { shine: 40 };
  assert.equal(saved.all.smooth, 10);
  assert.deepEqual(saved.faces, {});
});

test('smoothstep is 0 below, 1 above, and runs either way', () => {
  assert.equal(smoothstep(0, 10, -5), 0);
  assert.equal(smoothstep(0, 10, 15), 1);
  assert.equal(smoothstep(10, 0, 15), 0);
  assert.ok(Math.abs(smoothstep(0, 10, 5) - 0.5) < 1e-9);
});

// ---------------------------------------------------------------------------
// Natural skin tone
// ---------------------------------------------------------------------------

import { naturalTone, balance, SKIN_LINE } from '../ninaivu/static/js/studio/natural-tone.mjs';

const faceOf = (lightness, hue, chroma = 24) => ({ usable: true, skin: { lightness, hue, chroma } });
const hueAfter = (face, { warmth = 0, tint = 0 }) => {
  const radians = face.skin.hue * Math.PI / 180;
  const rgb = balance(labToRgb(face.skin.lightness, face.skin.chroma * Math.cos(radians), face.skin.chroma * Math.sin(radians)), warmth, tint);
  const [, a, b] = rgbToLab(...rgb.map(Math.round));
  return Math.atan2(b, a) * 180 / Math.PI;
};

test('a cast that turns every face yellow is turned back, and only to the edge of natural', () => {
  const faces = [faceOf(60, 74), faceOf(45, 72), faceOf(70, 77)];          // a tungsten room
  const found = naturalTone(faces);
  assert.ok(found && (found.warmth !== 0 || found.tint !== 0), `something is offered: ${JSON.stringify(found)}`);
  for (const face of faces) {
    const after = hueAfter(face, found);
    assert.ok(Math.abs(after - SKIN_LINE) < 12 && Math.abs(after - SKIN_LINE) < Math.abs(face.skin.hue - SKIN_LINE), `${face.skin.hue} → ${after}`);
  }
});

test('a pink cast is turned towards yellow, and a yellow one the other way', () => {
  const faces = [faceOf(65, 22), faceOf(58, 24)];
  const found = naturalTone(faces);
  assert.ok(found, 'a cast is found');
  for (const face of faces) assert.ok(hueAfter(face, found) > face.skin.hue + 8, `${face.skin.hue} → ${hueAfter(face, found)}`);
  const yellow = [faceOf(62, 76)];
  assert.ok(hueAfter(yellow[0], naturalTone(yellow)) < 76 - 8);
});

test('skin that is already natural is left alone, whatever its complexion', () => {
  for (const lightness of [30, 45, 62, 78]) assert.equal(naturalTone([faceOf(lightness, 48), faceOf(lightness, 52)]), null, `lightness ${lightness}`);
});

test('a dark face is judged by its hue and never asked to be paler', () => {
  const found = naturalTone([faceOf(33, 70, 18)]);
  assert.ok(found, 'a yellow cast on dark skin is a cast');
  const after = hueAfter(faceOf(33, 70, 18), found);
  assert.ok(after < 70);
});

test('nothing is offered when there is nothing to judge by', () => {
  assert.equal(naturalTone([]), null);
  assert.equal(naturalTone([{ usable: false, skin: { lightness: 50, hue: 90, chroma: 30 } }]), null);
  assert.equal(naturalTone([faceOf(20, 90, 30)]), null, 'a face in deep shadow has no hue worth judging');
  assert.equal(naturalTone([faceOf(60, 90, 5)]), null, 'skin with no colour has no hue');
});

// ---------------------------------------------------------------------------
// What is painted
// ---------------------------------------------------------------------------

import { Layer } from '../ninaivu/static/js/studio/layer.js';

test('a brush paints a soft round dab and erases it again', () => {
  const layer = new Layer(60, 40);
  assert.equal(layer.any(), false);
  assert.equal(layer.stamp(30, 20, 10), true);
  assert.equal(layer.bytes[20 * 60 + 30], 255, 'solid in the middle');
  assert.ok(layer.bytes[20 * 60 + 38] > 0 && layer.bytes[20 * 60 + 38] < 255, 'soft at the edge');
  assert.equal(layer.bytes[20 * 60 + 45], 0, 'nothing beyond it');
  assert.equal(layer.any(), true);
  layer.stamp(30, 20, 10, { erase: true });
  assert.equal(layer.bytes[20 * 60 + 30], 0);
  assert.ok(Math.max(...layer.bytes) < 50, 'run over its own dab, the eraser leaves nothing but the faintest edge');
});

test('a brush that follows colour paints only where the colour is near', () => {
  const layer = new Layer(40, 20);
  layer.stamp(20, 10, 8, { weight: (x) => (x < 20 ? 1 : 0) });
  assert.ok(layer.bytes[10 * 40 + 16] > 200 && layer.bytes[10 * 40 + 24] === 0);
});

test('history shares a copy of a layer that has not changed, and puts back one that has', () => {
  const layer = new Layer(30, 30);
  layer.stamp(15, 15, 6);
  const first = layer.snapshot();
  assert.equal(layer.snapshot(), first, 'the same copy while nothing has changed');
  layer.stamp(5, 5, 4);
  const second = layer.snapshot();
  assert.notEqual(second, first);
  assert.equal(first.bytes[5 * 30 + 5], 0, 'the earlier copy did not change with the layer');
  layer.restore(first);
  assert.equal(layer.bytes[5 * 30 + 5], 0);
  assert.equal(layer.bytes[15 * 30 + 15], 255);
  layer.clear();
  assert.equal(layer.any(), false);
  assert.equal(layer.snapshot().bytes[15 * 30 + 15], 0, 'and a cleared layer is a new state, not the old copy');
});
