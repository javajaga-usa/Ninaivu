/**
 * Memory films: "Make a film" beside "Make a book", and the films made
 * (ninaivu/media/films.py does the work; a trial of Ninaivu 2.0, shown only
 * while `memory_films` is switched on).
 *
 * The same three steps as a book (static/js/books.js), in the same dialog
 * classes: the settings (how long, how it moves, its colour, the music, the
 * subtitles), the moments Ninaivu picked — every one ticked — and the making,
 * which runs on the server while this polls it and ends in the film itself,
 * playing, with a Download button.
 *
 * Music: sound files in the library are for administrators alone, so anybody
 * may bring a song of their own for their films ("Bring a song…"), kept for
 * them alone.
 */

import { api, thumbUrl } from './api.js';
import * as i18n from './i18n.js';

const LENGTHS = [
  [15, i18n.key('Short, about a minute')],
  [40, i18n.key('Medium, about three minutes')],
  [80, i18n.key('Long, about six minutes')],
];
const PACES = [
  ['gentle', i18n.key('Gentle')],
  ['steady', i18n.key('Steady')],
  ['lively', i18n.key('Lively')],
];
const LOOKS = [
  ['natural', i18n.key('Natural')],
  ['warm', i18n.key('Warm')],
  ['faded', i18n.key('Faded')],
  ['mono', i18n.key('Black and white')],
];
const SUBTITLES = [
  ['both', i18n.key('What was said, and in English')],
  ['spoken', i18n.key('What was said')],
  ['english', i18n.key('English')],
  ['none', i18n.key('No subtitles')],
];
const SIZES = [
  ['1080p', i18n.key('Full HD (1080p)')],
  ['720p', i18n.key('HD (720p), quicker')],
];
const STYLES = [
  ['film', i18n.key('Memories')],
  ['growing_up', i18n.key('Growing up: the same face through the years')],
  ['then_and_now', i18n.key('Then and now: side by side, years apart')],
];

let available = null;
let modal = null;
let source = null;
let picked = [];
let poll = null;
let toastFn = () => {};
let pairs = [];
let chapters = [];
let choices = { count: 40, pace: 'steady', look: 'natural', subtitles: 'both', size: '1080p',
                stories: true, live: true, music: '', style: 'film', born: '' };

