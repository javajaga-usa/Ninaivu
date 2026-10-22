/**
 * Motion shared by the family app: a photograph that grows out of its tile
 * when it opens and shrinks back into it when it closes, the way a phone's
 * photo app does.
 *
 * One picture flies, on a layer of its own, for a quarter of a second: the
 * tile's own thumbnail, already decoded, so nothing is fetched or decoded for
 * the trip. Only `transform`, `clip-path` and `opacity` change, so the page
 * underneath is never laid out again while it moves, and a scroll of
 * thousands of tiles is no slower for it (no layer per tile: only the one
 * picture in flight has one, for as long as it flies).
 *
 * Anyone who has asked their device for less motion gets none: the viewer
 * simply appears and goes, as it always did.
 */

/** Whether this person has asked for less motion. Read each time: the
 *  setting can change while the page is open. */
export function calm() {
  try { return window.matchMedia('(prefers-reduced-motion: reduce)').matches; } catch { return false; }
}

const GROW_MS = 280;
const SHRINK_MS = 240;
/** Out of the tile quickly, into place gently (and the reverse on the way back). */
const EASE = 'cubic-bezier(.2, .8, .2, 1)';

/** Where a picture of `w` × `h` is drawn when fitted inside `box`
 *  (object-fit: contain), in page coordinates. */
export function fitInside(box, w, h) {
  if (!w || !h || !box.width || !box.height) return null;
  const scale = Math.min(box.width / w, box.height / h);
  const width = w * scale, height = h * scale;
  return {
    left: box.left + (box.width - width) / 2,
    top: box.top + (box.height - height) / 2,
    width, height,
  };
}

/** The box an element lays its content out in: its border box less its
 *  padding. The viewer's stage is padded clear of its toolbar and filmstrip,
 *  and the photograph is fitted inside what is left. */
export function contentBox(node) {
  const rect = node.getBoundingClientRect();
  const style = getComputedStyle(node);
  const px = (name) => parseFloat(style[name]) || 0;
  return {
    left: rect.left + px('paddingLeft'),
    top: rect.top + px('paddingTop'),
    width: rect.width - px('paddingLeft') - px('paddingRight'),
    height: rect.height - px('paddingTop') - px('paddingBottom'),
  };
}

/** On screen, at least partly: a tile scrolled out of sight is no place to
 *  shrink back into. */
function visible(rect) {
  return rect && rect.width > 1 && rect.height > 1
    && rect.bottom > 0 && rect.right > 0
    && rect.top < window.innerHeight && rect.left < window.innerWidth;
}

/**
 * The transform and clip that make a picture laid out at `full` look exactly
 * like the tile at `tile`: scaled until it covers the tile, centred on it, and
 * cut to the tile's shape and rounded corners.
 */
function asTile(full, tile, radius) {
  const scale = Math.max(tile.width / full.width, tile.height / full.height);
  const dx = (tile.left + tile.width / 2) - (full.left + full.width / 2);
  const dy = (tile.top + tile.height / 2) - (full.top + full.height / 2);
  // The clip is in the picture's own, unscaled, coordinates.
  const insetX = Math.max(0, (full.width - tile.width / scale) / 2);
  const insetY = Math.max(0, (full.height - tile.height / scale) / 2);
  const round = radius / scale;
  return {
    transform: `translate(${dx}px, ${dy}px) scale(${scale})`,
    clipPath: `inset(${insetY}px ${insetX}px round ${round}px)`,
  };
}

const AS_ITSELF = { transform: 'translate(0px, 0px) scale(1)', clipPath: 'inset(0px 0px round 0px)' };

/** A picture laid out at `rect`, fixed to the screen, on `host`. */
function flyer(src, rect, host) {
  const img = document.createElement('img');
  img.className = 'flying-picture';
  img.alt = '';
  img.decoding = 'sync';
  img.src = src;
  Object.assign(img.style, {
    left: `${rect.left}px`, top: `${rect.top}px`,
    width: `${rect.width}px`, height: `${rect.height}px`,
  });
  host.append(img);
  return img;
}

