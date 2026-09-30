# AiLeadZ - Chatbot x Dashboard Integration Plan

## Current State (March 2026)

Core chatbot works. Orders work. Dashboards exist but show partial data.
~60% of dashboard capability is functional. The remaining 40% is empty tables,
siloed data (SQLite vs MySQL), and missing pipelines.

---

## Phase 1: Fix the Data Foundation (Priority: CRITICAL)

Everything else depends on complete, accurate data flowing from chatbot to dashboards.

### 1.1 Enrich chatbot_interactions INSERT

**What:** Every chat interaction should capture the full picture, not just query/response.

**Fields to add to the INSERT in agent.py:**
- `tools_used` (VARCHAR) — comma-separated tool names called during this interaction
- `tool_results_count` (INT) — how many course results were shown
- `products_shown` (TEXT) — JSON list of product handles shown
- `feedback_rating` (TINYINT) — synced from frontend feedback endpoint
- `conversation_depth` (INT) — message count in this session so far
- `is_logged_in` (BOOL) — was user authenticated
- `referrer_url` (VARCHAR) — where did they come from

**Schema change:** ALTER TABLE chatbot_interactions ADD these columns.

**Why:** Admin dashboard can then show tool usage breakdown, conversion funnel
(shown products -> orders), and feedback scores. HR dashboard gets engagement depth.

### 1.2 Sync feedback to MySQL

**What:** The /app1/feedback endpoint currently writes to SQLite only. Add a MySQL
INSERT/UPDATE to chatbot_interactions after the SQLite log.

**How:** In app1/__init__.py feedback route, after log_event(), also:
```python
cur.execute("""
    UPDATE chatbot_interactions
    SET feedback_rating = %s
    WHERE session_id = %s AND username = %s
    ORDER BY created_at DESC LIMIT 1
""", (rating, session_id, username))
```

**Why:** Dashboards can show satisfaction trends, identify bad responses, and
correlate feedback with specific query types.

### 1.3 Populate user_location

**What:** Detect user location from request headers (IP geolocation or Accept-Language)
and set it on the chatbot_interactions INSERT.

**How:** Use request.remote_addr with a lightweight GeoIP lookup, or fall back to
company_users.department as a proxy for "location" in enterprise context.

**Why:** The admin dashboard already has a location chart — it just shows "Ukendt".

### 1.4 Bridge SQLite analytics to MySQL

**What:** The SQLite analytics table in memory_store.py has tool usage, latency,
search queries, and feedback that dashboards can't see.

**Options:**
- A) Eliminate SQLite for analytics entirely — log everything to MySQL (simplest)
- B) Add a periodic sync job (complex, fragile)
- Recommend A for production.

**Why:** Single source of truth. No data lives in an ephemeral file on one server.

---

## Phase 2: Conversion Attribution & Order Intelligence

### 2.1 Link chatbot sessions to orders

**What:** When create_course_order is called, tag the order with the session_id.

**Schema:** course_orders already has company_id/user_id. Add:
- `chatbot_session_id` (VARCHAR) — which chat session triggered this order
- `chatbot_queries_before_order` (INT) — how many messages before they bought
- `recommended_by_tool` (VARCHAR) — which tool surfaced the product (search, filter, recommend)

**Why:** Enables true conversion funnel: impressions -> clicks -> orders.
HR sees which chatbot interactions actually drive training spend.
Admin sees which recommendation strategies convert best.

### 2.2 Approval workflows for enterprise orders

**What:** When a company employee orders a course, it shouldn't auto-confirm.
Flow: Employee orders -> Manager gets notification -> Manager approves/rejects -> Order proceeds.

**Implementation:**
- New `order_approvals` table (order_id, approver_user_id, status, requested_at, decided_at, notes)
- New chatbot tool: `check_order_approval_status`
- New HR dashboard section: "Pending Approvals" with approve/reject buttons
- Email/notification to manager on new order
- Budget check: does this department have remaining training budget?

**Why:** No enterprise client will accept employees ordering courses without oversight.
This is table-stakes for B2B.

### 2.3 Department budget tracking

**What:** Each department gets a training budget. Orders deduct from it.
HR/managers see remaining budget per department.

**Schema:**
- `department_budgets` (company_id, department, annual_budget, spent, fiscal_year)
- Update spent on order completion

**Dashboard:** Budget utilization chart per department. Alerts when >80% spent.

---

## Phase 3: Smart Analytics & Insights

### 3.1 Skill gap analysis engine

**What:** Compare company employees' skills (from user_profile_db) against
industry benchmarks or company-defined target skill profiles.

**Implementation:**
- New `company_skill_targets` table (company_id, department, skill_name, target_level, priority)
- New chatbot tool: `analyze_skill_gaps` — returns personalized recommendations
- New HR dashboard widget: "Skill Gap Heatmap" (departments x skills, red/yellow/green)
- Auto-detect skills from chatbot conversations (when user mentions "I know X" or searches for Y)

