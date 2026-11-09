/**
 * Mugil → Two computers: the census, the plan and the proof, on the console.
 *
 *   · before pairing: the standing, no census yet, the warning to take it with
 *     the disk plugged in, and Take a census, with its progress
 *   · paired, on the Primary: Plan, its totals by outcome, and its files,
 *     filtered by outcome
 *   · Prove nothing was lost: failed, naming the missing path; then passed
 *   · Replacing the Secondary's disk, chosen beside Plan: what only that disk
 *     holds, by path, and what to do before the disk goes
 *
 * The server's census, plan and proof are fakes (page.route): what is tested
 * is what the page asks and says, not mugil/topology.py (tests/test_topology.py).
 *
 *   node tests/two_computers_safe_ui.mjs
 */
import { launch, ok, done, ADMIN, signInAtConsole } from './harness.mjs';

const b = await launch();
const page = await b.newPage({ viewport: { width: 1280, height: 900 } });
const errs = [];
page.on('pageerror', (e) => errs.push(e.message));
await signInAtConsole(page);

const now = () => Date.now() / 1000;
const N300 = '/media/jaga/Toshiba/NinaivuMaster/Ninaivu Archive';
const MAC = '/Volumes/JApple Extreme SSD/NinaivuMaster/Ninaivu Archive';
const items = [
  { on: 'secondary', file: `${N300}/2026/11/04/linux-only.jpg`, size: 30, outcome: 'brought_in', to: 'Ninaivu Archive/2026/11/04/linux-only.jpg' },
  { on: 'secondary', file: `${N300}/2026/11/04/linux-only.jpg.xmp`, size: 12, outcome: 'brought_in', to: 'Ninaivu Archive/2026/11/04/linux-only.jpg.xmp' },
  { on: 'secondary', file: `${N300}/_deleted/old.jpg`, size: 20, outcome: 'left_alone', reason: 'recycle_bin' },
  { on: 'primary', file: `${MAC}/2026/11/02/same.jpg`, size: 10, outcome: 'both' },
];
const world = {
  standing: { role: 'single', paired: false }, censuses: [], record: null, can_prove: false,
  running: false, job: null, progress: {}, finishing: null, posts: [],
};
const answer = () => ({
  running: world.running, job: world.job, progress: world.progress, standing: world.standing,
  censuses: world.censuses, record: world.record, can_prove: world.can_prove, moving: false,
});
const reply = (route, body, status = 200) => route.fulfill({
  status, contentType: 'application/json', body: JSON.stringify(body) });

await page.route((url) => url.pathname.startsWith('/api/admin/mugil/topology')
  || url.pathname === '/api/admin/mugil/census', async (route) => {
  const request = route.request();
  const url = new URL(request.url());
  if (request.method() === 'POST') {
    world.posts.push(url.pathname);
    if (url.pathname === '/api/admin/mugil/topology/plan') {
      world.transition = JSON.parse(request.postData() || '{}').transition;
    }
    world.running = true;
    world.job = { '/api/admin/mugil/census': 'census', '/api/admin/mugil/topology/plan': 'plan' }[url.pathname] || 'prove';
    world.progress = world.job === 'census' ? { files: 1200, bytes: 3 * 1024 ** 3 } : {};
    return reply(route, answer());
  }
  if (url.pathname === '/api/admin/mugil/topology/plan') {
    const outcome = url.searchParams.get('outcome');
    return reply(route, { record: world.record, next: null,
      items: items.filter((i) => !outcome || i.outcome === outcome).map((i, n) => ({ ...i, n: n + 1 })),
      reasons: { recycle_bin: 'In a recycle bin: kept where it is, not brought in.' } });
  }
  if (world.running) {
    // Seen running once; done the next time it is asked.
    if (world.finishing) { world.running = false; world.finishing(); world.finishing = null; }
    else world.finishing = finish[world.job];
  }
  return reply(route, answer());
});

