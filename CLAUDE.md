# CLAUDE.md — CPUC Voting Meeting Monitor

Project context for future Claude Code sessions. Read this first.

## What this project does

A Python web-scraping agent that monitors the **California Public Utilities
Commission (CPUC)** document site for newly published **voting meeting
documents** and sends **email alerts** when they appear.

It watches **two documents per meeting cycle**:

1. **Current Meeting Agenda** — published ~10 days before a meeting.
2. **Hold List** — published sometime in the days before the meeting,
   and only monitored *after* the agenda for that meeting is confirmed.

When each document is detected for the **target meeting**, a separate email
alert is sent.

It also watches **one standing target**, independent of the meeting cycle:

3. **ALJ Proposed Decision on proceeding A2507016** (the Charter/Cox merger).
   Checked every run against the CPUC Proposed Decisions list; a separate alert
   is sent when it appears. See "Proceeding A2507016 watch" below.

## How it runs (IMPORTANT — read before changing anything)

- **Host:** GitHub Actions (free, 24/7, no PC required).
  - The original prompt (`Prompt.txt`) targeted PythonAnywhere free tier.
    That was **rejected** because the free tier blocks outbound SMTP and
    restricts HTTP to an allowlist that does not include `docs.cpuc.ca.gov`.
  - The Python code itself is **host-agnostic** — it runs locally and on any
    host (including paid PythonAnywhere with its task scheduler).
- **Trigger model:** `monitor.py` runs **once per invocation** and
  **self-gates**. A scheduled GitHub Actions cron runs it **every 5 minutes**;
  the script decides whether each phase is actually "due" based on the dates in
  `config.json` and timestamps in `last_seen.json`. If nothing is due, it
  logs `skipped` and exits 0.
- **Repo is PUBLIC** so GitHub Actions minutes are unlimited/free (a private repo
  caps at 2,000 min/month, which a 5-minute cron would blow past). Because it's
  public: no real emails live in the tree — `config.json` emails are blank and
  the real sender/recipients come only from **secrets**. Do not commit personal
  emails. `CHEATSHEET.md` is gitignored (kept local) for the same reason.
- **State persistence:** GitHub runners are ephemeral, so the workflow
  **commits `last_seen.json` and `monitor.log` back to the repo** after every
  run. This is both the durable state store and the audit trail.

## Smart checking schedule (the core logic)

All timing is computed in **Pacific time** (`America/Los_Angeles`).
"Due" = enough time has elapsed since `last_checked` for that phase.

### Phase 1 — Agenda monitoring
The agenda must be published 10 days in advance, but CPUC's "10 days" may mean
calendar days (~day 10) or business days (~day 14, up to 15 with a holiday), so
the 5-minute peak zone spans the whole 8–15 day band to cover both.
- More than 20 days before meeting → do not check.
- 16–20 days before → check once per day (watching begins; agenda this early is
  unlikely).
- 8–15 days before → check every 5 minutes (peak zone).
- 7 days or fewer → check every hour (safety net, in case the agenda is late).
- On detect + match to target meeting → send **Agenda** email,
  set `agenda.confirmed = true`, begin Phase 2.
- Before sending, the agenda **PDF text is extracted** (`fetch_pdf_text()` via
  `pypdf`) and searched for `PROCEEDING_ID`. The result is folded **into the same
  agenda email** (not a separate alert): it states whether A2507016 appears on
  the agenda ("YES …" / "not found …"), or "could not be determined" if the PDF
  is unreadable. When found, the subject gets a "— A2507016 ON AGENDA" marker.
  Agenda items list proceeding numbers as `A.25-07-016`; `_normalize()` strips
  the punctuation so it matches `A2507016`. Verified: agenda PDFs are text-based
  (Oracle BI Publisher) and do contain proceeding numbers.

### Phase 2 — Hold List monitoring
- Begins only after the agenda is confirmed for the target meeting.
- Check every 3 hours; increase to every hour within 3 days of the meeting.
- On detect → send **Hold List** email, set `hold_list.confirmed = true`.
- Once the meeting date has passed → advance to the next meeting in
  `config.json` and reset `last_seen.json`.

