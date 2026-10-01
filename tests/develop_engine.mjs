/**
 * The develop engine, without a browser.
 *
 * Run with `node --test tests/develop_engine.mjs`. Every picture here is painted
 * into an array — a wall, a face in front of a window, a sky, a strip of silk —
 * and what is checked is what the engine does to the numbers.
 *
 * The promises worth a test:
 *   - a recipe that asks for nothing gives back the photograph, byte for byte;
 *   - light behaves like light: a stop is twice the light, and white balance
 *     changes the colour of a grey, not its brightness;
 *   - lifting the shadows on a face in front of a window lifts the face, keeps
 *     its colour, and leaves no halo on the window;
 *   - vibrance and every look leave skin its colour, and no look makes anybody
 *     paler;
 *   - a photograph worked in strips has no seams where the strips meet.
 */

import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  develop, developAsync, finish, histogram, whiteBalance, oklab, curveTable, bandWeights, skinness, toDisplay, toLinear,
} from '../ninaivu/static/js/studio/develop.mjs';
import { LOOKS, blank, effective, isIdentity, normalise, cleanCurve, RANGES } from '../ninaivu/static/js/studio/recipe.mjs';
import { SRGB_TO_LINEAR as LIN } from '../ninaivu/static/js/studio/colour.mjs';

// ---------------------------------------------------------------------------
// Pictures, painted
// ---------------------------------------------------------------------------

function rng(seed) {
  let s = seed >>> 0;
  return () => { s = (s * 1664525 + 1013904223) >>> 0; return s / 4294967296 - 0.5; };
}

/** A picture of W×H painted by `paint(x, y) → [r, g, b]`, with optional noise. */
function picture(W, H, paint, { noise = 0, seed = 3 } = {}) {
  const px = new Uint8ClampedArray(W * H * 4), rand = rng(seed);
  for (let y = 0; y < H; y++) {
    for (let x = 0; x < W; x++) {
      const i = (y * W + x) * 4, c = paint(x, y);
      const n = noise ? rand() * noise : 0;
      px[i] = c[0] + n; px[i + 1] = c[1] + n; px[i + 2] = c[2] + n; px[i + 3] = 255;
    }
  }
  return { px, W, H };
}

const flat = (W, H, c) => picture(W, H, () => c);
const run = (pic, recipe, opts) => develop(pic.px, pic.W, pic.H, { ...blank(), ...recipe }, opts);
const at = (pic, px, x, y) => { const i = (y * pic.W + x) * 4; return [px[i], px[i + 1], px[i + 2]]; };

/** The mean colour of a rectangle. */
function mean(pic, px, x0, y0, x1, y1) {
  const s = [0, 0, 0]; let n = 0;
  for (let y = y0; y < y1; y++) for (let x = x0; x < x1; x++) { const c = at(pic, px, x, y); s[0] += c[0]; s[1] += c[1]; s[2] += c[2]; n++; }
  return s.map((v) => v / n);
}

function std(pic, px, x0, y0, x1, y1, channel = 1) {
  let s = 0, s2 = 0, n = 0;
  for (let y = y0; y < y1; y++) for (let x = x0; x < x1; x++) { const v = at(pic, px, x, y)[channel]; s += v; s2 += v * v; n++; }
  return Math.sqrt(Math.max(0, s2 / n - (s / n) ** 2));
}

/** OKLab lightness, chroma and hue (degrees) of an 8-bit colour. */
function lch([r, g, b]) {
  const [L, a, bb] = oklab(LIN[Math.round(r)], LIN[Math.round(g)], LIN[Math.round(b)]);
  return { L, C: Math.hypot(a, bb), h: (Math.atan2(bb, a) * 180 / Math.PI + 360) % 360 };
}
const hueGap = (a, b) => { const d = Math.abs(a - b) % 360; return d > 180 ? 360 - d : d; };

/** Skin of several complexions, as a camera records it in fair light. */
const SKINS = { deep: [92, 58, 40], brown: [141, 85, 60], wheat: [198, 140, 104], light: [226, 184, 152] };

// ---------------------------------------------------------------------------
// The recipe
// ---------------------------------------------------------------------------

