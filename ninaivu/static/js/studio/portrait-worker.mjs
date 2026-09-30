/* The faces, retouched off the page's thread.
 *
 * The AI studio's portrait dialog uses this; the Photo Studio's own worker does the
 * same thing inside its larger pipeline. It holds the maps of the faces between
 * renders, because they change when somebody paints or crops, not when a slider
 * moves, and does one thing: retouch the people in the pixels it is sent.
 */
import { Maps, portraitPass } from './portrait.mjs';

let held = null;

self.onmessage = ({ data }) => {
  if (data.type === 'maps') {
    const m = data.maps;
    held = { key: data.key, maps: new Maps({ width: m.width, height: m.height, a: m.a, b: m.b, c: m.c, ids: m.ids, paint: m.paint || {} }) };
    return;
  }
  try {
    const pixels = new Uint8ClampedArray(data.pixels);
    if (data.portrait && held && held.key === data.portrait.mapsKey) {
      portraitPass({ pixels, width: data.width, height: data.height, maps: held.maps, portrait: data.portrait });
    }
    self.postMessage({ id: data.id, pixels: pixels.buffer }, [pixels.buffer]);
  } catch (error) {
    self.postMessage({ id: data.id, error: String((error && error.message) || error) });
  }
};
