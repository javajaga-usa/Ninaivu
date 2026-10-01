import {failure} from '../utils/messages.mjs';
//: Loaded by Node tests and the image worker, so it names its English with a
//: local key() rather than importing i18n.js; the component showing the text
//: translates it with i18n.t() (see ../utils/messages.mjs).
const i18n = {key: (s) => s};
export const defaults = () => ({exposure: 0, contrast: 0, saturation: 0, vibrance: 0, warmth: 0, sharpness: 0, noise: 0, shadows: 0, highlights: 0, clarity: 0, dehaze: 0, vignette: 0, angle: 0, crop: 'original'});
export const CROPS = ['original', 'square', 'landscape', 'portrait', 'story'];
export function validate(a) {
  const result = defaults();
  if (!a || typeof a !== 'object' || Array.isArray(a)) throw new Error(i18n.key('Invalid adjustments.'));
  for (const key of Object.keys(a)) if (!Object.hasOwn(result,key)) throw failure(i18n.key('Unsupported adjustment: {name}'), {name: key});
  for (const key of ['exposure', 'contrast', 'saturation', 'vibrance', 'warmth', 'sharpness', 'noise', 'shadows', 'highlights', 'clarity', 'dehaze', 'vignette', 'angle']) {
    const v = a[key] ?? 0;
    const min = ['sharpness', 'noise', 'vignette'].includes(key) ? 0 : key === 'angle' ? -10 : -100;
    const max = key === 'angle' ? 10 : 100;
    if (!Number.isFinite(v) || v < min || v > max) throw failure(i18n.key('Invalid {name} adjustment.'), {name: key});
    result[key] = v;
  }
  if (!CROPS.includes(a.crop ?? 'original')) throw new Error(i18n.key('Invalid crop.'));
  result.crop = a.crop ?? 'original';
  return result;
}
export function analyze(data, width, height) {
  let sum = 0, squared = 0, red = 0, blue = 0, spread = 0, count = 0;
  for (let i = 0; i < data.length; i += 4) {
    if (data[i + 3] < 128) continue;
    const r = data[i], g = data[i+1], b = data[i+2];
    const l = .2126*r + .7152*g + .0722*b;
    sum += l; squared += l*l; red += r; blue += b;
    spread += Math.max(r,g,b)-Math.min(r,g,b); count++;
  }
  count = Math.max(1, count);
  return {brightness: sum/count, contrast: Math.sqrt(Math.max(0, squared/count-(sum/count)**2)), warmth: (red-blue)/count, color: spread/count, width, height};
}
export function suggestions(a) {
  const s = [];
  const add = (title, reason, patch) => s.push({title, reason, patch});
  if (a.brightness < 105) add(i18n.key('Improve lighting'), i18n.key('The average light level is low.'), {exposure: 25});
  else if (a.brightness > 190) add(i18n.key('Fix exposure'), i18n.key('The image has a high average light level.'), {exposure: -20});
  if (a.contrast < 45) add(i18n.key('Adjust contrast'), i18n.key('The tonal range appears fairly flat.'), {contrast: 20});
  if (a.color < 40) add(i18n.key('Improve colors'), i18n.key('Colors appear muted; a gentle boost may help.'), {vibrance: 18});
  if (a.contrast < 45 && a.brightness > 150) add(i18n.key('Clear the haze'), i18n.key('Bright and flat, as a misty or backlit scene often is. Dehaze brings the depth back.'), {dehaze: 25});
  if (Math.abs(a.warmth) > 25) add(i18n.key('Improve white balance'), i18n.key('A color cast may be present; review before applying.'), {warmth: a.warmth > 0 ? -15 : 15});
  if (Math.min(a.width,a.height) < 1000) add(i18n.key('Improve sharpness'), i18n.key('This is a small image. Sharpening adds edge contrast, not resolution.'), {sharpness: 30});
  add(i18n.key('Auto enhance'), i18n.key('Try a gentle lighting and color adjustment.'), {exposure: a.brightness < 125 ? 12 : 0, contrast: 10, vibrance: 10, clarity: 6});
  if (a.width !== a.height) add(i18n.key('Social media crop'), i18n.key('Preview a centered square crop for a profile or social post.'), {crop: 'square'});
  return s;
}
