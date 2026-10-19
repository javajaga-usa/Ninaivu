/**
 * The living photo frame: the library drifting by, full screen, on a tablet
 * on the wall or a television's browser (ninaivu/media/frame.py picks what).
 *
 * Opened from "Photo frame" in the top bar, or by opening Ninaivu at
 * `/#frame`, which is what a tablet's home-screen bookmark should point at.
 * Each photograph comes in slowly over the last, drifts and zooms a little
 * towards the faces in it (a Ken Burns move, never a jump), and says what
 * the family would say looking at it: "14 years ago today", where, who, and
 * the first words of any story told about it.
 *
 * A frame runs for days, so it keeps the screen awake where the browser
 * allows, asks for more photographs before it runs out, carries on after a
 * dropped connection, and dims itself at night. The controls appear on a
 * touch or a mouse move and fade again; the keyboard works for a TV remote
 * that sends arrow keys.
 *
 * Built here rather than written into index.html, as the photo books are:
 * most visits never open it.
 */

import * as i18n from './i18n.js';

const INTERVALS = [
  [15, i18n.key('15 seconds')],
  [30, i18n.key('30 seconds')],
  [60, i18n.key('1 minute')],
  [300, i18n.key('5 minutes')],
];
/** Seconds one photograph takes to come in over the last. */
const FADE = 2.4;
/** What the frame remembers having shown, so a batch brings new ones. */
const HISTORY = 200;
/** Hours the frame dims through (local time). */
const NIGHT = [22, 6];

let root = null;
let layers = [];
let front = 0;
let queue = [];
let history = [];
let current = null;
let timer = null;
let clockTimer = null;
let idleTimer = null;
let paused = false;
let fetching = null;
let wakeLock = null;
let failures = 0;
let seed = 0;

function remembered(name, fallback) {
  try { return localStorage.getItem(`ninaivu.frame.${name}`) ?? fallback; } catch { return fallback; }
}

function remember(name, value) {
  try { localStorage.setItem(`ninaivu.frame.${name}`, String(value)); } catch { /* private window */ }
}

function interval() {
  const seconds = Number(remembered('interval', 30));
  return INTERVALS.some(([s]) => s === seconds) ? seconds : 30;
}

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k in node) node[k] = v;
    else node.setAttribute(k, v);
  }
  node.append(...children.filter(Boolean));
  return node;
}

function iconButton(label, path, onClick) {
  const button = el('button', { class: 'frame-btn', type: 'button', 'aria-label': i18n.t(label), title: i18n.t(label) });
  button.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">${path}</svg>`;
  button.onclick = (event) => { event.stopPropagation(); onClick(); };
  return button;
}

const ICONS = {
  close: '<path d="M18 6 6 18M6 6l12 12"/>',
  pause: '<path d="M8 5v14M16 5v14"/>',
  play: '<path d="m7 4 13 8-13 8Z"/>',
  next: '<path d="m9 5 7 7-7 7"/>',
  back: '<path d="m15 5-7 7 7 7"/>',
  full: '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>',
};