/** Ready to show within a frame or two, or not worth waiting for: a picture
 *  that has to be fetched would hold the viewer back, and opening at once is
 *  better than opening late. */
async function readyQuickly(img) {
  if (img.complete && img.naturalWidth) return true;
  const decoded = img.decode ? img.decode().then(() => true, () => false) : Promise.resolve(false);
  const late = new Promise((resolve) => setTimeout(() => resolve(false), 90));
  return Promise.race([decoded, late]);
}

const finished = (animation) => animation.finished.catch(() => {});

/**
 * Grow a photograph out of its tile into the viewer.
 *
 *   tile     the grid's tile: its picture is the one that flies
 *   stage    the viewer's stage, where the photograph will be drawn
 *   width,   the photograph's shape, upright (the tile's thumbnail has it)
 *   height
 *   viewer   the viewer's root: its backdrop fades in under the picture
 *
 * Resolves when the picture has landed, with the picture itself: the viewer
 * takes it away once its own copy is on screen, so there is never a frame
 * with neither. Resolves with nothing, at once, when nothing flies and the
 * viewer should simply appear.
 */
export async function growFrom({ tile, stage, viewer }) {
  if (calm() || !tile || !stage || !viewer) return null;
  const thumb = tile.querySelector('img');
  if (!thumb || thumb.hidden || !thumb.complete || !thumb.naturalWidth) return null;
  const from = tile.getBoundingClientRect();
  if (!visible(from)) return null;
  const to = fitInside(contentBox(stage), thumb.naturalWidth, thumb.naturalHeight);
  if (!to) return null;

  const img = flyer(thumb.currentSrc || thumb.src, to, viewer);
  if (!(await readyQuickly(img))) { img.remove(); return null; }
  const radius = parseFloat(getComputedStyle(tile).borderTopLeftRadius) || 0;
  const flight = img.animate([asTile(to, from, radius), AS_ITSELF],
                             { duration: GROW_MS, easing: EASE, fill: 'both' });
  const backdrop = viewer.animate([{ backgroundColor: 'rgba(10, 9, 8, 0)' }, { backgroundColor: 'rgba(10, 9, 8, 1)' }],
                                  { duration: GROW_MS, easing: 'ease-out' });
  await finished(flight);
  await finished(backdrop);
  return img;
}

/**
 * Shrink the photograph on screen back into its tile, after the viewer has
 * closed and the grid has scrolled to it.
 *
 *   src      a picture of it already in the browser (its thumbnail)
 *   from     where the photograph was drawn in the viewer
 *   tile     the tile to land in, or nothing: then it fades where it is
 */
export async function shrinkTo({ src, from, tile }) {
  if (calm() || !src || !from) return;
  const layer = document.createElement('div');
  layer.className = 'flying-layer';
  const img = flyer(src, from, layer);
  document.body.append(layer);
  if (!(await readyQuickly(img))) { layer.remove(); return; }

  const to = tile?.getBoundingClientRect();
  const thumb = tile?.querySelector('img');
  const landing = visible(to);
  // The tile's own picture waits under the one flying in, so it is not shown twice.
  if (landing && thumb) thumb.style.visibility = 'hidden';
  const radius = landing ? parseFloat(getComputedStyle(tile).borderTopLeftRadius) || 0 : 0;
  const frames = landing
    ? [AS_ITSELF, asTile(from, to, radius)]
    : [{ ...AS_ITSELF, opacity: 1 }, { transform: 'translate(0px, 0px) scale(.92)', clipPath: AS_ITSELF.clipPath, opacity: 0 }];
  const flight = img.animate(frames, { duration: SHRINK_MS, easing: EASE, fill: 'both' });
  const backdrop = layer.animate([{ backgroundColor: 'rgba(10, 9, 8, 1)' }, { backgroundColor: 'rgba(10, 9, 8, 0)' }],
                                 { duration: SHRINK_MS, easing: 'ease-in', fill: 'both' });
  await finished(flight);
  await finished(backdrop);
  if (thumb) thumb.style.visibility = '';
  layer.remove();
}
