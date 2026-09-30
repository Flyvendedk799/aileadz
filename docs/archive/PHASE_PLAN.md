# AiLeadZ — 3-Phase Completion Plan

Based on full platform audit (March 2026). Organized by impact and dependency order.

---

## Phase 1: Fix What's Broken (data integrity, broken flows, dead ends)

The platform is ~75% functional but has data gaps and dead-end UX paths that undermine trust.

### 1.1 — Fix data integration: SQLite ↔ MySQL split
**Problem:** Chatbot logs analytics to SQLite (`memory_store.py`) but dashboards read from MySQL. HR dashboard shows "0" for chatbot metrics even though the chatbot is being used.
**Fix:** Ensure all chatbot interactions, tool usage, feedback ratings, and search analytics write directly to MySQL `chatbot_interactions` table (which already exists). Remove SQLite-only analytics paths.
**Files:** `app1/agent.py`, `app1/memory_store.py`, `app1/__init__.py`
**Impact:** HR dashboards finally show real data. Trending topics, tool usage, feedback scores all become live.

### 1.2 — Fix feedback loop: frontend → MySQL
**Problem:** Users can thumbs-up/down chatbot responses. The frontend POSTs to `/app1/feedback` but this doesn't update the `chatbot_interactions.feedback_rating` in MySQL.
**Fix:** Wire the feedback endpoint to UPDATE the corresponding `chatbot_interactions` row.
**Files:** `app1/__init__.py` (feedback route)
**Impact:** HR dashboard feedback scores become real. Currently always shows 0/5.

### 1.3 — Link chatbot sessions to orders
**Problem:** When a user creates an order through the chatbot, there's no `chatbot_session_id` linking back to the conversation that led to it. HR can't see which chatbot conversations convert.
**Fix:** Pass the current session ID when `create_course_order` is called. The `course_orders` table already has `chatbot_session_id` and `chatbot_queries_before_order` columns — just populate them.
**Files:** `app1/tools.py` (`create_course_order` function), `app1/agent.py`
**Impact:** Conversion tracking works. ROI dashboard can show "chatbot-influenced orders".

### 1.4 — Fix broken/dead-end navigation links
**Problem:** Several sidebar links lead to 404 or empty pages.
- `/app1/adminlog` — referenced in sidebar but no route
- Profile page (`/profile`) — template exists but may not render correctly for company employees
- `/about`, `/analytics`, `/indstillinger` — exist but are generic/placeholder

**Fix:** Either wire up the routes properly or remove the dead links. Priority: make `/profile` work correctly for company employees (show their company context, skills vs. targets).
**Files:** `pages.py`, `app1/__init__.py`, `templates/base.html`

### 1.5 — Clean up orphan templates
**Problem:** ~8 templates are unused or duplicated: `hr_dashboard/employees.html` and `hr_dashboard/add_employee.html` (duplicates of companies/ versions), `app2/`, `app3/` templates, `order_detail.html` (root), `hr_dashboard.html` (root).
**Fix:** Delete or consolidate. Reduces confusion for future development.

---

## Phase 2: Core Experience Gaps (onboarding, course browsing, profile)

These are the "obvious missing features" — things a user expects but doesn't find.

### 2.1 — Post-registration onboarding checklist
**Problem:** After company registration, admin lands on the company dashboard with zero guidance. No prompt to add employees, set budgets, configure departments, or try the chatbot.
**Fix:** Add an onboarding checklist card to the company dashboard (shown until dismissed). Steps:
1. ✅ Create company (done)
2. Add your first department
3. Add your first employee
4. Set training budgets
5. Try the HR AI chatbot
6. Explore the employee chatbot

Track completion in `companies` table (`onboarding_completed` JSON field or similar).
**Files:** `companies/__init__.py` (dashboard route), `templates/companies/dashboard.html`

### 2.2 — Employee first-login experience
**Problem:** When HR adds an employee and they log in for the first time, they land directly in the chatbot with no context. They don't know what the platform does or what they should do first.
**Fix:** Add a first-login welcome modal/overlay in `app1/templates/index.html`:
- "Velkommen til AiLeadZ" — brief explanation (3 bullets max)
- "Start med at fortælle mig om dig selv" — triggers profile-building conversation
- "Eller udforsk kurser direkte" — triggers course search
- Mark `first_login_completed` in `company_users` so it only shows once.

**Files:** `app1/templates/index.html`, `app1/__init__.py`, `company_users` table (add column)

### 2.3 — Course catalog page (browse without chatbot)
**Problem:** Employees can ONLY discover courses by talking to the chatbot. There's no way to browse, filter, or search courses independently. This is a major gap — many users prefer self-service browsing.
**Fix:** Create a `/courses` page with:
- Search bar with text input
- Filter sidebar: category, price range, location, format (online/physical), date
- Course cards in a grid (reuse the card design from the chatbot)
- Click to see details → "Spoerg chatbot om dette kursus" button to continue in chat
- Data source: same `shopify_products_augmented.json` or product database the chatbot uses

**Files:** New route in `app1/__init__.py` or new blueprint, new template `templates/courses.html`

