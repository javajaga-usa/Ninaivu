/**
 * Is the colour of the skin in this photograph the colour of skin?
 *
 * Indoor light is yellow, a shaded courtyard is blue, a flash on a wall is pink,
 * and a camera that guessed the white balance wrong turns everything one way. The
 * faces are the best thing in a family photograph to judge it by: skin of every
 * complexion lies along one line in colour (see `SKIN_LINE` in media/portrait.py),
 * so a face that sits well off it is a cast, not a complexion.
 *
 * This finds the warmth and tint that turn the faces back towards that line — and
 * only as far as the edge of the band natural skin varies in, never all the way to
 * an average, and never by touching lightness. Nothing here makes anybody paler.
 * It is a suggestion: it moves two sliders, and they can be moved back.
 */

import { SRGB_TO_LINEAR, encode, labToRgb, rgbToLab } from './colour.mjs';
import { whiteBalance } from './develop.mjs';

/** Where skin sits on the hue circle of the a*-b* plane, in degrees. */
export const SKIN_LINE = 48;
/** Complexions differ within this many degrees of it; a face inside the band has no cast. */
export const BAND = 6;

/** What the editor's warmth and tint sliders do to one colour (0-255): the engine's own white balance, in linear light. */
export function balance(rgb, warmth, tint) {
  const gains = whiteBalance(warmth, tint);
  return rgb.map((v, c) => encode(SRGB_TO_LINEAR[Math.max(0, Math.min(255, Math.round(v)))] * gains[c]));
}

/** The hue of a colour in degrees, or null when it has too little colour for a hue to mean anything. */
function hueOf([r, g, b]) {
  const [, a, bb] = rgbToLab(Math.round(r), Math.round(g), Math.round(b));
  return Math.hypot(a, bb) > 8 ? Math.atan2(bb, a) * 180 / Math.PI : null;
}

const median = (values) => {
  const sorted = [...values].sort((x, y) => x - y);
  const mid = sorted.length >> 1;
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
};

/**
 * The warmth and tint to offer for these faces (`{ warmth, tint }`), or null when
 * their skin is already inside the band, or there is nothing to judge by.
 *
 * @param faces  the analysis's faces, each with `skin: { lightness, hue, chroma }`, the colour its skin was measured to be
 */
export function naturalTone(faces) {
  const colours = [];
  for (const face of faces || []) {
    const skin = face && face.usable !== false && face.skin;
    // A face in deep shadow, or with hardly any colour, has no hue worth judging.
    if (!skin || !(skin.lightness > 28) || !(skin.chroma > 8)) continue;
    const radians = skin.hue * Math.PI / 180;
    colours.push(labToRgb(skin.lightness, skin.chroma * Math.cos(radians), skin.chroma * Math.sin(radians)));
  }
  if (!colours.length) return null;

  const excess = (warmth, tint) => {
    const offsets = colours.map((c) => hueOf(balance(c, warmth, tint))).filter((h) => h !== null).map((h) => Math.abs(h - SKIN_LINE));
    return offsets.length ? median(offsets.map((o) => Math.max(0, o - BAND))) : Infinity;
  };
  if (excess(0, 0) < 3) return null;

  let best = { warmth: 0, tint: 0, cost: excess(0, 0) };
  for (let warmth = -50; warmth <= 50; warmth += 2) {
    for (let tint = -30; tint <= 30; tint += 2) {
      // A little cost for how far the sliders move, so the gentlest of equals is chosen.
      const cost = excess(warmth, tint) + 0.03 * (Math.abs(warmth) + Math.abs(tint));
      if (cost < best.cost - 1e-9) best = { warmth, tint, cost };
    }
  }
  return best.warmth || best.tint ? { warmth: best.warmth, tint: best.tint } : null;
}
