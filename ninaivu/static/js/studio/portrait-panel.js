/**
 * The Skin and Hair panels: the people in the photograph, and what each needs.
 *
 * One strip of faces across the top — Everyone first — and under it the sliders
 * for whoever is chosen. Move a slider with Everyone chosen and it moves for
 * everyone who has not been given a setting of their own; choose one person and
 * it moves for them alone, and a dot beside the slider says it is theirs. That is
 * the whole model, and it is the one a family photograph needs: the grandmother
 * in the window light and the child in the shade are in the same picture and
 * want different things.
 *
 * "Improve faces" reads what the server measured about each person — how far
 * into shadow, how big a colour cast, how shiny — and sets each person's own
 * sliders to match, for the things that stood out and nothing else. It is one
 * step of history, and every slider it moved is still a slider.
 *
 * Two promises the panel states in plain words, because they are the ones people
 * worry about: a bindi, sindoor or sacred ash is left exactly as it is, and
 * nothing here makes skin lighter. The second is a property of the tools, not of
 * the wording: see `portrait-params.mjs`.
 */

import * as i18n from '../i18n.js';
import { SKIN_KEYS, HAIR_KEYS, SIGNED, HAIR_COLOURS, effective, KEYS } from './portrait-params.mjs';

const LABELS = {
  faceLight: i18n.key('Face light'),
  tone: i18n.key('Skin tone'),
  balance: i18n.key('Even out the light'),
  shine: i18n.key('Calm the shine'),
  even: i18n.key('Even out the tone'),
  underEye: i18n.key('Brighten under-eyes'),
  smooth: i18n.key('Soften'),
  richness: i18n.key('Richness'),
  hairDetail: i18n.key('Strands & shine'),
  greyCover: i18n.key('Cover grey'),
  hairAmount: i18n.key('Hair colour'),
};

/** One line on what each slider does, for the tooltip — and, by saying what it keeps, what it does not do. */
const HINTS = {
  faceLight: i18n.key('Lifts a face that is in shadow. Skin of every colour is lifted by the same amount.'),
  tone: i18n.key('Turns a colour cast on the skin back towards natural. Lightness is not changed.'),
  balance: i18n.key('Evens out a face that is brighter on one side.'),
  shine: i18n.key('Eases shiny patches back towards the skin around them. Jewellery is left alone.'),
  even: i18n.key('Evens out patches of colour, without flattening the face.'),
  underEye: i18n.key('Lifts the dark under the eyes towards the cheek beside it.'),
  smooth: i18n.key('Softens blemishes and keeps the skin: pores, folds and moles stay.'),
  richness: i18n.key("A little more of the skin's own colour."),
  hairDetail: i18n.key('Brings out strands and shine; black stays black.'),
  greyCover: i18n.key('Darkens grey and white hair, leaving the rest as it is.'),
  hairAmount: i18n.key('Moves the hair towards the colour chosen above.'),
};

/** What a person sees when something stood out about a face. */
const NEEDS = {
  faceLight: i18n.key('in shadow'),
  tone: i18n.key('a colour cast'),
  balance: i18n.key('lit from one side'),
  shine: i18n.key('shiny'),
  even: i18n.key('patchy tone'),
  underEye: i18n.key('dark under the eyes'),
  richness: i18n.key('a little pale'),
  hairDetail: i18n.key('hair that could show more detail'),
  greyCover: i18n.key('grey hair'),
};

const COLOUR_NAMES = {
  Black: i18n.key('Black'), 'Dark brown': i18n.key('Dark brown'), Brown: i18n.key('Brown'),
  Chestnut: i18n.key('Chestnut'), Henna: i18n.key('Henna'), Burgundy: i18n.key('Burgundy'),
};

const escape = (text) => String(text).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

export class PortraitPanel {
  /**
   * @param session  a PortraitSession
   * @param kind     'skin' or 'hair'
   * @param host     what the studio around it provides:
   *                   `picture()`        a canvas showing the photograph, for the faces' pictures
   *                   `remember()`       take a history step before a change
   *                   `changed()`        a setting changed: draw again
   *                   `refine(mode)`     start or stop painting by hand: 'add', 'erase', or null
   *                   `select(on)`       show or hide what the tools would reach
   *                   `say(text)`        one line for the status bar
   */
  constructor({ session, kind, host }) {
    this.session = session;
    this.kind = kind;
    this.host = host;
    this.element = document.createElement('section');
    this.element.className = `pp pp-${kind}`;
    this.brush = { mode: null, size: 55, edge: true };
    this.thumbKey = '';
    this.build();
    session.watch(() => this.refresh());
  }

