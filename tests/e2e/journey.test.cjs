// Browser journey over the sandbox demo tenant: employee -> HR -> booking ->
// attendance -> outcome, plus goal privacy. Not part of the offline pytest suite:
// it needs the sandbox server (see tests/e2e/README.md).
//
//   BASE_URL=http://127.0.0.1:5001 node --test tests/e2e/
//
// The steps run in order and share one order; a failed step fails the ones after it.
const test = require('node:test');
const assert = require('node:assert/strict');
const { chromium } = require('playwright');

const BASE = process.env.BASE_URL || 'http://127.0.0.1:5001';
const PASSWORD = process.env.E2E_PASSWORD || 'test';
const COURSE = 'demo-python-dataanalyse';
const COURSE_TITLE = 'Python til dataanalyse';

let browser;
const state = { orderId: null, privateGoal: 'E2E privat mål ' + Date.now(), sharedGoal: 'E2E delt mål ' + Date.now() };
const serverErrors = [];

async function as(user) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, locale: 'da-DK' });
  // Only the app itself: the journey must not depend on a CDN or a font host.
  await context.route('**/*', (route) => (route.request().url().startsWith(BASE) ? route.continue() : route.abort()));
  context.setDefaultTimeout(12000);
  const page = await context.newPage();
  page.on('response', (r) => { if (r.status() >= 500) serverErrors.push(r.status() + ' ' + r.url()); });
  page.go = (path) => page.goto(BASE + path, { waitUntil: 'domcontentloaded' });
  await page.go('/login');
  await page.fill('#u', user);
  await page.fill('#p', PASSWORD);
  await page.press('#p', 'Enter');
  await page.waitForURL((u) => !String(u).includes('/login'), { waitUntil: 'domcontentloaded' });
  page.close_ = () => context.close();
  return page;
}

// Submit the form that holds `locator` and wait for the page that follows.
async function submit(page, locator) {
  await Promise.all([page.waitForLoadState('domcontentloaded'), locator.click()]);
  await page.waitForLoadState('domcontentloaded');
}

