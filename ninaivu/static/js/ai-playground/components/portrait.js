/**
 * Skin and hair, for the people in a photograph.
 *
 * The same tools as the Photo Studio's Skin and Hair panels — one engine, one
 * panel (studio/portrait-*.js) — shown in the AI studio's own dialog. Every face
 * in the photograph is found, each is judged against its own skin, and what is
 * offered to each is what was measured about them: a face in shadow is brought
 * up, a colour cast is turned back towards skin, grey hair is covered. A bindi,
 * kumkum, sindoor, sacred ash or sandal paste is left exactly as it is, and
 * nothing here makes skin lighter.
 *
 * This replaced a pair of filters that treated everything the colour of skin as
 * skin and everything else that was not too bright as hair — the wall behind a
 * head as much as the head — and whose presets were named for making a
 * complexion paler. There was nothing in them worth keeping.
 */

import * as i18n from '../../i18n.js';
import { PortraitSession } from '../../studio/portrait-session.js';
import { PortraitPanel } from '../../studio/portrait-panel.js';
import { isNeutral } from '../../studio/portrait-params.mjs';

/** The longest edge of what is drawn while sliders move. The full picture is only worked on when it is applied. */
const PREVIEW_LONGEST = 1400;
const MAPS_LONGEST = 2400;