  get selected() { return this.session.selected || 'all'; }
  set selected(value) { this.session.selected = value; }

  keys() { return this.kind === 'hair' ? HAIR_KEYS : SKIN_KEYS; }

  // -- markup --------------------------------------------------------------

  build() {
    const sliders = this.keys().filter((key) => key !== 'hairAmount').map((key) => this.slider(key)).join('');
    const hair = this.kind === 'hair';
    this.element.innerHTML = `
      <div class="pp-faces" role="radiogroup" aria-label="${escape(i18n.t('People in this photograph'))}"></div>
      <p class="pp-state" role="status" aria-live="polite"></p>
      <p class="pp-needs" hidden></p>
      <button type="button" class="pp-improve" data-pp="improve" hidden>✦ ${escape(i18n.t('Improve faces'))}</button>
      <div class="pp-sliders">
        ${sliders}
        ${hair ? `
        ${this.colours()}
        ${this.slider('hairAmount')}
        <label class="pp-check"><input type="checkbox" data-pp="beard" checked> ${escape(i18n.t('Include beard and moustache'))}</label>` : ''}
      </div>
      <button type="button" class="pp-quiet" data-pp="reset-face" hidden>${escape(i18n.t('Use the settings for everyone'))}</button>
      ${this.host.refine ? this.refineMarkup() : ''}

      <details class="pp-note"><summary>${escape(i18n.t('How these work'))}</summary>
        <div>
          <p>${escape(i18n.t('A bindi, kumkum, sindoor, sacred ash or sandal paste is left exactly as it is. Nothing here smooths, lightens, recolours or blurs it.'))}</p>
          <p>${escape(i18n.t('Nothing here makes skin lighter or fairer. Face light is exposure for a face that is in shadow: skin of every colour is lifted by the same amount, so a dark face stays dark.'))}</p>
          <p>${escape(i18n.t('Each person is judged against their own skin. A setting chosen for one person is theirs alone.'))}</p>
        </div>
      </details>`;
    this.wire();
    this.refresh();
  }

  /** Painting by hand where the tools missed, or erred — only in a studio that has a picture to paint on. */
  refineMarkup() {
    return `
      <div class="pp-refine">
        <p class="pp-label">${escape(i18n.t('Fix the selection'))}</p>
        <div class="pp-seg" role="group" aria-label="${escape(i18n.t('Fix the selection'))}">
          <button type="button" data-pp="refine" data-mode="add" aria-pressed="false">${escape(i18n.t('Add'))}</button>
          <button type="button" data-pp="refine" data-mode="erase" aria-pressed="false">${escape(i18n.t('Remove'))}</button>
        </div>
        <div class="pp-refine-more" hidden>
          <label class="pp-slider pp-size"><span class="pp-name">${escape(i18n.t('Brush size'))}</span>
            <input type="range" data-pp="size" min="8" max="220" value="${this.brush.size}"></label>
          <label class="pp-check"><input type="checkbox" data-pp="edge" checked> ${escape(i18n.t('Follow colours near the start of a stroke'))}</label>
          <button type="button" class="pp-quiet" data-pp="clear-paint">${escape(i18n.t('Clear my painting'))}</button>
        </div>
        <label class="pp-check"><input type="checkbox" data-pp="show"> ${escape(i18n.t('Show what is selected'))}</label>
      </div>`;
  }

  slider(key) {
    const min = SIGNED.has(key) ? -100 : 0;
    const both = SIGNED.has(key) ? ` data-both="1"` : '';
    return `<label class="pp-slider" data-key="${key}"${both} title="${escape(i18n.t(HINTS[key]))}">
        <span class="pp-name">${escape(i18n.t(LABELS[key]))}<i class="pp-own" title="${escape(i18n.t("This person's own setting"))}" hidden>●</i></span>
        <button type="button" class="pp-clear" data-pp="clear" title="${escape(i18n.t('Use the setting for everyone'))}" aria-label="${escape(i18n.t('Use the setting for everyone'))}" hidden>↺</button>
        <output data-pp="value">0</output>
        <input type="range" data-pp="slider" min="${min}" max="100" value="0" aria-label="${escape(i18n.t(LABELS[key]))}">
        ${SIGNED.has(key) ? `<span class="pp-ends" aria-hidden="true"><i>${escape(i18n.t('redder'))}</i><i>${escape(i18n.t('yellower'))}</i></span>` : ''}
      </label>`;
  }

