#!/usr/bin/env python3
"""CPUC Meeting Monitor.

Watches the California Public Utilities Commission (CPUC) document site for
newly published meeting documents (the Current Meeting Agenda and the
Hold List) and sends an email alert when each appears for the target meeting.

Design (see CLAUDE.md):
  * Runs ONCE per invocation and self-gates. A scheduler (GitHub Actions cron)
    runs it hourly; this script decides whether each phase is actually "due"
    based on config.json dates and last_seen.json timestamps.
  * All timing is computed in Pacific time.
  * Phase 1 watches the Agenda; once confirmed, Phase 2 watches the Hold List.
  * State (last_seen.json) and the log are committed back to the repo by the
    workflow so they survive between ephemeral runs.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, tzinfo
from pathlib import Path
from urllib.parse import urljoin

# requests and bs4 are imported lazily inside fetch_latest() so the rest of the
# module (parsing, cadence, scheduling) can be imported and tested without them.

try:
    from dotenv import load_dotenv
    load_dotenv()  # loads .env when running locally; harmless if absent
except ImportError:
    pass  # python-dotenv optional; in CI the env comes from secrets

# --------------------------------------------------------------------------
# Paths and constants
# --------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "last_seen.json"
LOG_PATH = BASE_DIR / "monitor.log"

SITE_BASE = "https://docs.cpuc.ca.gov"
AGENDA_URL = (
    "https://docs.cpuc.ca.gov/SearchRes.aspx?DocTypeID=1"
    "&DocTitleStart=Current%20Meeting%20Agenda&Latest=1"
)
HOLD_LIST_URL = (
    "https://docs.cpuc.ca.gov/SearchRes.aspx?DocTypeID=1"
    "&DocTitleStart=Hold%20List&Latest=1"
)

# Additional, independent target: the ALJ Proposed Decision on proceeding
# A2507016 (the Charter/Cox merger). Watched via the CPUC "Decisions and
# Resolutions for Public Comment" list — the same SearchRes.aspx page type used
# above, and the most reliable source (entries carry the proceeding number, ALJ
# name, title, filed date, and a direct PDF link). This watch runs every
# invocation on its own cadence, independent of the meeting cycle.
PROPOSED_DECISIONS_URL = (
    "https://docs.cpuc.ca.gov/SearchRes.aspx?ProposedDecisions=1&DaySearch=30"
)
PROCEEDING_ID = "A2507016"
# Alert when a list entry mentions the proceeding AND at least one of these.
PROCEEDING_KEYWORDS = ("proposed decision", "alj")

# monitor.log is append-only and committed back to the repo every run, so cap it
# to the most recent LOG_MAX_LINES lines to keep the repo from bloating over time.
LOG_MAX_LINES = 5000

REQUEST_TIMEOUT = 30
USER_AGENT = (
    "Mozilla/5.0 (compatible; CPUC-Meeting-Monitor/1.0; "
    "automated document-availability check)"
)

# Email is sent through Brevo's HTTPS web API (port 443). Plain SMTP is blocked
# both by the Cox network and by Yahoo for automated senders, so we don't use it.
BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"
SENDER_NAME = "CPUC Monitor"

# Cadence windows (minimum time between checks).
FIVE_MIN = timedelta(minutes=5)
HOUR = timedelta(hours=1)
THREE_HOURS = timedelta(hours=3)
ONE_DAY = timedelta(hours=24)

# The A2507016 Proposed Decision watch has no known target date (the PD could
# post any business day), so it runs year-round at a fixed cadence. The source
# updates ~once per business day, so every 3 hours catches it the same day
# without needless polling.
PROCEEDING_INTERVAL = THREE_HOURS


# --------------------------------------------------------------------------
# Pacific time (works with or without the tzdata package)
# --------------------------------------------------------------------------
class _USPacific(tzinfo):
    """US Pacific time with DST, used as a fallback when zoneinfo has no data.

    DST rule (since 2007): starts 2nd Sunday of March at 02:00 local, ends
    1st Sunday of November at 02:00 local. PST = UTC-8, PDT = UTC-7.
    """

    _STD = timedelta(hours=-8)
    _DST = timedelta(hours=-7)

    @staticmethod
    def _nth_sunday(year: int, month: int, nth: int) -> date:
        d = date(year, month, 1)
        # weekday(): Mon=0 .. Sun=6
        first_sunday = 1 + (6 - d.weekday()) % 7
        return date(year, month, first_sunday + (nth - 1) * 7)

    def _is_dst(self, dt: datetime) -> bool:
        year = dt.year
        dst_start = datetime(year, 3, self._nth_sunday(year, 3, 2).day, 2, 0)
        dst_end = datetime(year, 11, self._nth_sunday(year, 11, 1).day, 2, 0)
        naive = dt.replace(tzinfo=None)
        return dst_start <= naive < dst_end

    def utcoffset(self, dt):
        return self._DST if self._is_dst(dt) else self._STD

    def dst(self, dt):
        return HOUR if self._is_dst(dt) else timedelta(0)

    def tzname(self, dt):
        return "PDT" if self._is_dst(dt) else "PST"


def _pacific():
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/Los_Angeles")
        # Touch it to make sure the data actually exists.
        datetime.now(tz)
        return tz
    except Exception:
        return _USPacific()


PACIFIC = _pacific()


def now_pacific() -> datetime:
    return datetime.now(PACIFIC)


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
def log(message: str) -> None:
    """Append a Pacific-timestamped line to monitor.log and echo to stdout."""
    stamp = now_pacific().strftime("%Y-%m-%d %H:%M:%S %Z")
    line = f"[{stamp}] {message}"
    print(line, flush=True)
    try:
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError as exc:  # never let logging crash the run
        print(f"[{stamp}] WARNING: could not write log file: {exc}", flush=True)


def trim_log() -> None:
    """Keep monitor.log bounded by retaining only the most recent LOG_MAX_LINES
    lines. Runs once per invocation before any new lines are written."""
    try:
        if not LOG_PATH.exists():
            return
        with LOG_PATH.open(encoding="utf-8") as fh:
            lines = fh.readlines()
        if len(lines) <= LOG_MAX_LINES:
            return
        with LOG_PATH.open("w", encoding="utf-8") as fh:
            fh.writelines(lines[-LOG_MAX_LINES:])
    except OSError as exc:  # never let log rotation crash the run
        print(f"WARNING: could not trim log file: {exc}", flush=True)


# --------------------------------------------------------------------------
# Config and state
# --------------------------------------------------------------------------
def load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def save_state(state: dict) -> None:
    with STATE_PATH.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
        fh.write("\n")


def fresh_doc_state() -> dict:
    return {"confirmed": False, "pdf_url": None, "detected_at": None, "last_checked": None}


def fresh_proceeding_state() -> dict:
    """State for the A2507016 Proposed Decision watch. Kept separate from the
    meeting state (and preserved across meeting resets) so the two never
    interfere. `seen` holds signatures of entries already alerted on, so a
    repeated listing never triggers a duplicate alert."""
    return {
        "id": PROCEEDING_ID,
        "last_checked": None,
        "detected_at": None,
        "seen": [],
    }


def reset_state_for(meeting: dict) -> dict:
    return {
        "current_meeting": {
            "date": meeting["date"],
            "agenda_number": str(meeting.get("agenda_number", "")),
        },
        "agenda": fresh_doc_state(),
        "hold_list": fresh_doc_state(),
    }


def parse_iso_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def select_target_meeting(config: dict, state: dict, today: date):
    """Return (target_meeting_dict, state), resetting/advancing as needed.

    Picks the earliest config meeting whose date has not yet passed and keeps
    last_seen.json's current_meeting pointed at it. Returns (None, state) when
    there are no upcoming meetings.
    """
    meetings = sorted(config.get("meetings", []), key=lambda m: m["date"])
    upcoming = [m for m in meetings if parse_iso_date(m["date"]) >= today]

    if not upcoming:
        return None, state

    target = upcoming[0]
    current = state.get("current_meeting") or {}

    # (Re)initialise state if it points at a different / passed meeting.
    if current.get("date") != target["date"]:
        log(
            f"Target meeting set to {target['date']} "
            f"(Agenda #{target.get('agenda_number')}); resetting state."
        )
        state = reset_state_for(target)
    else:
        # Keep the agenda number in sync if config was edited.
        state["current_meeting"]["agenda_number"] = str(target.get("agenda_number", ""))

    return target, state


# --------------------------------------------------------------------------
# Scraping
# --------------------------------------------------------------------------
MONTHS = (
    "January February March April May June July "
    "August September October November December"
).split()
_DATE_RE = re.compile(
    r"for\s+(" + "|".join(MONTHS) + r")\s+(\d{1,2}),\s*(\d{4})", re.IGNORECASE
)
_AGENDA_RE = re.compile(r"Agenda\s*#?\s*(\d{3,5})", re.IGNORECASE)


def parse_title(title: str):
    """Extract (meeting_date|None, agenda_number|None) from a result title.

    Handles both observed formats, e.g.:
      "Current Meeting Agenda for July 2, 2026 (Agenda #3583 - #3)"
      "Hold List for June 11, 2026 (Agenda 3582) (Final).docx"
    """
    meeting_date = None
    m = _DATE_RE.search(title)
    if m:
        month = MONTHS.index(m.group(1).capitalize()) + 1
        meeting_date = date(int(m.group(3)), month, int(m.group(2)))

    agenda_number = None
    a = _AGENDA_RE.search(title)
    if a:
        agenda_number = a.group(1)

    return meeting_date, agenda_number


def fetch_latest(url: str):
    """Fetch a CPUC search-results URL and return the single latest result.

    Returns a dict {title, pdf_url, published_date, meeting_date,
    agenda_number} or None if no parseable result row is present.
    """
    import requests
    from bs4 import BeautifulSoup

    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    title_cell = soup.find("td", class_="ResultTitleTD")
    if not title_cell:
        return None

    title = title_cell.get_text(" ", strip=True)
    row = title_cell.find_parent("tr")

    pdf_url = None
    if row:
        for anchor in row.find_all("a", href=True):
            if anchor["href"].lower().endswith(".pdf"):
                pdf_url = urljoin(SITE_BASE, anchor["href"])
                break

    published_date = None
    if row:
        date_cell = row.find("td", class_="ResultDateTD")
        if date_cell:
            published_date = date_cell.get_text(strip=True)

    meeting_date, agenda_number = parse_title(title)
    return {
        "title": title,
        "pdf_url": pdf_url,
        "published_date": published_date,
        "meeting_date": meeting_date,
        "agenda_number": agenda_number,
    }


def result_matches_target(result: dict, target: dict) -> bool:
    """True if the scraped result is for the target meeting.

    Matches primarily on the meeting date; if the date is unparseable, falls
    back to the agenda number.
    """
    target_date = parse_iso_date(target["date"])
    target_agenda = str(target.get("agenda_number", "")).strip()

    if result.get("meeting_date"):
        return result["meeting_date"] == target_date
    if result.get("agenda_number") and target_agenda:
        return result["agenda_number"] == target_agenda
    return False


def fetch_proposed_decisions(url: str):
    """Fetch the CPUC Proposed Decisions list and return ALL result rows.

    Unlike fetch_latest() (which returns only the single newest row), this walks
    every result row so we can scan the list for the target proceeding. Each item
    is {title, row_text, pdf_url, published_date}; row_text is the full row so the
    proceeding number and ALJ/Proposed Decision labels are all searchable.
    """
    import requests
    from bs4 import BeautifulSoup

    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    results = []
    for title_cell in soup.find_all("td", class_="ResultTitleTD"):
        row = title_cell.find_parent("tr")
        title = title_cell.get_text(" ", strip=True)
        row_text = row.get_text(" ", strip=True) if row else title

        pdf_url = None
        published_date = None
        if row:
            for anchor in row.find_all("a", href=True):
                if anchor["href"].lower().endswith(".pdf"):
                    pdf_url = urljoin(SITE_BASE, anchor["href"])
                    break
            date_cell = row.find("td", class_="ResultDateTD")
            if date_cell:
                published_date = date_cell.get_text(strip=True)

        results.append(
            {
                "title": title,
                "row_text": row_text,
                "pdf_url": pdf_url,
                "published_date": published_date,
            }
        )
    return results


def _normalize(text: str) -> str:
    """Lowercase and strip all non-alphanumerics so proceeding numbers match
    regardless of punctuation (e.g. 'A.25-07-016' -> 'a2507016')."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def proceeding_entry_matches(row_text: str) -> bool:
    """True if a list row is for the target proceeding AND mentions a Proposed
    Decision / ALJ (the combination the alert requires)."""
    if PROCEEDING_ID.lower() not in _normalize(row_text):
        return False
    low = row_text.lower()
    return any(keyword in low for keyword in PROCEEDING_KEYWORDS)


