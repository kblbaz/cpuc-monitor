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

### Phase 2 — Hold List monitoring
- Begins only after the agenda is confirmed for the target meeting.
- Check every 3 hours; increase to every hour within 3 days of the meeting.
- On detect → send **Hold List** email, set `hold_list.confirmed = true`.
- Once the meeting date has passed → advance to the next meeting in
  `config.json` and reset `last_seen.json`.

### Hard rules
- Never check the Hold List before the agenda is confirmed.
- Never check anything more than 20 days before a meeting.
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
| `requirements.txt` | requests, beautifulsoup4, python-dotenv, tzdata |
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
  `textContent` and `htmlContent`. Only the `TEST_EMAIL` message uses this (for
  underline/bold/bulleted formatting) — the Agenda/Hold List alerts are plain text.

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

```json
{
  "current_meeting": { "date": "2026-07-16", "agenda_number": "3584" },
  "agenda":    { "confirmed": false, "pdf_url": null, "detected_at": null, "last_checked": null },
  "hold_list": { "confirmed": false, "pdf_url": null, "detected_at": null, "last_checked": null }
}
```

## Conventions

- Python 3.9+ (`zoneinfo` from stdlib; `tzdata` in requirements for Windows).
- All file paths relative (no absolute Windows paths).
- BeautifulSoup4 + requests for scraping; `requests` to the Brevo API for email.
- Keep parsing resilient to title-format variation.