let proofs = 0;
const finish = {
  census: () => {
    world.censuses = [{ file: 'mugil-census-20261108-090000.jsonl.gz', role: world.standing.role,
      paired: false, taken_at: now() - 60, files: 48211, bytes: 912 * 1024 ** 3, unreadable: 0, away: [] }];
  },
  plan: () => {
    if (world.transition === 'replace_secondary_disk') {
      world.record = { id: 'p2', transition: 'replace_secondary_disk', state: 'planned', planned: now(),
        to: { role: 'primary', paired: true }, plan_file: 'mugil-plan-p2.jsonl.gz',
        plan: { only_on_the_secondary: 1, only_bytes: 30 * 1024 ** 2, files: 9, bytes: 4096,
          named: [{ computer: 'Linux PC', role: 'secondary', file: `${N300}/2026/11/04/linux-only.jpg`,
            size: 30 * 1024 ** 2 }] } };
      world.can_prove = true;
      return;
    }
    world.record = { id: 'p1', transition: 'pair', state: 'planned', planned: now(),
      to: { role: 'primary', paired: true }, plan_file: 'mugil-plan-p1.jsonl.gz',
      plan: { who: 'Linux PC',
        secondary: { brought_in: { files: 2, bytes: 42 }, left_alone: { files: 1, bytes: 20 } },
        primary: { both: { files: 1, bytes: 10 } } } };
    world.can_prove = true;
  },
  prove: () => {
    proofs += 1;
    const missing = proofs === 1 ? [{ computer: 'Linux PC', role: 'secondary',
      file: `${N300}/2025/01/01/deleted-on-mac.jpg`, size: 2048 }] : [];
    world.record = { ...world.record, state: missing.length ? 'failed' : 'done',
      proof: { passed: !missing.length, checked: 11, bytes: 4096, proved_at: now(),
        missing_count: missing.length, missing, away_before: [] } };
    world.can_prove = Boolean(missing.length);
  },
};

const open = async () => {
  await page.goto(ADMIN, { waitUntil: 'networkidle' });
  await page.waitForSelector('#tabs button[data-tab="cloud"]', { state: 'attached', timeout: 20000 });
  await page.evaluate(() => document.querySelector('#tabs button[data-tab="cloud"]').click());
  await page.waitForSelector('#tc-safe', { state: 'visible', timeout: 15000 });
};
const text = (sel) => page.textContent(sel).then((t) => t.trim());
const until = (sel, pattern) => page.waitForFunction(([s, p]) => new RegExp(p).test(
  document.querySelector(s)?.textContent || ''), [sel, pattern.source], { timeout: 15000 });

// --- before pairing: the census -------------------------------------------------

await open();
ok('before pairing: the standing is said', /Standing: One computer, not paired\./.test(await text('#tc-safe-standing')),
  await text('#tc-safe-standing'));
ok('before pairing: no census yet', /No census taken on this computer yet/.test(await text('#tc-safe-standing')));
ok('before pairing: the disk must be plugged in', await page.isVisible('#tc-safe-warn')
  && /disk that holds the photographs plugged in/.test(await text('#tc-safe-warn')));
ok('before pairing: no plan and no proof', !(await page.isVisible('#tc-plan-go')) && !(await page.isVisible('#tc-prove-go')));

await page.click('#tc-census-go');
await until('#tc-safe-says', /Taking a census: 1,200 files so far/);
ok('census: its progress is said, and it is not asked twice', !(await page.isVisible('#tc-census-go')));
await until('#tc-safe-standing', /Last census .*today: 48,211 files/);
ok('census: its age and size are said', /912 GB/.test(await text('#tc-safe-standing')), await text('#tc-safe-standing'));
ok('census: it can be taken again', await page.isVisible('#tc-census-go'));
ok('census: one request', world.posts.join() === '/api/admin/mugil/census', world.posts.join());

// --- paired, on the Primary: the plan --------------------------------------------