export function openPortraitStudio(source, onApply = null) {
  const dialog = document.createElement('dialog');
  dialog.className = 'ap-portrait-dialog';
  dialog.setAttribute('aria-label', i18n.t('Skin and hair'));

  dialog.innerHTML = `
    <header class="ap-portrait-header">
      <div style="display:flex; align-items:center; gap:10px;">
        <span class="ap-brand-mark" style="width:28px; height:28px; font-size:15px;" aria-hidden="true">✦</span>
        <div>
          <h2 style="margin:0; font-size:14.5px; font-weight:650; letter-spacing:-0.2px;">${i18n.t('Skin and hair')}</h2>
          <span style="font-size:10.5px; color:var(--ap-muted);">${i18n.t('Each person in the photograph, on this computer')}</span>
        </div>
      </div>
      <button type="button" class="btn" data-close-portrait aria-label="${i18n.t('Close portrait studio')}" style="width:28px; height:28px; padding:0; border-radius:50%; display:grid; place-items:center;">✕</button>
    </header>

    <div class="ap-portrait-tabs" role="tablist">
      <button type="button" class="btn active" data-tab="skin" role="tab" aria-selected="true">${i18n.t('Skin')}</button>
      <button type="button" class="btn" data-tab="hair" role="tab" aria-selected="false">${i18n.t('Hair')}</button>
    </div>

    <div class="ap-portrait-grid">
      <div class="ap-portrait-preview">
        <canvas id="ap-portrait-canvas" aria-label="${i18n.t('Portrait preview')}"></canvas>
        <canvas class="ap-portrait-overlay" aria-hidden="true" style="position:absolute; inset:0; width:100%; height:100%; object-fit:contain; pointer-events:none;"></canvas>
        <span class="ap-before ap-portrait-before">${i18n.t('Original')}</span>
        <span class="ap-after ap-portrait-after">${i18n.t('Retouched')}</span>

        <label class="ap-floating-compare" style="position:absolute; left:50%; bottom:14px; transform:translateX(-50%); display:flex; align-items:center; gap:10px; font-size:10.5px; padding:6px 16px; background:var(--ap-overlay-bg); color:var(--ap-overlay-text); border:1px solid var(--ap-line); border-radius:20px; backdrop-filter:blur(12px); z-index:10; white-space:nowrap;">
          ${i18n.t('Before')}
          <input data-compare type="range" min="0" max="100" value="50" style="width:140px; margin:0; accent-color:var(--ap-accent);" aria-label="${i18n.t('Before and after split slider')}">
          ${i18n.t('After')}
        </label>
      </div>

      <div class="ap-portrait-controls">
        <div data-panel-host="skin"></div>
        <div data-panel-host="hair" hidden></div>

        <div style="margin-top:auto; padding-top:14px; border-top:1px solid var(--ap-line); display:flex; flex-direction:column; gap:8px;">
          <div style="display:flex; gap:8px;">
            <button class="btn primary" data-apply-portrait style="flex:1;">${i18n.t('Apply to photo')}</button>
            <button class="btn" data-download-portrait>${i18n.t('Download PNG')}</button>
          </div>
          <p role="status" class="ap-portrait-status" style="margin:0; font-size:10.5px; color:var(--ap-dim); text-align:center;">${i18n.t('Choose a person, or everyone, and adjust what they need.')}</p>
        </div>
      </div>
    </div>
  `;

  document.body.append(dialog);
  const $ = (selector) => dialog.querySelector(selector);
  const canvas = $('#ap-portrait-canvas');
  const say = (text) => { $('.ap-portrait-status').textContent = text; };

  const scale = Math.min(1, PREVIEW_LONGEST / Math.max(source.width, source.height));
  canvas.width = Math.max(1, Math.round(source.width * scale));
  canvas.height = Math.max(1, Math.round(source.height * scale));
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  ctx.drawImage(source, 0, 0, canvas.width, canvas.height);
  const original = ctx.getImageData(0, 0, canvas.width, canvas.height);
  const originalCanvas = Object.assign(document.createElement('canvas'), { width: canvas.width, height: canvas.height });
  originalCanvas.getContext('2d').putImageData(original, 0, 0);
  const overlay = $('.ap-portrait-overlay');
  overlay.width = canvas.width; overlay.height = canvas.height;

  // -- the faces, and the worker that retouches them ---------------------------

  const worker = new Worker(new URL('../../studio/portrait-worker.mjs', import.meta.url), { type: 'module' });
  const jobs = new Map();
  let serial = 0, mapsSent = null;
  worker.onmessage = ({ data }) => {
    const job = jobs.get(data.id);
    jobs.delete(data.id);
    if (data.error) job?.reject(new Error(data.error)); else job?.resolve(data.pixels);
  };
  worker.onerror = () => { say(i18n.t('Photo processing failed. Close the editor and try again.')); };

  const session = new PortraitSession({ source, onChange: () => draw() });

  // -- painting by hand: where the tools missed, or where there was no face to find ----

  let paintMode = null, paintKind = null, showSelection = false, stroke = null, last = null, sample = null;

  /** What the tools would reach, and what has been painted, laid over the photograph. */
  function showMask() {
    const octx = overlay.getContext('2d');
    octx.setTransform(1, 0, 0, 1, 0, 0);
    octx.clearRect(0, 0, overlay.width, overlay.height);
    if (!(showSelection || paintKind)) return;
    if (session.state !== 'ready' && !session.hasPaint) return;
    const w = overlay.width, h = overlay.height;
    const ratio = Math.min(1, 700 / Math.max(w, h));
    const small = session.overlay(Math.max(1, Math.round(w * ratio)), Math.max(1, Math.round(h * ratio)), session.selected);
    const scratch = Object.assign(document.createElement('canvas'), { width: small.width, height: small.height });
    scratch.getContext('2d').putImageData(small, 0, 0);
    octx.drawImage(scratch, 0, 0, w, h);
  }

  /** A pointer event as a point on the preview, allowing for the letterbox object-fit leaves round it. */
  function point(e) {
    const box = overlay.getBoundingClientRect();
    const fit = Math.min(box.width / overlay.width, box.height / overlay.height) || 1;
    const left = box.left + (box.width - overlay.width * fit) / 2, top = box.top + (box.height - overlay.height * fit) / 2;
    return { x: (e.clientX - left) / fit, y: (e.clientY - top) / fit };
  }

  function brush(p) {
    if (!paintKind || !paintMode) return;
    const { size, edge } = panels[paintKind].brush;
    const layer = paintMode === 'grow' ? session.paint.hairGrow : session.paint[`${paintKind}${paintMode === 'add' ? 'Add' : 'Erase'}`];
    const opposite = session.paint[`${paintKind}${paintMode === 'erase' ? 'Add' : 'Erase'}`];
    const factor = layer.w / canvas.width;
    let weight = null;
    if (edge && paintMode === 'add' && sample) {
      // A brush that follows colour: paint takes where the colour is near the colour under the first touch.
      const data = original.data, w = canvas.width, h = canvas.height;
      weight = (lx, ly) => {
        const x = Math.floor((lx + 0.5) / factor), y = Math.floor((ly + 0.5) / factor);
        if (x < 0 || y < 0 || x >= w || y >= h) return 0;
        const j = (y * w + x) * 4;
        const diff = Math.hypot(data[j] - sample[0], data[j + 1] - sample[1], data[j + 2] - sample[2]);
        return Math.exp(-diff * diff / 5000);
      };
    }
    layer.stamp(p.x * factor, p.y * factor, size / 2 * factor, { weight });
    // The last word about a place is the latest: painting in takes any painting out away, and the other way round.
    opposite.stamp(p.x * factor, p.y * factor, size / 2 * factor, { erase: true });
    if (paintMode === 'erase' && paintKind === 'hair') session.paint.hairGrow.stamp(p.x * factor, p.y * factor, size / 2 * factor, { erase: true });
  }

  overlay.onpointerdown = (e) => {
    if (!paintMode || e.button !== 0) return;
    e.preventDefault();
    const p = point(e);
    overlay.setPointerCapture(e.pointerId); stroke = e.pointerId; last = p;
    const i = (Math.max(0, Math.min(canvas.height - 1, Math.floor(p.y))) * canvas.width + Math.max(0, Math.min(canvas.width - 1, Math.floor(p.x)))) * 4;
    sample = original.data.slice(i, i + 3);
    brush(p); showMask();
  };
  overlay.onpointermove = (e) => {
    if (stroke !== e.pointerId) return;
    const p = point(e), dx = p.x - last.x, dy = p.y - last.y;
    const steps = Math.max(1, Math.ceil(Math.hypot(dx, dy) / 3));
    for (let i = 1; i <= steps; i++) brush({ x: last.x + dx * i / steps, y: last.y + dy * i / steps });
    last = p; showMask();
  };
  const finishStroke = (e) => {
    if (stroke !== e.pointerId) return;
    stroke = null;
    try { overlay.releasePointerCapture(e.pointerId); } catch { /* already let go */ }
    // Hair painted with Add hair shows at once: its slider is put to full if it was at nothing.
    if (paintMode === 'grow' && session.paint.hairGrow.any() && !(session.portrait.all.hairGrow > 0)) session.portrait.all.hairGrow = 100;
    session.bump(); session.touch();            // what was painted is now part of what the worker is sent
    showMask();
  };
  overlay.onpointerup = finishStroke; overlay.onpointercancel = finishStroke;

  const host = {
    picture: () => originalCanvas,
    remember: () => {},
    changed: () => draw(),
    say,
    select: (on) => { showSelection = on; showMask(); },
    refine: (mode, kind) => {
      paintMode = mode; paintKind = mode ? kind : null;
      if (mode) showSelection = true;
      overlay.style.pointerEvents = mode ? 'auto' : 'none';
      overlay.style.cursor = mode ? 'crosshair' : '';
      if (mode) say(mode === 'add' ? i18n.t('Paint over what the tools missed.') : mode === 'grow' ? i18n.t('Paint where there should be hair. It is drawn from the hair beside it.') : i18n.t('Paint over what the tools took by mistake.'));
      showMask();
    },
  };
  session.watch(() => showMask());              // a different person chosen, or a different answer: the overlay follows
  const panels = {};
  for (const kind of ['skin', 'hair']) {
    panels[kind] = new PortraitPanel({ session, kind, host });
    $(`[data-panel-host="${kind}"]`).append(panels[kind].element);
  }

  /** Retouch *pixels* (RGBA bytes, `width × height`) with what has been asked. */
  function retouch(pixels, width, height, mapWidth, mapHeight) {
    const id = ++serial;
    const portrait = isNeutral(session.portrait) ? null : (() => {
      const { key, maps, frames } = session.payload(mapWidth, mapHeight);
      if (mapsSent !== key) { worker.postMessage({ type: 'maps', key, maps }); mapsSent = key; }
      return { params: session.portrait, frames, mapsKey: key };
    })();
    return new Promise((resolve, reject) => {
      jobs.set(id, { resolve, reject });
      worker.postMessage({ id, pixels, width, height, portrait }, [pixels]);
    });
  }

  // -- the preview, with its before-and-after split ---------------------------

  let rendering = false, pending = false, result = null;

  async function draw() {
    pending = true;
    if (rendering) return;
    rendering = true;
    try {
      while (pending) {
        pending = false;
        const bytes = await retouch(new Uint8ClampedArray(original.data).buffer, canvas.width, canvas.height, canvas.width, canvas.height);
        result = new ImageData(new Uint8ClampedArray(bytes), canvas.width, canvas.height);
        paint();
      }
    } catch (error) {
      say(error.message);
    } finally {
      rendering = false;
    }
  }

  function paint() {
    const percent = Math.max(0, Math.min(100, Number($('[data-compare]').value)));
    const split = Math.round(canvas.width * percent / 100);
    ctx.putImageData(result || original, 0, 0);
    if (split > 0) {
      ctx.save();
      ctx.beginPath();
      ctx.rect(0, 0, split, canvas.height);
      ctx.clip();
      ctx.drawImage(originalCanvas, 0, 0);
      ctx.restore();
    }
    if (split > 0 && split < canvas.width) {
      ctx.save();
      ctx.fillStyle = '#ffffff';
      ctx.shadowColor = 'rgba(0, 0, 0, 0.65)';
      ctx.shadowBlur = 5;
      ctx.fillRect(split - 1, 0, 2, canvas.height);
      ctx.restore();
    }
    const before = $('.ap-portrait-before'), after = $('.ap-portrait-after');
    if (before) before.style.opacity = percent < 8 ? '0' : '1';
    if (after) after.style.opacity = percent > 92 ? '0' : '1';
  }

  $('[data-compare]').oninput = paint;
  let dragging = false;
  const follow = (e) => {
    const box = canvas.getBoundingClientRect();
    if (!box.width) return;
    $('[data-compare]').value = String(Math.max(0, Math.min(100, Math.round((e.clientX - box.left) / box.width * 100))));
    paint();
  };
  canvas.onpointerdown = (e) => { dragging = true; canvas.setPointerCapture(e.pointerId); follow(e); };
  canvas.onpointermove = (e) => { if (dragging) follow(e); };
  canvas.onpointerup = canvas.onpointercancel = (e) => {
    if (!dragging) return;
    dragging = false;
    try { canvas.releasePointerCapture(e.pointerId); } catch { /* already let go */ }
  };

  // -- tabs ---------------------------------------------------------------------

  dialog.querySelectorAll('.ap-portrait-tabs button').forEach((button) => {
    button.onclick = () => {
      dialog.querySelectorAll('.ap-portrait-tabs button').forEach((b) => {
        b.classList.toggle('active', b === button);
        b.setAttribute('aria-selected', String(b === button));
      });
      for (const kind of ['skin', 'hair']) $(`[data-panel-host="${kind}"]`).hidden = kind !== button.dataset.tab;
      // A brush at work on the other panel is put down when the panel is left.
      if (paintKind && paintKind !== button.dataset.tab) panels[paintKind].refine(null);
      showMask();
    };
  });

  // -- applying it to the whole picture ---------------------------------------

  /** The photograph at full size, retouched. The maps are asked for at working size and read up to it. */
  async function full() {
    const out = Object.assign(document.createElement('canvas'), { width: source.width, height: source.height });
    const octx = out.getContext('2d', { willReadFrequently: true });
    octx.drawImage(source, 0, 0);
    const image = octx.getImageData(0, 0, out.width, out.height);
    const k = Math.min(1, MAPS_LONGEST / Math.max(out.width, out.height));
    const bytes = await retouch(image.data.buffer, out.width, out.height,
      Math.max(1, Math.round(out.width * k)), Math.max(1, Math.round(out.height * k)));
    octx.putImageData(new ImageData(new Uint8ClampedArray(bytes), out.width, out.height), 0, 0);
    return out;
  }

  const busy = (on) => dialog.querySelectorAll('[data-apply-portrait], [data-download-portrait]').forEach((b) => { b.disabled = on; });

  $('[data-apply-portrait]').onclick = async () => {
    busy(true);
    say(i18n.t('Retouching at full size…'));
    try {
      const finished = await full();
      const bitmap = await createImageBitmap(finished);
      dialog.close();
      onApply?.(bitmap);
    } catch (error) {
      say(error.message);
      busy(false);
    }
  };

  $('[data-download-portrait]').onclick = async () => {
    busy(true);
    say(i18n.t('Retouching at full size…'));
    try {
      const finished = await full();
      const blob = await new Promise((resolve) => finished.toBlob(resolve, 'image/png'));
      if (!blob) throw new Error(i18n.t('This browser cannot export that format. Choose another.'));
      const url = URL.createObjectURL(blob);
      const link = Object.assign(document.createElement('a'), { href: url, download: 'ninaivu-portrait-retouched.png' });
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 30000);
      say(i18n.t('Downloaded full-resolution retouched PNG.'));
    } catch (error) {
      say(error.message);
    } finally {
      busy(false);
    }
  };

  $('[data-close-portrait]').onclick = () => dialog.close();
  dialog.onclose = () => {
    worker.terminate();
    for (const job of jobs.values()) job.reject(new Error('Closed.'));
    jobs.clear();
    source.close?.();
    dialog.remove();
  };

  dialog.showModal();
  paint();
  session.analyse();                  // the faces are looked for as the dialog opens
}