function build() {
  if (root) return root;
  root = el('div', { class: 'frame', id: 'photo-frame', hidden: true, role: 'dialog',
                     'aria-modal': 'true', 'aria-label': i18n.t('Photo frame'), tabindex: '-1' });
  layers = [el('figure', { class: 'frame-layer' }), el('figure', { class: 'frame-layer' })];
  const clock = el('div', { class: 'frame-clock', 'aria-hidden': 'true' },
                   el('b', { class: 'frame-time' }), el('span', { class: 'frame-date' }));
  const caption = el('div', { class: 'frame-caption', 'aria-live': 'polite' },
                     el('div', { class: 'frame-when' }), el('div', { class: 'frame-where' }),
                     el('blockquote', { class: 'frame-story', hidden: true }));
  const pace = el('select', { class: 'frame-pace', 'aria-label': i18n.t('Each photograph for') });
  for (const [seconds, name] of INTERVALS) pace.add(new Option(i18n.t(name), String(seconds)));
  pace.value = String(interval());
  pace.onclick = (event) => event.stopPropagation();
  pace.onchange = () => { remember('interval', pace.value); schedule(); };
  const controls = el('div', { class: 'frame-controls' },
    iconButton(i18n.key('Previous photograph'), ICONS.back, back),
    iconButton(i18n.key('Pause'), ICONS.pause, togglePause),
    iconButton(i18n.key('Next photograph'), ICONS.next, () => advance()),
    pace,
    document.fullscreenEnabled ? iconButton(i18n.key('Full screen'), ICONS.full, toggleFullscreen) : null,
    iconButton(i18n.key('Close the frame'), ICONS.close, close));
  const empty = el('div', { class: 'frame-empty', hidden: true });
  root.append(...layers, el('div', { class: 'frame-shade' }), clock, caption, controls, empty);
  root.addEventListener('pointermove', wake);
  root.addEventListener('pointerdown', wake);
  root.addEventListener('keydown', onKey);
  document.body.appendChild(root);
  return root;
}

function onKey(event) {
  wake();
  if (event.key === 'Escape') close();
  else if (event.key === 'ArrowRight' || event.key === 'PageDown') advance();
  else if (event.key === 'ArrowLeft' || event.key === 'PageUp') back();
  else if (event.key === ' ' || event.key === 'Enter' || event.key === 'MediaPlayPause') {
    if (event.target?.tagName === 'SELECT' || event.target?.tagName === 'BUTTON') return;
    togglePause();
  } else return;
  event.preventDefault();
}

/** Show the controls for a few seconds after a touch or a mouse move. */
function wake() {
  root.classList.add('awake');
  clearTimeout(idleTimer);
  idleTimer = setTimeout(() => root.classList.remove('awake'), 3500);
}

function togglePause() {
  paused = !paused;
  root.classList.toggle('paused', paused);
  const button = root.querySelector('.frame-controls .frame-btn:nth-child(2)');
  const label = paused ? i18n.key('Play') : i18n.key('Pause');
  button.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">${paused ? ICONS.play : ICONS.pause}</svg>`;
  button.setAttribute('aria-label', i18n.t(label));
  button.title = i18n.t(label);
  schedule();
}

function toggleFullscreen() {
  if (document.fullscreenElement) document.exitFullscreen?.();
  else root.requestFullscreen?.().catch(() => {});
}

/* -- what to show ------------------------------------------------------ */

async function more() {
  if (fetching) return fetching;
  const params = new URLSearchParams({ n: '12', seed: String(seed) });
  if (history.length) params.set('shown', history.slice(-HISTORY).join(','));
  fetching = fetch(`/api/frame/next?${params}`, {
    headers: { Accept: 'application/json', 'X-Requested-With': 'fetch' },
  }).then(async (response) => {
    if (!response.ok) throw Object.assign(new Error(response.statusText), { status: response.status });
    const body = await response.json();
    const known = new Set(queue.map((item) => item.id));
    for (const item of body.items || []) if (!known.has(item.id)) queue.push(item);
    failures = 0;
    return body.items?.length || 0;
  }).finally(() => { fetching = null; });
  return fetching;
}

/** The picture, loaded and decoded before it is shown, so it never paints in
 *  halves over the last one. */
function load(item) {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.decoding = 'async';
    image.alt = '';
    image.onload = () => (image.decode ? image.decode().catch(() => {}) : Promise.resolve())
      .then(() => resolve(image));
    image.onerror = reject;
    image.src = item.src;
  });
}

async function advance(step = null) {
  clearTimeout(timer);
  let item = step;
  try {
    if (!item) {
      if (queue.length < 4) more().catch(() => {});
      if (!queue.length) await more();
      item = queue.shift();
    }
    if (!item) {
      showEmpty(i18n.t('There are no photographs for the frame yet.'));
      return;
    }
    const image = await load(item);
    show(item, image);
    failures = 0;
  } catch (error) {
    failures += 1;
    if (error?.status === 404) {
      showEmpty(i18n.t('The photo frame is switched off. An administrator can switch it on in Settings.'));
      return;
    }
    if (error?.status === 401) {
      showEmpty(i18n.t('Sign in on this screen to show the family photographs.'));
      return;
    }
    // A photograph that would not load, or a connection that dropped: try
    // the next one soon, more slowly each time, and never give up for good.
    timer = setTimeout(() => advance(), Math.min(60000, 2000 * failures));
    return;
  }
  schedule();
}