def proceeding_signature(entry: dict) -> str:
    """A stable identifier for a matched entry, used to avoid re-alerting. The
    PDF link is unique per document; fall back to a hash of the row text."""
    if entry.get("pdf_url"):
        return entry["pdf_url"]
    return "sig:" + hashlib.md5(_normalize(entry["row_text"]).encode("utf-8")).hexdigest()


def fetch_pdf_text(url: str) -> str:
    """Download a PDF and return its extracted text. Raises on network/parse
    failure so callers can distinguish 'unreadable' from 'read, no match'."""
    import requests
    from io import BytesIO
    from pypdf import PdfReader

    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    reader = PdfReader(BytesIO(resp.content))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def agenda_mentions_proceeding(pdf_url):
    """Whether PROCEEDING_ID appears in the agenda PDF text.

    Returns True/False when the PDF is read successfully, or None when it can't
    be fetched/parsed (so the email can say 'undetermined' rather than 'absent').
    Proceeding numbers print on agendas as 'A.25-07-016'; _normalize() strips the
    punctuation so they match PROCEEDING_ID ('A2507016').
    """
    if not pdf_url:
        return None
    try:
        text = fetch_pdf_text(pdf_url)
    except Exception as exc:
        log(f"Agenda: could not read PDF for {PROCEEDING_ID} check: {exc!r}")
        return None
    if not text.strip():
        return None
    return PROCEEDING_ID.lower() in _normalize(text)


# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------
def send_email(subject: str, body: str, config: dict, html_body: str = None) -> None:
    """Send an alert through Brevo's HTTPS web API. Raises on failure.

    ALERT_EMAIL (or config["alert_email"]) may contain several recipients
    separated by commas; every address listed receives the alert. The sender
    must be a Brevo-verified address (YAHOO_EMAIL / config["from_email"]).
    """
    import requests

    api_key = os.getenv("BREVO_API_KEY")
    from_email = os.getenv("YAHOO_EMAIL") or config.get("from_email")
    raw_recipients = os.getenv("ALERT_EMAIL") or config.get("alert_email") or ""
    recipients = [addr.strip() for addr in raw_recipients.split(",") if addr.strip()]

    if not api_key or not from_email or not recipients:
        raise RuntimeError(
            "Missing email settings. Need BREVO_API_KEY, a sender "
            "(YAHOO_EMAIL / from_email), and at least one recipient (ALERT_EMAIL)."
        )

    # Put recipients in BCC so they can't see each other's addresses. Brevo
    # still requires a "to", so we address it to the sender itself.
    payload = {
        "sender": {"name": SENDER_NAME, "email": from_email},
        "to": [{"email": from_email, "name": SENDER_NAME}],
        "bcc": [{"email": addr} for addr in recipients],
        "subject": subject,
        "textContent": body,
    }
    if html_body:
        payload["htmlContent"] = html_body
    headers = {
        "api-key": api_key,
        "accept": "application/json",
        "content-type": "application/json",
    }

    resp = requests.post(
        BREVO_API_URL, json=payload, headers=headers, timeout=REQUEST_TIMEOUT
    )
    if resp.status_code not in (200, 201, 202):
        raise RuntimeError(
            f"Brevo rejected the send (HTTP {resp.status_code}): {resp.text[:300]}"
        )
    log(f"Email: sent via Brevo to {len(recipients)} recipient(s).")