world.standing = { role: 'primary', paired: true };
await open();
ok('paired: no census and no warning', !(await page.isVisible('#tc-census-go')) && !(await page.isVisible('#tc-safe-warn')));
ok('paired: the Primary plans', await page.isVisible('#tc-plan-go'));
ok('paired: and chooses which change', await page.isVisible('#tc-plan-which')
  && (await page.$$eval('#tc-plan-which option', (o) => o.map((x) => x.value))).join()
    === 'pair,replace_secondary_disk,move_primary,unpair');
await page.click('#tc-plan-go');
await until('#tc-safe-says', /Making the plan/);
await page.waitForSelector('#tc-plan:not([hidden])', { timeout: 15000 });
const totals = await text('#tc-plan-totals');
ok('plan: totals by outcome', /On the Secondary: brought in, 2 files/.test(totals)
  && /On the Secondary: left alone, 1 files/.test(totals) && /On the Primary: on both, 1 files/.test(totals), totals);
await page.waitForFunction(() => document.querySelectorAll('#tc-plan-list li').length === 4, null, { timeout: 5000 });
ok('plan: every file listed', true);
ok('plan: a reason is said', /recycle bin/.test(await text('#tc-plan-list')));
await page.selectOption('#tc-plan-filter', 'brought_in');
await page.waitForFunction(() => document.querySelectorAll('#tc-plan-list li').length === 2, null, { timeout: 5000 });
const shown = await page.$$eval('#tc-plan-list li', (rows) => rows.map((r) => r.dataset.outcome));
ok('plan: filtered by outcome', shown.every((o) => o === 'brought_in'), shown.join());
ok('plan: where each goes', /to Ninaivu Archive\/2026\/11\/04\/linux-only\.jpg/.test(await text('#tc-plan-list')));
ok('plan: what to do next', /Bring them in, then prove nothing was lost|Carry the change out/.test(await text('#tc-safe-says')));

// --- the proof ----------------------------------------------------------------------

ok('proof: offered once planned', await page.isVisible('#tc-prove-go'));
await page.click('#tc-prove-go');
await until('#tc-proof-says', /Failed: 1 files seen before are missing/);
ok('proof: the missing path is named',
  (await text('#tc-proof-missing')).includes(`${N300}/2025/01/01/deleted-on-mac.jpg`), await text('#tc-proof-missing'));
ok('proof: it can be run again', await page.isVisible('#tc-prove-go'));
await page.click('#tc-prove-go');
await until('#tc-proof-says', /Passed: nothing was lost\. All 11 files/);
ok('proof: nothing missing is listed', (await page.$$('#tc-proof-missing li')).length === 0);
ok('proof: done is done', !(await page.isVisible('#tc-prove-go')));

ok('plan: the pairing was asked for', world.transition === 'pair', world.transition);

// --- replacing the Secondary's disk --------------------------------------------------------

world.can_prove = false;
await page.selectOption('#tc-plan-which', 'replace_secondary_disk');
await page.click('#tc-plan-go');
await page.waitForSelector('#tc-disk-plan:not([hidden])', { timeout: 15000 });
ok('disk: that change was asked for', world.transition === 'replace_secondary_disk', world.transition);
ok('disk: what only that disk holds is counted', /1 files are only on the Secondary's disk \(30 MB\)/
  .test(await text('#tc-disk-says')), await text('#tc-disk-says'));
ok('disk: and named by path', (await text('#tc-disk-only')).includes(`${N300}/2026/11/04/linux-only.jpg`));
ok('disk: what to do before the disk goes', /put the new disk in/.test(await text('#tc-safe-says')),
  await text('#tc-safe-says'));
ok('disk: no pairing totals', await page.isHidden('#tc-plan'));
ok('disk: then prove', await page.isVisible('#tc-prove-go'));

// --- a phone ---------------------------------------------------------------------------

await page.setViewportSize({ width: 390, height: 844 });
const box = await page.evaluate(() => ({
  right: document.querySelector('#tc-safe').getBoundingClientRect().right, width: window.innerWidth }));
ok('phone: the block fits the screen', box.right <= box.width + 1, JSON.stringify(box));

ok('no script errors', errs.length === 0, errs.join(' | '));
await done(b);