### 2.4 — Guided profile completion
**Problem:** Profile page exists (`/profile`) with skills, experience, education, courses, and summary sections. But there's no encouragement to complete it, and employees don't understand why it matters.
**Fix:**
- Add a profile completeness indicator to the chatbot sidebar (progress ring or percentage)
- When profile is <50% complete, chatbot proactively suggests: "Tip: Udfyld din profil for bedre anbefalinger"
- Profile page: add "Hvorfor udfylde din profil?" tooltip explaining that it enables personalized course recommendations and skill gap analysis
- Connect profile skills to `employee_skills_matrix` so HR can see skill coverage

**Files:** `templates/profile.html`, `app1/templates/index.html` (sidebar), `api.py` (profile completeness endpoint)

### 2.5 — Email notifications for key events
**Problem:** No emails are sent for anything — not registration confirmation, not employee invitation, not order approval/rejection, not course reminders.
**Fix:** Add email sending for the highest-impact events (use Flask-Mail or SMTP directly):
1. Welcome email on company registration (admin)
2. Invitation email when HR adds an employee (with login credentials)
3. Order status change (approved/rejected) → employee notification
4. Weekly digest for HR: pending approvals, budget alerts, new orders

**Files:** New `email_service.py`, modifications to `companies/__init__.py`, `hr_dashboard/__init__.py`

---

## Phase 3: Engagement & Intelligence (make the platform sticky)

These features differentiate AiLeadZ from a simple course catalog.

### 3.1 — Learning path assignment & tracking UI
**Problem:** `learning_paths` and `employee_learning_progress` tables exist in the database, and the chatbot can `suggest_learning_path`. But HR has no UI to create, assign, or track learning paths for teams.
**Fix:** Add to HR dashboard:
- "Laeringsforloeb" page: create paths (name, courses in sequence, target roles/departments)
- Assign paths to employees or departments
- Track progress: who started, who completed, who's stuck
- Notification when employee completes a path

**Files:** New routes in `hr_dashboard/__init__.py`, new template `templates/hr_dashboard/learning_paths.html`

### 3.2 — Proactive chatbot nudges
**Problem:** The chatbot is reactive — it waits for the user to ask. It should proactively engage based on data.
**Fix:** When a user opens the chatbot, check:
- Upcoming course deadlines → "Du har et kursus der starter om 5 dage"
- Incomplete profile → "Tilfoej dine kompetencer for bedre anbefalinger"
- Skill gaps (if company has targets) → "Din afdeling mangler kompetencer inden for X"
- New courses matching their profile → "Nyt kursus der matcher dine maal"
- Order status update → "Din kursusordre er blevet godkendt"

Show as a dismissible alert bar above the chat input.
**Files:** `app1/__init__.py` (new endpoint), `app1/templates/index.html`

### 3.3 — HR dashboard: actionable insights with one-click actions
**Problem:** AI Insights section generates text insights but they're not actionable. HR reads "3 employees haven't logged in for 30 days" but can't click to do anything about it.
**Fix:** Make insights clickable:
- "3 inaktive medarbejdere" → click to see list, send reminder email
- "Budget 85% brugt i IT" → click to adjust budget
- "Kompetencegab i ledelse" → click to see recommended courses for the team
- "5 ordrer afventer godkendelse" → click to go to approvals page

**Files:** `hr_dashboard/__init__.py` (generate_insights route), `templates/hr_dashboard/dashboard.html`

### 3.4 — Manager view for department heads
**Problem:** Department heads (role: `department_head`) have no dedicated view. They're stuck between the employee chatbot and the full HR dashboard (which they may not have access to).
**Fix:** Add a lightweight "Min afdeling" view accessible to department heads:
- Their department's employees and training status
- Budget remaining for their department
- Pending approvals they need to action
- Top skill gaps in their team
- Link to chatbot for course recommendations

**Files:** New route, new template, or filtered view of HR dashboard

### 3.5 — Analytics: chatbot conversion funnel
**Problem:** No visibility into the funnel: chatbot opened → courses shown → course clicked → order created → order completed. This is the core business metric.
**Fix:** Add a conversion funnel visualization to the HR analytics:
- Sessions this month
- Sessions with course shown
- Sessions with order created
- Orders completed
- Revenue attributed to chatbot

Data already exists across `chatbot_interactions` and `course_orders` — just needs a query and visualization.
**Files:** `hr_dashboard/__init__.py` (analytics route), new chart in dashboard or analytics page

---

## Priority Summary

| # | Item | Effort | Impact | Phase |
|---|------|--------|--------|-------|
| 1.1 | SQLite→MySQL data sync | Medium | Critical | 1 |
| 1.2 | Feedback → MySQL | Low | High | 1 |
| 1.3 | Session→Order linking | Low | High | 1 |
| 1.4 | Fix dead navigation links | Low | Medium | 1 |
| 1.5 | Clean orphan templates | Low | Low | 1 |
| 2.1 | Onboarding checklist | Medium | High | 2 |
| 2.2 | Employee first-login | Low | High | 2 |
| 2.3 | Course catalog page | High | Critical | 2 |
| 2.4 | Guided profile completion | Medium | Medium | 2 |
| 2.5 | Email notifications | Medium | High | 2 |
| 3.1 | Learning path UI | High | High | 3 |
| 3.2 | Proactive chatbot nudges | Medium | High | 3 |
| 3.3 | Actionable HR insights | Medium | Medium | 3 |
| 3.4 | Department head view | Medium | Medium | 3 |
| 3.5 | Conversion funnel analytics | Medium | High | 3 |