test('a blank recipe is the photograph, byte for byte', () => {
  const pic = picture(120, 80, (x, y) => [x * 2, y * 3, (x * y) % 256], { noise: 20 });
  assert.deepEqual(run(pic, {}), pic.px);
  assert.ok(isIdentity(blank()));
  // A look at no strength, and the numbers that only say how something is done, are still nothing.
  assert.ok(isIdentity({ ...blank(), look: 'film', lookAmount: 0, sharpenRadius: 20, toneHiHue: 120 }));
  assert.ok(!isIdentity({ ...blank(), look: 'film' }));
});

test('a recipe from anywhere is made safe: unknown keys dropped, numbers clamped, curves cleaned', () => {
  const r = normalise({ exposure: 900, contrast: 'x', nonsense: 4, look: 'no-such-look',
                        curve: { rgb: [[0.5, 0.5], [2, -1], [0.5, 0.9], 'x', [0, 0]] } });
  assert.equal(r.exposure, 100);
  assert.equal(r.contrast, 0);
  assert.equal('nonsense' in r, false);
  assert.equal(r.look, '');
  assert.deepEqual(r.curve.rgb, [[0, 0], [0.5, 0.5], [1, 0]]);
  assert.deepEqual(cleanCurve(null), [[0, 0], [1, 1]]);
  for (const [k, [min, max, neutral]] of Object.entries(RANGES)) assert.ok(min <= neutral && neutral <= max, k);
});

test('a look is laid over the sliders, and its amount scales it', () => {
  const half = effective({ ...blank(), look: 'festival', lookAmount: 50, vibrance: 10 });
  const full = LOOKS.find((l) => l.id === 'festival').recipe;
  assert.equal(half.vibrance, 10 + full.vibrance / 2);
  assert.equal(half.contrast, full.contrast / 2);
  const film = effective({ ...blank(), look: 'film', lookAmount: 50 });
  const [x, y] = film.lookCurve.rgb[0];
  assert.equal(x, 0);
  assert.ok(y > 0 && y < LOOKS.find((l) => l.id === 'film').recipe.curve.rgb[0][1], 'a faded black, faded by half');
});

// ---------------------------------------------------------------------------
// Light
// ---------------------------------------------------------------------------

test('a stop of exposure is twice the light', () => {
  const pic = flat(40, 30, [118, 118, 118]);
  const out = run(pic, { exposure: 40 });
  const before = LIN[118], after = LIN[out[0]];
  assert.ok(Math.abs(after / before - 2) < 0.06, `${before} → ${after}`);
  const down = run(pic, { exposure: -40 });
  assert.ok(Math.abs(LIN[down[0]] / before - 0.5) < 0.03);
});

test('white balance changes the colour of a grey, not its brightness', () => {
  assert.deepEqual(whiteBalance(0, 0).map((v) => +v.toFixed(6)), [1, 1, 1]);
  const pic = flat(20, 20, [128, 128, 128]);
  for (const [warmth, tint] of [[40, 0], [-40, 0], [0, 40], [0, -40], [30, -20]]) {
    const c = at(pic, run(pic, { warmth, tint }), 5, 5);
    const y = 0.2126 * LIN[c[0]] + 0.7152 * LIN[c[1]] + 0.0722 * LIN[c[2]];
    assert.ok(Math.abs(y - LIN[128]) / LIN[128] < 0.03, `${warmth}/${tint}: ${c}`);
  }
  const warm = at(pic, run(pic, { warmth: 50 }), 5, 5);
  assert.ok(warm[0] > warm[2] + 20, 'warmer is redder and less blue');
  const magenta = at(pic, run(pic, { tint: 50 }), 5, 5);
  assert.ok(magenta[1] < magenta[0] && magenta[1] < magenta[2], 'positive tint is magenta, as the label says');
});

test('contrast keeps black black, white white, and the order of every tone', () => {
  const pic = picture(256, 4, (x) => [x, x, x]);
  for (const contrast of [-100, -40, 40, 100]) {
    const out = run(pic, { contrast });
    assert.ok(out[0] <= 1 && out[255 * 4] >= 254, `${contrast}: ends held`);
    for (let x = 1; x < 256; x++) assert.ok(out[x * 4] >= out[(x - 1) * 4] - 1, `${contrast}: monotone at ${x}`);
  }
  const out = run(pic, { contrast: 60 });
  assert.ok(out[60 * 4] < 60 && out[200 * 4] > 200, 'shadows down, highlights up');
});

