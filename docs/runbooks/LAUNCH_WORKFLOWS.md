# Launch workflows: operations and acceptance

This runbook covers the complete sales-led learning journey. Billing remains in
external accounting. A successfully submitted request is not a supplier booking,
a learner attendance report is not verified completion, and SMTP acceptance is
not proof of inbox delivery.

## Ownership and entry points

| Journey | Canonical implementation | Human entry point |
|---|---|---|
| Assign a course from HR, AI or API | `learning_path_service.assign_course_to_people`, `enrollment_service.create_order` | `/hr/assign-course` |
| Assign/version a learning path | `learning_path_service.assign_path`, `learning_assignment_steps` | HR step editor `/hr/learning-paths/<id>/trin` (courses picked via `/hr/learning-paths/catalog-search?q=`, structured `step_type[]`/`course_handle[]`/`title[]` fields, errors re-render the editor); learner `/min-laering/forloeb/<id>` |
| Internal learning | tenant-scoped `company_courses`, `internal:<id>` handles | `/interne-kurser` |
| Renew an expiring requirement | `compliance_assign.has_open_or_done` | existing compliance assignment action |
| Confirm a booking (HR by default, reference optional; vendor may confirm in the portal) | `order_fulfillment.book`, `add_reference` | the role's one order page: learner `/min-ordre/<order_id>`, HR `/hr/order/<order_id>/details`, vendor `/vendor/orders/<order_id>/booking` |
| Cancel, reschedule or substitute | `request_change` / `resolve_change` | the change sections of the same order pages (`#aendring`) |
| Quote/charge a course | `enrollment_service.quote_course`, `order_service` | current stable session ID and server-computed quote |
| Report/verify attendance and assess outcome | `report_completion`, `complete_order`, `outcome_review` | the attendance and outcome sections of the order pages (`#deltagelse`, `#udbytte`) |
| Recover delivery failures | `mail_delivery`, `scheduled_reports` | `/hr/leveringer` (company-scoped for HR, global for platform admin) |
| Review customer readiness | `customer_success.readiness` | `/hr/kom-i-gang` |
| Review supplier edits and branding | canonical catalogue draft approval; `branding_service` | existing vendor/admin catalogue and branding editors |
| Sales/customer handover | `customer_routes`, `customer_accounts`, `customer_requests` | `/for-virksomheder`, `/admin/kundeforloeb`, `/virksomhed/kundeforloeb` |

Each role has exactly one order page. They all render the partials under
`templates/fm/order_sections/` from `fulfillment_routes.order_sections()` and post
to one action handler (`fulfillment_routes.perform_action`): learner
`POST /min-ordre/<id>/handling`, HR `POST /ordre/<id>/handling`, vendor
`POST /vendor/orders/<id>/booking`. The old console URLs `/ordre/<id>/booking` and
`/hr/ordre/<id>/udbytte` only redirect each role to its page (an old POST is still
handled for one release, then remove it). Notification and mail links go straight
to the page and section: `/hr/order/<id>/details#aendring`, `/min-ordre/<id>#aendring`,
`/vendor/orders/<id>/booking#aendring`.

## Deployment prerequisites

1. Back up the database and persistent catalogue instance directory.
2. Deploy web and worker from the same revision. Install requirements, including
   `filelock`. The existing additive enterprise bootstrap detects the changed DDL
   fingerprint. Alembic deployments additionally run `alembic upgrade head`;
   `c001_launch_workflows` is additive and tolerates runtime-created tables.
3. Check schema readiness. New tables are declared only in `schema_registry`;
   `course_orders.cancellation_fee` lives in the existing enterprise definition.
4. The runtime migration `learning_assignment_snapshots_v1` freezes existing HR
   assignments using their current definition and links existing matching orders.
   Its step note explicitly identifies that adoption; it cannot reconstruct a
   definition that was never historically recorded. Investigate migration warnings
   before using old assignments in a pilot.
5. Keep `python drain_worker.py --loop` running. `mail_delivery` runs every minute;
   `launch_followup` runs daily. Opportunistic request-time scheduling alone is
   inadequate when there is no web traffic.
6. Configure the existing SMTP settings and `APP_BASE_URL`. Platform administrators
   and the customer’s saved account email receive business-workflow notifications.
   `SALES_EMAIL` or `SUPPORT_EMAIL` is a fallback when no such recipient exists.
