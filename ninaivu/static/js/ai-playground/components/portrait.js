/**
 * AI Skin Retouch & Hair / Hairstyle Studio.
 *
 * Professional edge-aware bilateral skin smoothing, blemish reduction, radiance glow,
 * specular hair shine, strand volume and rich natural hair tinting.
 * Styled to Ninaivu Photo Studio craftsmanship standards.
 */

import * as i18n from '../../i18n.js';

export function openPortraitStudio(source, onApply = null) {
  const dialog = document.createElement('dialog');
  dialog.className = 'ap-portrait-dialog';
  dialog.setAttribute('aria-label', i18n.t('AI Skin Retouch and Hair Studio'));

  dialog.innerHTML = `
    <header class="ap-portrait-header">
      <div style="display:flex; align-items:center; gap:10px;">
        <span class="ap-brand-mark" style="width:28px; height:28px; font-size:15px;" aria-hidden="true">✦</span>
        <div>
          <h2 style="margin:0; font-size:14.5px; font-weight:650; letter-spacing:-0.2px;">${i18n.t('AI portrait · skin retouch & hairstyle')}</h2>
          <span style="font-size:10.5px; color:var(--ap-muted);">${i18n.t('Bilateral smoothing, specular shine & volume')}</span>
        </div>
      </div>
      <button type="button" class="btn" data-close-portrait aria-label="${i18n.t('Close portrait studio')}" style="width:28px; height:28px; padding:0; border-radius:50%; display:grid; place-items:center;">✕</button>
    </header>

    <div class="ap-portrait-tabs" role="tablist">
      <button type="button" class="btn active" data-tab="skin" role="tab" aria-selected="true">${i18n.t('Skin retouch')}</button>
      <button type="button" class="btn" data-tab="hair" role="tab" aria-selected="false">${i18n.t('Hair & texture')}</button>
      <button type="button" class="btn" data-tab="hairstyles" role="tab" aria-selected="false">${i18n.t('Hairstyle looks')}</button>
    </div>

    <div class="ap-portrait-grid">
      <div class="ap-portrait-preview">
        <canvas id="ap-portrait-canvas" aria-label="${i18n.t('Portrait preview')}"></canvas>
        <span class="ap-before ap-portrait-before">${i18n.t('Original')}</span>
        <span class="ap-after ap-portrait-after">${i18n.t('Retouched')}</span>

        <label class="ap-floating-compare" style="position:absolute; left:50%; bottom:14px; transform:translateX(-50%); display:flex; align-items:center; gap:10px; font-size:10.5px; padding:6px 16px; background:var(--ap-overlay-bg); color:var(--ap-overlay-text); border:1px solid var(--ap-line); border-radius:20px; backdrop-filter:blur(12px); z-index:10; white-space:nowrap;">
          ${i18n.t('Before')}
          <input data-compare type="range" min="0" max="100" value="50" style="width:140px; margin:0; accent-color:var(--ap-accent);" aria-label="${i18n.t('Before and after split slider')}">
          ${i18n.t('After')}
        </label>
      </div>

      <div class="ap-portrait-controls">
        <!-- Skin Tab -->
        <div data-panel="skin" style="display:flex; flex-direction:column; gap:14px;">
          <div>
            <div class="ap-group-title">${i18n.t('Skin retouching')}</div>
            <p style="font-size:11px; color:var(--ap-muted); margin:0 0 10px; line-height:1.5;">${i18n.t('Bilateral smoothing softens blemishes and pores while preserving eye, lip, and contour edges.')}</p>
          </div>

          <label class="ap-slider">
            <span class="ap-slider-title">${i18n.t('Skin smoothing')}</span>
            <output data-val-skin-smooth>40</output>
            <input data-skin-smooth type="range" min="0" max="100" value="40">
          </label>

          <label class="ap-slider">
            <span class="ap-slider-title">${i18n.t('Radiance glow')}</span>
            <output data-val-skin-glow>25</output>
            <input data-skin-glow type="range" min="0" max="100" value="25">
          </label>

          <label class="ap-slider">
            <span class="ap-slider-title">${i18n.t('Tone balance')}</span>
            <output data-val-skin-redness>30</output>
            <input data-skin-redness type="range" min="0" max="100" value="30">
          </label>

          <div>
            <div class="ap-group-title">${i18n.t('Quick presets')}</div>
            <div style="display:grid; grid-template-columns:1fr 1fr; gap:6px;">
              <button type="button" class="btn small" data-preset="natural" style="padding:6px 8px; font-size:11px;">${i18n.t('Natural soften')}</button>
              <button type="button" class="btn small" data-preset="glamour" style="padding:6px 8px; font-size:11px;">${i18n.t('Studio glamour')}</button>
              <button type="button" class="btn small" data-preset="porcelain" style="padding:6px 8px; font-size:11px;">${i18n.t('Porcelain finish')}</button>
              <button type="button" class="btn small" data-preset="reset-skin" style="padding:6px 8px; font-size:11px;">${i18n.t('Reset skin')}</button>
            </div>
          </div>
        </div>

        <!-- Hair Tab -->
        <div data-panel="hair" hidden style="display:flex; flex-direction:column; gap:14px;">
          <div>
            <div class="ap-group-title">${i18n.t('Hair enhancements')}</div>
            <p style="font-size:11px; color:var(--ap-muted); margin:0 0 10px; line-height:1.5;">${i18n.t('Boost specular gloss, strand separation, root dimension and tone tinting.')}</p>
          </div>

          <label class="ap-slider">
            <span class="ap-slider-title">${i18n.t('Hair shine & gloss')}</span>
            <output data-val-hair-shine>45</output>
            <input data-hair-shine type="range" min="0" max="100" value="45">
          </label>

          <label class="ap-slider">
            <span class="ap-slider-title">${i18n.t('Volume & texture')}</span>
            <output data-val-hair-volume>35</output>
            <input data-hair-volume type="range" min="0" max="100" value="35">
          </label>

          <label class="ap-slider">
            <span class="ap-slider-title">${i18n.t('Root dimension')}</span>
            <output data-val-hair-roots>25</output>
            <input data-hair-roots type="range" min="0" max="100" value="25">
          </label>

          <div>
            <div class="ap-group-title">${i18n.t('Hair color tone')}</div>
            <div class="ap-swatches" role="radiogroup" aria-label="${i18n.t('Hair color presets')}">
              <button type="button" class="ap-swatch active" style="background:transparent; border:2px dashed var(--ap-line-bright);" title="${i18n.t('Natural / no tint')}" data-color="none"></button>
              <button type="button" class="ap-swatch" style="background:#5c3826;" title="${i18n.t('Warm chestnut')}" data-color="#5c3826"></button>
              <button type="button" class="ap-swatch" style="background:#b88a44;" title="${i18n.t('Golden honey blonde')}" data-color="#b88a44"></button>
              <button type="button" class="ap-swatch" style="background:#873d23;" title="${i18n.t('Rich auburn')}" data-color="#873d23"></button>
              <button type="button" class="ap-swatch" style="background:#221e1d;" title="${i18n.t('Jet black')}" data-color="#221e1d"></button>
              <button type="button" class="ap-swatch" style="background:#4a3328;" title="${i18n.t('Chocolate brown')}" data-color="#4a3328"></button>
              <button type="button" class="ap-swatch" style="background:#a09c99;" title="${i18n.t('Platinum silver')}" data-color="#a09c99"></button>
              <input type="color" value="#5c3826" style="width:26px; height:26px; padding:0; border-radius:50%; border:none; cursor:pointer;" title="${i18n.t('Custom color')}">
            </div>
          </div>

          <label class="ap-slider">
            <span class="ap-slider-title">${i18n.t('Color tint depth')}</span>
            <output data-val-hair-tint>0</output>
            <input data-hair-tint type="range" min="0" max="100" value="0">
          </label>
        </div>

        <!-- Hairstyle Inspiration Tab -->
        <div data-panel="hairstyles" hidden style="display:flex; flex-direction:column; gap:10px;">
          <div>
            <div class="ap-group-title">${i18n.t('Hairstyle looks')}</div>
            <p style="font-size:11px; color:var(--ap-muted); margin:0 0 8px; line-height:1.5;">${i18n.t('Style combinations engineered for portrait lighting:')}</p>
          </div>
          <div style="display:flex; flex-direction:column; gap:6px;">
            <button type="button" class="btn" style="text-align:left; justify-content:flex-start; padding:10px; display:flex; flex-direction:column; align-items:flex-start; background:rgba(255,255,255,0.03);" data-style="voluminous">
              <strong style="color:var(--ap-accent); font-size:12px;">${i18n.t('Voluminous body & shine')}</strong>
              <span style="font-size:10.5px; color:var(--ap-muted); margin-top:2px;">${i18n.t('High-volume strand texture, root depth and specular shine')}</span>
            </button>
            <button type="button" class="btn" style="text-align:left; justify-content:flex-start; padding:10px; display:flex; flex-direction:column; align-items:flex-start; background:rgba(255,255,255,0.03);" data-style="sleek">
              <strong style="color:var(--ap-accent); font-size:12px;">${i18n.t('Sleek & glossy finish')}</strong>
              <span style="font-size:10.5px; color:var(--ap-muted); margin-top:2px;">${i18n.t('Mirror-like hair shine, porcelain skin, flyaway reduction')}</span>
            </button>
            <button type="button" class="btn" style="text-align:left; justify-content:flex-start; padding:10px; display:flex; flex-direction:column; align-items:flex-start; background:rgba(255,255,255,0.03);" data-style="golden">
              <strong style="color:var(--ap-accent); font-size:12px;">${i18n.t('Sunlit golden highlights')}</strong>
              <span style="font-size:10.5px; color:var(--ap-muted); margin-top:2px;">${i18n.t('Honey-tinted hair gloss with warm radiant skin glow')}</span>
            </button>
            <button type="button" class="btn" style="text-align:left; justify-content:flex-start; padding:10px; display:flex; flex-direction:column; align-items:flex-start; background:rgba(255,255,255,0.03);" data-style="glamour">
              <strong style="color:var(--ap-accent); font-size:12px;">${i18n.t('Hollywood portrait touch-up')}</strong>
              <span style="font-size:10.5px; color:var(--ap-muted); margin-top:2px;">${i18n.t('Full blemish smoothing, balanced skin tone, boosted hair volume')}</span>
            </button>
          </div>
        </div>

        <div style="margin-top:auto; padding-top:14px; border-top:1px solid var(--ap-line); display:flex; flex-direction:column; gap:8px;">
          <div style="display:flex; gap:8px;">
            <button class="btn primary" data-apply-portrait style="flex:1;">${i18n.t('Apply to photo')}</button>
            <button class="btn" data-download-portrait>${i18n.t('Download PNG')}</button>
          </div>
          <p role="status" class="ap-portrait-status" style="margin:0; font-size:10.5px; color:var(--ap-dim); text-align:center;">${i18n.t('Preview rendered at full clarity.')}</p>
        </div>
      </div>
    </div>
  `;

  document.body.append(dialog);
  const $ = s => dialog.querySelector(s);
  const canvas = $('#ap-portrait-canvas');
  const scale = Math.min(1, 1200 / Math.max(source.width, source.height));
  canvas.width = Math.round(source.width * scale);
  canvas.height = Math.round(source.height * scale);

  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  ctx.drawImage(source, 0, 0, canvas.width, canvas.height);
  const originalData = ctx.getImageData(0, 0, canvas.width, canvas.height);

  const origCanvas = document.createElement('canvas');
  origCanvas.width = canvas.width;
  origCanvas.height = canvas.height;
  origCanvas.getContext('2d').putImageData(originalData, 0, 0);

  let activeColor = 'none';
  const customColorInput = dialog.querySelector('input[type="color"]');

  // Tab switching
  dialog.querySelectorAll('.ap-portrait-tabs button').forEach(btn => {
    btn.onclick = () => {
      dialog.querySelectorAll('.ap-portrait-tabs button').forEach(b => {
        b.classList.toggle('active', b === btn);
        b.setAttribute('aria-selected', String(b === btn));
      });
      const tab = btn.dataset.tab;
      dialog.querySelectorAll('[data-panel]').forEach(p => {
        p.hidden = p.dataset.panel !== tab;
      });
    };
  });

  // Swatch handling
  dialog.querySelectorAll('.ap-swatch').forEach(swatch => {
    swatch.onclick = () => {
      dialog.querySelectorAll('.ap-swatch').forEach(s => s.classList.remove('active'));
      swatch.classList.add('active');
      activeColor = swatch.dataset.color;
      if (activeColor !== 'none' && Number($('[data-hair-tint]').value) === 0) {
        $('[data-hair-tint]').value = 40;
        $('[data-val-hair-tint]').textContent = '40';
      }
      renderPreview();
    };
  });

  customColorInput.oninput = () => {
    dialog.querySelectorAll('.ap-swatch').forEach(s => s.classList.remove('active'));
    activeColor = customColorInput.value;
    if (Number($('[data-hair-tint]').value) === 0) {
      $('[data-hair-tint]').value = 40;
      $('[data-val-hair-tint]').textContent = '40';
    }
    renderPreview();
  };

  // Presets
  $('[data-preset="natural"]').onclick = () => {
    $('[data-skin-smooth]').value = 35; $('[data-val-skin-smooth]').textContent = '35';
    $('[data-skin-glow]').value = 20; $('[data-val-skin-glow]').textContent = '20';
    $('[data-skin-redness]').value = 25; $('[data-val-skin-redness]').textContent = '25';
    renderPreview();
  };
  $('[data-preset="glamour"]').onclick = () => {
    $('[data-skin-smooth]').value = 60; $('[data-val-skin-smooth]').textContent = '60';
    $('[data-skin-glow]').value = 40; $('[data-val-skin-glow]').textContent = '40';
    $('[data-skin-redness]').value = 45; $('[data-val-skin-redness]').textContent = '45';
    renderPreview();
  };
  $('[data-preset="porcelain"]').onclick = () => {
    $('[data-skin-smooth]').value = 85; $('[data-val-skin-smooth]').textContent = '85';
    $('[data-skin-glow]').value = 30; $('[data-val-skin-glow]').textContent = '30';
    $('[data-skin-redness]').value = 60; $('[data-val-skin-redness]').textContent = '60';
    renderPreview();
  };
  $('[data-preset="reset-skin"]').onclick = () => {
    $('[data-skin-smooth]').value = 0; $('[data-val-skin-smooth]').textContent = '0';
    $('[data-skin-glow]').value = 0; $('[data-val-skin-glow]').textContent = '0';
    $('[data-skin-redness]').value = 0; $('[data-val-skin-redness]').textContent = '0';
    renderPreview();
  };

  // Hairstyle presets
  $('[data-style="voluminous"]').onclick = () => {
    $('[data-hair-volume]').value = 65; $('[data-val-hair-volume]').textContent = '65';
    $('[data-hair-shine]').value = 50; $('[data-val-hair-shine]').textContent = '50';
    $('[data-hair-roots]').value = 40; $('[data-val-hair-roots]').textContent = '40';
    renderPreview();
  };
  $('[data-style="sleek"]').onclick = () => {
    $('[data-hair-shine]').value = 75; $('[data-val-hair-shine]').textContent = '75';
    $('[data-hair-volume]').value = 20; $('[data-val-hair-volume]').textContent = '20';
    $('[data-skin-smooth]').value = 65; $('[data-val-skin-smooth]').textContent = '65';
    renderPreview();
  };
  $('[data-style="golden"]').onclick = () => {
    activeColor = '#b88a44';
    $('[data-hair-tint]').value = 45; $('[data-val-hair-tint]').textContent = '45';
    $('[data-hair-shine]').value = 60; $('[data-val-hair-shine]').textContent = '60';
    $('[data-skin-glow]').value = 40; $('[data-val-skin-glow]').textContent = '40';
    renderPreview();
  };
  $('[data-style="glamour"]').onclick = () => {
    $('[data-skin-smooth]').value = 60; $('[data-val-skin-smooth]').textContent = '60';
    $('[data-skin-glow]').value = 35; $('[data-val-skin-glow]').textContent = '35';
    $('[data-hair-shine]').value = 55; $('[data-val-hair-shine]').textContent = '55';
    $('[data-hair-volume]').value = 50; $('[data-val-hair-volume]').textContent = '50';
    renderPreview();
  };

  // Bind all slider value outputs
  dialog.querySelectorAll('input[type="range"]').forEach(input => {
    const key = input.dataset.skinSmooth !== undefined ? 'skin-smooth'
      : input.dataset.skinGlow !== undefined ? 'skin-glow'
      : input.dataset.skinRedness !== undefined ? 'skin-redness'
      : input.dataset.hairShine !== undefined ? 'hair-shine'
      : input.dataset.hairVolume !== undefined ? 'hair-volume'
      : input.dataset.hairRoots !== undefined ? 'hair-roots'
      : input.dataset.hairTint !== undefined ? 'hair-tint' : null;

    if (key) {
      input.oninput = () => {
        const out = $(`[data-val-${key}]`);
        if (out) out.textContent = input.value;
        renderPreview();
      };
    }
  });

  $('[data-compare]').oninput = () => renderPreview();

  let isDraggingSplit = false;
  function updateSplitFromPointer(e) {
    const rect = canvas.getBoundingClientRect();
    if (!rect.width) return;
    const pct = Math.max(0, Math.min(100, Math.round(((e.clientX - rect.left) / rect.width) * 100)));
    $('[data-compare]').value = String(pct);
    renderPreview();
  }
  canvas.onpointerdown = (e) => {
    isDraggingSplit = true;
    canvas.setPointerCapture(e.pointerId);
    updateSplitFromPointer(e);
  };
  canvas.onpointermove = (e) => {
    if (isDraggingSplit) updateSplitFromPointer(e);
  };
  canvas.onpointerup = (e) => {
    if (isDraggingSplit) {
      isDraggingSplit = false;
      try { canvas.releasePointerCapture(e.pointerId); } catch {}
    }
  };
  canvas.onpointercancel = (e) => {
    if (isDraggingSplit) {
      isDraggingSplit = false;
      try { canvas.releasePointerCapture(e.pointerId); } catch {}
    }
  };

  function processPixels(pixels, width, height, opts) {
    const out = new Uint8ClampedArray(pixels);
    const tintRGB = (opts.color && opts.color !== 'none')
      ? opts.color.match(/[a-f0-9]{2}/gi)?.map(v => parseInt(v, 16)) || [184, 138, 68]
      : null;

    for (let y = 0; y < height; y++) {
      for (let x = 0; x < width; x++) {
        const i = (y * width + x) * 4;
        const r = pixels[i], g = pixels[i + 1], b = pixels[i + 2];

        // Skin segmentation via YCbCr
        const cb = 128 - 0.168736 * r - 0.331264 * g + 0.5 * b;
        const cr = 128 + 0.5 * r - 0.418688 * g - 0.081312 * b;
        const isSkin = (cb >= 77 && cb <= 127 && cr >= 133 && cr <= 173) && (r > g && g > b) && (r - g >= 8);

        if (isSkin) {
          let pr = r, pg = g, pb = b;
          if (opts.smooth > 0) {
            let sumR = 0, sumG = 0, sumB = 0, weightSum = 0;
            for (let dy = -1; dy <= 1; dy++) {
              const ny = y + dy;
              if (ny < 0 || ny >= height) continue;
              for (let dx = -1; dx <= 1; dx++) {
                const nx = x + dx;
                if (nx < 0 || nx >= width) continue;
                const ni = (ny * width + nx) * 4;
                const dr = pixels[ni] - r, dg = pixels[ni + 1] - g, db = pixels[ni + 2] - b;
                const distSq = dr * dr + dg * dg + db * db;
                const w = Math.exp(-distSq / 1300);
                sumR += pixels[ni] * w;
                sumG += pixels[ni + 1] * w;
                sumB += pixels[ni + 2] * w;
                weightSum += w;
              }
            }
            const k = (opts.smooth / 100) * 0.85;
            if (weightSum > 0) {
              pr = pr * (1 - k) + (sumR / weightSum) * k;
              pg = pg * (1 - k) + (sumG / weightSum) * k;
              pb = pb * (1 - k) + (sumB / weightSum) * k;
            }
          }

          if (opts.glow > 0) {
            const glowAmt = opts.glow / 100;
            pr = Math.min(255, pr + glowAmt * 14);
            pg = Math.min(255, pg + glowAmt * 9);
            pb = Math.min(255, pb + glowAmt * 4);
          }

          if (opts.redness > 0) {
            const redFactor = (opts.redness / 100) * 0.4;
            const diff = Math.max(0, pr - pg);
            pr -= diff * redFactor;
            pg += diff * redFactor * 0.3;
          }

          out[i] = Math.max(0, Math.min(255, pr));
          out[i + 1] = Math.max(0, Math.min(255, pg));
          out[i + 2] = Math.max(0, Math.min(255, pb));
        } else {
          // Hair heuristic
          const lum = 0.2126 * r + 0.7152 * g + 0.0722 * b;
          const isHair = (lum > 12 && lum < 195) && Math.abs(r - b) < 70;

          if (isHair) {
            let hr = r, hg = g, hb = b;

            if (opts.hairShine > 0) {
              const shineWeight = Math.max(0, (lum - 40) / 140) * (opts.hairShine / 100) * 0.55;
              hr = Math.min(255, hr + (255 - hr) * shineWeight);
              hg = Math.min(255, hg + (255 - hg) * shineWeight);
              hb = Math.min(255, hb + (255 - hb) * shineWeight);
            }

            if (opts.hairVolume > 0) {
              const volWeight = (opts.hairVolume / 100) * 0.5;
              hr = Math.max(0, Math.min(255, hr + (hr - lum) * volWeight));
              hg = Math.max(0, Math.min(255, hg + (hg - lum) * volWeight));
              hb = Math.max(0, Math.min(255, hb + (hb - lum) * volWeight));
            }

            if (opts.hairRoots > 0 && lum < 100) {
              const rootDarken = (opts.hairRoots / 100) * (1 - lum / 100) * 22;
              hr = Math.max(0, hr - rootDarken);
              hg = Math.max(0, hg - rootDarken);
              hb = Math.max(0, hb - rootDarken);
            }

            if (tintRGB && opts.hairTint > 0) {
              const tintK = (opts.hairTint / 100) * 0.65;
              const tintLum = Math.max(1, 0.2126 * tintRGB[0] + 0.7152 * tintRGB[1] + 0.0722 * tintRGB[2]);
              const normR = tintRGB[0] * lum / tintLum;
              const normG = tintRGB[1] * lum / tintLum;
              const normB = tintRGB[2] * lum / tintLum;
              hr = hr * (1 - tintK) + normR * tintK;
              hg = hg * (1 - tintK) + normG * tintK;
              hb = hb * (1 - tintK) + normB * tintK;
            }

            out[i] = Math.max(0, Math.min(255, hr));
            out[i + 1] = Math.max(0, Math.min(255, hg));
            out[i + 2] = Math.max(0, Math.min(255, hb));
          }
        }
      }
    }
    return out;
  }

  function getOptions() {
    return {
      smooth: Number($('[data-skin-smooth]').value),
      glow: Number($('[data-skin-glow]').value),
      redness: Number($('[data-skin-redness]').value),
      hairShine: Number($('[data-hair-shine]').value),
      hairVolume: Number($('[data-hair-volume]').value),
      hairRoots: Number($('[data-hair-roots]').value),
      hairTint: Number($('[data-hair-tint]').value),
      color: activeColor,
    };
  }

  let renderPending = false;
  function renderPreview() {
    if (renderPending) return;
    renderPending = true;
    requestAnimationFrame(() => {
      renderPending = false;
      const opts = getOptions();
      const processed = processPixels(originalData.data, canvas.width, canvas.height, opts);
      const splitPct = Math.max(0, Math.min(100, Number($('[data-compare]').value)));
      const splitPos = Math.round(canvas.width * (splitPct / 100));

      const outImage = new ImageData(new Uint8ClampedArray(processed), canvas.width, canvas.height);
      ctx.putImageData(outImage, 0, 0);

      // Draw original on left side of split crossover
      if (splitPos > 0) {
        ctx.save();
        ctx.beginPath();
        ctx.rect(0, 0, splitPos, canvas.height);
        ctx.clip();
        ctx.drawImage(origCanvas, 0, 0);
        ctx.restore();
      }

      // Draw vertical divider line with shadow at crossover
      if (splitPos > 0 && splitPos < canvas.width) {
        ctx.save();
        ctx.fillStyle = '#ffffff';
        ctx.shadowColor = 'rgba(0, 0, 0, 0.65)';
        ctx.shadowBlur = 5;
        ctx.fillRect(splitPos - 1, 0, 2, canvas.height);
        ctx.restore();
      }

      const beforeBadge = $('.ap-portrait-before');
      const afterBadge = $('.ap-portrait-after');
      if (beforeBadge) beforeBadge.style.opacity = splitPct < 8 ? '0' : '1';
      if (afterBadge) afterBadge.style.opacity = splitPct > 92 ? '0' : '1';
    });
  }

  renderPreview();

  // Apply to canvas
  $('[data-apply-portrait]').onclick = async () => {
    const opts = getOptions();
    const fullCanvas = document.createElement('canvas');
    fullCanvas.width = source.width;
    fullCanvas.height = source.height;
    const fCtx = fullCanvas.getContext('2d');
    fCtx.drawImage(source, 0, 0);
    const fData = fCtx.getImageData(0, 0, source.width, source.height);
    fData.data.set(processPixels(fData.data, source.width, source.height, opts));
    fCtx.putImageData(fData, 0, 0);

    const retouchedBitmap = await createImageBitmap(fullCanvas);
    dialog.close();
    onApply?.(retouchedBitmap);
  };

  // Download high-resolution PNG
  $('[data-download-portrait]').onclick = () => {
    const opts = getOptions();
    const fullCanvas = document.createElement('canvas');
    fullCanvas.width = source.width;
    fullCanvas.height = source.height;
    const fCtx = fullCanvas.getContext('2d');
    fCtx.drawImage(source, 0, 0);
    const fData = fCtx.getImageData(0, 0, source.width, source.height);
    fData.data.set(processPixels(fData.data, source.width, source.height, opts));
    fCtx.putImageData(fData, 0, 0);

    fullCanvas.toBlob(blob => {
      if (!blob) return;
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'ninaivu-portrait-retouched.png';
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 30000);
      $('.ap-portrait-status').textContent = i18n.t('Downloaded full-resolution retouched PNG.');
    }, 'image/png');
  };

  $('[data-close-portrait]').onclick = () => dialog.close();
  dialog.onclose = () => {
    source.close();
    dialog.remove();
  };
  dialog.showModal();
}
