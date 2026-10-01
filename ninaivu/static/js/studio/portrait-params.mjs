/**
 * What can be asked of a face, and how those requests are kept.
 *
 * A request is a number for each thing the tools can do — 0 to 100, or −100 to
 * 100 where it runs both ways. There is one set for *everyone* and, for any
 * person who needs something different, a set of their own: the brighter face
 * at the back, the grandmother whose hair is white. A face's own number
 * replaces the shared one for that face alone; it does not add to it, which is
 * what keeps "everyone: soften 30, Amma: soften 60" meaning exactly that.
 *
 * None of these lightens, whitens or "fairs" a complexion, and no setting here
 * can. Face light is an exposure change for a face that is in shadow, applied to
 * skin of any colour by the same factor, so a dark face stays as dark as it is.
 * Tone turns the colour of a cast back towards skin and keeps its lightness and
 * its depth. The engine that reads these has no other way to change skin.
 */

/** Skin: the order they are shown in is the order they are most often needed. */
export const SKIN_KEYS = ['faceLight', 'tone', 'balance', 'shine', 'even', 'underEye', 'smooth', 'richness'];

/** Hair and beard. */
export const HAIR_KEYS = ['hairDetail', 'hairFill', 'hairGrow', 'greyCover', 'hairAmount'];

export const KEYS = [...SKIN_KEYS, ...HAIR_KEYS];

/** The numbers that run both ways; every other one is 0 to 100. */
export const SIGNED = new Set(['tone']);

/** What a slider reaches at the far end, per thing, for the words that sit under it. */
export const LIMITS = {
  faceLight: 1.5,        // stops of light
  tone: 12,              // degrees of hue turned, either way
  balance: 14,           // units of lightness taken off the lit side
  shine: 1,
  even: 1,
  underEye: 1,
  smooth: 1,
  richness: 0.35,        // share of extra colour
  hairDetail: 1,
  hairFill: 1,
  hairGrow: 1,
  greyCover: 1,
  hairAmount: 1,
};

/** Natural hair colours to choose from, as the colours a camera records. Names are the panel's. */
export const HAIR_COLOURS = [
  { key: 'Black', hex: '#1c1714' },
  { key: 'Dark brown', hex: '#33231b' },
  { key: 'Brown', hex: '#4e3424' },
  { key: 'Chestnut', hex: '#6b4029' },
  { key: 'Henna', hex: '#7a3b22' },
  { key: 'Burgundy', hex: '#4d1f2b' },
];

/** The colour grey is covered with when the person has not picked one and the hair has no darker strands to borrow from. */
export const NATURAL_DARK = '#2a1f19';

export const blank = () => ({ all: {}, faces: {}, beard: true, hairColor: null });

/** A deep copy, because history snapshots must not share the objects the sliders keep writing to. */
export const copy = (portrait) => JSON.parse(JSON.stringify(portrait || blank()));

/** The settings for one face: the shared ones, with that face's own laid over them. */
export function effective(portrait, id) {
  const own = (portrait.faces && portrait.faces[id]) || {};
  const out = {};
  for (const key of KEYS) {
    const value = key in own ? own[key] : portrait.all[key];
    out[key] = Number.isFinite(value) ? value : 0;
  }
  out.hairColor = own.hairColor !== undefined ? own.hairColor : portrait.hairColor;
  out.beard = portrait.beard !== false;
  return out;
}

export const asksForSkin = (p) => SKIN_KEYS.some((key) => p[key] !== 0);
export const asksForHair = (p) => HAIR_KEYS.some((key) => p[key] !== 0);

/** Has anything been asked of anybody? */
export function isNeutral(portrait) {
  if (!portrait) return true;
  const any = (values) => KEYS.some((key) => Number.isFinite(values[key]) && values[key] !== 0);
  return !any(portrait.all || {}) && !Object.values(portrait.faces || {}).some(any);
}

const clamp = (key, value) => {
  const low = SIGNED.has(key) ? -100 : 0;
  return Math.max(low, Math.min(100, Math.round(value)));
};

/**
 * Give every face what the measurements said it needs, and only that.
 *
 * `faces` is the analysis's list; each carries `suggest`, numbers for the
 * things that stood out. They are written as that face's *own* settings, on top
 * of whatever is shared, so that somebody's shadowed face is lifted without
 * lifting everybody's. A face with nothing that stood out is left as it is.
 * Returns how many faces were given something.
 */
export function applySuggestions(portrait, faces) {
  let given = 0;
  for (const face of faces || []) {
    if (!face || !face.usable) continue;
    const own = { ...(portrait.faces[face.id] || {}) };
    let any = false;
    for (const [key, value] of Object.entries(face.suggest || {})) {
      if (!KEYS.includes(key) || !Number.isFinite(value)) continue;
      own[key] = clamp(key, value);
      any = true;
    }
    if (any) { portrait.faces[face.id] = own; given++; }
  }
  return given;
}

/**
 * Look younger: what the tools here can honestly do about age, at full strength.
 * Smoother and more even skin, calmer shine, brighter under-eyes, a little of the
 * skin's own colour back; grey covered, strands brought out, thin hair filled.
 * Nothing on this list lightens skin (there is no face light here, and face light
 * is the one tool that could), turns its colour, or redraws a feature: the person
 * stays exactly who they are, which is the whole promise of these tools.
 */
export const YOUNGER = {
  smooth: 55, even: 45, shine: 40, underEye: 60, richness: 15,
  greyCover: 90, hairDetail: 25, hairFill: 40,
};

/**
 * Give one person, or everyone, the younger look at *strength* (0 to 1), as their
 * own slider values — so every one of them is still a slider afterwards. A tool
 * already set higher by hand is left where it is.
 */
export function applyYounger(portrait, id = 'all', strength = 0.6) {
  const k = Math.max(0, Math.min(1, Number(strength) || 0));
  const target = id === 'all' ? portrait.all : (portrait.faces[id] ||= {});
  for (const [key, full] of Object.entries(YOUNGER)) {
    const value = Math.round(full * k);
    const current = Number.isFinite(target[key]) ? target[key] : 0;
    target[key] = Math.max(current, value);
  }
  return portrait;
}

/** The keys that have a value for one face, own or shared, that differs from nothing. */
export const active = (portrait, id) => {
  const p = effective(portrait, id);
  return KEYS.filter((key) => p[key] !== 0);
};