**Why:** This is the killer feature for enterprise. HR buys this platform to close
skill gaps, not just to buy courses. Show them the gap AND the solution.

### 3.2 AI-powered conversation insights

**What:** Batch-process chatbot_interactions to extract:
- Top emerging skill interests (trending topics)
- Common pain points (repeated questions without orders)
- Department-level interest patterns
- Sentiment trends over time

**Implementation:**
- Nightly job (or on-demand) that runs GPT-4o-mini over recent interactions
- Stores results in `company_insights` table
- HR dashboard: "AI Insights" card with natural language summaries
  e.g. "IT-afdelingen har vist stigende interesse for cybersecurity de sidste 2 uger.
  3 medarbejdere har soegt efter CISSP-certificering."

**Why:** Transforms raw chat logs into actionable intelligence. This is what
justifies premium pricing.

### 3.3 ROI tracking

**What:** Track training outcomes: did the employee who completed a course
show improved chatbot engagement, skills growth, or performance?

**Dashboard metrics:**
- Training spend per employee
- Courses completed per kr spent
- Skill level improvements post-training
- Time-to-competency (how long from enrollment to course completion)
- Department ROI comparison

**Why:** CFOs approve training budgets. Give them numbers.

### 3.4 Predictive analytics

**What:** Use historical data to predict:
- Which employees are likely to need training soon (based on role, tenure, skill gaps)
- Which courses will be popular next quarter (based on search trends)
- Churn risk (employees who stopped using the platform)

**Implementation:** The enterprise_analytics module already imports sklearn.
Wire it to real data: IsolationForest for anomaly detection, KMeans for
employee clustering, simple regression for trend prediction.

**Why:** Proactive beats reactive. "3 employees in IT are at risk of skill
obsolescence" is worth more than a dashboard showing past completions.

---

## Phase 4: Enterprise Integration Layer

### 4.1 Real SSO implementation

**What:** enterprise_sso has the skeleton. Wire it up.

**Priority providers:**
1. Microsoft Entra ID (Azure AD) — covers 80% of enterprise clients
2. Google Workspace
3. Okta/Auth0
4. Generic SAML 2.0

**Why:** No enterprise client will create individual accounts. SSO is a hard
requirement for any company with >50 employees.

### 4.2 REST API for external integrations

**What:** enterprise_api has auth + rate limiting. Add real endpoints:

**Endpoints needed:**
- GET /api/v1/employees — list company employees
- GET /api/v1/employees/{id}/training — training history
- GET /api/v1/analytics/overview — company KPIs
- GET /api/v1/analytics/skills — skill matrix
- POST /api/v1/orders — create order programmatically
- GET /api/v1/orders — list company orders
- Webhooks: order.created, order.completed, employee.added

**Why:** Enterprise clients integrate everything. They'll push employee data
from Workday, pull training reports into PowerBI, and trigger orders from
their own LMS.

### 4.3 Bulk operations

**What:**
- CSV import of employees (name, email, department, role)
- Bulk course enrollment (select 10 employees -> enroll in same course)
- Bulk export (training reports, employee data, chatbot logs)

**Why:** Nobody adds 200 employees one by one.

### 4.4 SCIM provisioning

**What:** Automatic user sync from identity providers. When someone joins
the company in Azure AD, they automatically get an AiLeadZ account.

**Why:** Reduces admin overhead to zero. Enterprise gold standard.

---

## Phase 5: Chatbot Intelligence Upgrades

### 5.1 Company-aware recommendations

**What:** The chatbot should know the company context:
- "What courses has my team completed?" -> query company_users + course_orders
- "What's our department's skill gap?" -> query company_skill_targets vs user_skills
- "Show me courses within our budget" -> query department_budgets

**New tools:**
- `get_team_training_status` — what has my department done/planned
- `get_department_budget` — remaining training budget
- `get_company_skill_gaps` — skill gaps for user's department

**Why:** Transforms chatbot from a course search engine into a strategic
training advisor.

### 5.2 Manager mode

**What:** When a manager uses the chatbot, they get extra capabilities:
- "Enroll my team in this course" -> bulk order with approval bypass
- "Who on my team needs ITIL training?" -> skill gap query
- "Show me my team's training report" -> inline analytics

**Implementation:** Check company_users.role. If manager/hr_manager/company_admin,
inject additional tools and system prompt context.

**Why:** Managers are the buying decision-makers. Make them powerful.

### 5.3 Proactive notifications via chatbot