/** A face in shadow in front of a bright window: the picture everybody has taken. */
function backlit({ skin = SKINS.brown, shade = 0.35 } = {}) {
  const W = 240, H = 180;
  const rand = rng(11);
  return picture(W, H, (x, y) => {
    const inFace = ((x - 120) / 45) ** 2 + ((y - 100) / 60) ** 2 <= 1;
    if (inFace) {
      const t = 1 + rand() * 0.12;                 // the texture of skin
      return skin.map((c) => c * shade * t);
    }
    return [236, 238, 242];                        // the window
  });
}

test('lifting the shadows lifts a face in front of a window, and keeps its colour', () => {
  const pic = backlit();
  const out = run(pic, { shadows: 80 });
  const before = mean(pic, pic.px, 105, 85, 135, 115), after = mean(pic, out, 105, 85, 135, 115);
  const a = lch(before), b = lch(after);
  assert.ok(b.L > a.L + 0.08, `the face is lighter: ${a.L.toFixed(3)} → ${b.L.toFixed(3)}`);
  assert.ok(hueGap(a.h, b.h) < 4, `same hue: ${a.h.toFixed(1)} → ${b.h.toFixed(1)}`);
  assert.ok(b.C / b.L >= a.C / a.L * 0.9, 'not greyed: colour rises with the light');
  // The texture of the skin comes through: lifted, not flattened.
  assert.ok(std(pic, out, 105, 85, 135, 115) >= std(pic, pic.px, 105, 85, 135, 115) * 0.9, 'texture kept');
  // No halo: the window just beside the face is the window.
  const near = mean(pic, out, 60, 95, 70, 105), far = mean(pic, out, 5, 5, 15, 15);
  assert.ok(Math.abs(near[1] - far[1]) <= 3, `window beside the face ${near[1]} vs far away ${far[1]}`);
  assert.ok(Math.abs(far[1] - 238) <= 3, 'and the window itself is left alone');
});

test('true black stays black when the shadows are lifted', () => {
  const pic = picture(120, 90, (x) => (x < 60 ? [0, 0, 0] : [120, 120, 120]));
  const out = run(pic, { shadows: 100 });
  assert.ok(at(pic, out, 10, 45)[1] <= 3, `black: ${at(pic, out, 10, 45)}`);
});

test('recovering highlights brings a bright sky down and leaves the dark alone', () => {
  const pic = picture(200, 120, (x, y) => (y < 60 ? [228, 236, 246] : [40, 40, 42]));
  const out = run(pic, { highlights: -80 });
  assert.ok(mean(pic, out, 10, 10, 190, 40)[1] < 225, 'sky down');
  assert.ok(Math.abs(mean(pic, out, 10, 80, 190, 110)[1] - 40) <= 2, 'ground unchanged');
});

test('dehaze deepens a hazy scene and haze can be added back', () => {
  const pic = picture(160, 120, (x, y) => {
    const scene = ((x >> 4) + (y >> 4)) % 2 ? [40, 80, 40] : [120, 60, 50];
    return scene.map((c) => c * 0.5 + 200 * 0.5);          // half veiled by white haze
  });
  const clear = run(pic, { dehaze: 80 }), hazier = run(pic, { dehaze: -80 });
  const spread = (px) => std(pic, px, 0, 0, 160, 120, 0);
  assert.ok(spread(clear) > spread(pic.px) * 1.3, 'more contrast once the haze is out');
  assert.ok(spread(hazier) < spread(pic.px), 'less with more');
});

test('clarity adds local contrast in the midtones', () => {
  const pic = picture(160, 120, (x, y) => { const v = 110 + ((x >> 3) % 2 ? 12 : -12); return [v, v, v]; });
  assert.ok(std(pic, run(pic, { clarity: 80 }), 20, 20, 140, 100) > std(pic, pic.px, 20, 20, 140, 100) * 1.1);
  assert.ok(std(pic, run(pic, { clarity: -80 }), 20, 20, 140, 100) < std(pic, pic.px, 20, 20, 140, 100) * 0.95);
});

// ---------------------------------------------------------------------------
// Curves
// ---------------------------------------------------------------------------