### Proceeding A2507016 watch (independent of the meeting cycle)
- Runs on **every** invocation via `run_proceeding_watch()`, including when the
  meeting cycle is idle (no upcoming meeting) or outside its checking window
  (>20 days out). It is **not** gated by the meeting logic.
- **Source:** the CPUC "Decisions and Resolutions for Public Comment" list
  (`PROPOSED_DECISIONS_URL` — a `SearchRes.aspx?ProposedDecisions=1` page, same
  page type as the agenda/hold-list searches). Chosen over the Daily Calendar,
  whose URL 404'd and whose only machine-readable form is a PDF search.
- **Cadence:** every 3 hours (`PROCEEDING_INTERVAL`). The source updates about
  once per business day, so 3h catches a PD the same day without needless polls.
- **Match rule:** a list row matches when its text contains the proceeding id
  (`PROCEEDING_ID`, matched punctuation-insensitively so `A.25-07-016` ==
  `A2507016`) **and** one of `PROCEEDING_KEYWORDS` ("proposed decision" / "alj").
- **De-dup:** each alerted entry's signature (its PDF url, else a hash of the
  row text) is stored in `proceeding.seen`; a repeat listing never re-alerts.
- **Separate alert:** sends its own email (`build_proceeding_email()`) with the
  document title, date posted, and direct PDF link — never merged with the
  agenda/hold-list emails.
- **State isolation:** lives under the `proceeding` key and is **preserved across
  meeting resets** (captured in `main()` before `select_target_meeting()` may
  replace the state dict, then re-attached). `reset_state_for()` deliberately
  does not include it.

### Hard rules
- Never check the Hold List before the agenda is confirmed.
- Never check anything more than 20 days before a meeting (meeting cycle only —
  the A2507016 watch runs regardless).
- All timestamps in Pacific time.
- Log every check with timestamp and result. `monitor.log` is auto-trimmed to
  the last `LOG_MAX_LINES` (5000) lines each run (`trim_log()`), since it is
  committed back to the repo every run.

## How the CPUC pages work

Two constant search-query URLs, each returning the single latest matching
document (the `Latest=1` parameter does this):

- **Agenda:**
  `https://docs.cpuc.ca.gov/SearchRes.aspx?DocTypeID=1&DocTitleStart=Current%20Meeting%20Agenda&Latest=1`
- **Hold List:**
  `https://docs.cpuc.ca.gov/SearchRes.aspx?DocTypeID=1&DocTitleStart=Hold%20List&Latest=1`

The A2507016 watch uses a third URL — the Proposed Decisions list — which is the
**same `SearchRes.aspx` page type**, but returns *many* rows (no `Latest=1`), so
`fetch_proposed_decisions()` walks every row instead of taking just the first:

- **Proposed Decisions:**
  `https://docs.cpuc.ca.gov/SearchRes.aspx?ProposedDecisions=1&DaySearch=30`

Each result row contains a document **title with the meeting date and agenda
number**, a **direct PDF link**, and a **published date**.

### Title formats are messy — parse defensively
Observed live examples (June 2026):
- Agenda:   `Current Meeting Agenda for July 2, 2026 (Agenda #3583 - #3)`
- Hold List: `Hold List for June 11, 2026 (Agenda 3582) (Final).docx`

Note the agenda number appears as `#3583 - #3` in one and `3582` (no `#`) in
the other. **Match primarily on the meeting date**; treat the agenda number as
a secondary confirmation, not a strict requirement.

## Files

| File | Purpose |
|---|---|
| `CLAUDE.md` | This file |
| `Prompt.txt` | Original project brief (historical) |
| `monitor.py` | Scraper, phase logic, email alerts |
| `config.json` | Meeting dates, agenda numbers, email prefs |
| `last_seen.json` | Per-meeting state + per-phase `last_checked` |
| `.env` | Local credentials (gitignored, never commit) |
| `.env.example` | Template for `.env` |
| `.gitignore` | Excludes `.env` |
| `requirements.txt` | requests, beautifulsoup4, python-dotenv, pypdf, tzdata |
| `.github/workflows/monitor.yml` | Hourly cron + commit-back of state |
| `README.md` | Full setup + deployment instructions |
| `monitor.log` | Append-only run log (committed by the workflow) |

## Credentials / config

