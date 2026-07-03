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

3. **Proposed Decisions on proceeding A2507016** (the Charter/Cox merger).
   Checked every run against the CPUC Proposed Decisions list. Sends a separate
   alert for the **ALJ's Proposed Decision** and a distinct alert for any
   **Alternate Proposed Decision** (a Commissioner's competing version). See
   "Proceeding A2507016 watch" below.

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
The "~10 days before the meeting" agenda-publication timing is **customary, not a
rule**, so cadence is driven by whether A2507016 is **agenda-eligible** yet, not
by a fixed peak window (`agenda_interval(days_until, frequent)`):
- More than 20 days before meeting → do not check.
- **Before A2507016 is agenda-eligible** → check **once per day**. It becomes
  eligible only after its ALJ PD drops AND the comment period plus reply period
  (unless waived) fully expire — until then it legally cannot be on any voting
  agenda, so frequent polling is pointless.
- **On/after the eligibility date** → check **every 15 minutes** uniformly across
  the ≤20-day window (the agenda could publish at any point once eligible).
- On detect + match to target meeting → send **Agenda** email,
  set `agenda.confirmed = true`, begin Phase 2.
- Eligibility date = `proceeding.agenda_eligible_date`, computed by `pd_schedule()`
  from the PD (comment_end + reply window; reply=0 if waived) and stored when the
  PD/APD alert fires. It's the **latest** window-close across all detected
  decisions (a later Alternate PD pushes it out), and persists across meeting
  resets — so every subsequent meeting's agenda uses the frequent cadence once
  eligible.
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
- **Match + classify:** a list row must contain the proceeding id
  (`PROCEEDING_ID`, matched punctuation-insensitively so `A.25-07-016` ==
  `A2507016`). `classify_proceeding_entry()` then labels it:
  - `"alternate"` — row has "alternate" + "decision" (catches both "Alternate
    Proposed Decision" and "Alternate Decision") → an **Alternate Proposed
    Decision** (filed by a Commissioner proposing a different outcome than the
    ALJ, before the vote). Checked first, since an APD also contains the
    substring "proposed decision".
  - `"proposed_decision"` — row matches a `PROCEEDING_KEYWORDS` term
    ("proposed decision" / "alj") → the ALJ's original Proposed Decision.
- **De-dup:** each alerted entry's signature (its PDF url, else a hash of the
  row text) is stored in `proceeding.seen`; a repeat listing never re-alerts.
  Because signatures are per-document, the original PD and its Alternate are
  tracked independently.
- **Separate alerts per kind:** `run_proceeding_watch()` groups new documents by
  kind and sends one email per kind via `build_proceeding_email(..., kind=...)`.
  The Alternate alert's subject/body explain it's a Commissioner's alternate to
  the ALJ's decision. Each carries the title, date posted, and direct PDF link —
  never merged with the agenda/hold-list emails.
- **Procedural timeline (`build_pd_timeline_blocks()`):** for both a PD and an
  Alternate PD, the email downloads the document's PDF (`fetch_pdf_text`) and
  reports the comment/reply windows and voting-meeting viability vs. the
  antitrust clearance deadline (the `PROCEEDING_HSR_DEADLINE` constant — kept
  that internal name, but shown to recipients only as "antitrust clearance
  deadline"). It extracts, defensively and labelled "VERIFY":
  - comment-period length (`extract_comment_period_days`; standard
    `STANDARD_COMMENT_DAYS`=20 if not found). NOTE from inspecting real CPUC PDs:
    a standard PD does **not** restate a day count — it says "parties of record
    may file comments on the proposed decision **as provided in Rule 14.3**",
    incorporating the 20-day default by reference. Since a readable PD always
    states *something*, the note wording reflects: an **explicit number** (usually
    a reduction) → "stated in the document — BUT VERIFY"; a bare **Rule 14.3
    reference** → "per Rule 14.3 cited in the document — the standard period"
    (confident, = 20 days); **neither** (only when the PDF couldn't be read) →
    "couldn't confirm from the document — using the standard; please verify".
    Reply comments follow the same pattern,
  - whether reply comments are waived (`reply_comments_waived`; else
    `REPLY_COMMENT_DAYS`=5),
  - whether the comment period is waived (`comment_period_waived` — valid for an
    Alternate PD if parties stipulate; flagged as unusual/emergency for a PD).
  It then computes comment-end, reply-end, the earliest agendizable date, marks
  each config voting meeting VIABLE/NOT, and flags **TIMELINE RISK** if no
  scheduled meeting on/before the antitrust clearance deadline falls after the
  window. That deadline is shown to recipients simply as "antitrust clearance
  deadline" (`HSR_LABEL`; the "HSR"/Hart-Scott-Rodino jargon is intentionally
  omitted). Comment/reply deadlines are computed as calendar days then rolled off
  weekends and CA state holidays to the next business day (`_roll_to_business_day`
  + `CPUC_HOLIDAYS`, per Rule 1.15) — **maintain `CPUC_HOLIDAYS` as years pass**.
  The PD text and Daily Calendar are authoritative. Never raises — failures
  degrade to labelled
  assumptions.
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
- **Every email is HTML with a plain-text fallback, by default.** `send_email()`
  always sets both `textContent` and `htmlContent`; if a caller doesn't pass
  `html_body`, one is generated from the text via `_text_to_html()` (escaped,
  newlines→`<br>`). Every email's HTML is then wrapped by `_wrap_html()` in a
  standard font (`EMAIL_FONT_STACK` = Arial/Helvetica/sans-serif) so all
  messages render consistently (clients otherwise default to a serif). So real
  alerts and every `TEST_*` path are HTML+text without each call site having to
  remember. `build_email()` and
  `build_proceeding_email()` return `(subject, text_body, html_body)` with rich
  HTML. Scraped fields are `html.escape()`d (CPUC titles contain `&`, e.g.
  "PG&E"). The proceeding is shown as `A2507016 (Charter/Cox)` everywhere via
  `PROCEEDING_ID` + `PROCEEDING_LABEL`.

### Debug / test toggles (env vars; each skips monitoring and exits)
- `TEST_EMAIL=1` → send the subscriber "You're Subscribed" test email and exit.
- `TEST_AGENDA_PDF=<url>` → read that agenda PDF, log whether `A2507016` appears
  (plus the proceeding numbers it found), and exit. Sends no email. Use it to
  dry-run the PDF parse without waiting for a real agenda detection.
- `TEST_ALERT=<kind>` → build a **real** alert email from sample data and send it
  (subject prefixed `[TEST]`), then exit. Kinds: `agenda`, `agenda-notfound`,
  `agenda-undetermined`, `holdlist`, `proceeding`, `alternate`. Lets you preview
  the actual alert formatting in your inbox. Value `none`/empty is a no-op.
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
  "proceeding": { "id": "A2507016", "last_checked": null, "detected_at": null, "agenda_eligible_date": null, "seen": [] }
}
```

`proceeding.seen` accumulates signatures (PDF url or a row-text hash) of entries
already alerted on, so the same Proposed Decision listing never re-alerts.
`proceeding.agenda_eligible_date` (set from the PD) gates the agenda cadence:
daily until it passes, every 15 min after.

## Conventions

- Python 3.9+ (`zoneinfo` from stdlib; `tzdata` in requirements for Windows).
- All file paths relative (no absolute Windows paths).
- BeautifulSoup4 + requests for scraping; `pypdf` to read agenda PDF text;
  `requests` to the Brevo API for email.
- Keep parsing resilient to title-format variation.
