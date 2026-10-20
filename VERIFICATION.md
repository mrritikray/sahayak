# SAHAYAK implementation and actual verification

Verified 19 October 2026 on Linux/Python 3.12. This updates the supplied
`SAHAYAK-local-demo.zip`: the existing repository was available, inspected and used.
The earlier package's claims that no repository was supplied are no longer applicable.

## Original inspection and preservation

The upload contains 44 files. Existing Flask factory, Waitress entry, SQLite/WAL schema,
CLI worker, ownership/CSRF/authentication, private upload validation/downloads, schedule
CRUD, check-ins, report/CSV and local CSS/JS were inspected. This is not a separate
replacement technology stack. Existing endpoints and their core behavior remain.

Specific gaps found and addressed:
- Alerts used the account family phone instead of the patient's stored responsible contact.
- NOT YET and the full REMIND ME LATER vocabulary/policy were missing.
- Timeout escalation happened immediately, with no separate configurable escalation window.
- The dashboard lacked the recent medicine replies, outbound activity and unresolved-alert count.
- Reply auditing, alert delivery metadata and callback race reconciliation were incomplete.
- An uploaded-schema fixture now proves non-destructive migration of existing records.

New modules separate reply grammar, text, family routing, transport decisions and callbacks;
the existing orchestration functions remain usable from the retained routes and worker.
See FILES.txt for exact modified/added/unchanged paths against the uploaded ZIP.

## Executed automated tests

Command: `python -m pytest -q --tb=short`

```text
........................................................................ [ 53%]
...............................................................          [100%]
135 passed in 32.17s
```

Final result: **135 passed; 0 failed; 0 skipped**. Captured output is TEST_RESULTS.txt.
The test databases are temporary files, not the user's data. All Twilio client interactions
are mocked; a global test fixture blocks external requests. No genuine WhatsApp message
was sent. The full mocked-provider test checks patient destination/body, signed inbound
TAKEN/NOT YET, dashboard update, correct-family outbound alert, separate response and
escalation beats, signed delivery callback and no repeated send on duplicate runs.

Coverage includes each mandatory workflow stage:
1. Authenticated fictional patient creation and responsible-family mapping.
2. Reviewed medicine schedule and persistent due-dose record.
3. Due reminder through the outbox/actual adapter code (mocked external client).
4. Correct patient recipient and prescribed information.
5. Signed simulated TAKEN webhook with duplicate MessageSid replay.
6. Correct dose/time/source and only one confirmation event.
7. Actual database-derived dashboard statistic and report.
8. NOT YET unresolved, immediate/deferred policy, patient-requested follow-up.
9. Window expiry and missed-response status (not a confirmed missed dose).
10. Responsible-family recipient, patient/medicine/time/reason and delivery state.
11. Repeated scheduler/outbox work, concurrent claims, deduplicated alerts.
12. Bounded 429 retry, missing credentials, ambiguous network send no blind resend.
13. App recreation with the same SQLite file preserves historical records.

Extra checks: invalid/negated/conflicting/ambiguous/future/late replies; wrong patient code;
foreign account denial; CSRF/signatures; callbacks before REST completion, duplicate and
out-of-order callbacks; current contact after edit; pause/deactivation; original-schema
migration; India/DST boundaries; validation, uploads and CSV formula safety.
One retained legacy reminder test explicitly sets the newly separate requested-delay setting
to five minutes; its original automatic-follow-up expectations remain exercised.

## Failures encountered (not hidden)

Intermediate development runs were not clean: an early run had 12 failed / 64 passed,
and a later expanded run had 20 failed / 84 passed. These were fixed before release.
Examples: a phone formatter constant typo, missing retained check-in timeout behavior,
missing template CSRF import, legacy response text compatibility, a new test assuming
08:00 India was due at the current instant, and same-second simulator uniqueness collisions.

Further review caught a real delayed-escalation bug: already-missed response records were
excluded from later beats. A new separate-beat regression test now verifies nonzero delay.
Reply parsing was tightened to reject conflicting/negated prose. Requested delays now extend
the response deadline so their queued reminders are not cancelled prematurely. A TAKEN reply
resolves alerts without falsely recording that a family member acknowledged them.

The first browser attempt could not launch because the Linux image lacked Chromium libraries.
Those dependencies were installed for QA only. A later screenshot-wait assertion hit the app's
strict CSP; the test harness was corrected without enabling unsafe-eval or weakening CSP.
Final browser output below is a successful rerun, not a claim based on the failed attempts.

## Actual browser and process checks

```text
PASS: real-browser sign-in and desktop dashboard
PASS: patient and reviewed multi-dose medicine creation in browser
PASS: simulated medicine reminder and missed-response family escalation
PASS: late confirmation, concerning check-in and family acknowledgement
PASS: CSV download through browser
PASS: tablet responsive layout and navigation
PASS: mobile responsive layout and navigation
PASS: no browser JavaScript exceptions
BROWSER SMOKE PASSED: all journey assertions completed; no real WhatsApp messages sent.
```

Chromium viewports: desktop 1440x1000; tablet 768x1024; mobile 390x844. Checked sign-in,
profile/schedule creation, simulated reminder and family timeout alert, late TAKEN, concerning
check-in, family acknowledgement dialog, CSV download, all primary pages, mobile nav close
animation, horizontal overflow and no JavaScript page exceptions. This is smoke testing,
not an exhaustive accessibility or all-device audit.

The actual `python app.py` Waitress process started on localhost. Actual GET /health:
`{"database":"reachable","demo_mode":true,"scheduler_recent":true,"status":"ok"}`.
The real `python -m flask --app app worker` CLI printed its startup message, processed a beat
and was intentionally stopped by the test harness after 3 seconds (exit 124 is that planned
timeout, not an app crash). The server was restarted against the same QA database; browser
records persisted and later browser runs completed successfully.

All source/test/utility Python compiled. `pip check`: No broken requirements found.
No runtime database, secret, credentials, user uploads or fictional QA database is in the ZIP.

## Not verified / remaining limitations

- Real Twilio/WhatsApp phone-to-phone delivery, credentials, tunnel and genuine sandbox sessions:
  NOT VERIFIED. Mocked tests are not an end-to-end phone test.
- Free-form WhatsApp bodies require active messaging windows; approved-template/Content SID
  initiation outside that window is not implemented. Not unattended production-ready.
- Native Windows execution, PowerShell on the user's PC and batch launcher were not run here.
- No monitoring while backend/scheduler/computer are stopped; no multi-day historical backfill.
- No provider exactly-once guarantee or blind retries for uncertain sends/permanent failures.
- No production medical/security/compliance certification, MFA/password recovery, phone ownership
  verification, at-rest encryption, high availability or multiple-caregiver invitations.
- Profile/activity pages have recent-row limits; date-filtered dose reports retain full stored
  history. Demo charts include labelled fictional practice/seed data. Manual prescription entry
  is preserved; no OCR/AI extraction or medical recommendations.

README.md contains exact beginner Windows commands, safe upgrade/backup instructions,
local workflow walkthrough, scheduling/reply semantics, and optional Twilio Sandbox setup.