- **Secrets** (`BREVO_API_KEY`, `YAHOO_EMAIL`, `ALERT_EMAIL`) come from
  environment variables. In GitHub Actions they are repository **Secrets**;
  locally they come from `.env` via `python-dotenv`.
- **Never hardcode credentials.** `.env` is gitignored.
- Email is sent via **Brevo's HTTPS web API** (`POST https://api.brevo.com/v3/smtp/email`),
  NOT SMTP. Plain SMTP (Yahoo 587/465) is blocked both by the Cox network and by
  Yahoo for automated senders — confirmed during setup — so it was abandoned.
  `YAHOO_EMAIL` is reused only as the Brevo-verified **sender** ("from") address;
  `YAHOO_APP_PASSWORD` is no longer used. `BREVO_API_KEY` starts with `xkeysib-`.
- `send_email()` takes an optional `html_body`; when given, it sends both
  `textContent` and `htmlContent` (clients fall back to text). All real alerts
  (Agenda, Hold List, A2507016 Proposed Decision) and the `TEST_EMAIL` message
  send both. `build_email()` and `build_proceeding_email()` return
  `(subject, text_body, html_body)`. Scraped fields are `html.escape()`d (CPUC
  titles contain `&`, e.g. "PG&E"). The proceeding is shown as
  `A2507016 (Charter/Cox)` everywhere via `PROCEEDING_ID` + `PROCEEDING_LABEL`.

### Debug / test toggles (env vars; each skips monitoring and exits)
- `TEST_EMAIL=1` → send the subscriber "You're Subscribed" test email and exit.
- `TEST_AGENDA_PDF=<url>` → read that agenda PDF, log whether `A2507016` appears
  (plus the proceeding numbers it found), and exit. Sends no email. Use it to
  dry-run the PDF parse without waiting for a real agenda detection.
- `TEST_ALERT=<kind>` → build a **real** alert email from sample data and send it
  (subject prefixed `[TEST]`), then exit. Kinds: `agenda`, `agenda-notfound`,
  `agenda-undetermined`, `holdlist`, `proceeding`. Lets you preview the actual
  alert formatting in your inbox. Value `none`/empty is a no-op.
- `TEST_RECIPIENT=<email>` → optional override (a **secret**, not a workflow
  input, so it stays out of the public repo's run logs). When set, `TEST_EMAIL`
  and `TEST_ALERT` send **only** to this address via `send_email`'s
  `recipients_override`; real alerts still go to the full `ALERT_EMAIL` list.
- The toggles above are `workflow_dispatch` inputs in `monitor.yml`;
  `TEST_RECIPIENT` is a repository Secret wired into the run's env.

## config.json shape

```json
{
  "meetings": [
    { "date": "2026-07-16", "agenda_number": "3584" },
    { "date": "2026-08-13", "agenda_number": "3585" }
  ],
  "alert_email": "",
  "from_email": ""
}
```

`alert_email` / `from_email` are intentionally **blank** in the public repo; the
real sender and recipients come from the `YAHOO_EMAIL` and `ALERT_EMAIL` secrets,
so no personal emails live in the tree.

## last_seen.json shape

Extends the original spec with `last_checked` timestamps so the 5-minute cron can
enforce the per-phase cadences.

The `proceeding` block is independent of the meeting cycle and is preserved when
the meeting advances (`reset_state_for()` does not touch it).

```json
{
  "current_meeting": { "date": "2026-07-16", "agenda_number": "3584" },
  "agenda":    { "confirmed": false, "pdf_url": null, "detected_at": null, "last_checked": null },
  "hold_list": { "confirmed": false, "pdf_url": null, "detected_at": null, "last_checked": null },
  "proceeding": { "id": "A2507016", "last_checked": null, "detected_at": null, "seen": [] }
}
```

`proceeding.seen` accumulates signatures (PDF url or a row-text hash) of entries
already alerted on, so the same Proposed Decision listing never re-alerts.

## Conventions

- Python 3.9+ (`zoneinfo` from stdlib; `tzdata` in requirements for Windows).
- All file paths relative (no absolute Windows paths).
- BeautifulSoup4 + requests for scraping; `pypdf` to read agenda PDF text;
  `requests` to the Brevo API for email.
- Keep parsing resilient to title-format variation.
