# Connected talent workspace

The employee workspace is `/udvikling`; HR manages modules at `/hr/talent`.
`/muligheder` lists published internal roles/projects. Hiring-team members use
`/muligheder/ansaettelsesteam`. Existing profile, CV, learning and booking URLs remain.
All mutation routes are POSTs under `/talent/actions/` and use the app's CSRF protection.

## Competency contract

`competency_model.snapshot` is the pure projection of employee claims and manager
assessments. Competency identifiers derive from canonical skill keys. Self and manager
claims stay separate. A manager assessment supplies the workplace level; otherwise the
self-assessment is provisional. Unknown is `None`, not zero. Numeric levels 1–5 survive
in `user_skills.numeric_level`; legacy enum rows remain readable without backfilling an
invented assessment date. Import upserts preserve the retained claim's source.

`evaluate` evaluates the complete, versioned requirement set before any UI truncation.
It returns satisfied/below-target/unknown counts and a separate provisional count.
Failure to load assessment sources returns unavailable, never a positive readiness score.
CV role hints are explicitly suggested requirements, separate from assigned workplace
roles. CV-writing quality does not score role readiness. Manager badges describe the
manager's actual level. A course handle alone does not verify completion: CV completion
badges require a matching completed order. Course participation does not verify mastery.

`/api/talent/competencies` exposes the current employee's company-scoped state. The
learner profile tool uses the same projection on both AI providers. Existing gap tools
include published current-role requirements and linked personal development requirements.
Home recommendations use known gaps; unknown skills first invite assessment.

Matrix writes use `skill_history.set_matrix_level`, serializing on active company
membership and updating alias rows without relying on a nonexistent unique index.
Assessment history and manager writes share a transaction; a history failure rolls back
the change. Explicit workspace assessment retries deduplicate by source reference.

## Roles, development and sharing

Role definitions are immutable versions. Publishing makes a version discoverable;
assignments pin a version, so editing a framework never silently changes someone's role.
The existing learning-goal record remains the source for title/status. `talent_goal_links`
adds requirements, an initial baseline, next actions, review date and explicit sharing.
Visibility belongs to the goal (`goal_service.share_own_goal`): sharing or unsharing in the
workspace or on `/mine-maal` moves the goal and all its links together. Only shared links
enter the HR queue. Unsharing immediately removes the link from that queue. Neither shared goals nor applications expose private chat/check-in history.
Practical/mentoring tasks can be acknowledged without changing proficiency. Course steps
can reference the employee's own order and follow its actual completion status. HR's
outcome queue links to the existing guarded order review.

A course step is done exactly when its order is completed: `talent_service.goals` derives
it for the employee and HR views alike, and the `course.completed` subscriber
(`domain_reactions.py`) persists it and tells the learner once.

## Notifications

Talent writes notify inside the caller's transaction (`talent_service._tell`), never the
person who acted: HR when a goal becomes shared (never for a private goal), the hiring
team on a new application and on the applicant's accept/withdraw, the applicant on each
stage set by the hiring team and on placement, and the employee on a manager assessment.

## Internal recruitment

Opportunities pin published role requirements and have explicit hiring-team membership.
Browsing is read-only and stores no interest signal. Applying requires a preview of
selected profile sections; the confirmation verifies the preview digest against current
server data before storing an immutable snapshot. Private target roles and goals are
excluded. Applications are unique per employee/opportunity; repeat submissions do not
create another application.

Stages: submitted → screening → interview → offer → accepted; rejection/withdrawal are
terminal. Projects may skip interview. Stage writes use row locks and revision checks.
Only the applicant accepts or withdraws; only the assigned hiring team advances a case.
An accepted offer needs a separate HR placement confirmation before changing a role or
adding project membership. Confirming a role placement also sets `company_users.job_title`
to the role title and, when the opportunity names one, the department; the employee's own
`target_role` is left alone. No automatic ranking, rejection, hiring or role changes.

## Capability scenarios

Scenarios contain department, target date, competency levels, required people and optional
catalog learning costs. Unknowns, provisional coverage and manager-assessed coverage are
separate. Small cells are suppressed, including small complementary cells. Direct
calculations are bounded to 500 employees and larger cohorts show unavailable rather than
partial figures. Course costs are estimates using company catalog discounts; assignment
links open the existing participant/cost/budget review and do not purchase anything.
Publishing a scenario explicitly updates company/department proficiency targets. Required
headcount remains a planning goal, not a claim about current staffing.

The HR assistant can draft a role framework and run a scenario behind a confirm card
(`hr_draft_role_framework`, `hr_run_capability_scenario` in `hr_tools.py`). Both save drafts
through this service and use the page's coverage (`scenario_coverage`); neither publishes.

## Rollout and data lifecycle

The single schema registry owns additive talent tables; normal enterprise schema sync
creates them. Profile schema maintenance adds the nullable numeric level. No destructive
backfill is needed: legacy profile/matrix rows are projected directly. Activate roles,
mobility and planning per company in HR (company-settings capability). New modules default
to off. Disabling a module preserves its data and blocks its gated routes/writes.
The existing time-limited platform-admin company workspace can manage HR modules
without adding the administrator as an employee. Its opportunity owner can manage
their hiring team, but added colleagues must be active members. Employee profiles,
development and applications still require actual company membership.

Personal talent tables participate in GDPR export/erasure. Hiring membership is stored
separately so removing a user also removes team access. Assessment actors and opportunity
owners are anonymized when their own account is erased. Rejected/withdrawn applications
expire after 365 days by default (`RETENTION_TALENT_APPLICATIONS_DAYS`); active development
and assessment records remain until erasure. Application stage history stores actor kind,
not another person's identifier.

Validation: `tests/test_talent_platform.py` exercises the database and Flask journeys,
including assessment disagreement, tenant isolation, sharing, immutable role versions,
application review, hiring permissions, acceptance/placement, scenario isolation and
privacy floors. Run the existing CV, competency, order, HR, schema and GDPR suites too.
