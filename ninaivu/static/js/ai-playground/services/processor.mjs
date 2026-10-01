import {validate} from '../models/adjustments.mjs';
import {developAsync} from '../../studio/develop.mjs';

/**
 * Sudar's adjustments in the engine's vocabulary. Sudar keeps its own, smaller
 * set of names — they are what Gemini and the suggestions speak — and each one
 * maps onto the engine's key of the same meaning. Sudar's exposure was a little
 * gentler per step than the engine's, so it is scaled to keep its old reach.
 */
export function recipeFor(a) {
  return {
    exposure: a.exposure * 0.9, contrast: a.contrast, saturation: a.saturation, warmth: a.warmth,
    shadows: a.shadows, highlights: a.highlights, vignette: a.vignette,
    sharpen: a.sharpness, sharpenMasking: a.sharpness ? 20 : 0,
    noise: a.noise, colourNoise: a.noise,
  };
}
export async function render(bitmap, adjustments, maxSide = Infinity) {
  const a = validate(adjustments);
  const scale = Math.min(1, maxSide / Math.max(bitmap.width, bitmap.height));
  let sw = bitmap.width, sh = bitmap.height;
  const ratio = {square: 1, landscape: 16/9, portrait: 4/5}[a.crop];
  if (ratio) { if (sw/sh > ratio) sw = sh*ratio; else sh = sw/ratio; }
  const w = Math.max(1, Math.round(sw*scale)), h = Math.max(1, Math.round(sh*scale));
  const canvas = typeof OffscreenCanvas !== 'undefined' ? new OffscreenCanvas(w,h) : Object.assign(document.createElement('canvas'), {width:w,height:h});
  const ctx = canvas.getContext('2d', {willReadFrequently: true});
  ctx.save(); ctx.translate(w/2,h/2);
  const angle = a.angle*Math.PI/180;
  // Zoom to avoid empty corners when straightening.
  const zoom = Math.max(Math.cos(angle)+Math.abs(Math.sin(angle))*h/w, Math.cos(angle)+Math.abs(Math.sin(angle))*w/h);
  ctx.rotate(angle); ctx.scale(zoom,zoom);
  ctx.drawImage(bitmap, (bitmap.width-sw)/2, (bitmap.height-sh)/2, sw,sh, -w/2,-h/2,w,h); ctx.restore();
  // The pixel work is the Photo Studio's own engine (studio/develop.mjs), so a
  // slider here means what the same slider means there. Detail is sized to the
  // photograph at full size, so a preview and the saved copy agree.
  const pixels = ctx.getImageData(0,0,w,h);
  const developed = await developAsync(pixels.data, w, h, recipeFor(a), {fullWidth: Math.round(sw)});
  pixels.data.set(developed);
  ctx.putImageData(pixels,0,0);
  return canvas;
}