**What:** Chatbot initiates conversations:
- "Hey, dit Prince2-kursus starter om 3 dage. Er du klar?"
- "Din afdeling har 40% ubrugt uddannelsesbudget. Vil du se anbefalinger?"
- "3 nye kurser inden for dit interesseomraade er lige kommet."

**Implementation:** Notification queue table + check on session init in agent.py.

**Why:** Drives engagement and course completion rates.

### 5.4 Multi-language support

**What:** The chatbot currently only speaks Danish. Enterprise clients
may have international employees.

**Implementation:** Detect language from first message. Adjust system prompt.
Course data stays as-is (Danish courses), but conversation is bilingual.

**Priority:** Danish (default), English, Swedish, Norwegian.

**Why:** International teams are common in Danish enterprise.

---

## Phase 6: Payment & Billing

### 6.1 Stripe integration

**What:** Replace placeholder payment instructions with real checkout.

**Flow:** Order created -> Stripe Checkout session -> webhook confirms payment
-> order status updated -> email confirmation sent.

**Why:** Can't run a SaaS without taking payments.

### 6.2 Invoice management

**What:** Auto-generate invoices per company. Monthly billing for
enterprise accounts.

**Features:**
- Per-order invoices (PDF generation)
- Monthly consolidated invoices
- EAN/electronic invoicing (required for Danish public sector)
- Credit notes for cancellations

### 6.3 Subscription tiers

**What:** Formalize pricing:
- Starter (up to 25 employees, basic chatbot + dashboard)
- Professional (up to 100, + analytics + API + SSO)
- Enterprise (unlimited, + white-label + dedicated support + SLA)

---

## Implementation Priority Matrix

| Phase | Effort | Impact | Priority |
|-------|--------|--------|----------|
| 1.1 Enrich chatbot INSERT | Small | High | Week 1 |
| 1.2 Sync feedback to MySQL | Small | High | Week 1 |
| 1.3 Populate user_location | Small | Medium | Week 1 |
| 1.4 Bridge SQLite to MySQL | Medium | High | Week 2 |
| 2.1 Link sessions to orders | Small | High | Week 2 |
| 2.2 Approval workflows | Medium | Critical | Week 3-4 |
| 2.3 Department budgets | Medium | High | Week 4 |
| 3.1 Skill gap analysis | Large | Critical | Week 5-6 |
| 3.2 AI conversation insights | Medium | High | Week 6-7 |
| 3.3 ROI tracking | Medium | High | Week 7 |
| 5.1 Company-aware chatbot | Medium | Critical | Week 5 |
| 5.2 Manager mode | Medium | High | Week 6 |
| 4.1 SSO (Azure AD first) | Large | Critical | Week 8-9 |
| 4.2 REST API endpoints | Medium | High | Week 9-10 |
| 6.1 Stripe integration | Large | Critical | Week 10-12 |
| 4.3 Bulk operations | Medium | High | Week 11 |
| 3.4 Predictive analytics | Large | Medium | Week 12+ |
| 5.3 Proactive notifications | Medium | Medium | Week 13 |
| 6.2 Invoice management | Large | High | Week 14 |
| 4.4 SCIM provisioning | Large | Medium | Week 15+ |
| 5.4 Multi-language | Medium | Medium | Week 16+ |

---

## Quick Wins (Can ship this week)

1. **Enrich chatbot INSERT** — add tools_used, products_shown, conversation_depth
2. **Sync feedback to MySQL** — one UPDATE query in the feedback endpoint
3. **Populate user_location** — use request headers or company data
4. **Add chatbot_session_id to orders** — one field addition
5. **Admin dashboard: feedback chart** — query the new feedback_rating field
6. **HR dashboard: "Trending Topics"** — GROUP BY query_type from chatbot_interactions

These 6 changes would immediately make dashboards significantly more useful
with minimal code changes.

---

## Architecture Target State

```
User / Employee
      |
      v
  AI Chatbot (agent.py)
      |
      |-- logs every interaction --> MySQL chatbot_interactions (enriched)
      |-- creates orders ----------> MySQL course_orders (with session linkage)
      |-- updates profiles --------> MySQL user_profile_db tables
      |-- triggers notifications --> MySQL notification_queue
      |
      v
  Dashboard Layer
      |
      |-- Admin Dashboard: global metrics, all companies, revenue, chatbot health
      |-- HR Dashboard: company-scoped, employee performance, skill gaps, budgets
      |-- Company Dashboard: self-service analytics, department view, approvals
      |-- Manager View: team training, budget, recommendations
      |
      v
  Integration Layer
      |
      |-- REST API: external system access
      |-- Webhooks: push events to client systems
      |-- SSO: Azure AD, Google, Okta
      |-- SCIM: auto-provision users
      |-- Stripe: payments & billing
```

All analytics data flows through MySQL. SQLite is eliminated for business data.
Every chatbot interaction is a first-class event with full attribution.