  colours() {
    const swatches = HAIR_COLOURS.map((c) => `<button type="button" class="pp-swatch" data-pp="colour" data-hex="${c.hex}"
        style="--swatch:${c.hex}" title="${escape(i18n.t(COLOUR_NAMES[c.key]))}" aria-label="${escape(i18n.t(COLOUR_NAMES[c.key]))}" aria-pressed="false"></button>`).join('');
    return `<div class="pp-colours" role="group" aria-label="${escape(i18n.t('Hair colour'))}">
        ${swatches}
        <label class="pp-swatch pp-custom" title="${escape(i18n.t('Pick a colour'))}"><input type="color" data-pp="custom" value="#4e3424" aria-label="${escape(i18n.t('Pick a colour'))}"></label>
      </div>`;
  }

  // -- wiring --------------------------------------------------------------

  wire() {
    const q = (s) => this.element.querySelector(s);
    q('[data-pp="improve"]').onclick = () => this.improve();
    q('[data-pp="reset-face"]').onclick = () => {
      if (this.selected === 'all') return;
      this.host.remember();
      delete this.session.portrait.faces[this.selected];
      this.session.bump(); this.session.touch(); this.host.changed();
    };

    this.element.querySelectorAll('.pp-slider[data-key]').forEach((row) => {
      const key = row.dataset.key, input = row.querySelector('[data-pp="slider"]');
      let started = false;
      input.oninput = () => {
        if (!started) { this.host.remember(); started = true; }
        this.set(key, Number(input.value));
        row.querySelector('[data-pp="value"]').value = input.value;
        this.host.changed();
      };
      input.onchange = () => { started = false; };
      row.querySelector('[data-pp="clear"]').onclick = () => {
        if (this.selected === 'all') return;
        this.host.remember();
        const own = this.session.portrait.faces[this.selected];
        if (own) { delete own[key]; if (!Object.keys(own).length) delete this.session.portrait.faces[this.selected]; }
        this.session.bump(); this.session.touch(); this.host.changed();
      };
    });

    this.element.querySelectorAll('[data-pp="colour"]').forEach((button) => {
      button.onclick = () => this.pick(button.dataset.hex);
    });
    const custom = q('[data-pp="custom"]');
    if (custom) custom.onchange = () => this.pick(custom.value);
    const beard = q('[data-pp="beard"]');
    if (beard) beard.onchange = () => {
      this.host.remember();
      this.session.portrait.beard = beard.checked;
      this.session.bump(); this.session.touch(); this.host.changed();
    };

    if (!this.host.refine) return;
    this.element.querySelectorAll('[data-pp="refine"]').forEach((button) => {
      button.onclick = () => this.refine(this.brush.mode === button.dataset.mode ? null : button.dataset.mode);
    });
    q('[data-pp="size"]').oninput = (e) => { this.brush.size = Number(e.target.value); };
    q('[data-pp="edge"]').onchange = (e) => { this.brush.edge = e.target.checked; };
    q('[data-pp="clear-paint"]').onclick = () => {
      this.host.remember();
      const names = this.kind === 'hair' ? ['hairAdd', 'hairErase'] : ['skinAdd', 'skinErase'];
      for (const name of names) this.session.paint[name].clear();
      this.session.bump(); this.session.touch(); this.host.changed();
    };
    q('[data-pp="show"]').onchange = (e) => this.host.select(e.target.checked);
  }

  /** Put the value where it belongs: with everyone chosen, in the shared settings; with one person chosen, in theirs. */
  set(key, value) {
    const portrait = this.session.portrait;
    if (this.selected === 'all') portrait.all[key] = value;
    else (portrait.faces[this.selected] ||= {})[key] = value;
    this.session.bump();
    this.refreshOwn();
  }

