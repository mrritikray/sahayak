# SAHAYAK — connected medicine monitoring

This is an update of the uploaded **SAHAYAK-local-demo.zip**, not a separate unrelated app.
It preserves the existing Flask app factory, SQLite storage, authentication, private prescription
uploads, medicine CRUD, daily well-being check-ins, CSV reports, local assets and localhost server.

**Core loop:** medicine schedule → patient WhatsApp reminder → explicit patient reply → database
status → responsible-family alert when needed → dashboard/history.

> **Safety:** no reply does not prove a missed dose. TAKEN records a confirmation, not medically
> verified ingestion. SAHAYAK never recommends a medicine/dosage, diagnoses illness, or provides
> emergency response. Use fictional records for this hackathon demonstration.

## 1. What is verified, and what is not

| Capability | Local Demo Mode | Real WhatsApp |
|---|---|---|
| Patient + responsible family contact | Implemented and tested | Same storage/ownership rules |
| Reviewed medicine, dates, multiple times, pause/inactive | Implemented and tested | Same scheduling rules |
| Scheduled reminder and one requested follow-up | Simulated outbox, no network | Twilio adapter, mocked in tests |
| TAKEN / NOT YET / REMIND ME LATER | Simulator and audited history | Signature-validated webhook, mocked in tests |
| No-reply timeout and delayed family escalation | Implemented and tested | Correct recipient checked through mocked adapter |
| Dashboard, reports, acknowledgement | Real SQLite records | Same UI and records |
| Delivery receipts | No fake delivered receipts | Signed callbacks implemented, including early/out-of-order callbacks |
| Actual phone-to-phone delivery | **Not sent** | **NOT VERIFIED — genuine end-to-end test still required** |

See `TEST_RESULTS.txt`, `BROWSER_RESULTS.txt` and `VERIFICATION.md` for executed results. Windows
commands below are provided for your PC; verification here used Linux/Python 3.12 and Chromium,
not a physical Windows machine. No real WhatsApp messages were sent during tests.

## 2. Safely keep your existing data

1. Stop BOTH your existing Flask server and scheduler (Ctrl+C in each terminal).
2. Make a private backup of your existing project folder, especially `.env`, `instance` and uploads.
   If your old app is running the uploaded version, its `tools\backup.py` utility is still preserved.
3. Extract this updated ZIP into a **new folder** first. Do not unzip over your only working copy.
4. To keep your data, copy your old `instance` folder into this extracted project's `sahayak` folder
   BEFORE its first startup. Keep your original `.env` privately too. The additive migration adds
   missing columns/tables; it does not reset records. The uploaded original schema is included as
   a test fixture and its migration is actually tested.
5. Older patients may have optional contacts empty. Open each profile, enter and verify the
   responsible family name/WhatsApp number, then save. Alerts use that patient's contact first;
   only legacy empty-contact records fall back to the owning account's family number.
6. An older account's explicitly selected **in-app** notification channel is preserved. For real
   family WhatsApp alerts, choose **WhatsApp** in Settings. New accounts default to WhatsApp;
   Demo Mode still simulates every outbound notification regardless of this selection.

Only this uploaded repository's schema is covered. Do not point the app at an unrelated database.
Keep old backups until your own local demonstration works. Never share databases, uploads or secrets.

## 3. Beginner Windows + VS Code setup (PowerShell)

Install **Python 3.12, 64-bit**, including the Python launcher, and VS Code. Download the updated ZIP
into Downloads using the filename below. In PowerShell:

