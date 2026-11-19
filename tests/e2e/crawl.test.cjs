// Every page a role can reach from the navigation must render: no server error on
// real MySQL, and with the structure a screen reader needs. Complements journey.test.cjs (which walks one flow in depth) with
// breadth: it follows the sidebar, the sub-navigation and tab bars for each role.
//
//   BASE_URL=http://127.0.0.1:5001 node --test tests/e2e/crawl.test.cjs
//
// It only reads (GET), so it can run before or after the journey on the same seed.
const test = require('node:test');
const assert = require('node:assert/strict');
const { chromium } = require('playwright');

const BASE = process.env.BASE_URL || 'http://127.0.0.1:5001';
const PASSWORD = process.env.E2E_PASSWORD || 'test';
// Never followed: anything that changes state or hands back a file.
const SKIP = /logout|\/slet|\/fjern|export|download|\.ics|\.csv|\/api\/|mine-data|\/ui\b|impersonat|\/vendor/;
const NAV = '#fmSide a[href^="/"], .pg-subnav a[href^="/"], .pg-tab[href^="/"], nav a[href^="/"]';
// Structure a screen reader needs, checked on every page: a level-1 heading, a name for
// every visible field and control. No page is exempt.
function a11yFindings() {
  const out = [];
  const visible = (e) => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const headings = document.querySelectorAll('h1, [role=heading][aria-level="1"]').length;
  if (headings === 0) out.push('no level-1 heading');
  if (document.querySelectorAll('main').length !== 1) out.push('not exactly one <main>');
  document.querySelectorAll('img').forEach((i) => { if (!i.hasAttribute('alt')) out.push('image without alt'); });
  document.querySelectorAll('input:not([type=hidden]):not([type=submit]):not([type=button]), select, textarea').forEach((e) => {
    if (!visible(e)) return;
    const named = e.getAttribute('aria-label') || e.getAttribute('aria-labelledby') || e.getAttribute('title')
      || (e.id && document.querySelector('label[for="' + CSS.escape(e.id) + '"]')) || e.closest('label');
    if (!named) out.push('field without a name: ' + (e.name || e.id || e.type));
  });
  document.querySelectorAll('button, a.btn, a.fm-icon-btn').forEach((e) => {
    if (visible(e) && !(e.textContent || '').trim() && !e.getAttribute('aria-label') && !e.getAttribute('title')) out.push('control without a name');
  });
  return out;
}

// role -> the fewest pages a healthy navigation yields for it on the demo tenant
const ROLES = { ada: 12, leder: 20, hr: 35, test: 55 };

let browser;
test.before(async () => { browser = await chromium.launch(); });
test.after(async () => { await browser.close(); });

for (const [user, minimum] of Object.entries(ROLES)) {
  test(`${user}: every page in the navigation renders`, async () => {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, locale: 'da-DK' });
    context.setDefaultTimeout(15000);
    await context.route('**/*', (route) => (route.request().url().startsWith(BASE) ? route.continue() : route.abort()));
    const page = await context.newPage();
    const go = (path) => page.goto(BASE + path, { waitUntil: 'domcontentloaded' });
    await go('/login');
    await page.fill('#u', user);
    await page.fill('#p', PASSWORD);
    await page.press('#p', 'Enter');
    await page.waitForURL((u) => !String(u).includes('/login'), { waitUntil: 'domcontentloaded' });

    const seen = new Set();
    const queue = [page.url().replace(BASE, '')];
    const failures = [];
    const structure = [];
    let visited = 0;
    while (queue.length && visited < 160) {
      const path = queue.shift();
      if (seen.has(path)) continue;
      seen.add(path);
      if (SKIP.test(path)) continue;
      const response = await go(path);
      visited += 1;
      if (!response || response.status() >= 500) failures.push(`${response ? response.status() : 'no response'} ${path}`);
      if (response && response.status() < 400) {
        for (const finding of await page.evaluate(a11yFindings)) structure.push(`${path}: ${finding}`);
      }
      const links = await page.$$eval(NAV, (as) => as.map((a) => a.getAttribute('href').split('#')[0]));
      for (const link of links) if (!seen.has(link)) queue.push(link);
    }
    await context.close();
    assert.deepEqual(failures, [], `${user} got server errors`);
    assert.deepEqual(structure, [], `${user}: pages a screen reader cannot use properly`);
    assert.ok(visited >= minimum, `${user} reached only ${visited} pages; the navigation lost entries`);
  });
}