  pick(hex) {
    this.host.remember();
    const portrait = this.session.portrait;
    const target = this.selected === 'all' ? portrait : (portrait.faces[this.selected] ||= {});
    target.hairColor = hex;
    // A colour chosen and nothing happening is a button that looks broken.
    const current = effective(portrait, this.selected === 'all' ? 0 : this.selected).hairAmount;
    if (!current) this.set('hairAmount', 50);
    this.session.bump();
    this.session.touch();
    this.host.changed();
  }

  async improve() {
    const session = this.session;
    if (session.state !== 'ready') await session.analyse();
    if (session.state !== 'ready') return;
    this.host.remember();
    const given = session.improve();
    this.host.changed();
    this.host.say(given === 1
      ? i18n.t('Improved one person by what stood out about them. Nothing else was touched.')
      : given
        ? i18n.t('Improved {count} people, each by what stood out about them. Nothing else was touched.', { count: given })
        : i18n.t('Nothing stood out about anybody in this photograph. They look fine as they are.'));
  }

  refine(mode) {
    this.brush.mode = mode;
    this.element.querySelectorAll('[data-pp="refine"]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.mode === mode)));
    this.element.querySelector('.pp-refine-more').hidden = !mode;
    if (mode) this.element.querySelector('[data-pp="show"]').checked = true;
    this.host.refine(mode, this.kind);
  }

  // -- drawing the state ---------------------------------------------------

  /** Everything that depends on the session: the strip, the message, the sliders. */
  refresh() {
    const session = this.session, q = (s) => this.element.querySelector(s);
    const state = session.state;
    const faces = session.faces().filter((f) => f.inside);
    if (this.selected !== 'all' && !faces.some((f) => f.id === this.selected)) this.selected = 'all';

    const message = {
      idle: '',
      finding: i18n.t('Finding the faces…'),
      none: i18n.t('No face was found here. You can still paint skin or hair by hand.'),
      unavailable: i18n.t('Finding faces needs the face model. Download it under AI models, or paint the area by hand.'),
      failed: i18n.t('Could not look for faces just now. You can still paint by hand.'),
      ready: '',
    }[state] || '';
    const status = q('.pp-state');
    status.textContent = message;
    status.hidden = !message;
    status.dataset.state = state;
    if (state === 'none' || state === 'failed') {
      status.insertAdjacentHTML('beforeend', ` <button type="button" class="pp-link" data-pp="retry">${escape(i18n.t('Try again'))}</button>`);
      q('[data-pp="retry"]').onclick = () => session.analyse(true);
    }

    q('[data-pp="improve"]').hidden = !(state === 'ready' && faces.length);
    this.drawStrip(faces);
    this.drawNeeds(faces);
    this.refreshSliders();
    this.refreshOwn();
  }

  drawStrip(faces) {
    const strip = this.element.querySelector('.pp-faces');
    const signature = `${this.session.generation}|${this.session.geometry.key()}|${faces.map((f) => f.id).join(',')}`;
    strip.hidden = !faces.length && this.session.state !== 'ready';
    const rebuild = signature !== this.thumbKey;
    if (rebuild) {
      this.thumbKey = signature;
      strip.innerHTML = '';
      const everyone = document.createElement('button');
      everyone.type = 'button'; everyone.className = 'pp-face pp-everyone'; everyone.dataset.face = 'all';
      everyone.setAttribute('role', 'radio');
      everyone.innerHTML = `<span class="pp-all" aria-hidden="true">${faces.length > 1 ? '👥' : '🙂'}</span><small>${escape(i18n.t('Everyone'))}</small>`;
      strip.append(everyone);
      faces.forEach((face, index) => {
        const button = document.createElement('button');
        button.type = 'button'; button.className = 'pp-face'; button.dataset.face = String(face.id);
        button.setAttribute('role', 'radio');
        const picture = this.session.thumb(face.id, this.host.picture(), 44);
        picture.setAttribute('aria-hidden', 'true');
        button.append(picture);
        button.insertAdjacentHTML('beforeend', `<small>${index + 1}</small><i class="pp-own" hidden>●</i>`);
        button.title = i18n.t('Person {number}', { number: index + 1 });
        strip.append(button);
      });
      strip.querySelectorAll('.pp-face').forEach((button) => {
        button.onclick = () => {
          this.selected = button.dataset.face === 'all' ? 'all' : Number(button.dataset.face);
          this.session.touch();               // every panel, and the overlay, follow
        };
      });
      // A row of radio buttons is walked with the arrow keys, and the one landed on is the one chosen.
      strip.onkeydown = (e) => {
        const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
        if (!step) return;
        const buttons = [...strip.querySelectorAll('.pp-face')];
        const here = buttons.indexOf(document.activeElement);
        if (here < 0) return;
        e.preventDefault();
        const next = buttons[(here + step + buttons.length) % buttons.length];
        next.focus();
        next.click();
      };
    }
    strip.querySelectorAll('.pp-face').forEach((button) => {
      const id = button.dataset.face === 'all' ? 'all' : Number(button.dataset.face);
      const on = id === this.selected;
      button.setAttribute('aria-checked', String(on));
      button.classList.toggle('pp-on', on);
      if (id !== 'all') button.querySelector('.pp-own').hidden = !this.hasOwn(id);
    });
  }

  hasOwn(id) {
    const own = this.session.portrait.faces[id];
    return !!own && KEYS.some((key) => Number.isFinite(own[key]) && own[key] !== 0);
  }

  /** "Needs: in shadow, a colour cast" — what the measurements found, in the words a person would use. */
  drawNeeds(faces) {
    const line = this.element.querySelector('.pp-needs');
    const face = this.selected === 'all' ? null : faces.find((f) => f.id === this.selected);
    // Hair that was not found cannot be retouched, and a slider that does nothing is a
    // slider that looks broken: say why, and what to do about it.
    if (this.kind === 'hair' && faces.length && (face ? !(face.hair && face.hair.found) : !faces.some((f) => f.hair && f.hair.found))) {
      line.textContent = this.host.refine
        ? i18n.t('Hair could not be told apart from what is behind it here, so it is not selected. Paint it in to use these tools.')
        : i18n.t('Hair could not be told apart from what is behind it here, so it is not selected. The Photo Studio can paint it in.');
      line.hidden = false;
      return;
    }
    if (!face) { line.hidden = true; return; }
    const wanted = Object.keys(face.suggest || {}).filter((key) => NEEDS[key] && this.keys().includes(key)).map((key) => i18n.t(NEEDS[key]));
    const marks = this.kind === 'skin' && face.marks ? ` ${i18n.t('A mark on the forehead is kept as it is.')}` : '';
    line.textContent = (wanted.length ? i18n.t('Stands out: {list}.', { list: wanted.join(', ') }) : i18n.t('Nothing stands out here.')) + marks;
    line.hidden = false;
  }

  refreshSliders() {
    const portrait = this.session.portrait, id = this.selected === 'all' ? 0 : this.selected;
    const values = effective(portrait, id);
    if (this.selected === 'all') Object.assign(values, Object.fromEntries(KEYS.map((key) => [key, Number(portrait.all[key]) || 0])), { hairColor: portrait.hairColor });
    this.element.querySelectorAll('.pp-slider[data-key]').forEach((row) => {
      const key = row.dataset.key, input = row.querySelector('[data-pp="slider"]');
      input.value = String(values[key] ?? 0);
      row.querySelector('[data-pp="value"]').value = input.value;
    });
    const hex = (values.hairColor || '').toLowerCase();
    this.element.querySelectorAll('[data-pp="colour"]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.hex === hex)));
    const custom = this.element.querySelector('[data-pp="custom"]');
    if (custom && /^#[0-9a-f]{6}$/.test(hex)) custom.value = hex;
    const beard = this.element.querySelector('[data-pp="beard"]');
    if (beard) beard.checked = portrait.beard !== false;
  }

  /** The dots: which sliders, for the person chosen, are their own. */
  refreshOwn() {
    const portrait = this.session.portrait, id = this.selected;
    const own = id === 'all' ? null : portrait.faces[id] || {};
    this.element.querySelectorAll('.pp-slider[data-key]').forEach((row) => {
      const mine = !!own && Number.isFinite(own[row.dataset.key]);
      row.querySelector('.pp-own').hidden = !mine;
      row.querySelector('[data-pp="clear"]').hidden = !mine;
    });
    this.element.querySelector('[data-pp="reset-face"]').hidden = !(own && Object.keys(own).length);
    this.element.querySelectorAll('.pp-face:not(.pp-everyone) .pp-own').forEach((dot) => {
      dot.hidden = !this.hasOwn(Number(dot.closest('.pp-face').dataset.face));
    });
  }
}