def build_email(kind: str, result: dict, target: dict, detected_at: datetime,
                proceeding_on_agenda=None):
    """Return (subject, body) for an Agenda or Hold List alert.

    For the Agenda alert, proceeding_on_agenda (True/False/None) is folded in so
    the same email reports whether proceeding A2507016 appears on the agenda.
    """
    meeting = result.get("meeting_date") or parse_iso_date(target["date"])
    # "%-d" (no leading zero) is non-portable on Windows; fall back gracefully.
    try:
        meeting_str = meeting.strftime("%B %-d, %Y")
    except ValueError:
        meeting_str = meeting.strftime("%B %d, %Y").replace(" 0", " ")

    agenda_no = result.get("agenda_number") or target.get("agenda_number")
    pdf = result.get("pdf_url") or "(no direct PDF link found)"
    detected_str = detected_at.strftime("%Y-%m-%d %H:%M:%S %Z")

    label = "Meeting Agenda" if kind == "agenda" else "Hold List"
    subject = f"CPUC {label} Published — {meeting_str} (Agenda #{agenda_no})"
    body = (
        f"A new CPUC {label} has been published.\n\n"
        f"Meeting date:   {meeting_str}\n"
        f"Agenda number:  #{agenda_no}\n"
        f"Document title: {result.get('title', '')}\n"
        f"Published date: {result.get('published_date', 'unknown')}\n"
        f"PDF link:       {pdf}\n"
        f"Detected at:    {detected_str} (Pacific time)\n"
    )

    if kind == "agenda":
        if proceeding_on_agenda is True:
            subject += f" — {PROCEEDING_ID} ON AGENDA"
            body += (
                f"\nProceeding {PROCEEDING_ID} (Charter/Cox merger): "
                f"YES — this proceeding appears on this agenda.\n"
            )
        elif proceeding_on_agenda is False:
            body += (
                f"\nProceeding {PROCEEDING_ID} (Charter/Cox merger): "
                f"not found on this agenda.\n"
            )
        else:
            body += (
                f"\nProceeding {PROCEEDING_ID} (Charter/Cox merger): "
                f"could not be determined (agenda PDF could not be read).\n"
            )

    return subject, body