```powershell
Expand-Archive -LiteralPath "$HOME\Downloads\SAHAYAK-updated.zip" -DestinationPath "$HOME\SAHAYAK-updated"
cd "$HOME\SAHAYAK-updated\sahayak"
py -3.12 --version
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

If the extraction folder already exists, choose another new folder; do not add `-Force` over a
working copy. In VS Code: **File → Open Folder → select this `sahayak` folder**. Press Ctrl+Shift+P,
choose **Python: Select Interpreter**, and select `.venv\Scripts\python.exe`.

**No activation is required:** the commands explicitly call the correct Python, so PowerShell's
script-execution policy is not a barrier. Python packages need internet once during installation;
the Demo Mode then works without credentials, cloud storage or paid APIs.

### Terminal 1 — start Flask/Waitress

```powershell
cd "$HOME\SAHAYAK-updated\sahayak"
.\.venv\Scripts\python.exe -m flask --app app init-db
.\.venv\Scripts\python.exe app.py
```

Open **http://127.0.0.1:5000**. Keep this terminal open. The default is Demo Mode. No `.env` is
required. `init-db` can be repeated safely without deleting records. `app.py` uses Waitress on
localhost rather than exposing the server to your whole network.

### Terminal 2 — start the automatic scheduler

In VS Code select **Terminal → New Terminal**:

```powershell
cd "$HOME\SAHAYAK-updated\sahayak"
.\.venv\Scripts\python.exe -m flask --app app worker
```

Keep this second terminal open. It checks every 15 seconds. SQLite leases, dose/message unique
keys and transaction-protected outbox claims prevent duplicate work even if another worker is
accidentally started. Settings shows a recent heartbeat. The simulator works immediately even
without this worker, but automatic reminders require a running worker (and an awake computer).

After installing packages you can instead double-click `run_demo.bat` to open both processes.
Close BOTH windows to stop the application. The batch file is provided, but was not executed on
Windows in this environment.

Optional diagnostics:

```powershell
.\.venv\Scripts\python.exe -m flask --app app check-config
.\.venv\Scripts\python.exe -m flask --app app tick
```

`tick` processes one scheduler beat. In live mode it can send real configured messages; do not
use it casually with real contacts. The browser's “Run scheduler now” action is Demo Mode only.

Optional single-window mode: copy `.env.example` to `.env`, set `SCHEDULER_IN_APP=true`, then run
only `app.py`. The default/recommended setup remains the two-terminal arrangement. Do not use
Flask debug reloaders or multiple web processes with this optional mode.

## 4. Your five-minute hackathon demonstration

Default local-only sign-in:

- Email: **demo@sahayak.local**
- Password: **SahayakDemo!2026**

These are public demo credentials. Never expose that account through a public tunnel or enter
real patient information. The initial sample history is clearly fictional and stored in SQLite;
the dashboard never invents counts. You can also register an empty private family account.

1. **Patients → Add patient.** Enter `Robin Demo`, patient number `+12025550104`, timezone
   `Asia/Kolkata`, family name `Sam Demo`, and family number `+12025550106`. Use another unused
   reserved demo number if either is already registered. Format validation cannot prove who
   owns a number: confirm it with the actual contact before any future live use.
2. **Add medicine.** Enter `Fictional Demo Tablet`, dosage `Fictional label only — not for use`,
   instructions `Practice data`, today's start date, `08:00, 20:00`, Active. Review the details
   and tick the required authorization checkbox. These are NOT medication recommendations.
3. **Demo simulator → select Robin → Simulate medicine reminder.** This creates an extra labelled
   practice dose for Robin's first active medicine schedule and routes it through the real outbox.
   The notification is `demo / simulated`; no real WhatsApp send occurs.
4. Find the new dose's actual **D code**, such as `D23`. Click **TAKEN**, or submit `TAKEN D23`
   in the reply form with Robin selected. Use the number actually displayed, not this example.
   The correct dose becomes taken with timestamp/source. Overview and reports reflect the change.
5. Simulate another reminder. Click **NOT YET**. The dose stays unresolved; by default an alert
   to Robin's responsible family contact is created and simulated. Settings can defer that alert.
6. Simulate another reminder. Click **REMIND ME LATER**, or type `REMIND ME LATER 45 D<number>`.
   One follow-up is scheduled. Default delay is 30 minutes; supported explicit delays are 5–240
   minutes. A second request adjusts the queued reminder rather than creating another message.
   It never claims medicine was taken. Keep the worker running to observe future sends.
7. For a fresh unanswered reminder click **Expire window → family alert**. This explicitly moves
   only that dose's deadline into the past, including the escalation delay, so you can demonstrate
   a family alert immediately without changing the computer's clock. The normal scheduler policy
   creates the no-reply status and alert. It does not claim a confirmed missed dose.
8. **Family alerts:** inspect patient, medicine, scheduled time, reason and delivery state.
   Click **Acknowledge**, then confirm in the dialog. This records which signed-in family member
   saw it. A late TAKEN confirmation separately records resolution without forging acknowledgement.
9. **Message activity:** inspect the actual recipient, simulated channel, attempts and error/delivery
   state. Use patient/date/status filters. The simulator's failure button creates a deliberately
   labelled simulated failure without changing the original dose's delivery or confirmation.
10. **History & reports:** filter by patient and date, switch daily/last-seven-day views, inspect the
    real trend and export CSV. Older history remains available here even though the profile shows
    only its latest 100 doses and 30 replies/check-ins/alerts.
11. Stop and restart both processes in this same folder. Your records remain in `instance\sahayak.sqlite3`.

Preserved extra features: **Prescriptions** stores private PDF/PNG/JPEG files for manual reviewed
entry (no OCR/AI), and **daily check-ins** support BETTER, SAME, WORSE and NEED HELP. WORSE or
NEED HELP creates a family alert. Nothing is prescribed automatically from an upload.

## 5. Replies, timeouts, escalation and delivery semantics

- `TAKEN [Dcode]` explicitly confirms that dose. Authorized dashboard family confirmation is
  tagged `family_user:<id>`, not disguised as a patient WhatsApp reply. The dashboard separately
  lists the number of patient confirmations inside the confirmed-total card.
- `NOT YET [Dcode]` / `NOT TAKEN [Dcode]` records an unresolved reply, not confirmed ingestion
  or a permanently skipped dose. Settings chooses immediate family alert or escalation after
  the response deadline plus escalation window. A no-confirmation state is never a missed dose.
- `REMIND ME LATER [minutes] [Dcode]` / legacy `REMIND Dcode`: one patient-requested follow-up
  per dose. When no supported delay is supplied, the configured default is clearly reported.
  The response deadline extends to the requested reminder time plus one response window.
  Once that follow-up has been sent, repeated requests do not create another one.
- Plain words work only when there is exactly one matching unresolved dose (or check-in).
  Multiple medicines need their explicit D code. Different-profile/future/invalid/conflicting
  codes do not confirm anything. The parser does not infer TAKEN from negated prose such as
  “I have not taken it”. Every recognized or rejected patient reply is audited.
- A repeated Twilio MessageSid is a no-op. A repeat TAKEN confirmation does not add another
  dose-confirmation event. Late TAKEN is allowed for the same patient's explicit dose code;
  prior timeout events remain preserved. Related alerts get `resolved_at`, not a fake family ack.
- A response window starts when the initial reminder is simulated or accepted by Twilio, not
  proof of delivery. After it expires, status `missed` means **no confirmation received**. An
  optional single timeout follow-up is sent, then the family alert is created when the configured
  escalation window closes. Defaults: 60-minute response, 0-minute escalation, 30-minute requested
  delay, immediate NOT YET alert. Settings also preserves the existing bounded automatic repeats.
- Active unresolved reminders can be followed up; TAKEN/pause/deactivate cancels unsent reminders.
  Already-sent dose history and its response window remain for follow-up/monitoring. Unsent alerts
  resolved by a confirmation are cancelled. Outbox delivery rechecks the current contact number.
- Each alert notification targets **this patient's `contact_phone`**, never another owning family's
  record. Legacy records with an empty contact may use the account fallback. Demo Mode simulates;
  live WhatsApp requires the WhatsApp channel; explicit in-app fallback never claims a real send.
- `simulated` = no network. `in_app` = local family notification. `accepted` = Twilio accepted,
  not delivered. `delivered`/`read` are only provider callback states. `failed`/`unknown` need review.
- Explicit 429 rejection safely retries at most 3 attempts with backoff. Timeouts/uncertain or
  interrupted sends never blindly retry; the provider may already have accepted them. Permanent
  failures stay visible. Message-attempt records and callbacks persist. No provider exactly-once
  guarantee is claimed. No credentials or full message bodies are written to application logs.

## 6. Automated verification (no real messages)

In a third VS Code terminal:

```powershell
cd "$HOME\SAHAYAK-updated\sahayak"
.\.venv\Scripts\python.exe -m pytest -q
```

Tests use temporary databases, fictional contacts and mocked Twilio services. An autouse network
block prevents unexpected external HTTP requests, even when someone has credentials locally.
The new full mocked-provider journey checks the actual adapter destination/body, signed TAKEN
and NOT YET webhooks, delayed no-reply escalation to the right family, receipts and deduplication.
Additional tests cover all 13 requested workflow stages, restart persistence, authorization,
original-schema migration, DST/timezones, invalid/ambiguous/late replies, retry limits, callback
races, pause/edit cancellation, private uploads and CSRF. See captured outputs, not an assumed pass.

Optional actual browser smoke test, against a running LOCAL DEMO instance on port 5000:

```powershell
.\.venv\Scripts\python.exe -m pip install playwright
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe tools\browser_smoke.py
```

This modifies only fictional local records and saves QA screenshots to `browser-artifacts`.
Playwright is not a runtime dependency for using the app.

## 7. Optional Twilio Sandbox: real delivery must be verified separately

**No genuine WhatsApp end-to-end test was possible here.** The implementation is not certified
live merely because its mocked tests pass. Sandbox use may incur Twilio messaging charges.

1. Stop app and scheduler. Copy the example only if `.env` does not already exist:

```powershell
Copy-Item .env.example .env
```

2. Edit `.env` privately. Use a NEW database, real consenting phone numbers, and no seeded account:

```dotenv
DEMO_MODE=false
DATABASE_PATH=instance/live.sqlite3
COOKIE_SECURE=true
SCHEDULER_IN_APP=false
TWILIO_ACCOUNT_SID=copy_your_own_sid
TWILIO_AUTH_TOKEN=copy_your_own_auth_token
TWILIO_WHATSAPP_FROM=whatsapp:copy_the_sandbox_number_from_your_console
```

Values above are instructions, not real credentials. Never send Auth Tokens in chat or screenshots.
A session-signing secret is generated privately in `instance\secret.key` unless SECRET_KEY is set.
`TWILIO_WHATSAPP_FROM` must be exactly `whatsapp:+<country-code-and-number>` as shown for your
active Sandbox sender in your own Twilio Console. The recipient phone numbers must be E.164
numbers (`+` and country code); the app adds the `whatsapp:` prefix when calling Twilio.

Outbound sends from the local dashboard/worker need only valid Twilio credentials and the Sandbox
sender; `PUBLIC_BASE_URL` is optional for sending. Therefore you can keep the dashboard bound to
`127.0.0.1:5000` without deploying it. Inbound WhatsApp replies and delivery-status callbacks need
Twilio to reach the local app, so they require a temporary, publicly reachable HTTPS tunnel. Set
`PUBLIC_BASE_URL` to that tunnel host (no path, query, or trailing slash) and restart both processes.
Without it, outbound sends may work but replies/callbacks will not reach the app.

The `check-config` command checks local formats only; it does not prove account ownership or send
a message. A read-only authenticated Twilio Account API check can verify that the SID and token pair
belong together without sending WhatsApp messages.

3. Activate the Twilio WhatsApp Sandbox. EACH test phone (patient AND family) must send
   `join <your sandbox code>` to the number shown in YOUR Twilio console. Both should send a fresh
   WhatsApp message before testing to ensure their customer-service windows are active.
4. Start the web app on local port 5000. Outbound sends work without a tunnel. To receive replies or
   delivery callbacks, install/configure an HTTPS tunnel separately; if using ngrok, run
   `ngrok http 5000`. Set its exact hostname in PUBLIC_BASE_URL. No path prefix or trailing slash.
   Restart app AND worker whenever environment values change.
5. In Twilio Sandbox configuration, set **When a Message Comes In**, method **POST**, to:

```text
https://your-current-public-tunnel-host/webhooks/twilio
```

   Set the status callback to:

```text
https://your-current-public-tunnel-host/webhooks/twilio/status
```

   Each outbound API request also includes that callback URL. The Twilio SDK validates signatures
   against the exact configured URL + query + POST parameters; do not disable validation to fix 403.
6. Sign in/register through the HTTPS tunnel URL (secure cookies require HTTPS). Add your consenting
   test patient and their correct family contact. Use fictional medicine information only. Select
   **WhatsApp** in Settings; existing in-app preferences stay local until changed.
7. Schedule a reminder a few minutes ahead in the patient's own timezone and start the worker.
   Observe actual reminder on patient phone, reply `TAKEN D<actual code>`, verify the correct dose
   and dashboard. Test another dose with `NOT YET D<code>` and check the FAMILY phone, then use a
   fresh unanswered dose to test expiry/escalation. Verify patient, medicine, time and recipient.
8. Check Message activity and Twilio logs for delivered/read callbacks. Record this checklist on
   YOUR environment before describing real WhatsApp as verified. The Demo Simulator is disabled
   in live mode; real timeout tests must wait for your configured windows.

### Important Sandbox restriction

Twilio Sandbox is for testing only, supports only joined recipients, has rate limits and expiring
Sandbox sessions. Free-form text requires an active **24-hour customer-service window** for each
recipient. Outside it, approved templates are required. This adapter sends free-form bodies;
**approved-template/Content SID initiation is NOT implemented**. It is therefore not production-ready
for unattended, arbitrary-time reminders outside an active window. Do not misuse appointment/order
Sandbox templates as medical reminders. A registered sender and appropriate approved templates
would be a separate production extension.

Official references checked for these instructions:
- https://www.twilio.com/docs/whatsapp/sandbox
- https://www.twilio.com/docs/usage/webhooks/webhooks-security

## 8. Storage, restart, backup and architecture

- SQLite: `instance\sahayak.sqlite3` unless DATABASE_PATH is set; WAL and foreign keys are enabled.
- Uploaded originals: `instance\uploads`; not public, UUID filenames, owner-only downloads.
- Configuration: `.env`; session secret: `instance\secret.key`. Keep these private.
- Stop: Ctrl+C in BOTH terminals. Restart in the same folder/configuration to keep data.
- Backup: stop both processes, then `.\.venv\Scripts\python.exe tools\backup.py`. For live DB use
  `--database instance/live.sqlite3`. Keep the resulting `backups` folder offline/private. To restore,
  stop both processes, preserve current data, restore the snapshot database to DATABASE_PATH and
  uploads to `instance\uploads`, then restart. Never restore a demo DB over a live DB blindly.
- Safe clean demo: with processes stopped and backups kept, choose a fresh folder/database rather
  than deleting your only history. A custom DATABASE_PATH must be handled explicitly.

Architecture retained and extended:

```text
app.py / __init__.py      Flask factory, credentials/config, CSRF, CLI worker
web.py                   authenticated CRUD, dashboard, reports, simulator
validation.py            prescribed input, E.164 formats, timezone/date validation
db.py                    SQLite schema and non-destructive additive migration
services.py              schedule/event orchestration, unique outbox, reply state transitions
messages.py              reviewed reminder/alert text (no inferred prescription details)
replies.py               conservative patient reply grammar
alerts.py                patient-specific family routing and alert keys/policy
providers.py             simulated/local/WhatsApp transport decisions
webhooks.py / callbacks.py signed inbound, SID dedupe, persisted delivery reconciliation
seed.py                  labelled fictional initial history, once only
```

Reminder times use the patient's IANA timezone; reports use the family dashboard timezone. UTC
instants are persisted. DST fallback uses the first occurrence; spring gaps shift by the gap.
The scheduler materializes current/next local day, not every past prescription day. Materialized
missed jobs can catch up on restart; periods while the computer is off are not monitored. Editing
a schedule keeps old medicine snapshots and safely regenerates unnotified future events. Settings
changes preserve sent response deadlines, but current escalation/repeat policy applies on later beats.

## 9. Troubleshooting

| Problem | Simple action |
|---|---|
| `py` / Python 3.12 missing | Install Python 3.12 + launcher, reopen VS Code |
| `No module named flask` | Use `.\.venv\Scripts\python.exe` and install requirements with it |
| PowerShell activation blocked | No activation is needed with the full Python path above |
| Port 5000 in use | Stop the OLD app/server first; otherwise you may be seeing the wrong copy |
| No automatic reminders | Keep worker + computer awake; check Settings heartbeat, patient/schedule active, dates/timezone |
| Local login doesn't stick | DEMO_MODE=true, COOKIE_SECURE=false locally; restart after edits |
| 400 / expired CSRF | Reload form before submitting; don't disable CSRF |
| Login 429 | Wait 15 minutes; repeated wrong passwords are throttled |
| Phone already registered | One patient per phone prevents ambiguous inbound mapping |
| Plain TAKEN does nothing | More than one unresolved dose: use the exact D code |
| NOT YET family alert not WhatsApp | Demo simulates; in live mode select WhatsApp and verify patient family contact |
| WhatsApp failed / uncertain | Review activity + Twilio logs, joined phones, 24-hour windows; don't blindly resend |
| Webhook 403 / 503 | Check exact public URL + token; 503 means missing config; restart after changes |
| Upload rejected | Use readable PDF/PNG/JPEG below 8 MB total request, PDF ≤100 pages, image ≤25 megapixels |
| Database locked | Stop duplicate tools/processes; keep SQLite on local disk, not cloud-sync/network folders |
| Demo check-in already answered | One completed check-in per patient-local day; use another fictional patient |

## 10. Remaining limitations

- Real WhatsApp phone-to-phone flow remains unverified. Approved-template initiation outside the
  24-hour window, permanent-failure remediation and uncertain-send reconciliation are not automatic.
- Native Windows/batch-launcher execution was not performed here. Commands avoid activation-policy
  issues, but your PC still needs Python/dependencies and a free port.
- No monitoring while the machine sleeps or the scheduler is stopped; no multi-day historic backfill.
  This is not hard real-time or highly available infrastructure.
- One owning family account per patient; no caregiver invitations, MFA, password reset, email/phone
  ownership verification or legal medical/privacy compliance certification. Format validation is not
  ownership verification. Do not expose the public demo login or use real health data.
- SQLite and uploads are not app-encrypted at rest. Use local disk protection and private backups.
- Manual prescribed entry only; no OCR/AI interpretation or medical recommendations. Existing upload,
  check-in and reporting features were retained, not turned into unnecessary new products.
- Views have explicit recent-record limits; full date-filtered dose history is available in reports.
  Reports include labelled fictional/practice events in Demo Mode. Large-history performance and
  accessibility/security audits are not a production certification.

See `FILES.txt` for the exact added/modified/unchanged inventory compared with the uploaded ZIP.
The release excludes credentials, private DBs, uploads, caches, virtual environments and QA browser data.
