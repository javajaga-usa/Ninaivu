/**
 * Turning the video pass off from the console.
 *
 * Describing a clip by several moments is the slowest thing a scan does —
 * five decodes and five passes through the model per video, which on a
 * processor is about three seconds each. On a library with seventeen thousand
 * videos in it that is fifteen hours, and the only way to say no to it was to
 * edit config.json by hand: a setting the next settings change in the console
 * then quietly threw away, because `Config.save()` writes a named subset and
 * `video_keyframes` was not in it.
 *
 * The last check here is that regression, which is the one worth having: it is
 * silent, it only shows up on the *second* settings change, and what it costs
 * is another fifteen hours.
 *
 *   node tests/video_keyframes_ui.mjs   (wants the console up and admin
 *                                        dad / correcthorse1)
 */
import { launch, ok, done, ADMIN } from './harness.mjs';

const b = await launch();
const p = await b.newPage({ viewport: { width: 1500, height: 1100 } });
const errs = [];
p.on('pageerror', (e) => errs.push(e.message));

await p.goto(ADMIN, { waitUntil: 'networkidle' });
await p.fill('#gate input[type="text"]', 'dad');
await p.fill('#gate input[type="password"]', 'correcthorse1');
await p.click('#gate .btn.primary');
await p.waitForSelector('.admin-person', { state: 'attached', timeout: 20000 });
await p.waitForTimeout(1500);

// The AI passes' switches live on AI -> AI models, beside the models they use.
// On a wide screen the section row is hidden and the sidebar lists every page,
// so the tab is reached directly.
await p.click('#tabs button[data-tab="ai-models"]');
await p.waitForTimeout(1200);

/** What the server currently believes, rather than what the checkbox shows. */
const stored = () => p.evaluate(async () =>
  (await (await fetch('/api/admin/overview')).json()).app.video_keyframes);

const row = p.locator('label.toggle', { hasText: 'Describe videos by several moments' });
ok('the console offers the switch', await row.count() === 1, `${await row.count()} rows`);
ok('and says what it costs before anybody turns it on',
  // Its own words, not "the slowest thing a scan does": the faces switch
  // says that too, about itself, and a check expecting one match found two.
  (await p.locator('p.hint', { hasText: 'five decodes and five passes' }).count()) === 1);

const box = row.locator('input[type="checkbox"]');
// Not "it starts on": this suite leaves it off, so asserting the default here
// would pass once and fail on every run after. What must always hold is that
// the switch shows what the server has, rather than what it was last clicked to.
ok('the switch shows what the server has stored',
  await box.isChecked() === (await stored() >= 2), String(await stored()));

// A Basic computer (server/tiers.py) starts with one moment, so the switch
// starts off and unchecking it would send nothing. Turn it on first, so that
// turning it off is a choice the server is actually told about.
if (!await box.isChecked()) {
  await box.check();
  await p.waitForTimeout(1200);
}
await box.uncheck();
await p.waitForTimeout(1200);
ok('turning it off means no moments at all', await stored() === 0, String(await stored()));

await box.check();
await p.waitForTimeout(1200);
ok('turning it back on means five', await stored() === 5, String(await stored()));

await box.uncheck();
await p.waitForTimeout(1200);
await p.evaluate(async () => {
  await fetch('/api/admin/settings', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ watch: false }),
  });
});
await p.waitForTimeout(600);
ok('and the choice survives an unrelated settings change',
  await stored() === 0, String(await stored()));

ok('no page errors', errs.length === 0, errs.join(' | '));

await done(b);