test('a curve goes through its points, smoothly and without overshoot', () => {
  const table = curveTable([[0, 0], [0.25, 0.4], [0.75, 0.8], [1, 1]]);
  const read = (x) => table[Math.round(x * (table.length - 1))];
  assert.ok(Math.abs(read(0.25) - 0.4) < 0.01 && Math.abs(read(0.75) - 0.8) < 0.01);
  for (let i = 1; i < table.length; i++) assert.ok(table[i] >= table[i - 1] - 1e-6, 'monotone');
  const pic = picture(256, 2, (x) => [x, x, x]);
  const out = run(pic, { curve: { rgb: [[0, 0], [0.5, 0.62], [1, 1]] } });
  assert.ok(Math.abs(out[128 * 4] - 0.62 * 255) <= 3, `mid lifted to ${out[128 * 4]}`);
  const red = run(pic, { curve: { r: [[0, 0], [0.5, 0.35], [1, 1]] } });
  assert.ok(red[128 * 4] < 100 && Math.abs(red[128 * 4 + 1] - 128) <= 1, 'a channel curve moves only its channel');
});

// ---------------------------------------------------------------------------
// Colour
// ---------------------------------------------------------------------------

test('the bands of the mixer share every hue between two neighbours', () => {
  for (let h = 0; h < 360; h += 7) {
    const w = bandWeights(h);
    assert.ok(Math.abs(w.reduce((a, b) => a + b, 0) - 1) < 1e-3, `hue ${h}`);
    assert.ok(w.filter((v) => v > 1e-3).length <= 2);
  }
});

test('every complexion is recognised as skin, and sky, leaves and red silk are not', () => {
  for (const [name, c] of Object.entries(SKINS)) { const { h, C } = lch(c); assert.ok(skinness(h, C) > 0.5, name); }
  for (const c of [[120, 160, 220], [60, 120, 40], [180, 20, 40]]) { const { h, C } = lch(c); assert.ok(skinness(h, C) < 0.2, String(c)); }
});

test('vibrance lifts a muted sky and leaves skin nearly alone', () => {
  const sky = [150, 170, 200];
  for (const [name, skin] of Object.entries(SKINS)) {
    const pic = picture(40, 20, (x) => (x < 20 ? skin : sky));
    const out = run(pic, { vibrance: 100 });
    const s0 = lch(at(pic, pic.px, 5, 5)), s1 = lch(at(pic, out, 5, 5));
    const k0 = lch(at(pic, pic.px, 30, 5)), k1 = lch(at(pic, out, 30, 5));
    assert.ok(k1.C > k0.C * 1.4, `${name}: sky gains colour ${k0.C.toFixed(3)} → ${k1.C.toFixed(3)}`);
    assert.ok(s1.C < s0.C * 1.2, `${name}: skin barely moves ${s0.C.toFixed(3)} → ${s1.C.toFixed(3)}`);
    assert.ok(Math.abs(s1.L - s0.L) < 0.01 && hueGap(s0.h, s1.h) < 3, `${name}: nor its lightness or hue`);
    const down = lch(at(pic, run(pic, { vibrance: -100 }), 5, 5));
    assert.ok(down.C > s0.C * 0.7, `${name}: and turning it down does not leave skin ashen`);
  }
});

test('saturation at nothing is black and white', () => {
  const pic = flat(10, 10, [200, 80, 40]);
  const c = at(pic, run(pic, { saturation: -100 }), 2, 2);
  assert.ok(Math.max(...c) - Math.min(...c) <= 2, String(c));
});

test('the mixer turns, strengthens and lightens one band and leaves skin alone', () => {
  const blue = [40, 80, 200], green = [50, 150, 60], skin = SKINS.brown;
  const pic = picture(30, 10, (x) => (x < 10 ? blue : x < 20 ? green : skin));
  const out = run(pic, { hueBlue: 60, satGreen: -100 });
  assert.ok(hueGap(lch(at(pic, out, 5, 5)).h, lch(blue).h) > 8, 'blue turned');
  assert.ok(lch(at(pic, out, 15, 5)).C < lch(green).C * 0.2, 'green drained');
  const s = lch(at(pic, out, 25, 5)), s0 = lch(skin);
  assert.ok(Math.abs(s.C - s0.C) < 0.004 && hueGap(s.h, s0.h) < 1, 'skin untouched');
});

test('black and white takes its tones from the colours, as the mixer says', () => {
  const pic = picture(20, 10, (x) => (x < 10 ? [70, 120, 220] : SKINS.wheat));
  const plain = run(pic, { mono: 1 });
  const darkSky = run(pic, { mono: 1, lumBlue: -80 });
  for (const px of [plain, darkSky]) { const c = at(pic, px, 5, 5); assert.ok(Math.max(...c) - Math.min(...c) <= 2, 'grey'); }
  assert.ok(at(pic, darkSky, 5, 5)[1] < at(pic, plain, 5, 5)[1] - 10, 'a darker sky in black and white');
  assert.ok(Math.abs(at(pic, darkSky, 15, 5)[1] - at(pic, plain, 15, 5)[1]) <= 1, 'and the face as it was');
});

