/**
 * A painted area: one byte a pixel, how much of each place is in it.
 *
 * Every area somebody paints in the studio — where a brush lightens, where skin
 * was missed, where hair was wrongly taken — is one of these, and all of them
 * live in *the photograph's own space*, at a fixed size, not in whatever the
 * window is showing. That is what lets a photograph be cropped, turned or
 * straightened after something has been painted on it and have the paint go
 * with the picture; the editor used to keep them in the view's pixels and
 * stretched them over a new crop, so the brush work slid off the face it was
 * drawn on.
 *
 * History keeps copies of them, and a photograph at 1600 × 1067 makes each copy
 * 1.7 MB. A layer remembers which of its states has already been copied
 * (`version`), so a snapshot taken while a layer has not changed shares the copy
 * before it instead of making another.
 */

export class Layer {
  constructor(width, height) {
    this.w = width;
    this.h = height;
    this.bytes = new Uint8Array(width * height);
    this.version = 0;
    this.saved = null;          // { version, bytes }: the last copy made, reused while nothing has changed
    this.drawn = null;          // { version, canvas }: a picture of the layer, for drawing it
  }

  /** Is anything painted? */
  any() {
    const b = this.bytes;
    for (let i = 0; i < b.length; i++) if (b[i]) return true;
    return false;
  }

  clear() {
    this.bytes.fill(0);
    this.version++;
  }

  /**
   * One dab of a soft round brush, in this layer's own pixels. `weight(x, y)`
   * says how much a place takes, 0 to 1, for a brush that follows colour;
   * `erase` takes paint away instead of adding it.
   */
  stamp(cx, cy, radius, { erase = false, weight = null } = {}) {
    const x0 = Math.max(0, Math.floor(cx - radius)), x1 = Math.min(this.w - 1, Math.ceil(cx + radius));
    const y0 = Math.max(0, Math.floor(cy - radius)), y1 = Math.min(this.h - 1, Math.ceil(cy + radius));
    let touched = false;
    for (let y = y0; y <= y1; y++) {
      for (let x = x0; x <= x1; x++) {
        const distance = Math.hypot(x + 0.5 - cx, y + 0.5 - cy) / radius;
        if (distance >= 1) continue;
        const strength = Math.min(1, (1 - distance) * 3) * (weight && !erase ? weight(x, y) : 1);
        if (strength <= 0) continue;
        const i = y * this.w + x, was = this.bytes[i];
        // Taking paint away is twice as firm as putting it on, so that a brush run over
        // its own dab takes all of it and not all but a faint ring at the soft edge.
        const next = erase ? Math.round(was * (1 - Math.min(1, strength * 2))) : Math.max(was, Math.round(strength * 255));
        if (next !== was) { this.bytes[i] = next; touched = true; }
      }
    }
    if (touched) this.version++;
    return touched;
  }

  /** What to put in history: the bytes, shared with the last copy if the layer has not changed since. */
  snapshot() {
    if (!this.saved || this.saved.version !== this.version) this.saved = { version: this.version, bytes: this.bytes.slice() };
    return this.saved;
  }

  restore(snapshot) {
    if (!snapshot || snapshot.bytes.length !== this.bytes.length) return;
    this.bytes.set(snapshot.bytes);
    this.version++;
    this.saved = { version: this.version, bytes: snapshot.bytes };     // the next snapshot shares this copy, which is never written to
  }

  /** A canvas of the layer, white with the paint as its alpha, for drawing through a transform. Kept until the layer changes. */
  canvas() {
    if (this.drawn && this.drawn.version === this.version) return this.drawn.canvas;
    const canvas = this.drawn ? this.drawn.canvas : Object.assign(document.createElement('canvas'), { width: this.w, height: this.h });
    const image = new ImageData(this.w, this.h);
    const data = image.data;
    for (let i = 0, j = 0; i < this.bytes.length; i++, j += 4) {
      data[j] = data[j + 1] = data[j + 2] = 255;
      data[j + 3] = this.bytes[i];
    }
    canvas.getContext('2d').putImageData(image, 0, 0);
    this.drawn = { version: this.version, canvas };
    return canvas;
  }
}