7. Catalogue edits require a shared, persistent instance directory for all web
   workers. Writes use a cross-process file lock and atomic replacement. Multiple
   hosts with unrelated local disks do **not** share catalogue changes. Keep the
   source/overlay/drafts on one shared deployment volume or one catalogue writer.
8. Check a published branding identity and a saved draft with separate sessions.
   Only the authorized editor asks for draft preview; learner pages and mail use
   published settings.

## Mail recovery

- `pending`: queued, or retryable failure with backoff (up to eight attempts).
- `sending`: worker holds a five-minute lease with a unique claim ID.
- `sent`: accepted by the mail transport. Ask a real pilot recipient to check the
  inbox/spam folder; this status does not mean read or delivered to the inbox.
- `failed`: attempts exhausted; correct configuration and explicitly retry.
- `uncertain`: connection outcome or expired in-flight lease is ambiguous. Check
  whether the recipient received it before confirming a resend. Stable Message-ID
  helps diagnosis but does not promise exactly-once SMTP delivery.
- `skipped`: non-transactional recipient preference was respected.

Orders stage their business mail within the order transaction. Scheduled reports
stay queued/attention until their delivery group finishes; failure does not advance
`last_sent_at`. A report retry reuses its original queued attachment. It does not
regenerate a different report while claiming it is the original one.

Retention removes only terminal sent/skipped queue rows after the configured
period. Unresolved failed/uncertain messages remain available for investigation.
Auth/reset mail and other existing low-volume notification writers keep their
existing delivery paths; the durable workflow introduced here covers order,
booking-change, scheduled-report and customer-handover messages.

## Pilot acceptance (use a sandbox customer)

1. Create an account handover with owner, offer, included services, pilot criteria,
   pilot end and next review. Submit a demo request and link it to that company.
   Verify the administrator sees the lead and its next action.
2. Add an employee with department/manager, a budget, supplier preference and an
   agreement requiring two participants. Confirm one participant gets the correct
   single-person price and two valid participants qualify for the group price.
3. Preview an HR assignment, change its catalogue price in another session, and
   confirm the old preview: creation must stop and ask for a fresh confirmation.
   Repeat through AI and the company API using stable `session_id`.
4. Assign a path containing a course and a guidance step. Change the path template.
   The learner must still see the assigned snapshot. Choose a missing session from
   the learner view and retry twice; one order must be linked to the step.
5. Enrol in an internal course. Approve, confirm practical details, self-report
   attendance and verify it. An internal course from another company must not
   appear in the catalogue or be enrolable.
6. Verify an expired recurring requirement can be renewed, a valid completion is
   not duplicated, and an already-open renewal remains single.
7. Book a course with date/time, venue or join URL, reference and instructions.
   Check the learner’s booking view and calendar reflect those confirmed details.
8. Request a cancellation. Until acceptance, booking and budget must remain intact.
   Accept with an agreed fee: retain that fee, release the rest, and retry the same
   decision without a second refund. Also exercise rejected cancellation,
   rescheduling with price revalidation, and participant substitution.
9. Report attendance as the learner: order/compliance metrics must remain booked.
   Verify as HR/vendor, then assess the skill outcome as the responsible manager.
   Check the history is linked to the order and the personal/HR path updates.
10. Stop SMTP in the sandbox, submit an order and queue a report. Both should remain
    visible and recoverable. Restore SMTP, run the worker and verify actual receipt.
    Exercise uncertain-send recovery without blind automatic replay.
11. Submit a vendor session/price edit. It must remain invisible to learners until
    admin approval; an intervening edit must produce a conflict rather than an
    overwrite. Verify the published session keeps its stable ID.
12. Send a seat/support request as HR, answer it as admin, and check the reply and
    notification reach the company. No automatic payment or entitlement change
    should occur. Review `/hr/kom-i-gang`: manual setup review must not fabricate
    successful booking, mail or learning-outcome evidence.

## Limits to communicate honestly

- Availability is the catalogue’s latest declared seat count, revalidated on
  request. The platform does not hold an external supplier seat or synchronize
  live inventory with a supplier API. Only supplier/HR confirmation makes it booked.
- Attendance evidence is a note and optional certificate URL, not a new binary
  certificate-storage service. Managers verify it before it affects completion.
- Human confirmation is supported for changes agreed outside the platform. The
  confirmer records the agreement and any fee; the application cannot independently
  prove a phone call or email from a supplier.
- Commercial owners still need to choose the actual offer, margin/pricing,
  customer support expectations and launch pilot. Code and readiness counters do
  not substitute for a real end-to-end pilot with actual recipients/suppliers.
