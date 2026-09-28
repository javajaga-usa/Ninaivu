import {AIPhotoService, describeServerJob} from '../services/AIPhotoService.mjs';
import {defaults} from '../models/adjustments.mjs';
import {decode} from '../utils/files.mjs';
import {History} from '../hooks/history.mjs';
import {saveLibraryCopy} from '../services/library.mjs';
import {isBackgroundRemovalRequest, isBackgroundBlurRequest, isObjectRemovalRequest, isPortraitRetouchRequest} from '../safety/commands.mjs';
import {openPortraitStudio} from './portrait.js';
import {openRemoveObject} from './removeobject.js';

export function openPlayground({item=null, returnFocus=document.activeElement, canSave=false, onSaved=null}={}) {
  const existing=document.getElementById('ai-playground');
  if(existing) return;
  if(!document.getElementById('ai-playground-style')) {
    const link=document.createElement('link'); link.id='ai-playground-style';link.rel='stylesheet';link.href='/static/css/ai-playground.css';document.head.append(link);
  }
  const dialog=document.createElement('dialog'); dialog.id='ai-playground';dialog.setAttribute('aria-labelledby','ap-title');
  dialog.innerHTML=`
    <header class="ap-header">
      <div class="ap-brand">
        <span class="ap-brand-mark" aria-hidden="true">✦</span>
        <h1 id="ap-title">Sudar <span class="ap-private">On-device AI</span></h1>
      </div>
      <div class="ap-bar">
        <div class="ap-cluster" role="group" aria-label="History actions">
          <button type="button" class="btn" data-undo title="Undo (Ctrl+Z)" disabled>Undo</button>
          <button type="button" class="btn" data-redo title="Redo (Ctrl+Y)" disabled>Redo</button>
          <button type="button" class="btn" data-reset title="Reset to original">Reset all</button>
        </div>
        <div class="ap-cluster ap-view-modes" role="group" aria-label="Preview mode">
          <button type="button" class="btn" data-view="compare" aria-pressed="true">Compare</button>
          <button type="button" class="btn" data-view="edited" aria-pressed="false">Edited</button>
          <button type="button" class="btn" data-view="original" aria-pressed="false">Original</button>
        </div>
        <label class="ap-zoom" style="margin-left:auto; display:flex; align-items:center; gap:6px; font-size:11px; color:var(--ap-muted);">
          Zoom
          <select data-zoom style="padding:4px 8px; font-size:11px; background:var(--ap-card); border-radius:6px; color:inherit; border:1px solid var(--ap-line);">
            <option value="fit">Fit</option>
            <option value="1">100%</option>
            <option value="1.5">150%</option>
          </select>
        </label>
        <button type="button" class="btn ap-theme-btn" id="ap-theme-btn" title="Theme (T)" aria-label="Toggle theme">
          <svg viewBox="0 0 24 24" class="ico-sun"><circle cx="12" cy="12" r="4.5"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M19.1 4.9l-1.4 1.4M6.3 17.7l-1.4 1.4"/></svg>
          <svg viewBox="0 0 24 24" class="ico-moon"><path d="M20 14.5A8.5 8.5 0 1 1 10.2 4a7 7 0 0 0 9.8 10.5Z"/></svg>
        </button>
      </div>
      <button type="button" data-close aria-label="Close Sudar">✕</button>
    </header>

    <div class="ap-layout">
      <section class="ap-workspace" aria-label="Photo preview">
        <div class="ap-viewport">
          <div class="ap-empty">Open a photo from the gallery to begin editing.</div>
          <div class="ap-stage" hidden>
            <img class="ap-edited" alt="Edited preview">
            <img class="ap-original" alt="Original photo">
            <div class="ap-divider" aria-hidden="true"><span class="ap-divider-handle">↔</span></div>
            <span class="ap-before">Original</span>
            <span class="ap-after">Edited</span>
          </div>
        </div>

        <!-- Overlaid floating controls on workspace foot -->
        <label class="ap-floating-compare ap-compare" hidden>
          Before
          <input data-compare type="range" min="0" max="100" value="50" aria-label="Before and after comparison position">
          After
        </label>
        <p class="ap-status" role="status" aria-live="polite">Open a photo to begin.</p>
      </section>

      <aside class="ap-studio" aria-label="Editing tools">
        <!-- Studio Navigation Switcher -->
        <div class="ap-studio-nav" role="tablist">
          <button type="button" class="ap-mode-btn active" data-tab="adjustments">🎛️ Adjust</button>
          <button type="button" class="ap-mode-btn" data-tab="ai">✨ AI Assist</button>
          <button type="button" class="ap-mode-btn" data-tab="magic">🪄 Magic Tools</button>
        </div>

        <div class="ap-tools-body">
          <!-- PANEL 1: ADJUSTMENTS & LOOKS -->
          <div data-panel="adjustments" style="display:flex; flex-direction:column; gap:16px;">
            <div>
              <div class="ap-group-title">Creative Looks</div>
              <div class="ap-presets-grid">
                <button type="button" class="ap-preset-card" data-preset="vivid"><strong>✨ Vivid</strong><span>Punchy contrast & color</span></button>
                <button type="button" class="ap-preset-card" data-preset="golden"><strong>🌅 Golden Hour</strong><span>Warm sunlit tone</span></button>
                <button type="button" class="ap-preset-card" data-preset="cinematic"><strong>🎬 Cinematic</strong><span>Moody contrast & shade</span></button>
                <button type="button" class="ap-preset-card" data-preset="bw"><strong>📷 Studio B&W</strong><span>Rich monochrome</span></button>
                <button type="button" class="ap-preset-card" data-preset="bright"><strong>☀️ Bright & Clean</strong><span>Lifted shadows & clarity</span></button>
                <button type="button" class="ap-preset-card" data-preset="vintage"><strong>🎞️ Vintage Warm</strong><span>Soft film warmth</span></button>
              </div>
            </div>

            <div class="ap-suggestions-section">
              <div class="ap-group-title">Contextual AI Suggestions</div>
              <div class="ap-suggestions">Choose a photo to analyze.</div>
            </div>

            <fieldset disabled class="ap-panel ap-panel-sliders" style="border:0; padding:0; margin:0; display:flex; flex-direction:column; gap:16px; background:none;">
              <div>
                <div class="ap-group-title">Light</div>
                <div class="ap-controls-light ap-controls"></div>
              </div>

              <div>
                <div class="ap-group-title">Color</div>
                <div class="ap-controls-color"></div>
              </div>

              <div>
                <div class="ap-group-title">Detail & Optics</div>
                <div class="ap-controls-detail"></div>
              </div>

              <div>
                <div class="ap-group-title">Composition</div>
                <div class="ap-controls-geometry"></div>
                <div style="display:grid; grid-template-columns:minmax(0,1fr) auto; align-items:center; gap:8px; font-size:11.5px; color:var(--ap-muted); margin-top:6px;">
                  <span style="color:var(--ap-muted);">Crop Framing</span>
                  <select data-crop style="font-size:11px; padding:4px 8px; background:var(--ap-card);">
                    <option value="original">Original framing</option>
                    <option value="square">Square · 1:1</option>
                    <option value="landscape">Landscape · 16:9</option>
                    <option value="portrait">Portrait · 4:5</option>
                  </select>
                </div>
                <p class="ap-crop-note" hidden>Crop preview uses same framing. Reset to view full original.</p>
              </div>
            </fieldset>
          </div>

          <!-- PANEL 2: AI ASSIST -->
          <div data-panel="ai" hidden style="display:flex; flex-direction:column; gap:14px;">
            <form class="ap-ask">
              <h2 class="ap-ask-title">✨ Natural Language Editing</h2>
              <div style="display:flex; flex-direction:column; gap:4px; font-size:11px; color:var(--ap-muted);">
                <span>Engine</span>
                <select data-provider style="font-size:11.5px; padding:6px 10px; background:var(--ap-card);">
                  <option value="builtin">Built-in · private browser adjustments</option>
                  <option value="local">Local AI · flexible language (Ollama)</option>
                  <option value="generate">Generative AI · preview diffusion</option>
                </select>
              </div>

              <div class="ap-prompt-box">
                <textarea id="ap-prompt" rows="3" maxlength="2000" placeholder="e.g. Lift shadows, soften highlights, warm white balance, crop to 4:5" disabled></textarea>
                <div class="ap-prompt-bar">
                  <span class="ap-prompt-shortcut">Ctrl+Enter to plan</span>
                  <button class="btn primary" type="submit" disabled style="padding:6px 14px; font-size:12px;">Plan edits</button>
                </div>
              </div>

              <fieldset data-generation-options hidden style="border:1px solid var(--ap-line); border-radius:10px; padding:12px; display:flex; flex-direction:column; gap:8px; background:var(--ap-rail);">
                <legend style="font-size:11px; font-weight:600; color:var(--ap-accent); padding:0 4px;">Generation Controls</legend>
                <label style="display:flex; justify-content:space-between; align-items:center; font-size:11px; color:var(--ap-muted);">Quality
                  <select data-generation-quality style="font-size:11px; padding:3px 6px; background:var(--ap-card);">
                    <option value="draft">Draft</option>
                    <option value="balanced" selected>Balanced</option>
                    <option value="detailed">Detailed</option>
                  </select>
                </label>
                <label style="display:flex; justify-content:space-between; align-items:center; font-size:11px; color:var(--ap-muted);">Fidelity
                  <select data-generation-fidelity style="font-size:11px; padding:3px 6px; background:var(--ap-card);">
                    <option value="preserve">Preserve</option>
                    <option value="balanced">Balanced</option>
                    <option value="creative">Creative</option>
                  </select>
                </label>
                <label style="display:flex; flex-direction:column; gap:3px; font-size:11px; color:var(--ap-muted);">Avoid in result
                  <textarea data-generation-negative rows="2" maxlength="500" placeholder="blur, artifacts, oversaturation" style="font:inherit; font-size:11px; background:var(--ap-card); color:var(--ap-text); border:1px solid var(--ap-line); border-radius:6px; padding:5px 8px;"></textarea>
                </label>
                <div style="display:flex; gap:8px; align-items:center;">
                  <label style="display:flex; align-items:center; gap:6px; font-size:11px; color:var(--ap-muted);">Seed
                    <input data-generation-seed type="number" min="0" max="2147483647" step="1" value="42" style="width:90px; font-size:11px; padding:3px 6px; background:var(--ap-card); color:var(--ap-text); border:1px solid var(--ap-line); border-radius:6px;">
                  </label>
                  <button class="btn" type="button" data-new-seed style="padding:4px 8px; font-size:11px;">Random</button>
                </div>
              </fieldset>

              <small data-provider-note style="font-size:11px; color:var(--ap-dim); line-height:1.4;">Combine lighting, color, mood and framing requests. Review proposed changes before applying.</small>
              <div>
                <div class="ap-group-title">Ideas</div>
                <div class="ap-prompt-ideas"></div>
              </div>
            </form>

            <section class="ap-plan" hidden aria-label="Proposed AI edit">
              <h2>Proposed AI Edit</h2>
              <p data-plan-summary style="font-size:11.5px; color:var(--ap-text); margin:0;"></p>
              <ul data-plan-changes style="margin:4px 0; padding-left:18px;"></ul>
              <button class="btn primary" data-apply-plan style="width:100%;">Apply Planned Edits</button>
            </section>

            <section class="ap-generated" hidden aria-label="AI-generated result">
              <h2>Generated Result</h2>
              <p data-generated-note style="font-size:11px; color:var(--ap-muted); margin:0;"></p>
              <img alt="AI-generated edit preview">
              <div style="display:flex; gap:8px;">
                <button class="btn primary" data-download-generated style="flex:1;">Download Image</button>
                <button class="btn" data-discard-generated>Discard</button>
              </div>
            </section>
          </div>

          <!-- PANEL 3: MAGIC TOOLS -->
          <div data-panel="magic" hidden style="display:flex; flex-direction:column; gap:12px;">
            <div class="ap-magic-card">
              <h3>✨ AI Skin & Hair Retouch Studio</h3>
              <p>Edge-preserving bilateral skin smoothing, blemish reduction, radiance glow, hair volume and rich color styling.</p>
              <button class="btn primary" data-portrait style="align-self:flex-start;">Open Retouch Studio</button>
            </div>

            <div class="ap-magic-card">
              <h3>🖼️ Background Tools</h3>
              <p>Automatically segment subject to remove background or apply realistic depth-of-field lens blur.</p>
              <div style="display:flex; gap:6px; flex-wrap:wrap;">
                <button class="btn" data-remove-bg hidden>Cut Out Subject</button>
                <button class="btn" data-blur-bg hidden>Blur Background</button>
              </div>
              <section class="ap-background" hidden aria-label="Background result" style="margin-top:10px; padding:10px; background:var(--ap-rail);">
                <h2 data-background-title style="font-size:12px;">Background Result</h2>
                <img alt="Background preview" style="max-height:160px; object-fit:contain; margin:4px 0;">
                <label data-blur-control hidden style="display:grid; grid-template-columns:minmax(0,1fr) auto; gap:4px; font-size:11px; color:var(--ap-muted); margin:4px 0;">
                  <span>Blur Strength</span>
                  <input data-blur-strength type="range" min="2" max="40" value="16" style="grid-column:1/-1;">
                </label>
                <div style="display:flex; gap:6px; margin-top:6px;">
                  <button class="btn primary" data-download-background style="flex:1; font-size:11.5px; padding:5px 10px;">Download Result</button>
                  <button class="btn" data-discard-background style="font-size:11.5px; padding:5px 10px;">Discard</button>
                </div>
              </section>
            </div>

            <div class="ap-magic-card">
              <h3>🪄 Object Removal & Inpainting</h3>
              <p>Paint over unwanted objects, photobombers, wires or blemishes to seamlessly patch from surroundings.</p>
              <button class="btn" data-remove-object hidden style="align-self:flex-start;">Remove Object</button>
            </div>

            <div class="ap-magic-card" data-gemini-magic-card hidden>
              <h3>🤖 Google Gemini Vision & Analysis</h3>
              <p>Multimodal scene analysis, automatic photo description, semantic tagging, and smart photographic recommendations.</p>
              <button class="btn primary" data-gemini-analyze style="align-self:flex-start;">✨ Analyze with Gemini</button>
              <div data-gemini-analysis-result hidden style="margin-top:8px; display:flex; flex-direction:column; gap:6px; font-size:11px;">
                <p data-gemini-caption style="color:var(--ap-text); margin:0; font-weight:500;"></p>
                <div data-gemini-tags style="display:flex; flex-wrap:wrap; gap:4px;"></div>
                <p data-gemini-critique style="color:var(--ap-dim); margin:0; font-style:italic;"></p>
                <button class="btn" data-gemini-apply-suggestions style="align-self:flex-start; margin-top:4px;" hidden>Apply Suggested Edits</button>
              </div>
            </div>

            <div class="ap-magic-card" data-server-tools hidden>
              <h3>🖥️ Enhance Tools</h3>
              <p data-server-tools-note></p>
              <div style="display:flex; gap:6px; flex-wrap:wrap;">
                <button class="btn" data-server-job="upscale" hidden>Upscale</button>
                <button class="btn" data-server-job="restore" hidden>Restore Faces</button>
                <button class="btn" data-server-job="colorize" hidden>Colourise</button>
              </div>
              <section class="ap-server-result" hidden aria-label="AI server result" style="margin-top:10px; padding:10px; background:var(--ap-rail);">
                <h2 data-server-title style="font-size:12px;">AI Server Result</h2>
                <p data-server-note style="font-size:11px; color:var(--ap-muted); margin:0;"></p>
                <img alt="AI server result preview" style="max-height:160px; object-fit:contain; margin:4px 0;">
                <div style="display:flex; gap:6px; margin-top:6px;">
                  <button class="btn primary" data-download-server style="flex:1; font-size:11.5px; padding:5px 10px;">Download Result</button>
                  <button class="btn" data-discard-server style="font-size:11.5px; padding:5px 10px;">Discard</button>
                </div>
              </section>
            </div>

            <div class="ap-magic-card">
              <h3>🎨 Creative Studio</h3>
              <p>Create picture-in-picture collages, family album memories and bespoke compositions.</p>
              <button class="btn" data-creative style="align-self:flex-start;">Open Creative Studio</button>
            </div>
          </div>
        </div>

        <!-- Footer: Sticky Download / Save -->
        <div class="ap-studio-footer">
          <div class="ap-export-row">
            <select data-format aria-label="Export format">
              <option value="image/png">PNG · Lossless</option>
              <option value="image/jpeg">JPEG · Standard</option>
              <option value="image/webp">WebP · Modern</option>
            </select>
            <button class="btn primary" data-export disabled style="flex:1;">Download Image</button>
          </div>
          <button class="btn" data-save hidden style="background:rgba(230,200,155,0.12); color:var(--ap-accent); border-color:rgba(230,200,155,0.25);">Save copy to Ninaivu library</button>
          <p data-save-note hidden style="font-size:10.5px; color:var(--ap-dim); margin:0;">Saved alongside your original on Ninaivu server.</p>
          <div style="display:flex; justify-content:space-between; align-items:center; font-size:10.5px; color:var(--ap-dim); margin-top:2px;">
            <span class="ap-dimensions"></span>
            <span>Metadata stripped for privacy</span>
          </div>
        </div>
      </aside>
    </div>`;

  document.body.append(dialog);
  const $=s=>dialog.querySelector(s), service=new AIPhotoService(), controller=new AbortController();

  // Mode switching (Adjustments / AI Assist / Magic Tools)
  dialog.querySelectorAll('.ap-mode-btn').forEach(btn => {
    btn.onclick = () => {
      dialog.querySelectorAll('.ap-mode-btn').forEach(b => b.classList.toggle('active', b === btn));
      const tab = btn.dataset.tab;
      dialog.querySelectorAll('[data-panel]').forEach(p => {
        p.hidden = p.dataset.panel !== tab;
      });
    };
  });

  const prompts = $('.ap-prompt-ideas');
  for(const [title,prompt] of [
    ['Natural Light','Lift the shadows, reduce highlights and gently improve contrast'],
    ['Golden Hour','Make it warmer with soft contrast and a golden hour look'],
    ['Monochrome','Make it black and white with stronger contrast'],
    ['Cinematic Film','Cinematic film mood with rich contrast and deep shadows'],
    ['Bright & Clean','Lift shadows, reduce highlights, and brighten the photo for a clean open feel'],
    ['Skin Retouch','Retouch skin, soften blemishes, and add gentle radiance glow'],
    ['Hair & Hairstyle','Boost hair volume, strand texture, and high-gloss luster'],
  ]) {
    const button=document.createElement('button');button.type='button';button.className='btn';button.textContent=title;
    button.onclick=()=>{if(busy)return;$('#ap-prompt').value=prompt;clearPlan();$('#ap-prompt').focus();};prompts.append(button);
  }

  let viewMode='compare';
  function updateCompareDivider(val){
    const divider=$('.ap-divider');
    if(divider){
      divider.style.left=`${val}%`;
      divider.hidden=viewMode!=='compare'||!previewReady||val<=0||val>=100;
    }
  }
  function setView(mode){
    viewMode=mode;
    dialog.querySelectorAll('[data-view]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.view===mode)));
    $('.ap-original').hidden=mode==='edited';
    const compVal=Number($('[data-compare]').value);
    $('.ap-original').style.clipPath=mode==='original'?'none':`inset(0 ${100-compVal}% 0 0)`;
    $('.ap-compare').hidden=mode!=='compare'||!previewReady;
    $('.ap-before').hidden=mode==='edited';$('.ap-after').hidden=mode==='original';
    updateCompareDivider(compVal);
  }
  dialog.querySelectorAll('[data-view]').forEach(b=>b.onclick=()=>setView(b.dataset.view));

  // Zoom Fit / 100% / 150%
  const zoomSelect = $('[data-zoom]');
  if(zoomSelect) {
    zoomSelect.onchange = () => {
      const stage = $('.ap-stage');
      const img = $('.ap-edited');
      if(!stage || !img) return;
      if(zoomSelect.value === 'fit') {
        img.style.maxHeight = 'calc(97dvh - 160px)';
        img.style.width = 'auto';
      } else if(zoomSelect.value === '1') {
        img.style.maxHeight = 'none';
        img.style.width = bitmap ? `${bitmap.width}px` : 'auto';
      } else {
        img.style.maxHeight = 'none';
        img.style.width = bitmap ? `${Math.round(bitmap.width * 1.5)}px` : 'auto';
      }
    };
  }

  let bitmap=null, history=new History(defaults()), busy=false, closed=false, previewReady=false, urls=[];
  let sourceId=null, saving=false, savedState=JSON.stringify(defaults());
  let pendingPlan=null, analysis={}, generatedURL=null;
  let capabilities={}, background=null, backgroundURL=null, backgroundKind=null, backgroundBlob=null;
  let backgroundRevision=0,generatedUnsaved=false,backgroundUnsaved=false;

  function clearPlan(){pendingPlan=null;$('.ap-plan').hidden=true;}
  function clearGenerated(){if(generatedURL)URL.revokeObjectURL(generatedURL);generatedURL=null;generatedUnsaved=false;$('.ap-generated img').removeAttribute('src');$('.ap-generated').hidden=true;}
  function clearBackground(){
    backgroundRevision++;backgroundUnsaved=false;
    if(backgroundURL)URL.revokeObjectURL(backgroundURL);backgroundURL=null;backgroundBlob=null;backgroundKind=null;
    if(background){background.subject.close();background.mask.close();background=null;}
    $('.ap-background img').removeAttribute('src');$('.ap-background').hidden=true;$('[data-blur-control]').hidden=true;
  }
  const dirty=()=>JSON.stringify(history.current)!==savedState;
  const confirmDiscard=()=>!(dirty()||generatedUnsaved||backgroundUnsaved)||window.confirm('Discard your unsaved photo edits and results?');
  const status=message=>{$('.ap-status').textContent=message;};
  const releaseURLs=()=>{urls.forEach(URL.revokeObjectURL);urls=[];};

  function lock(value) {
    busy=value; dialog.setAttribute('aria-busy',String(value));
    $('.ap-panel-sliders').disabled=value||!bitmap;
    $('#ap-prompt').disabled=value||!bitmap;$('.ap-ask button[type=submit]').disabled=value||!bitmap;$('[data-provider]').disabled=value;
    $('[data-generation-options]').disabled=value||!bitmap;
    $('[data-apply-plan]').disabled=value||!pendingPlan;
    $('[data-discard-background]').disabled=value;$('[data-blur-strength]').disabled=value;
    dialog.querySelectorAll('.ap-suggestions button').forEach(b=>b.disabled=value);
    dialog.querySelectorAll('.ap-prompt-ideas button').forEach(b=>b.disabled=value||!bitmap);
    dialog.querySelectorAll('.ap-preset-card').forEach(b=>b.disabled=value||!bitmap);
    $('[data-undo]').disabled=history.index===0; $('[data-redo]').disabled=history.index===history.items.length-1;
    $('[data-export]').disabled=!previewReady;
    $('[data-save]').hidden=!canSave||!sourceId;
    $('[data-save-note]').hidden=!canSave||!sourceId;
    $('[data-save]').disabled=!previewReady;
    $('[data-remove-bg]').hidden=!capabilities.segmentation_model;$('[data-blur-bg]').hidden=!capabilities.segmentation_model;
    $('[data-remove-object]').hidden=!capabilities.object_removal;
    const serverJobs=capabilities.server_jobs||[],localJobs=capabilities.local_jobs||[];let anyServerTool=false,anyServer=false;
    for(const button of dialog.querySelectorAll('[data-server-job]')){const kind=button.dataset.serverJob;button.hidden=!serverJobs.includes(kind)&&!localJobs.includes(kind);anyServerTool||=!button.hidden;anyServer||=serverJobs.includes(kind);button.disabled=value||!bitmap;}
    $('[data-server-tools]').hidden=!anyServerTool;
    $('[data-server-tools-note]').textContent=anyServer&&localJobs.length?'Some run on the graphics-card PC on your home network (a preview of this photo is sent there), the rest on your Ninaivu server.':anyServer?'Run on the graphics-card PC on your home network. A preview of this photo is sent there and nowhere else.':'Run on your Ninaivu server with downloaded AI models. Nothing leaves it.';
  }

  const sliderGroups = {
    light: [
      ['exposure', 'Exposure'],
      ['contrast', 'Contrast'],
      ['shadows', 'Shadows'],
      ['highlights', 'Highlights'],
    ],
    color: [
      ['saturation', 'Color Tint'],
      ['warmth', 'White Balance'],
    ],
    detail: [
      ['sharpness', 'Sharpness'],
      ['noise', 'Noise Reduction'],
      ['vignette', 'Vignette'],
    ],
    geometry: [
      ['angle', 'Straighten'],
    ],
  };

  function formatValue(key, val) {
    const num = Number(val);
    if (num > 0 && key !== 'noise' && key !== 'sharpness' && key !== 'vignette') return `+${num}`;
    return String(num);
  }

  for(const [group, items] of Object.entries(sliderGroups)) {
    const container = $(`.ap-controls-${group}`);
    if(!container) continue;

    for(const [key, label] of items) {
      const row = document.createElement('label');
      row.className = 'ap-slider';
      row.title = 'Double-click to reset to 0';

      const title = document.createElement('span');
      title.className = 'ap-slider-title';
      title.textContent = label;

      const output = document.createElement('output');
      output.dataset.value = key;
      output.textContent = '0';

      const input = document.createElement('input');
      input.type = 'range';
      input.min = ['noise','sharpness','vignette'].includes(key) ? 0 : key === 'angle' ? -10 : -100;
      input.max = key === 'angle' ? 10 : 100;
      input.step = key === 'angle' ? 0.5 : 1;
      input.value = 0;
      input.dataset.adjust = key;
      input.setAttribute('aria-label', label);
      input.id = `ap-${key}`;
      output.htmlFor = input.id;

      // Double-click to reset
      const resetSingle = (e) => {
        e.preventDefault();
        if(busy || !bitmap) return;
        input.value = 0;
        output.textContent = '0';
        apply({[key]: 0});
      };
      row.ondblclick = resetSingle;

      row.append(title, output, input);
      container.append(row);

      input.oninput = () => { output.textContent = formatValue(key, input.value); };
      input.onchange = () => apply({[key]: Number(input.value)});
    }
  }

  // Creative Presets
  const presetDefinitions = {
    vivid: {contrast: 20, saturation: 25, sharpness: 20, exposure: 5},
    golden: {warmth: 35, highlights: -15, shadows: 15, vignette: 10},
    cinematic: {contrast: 25, shadows: -15, highlights: -15, warmth: 10, vignette: 20},
    bw: {saturation: -100, contrast: 25, sharpness: 25, exposure: 10},
    bright: {exposure: 20, shadows: 30, highlights: -25, sharpness: 15},
    vintage: {warmth: 25, contrast: -10, vignette: 25, shadows: 10},
  };
  dialog.querySelectorAll('[data-preset]').forEach(btn => {
    btn.onclick = () => {
      const p = presetDefinitions[btn.dataset.preset];
      if(p) apply(p);
    };
  });

  function sync() {
    const a = history.current;
    dialog.querySelectorAll('[data-adjust]').forEach(i => {
      i.value = a[i.dataset.adjust];
      const out = $(`[data-value="${i.dataset.adjust}"]`);
      if(out) out.textContent = formatValue(i.dataset.adjust, i.value);
    });
    $('[data-crop]').value = a.crop;
  }

  async function preview() {
    clearPlan();
    previewReady = false; lock(true); status('Applying adjustments…');
    try {
      const a = history.current;
      const edited = await service.applyAdjustments(bitmap, a);
      const original = await service.applyAdjustments(bitmap, {...defaults(), crop: a.crop, angle: a.angle});
      if(closed) return;
      releaseURLs(); urls = [URL.createObjectURL(edited), URL.createObjectURL(original)];
      $('.ap-edited').src = urls[0]; $('.ap-original').src = urls[1];
      $('.ap-stage').hidden = false; $('.ap-compare').hidden = false;
      $('.ap-crop-note').hidden = a.crop === 'original' && a.angle === 0;
      previewReady = true; $('.ap-empty').hidden = true; setView(viewMode); sync(); status('Preview updated.');
    } catch(error) { sync(); status(`${error.message} Preview could not update.`); }
    finally { if(!closed) lock(false); }
  }

  async function apply(patch) {
    if(busy || !bitmap) return;
    const next = {...history.current, ...patch};
    if(JSON.stringify(next) === JSON.stringify(history.current)) return;
    history.push(next);
    await preview();
  }

  async function load(file, rotation=0, libraryId=null) {
    if(busy || !file || !confirmDiscard()) return; lock(true); status('Analyzing photograph…');
    try {
      let next = await decode(file); if(closed){next.close(); return;}
      const angle = ((rotation % 360) + 360) % 360;
      if(angle) {
        const canvas = document.createElement('canvas');
        canvas.width = angle % 180 ? next.height : next.width; canvas.height = angle % 180 ? next.width : next.height;
        const ctx = canvas.getContext('2d'); ctx.translate(canvas.width/2, canvas.height/2); ctx.rotate(angle * Math.PI / 180); ctx.drawImage(next, -next.width/2, -next.height/2);
        next.close(); next = await createImageBitmap(canvas);
        if(closed){next.close(); return;}
      }
      bitmap?.close(); bitmap = next; history = new History(defaults());
      sourceId = libraryId; savedState = JSON.stringify(defaults());
      clearGenerated(); clearBackground();
      $('.ap-dimensions').textContent = `${file.name} · ${bitmap.width} × ${bitmap.height}`;
      status('Generating contextual suggestions…');
      analysis = service.analyzeImage(bitmap);
      const cards = service.getSuggestions(analysis); $('.ap-suggestions').replaceChildren();
      for(const card of cards) {
        const button = document.createElement('button'); button.className = 'ap-card'; button.type = 'button';
        const title = document.createElement('strong'), reason = document.createElement('span');
        title.textContent = card.title; reason.textContent = card.reason;
        button.append(title, reason); button.onclick = () => apply(card.patch); $('.ap-suggestions').append(button);
      }
      await preview();
    } catch(error) { status(error.message); }
    finally { if(!closed) lock(false); }
  }

  $('[data-compare]').oninput = e => {
    const val = Number(e.target.value);
    $('.ap-original').style.clipPath = `inset(0 ${100-val}% 0 0)`;
    updateCompareDivider(val);
  };

  const stage = $('.ap-stage');
  let stageDragging = false;
  function updateStageCompare(e){
    const rect = $('.ap-edited').getBoundingClientRect();
    if(!rect.width) return;
    const pct = Math.max(0, Math.min(100, Math.round(((e.clientX - rect.left) / rect.width) * 100)));
    $('[data-compare]').value = String(pct);
    $('.ap-original').style.clipPath = `inset(0 ${100-pct}% 0 0)`;
    updateCompareDivider(pct);
  }
  stage.onpointerdown = e => { if(viewMode !== 'compare') return; stageDragging = true; stage.setPointerCapture(e.pointerId); updateStageCompare(e); };
  stage.onpointermove = e => { if(stageDragging) updateStageCompare(e); };
  stage.onpointerup = e => { if(stageDragging){ stageDragging = false; try{stage.releasePointerCapture(e.pointerId);}catch{} } };
  stage.onpointercancel = e => { if(stageDragging){ stageDragging = false; try{stage.releasePointerCapture(e.pointerId);}catch{} } };

  $('[data-crop]').onchange = e => apply({crop: e.target.value});

  async function portraitStudio(){
    if(busy || !bitmap) return;
    lock(true); status('Opening AI Skin & Hair Retouch Studio…');
    try{
      const blob = await service.applyAdjustments(bitmap, history.current, {maxSide: Infinity, type: 'image/png'});
      if(closed) return;
      const source = await createImageBitmap(blob);
      if(closed){source.close(); return;}
      openPortraitStudio(source, async (retouchedBitmap)=>{
        if(closed || !retouchedBitmap) return;
        bitmap?.close();
        bitmap = retouchedBitmap;
        history = new History(defaults());
        savedState = JSON.stringify(defaults());
        clearGenerated(); clearBackground();
        await preview();
        status('Skin and hair enhancements applied to canvas.');
      });
      status('Adjust skin smoothing, radiance, hair luster, volume and color.');
    } catch(error){ status(error.message); }
    finally { if(!closed) lock(false); }
  }
  $('[data-portrait]').onclick = () => { if(!busy && bitmap) portraitStudio(); };

  $('[data-creative]').onclick = async () => {
    if(busy || !bitmap) return; lock(true); status('Preparing Creative Studio…');
    try{
      const original = await service.applyAdjustments(bitmap, defaults(), {maxSide: 1600, type: 'image/png'});
      const edited = await service.applyAdjustments(bitmap, history.current, {maxSide: 1600, type: 'image/png'});
      if(closed) return;
      const {openCreativeStudio} = await import('../../creative-studio.js');
      await openCreativeStudio({original, edited, returnFocus: $('[data-creative]')});
      status('Creative Studio opened.');
    } catch(error){ if(!closed) status(error.message); }
    finally { if(!closed) lock(false); }
  };

  async function refreshBackground(){
    backgroundRevision++;
    if(background){background.subject.close(); background.mask.close(); background=null;}
    const next = await service.computeBackground(bitmap, history.current, controller.signal);
    if(closed){next.subject.close(); next.mask.close(); throw new Error('Workspace closed.');}
    background = next;
    return background;
  }

  async function removeBackground(){
    if(busy || !bitmap) return; lock(true); status('Segmenting subject on Ninaivu server…');
    try {
      const {subject, mask} = await refreshBackground(); if(closed) return;
      const blob = await service.compositeCutout(subject, mask); if(closed) return;
      if(backgroundURL) URL.revokeObjectURL(backgroundURL);
      backgroundBlob = blob; backgroundUnsaved = true; backgroundKind = 'cutout'; backgroundURL = URL.createObjectURL(blob);
      $('.ap-background img').src = backgroundURL; $('[data-background-title]').textContent = 'Background removed';
      $('[data-blur-control]').hidden = true; $('.ap-background').hidden = false;
      status('Background removed.');
    } catch(error) { if(!closed) status(error.message); }
    finally { if(!closed) lock(false); }
  }

  async function blurBackground() {
    if(busy || !bitmap) return; lock(true); status('Segmenting subject on Ninaivu server…');
    try {
      const {subject, mask} = await refreshBackground(); if(closed) return;
      const blob = await service.compositeBlur(subject, mask, Number($('[data-blur-strength]').value)); if(closed) return;
      if(backgroundURL) URL.revokeObjectURL(backgroundURL);
      backgroundBlob = blob; backgroundUnsaved = true; backgroundKind = 'blur'; backgroundURL = URL.createObjectURL(blob);
      $('.ap-background img').src = backgroundURL; $('[data-background-title]').textContent = 'Background blurred';
      $('[data-blur-control]').hidden = false; $('.ap-background').hidden = false;
      status('Background blurred.');
    } catch(error) { if(!closed) status(error.message); }
    finally { if(!closed) lock(false); }
  }
  $('[data-remove-bg]').onclick = () => { if(!busy && bitmap) removeBackground(); };
  $('[data-blur-bg]').onclick = () => { if(!busy && bitmap) blurBackground(); };

  $('[data-blur-strength]').oninput = async () => {
    if(busy || !background || backgroundKind !== 'blur') return;
    const revision = ++backgroundRevision;
    try{
      const blob = await service.compositeBlur(background.subject, background.mask, Number($('[data-blur-strength]').value));
      if(closed || revision !== backgroundRevision) return;
      if(backgroundURL) URL.revokeObjectURL(backgroundURL);
      backgroundBlob = blob; backgroundUnsaved = true; backgroundURL = URL.createObjectURL(blob); $('.ap-background img').src = backgroundURL;
    } catch(error){ if(!closed && revision === backgroundRevision) status(error.message); }
  };

  $('[data-download-background]').onclick = () => {
    if(!backgroundBlob) return;
    const url = URL.createObjectURL(backgroundBlob), a = document.createElement('a');
    a.href = url; a.download = backgroundKind === 'cutout' ? 'ninaivu-background-removed.png' : 'ninaivu-background-blurred.png';
    a.click(); backgroundUnsaved = false; setTimeout(() => URL.revokeObjectURL(url), 30000);
  };
  $('[data-discard-background]').onclick = clearBackground;

  async function removeObject() {
    if(busy || !bitmap) return; lock(true); status('Preparing photo for object removal…');
    try {
      const blob = await service.applyAdjustments(bitmap, history.current, {maxSide: 1600, type: 'image/png'}); if(closed) return;
      const source = await createImageBitmap(blob); if(closed){source.close(); return;}
      openRemoveObject(source, service, {provider: capabilities.object_removal_provider});
      status('Paint over the object to remove, then apply.');
    } catch(error) { if(!closed) status(error.message); }
    finally { if(!closed) lock(false); }
  }
  $('[data-remove-object]').onclick = () => { if(!busy && bitmap) removeObject(); };

  let serverURL = null;
  function clearServerResult(){ if(serverURL) URL.revokeObjectURL(serverURL); serverURL = null; $('.ap-server-result img').removeAttribute('src'); $('.ap-server-result').hidden = true; }
  const serverLabels = {upscale: 'Upscaled', restore: 'Faces restored', colorize: 'Colourised'};
  for(const button of dialog.querySelectorAll('[data-server-job]')) {
    button.onclick = async () => {
      if(busy || !bitmap) return;
      const kind = button.dataset.serverJob;
      lock(true); status('Sending to the AI server…');
      try {
        const local = !(capabilities.server_jobs || []).includes(kind);
        const blob = await service.serverTool(kind, bitmap, history.current, controller.signal,
          local ? (kind === 'upscale' ? 1024 : 2048) : (capabilities.image_edit_max_side || 1024),
          state => { if(!closed) status(describeServerJob(state, local)); });
        if(closed) return;
        clearServerResult(); serverURL = URL.createObjectURL(blob);
        const img = $('.ap-server-result img'); img.src = serverURL;
        await img.decode().catch(() => {});
        $('[data-server-title]').textContent = `${serverLabels[kind] || 'AI server'} result`;
        $('[data-server-note]').textContent = img.naturalWidth ? `${img.naturalWidth} × ${img.naturalHeight} px` : '';
        $('.ap-server-result').hidden = false;
        status(`${serverLabels[kind] || 'AI server'} result ready.`);
      } catch(error) { if(!closed) status(error.name === 'AbortError' ? 'Cancelled.' : error.message); }
      finally { if(!closed) lock(false); }
    };
  }
  $('[data-download-server]').onclick = () => {
    if(serverURL){ const a = document.createElement('a'); a.href = serverURL; a.download = 'ninaivu-ai-server.png'; a.click(); }
  };
  $('[data-discard-server]').onclick = clearServerResult;

  $('[data-reset]').onclick = () => apply(defaults());
  $('[data-undo]').onclick = () => { if(!busy){ history.undo(); preview(); } };
  $('[data-redo]').onclick = () => { if(!busy){ history.redo(); preview(); } };

  $('#ap-prompt').oninput = clearPlan;
  $('#ap-prompt').onkeydown = e => {
    if((e.ctrlKey || e.metaKey) && e.key === 'Enter'){
      e.preventDefault();
      $('.ap-ask').requestSubmit();
    }
  };

  $('[data-new-seed]').onclick = () => { $('[data-generation-seed]').value = crypto.getRandomValues(new Uint32Array(1))[0] % 2147483648; };
  $('[data-provider]').onchange = () => {
    clearPlan(); const mode = $('[data-provider]').value;
    const isGen = (mode === 'generate' || mode === 'gemini-edit');
    $('[data-generation-options]').hidden = !isGen;
    $('.ap-ask button[type=submit]').textContent = isGen ? 'Generate preview' : 'Plan edits';
    $('[data-provider-note]').textContent = mode === 'builtin'
      ? 'Combine adjustments and creative looks; review the plan before applying.'
      : mode === 'local'
      ? 'Sends only your request and slider values to the local Ollama model on your Ninaivu server.'
      : mode === 'gemini-edit'
      ? `Generates image edit with Google Gemini (${capabilities.gemini_image_model||'gemini-3.1-flash-image'}).`
      : mode === 'gemini-plan'
      ? `Plans photo adjustments using Google Gemini (${capabilities.gemini_vision_model||'gemini-3.8-flash'}).`
      : capabilities.image_provider === 'ai-server'
      ? `Sends a ${capabilities.image_edit_max_side||1024}-pixel preview and your request to the AI server on your home network.`
      : `Sends a ${capabilities.image_edit_max_side||512}-pixel preview and your request to the local image model on your Ninaivu server.`;
  };

  $('.ap-ask').onsubmit = async e => {
    e.preventDefault(); if(busy || !bitmap) return; clearPlan();
    if(isPortraitRetouchRequest($('#ap-prompt').value)){ await portraitStudio(); return; }
    if(isBackgroundRemovalRequest($('#ap-prompt').value)){ await removeBackground(); return; }
    if(isBackgroundBlurRequest($('#ap-prompt').value)){ await blurBackground(); return; }
    if(isObjectRemovalRequest($('#ap-prompt').value)){ await removeObject(); return; }
    lock(true);
    const mode = $('[data-provider]').value;
    const isGen = (mode === 'generate' || mode === 'gemini-edit');
    status(isGen
      ? (mode === 'gemini-edit' ? 'Generating preview with Google Gemini…' : (capabilities.image_provider === 'ai-server' ? 'Generating preview on the AI server…' : 'Generating preview with local model…'))
      : (mode === 'gemini-plan' ? 'Planning edits with Google Gemini…' : 'Planning edits…'));
    try {
      if(isGen) {
        const side = mode === 'gemini-edit' ? 1024 : (capabilities.image_edit_max_side || 512);
        const options = {quality: $('[data-generation-quality]').value, fidelity: $('[data-generation-fidelity]').value, negative_prompt: $('[data-generation-negative]').value, seed: Number($('[data-generation-seed]').value)};
        const serverJob = mode !== 'gemini-edit' && (capabilities.server_jobs || []).includes('edit');
        const provider = mode === 'gemini-edit' ? 'gemini' : undefined;
        const blob = await service.generateEdit($('#ap-prompt').value, bitmap, history.current, controller.signal, side, options,
          {serverJob, provider, onStatus: state => { if(!closed) status(describeServerJob(state)); }});
        if(closed) return; clearGenerated(); generatedURL = URL.createObjectURL(blob); generatedUnsaved = true; $('.ap-generated img').src = generatedURL; $('.ap-generated').hidden = false;
        $('[data-generated-note]').textContent = `${options.quality} quality · ${options.fidelity} fidelity · seed ${options.seed}. Up to ${side}px per side.`;
        status('Generated preview ready.');
      } else {
        const provider = mode === 'gemini-plan' ? 'gemini' : mode;
        const plan = await service.planEdit($('#ap-prompt').value, history.current, analysis, provider, controller.signal);
        if(closed) return; pendingPlan = plan;
        $('[data-plan-summary]').textContent = `${plan.provider}: ${plan.summary}`; $('[data-plan-changes]').replaceChildren();
        const labels = {exposure:'Exposure',contrast:'Contrast',saturation:'Color',warmth:'White balance',sharpness:'Sharpness',noise:'Noise reduction',shadows:'Shadows',highlights:'Highlights',vignette:'Vignette',angle:'Straighten'};
        for(const [key, value] of Object.entries(plan.patch)) {
          const li = document.createElement('li'); li.textContent = `${labels[key]||key}: ${history.current[key]} → ${value}`; $('[data-plan-changes]').append(li);
        }
        $('.ap-plan').hidden = false; status('Plan ready. Review proposed changes.');
      }
    } catch(error){ if(!closed) status(error.message); }
    finally { if(!closed) lock(false); }
  };

  $('[data-apply-plan]').onclick = () => { if(pendingPlan && !busy) apply(pendingPlan.patch); };
  $('[data-download-generated]').onclick = () => {
    if(generatedURL){ const a = document.createElement('a'); a.href = generatedURL; a.download = 'ninaivu-ai-generated.png'; a.click(); generatedUnsaved = false; }
  };
  $('[data-discard-generated]').onclick = clearGenerated;

  let geminiSuggestions = null;
  $('[data-gemini-analyze]').onclick = async () => {
    if(busy || !bitmap) return;
    lock(true); status('Analyzing photograph with Google Gemini…');
    try {
      const res = await service.geminiAnalyze(bitmap, history.current, controller.signal);
      if(closed) return;
      $('[data-gemini-caption]').textContent = res.caption || '';
      $('[data-gemini-critique]').textContent = res.critique ? `Critique: ${res.critique}` : '';
      $('[data-gemini-tags]').replaceChildren();
      (res.tags || []).forEach(t => {
        const span = document.createElement('span');
        span.style = 'padding:2px 8px; border-radius:12px; background:var(--ap-card); border:1px solid var(--ap-line); color:var(--ap-muted); font-size:10.5px;';
        span.textContent = t;
        $('[data-gemini-tags]').append(span);
      });
      if(res.adjustments && Object.keys(res.adjustments).length > 0) {
        geminiSuggestions = res.adjustments;
        $('[data-gemini-apply-suggestions]').hidden = false;
      } else {
        $('[data-gemini-apply-suggestions]').hidden = true;
      }
      $('[data-gemini-analysis-result]').hidden = false;
      status('Gemini analysis complete.');
    } catch(err) {
      if(!closed) status(err.message);
    } finally {
      if(!closed) lock(false);
    }
  };
  $('[data-gemini-apply-suggestions]').onclick = () => {
    if(geminiSuggestions && !busy) {
      apply(geminiSuggestions);
      status('Applied Gemini suggested adjustments.');
    }
  };

  $('[data-export]').onclick = async () => {
    if(busy || !bitmap || !previewReady) return; lock(true); status('Preparing full-resolution export…');
    try {
      const blob = await service.applyAdjustments(bitmap, history.current, {maxSide: Infinity, type: $('[data-format]').value});
      if(closed) return;
      const url = URL.createObjectURL(blob), a = document.createElement('a'); a.href = url;
      a.download = `ninaivu-edited.${({'image/png':'png','image/jpeg':'jpg','image/webp':'webp'})[blob.type]||'png'}`;
      a.click(); setTimeout(() => URL.revokeObjectURL(url), 30000); savedState = JSON.stringify(history.current);
      status('Download ready. Metadata stripped.');
    } catch(error){ status(error.message); }
    finally { if(!closed) lock(false); }
  };

  $('[data-save]').onclick = async () => {
    if(busy || !sourceId || !canSave || !previewReady) return;
    saving = true; lock(true); status('Saving new library copy…');
    try {
      const blob = await service.applyAdjustments(bitmap, history.current, {maxSide: Infinity, type: 'image/png'});
      const copy = await saveLibraryCopy(sourceId, blob);
      savedState = JSON.stringify(history.current); saving = false;
      // Nothing to open while it is waiting to be reviewed: say so and stay put
      // rather than closing onto a library that does not have it yet.
      if(copy.pending){ status(copy.message); return; }
      if(generatedUnsaved || backgroundUnsaved) status('Copy saved. Download separate AI/background results before closing.');
      else dialog.close();
      onSaved?.(copy);
    } catch(error){ status(error.message); }
    finally { saving = false; if(!closed) lock(false); }
  };

  function requestClose() {
    if(saving){ status('Please wait while your copy is saving.'); return; }
    if(confirmDiscard()) dialog.close();
  }
  $('[data-close]').onclick = requestClose;
  dialog.oncancel = e => { e.preventDefault(); requestClose(); };

  const cycleAppTheme = () => {
    if (typeof window.cycleTheme === 'function') {
      window.cycleTheme();
    } else {
      const order = ['system', 'light', 'dark'];
      const cur = document.documentElement.dataset.theme || 'system';
      const next = order[(order.indexOf(cur) + 1) % order.length];
      document.documentElement.dataset.theme = next;
      try {
        localStorage.setItem('mv.theme', JSON.stringify(next));
        localStorage.setItem('ninaivu.theme', JSON.stringify(next));
      } catch {}
    }
  };

  const themeBtn = $('#ap-theme-btn');
  if (themeBtn) themeBtn.onclick = cycleAppTheme;

  const handleKey = e => {
    if(!dialog.open || document.querySelector('.ap-portrait-dialog[open], .ap-recolor[open], .ap-remove-dialog[open], #creative-studio[open]')) return;
    const typing = e.target.matches('input:not([type=range]),textarea') || e.target.isContentEditable;
    if(!typing && e.key.toLowerCase() === 't' && !e.ctrlKey && !e.metaKey && !e.altKey) {
      e.preventDefault();
      cycleAppTheme();
      return;
    }
    if(!(e.ctrlKey || e.metaKey) || e.altKey || typing) return;
    const key = e.key.toLowerCase();
    if(key !== 'z' && key !== 'y') return;
    e.preventDefault(); if(busy || !bitmap) return;
    if(key === 'y' || e.shiftKey) history.redo(); else history.undo(); preview();
  };

  document.addEventListener('keydown', handleKey);
  dialog.onclose = () => {
    closed = true; clearGenerated(); clearBackground();
    document.removeEventListener('keydown', handleKey);
    controller.abort(); bitmap?.close(); releaseURLs(); if(serverURL) URL.revokeObjectURL(serverURL); dialog.remove();
    if(returnFocus?.isConnected) returnFocus.focus();
  };
  dialog.showModal();

  fetch('/api/ai-playground/capabilities', {signal: controller.signal}).then(r => r.ok ? r.json() : null).then(info => {
    if(closed || !info) return;
    capabilities = info; lock(busy);
    if(info.gemini_enabled) {
      $('[data-gemini-magic-card]').hidden = false;
      if(!$('[data-provider] option[value="gemini-edit"]')) {
        const optGen = document.createElement('option');
        optGen.value = 'gemini-edit';
        optGen.textContent = 'Google Gemini · generative image editing (sends the photo to Google)';
        $('[data-provider]').append(optGen);
        const optPlan = document.createElement('option');
        optPlan.value = 'gemini-plan';
        optPlan.textContent = 'Google Gemini · multimodal AI plan (sends the photo to Google)';
        $('[data-provider]').append(optPlan);
      }
    }
    // A local language model becomes the default planner; an extension never
    // does. Gemini used to be picked here by itself when no local model was
    // installed, which sent the photograph to Google on the first "Ask AI"
    // without the person having chosen that. It is offered, never assumed.
    if(!info.language_model) return;
    if(!$('#ap-prompt').value && $('[data-provider]').value === 'builtin') {
      $('[data-provider]').value = 'local';
      $('[data-provider]').onchange();
    }
  }).catch(()=>{});

  if(item) {
    lock(true); status('Opening photograph…');
    (async () => {
      try {
        const url = new URL(item.view || item.src, location.href);
        if(url.origin !== location.origin) throw new Error('This photo must come from your Ninaivu library.');
        const response = await fetch(url, {signal: controller.signal, credentials: 'same-origin'});
        if(!response.ok) throw new Error('Photo could not be loaded.');
        const blob = await response.blob(); if(closed) return;
        lock(false); await load(new File([blob], item.name || 'Current photo', {type: blob.type}), item.rotation || 0, item.id);
      } catch(error){ if(!closed){ lock(false); status(error.message); } }
    })();
  }
}
