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

test('another employee cannot open the order, and nothing answered with a server error', async () => {
  const page = await as('medarbejder');
  const response = await page.go('/min-ordre/' + state.orderId);
  const text = await page.locator('body').innerText();
  assert.ok(response.status() === 404 || response.status() === 403 || !text.includes('E2E-REF-1'),
    'a colleague in another department must not see the booking');
  await page.close_();
  assert.deepEqual(serverErrors, []);
});
