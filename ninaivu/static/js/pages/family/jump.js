/**
 * The gallery's "Jump to" list: the years and months this view has, loaded
 * when it is first opened (app.js, openPage). Whether the button is shown,
 * and closing the list, stay in app.js, which knows when the view changes.
 */

import * as i18n from '../../i18n.js';
import { sectionAt } from '../../layout.js';
import { $ } from '../../ui.js';

let shell = null;
let jumpYear = null;

function renderJump() {
  const { grid } = shell;
  const { years, undated } = shell.jumpIndex();
  const yearBox = $('#jump-years');
  const monthBox = $('#jump-months');
  yearBox.innerHTML = '';
  monthBox.innerHTML = '';
  if (!years.size && !undated) return;
  const current = sectionAt(grid.layout, grid.scroller.scrollTop + 40)?.key || '';
  if (!jumpYear || !years.has(jumpYear)) {
    jumpYear = years.has(current.slice(0, 4)) ? current.slice(0, 4) : [...years.keys()][0];
  }
  for (const year of years.keys()) {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = year;
    button.classList.toggle('active', year === jumpYear);
    button.onclick = () => { jumpYear = year; renderJump(); };
    yearBox.appendChild(button);
  }
  const months = years.get(jumpYear) || new Map();
  const name = new Intl.DateTimeFormat(i18n.locale(), { month: 'short' });
  for (let m = 1; m <= 12; m += 1) {
    const key = String(m).padStart(2, '0');
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = name.format(new Date(2000, m - 1, 1));
    const header = months.get(key);
    button.disabled = !header;
    button.classList.toggle('current', current.slice(0, 7) === `${jumpYear}-${key}`);
    if (header) {
      button.onclick = () => jumpTo(header);
      // Pointed at, its month starts arriving before it is chosen.
      button.onpointerenter = () => grid.prefetchAround(Math.max(0, header.y - 4));
    }
    monthBox.appendChild(button);
  }
  const undatedButton = $('#jump-undated');
  undatedButton.hidden = !undated;
  undatedButton.onclick = undated ? () => jumpTo(undated) : null;
  // The year being looked at in the middle of the list, with the years either
  // side of it in view, rather than at an edge where the fade would cover it.
  const active = yearBox.querySelector('.active');
  if (active && (active.offsetTop < yearBox.scrollTop
      || active.offsetTop + active.offsetHeight > yearBox.scrollTop + yearBox.clientHeight)) {
    yearBox.scrollTop = active.offsetTop - (yearBox.clientHeight - active.offsetHeight) / 2;
  }
  shadeJumpYears();
}

/** Fades the year list at an end that has more years beyond it. */
function shadeJumpYears() {
  const list = $('#jump-years');
  if (!list) return;
  list.classList.toggle('more-above', list.scrollTop > 2);
  list.classList.toggle('more-below',
    list.scrollTop + list.clientHeight < list.scrollHeight - 2);
}

/** Up and Down on the year list move to the next or previous year, as far as
 *  the first and the last there are. */
function stepJumpYear(step) {
  const { years } = shell.jumpIndex();
  const list = [...years.keys()];
  const at = list.indexOf(jumpYear);
  const next = list[Math.max(0, Math.min(list.length - 1, at + step))];
  if (next && next !== jumpYear) {
    jumpYear = next;
    renderJump();
  }
  $('#jump-years .active')?.focus();
}

function jumpTo(header) {
  const { grid } = shell;
  shell.closeJump();
  grid.scroller.scrollTop = Math.max(0, header.y - 4);
}

export async function openJump() {
  const pop = $('#jump-pop');
  if (!pop.hidden) { shell.closeJump(); return; }
  jumpYear = null;
  pop.hidden = false;
  $('#jump').classList.add('open');
  $('#jump-btn').setAttribute('aria-expanded', 'true');
  renderJump();
  if (shell.paging.next != null) {
    await shell.loadAllPages();
    if (!pop.hidden) renderJump();
  }
  (pop.querySelector('.jump-months button.current')
    || pop.querySelector('.jump-years button.active'))?.focus();
}

/** Bind the year list: once, when it is first opened. */
export function wire(context) {
  shell = context;
  $('#jump-years').addEventListener('scroll', shadeJumpYears, { passive: true });
  $('#jump-years').addEventListener('keydown', (event) => {
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
    event.preventDefault();
    stepJumpYear(event.key === 'ArrowDown' ? 1 : -1);
  });
}