function back() {
  // The one before the one on screen, if the frame still remembers it.
  const index = history.lastIndexOf(current?.id);
  if (index < 1) return;
  const previous = shownItems.get(history[index - 1]);
  if (!previous) return;
  if (current) queue.unshift(current);
  history.splice(index - 1, 2);
  advance(previous);
}

const shownItems = new Map();

function schedule() {
  clearTimeout(timer);
  if (paused || !root || root.hidden) return;
  timer = setTimeout(() => advance(), interval() * 1000);
}

function showEmpty(text) {
  const box = root.querySelector('.frame-empty');
  box.textContent = text;
  box.hidden = false;
  root.classList.add('awake');
}

/* -- one photograph ---------------------------------------------------- */

function show(item, image) {
  root.querySelector('.frame-empty').hidden = true;
  current = item;
  history.push(item.id);
  shownItems.set(item.id, item);
  if (history.length > HISTORY * 2) {
    for (const id of history.splice(0, history.length - HISTORY)) shownItems.delete(id);
  }
  const next = layers[1 - front];
  next.replaceChildren(image);
  if (item.color) next.style.setProperty('--frame-tint', item.color);
  drift(image, item);
  next.classList.add('shown');
  layers[front].classList.remove('shown');
  front = 1 - front;
  caption(item);
}

/**
 * The slow move. A picture that fills the screen the other way round
 * (portrait on a television) is shown whole over a blurred copy of itself,
 * with only a gentle zoom; one that nearly fits is filled and drifts towards
 * the faces in it.
 */
function drift(image, item) {
  const screen = window.innerWidth / Math.max(1, window.innerHeight);
  const picture = item.width && item.height ? item.width / item.height : screen;
  const fills = Math.abs(Math.log(picture / screen)) < 0.45;
  const figure = image.parentElement;
  figure.classList.toggle('whole', !fills);
  figure.style.setProperty('--frame-backdrop', `url("${item.thumb}")`);
  const seconds = interval() + FADE * 2;
  const [fx, fy] = item.focus || [0.5, 0.42];
  // Start a little to the far side of the faces and come in towards them.
  const reach = fills ? 4 : 1.5;
  const from = [(0.5 - fx) * -reach, (0.5 - fy) * -reach];
  const to = [(0.5 - fx) * reach, (0.5 - fy) * reach];
  const zoom = fills ? [1.04, 1.16] : [1, 1.05];
  const outward = Math.random() < 0.3;
  image.style.objectPosition = `${fx * 100}% ${fy * 100}%`;
  image.animate([
    { transform: `translate(${from[0]}%, ${from[1]}%) scale(${outward ? zoom[1] : zoom[0]})` },
    { transform: `translate(${to[0]}%, ${to[1]}%) scale(${outward ? zoom[0] : zoom[1]})` },
  ], { duration: seconds * 1000, easing: 'linear', fill: 'forwards' });
}

function monthYear(item) {
  const key = String(item.date_key || '');
  let when = null;
  if (/^\d{4}-\d{2}/.test(key)) when = new Date(Date.UTC(+key.slice(0, 4), +key.slice(5, 7) - 1, 15));
  else if (item.captured_at) when = new Date(item.captured_at * 1000);
  if (!when) return '';
  try {
    return new Intl.DateTimeFormat(i18n.locale(), { month: 'long', year: 'numeric', timeZone: 'UTC' })
      .format(when);
  } catch {
    return String(item.year || '');
  }
}