function localDateTime(daysFromNow, hour) {
  const d = new Date(Date.now() + daysFromNow * 86400000);
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(hour)}:00`;
}

test.before(async () => { browser = await chromium.launch(); });
test.after(async () => { await browser.close(); });

test('employee requests a course and sees it awaiting approval', async () => {
  const page = await as('ada');
  await page.go('/products/' + COURSE);
  assert.ok(await page.locator('[data-pd-open]:visible').count(),
    'the course cannot be requested: run on a freshly seeded sandbox (see tests/e2e/README.md)');
  await page.locator('[data-pd-open]:visible').first().click();
  await page.locator('#pd-request-form').waitFor({ state: 'visible' });
  await page.fill('#rq-note', 'Bestilt af browser-testen');
  await submit(page, page.locator('#pd-request-form button:has-text("Send anmodning")'));
  const order = new URL(page.url()).searchParams.get('order');
  assert.ok(order, 'the course page should come back with the new order id, got ' + page.url());
  state.orderId = order;
  await page.go('/min-ordre/' + order);
  await assert.doesNotReject(page.getByText('Afventer godkendelse').first().waitFor());
  assert.equal(await page.locator('[data-section="book_form"]').count(), 0, 'a learner never gets the booking form');
  await page.close_();
});

test('HR approves the request from the approvals list', async () => {
  const page = await as('hr');
  await page.go('/hr/approvals');
  const row = page.locator(`[data-order-id="${state.orderId}"], .fm-card:has-text("${COURSE_TITLE}")`).filter({ hasText: 'Godkend' }).first();
  await row.getByRole('button', { name: 'Godkend' }).click();
  const confirm = page.getByRole('button', { name: /^(Godkend|Bekræft)/ }).last();
  if (await confirm.isVisible().catch(() => false)) await confirm.click().catch(() => {});
  await page.waitForTimeout(1500);
  await page.go('/hr/order/' + state.orderId + '/details');
  await assert.doesNotReject(page.locator('[data-section="book_form"]').waitFor(), 'an approved order is ready for booking');
  await page.close_();
});

test('HR books it; the course took place earlier than the ordered session', async () => {
  const page = await as('hr');
  await page.go('/hr/order/' + state.orderId + '/details');
  const form = page.locator('[data-section="book_form"]');
  await form.locator('[name="start_at"]').fill(localDateTime(-3, 9));
  await form.locator('[name="location"]').fill('Online');
  await form.locator('[name="reference"]').fill('E2E-REF-1');
  // Without the tick the app must refuse a date outside the ordered session.
  await submit(page, form.getByRole('button', { name: 'Bekræft booking' }));
  await assert.doesNotReject(page.getByText('Datoen afviger fra den bestilte session').first().waitFor());
  const again = page.locator('[data-section="book_form"]');
  await again.locator('[name="start_at"]').fill(localDateTime(-3, 9));
  await again.locator('[name="location"]').fill('Online');
  await again.locator('[name="reference"]').fill('E2E-REF-1');
  await again.locator('[name="confirm_date_change"]').check();
  await submit(page, again.getByRole('button', { name: 'Bekræft booking' }));
  await assert.doesNotReject(page.getByText('E2E-REF-1').first().waitFor());
  await page.close_();
});

test('employee sees the booking and reports attendance', async () => {
  const page = await as('ada');
  await page.go('/min-ordre/' + state.orderId);
  await assert.doesNotReject(page.getByText('E2E-REF-1').first().waitFor());
  const note = page.locator('[name="evidence_note"]');
  await note.fill('Deltog hele dagen. Kursusbevis følger.');
  await submit(page, page.getByRole('button', { name: 'Indsend deltagelse til bekræftelse' }));
  // A report is not completion: the order stays booked until HR verifies it.
  await page.go('/min-tidslinje');
  const card = page.locator(`a[href*="${state.orderId}"]`).first();
  assert.doesNotMatch(await card.innerText(), /Gennemført/);
  await page.close_();
});

test('HR verifies attendance and the order is completed', async () => {
  const page = await as('hr');
  await page.go('/hr/order/' + state.orderId + '/details');
  const verify = page.locator('form:has([name="action"][value="verify"])');
  await verify.locator('[name="note"]').fill('Bekræftet med udbyderen');
  await submit(page, verify.getByRole('button', { name: 'Bekræft gennemført kursus' }));
  await page.go('/hr/order/' + state.orderId + '/details');
  await assert.doesNotReject(page.getByText('Gennemført').first().waitFor());
  await page.close_();
});

test('the completed course reaches the employee: orders, CV and development workspace', async () => {
  const page = await as('ada');
  await page.go('/min-ordre/' + state.orderId);
  await assert.doesNotReject(page.getByText('Gennemført').first().waitFor());
  await assert.doesNotReject(page.getByRole('link', { name: /Se dine kompetencer og mål/ }).first().waitFor());
  await page.go('/profil/cv');
  await assert.doesNotReject(page.getByText(COURSE_TITLE).first().waitFor());
  await page.go('/udvikling');
  assert.ok(await page.getByRole('link', { name: 'Find læring' }).count() > 0, 'an unmet requirement links to the catalogue');
  // The company uses internal mobility, so the sidebar offers it and the page lights its entry.
  await page.locator('#fmSide').getByRole('link', { name: 'Interne muligheder' }).click();
  await page.waitForURL('**/muligheder');
  await assert.doesNotReject(page.getByText('Teamleder til Salg Øst').first().waitFor());
  assert.equal(await page.locator('#fmSide a[aria-current="page"]').innerText(), 'Interne muligheder');
  // A course that is about one of the person's own gaps says so, and the CV leads on to opportunities.
  await page.go('/products/demo-ledelse-grundforloeb');
  await assert.doesNotReject(page.locator('.pd-gaps', { hasText: 'Ledelse' }).waitFor());
  await page.go('/profil/cv');
  await assert.doesNotReject(page.getByRole('link', { name: 'Se interne muligheder' }).first().waitFor());
  await page.go('/min-laering');
  await assert.doesNotReject(page.getByRole('navigation', { name: 'Dit næste skridt' }).getByRole('link', { name: 'Min udvikling' }).waitFor());
  await page.close_();
});

test('a goal stays private until the employee shares it, and HR sees only the shared one', async () => {
  const ada = await as('ada');
  for (const title of [state.privateGoal, state.sharedGoal]) {
    await ada.go('/mine-maal');
    await ada.fill('input[name="title"]', title);
    await submit(ada, ada.getByRole('button', { name: 'Tilføj' }));
  }
  await ada.go('/mine-maal');
  const card = ada.locator('.goal-card', { hasText: state.sharedGoal });
  await assert.doesNotReject(card.getByText('Privat · kun du kan se det').waitFor());
  await submit(ada, card.getByRole('button', { name: 'Del med HR' }));
  await assert.doesNotReject(ada.locator('.goal-card', { hasText: state.sharedGoal }).getByText('Delt med HR').waitFor());
  await ada.close_();

  const hr = await as('hr');
  // HR gets there from the notification that the goal was shared.
  await hr.go('/notifications');
  const link = hr.locator('a[href*="/hr/employee/"][href*="/goals"]').first();
  await assert.doesNotReject(link.waitFor(), 'HR is notified when a goal is shared, with a link to the goals page');
  await hr.go(new URL(await link.getAttribute('href'), BASE).pathname);
  const body = await hr.locator('main, body').first().innerText();
  assert.ok(body.includes(state.sharedGoal), 'HR sees the goal the employee shared');
  assert.ok(!body.includes(state.privateGoal), 'HR must never see the private goal');
  await hr.close_();
});

test('cancelling a booked course is a request until HR accepts it, with the agreed fee kept', async () => {
  // The seeded booked order: Ada, "Salgstræning i praksis", reference DEMO-2041, 8.500 kr.
  const ada = await as('ada');
  await ada.go('/min-tidslinje');
  const href = await ada.locator('a[href*="/min-ordre/"]', { hasText: 'Salgstræning i praksis' }).first().getAttribute('href');
  const seeded = /min-ordre\/([^#?]+)/.exec(href)[1];
  await ada.go('/min-ordre/' + seeded);
  const request = ada.locator('form[data-change-form]');
  await request.locator('[name="kind"]').selectOption('cancel');
  await request.locator('[name="note"]').fill('Kan ikke deltage alligevel');
  await submit(ada, request.getByRole('button', { name: 'Send ændringsønske' }));
  await ada.go('/min-ordre/' + seeded);
  await assert.doesNotReject(ada.getByText('DEMO-2041').first().waitFor(), 'the booking stands while the request is open');
  assert.equal(await ada.getByText('Annulleret', { exact: true }).count(), 0, 'a request is not a cancellation');
  await ada.close_();

  const hr = await as('hr');
  await hr.go('/hr/budgets');
  const before = await hr.locator('body').innerText();
  await hr.go('/hr/order/' + seeded + '/details');
  const resolve = hr.locator('form:has([name="action"][value="resolve"])');
  await resolve.locator('[name="note"]').fill('Aftalt med udbyderen: gebyr 1.000 kr.');
  await resolve.locator('[name="fee"]').fill('1000');
  await submit(hr, resolve.getByRole('button', { name: 'Accepter ændring' }));
  await hr.go('/hr/order/' + seeded + '/details');
  await assert.doesNotReject(hr.getByText('Annulleret').first().waitFor());
  await hr.go('/hr/budgets');
  const after = await hr.locator('body').innerText();
  assert.notEqual(after, before, 'the released amount shows on the budget page');
  await hr.close_();

  const again = await as('ada');
  await again.go('/min-ordre/' + seeded);
  await assert.doesNotReject(again.getByText('Annulleret').first().waitFor());
  await again.close_();
});

test('search finds a course, the person\'s own order and own goal, and the skip link comes first', async () => {
  const page = await as('ada');
  await page.go('/min-laering');
  await page.keyboard.press('Tab');
  assert.equal(await page.evaluate(() => document.activeElement.textContent.trim()), 'Spring til indhold');
  await page.getByRole('button', { name: 'Søg' }).click();
  await page.fill('#cmdkInput', 'python');
  await assert.doesNotReject(page.locator('#cmdkList .cmdk-group', { hasText: 'Kurser' }).waitFor());
  await assert.doesNotReject(page.locator('#cmdkList .cmdk-group', { hasText: 'Mine bestillinger' }).waitFor());
  await page.fill('#cmdkInput', 'E2E delt');
  await assert.doesNotReject(page.locator('#cmdkList .cmdk-group', { hasText: 'Mine mål' }).waitFor());
  await page.close_();
});

test('the course provider sees its orders in the vendor portal, in the theme it chose', async () => {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, locale: 'da-DK' });
  context.setDefaultTimeout(12000);
  await context.route('**/*', (route) => (route.request().url().startsWith(BASE) ? route.continue() : route.abort()));
  const page = await context.newPage();
  page.on('response', (r) => { if (r.status() >= 500) serverErrors.push(r.status() + ' ' + r.url()); });
  await page.goto(BASE + '/vendor/login', { waitUntil: 'domcontentloaded' });
  await page.fill('input[name="email"]', 'udbyder@sandbox.example');
  await page.fill('input[name="password"]', PASSWORD);
  await page.press('input[name="password"]', 'Enter');
  await page.waitForURL('**/vendor/', { waitUntil: 'domcontentloaded' });
  for (const [path, tab] of [['/vendor/', 'Oversigt'], ['/vendor/orders', 'Bestillinger'], ['/vendor/analytics', 'Analyser'],
    ['/vendor/submit-catalog', 'Indsend katalog'], ['/vendor/profile', 'Profil']]) {
    const response = await page.goto(BASE + path, { waitUntil: 'domcontentloaded' });
    assert.equal(response.status(), 200, path);
    assert.match(await page.locator('.vp-nav a[aria-current="page"]').innerText(), new RegExp('^' + tab));
  }
  await page.goto(BASE + '/vendor/orders', { waitUntil: 'domcontentloaded' });
  await assert.doesNotReject(page.getByText('Salgstræning i praksis').first().waitFor(), 'the approved order waits for the provider');
  await page.getByRole('button', { name: /tema/i }).click();
  assert.equal(await page.evaluate(() => document.documentElement.getAttribute('data-theme')), 'dark');
  await page.goto(BASE + '/vendor/profile', { waitUntil: 'domcontentloaded' });
  assert.equal(await page.evaluate(() => document.documentElement.getAttribute('data-theme')), 'dark', 'the choice is remembered');
  // A vendor session is not a company session.
  const company = await page.goto(BASE + '/hr/approvals', { waitUntil: 'domcontentloaded' });
  assert.ok(!page.url().includes('/hr/approvals') || company.status() >= 400, 'a vendor never reaches company pages');
  await context.close();
});

test('another employee cannot open the order, and nothing answered with a server error', async () => {
  const page = await as('medarbejder');
  const response = await page.go('/min-ordre/' + state.orderId);
  const text = await page.locator('body').innerText();
  assert.ok(response.status() === 404 || response.status() === 403 || !text.includes('E2E-REF-1'),
    'a colleague in another department must not see the booking');
  await page.close_();
  assert.deepEqual(serverErrors, []);
});
