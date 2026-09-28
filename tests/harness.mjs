/**
 * What every browser test needs, in one place.
 *
 * Twenty of these suites began with the same two lines pasted in: an import
 * from `/tmp/node_modules/playwright` and a Chromium at
 * `/opt/pw-browsers/chromium-1194/chrome-linux/chrome`. Those are Linux paths
 * from whichever machine wrote them, and on the Windows box that actually runs
 * Ninaivu none of the suites would start — which is a poor property for the
 * tests that check the thing people use.
 *
 * So the paths are found rather than assumed, and the ports come from the
 * environment:
 *
 *   NINAIVU_HOME   the family app        (default http://127.0.0.1:5000)
 *   NINAIVU_ADMIN  the console           (default http://127.0.0.1:3000)
 *   NINAIVU_USER / NINAIVU_PASS           (default dad / correcthorse1)
 *   PLAYWRIGHT_BROWSER  an explicit browser executable, if you have one
 *
 * Usage:
 *
 *   import { launch, ok, done, HOME, ADMIN, ADMIN_USER } from './harness.mjs';
 *   const browser = await launch();
 *   ok('the page loaded', title === 'Ninaivu');
 *   await done(browser);
 */

import { createRequire } from 'node:module';
import { existsSync, readdirSync, statSync } from 'node:fs';
import path from 'node:path';

export const HOME = process.env.NINAIVU_HOME || 'http://127.0.0.1:5000';
export const ADMIN = process.env.NINAIVU_ADMIN || 'http://127.0.0.1:3000';
export const ADMIN_USER = process.env.NINAIVU_USER || 'dad';
export const ADMIN_PASS = process.env.NINAIVU_PASS || 'correcthorse1';

const require_ = createRequire(import.meta.url);

/** Playwright, from wherever this machine keeps it. */
export function playwright() {
  const tried = [];
  for (const spec of ['playwright', 'playwright-core',
                      '/tmp/node_modules/playwright/index.mjs']) {
    try {
      return require_(spec.endsWith('.mjs') ? spec : spec);
    } catch (error) {
      tried.push(`${spec}: ${error.code || error.message}`);
    }
  }
  throw new Error(
    'Playwright is not installed for this machine.\n'
    + '  npm install --no-save playwright\n'
    + `  (tried ${tried.join('; ')})`);
}

/**
 * A browser to drive.
 *
 * Playwright's own download is used when there is one — that is the case on a
 * machine where `npx playwright install` has been run. An explicit path in
 * PLAYWRIGHT_BROWSER wins, and a Chromium sitting in a well-known folder is
 * the last resort, so the machines that already had one keep working.
 */
export async function launch(options = {}) {
  const { chromium } = playwright();
  const executablePath = process.env.PLAYWRIGHT_BROWSER || findBrowser();
  return chromium.launch({ headless: true, ...(executablePath ? { executablePath } : {}), ...options });
}

function findBrowser() {
  const guesses = [
    '/opt/pw-browsers/chromium',
    process.env.PLAYWRIGHT_BROWSERS_PATH,
    '/opt/pw-browsers',
  ].filter(Boolean);

  for (const base of guesses) {
    if (!existsSync(base)) continue;
    // A guess may name the executable itself; only a file can be one.
    if (statSync(base).isFile()) return base;
    for (const entry of readdirSync(base).filter(
           (name) => !name.startsWith('ffmpeg') && !name.includes('headless'))) {
      for (const suffix of ['chrome-linux/chrome',
                            'chrome-win64/chrome.exe', 'chrome-win/chrome.exe',
                            'chrome-mac/Chromium.app/Contents/MacOS/Chromium']) {
        const full = path.join(base, entry, suffix);
        if (existsSync(full)) return full;
      }
    }
  }
  return '';        // let Playwright use whatever it installed for itself
}

/* -- saying what happened -------------------------------------------------- */

let failures = 0;

export function ok(label, condition, detail) {
  console.log(`${condition ? 'PASS' : 'FAIL'}  ${label}`
    + (!condition && detail ? ` → ${detail}` : ''));
  if (!condition) {
    failures += 1;
    process.exitCode = 1;
  }
  return Boolean(condition);
}

export function failed() {
  return failures;
}

/** Close up and print the tally. */
export async function done(browser) {
  await browser?.close();
  console.log(failures ? `\n${failures} failed` : '\nall good');
  return failures;
}

/* -- signing in, which every suite does the same way ---------------------- */

/** Sign in to the family app by posting, then reload so the page has it. */
export async function signInAtHome(page, user = ADMIN_USER, pass = ADMIN_PASS) {
  await page.goto(HOME, { waitUntil: 'networkidle' });
  await page.evaluate(async ([username, password]) => {
    await fetch('/api/auth/login', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
  }, [user, pass]);
  await page.reload({ waitUntil: 'networkidle' });
  return page;
}

/** Sign in to the console through its own gate, the way a person does. */
export async function signInAtConsole(page, user = ADMIN_USER, pass = ADMIN_PASS) {
  await page.goto(ADMIN, { waitUntil: 'networkidle' });
  await page.fill('#gate input[type="text"]', user);
  await page.fill('#gate input[type="password"]', pass);
  await page.click('#gate .btn.primary');
  await page.waitForSelector('.admin-person', { state: 'attached', timeout: 20000 });
  return page;
}

/** Is there an instance to test against? */
export async function reachable(url = HOME, timeoutMs = 2000) {
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    const response = await fetch(url, { signal: controller.signal });
    clearTimeout(timer);
    return response.ok || response.status === 401 || response.status === 403;
  } catch {
    return false;
  }
}
