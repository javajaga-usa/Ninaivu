/**
 * The phone's section row: a dot on a section means a tab inside it shows a
 * number, and the dots keep up while another page is open.
 *
 * On a phone the console shows its sections as a row of pills, and a section
 * holding a queue with something in it wears a dot. The dot used to follow a
 * list of its own: "Backup & health" was lit by a handover plan nobody had
 * written, and "People & access" by faces without a name, while no tab in
 * either showed a count; and the dots only moved while the Overview was open.
 *
 *   node tests/console_tab_marks_ui.mjs   (wants the console up)
 * @fixture several
 */
import { launch, ok, done, signInAtConsole } from './harness.mjs';

const b = await launch();
const errs = [];
const context = await b.newContext({ viewport: { width: 390, height: 844 }, serviceWorkers: 'block' });
const page = await context.newPage();
page.on('pageerror', (e) => errs.push(e.message));
await signInAtConsole(page);
await page.waitForFunction(() => document.querySelector('#attention-headline')?.textContent.trim(),
  null, { timeout: 20000 });

/** Each section, whether it is marked, and the counts its tabs show. */
const sections = () => page.evaluate(() =>
  [...document.querySelectorAll('#tab-groups button[data-group]')].map((button) => ({
    group: button.dataset.group,
    marked: button.classList.contains('needs-attention'),
    counts: [...document.querySelectorAll(`#tabs button[data-group="${button.dataset.group}"] .tab-count`)]
      .filter((badge) => !badge.hidden && badge.textContent.trim())
      .map((badge) => `${badge.closest('button').dataset.tab}:${badge.textContent.trim()}`),
  })));

/** A dot on every section with a number in it, and on no other. */
async function marksMatchCounts(when) {
  for (const s of await sections()) {
    ok(`${when}: ${s.group} is ${s.marked ? '' : 'not '}marked, and its tabs agree`,
      s.marked === s.counts.length > 0, JSON.stringify(s));
  }
}

const attention = () => page.evaluate(async () =>
  Object.fromEntries((await (await fetch('/api/admin/attention')).json()).items
    .map((item) => [item.page, item.count])));

await marksMatchCounts('at sign-in');
const before = await attention();
ok('a library with no handover plan asks for one', before.handover === 1, JSON.stringify(before));
ok('…and the Handover tab says so', (await sections()).find((s) => s.group === 'backup')
  .counts.includes('handover:1'));

/* ---------- somebody asks for help while another page is open ---------- */
await page.click('#tab-groups button[data-group="library"]');
await page.waitForFunction(() => !document.querySelector('#tabs button[data-tab="overview"].active'));
const made = await page.evaluate(async () => (await fetch('/api/people', {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ username: 'leela', password: 'riverstones42', name: 'Leela',
    role: 'family', must_change: false }),
})).status);
ok('a family member is made', made === 200, String(made));
const asked = await page.evaluate(async () => (await fetch('/api/auth/forgot', {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ username: 'leela' }),
})).status);
ok('…who asks for help signing in', asked === 200, String(asked));
const marked = await page.waitForFunction(() =>
  document.querySelector('#tab-groups button[data-group="people"]')?.classList.contains('needs-attention'),
null, { timeout: 25000 }).then(() => true, () => false);
ok('the People section is marked without going back to the Overview', marked);
const people = (await sections()).find((s) => s.group === 'people');
ok('…and its People tab shows the one asking', people.counts.includes('people:1'), JSON.stringify(people));
await marksMatchCounts('after the ask');

ok('no page errors', errs.length === 0, errs.join('\n'));
await done(b);
