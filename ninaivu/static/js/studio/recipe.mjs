/**
 * What an edit is, as numbers: the recipe both editors hand to the engine.
 *
 * The Photo Studio and Sudar used to keep a vocabulary each, with the same word
 * meaning different things in each ("saturation" was vibrance in one of them).
 * This is the one vocabulary now. Every key has a range and a neutral value, and
 * a recipe that is neutral everywhere is the photograph as it was, byte for byte.
 *
 * Looks live here too, because a look is only a recipe with a name: it is added
 * to whatever the sliders say, scaled by an amount, so choosing one never throws
 * away the work already done and the sliders stay the person's own.
 *
 * Loaded by the workers and by Node's tests, so the English names are marked with
 * a local key() rather than by importing i18n.js; the panel that shows them
 * translates them.
 */

const i18n = { key: (s) => s };

/** The eight colour bands of the mixer, in hue order. */
export const BANDS = ['red', 'orange', 'yellow', 'green', 'aqua', 'blue', 'purple', 'magenta'];

const cap = (s) => s[0].toUpperCase() + s.slice(1);
export const bandKey = (kind, band) => `${kind}${cap(band)}`;

/** Every number a recipe may carry: [min, max, neutral]. */
export const RANGES = (() => {
  const r = {
    // light
    exposure: [-100, 100, 0], contrast: [-100, 100, 0],
    highlights: [-100, 100, 0], shadows: [-100, 100, 0],
    whites: [-100, 100, 0], blacks: [-100, 100, 0], dehaze: [-100, 100, 0],
    // colour
    warmth: [-100, 100, 0], tint: [-100, 100, 0],
    vibrance: [-100, 100, 0], saturation: [-100, 100, 0],
    mono: [0, 1, 0],
    toneHiHue: [0, 360, 45], toneHiSat: [0, 100, 0],
    toneShHue: [0, 360, 215], toneShSat: [0, 100, 0], toneBalance: [-100, 100, 0],
    // detail
    clarity: [-100, 100, 0],
    sharpen: [0, 150, 0], sharpenRadius: [5, 30, 10], sharpenMasking: [0, 100, 0],
    noise: [0, 100, 0], colourNoise: [0, 100, 0],
    grain: [0, 100, 0], grainSize: [0, 100, 25],
    vignette: [-100, 100, 0], vignetteMidpoint: [0, 100, 50], vignetteFeather: [0, 100, 50],
    // a look, and how much of it
    lookAmount: [0, 100, 100],
  };
  for (const band of BANDS) {
    r[bandKey('hue', band)] = [-100, 100, 0];
    r[bandKey('sat', band)] = [-100, 100, 0];
    r[bandKey('lum', band)] = [-100, 100, 0];
  }
  return r;
})();

/**
 * The keys that do something by themselves. The rest (a radius, a hue, a
 * midpoint) only say how something else is done, so they neither make a recipe
 * an edit nor are they added up when a look is laid over the sliders.
 */
export const EFFECTS = Object.keys(RANGES).filter((k) => ![
  'toneHiHue', 'toneShHue', 'sharpenRadius', 'sharpenMasking', 'grainSize',
  'vignetteMidpoint', 'vignetteFeather', 'lookAmount',
].includes(k));

export const CHANNELS = ['rgb', 'r', 'g', 'b'];
const straight = () => [[0, 0], [1, 1]];
export const identityCurve = () => Object.fromEntries(CHANNELS.map((c) => [c, straight()]));

/** A recipe that changes nothing. */
export function blank() {
  const out = Object.fromEntries(Object.entries(RANGES).map(([k, [, , n]]) => [k, n]));
  out.curve = identityCurve();
  out.look = '';
  return out;
}

export const isStraight = (points) => !points || (points.length === 2
  && points[0][0] === 0 && points[0][1] === 0 && points[1][0] === 1 && points[1][1] === 1);

/** A curve's points, made safe: 2 to 16 of them, inside the square, left to right. */
export function cleanCurve(points) {
  if (!Array.isArray(points)) return straight();
  const kept = [];
  for (const p of points.slice(0, 16)) {
    if (!Array.isArray(p) || p.length !== 2) continue;
    const x = Number(p[0]), y = Number(p[1]);
    if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
    kept.push([Math.min(1, Math.max(0, x)), Math.min(1, Math.max(0, y))]);
  }
  kept.sort((a, b) => a[0] - b[0]);
  const out = [];
  for (const p of kept) if (!out.length || p[0] - out[out.length - 1][0] >= 0.01) out.push(p);
  return out.length >= 2 ? out : straight();
}

/** Any recipe-shaped thing, made into a recipe: unknown keys dropped, numbers clamped, gaps filled. */
export function normalise(input = {}) {
  const out = blank();
  if (!input || typeof input !== 'object') return out;
  for (const [k, [min, max]] of Object.entries(RANGES)) {
    const v = Number(input[k]);
    if (input[k] != null && Number.isFinite(v)) out[k] = Math.min(max, Math.max(min, v));
  }
  if (input.curve && typeof input.curve === 'object') {
    for (const c of CHANNELS) out.curve[c] = cleanCurve(input.curve[c]);
  }
  out.look = LOOKS.some((l) => l.id === input.look) ? input.look : '';
  return out;
}

