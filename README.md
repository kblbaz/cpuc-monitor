# CPUC Voting Meeting Monitor

Automatically watches the **California Public Utilities Commission (CPUC)**
document site and emails you when new **voting-meeting documents** are
published — the **Current Meeting Agenda** and the **Hold List** — for each
upcoming meeting.

It runs **free, 24/7, in the cloud via GitHub Actions** — your PC does not need
to be on.

---

## Contents

1. [How it works](#how-it-works)
2. [What you need](#what-you-need)
3. [Step 1 — Set up Brevo (free email service)](#step-1--set-up-brevo-free-email-service)
4. [Step 2 — Put the project on GitHub](#step-2--put-the-project-on-github)
5. [Step 3 — Add your secrets to GitHub](#step-3--add-your-secrets-to-github)
6. [Step 4 — Turn on the schedule](#step-4--turn-on-the-schedule)
7. [Updating meeting dates](#updating-meeting-dates-and-agenda-numbers)
8. [Reading the log](#reading-the-log)
9. [Testing locally (optional)](#testing-locally-optional)
10. [How the smart schedule works](#how-the-smart-schedule-works)
11. [Troubleshooting](#troubleshooting)
12. [Appendix: running on PythonAnywhere instead](#appendix-running-on-pythonanywhere-instead)

---

## How it works

- The script (`monitor.py`) checks two constant CPUC search URLs that always
  return the latest Agenda and latest Hold List.
- It compares the **meeting date** in each document title against your list of
  **upcoming meetings** in `config.json`.
- When the Agenda for your target meeting appears, you get **email #1**. After
  that, it starts watching for the **Hold List** and sends **email #2** when it
  appears.
- That Agenda email also **reads the agenda PDF** and tells you, in the same
  email, whether proceeding **A2507016** (the Charter/Cox merger) appears on that
  agenda — or that it doesn't. (No separate email; it's folded in.)
- In the **same run**, it also watches for the **ALJ Proposed Decision on
  proceeding A2507016** (the Charter/Cox merger) on the CPUC Proposed Decisions
  list, and sends a **separate alert** if it appears. This check is independent
  of the meeting cycle — it runs every time, even between meetings.
- A GitHub Actions schedule runs the script **every 5 minutes**. The script itself
  decides whether it's actually time to check (e.g. nothing happens until 20
  days before a meeting), so it never wastes effort or spams the CPUC site.
- Progress is saved in `last_seen.json` and `monitor.log`, which the workflow
  commits back to your repository after each run.

> **Why GitHub Actions and not PythonAnywhere?** The original plan targeted
> PythonAnywhere's free tier, but that tier **blocks outbound email (SMTP)** and
> only allows web access to an **allowlist that doesn't include the CPUC site**.
> GitHub Actions is free and has neither restriction. If you'd still prefer
> PythonAnywhere, see the [appendix](#appendix-running-on-pythonanywhere-instead).

---

## What you need

- A **Yahoo Mail** account (to send the alerts from).
- A free **GitHub** account: <https://github.com/join>.
- About 15 minutes for first-time setup.

You do **not** need to know how to code. Every step below is point-and-click.

---

## Step 1 — Set up Brevo (free email service)

Alerts are sent through **Brevo**, a free email-delivery service. We use this
instead of plain Yahoo/SMTP email because automated SMTP is blocked both by many
home/office networks and by Yahoo itself — Brevo sends over the web (HTTPS),
which works reliably from GitHub.

1. **Create a free account** at <https://www.brevo.com/> (the free plan sends
   300 emails/day — far more than this needs). The signup form asks for company
   info and an address; a personal name and your home address are fine.
2. **Verify a sender address** — this is the "from" address on your alerts:
   - Go to **<https://app.brevo.com/senders/list>** → **Add a sender**.
   - From name: `CPUC Monitor`; From email: the address you'll send from
     (e.g. a Yahoo or Gmail address you control).
   - Brevo emails that address a confirmation link — open it and click to verify.
   - (Ignore the "use a domain you own" suggestion — a verified free-email
     sender is fine for low-volume personal alerts.)
3. **Create an API key**:
   - Go to **<https://app.brevo.com/settings/keys/api>** → **Generate a new API key**.
   - Name it `cpuc-monitor`, generate, and **copy the key** (starts with
     `xkeysib-`). You won't see it again — keep it for Step 3.
   - Do **not** turn on "IP address blocking" for the key — GitHub's IPs change,
     and it would block the alerts.

---

## Step 2 — Put the project on GitHub

You'll upload this whole `CPUC` folder to a new GitHub repository.

### Option A — Upload in the browser (easiest, no tools)

1. Go to **<https://github.com/new>**.
2. **Repository name:** `cpuc-monitor` (anything is fine).
3. Set it to **Private** (recommended) and click **Create repository**.
4. On the next page click **"uploading an existing file"**.
5. Drag in **all** the project files and folders:
   - `monitor.py`, `config.json`, `last_seen.json`, `requirements.txt`,
     `monitor.log`, `README.md`, `CLAUDE.md`, `.gitignore`, `.env.example`
   - the `.github` folder (contains `workflows/monitor.yml`)
   - **Do NOT upload `.env`** — it holds your password. (`.gitignore` already
     excludes it, but if uploading by hand, just skip it.)
6. Click **Commit changes**.

> Tip: GitHub's drag-and-drop sometimes flattens folders. Make sure the workflow
> ends up at exactly `.github/workflows/monitor.yml` in your repo. If it didn't,
> use **Add file → Create new file**, type that full path as the name, and paste
> the file's contents.

### Option B — Using Git on your PC

```bash
cd "CPUC"
git init
git add .
git commit -m "Initial commit: CPUC meeting monitor"
git branch -M main
git remote add origin https://github.com/<your-username>/cpuc-monitor.git
git push -u origin main
```

`.env` is ignored automatically, so your password stays off GitHub.

---

## Step 3 — Add your secrets to GitHub

Your credentials live as encrypted **repository secrets**, never in the code.

1. In your repo, go to **Settings → Secrets and variables → Actions**.
2. Click **New repository secret** and add these **three** (one at a time):

   | Name | Value |
   |---|---|
   | `BREVO_API_KEY` | your Brevo API key (starts with `xkeysib-`) — see Step 1 |
   | `YAHOO_EMAIL` | your **Brevo-verified sender** address, e.g. `your-sender@example.com` |
   | `ALERT_EMAIL` | where alerts go. For **multiple recipients**, separate them with commas, e.g. `you@example.com, someone@example.com, third@example.com` |

3. Click **Add secret** after each one.

Names must match **exactly** (they're case-sensitive).

---

## Step 4 — Turn on the schedule

1. In your repo, click the **Actions** tab.
2. If prompted, click **"I understand my workflows, enable them."**
3. Select **CPUC Meeting Monitor** in the left sidebar.
4. Click **Run workflow → Run workflow** to do a manual test run now.
5. Click into the run to watch it. You should see it check out the repo, install
   dependencies, run the monitor, and (if within a checking window) report what
   it found.

From now on it runs **automatically every 5 minutes** (free because the repo is
public). You don't need to do anything else until you want to add new meeting dates.

> **Note:** GitHub disables scheduled workflows in a repo that has had **no
> activity for 60 days**. Since this commits its state/log on every run, that
> won't happen while meetings are active — but if your repo ever goes quiet for
> two months, just open the Actions tab and click **Run workflow** once to wake
> it back up.

---

## Updating meeting dates and agenda numbers

This is the only ongoing maintenance. Edit **`config.json`** whenever you know
upcoming meeting dates.

1. In your repo, open **`config.json`** and click the **pencil (Edit)** icon.
2. Update the `meetings` list. Keep them in date order; add as many as you like:

   ```json
   {
     "meetings": [
       { "date": "2026-07-16", "agenda_number": "3584" },
       { "date": "2026-08-13", "agenda_number": "3585" },
       { "date": "2026-09-03", "agenda_number": "3586" }
     ],
     "alert_email": "",
     "from_email": ""
   }
   ```

   - `date` must be `YYYY-MM-DD`.
   - `agenda_number` is a best guess used as a secondary check; matching is done
     mainly on the **meeting date**, so a wrong agenda number won't cause a
     missed alert.
3. Click **Commit changes**.

The monitor automatically picks the earliest meeting that hasn't happened yet.
Once a meeting date passes, it advances to the next one and resets its state.

---

## Reading the log

Every run appends to **`monitor.log`**, which is committed back to your repo so
you always have a full history.

- **In the browser:** open `monitor.log` in your repo to see the latest entries,
  e.g.:

  ```
  [2026-07-06 09:07:14 PDT] Run start: target meeting 2026-07-16 (Agenda #3584), 10 days out.
  [2026-07-06 09:07:14 PDT] Agenda: checking (10 days out, interval 3:00:00).
  [2026-07-06 09:07:15 PDT] Agenda: latest is 'Current Meeting Agenda for July 16, 2026 (Agenda #3584)'.
  [2026-07-06 09:07:18 PDT] Agenda: CONFIRMED and alert sent. PDF: https://docs.cpuc.ca.gov/...pdf
  ```

- **Live run output:** the **Actions** tab shows the same lines in real time for
  each run (click a run → the **Run monitor** step).

`last_seen.json` shows the current status at a glance (which documents are
confirmed, their PDF links, and when they were detected).

---

## Testing locally (optional)

You can run it on your own PC to test email before trusting the cloud.

```bash
cd "CPUC"
python -m pip install -r requirements.txt

# Put your real credentials in a local .env (copy from .env.example):
#   BREVO_API_KEY=...
#   YAHOO_EMAIL=...      (your Brevo-verified sender)
#   ALERT_EMAIL=...

python monitor.py
```

`.env` is gitignored, so it never leaves your machine. To force a real check for
testing, temporarily set a meeting `date` in `config.json` to ~10 days from
today and delete `last_seen.json` (it will be recreated).

---

## How the smart schedule works

All times are **Pacific**. GitHub triggers the script every 5 minutes; the
script decides whether to actually check CPUC.

**Phase 1 — Agenda**
| Days before meeting | Action |
|---|---|
| More than 20 | Do nothing |
| 16–20 | Check once per day (watching begins; agenda this early is unlikely) |
| 8–15 | **Check every 5 minutes** — the peak zone |
| 7 or fewer | Check every hour (safety net, in case it's late) |

The agenda must be published 10 days in advance, but CPUC's "10 days" may mean
calendar days (~day 10) or business days (~day 14). The 8–15 day peak zone covers
both interpretations.

When the Agenda for the target meeting is found → send Agenda email, mark it
confirmed, and start Phase 2.

**Phase 2 — Hold List** (only after the Agenda is confirmed)
| Days before meeting | Action |
|---|---|
| More than 3 | Check every 3 hours |
| 3 or fewer | Check every hour |

When the Hold List is found → send Hold List email, mark it confirmed. After the
meeting date passes, the monitor moves on to the next meeting in `config.json`.

The Hold List is **never** checked before the Agenda is confirmed, and **nothing**
in the meeting cycle is checked more than 20 days before a meeting.

**A2507016 — ALJ Proposed Decision** (standing watch, independent of meetings)
| When | Action |
|---|---|
| Every run | Check the CPUC Proposed Decisions list every 3 hours |

Runs regardless of the meeting cycle (even between meetings). It scans the
Proposed Decisions list for a row mentioning proceeding **A2507016**, and
classifies each match:

- **ALJ Proposed Decision** — the assigned judge's proposed outcome.
- **Alternate Proposed Decision** — a Commissioner's competing version, filed
  when a Commissioner wants a different outcome than the ALJ before the vote.

Each kind sends its own **separate** alert (the Alternate alert's subject and
body explain what an Alternate Proposed Decision is). Alerts include the document
title, date posted, and direct PDF link, and each document alerts only once.

---

## Troubleshooting

**No email arrived.**
- Check the **Actions** log for the run — does it say "sent via Brevo", or an
  email error?
- Re-check the secrets (Step 3): `BREVO_API_KEY`, `YAHOO_EMAIL`, `ALERT_EMAIL`.
- Look in the recipient's **Spam** folder and mark the message "not spam".

**"Brevo rejected the send (HTTP 401)".**
- The `BREVO_API_KEY` secret is wrong or was revoked. Generate a new key at
  <https://app.brevo.com/settings/keys/api> and update the secret.

**"Brevo rejected the send (HTTP 400)" mentioning the sender.**
- The `YAHOO_EMAIL` sender isn't verified in Brevo. Verify it under
  <https://app.brevo.com/senders/list> (Step 1) and make sure the secret matches
  the verified address exactly.

**It says "does not match target yet."**
- That's normal — the document for your meeting hasn't been published. The
  Agenda and Hold List pages often show an *older* meeting until the new one
  posts; the monitor correctly waits for **your** target date.

**Scheduled runs stopped.**
- The repo may have been idle 60+ days. Open **Actions → CPUC Meeting Monitor →
  Run workflow** once to re-enable the schedule.

**A title format changed and parsing broke.**
- The parser matches `... for <Month Day, Year> ... (Agenda <number>)`. If CPUC
  changes the wording, update the regexes near the top of `monitor.py`
  (`_DATE_RE`, `_AGENDA_RE`). See `CLAUDE.md` for the observed formats.

---

## Appendix: running on PythonAnywhere instead

The Python code is host-agnostic and will also run on **paid** PythonAnywhere
(the free tier won't work — it blocks SMTP and the CPUC site).

1. Create an account at <https://www.pythonanywhere.com/> (a paid "Hacker" plan
   is required for outbound email + arbitrary site access).
2. Upload the project files under **Files** (you can skip `.github/`).
3. Open a **Bash console** and install dependencies:
   ```bash
   pip install --user -r requirements.txt
   ```
4. Create the credentials as a `.env` file in the project folder (same keys as
   `.env.example`), or set them as environment variables in your task.
5. Go to **Tasks** and add a **scheduled task** that runs hourly:
   ```
   python3 /home/<your-username>/cpuc-monitor/monitor.py
   ```
6. State (`last_seen.json`, `monitor.log`) persists on PythonAnywhere's disk
   automatically — no commit-back needed there.

Everything else (config, schedule logic, emails) behaves identically.