def build_proceeding_email(matches: list, now: datetime):
    """Return (subject, body) for the A2507016 Proposed Decision alert. One
    email covers all newly-detected matching documents (usually just one)."""
    detected_str = now.strftime("%Y-%m-%d %H:%M:%S %Z")
    subject = f"CPUC ALERT — Proposed Decision in Proceeding {PROCEEDING_ID}"

    parts = [
        f"A Proposed Decision matching proceeding {PROCEEDING_ID} "
        f"(Charter/Cox merger) has been posted to the CPUC Decisions and "
        f"Resolutions for Public Comment list.\n",
    ]
    for i, m in enumerate(matches, 1):
        pdf = m.get("pdf_url") or "(no direct PDF link found)"
        heading = f"Document {i}:" if len(matches) > 1 else "Document:"
        parts.append(
            f"{heading}\n"
            f"  Title:          {m.get('title', '')}\n"
            f"  Date posted:    {m.get('published_date', 'unknown')}\n"
            f"  PDF link:       {pdf}\n"
        )
    parts.append(
        f"Detected at:    {detected_str} (Pacific time)\n"
        f"Source:         {PROPOSED_DECISIONS_URL}\n"
    )
    return subject, "\n".join(parts)


# --------------------------------------------------------------------------
# Cadence helpers
# --------------------------------------------------------------------------
def parse_dt(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=PACIFIC)
    return dt


def is_due(last_checked, interval: timedelta, now: datetime) -> bool:
    prev = parse_dt(last_checked)
    if prev is None:
        return True
    return (now - prev) >= interval


def agenda_interval(days_until: int):
    """Required minimum interval for Phase 1, or None if outside the window."""
    if days_until > 20:
        return None
    if 16 <= days_until <= 20:
        return ONE_DAY  # watching begins; agenda this early is unlikely
    if 8 <= days_until <= 15:
        # Peak zone: the agenda must be published 10 days in advance, but CPUC's
        # "10 days" could mean calendar days (~day 10) or business days (~day 14,
        # up to 15 with a holiday). Poll frequently across the whole band.
        return FIVE_MIN
    return HOUR  # day 7 and earlier: hourly safety net (agenda is overdue)


def hold_list_interval(days_until: int) -> timedelta:
    """Required minimum interval for Phase 2."""
    return HOUR if days_until <= 3 else THREE_HOURS