/** Whether films are switched on for this household (asked once). */
export function filmsAvailable() {
  if (!available) available = api.filmOptions().then(() => true, () => false);
  return available;
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

function field(label, control) {
  return el('label', { class: 'export-field' }, el('span', { text: label }), control);
}

function select(options, value) {
  const box = el('select', { class: 'input' });
  for (const [code, name] of options) box.add(new Option(i18n.t(name), String(code)));
  box.value = String(value);
  return box;
}

function months() {
  try {
    const format = new Intl.DateTimeFormat(i18n.locale(), { month: 'long', timeZone: 'UTC' });
    return Array.from({ length: 12 }, (_, m) => format.format(new Date(Date.UTC(2020, m, 15))));
  } catch {
    return undefined;
  }
}

function build() {
  if (modal) return modal;
  modal = el('div', { class: 'modal', id: 'film-modal', hidden: true, role: 'dialog',
                      'aria-modal': 'true', 'aria-labelledby': 'film-title' });
  const card = el('form', { class: 'modal-card narrow export-form book-card', id: 'film-form' });
  const closer = el('button', { class: 'icon-btn', type: 'button', 'aria-label': i18n.t('Close') });
  closer.innerHTML = '<svg viewBox="0 0 24 24"><path d="M18 6 6 18M6 6l12 12"/></svg>';
  closer.onclick = close;
  card.append(el('div', { class: 'export-head' },
                 el('h2', { id: 'film-title', text: i18n.t('Make a film') }), closer));
  modal.append(card);
  modal.addEventListener('mousedown', (event) => { if (event.target === modal) close(); });
  modal.addEventListener('keydown', (event) => { if (event.key === 'Escape') close(); });
  document.body.appendChild(modal);
  return modal;
}

function step(heading, ...nodes) {
  const card = build().querySelector('form');
  while (card.children.length > 1) card.lastChild.remove();
  card.append(...nodes);
  card.querySelector('h2').textContent = heading;
  card.onsubmit = null;
  return card;
}

function close() {
  clearTimeout(poll);
  poll = null;
  if (modal) {
    modal.querySelectorAll('video').forEach((video) => video.pause());
    modal.hidden = true;
  }
}

function musicChoice(value) {
  if (!value) return null;
  const [kind, id] = value.split(':');
  return kind === 'upload' ? { kind, token: id } : { kind: 'library', id: Number(id) };
}

/**
 * Open "Make a film" for a source: `{ kind: 'album'|'occasion'|'person'|'smart'|'year',
 * id, name, title?, subtitle? }`.
 */
export function openFilmSheet(from, { toast } = {}) {
  source = { ...from };
  if (toast) toastFn = toast;
  build().hidden = false;
  settings();
}

async function settings() {
  const title = el('input', { class: 'input', type: 'text', maxLength: 120,
                              value: source.title || source.name || '' });
  const subtitle = el('input', { class: 'input', type: 'text', maxLength: 160,
                                 value: source.subtitle || source.place || '' });
  const length = select(LENGTHS, choices.count);
  const pace = select(PACES, choices.pace);
  const look = select(LOOKS, choices.look);
  const subtitles = select(SUBTITLES, choices.subtitles);
  const size = select(SIZES, choices.size);
  const music = el('select', { class: 'input' });
  const bring = el('input', { type: 'file', accept: 'audio/*', hidden: true });
  const bringButton = el('button', { class: 'btn ghost small', type: 'button', text: i18n.t('Bring a song…') });
  bringButton.onclick = () => bring.click();
  const stories = el('input', { type: 'checkbox', checked: choices.stories });
  const live = el('input', { type: 'checkbox', checked: choices.live });
  // A growing-up or then-and-now film is of one person.
  const ofPerson = source.kind === 'person';
  if (!ofPerson) choices.style = 'film';
  const style = select(STYLES, choices.style);
  const born = el('input', { class: 'input', type: 'number', min: 1880, max: new Date().getFullYear(),
                             placeholder: i18n.t('Optional'), value: choices.born || '' });
  const styleField = ofPerson ? field(i18n.t('Kind of film'), style) : null;
  const bornField = field(i18n.t('Year they were born, to show their age'), born);
  const lengthField = field(i18n.t('Length'), length);
  const subtitlesField = field(i18n.t('Subtitles under the voice stories'), subtitles);
  const storiesField = el('label', { class: 'export-check' }, stories,
    el('span', { text: i18n.t('Tell the voice stories over their photographs') }));
  const liveField = el('label', { class: 'export-check' }, live,
    el('span', { text: i18n.t('Let Live Photos move') }));
  const warning = el('p', { class: 'hint warn', hidden: true,
    text: i18n.t('This computer has no Tamil font, so Tamil words will be left out of the film. Installing one (for example the Noto Tamil fonts) fixes it.') });

  const fillMusic = (options) => {
    music.replaceChildren(new Option(i18n.t('No music'), ''));
    for (const song of options.my_music || []) music.add(new Option(song.title, `upload:${song.token}`));
    for (const song of options.music || []) music.add(new Option(song.title, `library:${song.id}`));
    music.value = [...music.options].some((o) => o.value === choices.music) ? choices.music : '';
  };
  api.filmOptions().then((options) => {
    fillMusic(options);
    warning.hidden = !options.fonts || options.fonts.tamil;
  }).catch(() => {});
  bring.onchange = async () => {
    const file = bring.files?.[0];
    if (!file) return;
    bringButton.disabled = true;
    try {
      const song = await api.filmSongAdd(file);
      choices.music = `upload:${song.token}`;
      fillMusic(await api.filmOptions());
    } catch (err) {
      toastFn(i18n.t('Could not add the song: {reason}', { reason: err.message }), true);
    } finally {
      bringButton.disabled = false;
      bring.value = '';
    }
  };

  const next = el('button', { class: 'btn primary', type: 'submit', text: i18n.t('Choose moments') });
  const cancel = el('button', { class: 'btn ghost', type: 'button', text: i18n.t('Cancel') });
  cancel.onclick = close;
  const card = step(i18n.t('Make a film'),
    el('p', { class: 'hint', text: i18n.t('Ninaivu picks the best photographs and clips, moves slowly towards the faces, tells the voice stories over their photographs with the words underneath, and cuts on the beat of the music.') }),
    styleField,
    bornField,
    field(i18n.t('Title'), title),
    field(i18n.t('Subtitle'), subtitle),
    lengthField,
    field(i18n.t('Pace'), pace),
    field(i18n.t('Look'), look),
    el('label', { class: 'export-field' }, el('span', { text: i18n.t('Music') }),
       el('span', { class: 'export-range' }, music, bringButton)),
    bring,
    subtitlesField,
    field(i18n.t('Picture'), size),
    storiesField,
    liveField,
    warning,
    el('div', { class: 'export-actions' }, cancel, next),
  );
  const showStyle = () => {
    bornField.hidden = style.value !== 'growing_up';
    // Stories and clips belong to a film of memories; a then-and-now film is
    // as long as its pairs.
    lengthField.hidden = style.value === 'then_and_now';
    for (const node of [subtitlesField, storiesField, liveField]) node.hidden = style.value !== 'film';
  };
  style.onchange = showStyle;
  showStyle();
  card.onsubmit = async (event) => {
    event.preventDefault();
    choices = { count: Number(length.value), pace: pace.value, look: look.value,
                subtitles: subtitles.value, size: size.value, stories: stories.checked,
                live: live.checked, music: music.value, style: style.value,
                born: born.value ? Number(born.value) : '' };
    source.title = title.value.trim();
    source.subtitle = subtitle.value.trim();
    next.disabled = true;
    try {
      const data = await api.filmPick({ kind: source.kind, id: source.id }, choices.count, choices.style);
      picked = data.items || [];
      pairs = data.pairs || [];
      chapters = [];
      preview();
    } catch (err) {
      toastFn(i18n.t('Could not choose moments: {reason}', { reason: err.message }), true);
      next.disabled = false;
    }
  };
  setTimeout(() => title.focus(), 0);
}

/** The chapters, each moved onto the first moment still ticked in it. */
function chaptersFor(ids) {
  const order = picked.map((item) => item.id);
  const ticked = new Set(ids);
  const starts = chapters.map((c) => order.indexOf(c.id));
  return chapters.map((chapter, n) => {
    const end = n + 1 < starts.length ? starts[n + 1] : order.length;
    const first = order.slice(Math.max(0, starts[n]), end).find((id) => ticked.has(id));
    return first ? { id: first, title: chapter.title, line: chapter.line } : null;
  }).filter((c) => c && c.title.trim());
}

function fields(ids) {
  const out = {
    source: { kind: source.kind, id: source.id }, ids, title: source.title || source.name || '',
    subtitle: source.subtitle || '', closing: source.closing || i18n.t('Made with Ninaivu'),
    months: months(), pace: choices.pace, look: choices.look, subtitles: choices.subtitles,
    size: choices.size, stories: choices.stories, live: choices.live,
    music: musicChoice(choices.music), style: choices.style || 'film',
  };
  if (out.style === 'growing_up' && choices.born) {
    out.born = choices.born;
    out.age_label = i18n.t('Age {age}', { age: '{age}' });
  }
  if (out.style === 'film' && chapters.length) out.chapters = chaptersFor(ids);
  return out;
}

/** "Draft the chapters": Ninaivu's storyboard, every word of it to change. */
function storyboardBlock(grid) {
  const list = el('div', { class: 'film-chapters' });
  const said = el('p', { class: 'hint', 'aria-live': 'polite' });
  const draft = el('button', { class: 'btn ghost small', type: 'button', text: i18n.t('Draft the chapters') });
  const draw = () => {
    list.replaceChildren();
    const order = picked.map((item) => item.id);
    for (const chapter of chapters) {
      const heading = el('input', { class: 'input', type: 'text', maxLength: 60, value: chapter.title,
                                    'aria-label': i18n.t('Chapter heading') });
      const line = el('input', { class: 'input', type: 'text', maxLength: 140, value: chapter.line || '',
                                 placeholder: i18n.t('A line under it (optional)'),
                                 'aria-label': i18n.t('Line under the heading') });
      heading.oninput = () => { chapter.title = heading.value; };
      line.oninput = () => { chapter.line = line.value; };
      const remove = el('button', { class: 'btn ghost small', type: 'button', text: i18n.t('Remove') });
      remove.onclick = () => { chapters = chapters.filter((c) => c !== chapter); draw(); };
      const at = picked[order.indexOf(chapter.id)];
      const thumb = at ? el('img', { src: thumbUrl(at.id, 120, at.thumb_v), alt: '', loading: 'lazy' }) : null;
      list.append(el('div', { class: 'film-chapter' }, thumb,
                      el('div', { class: 'film-chapter-text' }, heading, line), remove));
    }
  };
  draft.onclick = async () => {
    const ids = [...grid.querySelectorAll('input:checked')].map((box) => Number(box.value));
    if (!ids.length) return;
    draft.disabled = true;
    said.textContent = i18n.t('Ninaivu is looking through the moments…');
    try {
      const board = await api.filmStoryboard({
        source: { kind: source.kind, id: source.id }, ids, language: i18n.language() === 'ta' ? 'ta' : 'en',
        months: months(), title: source.title || source.name || '',
      });
      chapters = board.chapters || [];
      if (board.title) source.title = board.title;
      if (board.subtitle) source.subtitle = board.subtitle;
      if (board.closing) source.closing = board.closing;
      said.textContent = board.by === 'model'
        ? i18n.t('Drafted by Ninaivu on this computer: “{title}”. Change anything you like.', { title: source.title })
        : (chapters.length
          ? i18n.t('Drafted from the dates and places. With the writing model downloaded (AI models page), Ninaivu writes the headings itself.')
          : i18n.t('These moments do not turn anywhere, so the film has no chapters.'));
      draw();
    } catch (err) {
      said.textContent = i18n.t('Could not draft the chapters: {reason}', { reason: err.message });
    } finally {
      draft.disabled = false;
    }
  };
  draw();
  return el('section', { class: 'film-storyboard' },
    el('div', { class: 'export-range' }, el('b', { text: i18n.t('Chapters') }), draft), said, list);
}

function pairsGrid() {
  const grid = el('div', { class: 'film-pairs' });
  for (const [then, now] of pairs) {
    const tick = el('input', { type: 'checkbox', checked: true, value: `${then.id},${now.id}`,
                               'aria-label': `${then.date || ''} – ${now.date || ''}` });
    const img = (item) => el('img', { src: thumbUrl(item.id, 240, item.thumb_v), alt: '', loading: 'lazy' });
    grid.append(el('label', { class: 'book-pick film-pair' }, img(then), img(now), tick));
  }
  return grid;
}

function preview() {
  const paired = choices.style === 'then_and_now';
  const grid = paired ? pairsGrid() : el('div', { class: 'book-picks' });
  const count = el('p', { class: 'hint' });
  const make = el('button', { class: 'btn primary', type: 'submit', text: i18n.t('Make the film') });
  const sync = () => {
    const n = grid.querySelectorAll('input:checked').length;
    count.textContent = paired
      ? i18n.t('{count} of {total} pairs will be in the film. Untick any you would rather leave out.',
               { count: n, total: pairs.length })
      : i18n.t('{count} of {total} moments will be in the film. Untick any you would rather leave out.',
               { count: n, total: picked.length });
    make.disabled = n === 0;
  };
  for (const box of grid.querySelectorAll('input')) box.onchange = sync;
  for (const item of paired ? [] : picked) {
    const tick = el('input', { type: 'checkbox', checked: true, value: String(item.id),
                               'aria-label': item.date || String(item.id) });
    tick.onchange = sync;
    const img = el('img', { src: thumbUrl(item.id, 240, item.thumb_v), alt: '', loading: 'lazy' });
    const badge = item.kind === 'video' ? el('span', { class: 'film-pick-badge', text: '▶' })
      : (item.stories ? el('span', { class: 'film-pick-badge', text: '♪' }) : null);
    grid.append(el('label', { class: 'book-pick' }, img, badge, tick));
  }
  const back = el('button', { class: 'btn ghost', type: 'button', text: i18n.t('Back') });
  back.onclick = () => settings();
  let empty = i18n.t('There is nothing here that can go in a film.');
  if (choices.style === 'growing_up') {
    empty = i18n.t('No photographs show their face clearly enough, with a date, to hold it through the years.');
  } else if (paired) {
    empty = i18n.t('There are not enough years between their photographs for then and now: it needs at least five.');
  }
  const card = step(i18n.t('Make a film'),
    picked.length ? count : el('p', { class: 'hint', text: empty }),
    choices.style === 'film' && picked.length ? storyboardBlock(grid) : null,
    grid,
    el('div', { class: 'export-actions' }, back, make),
  );
  sync();
  card.onsubmit = async (event) => {
    event.preventDefault();
    const ids = [...grid.querySelectorAll('input:checked')]
      .flatMap((box) => box.value.split(',').map(Number));
    if (!ids.length) return;
    make.disabled = true;
    try {
      progress(await api.filmBuild(fields(ids)));
    } catch (err) {
      toastFn(i18n.t('Could not make the film: {reason}', { reason: err.message }), true);
      make.disabled = false;
    }
  };
  setTimeout(() => make.focus(), 0);
}

function minutes(seconds) {
  const whole = Math.round(seconds || 0);
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, '0')}`;
}

function player(film) {
  return el('video', { class: 'film-player', src: film.play, controls: true, playsInline: true,
                       preload: 'metadata' });
}

function downloadLink(film, small = false) {
  return el('a', { class: small ? 'btn primary small' : 'btn primary', href: film.download,
                   download: '', text: i18n.t('Download') });
}

function progress(film) {
  const bar = el('progress', { class: 'book-progress', max: 1, value: 0 });
  const said = el('p', { class: 'hint', 'aria-live': 'polite' });
  const body = el('div');
  const actions = el('div', { class: 'export-actions' });
  step(i18n.t('Make a film'), el('p', { text: film.title }), body, bar, said, actions);
  const show = (now) => {
    clearTimeout(poll);
    actions.replaceChildren();
    const { done, total } = now.progress || {};
    if (now.state === 'building') {
      bar.max = total || 1;
      bar.value = done || 0;
      said.textContent = total
        ? i18n.t('Making frame {done} of {total}…', { done, total })
        : i18n.t('Getting the photographs and the music ready…');
      const stop = el('button', { class: 'btn ghost', type: 'button', text: i18n.t('Stop') });
      stop.onclick = () => api.filmCancel(now.id).catch(() => {});
      actions.append(stop);
      poll = setTimeout(() => api.film(now.id).then(show).catch(() => {
        poll = setTimeout(() => show(now), 3000);
      }), 1000);
      return;
    }
    const closer = el('button', { class: 'btn ghost', type: 'button', text: i18n.t('Close') });
    closer.onclick = close;
    bar.hidden = now.state === 'done';
    if (now.state === 'done') {
      body.replaceChildren(player(now));
      said.textContent = i18n.t('Your film is ready: {length}. It stays in Books until you delete it.',
                                { length: minutes(now.seconds) });
      actions.append(closer, downloadLink(now));
    } else {
      said.textContent = now.state === 'cancelled' ? i18n.t('Stopped. No film was made.')
        : i18n.t('The film could not be made.');
      if (now.error && now.state !== 'cancelled') said.title = now.error;
      actions.append(closer);
    }
  };
  show(film);
}

/** One tap from a ready-made book idea: the same moments, as a film. */
export async function makeFilmFromIdea(plan, button, { toast } = {}) {
  if (toast) toastFn = toast;
  source = { ...plan.from, title: plan.title, subtitle: plan.subtitle };
  choices.style = 'film';
  chapters = [];
  button.disabled = true;
  try {
    const picks = await api.filmPick({ kind: source.kind, id: source.id }, choices.count);
    picked = picks.items || [];
    const ids = picked.map((item) => item.id);
    if (!ids.length) throw new Error(i18n.t('There is nothing here that can go in a film.'));
    const film = await api.filmBuild(fields(ids));
    build().hidden = false;
    progress(film);
  } catch (err) {
    toastFn(i18n.t('Could not make the film: {reason}', { reason: err.message }), true);
  } finally {
    button.disabled = false;
  }
}

/** "Your films", for the Books list: play, download again, or delete. */
export async function filmsSection({ toast } = {}) {
  if (toast) toastFn = toast;
  if (!(await filmsAvailable())) return null;
  const section = el('div', { class: 'book-list' });
  let data;
  try {
    data = await api.films();
  } catch {
    return null;
  }
  const films = data.films || [];
  if (!films.length) return null;
  section.append(el('h3', { class: 'book-ideas-title', text: i18n.t('Your films') }));
  for (const film of films) {
    let state = '';
    if (film.state === 'building') state = i18n.t('Being made…');
    else if (film.state === 'done') state = minutes(film.seconds);
    else if (film.state === 'cancelled') state = i18n.t('Stopped');
    else state = i18n.t('Could not be made');
    const when = new Date((film.finished_at || film.created_at) * 1000).toLocaleDateString(i18n.locale());
    const remove = el('button', { class: 'btn ghost small', type: 'button', text: i18n.t('Delete') });
    const row = el('div', { class: 'book-row' });
    remove.onclick = async () => {
      if (!confirm(i18n.t('Delete the film “{title}”? The photographs are not touched.', { title: film.title }))) return;
      try {
        await api.deleteFilm(film.id);
        row.remove();
      } catch (err) {
        toastFn(i18n.t('Could not delete the film: {reason}', { reason: err.message }), true);
      }
    };
    const play = film.play ? el('button', { class: 'btn ghost small', type: 'button', text: i18n.t('Play') }) : null;
    if (play) play.onclick = () => { build().hidden = false; progress(film); };
    const watch = film.state === 'building'
      ? el('button', { class: 'btn ghost small', type: 'button', text: i18n.t('Show progress') }) : null;
    if (watch) watch.onclick = () => { build().hidden = false; progress(film); };
    row.append(
      el('div', { class: 'book-row-text' }, el('b', { text: film.title }),
         el('span', { class: 'hint', text: [state, when].join(' · ') })),
      el('div', { class: 'book-row-actions' }, play || watch,
         film.download ? downloadLink(film, true) : null, remove));
    section.append(row);
  }
  return section;
}
