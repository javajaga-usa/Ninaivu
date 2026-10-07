import {AIPhotoService, describeServerJob} from '../services/AIPhotoService.mjs';
import {defaults} from '../models/adjustments.mjs';
import {decode} from '../utils/files.mjs';
import {History} from '../hooks/history.mjs';
import {saveLibraryCopy} from '../services/library.mjs';
import {isBackgroundRemovalRequest, isBackgroundBlurRequest, isObjectRemovalRequest, isPortraitRetouchRequest, isHairstyleRequest, HAIRSTYLES} from '../safety/commands.mjs';
import {isClothingColorRequest} from '../services/recolor.mjs';
import {openPortraitStudio} from './portrait.js';
import {openRemoveObject} from './removeobject.js';
import {openRecolor} from './recolor.js';
import * as i18n from '../../i18n.js';
import {errorText} from '../utils/messages.mjs';

export function openPlayground({item=null, returnFocus=document.activeElement, canSave=false, onSaved=null}={}) {
  const existing=document.getElementById('ai-playground');
  // A dialog that has been closed is removed when its close event fires, a
  // moment later; opened again inside that moment, it used to count as still
  // open and nothing happened. Only an open one is still somebody's.
  if(existing){ if(existing.open) return; existing.remove(); }
  if(!document.getElementById('ai-playground-style')) {
    const link=document.createElement('link'); link.id='ai-playground-style';link.rel='stylesheet';link.href='/static/css/ai-playground.css';document.head.append(link);
  }
  const dialog=document.createElement('dialog'); dialog.id='ai-playground';dialog.setAttribute('aria-labelledby','ap-title');
  dialog.innerHTML=`
    <header class="ap-header">
      <div class="ap-brand">
        <span class="ap-brand-mark" aria-hidden="true"><svg class="ap-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5Z"/></svg></span>
        <h1 id="ap-title">${i18n.t('Sudar')} <span class="ap-private">${i18n.t('On-device AI')}</span></h1>
      </div>
      <div class="ap-bar">
        <div class="ap-cluster" role="group" aria-label="${i18n.t('History actions')}">
          <button type="button" class="btn" data-undo title="${i18n.t('Undo (Ctrl+Z)')}" disabled>${i18n.t('Undo')}</button>
          <button type="button" class="btn" data-redo title="${i18n.t('Redo (Ctrl+Y)')}" disabled>${i18n.t('Redo')}</button>
          <button type="button" class="btn" data-reset title="${i18n.t('Reset to original')}">${i18n.t('Reset all')}</button>
        </div>
        <div class="ap-cluster ap-view-modes" role="group" aria-label="${i18n.t('Preview mode')}">
          <button type="button" class="btn" data-view="compare" aria-pressed="true">${i18n.t('Compare')}</button>
          <button type="button" class="btn" data-view="edited" aria-pressed="false">${i18n.t('Edited')}</button>
          <button type="button" class="btn" data-view="original" aria-pressed="false">${i18n.t('Original')}</button>
        </div>
        <label class="ap-zoom" style="margin-left:auto; display:flex; align-items:center; gap:6px; font-size:11px; color:var(--ap-muted);">
          ${i18n.t('Zoom')}
          <select data-zoom style="padding:4px 8px; font-size:11px; background:var(--ap-card); border-radius:6px; color:inherit; border:1px solid var(--ap-line);">
            <option value="fit">${i18n.t('Fit')}</option>
            <option value="1">100%</option>
            <option value="1.5">150%</option>
          </select>
        </label>
        <button type="button" class="btn ap-theme-btn" id="ap-theme-btn" title="${i18n.t('Theme (T)')}" aria-label="${i18n.t('Toggle theme')}">
          <svg viewBox="0 0 24 24" class="ico-sun"><circle cx="12" cy="12" r="4.5"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M19.1 4.9l-1.4 1.4M6.3 17.7l-1.4 1.4"/></svg>
          <svg viewBox="0 0 24 24" class="ico-moon"><path d="M20 14.5A8.5 8.5 0 1 1 10.2 4a7 7 0 0 0 9.8 10.5Z"/></svg>
        </button>
      </div>
      <button type="button" data-close aria-label="${i18n.t('Close Sudar')}"><svg class="ap-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12"/></svg></button>
    </header>

    <div class="ap-layout">
      <section class="ap-workspace" aria-label="${i18n.t('Photo preview')}">
        <div class="ap-viewport">
          <div class="ap-empty">${i18n.t('Open a photo from the gallery to begin editing.')}</div>
          <div class="ap-stage" hidden>
            <img class="ap-edited" alt="${i18n.t('Edited preview')}">
            <img class="ap-original" alt="${i18n.t('Original photo')}">
            <div class="ap-divider" aria-hidden="true"><span class="ap-divider-handle">↔</span></div>
            <span class="ap-before">${i18n.t('Original')}</span>
            <span class="ap-after">${i18n.t('Edited')}</span>
          </div>
        </div>

        <!-- Overlaid floating controls on workspace foot -->
        <label class="ap-floating-compare ap-compare" hidden>
          ${i18n.t('Before')}
          <input data-compare type="range" min="0" max="100" value="50" aria-label="${i18n.t('Before and after comparison position')}">
          ${i18n.t('After')}
        </label>
        <p class="ap-status" role="status" aria-live="polite">${i18n.t('Open a photo to begin.')}</p>
      </section>

      <aside class="ap-studio" aria-label="${i18n.t('Editing tools')}">
        <!-- Studio Navigation Switcher -->
        <div class="ap-studio-nav" role="tablist">
          <button type="button" class="ap-mode-btn active" data-tab="adjustments"><svg class="ap-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12"/><circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/></svg>${i18n.t('Adjust')}</button>
          <button type="button" class="ap-mode-btn" data-tab="ai"><svg class="ap-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M9.5 3.5 11.2 7.8 15.5 9.5 11.2 11.2 9.5 15.5 7.8 11.2 3.5 9.5 7.8 7.8Z"/><path d="M17.5 14 18.4 16.6 21 17.5 18.4 18.4 17.5 21 16.6 18.4 14 17.5 16.6 16.6Z"/></svg>${i18n.t('AI assist')}</button>
          <button type="button" class="ap-mode-btn" data-tab="magic"><svg class="ap-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M15 4V2M15 16v-2M8 9h2M20 9h2M17.8 11.8 19 13M17.8 6.2 19 5M3 21l9-9M12.2 6.2 11 5"/></svg>${i18n.t('Magic tools')}</button>
        </div>

        <div class="ap-tools-body">
          <!-- PANEL 1: ADJUSTMENTS & LOOKS -->
          <div data-panel="adjustments" style="display:flex; flex-direction:column; gap:16px;">
            <div>
              <div class="ap-group-title">${i18n.t('Creative looks')}</div>
              <div class="ap-presets-grid">
                <button type="button" class="ap-preset-card" data-preset="vivid"><strong>${i18n.t('Vivid')}</strong><span>${i18n.t('Punchy contrast & color')}</span></button>
                <button type="button" class="ap-preset-card" data-preset="golden"><strong>${i18n.t('Golden hour')}</strong><span>${i18n.t('Warm sunlit tone')}</span></button>
                <button type="button" class="ap-preset-card" data-preset="cinematic"><strong>${i18n.t('Cinematic')}</strong><span>${i18n.t('Moody contrast & shade')}</span></button>
                <button type="button" class="ap-preset-card" data-preset="bw"><strong>${i18n.t('Studio B&W')}</strong><span>${i18n.t('Rich monochrome')}</span></button>
                <button type="button" class="ap-preset-card" data-preset="bright"><strong>${i18n.t('Bright & clean')}</strong><span>${i18n.t('Lifted shadows & clarity')}</span></button>
                <button type="button" class="ap-preset-card" data-preset="vintage"><strong>${i18n.t('Vintage warm')}</strong><span>${i18n.t('Soft film warmth')}</span></button>
                <button type="button" class="ap-preset-card" data-preset="hdr"><strong>${i18n.t('HDR')}</strong><span>${i18n.t('Open shadows, held skies, crisp detail')}</span></button>
              </div>
            </div>

            <div class="ap-suggestions-section">
              <div class="ap-group-title">${i18n.t('Contextual AI suggestions')}</div>
              <div class="ap-suggestions">${i18n.t('Choose a photo to analyze.')}</div>
            </div>

            <fieldset disabled class="ap-panel ap-panel-sliders" style="border:0; padding:0; margin:0; display:flex; flex-direction:column; gap:16px; background:none;">
              <div>
                <div class="ap-group-title">${i18n.t('Light')}</div>
                <div class="ap-controls-light ap-controls"></div>
              </div>

              <div>
                <div class="ap-group-title">${i18n.t('Color')}</div>
                <div class="ap-controls-color"></div>
              </div>

              <div>
                <div class="ap-group-title">${i18n.t('Detail & optics')}</div>
                <div class="ap-controls-detail"></div>
              </div>

              <div>
                <div class="ap-group-title">${i18n.t('Composition')}</div>
                <div class="ap-controls-geometry"></div>
                <div style="display:grid; grid-template-columns:minmax(0,1fr) auto; align-items:center; gap:8px; font-size:11.5px; color:var(--ap-muted); margin-top:6px;">
                  <span style="color:var(--ap-muted);">${i18n.t('Crop framing')}</span>
                  <select data-crop style="font-size:11px; padding:4px 8px; background:var(--ap-card);">
                    <option value="original">${i18n.t('Original framing')}</option>
                    <option value="square">${i18n.t('Square · 1:1')}</option>
                    <option value="landscape">${i18n.t('Landscape · 16:9')}</option>
                    <option value="portrait">${i18n.t('Portrait · 4:5')}</option>
                    <option value="story">${i18n.t('Story · 9:16')}</option>
                  </select>
                </div>
                <p class="ap-crop-note" hidden>${i18n.t('Crop preview uses same framing. Reset to view full original.')}</p>
              </div>
            </fieldset>
          </div>

          <!-- PANEL 2: AI ASSIST -->
          <div data-panel="ai" hidden style="display:flex; flex-direction:column; gap:14px;">
            <form class="ap-ask">
              <h2 class="ap-ask-title">${i18n.t('Natural language editing')}</h2>
              <div style="display:flex; flex-direction:column; gap:4px; font-size:11px; color:var(--ap-muted);">
                <span>${i18n.t('Engine')}</span>
                <select data-provider style="font-size:11.5px; padding:6px 10px; background:var(--ap-card);">
                  <option value="builtin">${i18n.t('Built-in · private browser adjustments')}</option>
                  <option value="local">${i18n.t('Local AI · flexible language (Ollama)')}</option>
                  <option value="generate">${i18n.t('Generative AI · preview diffusion')}</option>
                </select>
              </div>

              <div class="ap-prompt-box">
                <textarea id="ap-prompt" rows="3" maxlength="2000" placeholder="${i18n.t('e.g. Lift shadows, soften highlights, warm white balance, crop to 4:5')}" disabled></textarea>
                <div class="ap-prompt-bar">
                  <span class="ap-prompt-shortcut">${i18n.t('Ctrl+Enter to plan')}</span>
                  <button class="btn primary" type="submit" disabled style="padding:6px 14px; font-size:12px;">${i18n.t('Plan edits')}</button>
                </div>
              </div>

              <fieldset data-generation-options hidden style="border:1px solid var(--ap-line); border-radius:10px; padding:12px; display:flex; flex-direction:column; gap:8px; background:var(--ap-rail);">
                <legend style="font-size:11px; font-weight:600; color:var(--ap-accent); padding:0 4px;">${i18n.t('Generation controls')}</legend>
                <label style="display:flex; justify-content:space-between; align-items:center; font-size:11px; color:var(--ap-muted);">${i18n.t('Quality')}
                  <select data-generation-quality style="font-size:11px; padding:3px 6px; background:var(--ap-card);">
                    <option value="draft">${i18n.t('Draft')}</option>
                    <option value="balanced" selected>${i18n.t('Balanced')}</option>
                    <option value="detailed">${i18n.t('Detailed')}</option>
                  </select>
                </label>
                <label style="display:flex; justify-content:space-between; align-items:center; font-size:11px; color:var(--ap-muted);">${i18n.t('Fidelity')}
                  <select data-generation-fidelity style="font-size:11px; padding:3px 6px; background:var(--ap-card);">
                    <option value="preserve">${i18n.t('Preserve')}</option>
                    <option value="balanced">${i18n.t('Balanced')}</option>
                    <option value="creative">${i18n.t('Creative')}</option>
                  </select>
                </label>
                <label style="display:flex; flex-direction:column; gap:3px; font-size:11px; color:var(--ap-muted);">${i18n.t('Avoid in result')}
                  <textarea data-generation-negative rows="2" maxlength="500" placeholder="blur, artifacts, oversaturation" style="font:inherit; font-size:11px; background:var(--ap-card); color:var(--ap-text); border:1px solid var(--ap-line); border-radius:6px; padding:5px 8px;"></textarea>
                </label>
                <div style="display:flex; gap:8px; align-items:center;">
                  <label style="display:flex; align-items:center; gap:6px; font-size:11px; color:var(--ap-muted);">${i18n.t('Seed')}
                    <input data-generation-seed type="number" min="0" max="2147483647" step="1" value="42" style="width:90px; font-size:11px; padding:3px 6px; background:var(--ap-card); color:var(--ap-text); border:1px solid var(--ap-line); border-radius:6px;">
                  </label>
                  <button class="btn" type="button" data-new-seed style="padding:4px 8px; font-size:11px;">${i18n.t('Random')}</button>
                </div>
              </fieldset>

              <small data-provider-note style="font-size:11px; color:var(--ap-dim); line-height:1.4;">${i18n.t('Combine lighting, color, mood and framing requests. Review proposed changes before applying.')}</small>
              <div>
                <div class="ap-group-title">${i18n.t('Ideas')}</div>
                <div class="ap-prompt-ideas"></div>
              </div>
              <div data-hairstyles hidden>
                <div class="ap-group-title">${i18n.t('Hairstyles · generative')}</div>
                <div class="ap-hairstyle-ideas"></div>
                <small style="font-size:11px; color:var(--ap-dim); line-height:1.4;">${i18n.t('A new hairstyle is drawn by the image model, so it is a generated preview to review, not a slider. Fuller hair, strands, grey and colour are in Skin & hair retouch.')}</small>
              </div>
            </form>

            <section class="ap-plan" hidden aria-label="${i18n.t('Proposed AI edit')}">
              <h2>${i18n.t('Proposed AI edit')}</h2>
              <p data-plan-summary style="font-size:11.5px; color:var(--ap-text); margin:0;"></p>
              <ul data-plan-changes style="margin:4px 0; padding-left:18px;"></ul>
              <button class="btn primary" data-apply-plan style="width:100%;">${i18n.t('Apply planned edits')}</button>
            </section>

            <section class="ap-generated" hidden aria-label="${i18n.t('AI-generated result')}">
              <h2>${i18n.t('Generated result')}</h2>
              <p data-generated-note style="font-size:11px; color:var(--ap-muted); margin:0;"></p>
              <img alt="${i18n.t('AI-generated edit preview')}">
              <div style="display:flex; gap:8px; flex-wrap:wrap;">
                <button class="btn primary" data-save-generated hidden style="flex:1;">${i18n.t('Save to library')}</button>
                <button class="btn" data-download-generated style="flex:1;">${i18n.t('Download image')}</button>
                <button class="btn" data-discard-generated>${i18n.t('Discard')}</button>
              </div>
            </section>
          </div>

          <!-- PANEL 3: MAGIC TOOLS -->
          <div data-panel="magic" hidden style="display:flex; flex-direction:column; gap:12px;">
            <div class="ap-magic-card">
              <h3><svg class="ap-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M9.5 3.5 11.2 7.8 15.5 9.5 11.2 11.2 9.5 15.5 7.8 11.2 3.5 9.5 7.8 7.8Z"/><path d="M17.5 14 18.4 16.6 21 17.5 18.4 18.4 17.5 21 16.6 18.4 14 17.5 16.6 16.6Z"/></svg>${i18n.t('Skin & hair retouch')}</h3>
              <p>${i18n.t('Every person in the photograph, each against their own skin: light, colour, shine and blemishes; hair strands, grey and colour. A bindi or sindoor is left as it is.')}</p>
              <button class="btn primary" data-portrait style="align-self:flex-start;">${i18n.t('Open retouch studio')}</button>
            </div>

            <div class="ap-magic-card">
              <h3><svg class="ap-icon" viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="8.5" cy="10" r="1.6"/><path d="m5 17 4.5-4.5 3.5 3.5 2.5-2.5L21 18"/></svg>${i18n.t('Background tools')}</h3>
              <p>${i18n.t('Automatically segment subject to remove background or apply realistic depth-of-field lens blur.')}</p>
              <div style="display:flex; gap:6px; flex-wrap:wrap;">
                <button class="btn" data-remove-bg hidden>${i18n.t('Cut out subject')}</button>
                <button class="btn" data-blur-bg hidden>${i18n.t('Blur background')}</button>
                <button class="btn" data-pop-bg hidden>${i18n.t('Colour pop')}</button>
              </div>
              <section class="ap-background" hidden aria-label="${i18n.t('Background result')}" style="margin-top:10px; padding:10px; background:var(--ap-rail);">
                <h2 data-background-title style="font-size:12px;">${i18n.t('Background result')}</h2>
                <img alt="${i18n.t('Background preview')}" style="max-height:160px; object-fit:contain; margin:4px 0;">
                <label data-blur-control hidden style="display:grid; grid-template-columns:minmax(0,1fr) auto; gap:4px; font-size:11px; color:var(--ap-muted); margin:4px 0;">
                  <span>${i18n.t('Blur strength')}</span>
                  <input data-blur-strength type="range" min="2" max="40" value="16" style="grid-column:1/-1;">
                </label>
                <div style="display:flex; gap:6px; margin-top:6px; flex-wrap:wrap;">
                  <button class="btn primary" data-save-background hidden style="flex:1; font-size:11.5px; padding:5px 10px;">${i18n.t('Save to library')}</button>
                  <button class="btn" data-download-background style="flex:1; font-size:11.5px; padding:5px 10px;">${i18n.t('Download result')}</button>
                  <button class="btn" data-discard-background style="font-size:11.5px; padding:5px 10px;">${i18n.t('Discard')}</button>
                </div>
              </section>
            </div>

            <div class="ap-magic-card">
              <h3><svg class="ap-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 20H8.5l-4.2-4.2a1.5 1.5 0 0 1 0-2.1l9.3-9.3a1.5 1.5 0 0 1 2.1 0l4.9 4.9a1.5 1.5 0 0 1 0 2.1L12 20M9 9l7 7"/></svg>${i18n.t('Object removal & inpainting')}</h3>
              <p>${i18n.t('Paint over unwanted objects, photobombers, wires or blemishes to seamlessly patch from surroundings.')}</p>
              <button class="btn" data-remove-object hidden style="align-self:flex-start;">${i18n.t('Remove object')}</button>
            </div>

            <div class="ap-magic-card" data-add-hair-card hidden>
              <h3><svg class="ap-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 20c0-6 3-10 7-12 4 2 7 6 7 12M12 8c-2 3-2 7 0 12M9 9c-3 3-3 8-1 11M15 9c3 3 3 8 1 11"/></svg>${i18n.t('Add hair · generative')}</h3>
              <p>${i18n.t('Paint where there should be hair — a receding hairline, a thin crown, a bald patch — and the AI server draws this person\'s own hair there. For hair that is there but thin, Skin & hair retouch has Fuller hair and Add hair, with no model at all.')}</p>
              <button class="btn" data-add-hair style="align-self:flex-start;">${i18n.t('Paint where hair should be')}</button>
            </div>

            <div class="ap-magic-card">
              <h3><svg class="ap-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M8 4h8l3 3-2 3-1-1v11H8V9L7 10 5 7Z"/><path d="M12 4a2 2 0 0 0 4 0"/></svg>${i18n.t('Clothing colour')}</h3>
              <p>${i18n.t('Brush over a dress, shirt or sari and choose a new colour. The folds and the weave stay; only the colour changes, and only where you painted.')}</p>
              <button class="btn" data-recolor style="align-self:flex-start;">${i18n.t('Open clothing colour')}</button>
            </div>

            <div class="ap-magic-card" data-gemini-magic-card hidden>
              <h3><svg class="ap-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 8V5a1 1 0 0 1 1-1h3M16 4h3a1 1 0 0 1 1 1v3M20 16v3a1 1 0 0 1-1 1h-3M8 20H5a1 1 0 0 1-1-1v-3"/><circle cx="12" cy="12" r="3"/></svg>${i18n.t('Google Gemini vision & analysis')}</h3>
              <p>${i18n.t('Multimodal scene analysis, automatic photo description, semantic tagging, and smart photographic recommendations.')}</p>
              <button class="btn primary" data-gemini-analyze style="align-self:flex-start;">${i18n.t('Analyze with Gemini')}</button>
              <div data-gemini-analysis-result hidden style="margin-top:8px; display:flex; flex-direction:column; gap:6px; font-size:11px;">
                <p data-gemini-caption style="color:var(--ap-text); margin:0; font-weight:500;"></p>
                <div data-gemini-tags style="display:flex; flex-wrap:wrap; gap:4px;"></div>
                <p data-gemini-critique style="color:var(--ap-dim); margin:0; font-style:italic;"></p>
                <button class="btn" data-gemini-apply-suggestions style="align-self:flex-start; margin-top:4px;" hidden>${i18n.t('Apply suggested edits')}</button>
              </div>
            </div>

            <div class="ap-magic-card" data-server-tools>
              <h3><svg class="ap-icon" viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/></svg>${i18n.t('Enhance tools')}</h3>
              <p data-server-tools-note>${i18n.t('Upscale a small photograph, restore the faces in an old one, and colourise a black-and-white print. Each needs a model: one tap per tool, nothing leaves the house.')}</p>
              <div style="display:flex; gap:6px; flex-wrap:wrap;">
                <button class="btn" data-server-job="upscale" disabled>${i18n.t('Upscale')}</button>
                <button class="btn" data-server-job="restore" disabled>${i18n.t('Restore faces')}</button>
                <button class="btn" data-server-job="colorize" disabled>${i18n.t('Colourise')}</button>
                <button class="btn" data-revive hidden title="${i18n.t('Restore faces, then colourise: one tap for an old black-and-white print.')}">${i18n.t('Revive old photo')}</button>
              </div>
              <ul data-enhance-needs hidden style="margin:4px 0 0; padding-left:16px; font-size:11px; color:var(--ap-dim); line-height:1.45;"></ul>
              <section class="ap-server-result" hidden aria-label="${i18n.t('AI server result')}" style="margin-top:10px; padding:10px; background:var(--ap-rail);">
                <h2 data-server-title style="font-size:12px;">${i18n.t('AI server result')}</h2>
                <p data-server-note style="font-size:11px; color:var(--ap-muted); margin:0;"></p>
                <img alt="${i18n.t('AI server result preview')}" style="max-height:160px; object-fit:contain; margin:4px 0;">
                <div style="display:flex; gap:6px; margin-top:6px; flex-wrap:wrap;">
                  <button class="btn primary" data-save-server hidden style="flex:1; font-size:11.5px; padding:5px 10px;">${i18n.t('Save to library')}</button>
                  <button class="btn" data-download-server style="flex:1; font-size:11.5px; padding:5px 10px;">${i18n.t('Download result')}</button>
                  <button class="btn" data-discard-server style="font-size:11.5px; padding:5px 10px;">${i18n.t('Discard')}</button>
                </div>
              </section>
            </div>

            <div class="ap-magic-card">
              <h3><svg class="ap-icon" viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="2"/><rect x="12" y="11" width="6" height="5" rx="1"/></svg>${i18n.t('Creative Studio')}</h3>
              <p>${i18n.t('Create picture-in-picture collages, family album memories and bespoke compositions.')}</p>
              <button class="btn" data-creative style="align-self:flex-start;">${i18n.t('Open Creative Studio')}</button>
            </div>
          </div>
        </div>

        <!-- Footer: Sticky Download / Save -->
        <div class="ap-studio-footer">
          <button class="btn primary" data-save hidden>${i18n.t('Save copy to Ninaivu library')}</button>
          <p data-save-note hidden style="font-size:10.5px; color:var(--ap-dim); margin:0;">${i18n.t('Saved alongside your original on Ninaivu server.')}</p>
          <div class="ap-export-row">
            <select data-format aria-label="${i18n.t('Export format')}">
              <option value="image/png">${i18n.t('PNG · lossless')}</option>
              <option value="image/jpeg">${i18n.t('JPEG · standard')}</option>
              <option value="image/webp">${i18n.t('WebP · modern')}</option>
            </select>
            <button class="btn" data-export disabled>${i18n.t('Download image')}</button>
          </div>
          <div style="display:flex; justify-content:space-between; align-items:center; font-size:10.5px; color:var(--ap-dim); margin-top:2px;">
            <span class="ap-dimensions"></span>
            <span>${i18n.t('Metadata stripped for privacy')}</span>
          </div>
        </div>
      </aside>
    </div>`;

  document.body.append(dialog);
  const $=s=>dialog.querySelector(s), service=new AIPhotoService(item?.id ?? null), controller=new AbortController();

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
    [i18n.t('Natural light'),'Lift the shadows, reduce highlights and gently improve contrast'],
    [i18n.t('Golden hour'),'Make it warmer with soft contrast and a golden hour look'],
    [i18n.t('Monochrome'),'Make it black and white with stronger contrast'],
    [i18n.t('Cinematic film'),'Cinematic film mood with rich contrast and deep shadows'],
    [i18n.t('Bright & clean'),'Lift shadows, reduce highlights, and brighten the photo for a clean open feel'],
    [i18n.t('Skin retouch'),'Retouch skin, soften blemishes and even out the tone'],
    [i18n.t('Hair & hairstyle'),'Cover grey hair and bring out the strands in the hair'],
  ]) {
    const button=document.createElement('button');button.type='button';button.className='btn';button.textContent=title;
    button.onclick=()=>{if(busy)return;$('#ap-prompt').value=prompt;clearPlan();$('#ap-prompt').focus();};prompts.append(button);
  }
  // A hairstyle is drawn, not adjusted: choosing one puts its request in the
  // box and turns the engine to a generative one, and the person still presses
  // Generate and reviews the preview. Shown only when such an engine is on.
  const hairstyles=$('.ap-hairstyle-ideas');
  for(const [title,prompt] of HAIRSTYLES) {
    const button=document.createElement('button');button.type='button';button.className='btn';button.textContent=i18n.t(title);
    button.onclick=()=>{if(busy)return;$('#ap-prompt').value=prompt;clearPlan();useGenerativeEngine();$('#ap-prompt').focus();};hairstyles.append(button);
  }
  /** The generative engine that is on, if one is: the image model here or on the AI server first, Gemini only when it is the one there is. */
  const generativeMode=()=>capabilities.image_model&&capabilities.image_provider?'generate':$('[data-provider] option[value="gemini-edit"]')?'gemini-edit':null;
  function useGenerativeEngine(){
    const mode=generativeMode();
    if(!mode)return false;
    if($('[data-provider]').value!==mode){$('[data-provider]').value=mode;$('[data-provider]').onchange();}
    return true;
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
  let pendingPlan=null, analysis={}, generatedURL=null, generatedBlob=null;
  let capabilities={}, background=null, backgroundURL=null, backgroundKind=null, backgroundBlob=null;
  let backgroundRevision=0,generatedUnsaved=false,backgroundUnsaved=false;

  function clearPlan(){pendingPlan=null;$('.ap-plan').hidden=true;}
  function clearGenerated(){if(generatedURL)URL.revokeObjectURL(generatedURL);generatedURL=null;generatedBlob=null;generatedUnsaved=false;$('.ap-generated img').removeAttribute('src');$('.ap-generated').hidden=true;}
  function clearBackground(){
    backgroundRevision++;backgroundUnsaved=false;
    if(backgroundURL)URL.revokeObjectURL(backgroundURL);backgroundURL=null;backgroundBlob=null;backgroundKind=null;
    if(background){background.subject.close();background.mask.close();background=null;}
    $('.ap-background img').removeAttribute('src');$('.ap-background').hidden=true;$('[data-blur-control]').hidden=true;
  }
  const dirty=()=>JSON.stringify(history.current)!==savedState;
  const confirmDiscard=()=>!(dirty()||generatedUnsaved||backgroundUnsaved)||window.confirm(i18n.t('Discard your unsaved photo edits and results?'));
  const status=message=>{$('.ap-status').textContent=message;};
  const releaseURLs=()=>{urls.forEach(URL.revokeObjectURL);urls=[];};
  //: The chosen option as the page shows it — "detailed" in English, its own word in Tamil.
  const optionName=selector=>($(selector).selectedOptions[0]?.textContent||$(selector).value).toLowerCase();

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
    // With no library to save to, downloading is the one thing left to do.
    $('[data-export]').classList.toggle('primary',$('[data-save]').hidden);
    // Every result can go into the library the same way the sliders' edit
    // does: an administrator's copy is published, a family member's waits in
    // the administrator's queue. The server decides which; the button is the same.
    for(const [selector,have] of [['[data-save-generated]',generatedBlob],['[data-save-background]',backgroundBlob],['[data-save-server]',serverBlob]]){
      $(selector).hidden=!canSave||!sourceId;$(selector).disabled=value||!have;
    }
    $('[data-remove-bg]').hidden=!capabilities.segmentation_model;$('[data-blur-bg]').hidden=!capabilities.segmentation_model;$('[data-pop-bg]').hidden=!capabilities.segmentation_model;
    $('[data-remove-object]').hidden=!capabilities.object_removal;
    $('[data-add-hair-card]').hidden=!(capabilities.server_jobs||[]).includes('inpaint');$('[data-add-hair]').disabled=value||!bitmap;
    $('[data-recolor]').disabled=value||!bitmap;
    $('[data-hairstyles]').hidden=!generativeMode();
    const serverJobs=capabilities.server_jobs||[],localJobs=capabilities.local_jobs||[],tools=capabilities.enhance_tools||{};let anyServer=false,anyLocal=false;
    const needs=$('[data-enhance-needs]');needs.replaceChildren();
    for(const button of dialog.querySelectorAll('[data-server-job]')){
      const kind=button.dataset.serverJob,ready=serverJobs.includes(kind)||localJobs.includes(kind);
      anyServer||=serverJobs.includes(kind);anyLocal||=localJobs.includes(kind);
      button.disabled=value||!bitmap||!ready;
      button.title=ready?'':i18n.t('Not set up yet; see below.');
      if(!ready){
        const li=document.createElement('li');
        const name={upscale:i18n.t('Upscale'),restore:i18n.t('Restore faces'),colorize:i18n.t('Colourise')}[kind];
        const tool=tools[kind]||{};
        li.textContent=tool.by_hand
          ?i18n.t('{tool} needs the {model} model, which an administrator places in the AI models folder (the console\'s AI models tab says where), or an AI server workflow.',{tool:name,model:tool.model||i18n.t('colourising')})
          :i18n.t('{tool} needs the {model} model, which an administrator downloads under AI → AI models, or an AI server workflow.',{tool:name,model:tool.model||name});
        needs.append(li);
      }
    }
    needs.hidden=!needs.childElementCount;
    const ready=kind=>serverJobs.includes(kind)||localJobs.includes(kind);
    $('[data-revive]').hidden=!(ready('restore')&&ready('colorize'));$('[data-revive]').disabled=value||!bitmap;
    if(anyServer||anyLocal)$('[data-server-tools-note]').textContent=anyServer&&anyLocal?i18n.t('Some run on the graphics-card PC on your home network (a preview of this photo is sent there), the rest on your Ninaivu server.'):anyServer?i18n.t('Run on the graphics-card PC on your home network. A preview of this photo is sent there and nowhere else.'):i18n.t('Run on your Ninaivu server with downloaded AI models. Nothing leaves it.');
  }

  const sliderGroups = {
    light: [
      ['exposure', i18n.t('Exposure')],
      ['contrast', i18n.t('Contrast')],
      ['shadows', i18n.t('Shadows')],
      ['highlights', i18n.t('Highlights')],
      ['dehaze', i18n.t('Dehaze')],
    ],
    color: [
      ['vibrance', i18n.t('Vibrance')],
      ['saturation', i18n.t('Saturation')],
      ['warmth', i18n.t('White balance')],
    ],
    detail: [
      ['clarity', i18n.t('Clarity')],
      ['sharpness', i18n.t('Sharpness')],
      ['noise', i18n.t('Noise reduction')],
      ['vignette', i18n.t('Vignette')],
    ],
    geometry: [
      ['angle', i18n.t('Straighten')],
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
      row.title = i18n.t('Double-click to reset to 0');

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
    vivid: {contrast: 20, vibrance: 30, saturation: 8, clarity: 15, sharpness: 20, exposure: 5},
    golden: {warmth: 35, highlights: -15, shadows: 15, vignette: 10},
    cinematic: {contrast: 25, shadows: -15, highlights: -15, warmth: 10, vignette: 20},
    bw: {saturation: -100, contrast: 25, clarity: 20, sharpness: 25, exposure: 10},
    bright: {exposure: 20, shadows: 30, highlights: -25, clarity: 10, sharpness: 15},
    vintage: {warmth: 25, contrast: -10, dehaze: -15, vignette: 25, shadows: 10},
    hdr: {shadows: 35, highlights: -35, clarity: 25, vibrance: 15, dehaze: 10},
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
    previewReady = false; lock(true); status(i18n.t('Applying adjustments…'));
    try {
      const a = history.current;
      const edited = await service.applyAdjustments(bitmap, a);
      const original = await service.applyAdjustments(bitmap, {...defaults(), crop: a.crop, angle: a.angle});
      if(closed) return;
      releaseURLs(); urls = [URL.createObjectURL(edited), URL.createObjectURL(original)];
      $('.ap-edited').src = urls[0]; $('.ap-original').src = urls[1];
      $('.ap-stage').hidden = false; $('.ap-compare').hidden = false;
      $('.ap-crop-note').hidden = a.crop === 'original' && a.angle === 0;
      previewReady = true; $('.ap-empty').hidden = true; setView(viewMode); sync(); status(i18n.t('Preview updated.'));
    } catch(error) { sync(); status(i18n.t('{reason} Preview could not update.', {reason: errorText(error, i18n.t)})); }
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
    if(busy || !file || !confirmDiscard()) return; lock(true); status(i18n.t('Analyzing photograph…'));
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
      status(i18n.t('Generating contextual suggestions…'));
      analysis = service.analyzeImage(bitmap);
      const cards = service.getSuggestions(analysis); $('.ap-suggestions').replaceChildren();
      for(const card of cards) {
        const button = document.createElement('button'); button.className = 'ap-card'; button.type = 'button';
        const title = document.createElement('strong'), reason = document.createElement('span');
        title.textContent = i18n.t(card.title); reason.textContent = i18n.t(card.reason);
        button.append(title, reason); button.onclick = () => apply(card.patch); $('.ap-suggestions').append(button);
      }
      await preview();
    } catch(error) { status(errorText(error, i18n.t)); }
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

  /** A tool's finished picture becomes the photograph on the canvas, with the sliders at zero again, so it can be adjusted further and saved like any edit. */
  async function takeOnto(next, message){
    if(closed || !next) return;
    bitmap?.close();
    bitmap = next;
    history = new History(defaults());
    savedState = JSON.stringify(defaults());
    clearGenerated(); clearBackground();
    await preview();
    status(message);
  }

  async function clothingColour(){
    if(busy || !bitmap) return;
    lock(true); status(i18n.t('Opening clothing colour…'));
    try{
      const blob = await service.applyAdjustments(bitmap, history.current, {maxSide: Infinity, type: 'image/png'});
      if(closed) return;
      const source = await createImageBitmap(blob);
      if(closed){source.close(); return;}
      openRecolor(source, {onApply: recoloured => takeOnto(recoloured, i18n.t('Clothing colour applied to the photo. Save a copy to keep it.'))});
      status(i18n.t('Brush over the clothing, choose a colour, then apply.'));
    } catch(error){ status(errorText(error, i18n.t)); }
    finally { if(!closed) lock(false); }
  }
  $('[data-recolor]').onclick = () => { if(!busy && bitmap) clothingColour(); };

  /** Save a tool's result as a copy in the library, the way the main Save does. */
  async function saveResult(blob, afterwards){
    if(busy || !blob || !sourceId || !canSave) return;
    saving = true; lock(true); status(i18n.t('Saving new library copy…'));
    try {
      const copy = await saveLibraryCopy(sourceId, blob);
      saving = false; afterwards?.();
      status(copy.pending ? i18n.t(copy.message) : i18n.t('Copy saved to the library.'));
      onSaved?.(copy);
    } catch(error){ status(errorText(error, i18n.t)); }
    finally { saving = false; if(!closed) lock(false); }
  }

  async function portraitStudio(){
    if(busy || !bitmap) return;
    lock(true); status(i18n.t('Opening skin & hair retouch…'));
    try{
      const blob = await service.applyAdjustments(bitmap, history.current, {maxSide: Infinity, type: 'image/png'});
      if(closed) return;
      const source = await createImageBitmap(blob);
      if(closed){source.close(); return;}
      openPortraitStudio(source, retouchedBitmap => takeOnto(retouchedBitmap, i18n.t('Skin and hair enhancements applied to canvas.')));
      status(i18n.t('Choose a person, or everyone, and adjust what they need.'));
    } catch(error){ status(errorText(error, i18n.t)); }
    finally { if(!closed) lock(false); }
  }
  $('[data-portrait]').onclick = () => { if(!busy && bitmap) portraitStudio(); };

  $('[data-creative]').onclick = async () => {
    if(busy || !bitmap) return; lock(true); status(i18n.t('Preparing Creative Studio…'));
    try{
      const original = await service.applyAdjustments(bitmap, defaults(), {maxSide: 1600, type: 'image/png'});
      const edited = await service.applyAdjustments(bitmap, history.current, {maxSide: 1600, type: 'image/png'});
      if(closed) return;
      const {openCreativeStudio} = await import('../../creative-studio.js');
      await openCreativeStudio({original, edited, returnFocus: $('[data-creative]'), sourceId, canSave, onSaved: copy => { onSaved?.(copy); }});
      status(i18n.t('Creative Studio opened.'));
    } catch(error){ if(!closed) status(errorText(error, i18n.t)); }
    finally { if(!closed) lock(false); }
  };

  async function refreshBackground(){
    backgroundRevision++;
    if(background){background.subject.close(); background.mask.close(); background=null;}
    const next = await service.computeBackground(bitmap, history.current, controller.signal);
    if(closed){next.subject.close(); next.mask.close(); throw new Error(i18n.t('Workspace closed.'));}
    background = next;
    return background;
  }

  async function removeBackground(){
    if(busy || !bitmap) return; lock(true); status(i18n.t('Segmenting subject on Ninaivu server…'));
    try {
      const {subject, mask} = await refreshBackground(); if(closed) return;
      const blob = await service.compositeCutout(subject, mask); if(closed) return;
      if(backgroundURL) URL.revokeObjectURL(backgroundURL);
      backgroundBlob = blob; backgroundUnsaved = true; backgroundKind = 'cutout'; backgroundURL = URL.createObjectURL(blob);
      $('.ap-background img').src = backgroundURL; $('[data-background-title]').textContent = i18n.t('Background removed');
      $('[data-blur-control]').hidden = true; $('.ap-background').hidden = false;
      status(i18n.t('Background removed.'));
    } catch(error) { if(!closed) status(errorText(error, i18n.t)); }
    finally { if(!closed) lock(false); }
  }

  async function blurBackground() {
    if(busy || !bitmap) return; lock(true); status(i18n.t('Segmenting subject on Ninaivu server…'));
    try {
      const {subject, mask} = await refreshBackground(); if(closed) return;
      const blob = await service.compositeBlur(subject, mask, Number($('[data-blur-strength]').value)); if(closed) return;
      if(backgroundURL) URL.revokeObjectURL(backgroundURL);
      backgroundBlob = blob; backgroundUnsaved = true; backgroundKind = 'blur'; backgroundURL = URL.createObjectURL(blob);
      $('.ap-background img').src = backgroundURL; $('[data-background-title]').textContent = i18n.t('Background blurred');
      $('[data-blur-control]').hidden = false; $('.ap-background').hidden = false;
      status(i18n.t('Background blurred.'));
    } catch(error) { if(!closed) status(errorText(error, i18n.t)); }
    finally { if(!closed) lock(false); }
  }
  async function colourPop() {
    if(busy || !bitmap) return; lock(true); status(i18n.t('Segmenting subject on Ninaivu server…'));
    try {
      const {subject, mask} = await refreshBackground(); if(closed) return;
      const blob = await service.compositeColourPop(subject, mask); if(closed) return;
      if(backgroundURL) URL.revokeObjectURL(backgroundURL);
      backgroundBlob = blob; backgroundUnsaved = true; backgroundKind = 'pop'; backgroundURL = URL.createObjectURL(blob);
      $('.ap-background img').src = backgroundURL; $('[data-background-title]').textContent = i18n.t('Colour pop');
      $('[data-blur-control]').hidden = true; $('.ap-background').hidden = false;
      status(i18n.t('Colour pop ready: the subject in colour, the rest in black and white.'));
    } catch(error) { if(!closed) status(errorText(error, i18n.t)); }
    finally { if(!closed) lock(false); }
  }
  $('[data-remove-bg]').onclick = () => { if(!busy && bitmap) removeBackground(); };
  $('[data-blur-bg]').onclick = () => { if(!busy && bitmap) blurBackground(); };
  $('[data-pop-bg]').onclick = () => { if(!busy && bitmap) colourPop(); };

  $('[data-blur-strength]').oninput = async () => {
    if(busy || !background || backgroundKind !== 'blur') return;
    const revision = ++backgroundRevision;
    try{
      const blob = await service.compositeBlur(background.subject, background.mask, Number($('[data-blur-strength]').value));
      if(closed || revision !== backgroundRevision) return;
      if(backgroundURL) URL.revokeObjectURL(backgroundURL);
      backgroundBlob = blob; backgroundUnsaved = true; backgroundURL = URL.createObjectURL(blob); $('.ap-background img').src = backgroundURL;
    } catch(error){ if(!closed && revision === backgroundRevision) status(errorText(error, i18n.t)); }
  };

  $('[data-download-background]').onclick = () => {
    if(!backgroundBlob) return;
    const url = URL.createObjectURL(backgroundBlob), a = document.createElement('a');
    a.href = url; a.download = {cutout: 'ninaivu-background-removed.png', blur: 'ninaivu-background-blurred.png', pop: 'ninaivu-colour-pop.png'}[backgroundKind] || 'ninaivu-background.png';
    a.click(); backgroundUnsaved = false; setTimeout(() => URL.revokeObjectURL(url), 30000);
  };
  $('[data-discard-background]').onclick = clearBackground;
  $('[data-save-background]').onclick = () => saveResult(backgroundBlob, () => { backgroundUnsaved = false; });

  async function removeObject() {
    if(busy || !bitmap) return; lock(true); status(i18n.t('Preparing photo for object removal…'));
    try {
      const blob = await service.applyAdjustments(bitmap, history.current, {maxSide: 1600, type: 'image/png'}); if(closed) return;
      const source = await createImageBitmap(blob); if(closed){source.close(); return;}
      openRemoveObject(source, service, {provider: capabilities.object_removal_provider, onApply: resultBitmap => takeOnto(resultBitmap, i18n.t('Object removed. Save a copy to keep it.'))});
      status(i18n.t('Paint over the object to remove, then apply.'));
    } catch(error) { if(!closed) status(errorText(error, i18n.t)); }
    finally { if(!closed) lock(false); }
  }
  $('[data-remove-object]').onclick = () => { if(!busy && bitmap) removeObject(); };

  async function addHair() {
    if(busy || !bitmap) return; lock(true); status(i18n.t('Preparing photo…'));
    try {
      const blob = await service.applyAdjustments(bitmap, history.current, {maxSide: 1600, type: 'image/png'}); if(closed) return;
      const source = await createImageBitmap(blob); if(closed){source.close(); return;}
      openRemoveObject(source, service, {provider: 'ai-server', onApply: resultBitmap => takeOnto(resultBitmap, i18n.t('Hair added. Save a copy to keep it.')), ask: {
        title: i18n.t('Add hair · generative'),
        lede: i18n.t('Paint where the hair should be; the AI server draws it from this person\'s own hair.'),
        prompt: 'Natural, thick hair in the painted area, continuing this person\'s own hair: the same colour, texture, direction and lighting, blended seamlessly with the hair that is there. Change nothing else in the photograph.',
        done: i18n.t('Done. Apply it to the photo, download it, or paint again and draw once more.'),
      }});
      status(i18n.t('Paint where the hair should be, then draw it.'));
    } catch(error) { if(!closed) status(errorText(error, i18n.t)); }
    finally { if(!closed) lock(false); }
  }
  $('[data-add-hair]').onclick = () => { if(!busy && bitmap) addHair(); };

  let serverURL = null, serverBlob = null;
  function clearServerResult(){ if(serverURL) URL.revokeObjectURL(serverURL); serverURL = null; serverBlob = null; $('.ap-server-result img').removeAttribute('src'); $('.ap-server-result').hidden = true; }
  const serverTitles = {upscale: i18n.t('Upscaled result'), restore: i18n.t('Faces restored result'), colorize: i18n.t('Colourised result'), revive: i18n.t('Revived old photo')};
  const serverReady = {upscale: i18n.t('Upscaled result ready.'), restore: i18n.t('Faces restored result ready.'), colorize: i18n.t('Colourised result ready.'), revive: i18n.t('Revived: faces restored and colourised.')};
  /** One Enhance tool on *source* with *adjustments* baked in, wherever it is set up; the finished PNG. */
  function enhance(kind, source, adjustments) {
    const local = !(capabilities.server_jobs || []).includes(kind);
    return service.serverTool(kind, source, adjustments, controller.signal,
      local ? (kind === 'upscale' ? 1024 : 2048) : (capabilities.image_edit_max_side || 1024),
      state => { if(!closed) status(describeServerJob(state, local)); });
  }
  async function showEnhanced(kind, blob) {
    clearServerResult(); serverBlob = blob; serverURL = URL.createObjectURL(blob);
    const img = $('.ap-server-result img'); img.src = serverURL;
    await img.decode().catch(() => {});
    $('[data-server-title]').textContent = serverTitles[kind] || i18n.t('AI server result');
    $('[data-server-note]').textContent = img.naturalWidth ? `${img.naturalWidth} × ${img.naturalHeight} px` : '';
    $('.ap-server-result').hidden = false;
    status(serverReady[kind] || i18n.t('AI server result ready.'));
  }
  for(const button of dialog.querySelectorAll('[data-server-job]')) {
    button.onclick = async () => {
      if(busy || !bitmap) return;
      const kind = button.dataset.serverJob;
      lock(true); status(i18n.t('Sending to the AI server…'));
      try {
        const blob = await enhance(kind, bitmap, history.current);
        if(closed) return;
        await showEnhanced(kind, blob);
      } catch(error) { if(!closed) status(error.name === 'AbortError' ? i18n.t('Cancelled.') : errorText(error, i18n.t)); }
      finally { if(!closed) lock(false); }
    };
  }
  // An old print in one tap: the faces first, so the colour is laid on
  // restored skin rather than on the damage, then colour for the whole picture.
  $('[data-revive]').onclick = async () => {
    if(busy || !bitmap) return;
    lock(true); status(i18n.t('Restoring faces…'));
    let restored = null;
    try {
      const faces = await enhance('restore', bitmap, history.current);
      if(closed) return;
      restored = await createImageBitmap(faces);
      status(i18n.t('Colourising…'));
      const blob = await enhance('colorize', restored, defaults());
      if(closed) return;
      await showEnhanced('revive', blob);
    } catch(error) { if(!closed) status(error.name === 'AbortError' ? i18n.t('Cancelled.') : errorText(error, i18n.t)); }
    finally { restored?.close(); if(!closed) lock(false); }
  };
  $('[data-download-server]').onclick = () => {
    if(serverURL){ const a = document.createElement('a'); a.href = serverURL; a.download = 'ninaivu-ai-server.png'; a.click(); }
  };
  $('[data-discard-server]').onclick = clearServerResult;
  $('[data-save-server]').onclick = () => saveResult(serverBlob);

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
    $('.ap-ask button[type=submit]').textContent = isGen ? i18n.t('Generate preview') : i18n.t('Plan edits');
    $('[data-provider-note]').textContent = mode === 'builtin'
      ? i18n.t('Combine adjustments and creative looks; review the plan before applying.')
      : mode === 'local'
      ? i18n.t('Sends only your request and slider values to the local Ollama model on your Ninaivu server.')
      : mode === 'gemini-edit'
      ? i18n.t('Generates image edit with Google Gemini ({model}).', {model: capabilities.gemini_image_model||'gemini-3.1-flash-image'})
      : mode === 'gemini-plan'
      ? i18n.t('Plans photo adjustments using Google Gemini ({model}).', {model: capabilities.gemini_vision_model||'gemini-3.8-flash'})
      : capabilities.image_provider === 'ai-server'
      ? i18n.t('Sends a {size}-pixel preview and your request to the AI server on your home network.', {size: capabilities.image_edit_max_side||1024})
      : i18n.t('Sends a {size}-pixel preview and your request to the local image model on your Ninaivu server.', {size: capabilities.image_edit_max_side||512});
  };

  $('.ap-ask').onsubmit = async e => {
    e.preventDefault(); if(busy || !bitmap) return; clearPlan();
    const asked = $('#ap-prompt').value;
    // A new hairstyle is drawn by the image model; without one there is
    // nothing honest to do but say so, rather than hand it to a tool that
    // only retouches the hair that is there.
    if(isHairstyleRequest(asked) && !/\b(fuller|thicker|thinning|volume|grey|gray|colou?r|dye|brunette|blonde|black|brown|highlights?)\b/i.test(asked)){
      if(!useGenerativeEngine()){ status(i18n.t('A new hairstyle needs a generative engine: the Creative Studio extension with an image model or AI server, or Gemini. Fuller hair, strands, grey and colour are in Skin & hair retouch.')); return; }
    } else {
      if(isPortraitRetouchRequest(asked)){ await portraitStudio(); return; }
      if(isClothingColorRequest(asked)){ await clothingColour(); return; }
    }
    if(isBackgroundRemovalRequest($('#ap-prompt').value)){ await removeBackground(); return; }
    if(isBackgroundBlurRequest($('#ap-prompt').value)){ await blurBackground(); return; }
    if(isObjectRemovalRequest($('#ap-prompt').value)){ await removeObject(); return; }
    lock(true);
    const mode = $('[data-provider]').value;
    const isGen = (mode === 'generate' || mode === 'gemini-edit');
    status(isGen
      ? (mode === 'gemini-edit' ? i18n.t('Generating preview with Google Gemini…') : (capabilities.image_provider === 'ai-server' ? i18n.t('Generating preview on the AI server…') : i18n.t('Generating preview with local model…')))
      : (mode === 'gemini-plan' ? i18n.t('Planning edits with Google Gemini…') : i18n.t('Planning edits…')));
    try {
      if(isGen) {
        const side = mode === 'gemini-edit' ? 1024 : (capabilities.image_edit_max_side || 512);
        const options = {quality: $('[data-generation-quality]').value, fidelity: $('[data-generation-fidelity]').value, negative_prompt: $('[data-generation-negative]').value, seed: Number($('[data-generation-seed]').value)};
        const serverJob = mode !== 'gemini-edit' && (capabilities.server_jobs || []).includes('edit');
        const provider = mode === 'gemini-edit' ? 'gemini' : undefined;
        const blob = await service.generateEdit($('#ap-prompt').value, bitmap, history.current, controller.signal, side, options,
          {serverJob, provider, onStatus: state => { if(!closed) status(describeServerJob(state)); }});
        if(closed) return; clearGenerated(); generatedBlob = blob; generatedURL = URL.createObjectURL(blob); generatedUnsaved = true; $('.ap-generated img').src = generatedURL; $('.ap-generated').hidden = false;
        $('[data-generated-note]').textContent = i18n.t('{quality} quality · {fidelity} fidelity · seed {seed}. Up to {side}px per side.', {quality: optionName('[data-generation-quality]'), fidelity: optionName('[data-generation-fidelity]'), seed: options.seed, side});
        status(i18n.t('Generated preview ready.'));
      } else {
        const provider = mode === 'gemini-plan' ? 'gemini' : mode;
        const plan = await service.planEdit($('#ap-prompt').value, history.current, analysis, provider, controller.signal);
        if(closed) return; pendingPlan = plan;
        $('[data-plan-summary]').textContent = `${i18n.t(plan.provider)}: ${i18n.t(plan.summary)}`; $('[data-plan-changes]').replaceChildren();
        const labels = {exposure:i18n.t('Exposure'),contrast:i18n.t('Contrast'),saturation:i18n.t('Saturation'),vibrance:i18n.t('Vibrance'),warmth:i18n.t('White balance'),sharpness:i18n.t('Sharpness'),noise:i18n.t('Noise reduction'),shadows:i18n.t('Shadows'),highlights:i18n.t('Highlights'),clarity:i18n.t('Clarity'),dehaze:i18n.t('Dehaze'),vignette:i18n.t('Vignette'),angle:i18n.t('Straighten'),crop:i18n.t('Crop framing')};
        for(const [key, value] of Object.entries(plan.patch)) {
          const li = document.createElement('li'); li.textContent = `${labels[key]||key}: ${history.current[key]} → ${value}`; $('[data-plan-changes]').append(li);
        }
        $('.ap-plan').hidden = false; status(i18n.t('Plan ready. Review proposed changes.'));
      }
    } catch(error){ if(!closed) status(errorText(error, i18n.t)); }
    finally { if(!closed) lock(false); }
  };

  $('[data-apply-plan]').onclick = () => { if(pendingPlan && !busy) apply(pendingPlan.patch); };
  $('[data-download-generated]').onclick = () => {
    if(generatedURL){ const a = document.createElement('a'); a.href = generatedURL; a.download = 'ninaivu-ai-generated.png'; a.click(); generatedUnsaved = false; }
  };
  $('[data-discard-generated]').onclick = clearGenerated;
  $('[data-save-generated]').onclick = () => saveResult(generatedBlob, () => { generatedUnsaved = false; });

  let geminiSuggestions = null;
  $('[data-gemini-analyze]').onclick = async () => {
    if(busy || !bitmap) return;
    lock(true); status(i18n.t('Analyzing photograph with Google Gemini…'));
    try {
      const res = await service.geminiAnalyze(bitmap, history.current, controller.signal);
      if(closed) return;
      $('[data-gemini-caption]').textContent = res.caption || '';
      $('[data-gemini-critique]').textContent = res.critique ? i18n.t('Critique: {critique}', {critique: res.critique}) : '';
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
      status(i18n.t('Gemini analysis complete.'));
    } catch(err) {
      if(!closed) status(errorText(err, i18n.t));
    } finally {
      if(!closed) lock(false);
    }
  };
  $('[data-gemini-apply-suggestions]').onclick = () => {
    if(geminiSuggestions && !busy) {
      apply(geminiSuggestions);
      status(i18n.t('Applied Gemini suggested adjustments.'));
    }
  };

  $('[data-export]').onclick = async () => {
    if(busy || !bitmap || !previewReady) return; lock(true); status(i18n.t('Preparing full-resolution export…'));
    try {
      const blob = await service.applyAdjustments(bitmap, history.current, {maxSide: Infinity, type: $('[data-format]').value});
      if(closed) return;
      const url = URL.createObjectURL(blob), a = document.createElement('a'); a.href = url;
      a.download = `ninaivu-edited.${({'image/png':'png','image/jpeg':'jpg','image/webp':'webp'})[blob.type]||'png'}`;
      a.click(); setTimeout(() => URL.revokeObjectURL(url), 30000); savedState = JSON.stringify(history.current);
      status(i18n.t('Download ready. Metadata stripped.'));
    } catch(error){ status(errorText(error, i18n.t)); }
    finally { if(!closed) lock(false); }
  };

  $('[data-save]').onclick = async () => {
    if(busy || !sourceId || !canSave || !previewReady) return;
    saving = true; lock(true); status(i18n.t('Saving new library copy…'));
    try {
      const blob = await service.applyAdjustments(bitmap, history.current, {maxSide: Infinity, type: 'image/png'});
      const copy = await saveLibraryCopy(sourceId, blob);
      savedState = JSON.stringify(history.current); saving = false;
      // Nothing to open while it is waiting to be reviewed: say so and stay put
      // rather than closing onto a library that does not have it yet.
      if(copy.pending){ status(i18n.t(copy.message)); return; }
      if(generatedUnsaved || backgroundUnsaved) status(i18n.t('Copy saved. Download separate AI/background results before closing.'));
      else dialog.close();
      onSaved?.(copy);
    } catch(error){ status(errorText(error, i18n.t)); }
    finally { saving = false; if(!closed) lock(false); }
  };

  function requestClose() {
    if(saving){ status(i18n.t('Please wait while your copy is saving.')); return; }
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
        optGen.textContent = i18n.t('Google Gemini · generative image editing (sends the photo to Google)');
        $('[data-provider]').append(optGen);
        const optPlan = document.createElement('option');
        optPlan.value = 'gemini-plan';
        optPlan.textContent = i18n.t('Google Gemini · multimodal AI plan (sends the photo to Google)');
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
    lock(true); status(i18n.t('Opening photograph…'));
    (async () => {
      try {
        const url = new URL(item.view || item.src, location.href);
        if(url.origin !== location.origin) throw new Error(i18n.t('This photo must come from your Ninaivu library.'));
        const response = await fetch(url, {signal: controller.signal, credentials: 'same-origin'});
        if(!response.ok) throw new Error(i18n.t('Photo could not be loaded.'));
        const blob = await response.blob(); if(closed) return;
        lock(false); await load(new File([blob], item.name || i18n.t('Current photo'), {type: blob.type}), item.rotation || 0, item.id);
      } catch(error){ if(!closed){ lock(false); status(errorText(error, i18n.t)); } }
    })();
  }
}