/** Does this recipe do anything at all? */
export function isIdentity(recipe) {
  const r = recipe || {};
  for (const k of EFFECTS) if ((r[k] ?? RANGES[k][2]) !== RANGES[k][2]) return false;
  if (r.curve && !CHANNELS.every((c) => isStraight(r.curve[c]))) return false;
  if (r.look && (r.lookAmount ?? 100) > 0 && LOOKS.some((l) => l.id === r.look)) return false;
  return true;
}

/*
 * Looks, for family photographs.
 *
 * Every one of them was tuned against brown skin first, because that is who is
 * in the photographs. None raises the lightness of skin or drains its colour —
 * the engine's tests hold each look to that — and the ones that add colour add
 * it through vibrance, which leaves skin nearly alone. The two black-and-white
 * looks are the exception by definition, and they take lightness from the
 * photograph as it was rather than brightening anyone.
 */
export const LOOKS = [
  { id: 'natural', name: i18n.key('Natural'), about: i18n.key('A little more depth and colour, and nothing you would notice as an effect.'),
    recipe: { contrast: 8, vibrance: 12, clarity: 6, blacks: -4 } },
  { id: 'festival', name: i18n.key('Festival'), about: i18n.key('Rich colour for silk, flowers and kolam, with skin kept as it is.'),
    recipe: { vibrance: 34, contrast: 9, clarity: 10, dehaze: 5, blacks: -8 } },
  { id: 'golden', name: i18n.key('Golden hour'), about: i18n.key('The warmth of late afternoon light.'),
    recipe: { warmth: 14, tint: 3, vibrance: 8, highlights: -10, contrast: 6,
              toneHiHue: 50, toneHiSat: 10, toneBalance: 20 } },
  { id: 'soft', name: i18n.key('Soft portrait'), about: i18n.key('Gentler contrast and softer texture, for faces close up.'),
    recipe: { contrast: -8, clarity: -18, highlights: -12, vibrance: 6 } },
  { id: 'backlit', name: i18n.key('Backlit rescue'), about: i18n.key('Opens up faces in front of a bright window or sky, without flattening the sky.'),
    recipe: { shadows: 42, highlights: -34, clarity: 8, vibrance: 8, blacks: -6 } },
  { id: 'revive', name: i18n.key('Revive old print'), about: i18n.key('Deepens the faded blacks and colour of an old print or scan.'),
    recipe: { contrast: 8, vibrance: 18, clarity: 10, blacks: -18, colourNoise: 30 } },
  { id: 'film', name: i18n.key('Film'), about: i18n.key('Softened blacks, quieter colour and a fine grain.'),
    recipe: { vibrance: -14, contrast: 6, grain: 22, grainSize: 30,
              curve: { rgb: [[0, 0.05], [0.25, 0.245], [0.75, 0.755], [1, 0.975]] } } },
  { id: 'monsoon', name: i18n.key('Monsoon'), about: i18n.key('Cool, deep and quiet, for grey skies and rain.'),
    recipe: { warmth: -6, contrast: 10, highlights: -18, dehaze: 8, vibrance: -6,
              toneShHue: 205, toneShSat: 8, toneBalance: -30 } },
  { id: 'mono', name: i18n.key('Classic black and white'), about: i18n.key('Black and white with depth, taking its tones from the colours in the photograph.'),
    recipe: { mono: 1, contrast: 16, clarity: 8, lumBlue: -12, lumAqua: -6 } },
  { id: 'sepia', name: i18n.key('Warm black and white'), about: i18n.key('A warm-toned black and white, like an old family print.'),
    recipe: { mono: 1, contrast: 10, toneHiHue: 60, toneHiSat: 18, toneShHue: 40, toneShSat: 22, toneBalance: -10,
              curve: { rgb: [[0, 0.03], [0.5, 0.5], [1, 0.98]] } } },
];

const lerpCurve = (points, amount) => points.map(([x, y]) => [x, x + (y - x) * amount]);

/**
 * The recipe the engine is given: the sliders, with the chosen look laid over
 * them at its amount. Numbers add; a look's curve is scaled towards straight and
 * applied before the person's own; black and white fades in with the amount.
 */
export function effective(recipe) {
  const r = normalise(recipe);
  const look = LOOKS.find((l) => l.id === r.look);
  r.lookCurve = null;
  if (!look || !(r.lookAmount > 0)) return r;
  const amount = r.lookAmount / 100;
  for (const [k, v] of Object.entries(look.recipe)) {
    if (k === 'curve') continue;
    const [min, max, neutral] = RANGES[k];
    if (EFFECTS.includes(k)) r[k] = Math.min(max, Math.max(min, r[k] + v * amount));
    else if (r[k] === neutral) r[k] = v;           // how a look's effect is done, unless the person has said otherwise
  }
  if (look.recipe.curve) {
    r.lookCurve = Object.fromEntries(CHANNELS.map((c) => [c, lerpCurve(cleanCurve(look.recipe.curve[c] || straight()), amount)]));
  }
  return r;
}