# --------------------------------------------------------------------------
# Phase handlers
# --------------------------------------------------------------------------
def run_phase_agenda(state, target, config, now, days_until) -> bool:
    """Phase 1. Returns True if the agenda is confirmed (now or already)."""
    if state["agenda"]["confirmed"]:
        return True

    interval = agenda_interval(days_until)
    if interval is None:
        log(f"Agenda: {days_until} days out (>12) — not checking yet.")
        return False

    if not is_due(state["agenda"]["last_checked"], interval, now):
        log(
            f"Agenda: not due yet ({days_until} days out, "
            f"interval {interval}). Last checked {state['agenda']['last_checked']}."
        )
        return False

    log(f"Agenda: checking ({days_until} days out, interval {interval}).")
    state["agenda"]["last_checked"] = now.isoformat()

    try:
        result = fetch_latest(AGENDA_URL)
    except Exception as exc:
        log(f"Agenda: fetch error: {exc!r}")
        return False

    if not result:
        log("Agenda: no result row found on page.")
        return False

    log(f"Agenda: latest is '{result['title']}'.")
    if not result_matches_target(result, target):
        log(
            f"Agenda: does not match target {target['date']} "
            f"(Agenda #{target.get('agenda_number')}) yet."
        )
        return False

    # Read the agenda PDF to report whether A2507016 is on this agenda.
    on_agenda = agenda_mentions_proceeding(result["pdf_url"])
    if on_agenda is True:
        log(f"Agenda: {PROCEEDING_ID} FOUND on this agenda.")
    elif on_agenda is False:
        log(f"Agenda: {PROCEEDING_ID} not on this agenda.")
    else:
        log(f"Agenda: {PROCEEDING_ID} presence undetermined (PDF unreadable).")

    subject, body = build_email("agenda", result, target, now, proceeding_on_agenda=on_agenda)
    try:
        send_email(subject, body, config)
    except Exception as exc:
        log(f"Agenda: MATCH found but email failed: {exc!r}. Will retry next run.")
        return False

    state["agenda"].update(
        confirmed=True, pdf_url=result["pdf_url"], detected_at=now.isoformat()
    )
    log(f"Agenda: CONFIRMED and alert sent. PDF: {result['pdf_url']}")
    return True


def run_phase_hold_list(state, target, config, now, days_until) -> None:
    """Phase 2. Only call after the agenda is confirmed."""
    if state["hold_list"]["confirmed"]:
        log("Hold List: already confirmed for this meeting. Nothing to do.")
        return

    interval = hold_list_interval(days_until)
    if not is_due(state["hold_list"]["last_checked"], interval, now):
        log(
            f"Hold List: not due yet ({days_until} days out, "
            f"interval {interval}). Last checked {state['hold_list']['last_checked']}."
        )
        return

    log(f"Hold List: checking ({days_until} days out, interval {interval}).")
    state["hold_list"]["last_checked"] = now.isoformat()

    try:
        result = fetch_latest(HOLD_LIST_URL)
    except Exception as exc:
        log(f"Hold List: fetch error: {exc!r}")
        return

    if not result:
        log("Hold List: no result row found on page.")
        return

    log(f"Hold List: latest is '{result['title']}'.")
    if not result_matches_target(result, target):
        log(
            f"Hold List: does not match target {target['date']} "
            f"(Agenda #{target.get('agenda_number')}) yet."
        )
        return

    subject, body = build_email("hold_list", result, target, now)
    try:
        send_email(subject, body, config)
    except Exception as exc:
        log(f"Hold List: MATCH found but email failed: {exc!r}. Will retry next run.")
        return

    state["hold_list"].update(
        confirmed=True, pdf_url=result["pdf_url"], detected_at=now.isoformat()
    )
    log(f"Hold List: CONFIRMED and alert sent. PDF: {result['pdf_url']}")