function whenText(item) {
  const ago = item.years_ago;
  if (item.on_this_day && ago) {
    return ago === 1 ? i18n.t('A year ago today') : i18n.t('{years} years ago today', { years: ago });
  }
  const month = monthYear(item);
  if (ago == null || ago < 1) return month;
  const years = ago === 1 ? i18n.t('A year ago') : i18n.t('{years} years ago', { years: ago });
  return month ? `${month} · ${years}` : years;
}

function caption(item) {
  const box = root.querySelector('.frame-caption');
  box.classList.remove('in');
  void box.offsetWidth;                               // restart the fade
  box.querySelector('.frame-when').textContent = whenText(item);
  const people = [...(item.people || [])];
  if (item.more_people) people.push(i18n.t('and {count} more', { count: item.more_people }));
  box.querySelector('.frame-where').textContent = [item.place, people.join(', ')].filter(Boolean).join(' · ');
  const quote = box.querySelector('.frame-story');
  const story = item.story;
  // The words in the reader's language where the engine translated them.
  const words = story && (i18n.language() === 'en' && story.english ? story.english : story.text);
  quote.hidden = !words;
  if (words) {
    quote.replaceChildren(el('span', { text: `“${words}”` }),
      story.speaker ? el('cite', { text: `— ${story.speaker}` }) : null);
  }
  box.classList.add('in');
}

/* -- the room ---------------------------------------------------------- */

function tick() {
  const now = new Date();
  const locale = i18n.locale();
  root.querySelector('.frame-time').textContent =
    now.toLocaleTimeString(locale, { hour: 'numeric', minute: '2-digit' });
  root.querySelector('.frame-date').textContent =
    now.toLocaleDateString(locale, { weekday: 'long', day: 'numeric', month: 'long' });
  const hour = now.getHours();
  root.classList.toggle('night', hour >= NIGHT[0] || hour < NIGHT[1]);
}

async function keepAwake() {
  try {
    if (navigator.wakeLock && !wakeLock && document.visibilityState === 'visible') {
      wakeLock = await navigator.wakeLock.request('screen');
      wakeLock.addEventListener('release', () => { wakeLock = null; });
    }
  } catch { /* not allowed here: the screen sleeps as it would */ }
}

function onVisible() {
  if (!root || root.hidden) return;
  if (document.visibilityState === 'visible') {
    keepAwake();
    schedule();
  } else {
    clearTimeout(timer);
  }
}

/** Open the frame. */
export async function openFrame() {
  build();
  if (!root.hidden) return;
  root.hidden = false;
  document.body.style.overflow = 'hidden';
  if (location.hash !== '#frame') history_replace('#frame');
  // A new shuffle each evening, the same one for the evening.
  seed = Number(remembered('seed', 0));
  const day = new Date().toDateString();
  if (!seed || remembered('seed-day', '') !== day) {
    seed = 1 + Math.floor(Math.random() * 2 ** 30);
    remember('seed', seed);
    remember('seed-day', day);
  }
  paused = false;
  root.classList.remove('paused');
  tick();
  clockTimer = setInterval(tick, 15000);
  document.addEventListener('visibilitychange', onVisible);
  keepAwake();
  root.focus();
  wake();
  advance();
}

export function closeFrame() {
  close();
}

function close() {
  if (!root || root.hidden) return;
  clearTimeout(timer);
  clearTimeout(idleTimer);
  clearInterval(clockTimer);
  document.removeEventListener('visibilitychange', onVisible);
  wakeLock?.release?.().catch(() => {});
  wakeLock = null;
  if (document.fullscreenElement) document.exitFullscreen?.().catch(() => {});
  for (const layer of layers) { layer.replaceChildren(); layer.classList.remove('shown'); }
  queue = [];
  current = null;
  root.hidden = true;
  document.body.style.overflow = '';
  if (location.hash === '#frame') history_replace('');
}

function history_replace(hash) {
  try {
    window.history.replaceState(window.history.state, '', `${location.pathname}${location.search}${hash}`);
  } catch { /* a page that cannot rewrite its address keeps it */ }
}

/** Whether this page was opened at `/#frame`. */
export function frameAsked() {
  return location.hash === '#frame';
}
