/**
 * A face drawn on a canvas, and the maps the server would send of it — so that the
 * browser's half of Skin and Hair can be tested without a face detector.
 *
 * `SYNTHETIC` is the body of a module script: it defines `window.photoUrl` (the
 * picture, a 600 × 800 data URL) and `window.analysis` (what /api/portrait/analyse
 * answers with). The face is a wall, dark hair, a face a little darker on the left,
 * eyes, a mouth and a bindi, at eye distance 110 centred on (300, 330).
 */
export const SYNTHETIC = `
  const W = 600, H = 800, CX = 300, CY = 330, D = 110;
  const canvas = (w, h) => Object.assign(document.createElement('canvas'), { width: w, height: h });
  const ellipse = (ctx, u, v, ru, rv, fill) => {
    ctx.fillStyle = fill; ctx.beginPath(); ctx.ellipse(CX + u * D, CY + v * D, ru * D, rv * D, 0, 0, 7); ctx.fill();
  };

  // The photograph: a wall, dark hair, a face a little darker on the left, eyes, a mouth, a bindi.
  const photo = canvas(W, H), p = photo.getContext('2d');
  p.fillStyle = '#969aa2'; p.fillRect(0, 0, W, H);
  ellipse(p, 0, -0.95, 1.12, 1.0, '#1c1816');
  ellipse(p, -0.98, 0.7, 0.27, 1.3, '#1c1816'); ellipse(p, 0.98, 0.7, 0.27, 1.3, '#1c1816');
  ellipse(p, 0, 2.0, 0.6, 0.75, '#b08468');
  const skin = p.createLinearGradient(CX - D, 0, CX + D, 0);
  skin.addColorStop(0, '#9a6d52'); skin.addColorStop(1, '#d09a76');
  ellipse(p, 0, 0.34, 1.0, 1.45, skin);
  for (const s of [-1, 1]) { ellipse(p, s * 0.5, 0, 0.2, 0.09, '#e6e6e2'); ellipse(p, s * 0.5, 0, 0.085, 0.085, '#26180f'); }
  ellipse(p, 0, 1.0, 0.32, 0.075, '#a64250');
  ellipse(p, 0, -0.62, 0.06, 0.06, '#bc1620');
  window.photoUrl = photo.toDataURL();

  // The maps: what the server would say of it. Skin, then the light zone (the face and neck as one piece), then what is protected.
  const plane = (draw) => {
    const c = canvas(W, H), x = c.getContext('2d', { willReadFrequently: true });
    const image = x.createImageData(W, H);
    for (let y = 0; y < H; y++) for (let xx = 0; xx < W; xx++) {
      const u = (xx - CX) / D, v = (y - CY) / D, i = (y * W + xx) * 4;
      const [r, g, b] = draw(u, v); image.data[i] = r; image.data[i + 1] = g; image.data[i + 2] = b; image.data[i + 3] = 255;
    }
    x.putImageData(image, 0, 0);
    return c.toDataURL('image/png').split(',')[1];
  };
  const inside = (u, v, cu, cv, ru, rv) => ((u - cu) / ru) ** 2 + ((v - cv) / rv) ** 2 <= 1;
  const face = (u, v) => inside(u, v, 0, 0.34, 1.0, 1.45);
  const eyes = (u, v) => inside(u, v, -0.5, 0, 0.37, 0.22) || inside(u, v, 0.5, 0, 0.37, 0.22) || inside(u, v, 0, 1.0, 0.45, 0.22);
  const hair = (u, v) => !face(u, v) && (inside(u, v, 0, -0.95, 1.12, 1.0) || inside(u, v, -0.98, 0.7, 0.27, 1.3) || inside(u, v, 0.98, 0.7, 0.27, 1.3));
  window.analysis = {
    size: [W, H], engine: 'builtin', cast: { hueOffset: 0, faces: 1 },
    faces: [{
      id: 1, usable: true, score: 0.95, box: [0.3, 0.2, 0.4, 0.3],
      frame: { x: CX / W, y: CY / H, d: D / W, cos: 1, sin: 0 },
      landmarks: [[0.3, 0.4], [0.7, 0.4], [0.5, 0.5], [0.4, 0.55], [0.6, 0.55]],
      skin: { lightness: 55, hue: 50, chroma: 25 }, hair: { found: true, confidence: 0.8, grey: 0, colour: [28, 24, 22], lightness: 9 },
      beard: 0, marks: true, measure: {}, suggest: { faceLight: 60, balance: 40, smooth: 20, hairDetail: 30 },
    }],
    maps: {
      a: plane((u, v) => [face(u, v) && !eyes(u, v) ? 255 : 0, face(u, v) || inside(u, v, 0, 2.0, 0.6, 0.75) ? 255 : 0, inside(u, v, 0, -0.62, 0.13, 0.13) ? 255 : 0]),
      b: plane((u, v) => [hair(u, v) ? 255 : 0, 0, 0]),
      c: plane((u, v) => [(face(u, v) || inside(u, v, 0, 2.0, 0.6, 0.75)) && !eyes(u, v) ? 255 : 0, 0, 0]),
      ids: plane((u, v) => [face(u, v) || inside(u, v, 0, 2.0, 0.6, 0.75) ? 1 : 0, hair(u, v) ? 1 : 0, 0]),
    },
  };
`;