test('split toning colours the highlights and the shadows their own way', () => {
  const pic = picture(20, 10, (x) => (x < 10 ? [230, 230, 230] : [40, 40, 40]));
  const out = run(pic, { toneHiHue: 60, toneHiSat: 80, toneShHue: 240, toneShSat: 80 });
  const hi = at(pic, out, 5, 5), lo = at(pic, out, 15, 5);
  assert.ok(hi[0] > hi[2] + 5, `warm highlights ${hi}`);
  assert.ok(lo[2] > lo[0] + 5, `cool shadows ${lo}`);
});

// ---------------------------------------------------------------------------
// Detail
// ---------------------------------------------------------------------------

test('sharpening crisps an edge, and masking keeps it off flat noise', () => {
  const pic = picture(120, 60, (x) => (x < 60 ? [80, 80, 80] : [170, 170, 170]));
  const out = run(pic, { sharpen: 120 });
  assert.ok(at(pic, out, 58, 30)[1] < 78 && at(pic, out, 61, 30)[1] > 172, 'overshoot either side of the edge');
  assert.equal(at(pic, out, 20, 30)[1], 80, 'flat stays flat');
  const noisy = picture(120, 60, () => [128, 128, 128], { noise: 10 });
  const masked = run(noisy, { sharpen: 120, sharpenMasking: 100 });
  assert.ok(std(noisy, masked, 10, 10, 110, 50) <= std(noisy, noisy.px, 10, 10, 110, 50) * 1.05, 'noise not sharpened');
});

test('noise reduction quietens a flat patch and keeps an edge', () => {
  const pic = picture(160, 80, (x) => (x < 80 ? [90, 90, 90] : [170, 170, 170]), { noise: 24 });
  const out = run(pic, { noise: 80 });
  assert.ok(std(pic, out, 10, 10, 70, 70) < std(pic, pic.px, 10, 10, 70, 70) * 0.6, 'quieter');
  const step = mean(pic, out, 85, 10, 95, 70)[1] - mean(pic, out, 65, 10, 75, 70)[1];
  assert.ok(step > 70, `edge kept: ${step}`);
});

test('colour noise is taken out without moving the lightness', () => {
  const rand = rng(9);
  const pic = picture(120, 80, () => { const n = rand() * 40; return [120 + n, 120 - n, 120 + n * 0.5]; });
  const out = run(pic, { colourNoise: 100 });
  const chroma = (px) => { let s = 0; for (let i = 0; i < px.length; i += 4) s += Math.abs(px[i] - px[i + 1]); return s / (px.length / 4); };
  assert.ok(chroma(out) < chroma(pic.px) * 0.5, 'less colour speckle');
  assert.ok(Math.abs(mean(pic, out, 0, 0, 120, 80)[1] - mean(pic, pic.px, 0, 0, 120, 80)[1]) < 2);
});

test('grain adds texture and not brightness; a vignette darkens the corners and not the middle', () => {
  const pic = flat(200, 150, [128, 128, 128]);
  const grain = run(pic, { grain: 80 });
  assert.ok(std(pic, grain, 0, 0, 200, 150) > 2);
  assert.ok(Math.abs(mean(pic, grain, 0, 0, 200, 150)[1] - 128) < 1.5);
  const v = run(pic, { vignette: 80 });
  assert.ok(at(pic, v, 2, 2)[1] < 90 && Math.abs(at(pic, v, 100, 75)[1] - 128) <= 1);
  const light = run(pic, { vignette: -80 });
  assert.ok(at(pic, light, 2, 2)[1] > 150);
});

test('a photograph worked in strips has no seams where the strips meet', () => {
  // The same scene, the second copy dropped ten rows: every strip boundary
  // falls somewhere else, and the local work must not notice.
  const paint = (x, y) => [(x * 7 + y * 3) % 256, (x * y) % 256, (x + y * 5) % 256];
  const a = picture(90, 300, paint, { noise: 30, seed: 1 });
  const b = picture(90, 310, (x, y) => paint(x, y - 10), { noise: 0 });
  // The same noise in both, row for row.
  for (let y = 0; y < 300; y++) for (let x = 0; x < 90; x++) for (let c = 0; c < 3; c++) b.px[((y + 10) * 90 + x) * 4 + c] = a.px[(y * 90 + x) * 4 + c];
  const recipe = { sharpen: 100, noise: 60, colourNoise: 60, contrast: 20, vibrance: 20 };
  const oa = run(a, recipe), ob = run(b, recipe);
  let worst = 0;
  for (let y = 20; y < 280; y++) {
    for (let x = 0; x < 90; x++) {
      for (let c = 0; c < 3; c++) worst = Math.max(worst, Math.abs(oa[(y * 90 + x) * 4 + c] - ob[((y + 10) * 90 + x) * 4 + c]));
    }
  }
  assert.ok(worst <= 1, `largest difference ${worst}`);
});