def run_proceeding_watch(state, config, now) -> None:
    """Independent target: watch the CPUC Proposed Decisions list for an ALJ
    Proposed Decision on proceeding A2507016. Runs every invocation on its own
    3-hour cadence, regardless of the meeting cycle, and sends its own separate
    alert. Neither reads nor writes the agenda/hold_list state.
    """
    pstate = state.setdefault("proceeding", fresh_proceeding_state())
    # Tolerate older/partial state files.
    pstate.setdefault("id", PROCEEDING_ID)
    pstate.setdefault("seen", [])

    if not is_due(pstate.get("last_checked"), PROCEEDING_INTERVAL, now):
        log(
            f"Proceeding {PROCEEDING_ID}: not due yet (interval "
            f"{PROCEEDING_INTERVAL}). Last checked {pstate.get('last_checked')}."
        )
        return

    log(f"Proceeding {PROCEEDING_ID}: checking Proposed Decisions list.")
    pstate["last_checked"] = now.isoformat()

    try:
        results = fetch_proposed_decisions(PROPOSED_DECISIONS_URL)
    except Exception as exc:
        log(f"Proceeding {PROCEEDING_ID}: fetch error: {exc!r}")
        return

    matches = [r for r in results if proceeding_entry_matches(r["row_text"])]
    log(
        f"Proceeding {PROCEEDING_ID}: {len(results)} PD list entries, "
        f"{len(matches)} match the proceeding + keyword filter."
    )

    seen = pstate["seen"]
    new_matches = [m for m in matches if proceeding_signature(m) not in seen]
    if not new_matches:
        if matches:
            log(f"Proceeding {PROCEEDING_ID}: match(es) already alerted — nothing new.")
        return

    subject, body = build_proceeding_email(new_matches, now)
    try:
        send_email(subject, body, config)
    except Exception as exc:
        log(
            f"Proceeding {PROCEEDING_ID}: MATCH found but email failed: {exc!r}. "
            "Will retry next run."
        )
        return

    for m in new_matches:
        seen.append(proceeding_signature(m))
    pstate["detected_at"] = now.isoformat()
    log(
        f"Proceeding {PROCEEDING_ID}: ALERT sent for {len(new_matches)} new "
        f"document(s)."
    )


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main() -> int:
    trim_log()  # cap monitor.log before this run appends to it
    now = now_pacific()
    today = now.date()

    if not CONFIG_PATH.exists():
        log("ERROR: config.json not found. Aborting.")
        return 1
    config = load_json(CONFIG_PATH)

    # On-demand email test (TEST_EMAIL=1). Sends one real email using the
    # configured credentials, then exits without touching the monitor logic.
    if os.getenv("TEST_EMAIL", "").strip().lower() in ("1", "true", "yes"):
        log("TEST_EMAIL set — sending a test email and exiting.")
        try:
            send_email(
                "CPUC Meeting Monitor — You're Subscribed!",
                (
                    "Hello,\n\n"
                    "You've been added to the CPUC Voting Meeting Monitor. This "
                    "is a test email to confirm that notifications are being "
                    "delivered successfully. No action is required.\n\n"
                    "WHAT TO EXPECT\n\n"
                    "The CPUC Voting Meeting Monitor automatically monitors the "
                    "California Public Utilities Commission (CPUC) website and "
                    "sends notifications whenever new voting meeting documents "
                    "are published.\n"
                    "Prior to each voting meeting, you will receive up to two "
                    "notifications:\n\n"
                    "AGENDA ALERT\n"
                    "Sent when the Current Voting Meeting Agenda is published "
                    "(typically about 10 days before the meeting).\n\n"
                    "HOLD LIST ALERT\n"
                    "Sent when the Hold List is published. The Hold List "
                    "identifies agenda items that were originally scheduled for "
                    "a vote but have been postponed to a future voting "
                    "meeting.\n\n"
                    "Each Notification Includes:\n"
                    "  - Voting meeting date and agenda number\n"
                    "  - Direct link to the official PDF document\n"
                    "  - CPUC publication date\n"
                    "  - Date and time the monitor detected the new document "
                    "(Pacific Time)\n\n"
                    "This concludes this test of the CPUC Voting Meeting "
                    "Monitor.\n"
                    "This email will self-destruct in 5 seconds... 😊\n"
                ),
                config,
                html_body=(
                    "<p>Hello,</p>"
                    "<p>You've been added to the CPUC Voting Meeting Monitor. "
                    "This is a test email to confirm that notifications are "
                    "being delivered successfully. No action is required.</p>"
                    "<p><u>WHAT TO EXPECT</u></p>"
                    "<p>The CPUC Voting Meeting Monitor automatically monitors "
                    "the California Public Utilities Commission (CPUC) website "
                    "and sends notifications whenever new voting meeting "
                    "documents are published.<br>"
                    "Prior to each voting meeting, you will receive up to two "
                    "notifications:</p>"
                    "<p><b>AGENDA ALERT</b><br>"
                    "Sent when the Current Voting Meeting Agenda is published "
                    "(typically about 10 days before the meeting).</p>"
                    "<p><b>HOLD LIST ALERT</b><br>"
                    "Sent when the Hold List is published. The Hold List "
                    "identifies agenda items that were originally scheduled for "
                    "a vote but have been postponed to a future voting "
                    "meeting.</p>"
                    "<p style=\"margin-bottom:0\">Each Notification "
                    "Includes:</p>"
                    "<ul style=\"margin:0; padding-left:0; "
                    "list-style-position:inside\">"
                    "<li>Voting meeting date and agenda number</li>"
                    "<li>Direct link to the official PDF document</li>"
                    "<li>CPUC publication date</li>"
                    "<li>Date and time the monitor detected the new document "
                    "(Pacific Time)</li>"
                    "</ul>"
                    "<p>This concludes this test of the CPUC Voting Meeting "
                    "Monitor.<br>"
                    "This email will self-destruct in 5 seconds... 😊</p>"
                ),
            )
            log("Test email sent successfully.")
            return 0
        except Exception as exc:
            log(f"Test email FAILED: {exc!r}")
            return 1

    # On-demand agenda-PDF check (TEST_AGENDA_PDF=<url>). Reads the given agenda
    # PDF, reports whether A2507016 appears (and lists the proceeding numbers it
    # found, for eyeballing), then exits. Lets you dry-run the PDF parse against
    # any agenda without waiting for a real detection. Sends no email.
    test_pdf_url = os.getenv("TEST_AGENDA_PDF", "").strip()
    if test_pdf_url:
        log(f"TEST_AGENDA_PDF set — checking {test_pdf_url}")
        try:
            text = fetch_pdf_text(test_pdf_url)
        except Exception as exc:
            log(f"TEST_AGENDA_PDF: could not read PDF: {exc!r}")
            return 1
        found = PROCEEDING_ID.lower() in _normalize(text)
        procs = sorted(set(re.findall(r"[ARIPC]\.\d{2}-\d{2}-\d{3}", text)))
        log(
            f"TEST_AGENDA_PDF: extracted {len(text)} chars; "
            f"{len(procs)} proceeding number(s) found."
        )
        log(f"TEST_AGENDA_PDF: {PROCEEDING_ID} present = {found}")
        if procs:
            log("TEST_AGENDA_PDF: sample proceedings: " + ", ".join(procs[:20]))
        return 0

    state = load_json(STATE_PATH) if STATE_PATH.exists() else {}

    # Preserve the proceeding watch's state across meeting resets:
    # select_target_meeting() may replace `state` wholesale with a fresh
    # meeting-only dict when the target meeting advances, which would otherwise
    # drop this key. Capture it first, then re-attach below.
    proceeding_state = state.get("proceeding") or fresh_proceeding_state()

    target, state = select_target_meeting(config, state, today)
    state["proceeding"] = proceeding_state

    # Independent target: the A2507016 ALJ Proposed Decision. Runs every
    # invocation on its own cadence, even when the meeting cycle is idle or
    # outside its checking window, and sends its own separate alert.
    run_proceeding_watch(state, config, now)

    if target is None:
        log("No upcoming meetings in config.json. Meeting cycle idle.")
        save_state(state)
        return 0

    days_until = (parse_iso_date(target["date"]) - today).days
    log(
        f"Run start: target meeting {target['date']} "
        f"(Agenda #{target.get('agenda_number')}), {days_until} days out."
    )

    if days_until > 20:
        log("More than 20 days before meeting — no meeting checks performed.")
        save_state(state)
        return 0

    # Phase 1 -> Phase 2 (Phase 2 only runs once the agenda is confirmed).
    agenda_confirmed = run_phase_agenda(state, target, config, now, days_until)
    if agenda_confirmed:
        run_phase_hold_list(state, target, config, now, days_until)
    else:
        log("Hold List: skipped (agenda not yet confirmed).")

    save_state(state)
    log("Run complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
