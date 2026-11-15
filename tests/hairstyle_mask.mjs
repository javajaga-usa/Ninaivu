// Sudar's face-safe hairstyles: what may be redrawn, and what is put back
// exactly. No browser and no model: synthetic maps of two people.
import assert from 'node:assert/strict';
import {test} from 'node:test';
import {hairstyleMasks, blendWeights, blendOnto, keepAt, REACH} from '../ninaivu/static/js/studio/hairstyle-mask.mjs';
import {HAIRSTYLES} from '../ninaivu/static/js/ai-playground/safety/commands.mjs';

const W = 320, H = 240;
// Two people side by side; eye distance 20 px. Frames as PortraitSession.faces() gives them.
const people = [
  {id: 1, cx: 100, cy: 90, d: 20},
  {id: 2, cx: 230, cy: 90, d: 20},
];
const faces = people.map(p => ({id: p.id, usable: true, frame: {x: p.cx / W, y: p.cy / H, d: p.d / W, cos: 1, sin: 0}}));
const uv = (p, x, y) => [(x - p.cx) / p.d, (y - p.cy) / p.d];

function maps() {
  const a = new Uint8ClampedArray(W * H * 4), b = new Uint8ClampedArray(W * H * 4), ids = new Uint8ClampedArray(W * H * 4);
  for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
    const k = (y * W + x) * 4;
    for (const p of people) {
      const [u, v] = uv(p, x, y);
      // Skin: the face oval and the neck.
      if ((u / 1.05) ** 2 + ((v - 0.5) / 1.45) ** 2 <= 1 || (Math.abs(u) < 0.6 && v > 1.8 && v < 2.8)) { a[k] = 230; ids[k] = p.id; }
      // A bindi between the brows.
      if (Math.hypot(u, v + 0.55) < 0.12) { a[k + 2] = 255; ids[k] = p.id; }
      // Hair: a cap over the top of the head.
      if (v < -0.9 && (u / 1.5) ** 2 + ((v + 0.9) / 1.4) ** 2 <= 1) { b[k] = 220; ids[k + 1] = p.id; }
    }
  }
  return {a, b, ids};
}
const M = maps();
const at = (mask, p, u, v) => mask[Math.round(p.cy + v * p.d) * W + Math.round(p.cx + u * p.d)];

test('the face, its bindi and the other person are kept; the hair is redrawn', () => {
  for (const kind of Object.keys(REACH)) {
    const {change, keep} = hairstyleMasks({width: W, height: H, maps: M, faces, target: [1], kind});
    const [me, other] = people;
    // Eyes, nose and the bindi of the person being restyled: kept, never changed.
    for (const [u, v] of [[-0.5, 0], [0.5, 0], [0, 0.4], [0, -0.55]]) {
      assert.equal(at(keep, me, u, v), 255, `${kind}: kept at ${u},${v}`);
      assert.equal(at(change, me, u, v), 0, `${kind}: not changed at ${u},${v}`);
    }
    // Nothing of the other person is touched.
    for (let i = 0; i < W * H; i++) {
      const x = i % W, y = Math.floor(i / W);
      const [u, v] = uv(other, x, y);
      if ((u / 1.2) ** 2 + ((v - 0.4) / 1.7) ** 2 <= 1 || M.ids[i * 4 + 1] === 2) assert.equal(change[i], 0, `${kind}: other person at ${x},${y}`);
    }
    // The two masks never overlap.
    for (let i = 0; i < W * H; i++) assert.ok(!(change[i] && keep[i]));
  }
});

test('each style reaches where it needs to and no further', () => {
  const [me] = people;
  const masks = kind => hairstyleMasks({width: W, height: H, maps: M, faces, target: [1], kind});
  const head = masks('head'), long = masks('long'), fringe = masks('fringe'), beard = masks('beard');
  // The hair that is there is redrawn for every head style.
  assert.equal(at(head.change, me, 0, -1.6), 255);
  // Long hair falls beside the shoulders, never across the neck and chest.
  assert.equal(at(long.change, me, -2, 3.5), 255);
  assert.equal(at(head.change, me, -2, 3.5), 0);
  assert.equal(at(long.change, me, 0, 3.5), 0);
  // A fringe may cover the forehead; the eyes stay.
  assert.equal(at(fringe.change, me, 0.4, -0.75), 255);
  assert.equal(at(head.keep, me, 0.4, -0.75), 255);
  // A beard is drawn on the jaw; the eyes and the hair on the head are not its business.
  assert.equal(at(beard.change, me, 0.6, 1.5), 255);
  assert.equal(at(beard.change, me, 0, -1.6), 0);
  assert.equal(at(beard.keep, me, 0.5, 0), 255);
});

test('what is kept comes back byte for byte, whatever the server drew and whatever was painted', () => {
  const {keep} = hairstyleMasks({width: W, height: H, maps: M, faces, target: [1], kind: 'long'});
  // Somebody painted the whole picture, and the server changed every pixel.
  const painted = new Uint8Array(W * H).fill(255);
  const weights = blendWeights(painted, keep, W, H, 3);
  // The photograph at twice the size of the masks, as in the studio.
  const FW = W * 2, FH = H * 2;
  const base = new Uint8ClampedArray(FW * FH * 4), drawn = new Uint8ClampedArray(FW * FH * 4);
  for (let i = 0; i < FW * FH; i++) { base.set([(i * 7) % 256, (i * 13) % 256, (i * 3) % 256, 255], i * 4); drawn.set([1, 2, 3, 255], i * 4); }
  const keepFull = keepAt(keep, W, H, FW, FH);
  const out = blendOnto(base, drawn, FW, FH, weights, W, H, keepFull);
  let kept = 0, changed = 0;
  for (let i = 0; i < FW * FH; i++) {
    const k = i * 4;
    if (keepFull[i]) { kept++; assert.deepEqual([...out.subarray(k, k + 4)], [...base.subarray(k, k + 4)], `pixel ${i}`); }
    else if (out[k] === 1 && out[k + 1] === 2) changed++;
  }
  assert.ok(kept > 1000, 'the faces are in the kept area');
  assert.ok(changed > FW * FH / 2, 'away from the faces the drawing is taken');
});

test('an unpainted pixel stays the photograph; the painted edge is soft', () => {
  const keep = new Uint8Array(W * H);
  const painted = new Uint8Array(W * H);
  for (let y = 50; y < 150; y++) for (let x = 50; x < 150; x++) painted[y * W + x] = 255;
  const weights = blendWeights(painted, keep, W, H, 3);
  assert.equal(weights[100 * W + 100], 1);
  assert.equal(weights[100 * W + 10], 0);
  assert.ok(weights[100 * W + 151] > 0 && weights[100 * W + 151] < 1);
});

test('every offered style says how much room it needs and what to draw', () => {
  for (const [title, request, kind, drawing] of HAIRSTYLES) {
    assert.ok(title && request && drawing, title);
    assert.ok(kind in REACH, `${title}: ${kind}`);
    assert.ok(drawing.length > 30 && !/\bface\b/i.test(drawing), `${title}: the drawing is about hair, not the face`);
  }
});