test('the vignette and grain left for later come out the same as done at once', () => {
  const pic = picture(160, 120, (x, y) => [40 + x, 40 + y, 100]);
  const recipe = { ...blank(), exposure: 10, vignette: 60 };
  const once = develop(pic.px, 160, 120, recipe);
  const later = finish(develop(pic.px, 160, 120, recipe, { defer: true }), 160, 120, recipe);
  let worst = 0;
  for (let i = 0; i < once.length; i++) worst = Math.max(worst, Math.abs(once[i] - later[i]));
  assert.ok(worst <= 2, `largest difference ${worst}`);
});

test('the slow way round gives the same answer', async () => {
  const pic = picture(60, 250, (x, y) => [x * 4, y, 90], { noise: 10 });
  const recipe = { ...blank(), shadows: 30, sharpen: 50 };
  assert.deepEqual(await developAsync(pic.px, 60, 250, recipe), develop(pic.px, 60, 250, recipe));
});

test('the histogram counts every pixel once', () => {
  const pic = picture(50, 40, (x) => [x * 5, 0, 255]);
  const h = histogram(pic.px);
  assert.equal(h.r.reduce((a, b) => a + b, 0), 2000);
  assert.equal(h.b[255], 2000);
  assert.equal(histogram(pic.px, 4).g[0], 500);
});

test('the encoding tables agree with the formula', () => {
  for (const v of [0, 0.001, 0.01, 0.18, 0.5, 1]) {
    const exact = v <= 0.0031308 ? v * 12.92 : 1.055 * v ** (1 / 2.4) - 0.055;
    assert.ok(Math.abs(toDisplay(v) - exact) < 2e-4, `${v}`);
    assert.ok(Math.abs(toLinear(toDisplay(v)) - v) < 2e-4, `${v} round trip`);
  }
});

// ---------------------------------------------------------------------------
// Looks, held to the promise about skin
// ---------------------------------------------------------------------------

test('no look makes anybody paler or drains the colour from their skin', () => {
  for (const look of LOOKS) {
    for (const [name, skin] of Object.entries(SKINS)) {
      // A face against a mid wall, lit evenly: the looks must leave it its own.
      const pic = picture(160, 120, (x, y) => ((((x - 80) / 40) ** 2 + ((y - 60) / 50) ** 2 <= 1) ? skin : [128, 124, 118]));
      const out = run(pic, { look: look.id, lookAmount: 100 });
      const a = lch(mean(pic, pic.px, 70, 50, 90, 70)), b = lch(mean(pic, out, 70, 50, 90, 70));
      const what = `${look.id} on ${name} skin`;
      if (look.id !== 'backlit') {
        assert.ok(b.L <= a.L + 0.02, `${what}: lightness ${a.L.toFixed(3)} → ${b.L.toFixed(3)}`);
      } else {
        // The one look that is about light on faces in shadow: it may lift, and must keep the colour while it does.
        assert.ok(b.C / b.L >= a.C / a.L * 0.9, `${what}: lifted without greying`);
      }
      if (look.recipe.mono) continue;
      assert.ok(b.C >= a.C * 0.88, `${what}: colour ${a.C.toFixed(3)} → ${b.C.toFixed(3)}`);
      assert.ok(hueGap(a.h, b.h) < 7, `${what}: hue ${a.h.toFixed(1)} → ${b.h.toFixed(1)}`);
    }
  }
});

test('every look has a name and a sentence, and only keys the engine knows', () => {
  const ids = new Set();
  for (const look of LOOKS) {
    assert.ok(look.name && look.about && !ids.has(look.id));
    ids.add(look.id);
    for (const k of Object.keys(look.recipe)) assert.ok(k === 'curve' || k in RANGES, `${look.id}: ${k}`);
  }
  assert.ok(LOOKS.length >= 10);
});
