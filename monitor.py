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
import html
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
# Human-friendly label shown in parentheses wherever the proceeding id appears.
PROCEEDING_LABEL = "Charter/Cox"
# Alert when a list entry mentions the proceeding AND at least one of these.
PROCEEDING_KEYWORDS = ("proposed decision", "alj")

# Procedural-timeline analysis for a Proposed / Alternate Proposed Decision
# (CPUC Rules of Practice & Procedure, Rule 14.3). The document states its
# comment period on its face (standard 20 days). Rule nuances:
#   * ALJ PD comment period: may be REDUCED but not waived (except a genuine
#     emergency).
#   * Alternate PD comment period: may be WAIVED if all parties stipulate, or
#     reduced by the ALJ in certain circumstances.
#   * Reply comments (5 days after): may be WAIVED entirely on both PD and APD.
# The item cannot be agendized until both windows fully expire. Relevant because
# the DOJ Hart-Scott-Rodino antitrust clearance expires on the HSR deadline.
PROCEEDING_HSR_DEADLINE = "2026-09-15"  # DOJ HSR antitrust clearance expiry
STANDARD_COMMENT_DAYS = 20              # Rule 14.3 default comment period
REPLY_COMMENT_DAYS = 5                  # Rule 14.3 reply-comment window

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
FIFTEEN_MIN = timedelta(minutes=15)
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
        "agenda_eligible_date": None,  # set from the PD; gates agenda cadence
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


def classify_proceeding_entry(row_text: str):
    """Classify a Proposed Decisions list row for proceeding A2507016.

    Returns:
      "alternate"          — an Alternate Proposed Decision (filed by a
                             Commissioner proposing a different outcome than the
                             ALJ), detected by "alternate" + "decision" (catches
                             both "Alternate Proposed Decision" and "Alternate
                             Decision" wordings).
      "proposed_decision"  — the ALJ's (original) Proposed Decision.
      None                 — not a match for this proceeding.

    The alternate check is done first because an Alternate Proposed Decision also
    contains the substring "proposed decision".
    """
    if PROCEEDING_ID.lower() not in _normalize(row_text):
        return None
    low = row_text.lower()
    if "alternate" in low and "decision" in low:
        return "alternate"
    if any(keyword in low for keyword in PROCEEDING_KEYWORDS):
        return "proposed_decision"
    return None


def proceeding_entry_matches(row_text: str) -> bool:
    """True if a list row is for the target proceeding AND is a Proposed Decision
    or Alternate Proposed Decision. Thin wrapper over classify_proceeding_entry."""
    return classify_proceeding_entry(row_text) is not None


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
def send_email(subject: str, body: str, config: dict, html_body: str = None,
               recipients_override: str = None) -> None:
    """Send an alert through Brevo's HTTPS web API. Raises on failure.

    ALERT_EMAIL (or config["alert_email"]) may contain several recipients
    separated by commas; every address listed receives the alert. The sender
    must be a Brevo-verified address (YAHOO_EMAIL / config["from_email"]).

    recipients_override (used by the TEST_* toggles via TEST_RECIPIENT) sends
    only to the given comma-separated address(es) instead of ALERT_EMAIL, so a
    preview never spams the real subscriber list.
    """
    import requests

    api_key = os.getenv("BREVO_API_KEY")
    from_email = os.getenv("YAHOO_EMAIL") or config.get("from_email")
    raw_recipients = recipients_override or os.getenv("ALERT_EMAIL") or config.get("alert_email") or ""
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


def _pdf_html(pdf: str) -> str:
    """Render a PDF value as an HTML anchor if it's a URL, else escaped text."""
    if pdf.startswith("http"):
        return f'<a href="{html.escape(pdf, quote=True)}">{html.escape(pdf)}</a>'
    return html.escape(pdf)


def build_email(kind: str, result: dict, target: dict, detected_at: datetime,
                proceeding_on_agenda=None):
    """Return (subject, text_body, html_body) for an Agenda or Hold List alert.

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
    title = result.get("title", "")
    published = result.get("published_date", "unknown")

    label = "Meeting Agenda" if kind == "agenda" else "Hold List"
    subject = f"CPUC {label} Published — {meeting_str} (Agenda #{agenda_no})"

    body = (
        f"A new CPUC {label} has been published.\n\n"
        f"Meeting date:   {meeting_str}\n"
        f"Agenda number:  #{agenda_no}\n"
        f"Document title: {title}\n"
        f"Published date: {published}\n"
        f"PDF link:       {pdf}\n"
        f"Detected at:    {detected_str} (Pacific time)\n"
    )
    html_body = (
        f"<p>A new CPUC {label} has been published.</p>"
        f"<p>"
        f"<b>Meeting date:</b> {html.escape(meeting_str)}<br>"
        f"<b>Agenda number:</b> #{html.escape(str(agenda_no))}<br>"
        f"<b>Document title:</b> {html.escape(title)}<br>"
        f"<b>Published date:</b> {html.escape(str(published))}<br>"
        f"<b>PDF link:</b> {_pdf_html(pdf)}<br>"
        f"<b>Detected at:</b> {html.escape(detected_str)} (Pacific time)"
        f"</p>"
    )

    if kind == "agenda":
        proc = f"{PROCEEDING_ID} ({PROCEEDING_LABEL})"
        if proceeding_on_agenda is True:
            subject += f" — {proc} ON AGENDA"
            body += (
                f"\nProceeding {proc}: "
                f"YES — this proceeding appears on this agenda.\n"
            )
            html_body += (
                f"<p><b>Proceeding {proc}: "
                f"YES — this proceeding appears on this agenda.</b></p>"
            )
        elif proceeding_on_agenda is False:
            body += f"\nProceeding {proc}: not found on this agenda.\n"
            html_body += (
                f"<p><b>Proceeding {proc}:</b> not found on this agenda.</p>"
            )
        else:
            body += (
                f"\nProceeding {proc}: "
                f"could not be determined (agenda PDF could not be read).\n"
                f"I'm an AI agent, give me a break.\n"
            )
            html_body += (
                f"<p><b>Proceeding {proc}:</b> "
                f"could not be determined (agenda PDF could not be read).<br>"
                f"<i>I'm an AI agent, give me a break.</i></p>"
            )

    return subject, body, html_body


_NUM_WORDS = {
    "five": 5, "seven": 7, "ten": 10, "twelve": 12, "fourteen": 14,
    "fifteen": 15, "twenty": 20, "twenty-five": 25, "thirty": 30,
}


def _parse_us_date(value):
    """Parse dates as they appear on CPUC listings ('06/22/2026') / in PD text
    ('June 22, 2026'). Returns a date or None."""
    if not value:
        return None
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def extract_comment_period_days(pd_text: str):
    """Best-effort: the comment-period length (in days) stated on the PD's face,
    plus a short surrounding snippet for human verification. (None, None) if not
    found. The ALJ may reduce the standard 20 days, so we read it from the doc."""
    t = re.sub(r"\s+", " ", pd_text).lower()
    patterns = [
        r"comment period (?:of |is |shall be |will be )?(?:reduced to )?(\d{1,3}) days",
        r"(\d{1,3})[- ]day comment period",
        r"comments?[^.]{0,80}?(?:within|not later than|no later than) (\d{1,3}) days",
        r"reduced[^.]{0,60}? to (\d{1,3}) days",
    ]
    for pat in patterns:
        mm = re.search(pat, t)
        if mm:
            days = int(mm.group(1))
            if 1 <= days <= 60:
                s = max(0, mm.start() - 45)
                e = min(len(t), mm.end() + 25)
                return days, "..." + t[s:e].strip() + "..."
    for word, val in _NUM_WORDS.items():
        if re.search(r"comment period[^.]{0,40}?" + word + r" days", t) or \
           re.search(word + r"[- ]day comment period", t):
            return val, f"...{word} day comment period..."
    return None, None


def reply_comments_waived(pd_text: str):
    """True if the PD/APD waives reply comments, False if it sets a reply window,
    None if undetermined. Reply comments may be waived on both PDs and APDs."""
    t = re.sub(r"\s+", " ", pd_text).lower()
    if re.search(r"reply comments?[^.]{0,30}?waived", t) or \
       re.search(r"waiv\w+[^.]{0,30}?reply comments?", t) or \
       re.search(r"no reply comments?", t):
        return True
    if re.search(r"reply comments?[^.]{0,60}?(?:within|not later than) \d{1,2} days", t):
        return False
    return None


def comment_period_waived(pd_text: str):
    """True if the document waives the (initial) comment period entirely. Only
    valid for an Alternate PD (all parties stipulate) or a genuine emergency on a
    PD. 'reply comment' mentions are neutralized first so they don't false-match."""
    t = re.sub(r"\s+", " ", pd_text).lower().replace("reply comment", "reply_x")
    return bool(
        re.search(r"comment period[^.]{0,30}?waived", t)
        or re.search(r"waiv\w+[^.]{0,40}? comment period", t)
    )


def _fmt_date(d) -> str:
    return d.strftime("%B %d, %Y").replace(" 0", " ")


def pd_schedule(entry: dict, now: datetime, kind: str = "proposed_decision") -> dict:
    """Read a PD / Alternate PD PDF and compute its comment/reply schedule.

    Returns a dict of dates and labelled notes; never raises. `reply_end` is the
    agenda-eligibility date — the proceeding cannot be placed on a voting agenda
    until that date (the comment period and, unless waived, the reply period must
    fully expire first).

    Rule nuances by kind:
      * ALJ PD — comment period may be reduced but NOT waived (except a genuine
        emergency); a detected comment waiver is flagged as unusual.
      * Alternate PD — comment period MAY be waived if all parties stipulate.
      * Reply comments — MAY be waived on both.
    """
    is_alt = kind == "alternate"
    doctype = "Alternate Proposed Decision" if is_alt else "Proposed Decision"

    parsed_issued = _parse_us_date(entry.get("published_date"))
    issued = parsed_issued or now.date()
    issued_assumed = parsed_issued is None

    pd_text = ""
    pdf_url = entry.get("pdf_url")
    try:
        if pdf_url and pdf_url.startswith("http"):
            pd_text = fetch_pdf_text(pdf_url)
    except Exception as exc:
        log(f"Proceeding {PROCEEDING_ID}: could not read {doctype} PDF for timeline: {exc!r}")

    comment_days, snippet, waived, comment_waived = None, None, None, False
    if pd_text.strip():
        comment_days, snippet = extract_comment_period_days(pd_text)
        waived = reply_comments_waived(pd_text)
        comment_waived = comment_period_waived(pd_text)

    if comment_waived:
        comment_days_used = 0
        comment_note = (
            "WAIVED (all parties stipulated); VERIFY in the document" if is_alt else
            "appears WAIVED — unusual for a PD (allowed only in a genuine "
            "emergency); VERIFY in the document"
        )
    elif comment_days is None:
        comment_days_used = STANDARD_COMMENT_DAYS
        comment_note = (
            f"NOT auto-detected — assuming the {STANDARD_COMMENT_DAYS}-day "
            f"standard; VERIFY in the document"
        )
    else:
        comment_days_used = comment_days
        comment_note = "as stated in the document" + (f' ({snippet})' if snippet else "")

    if waived is True:
        reply_desc = "WAIVED by the ALJ"
        reply_days_used = 0
    elif waived is False:
        reply_desc = f"{REPLY_COMMENT_DAYS} days (applies)"
        reply_days_used = REPLY_COMMENT_DAYS
    else:
        reply_desc = f"{REPLY_COMMENT_DAYS} days (standard — waiver not detected; VERIFY)"
        reply_days_used = REPLY_COMMENT_DAYS

    comment_end = issued + timedelta(days=comment_days_used)
    reply_end = comment_end + timedelta(days=reply_days_used)

    return {
        "doctype": doctype,
        "issued": issued,
        "issued_assumed": issued_assumed,
        "comment_days_used": comment_days_used,
        "comment_note": comment_note,
        "waived": waived,
        "reply_desc": reply_desc,
        "comment_end": comment_end,
        "reply_end": reply_end,
        "eligible_date": reply_end,  # earliest the item can be agendized
    }


def build_pd_timeline_blocks(entry: dict, config: dict, now: datetime,
                             kind: str = "proposed_decision"):
    """Render (text_block, html_block) for the procedural timeline: comment/reply
    windows, per-meeting viability, and HSR-deadline risk. Reuses a precomputed
    entry['_schedule'] if present (avoids re-reading the PDF), else computes one.
    Extracted values are labelled 'verify' because the legal text varies.
    """
    sched = entry.get("_schedule") or pd_schedule(entry, now, kind)
    doctype = sched["doctype"]
    issued = sched["issued"]
    issued_assumed = sched["issued_assumed"]
    comment_days_used = sched["comment_days_used"]
    comment_note = sched["comment_note"]
    waived = sched["waived"]
    reply_desc = sched["reply_desc"]
    comment_end = sched["comment_end"]
    reply_end = sched["reply_end"]

    hsr = parse_iso_date(PROCEEDING_HSR_DEADLINE)
    meetings = sorted(parse_iso_date(m["date"]) for m in config.get("meetings", []))
    viable = next((d for d in meetings if d > reply_end), None)
    pre_hsr = [d for d in meetings if issued <= d <= hsr]

    # Comment-period display handles the waived (0-day) case cleanly.
    if comment_days_used == 0:
        comment_line = f"- Comment period: {comment_note} -> no comment period"
    else:
        comment_line = (
            f"- Comment period: {comment_days_used} days ({comment_note}) "
            f"-> comments due {_fmt_date(comment_end)}"
        )

    # ---- plain text ----
    lines = [
        "WHAT HAPPENS NEXT (procedural timeline — CPUC Rules of Practice & Procedure):",
        f"- {doctype} issued: {_fmt_date(issued)}"
        + (" (assumed = detection date; not found in doc)" if issued_assumed else ""),
        comment_line,
        f"- Reply comments: {reply_desc}"
        + ("" if waived is True else f" -> reply comments due {_fmt_date(reply_end)}"),
        f"- Earliest the item can be agendized: after {_fmt_date(reply_end)} "
        f"(both windows must fully expire; no walk-on for this proceeding type)",
    ]
    if pre_hsr:
        lines.append(f"- Voting meetings before the HSR deadline ({_fmt_date(hsr)}):")
        for d in pre_hsr:
            ok = d > reply_end
            lines.append(
                f"    * {_fmt_date(d)}: "
                + ("VIABLE" if ok else f"NOT viable (window closes {_fmt_date(reply_end)})")
            )
    if viable is None or viable > hsr:
        lines.append(
            f"** TIMELINE RISK: no scheduled voting meeting on/before the HSR "
            f"deadline ({_fmt_date(hsr)}) falls after the comment/reply window. "
            + (f"Earliest possible meeting is {_fmt_date(viable)}"
               if viable else "No scheduled meeting qualifies")
            + " — the transaction may lose HSR clearance before the CPUC can vote. **"
        )
    else:
        lines.append(
            f"** Earliest viable voting meeting: {_fmt_date(viable)} "
            f"(before the {_fmt_date(hsr)} HSR deadline). **"
        )
    lines.append(
        "(Dates are calendar-day estimates; a deadline landing on a weekend or "
        "holiday rolls to the next business day per CPUC Rule 1.15. The PD text "
        "and CPUC Daily Calendar are authoritative — verify the extracted values.)"
    )
    text_block = "\n".join(lines)

    # ---- HTML ----
    hs = [
        "<p><b>What happens next</b> (procedural timeline — CPUC Rules of "
        "Practice &amp; Procedure):</p><ul>",
        f"<li>{html.escape(doctype)} issued: <b>{html.escape(_fmt_date(issued))}</b>"
        + (" (assumed = detection date)" if issued_assumed else "") + "</li>",
        (f"<li>Comment period: <b>{html.escape(comment_note)}</b> &rarr; no "
         f"comment period</li>"
         if comment_days_used == 0 else
         f"<li>Comment period: <b>{comment_days_used} days</b> "
         f"({html.escape(comment_note)}) &rarr; comments due "
         f"<b>{html.escape(_fmt_date(comment_end))}</b></li>"),
        f"<li>Reply comments: <b>{html.escape(reply_desc)}</b>"
        + ("" if waived is True else
           f" &rarr; due <b>{html.escape(_fmt_date(reply_end))}</b>") + "</li>",
        f"<li>Earliest the item can be agendized: <b>after "
        f"{html.escape(_fmt_date(reply_end))}</b> (both windows must fully "
        f"expire; no walk-on for this proceeding type)</li>",
    ]
    if pre_hsr:
        hs.append(
            f"<li>Voting meetings before the HSR deadline "
            f"({html.escape(_fmt_date(hsr))}):<ul>"
        )
        for d in pre_hsr:
            ok = d > reply_end
            hs.append(
                f"<li>{html.escape(_fmt_date(d))}: <b>"
                + ("VIABLE" if ok else "NOT viable")
                + "</b>"
                + ("" if ok else f" (window closes {html.escape(_fmt_date(reply_end))})")
                + "</li>"
            )
        hs.append("</ul></li>")
    hs.append("</ul>")
    if viable is None or viable > hsr:
        hs.append(
            f'<p style="color:#b00020"><b>⚠ TIMELINE RISK:</b> no scheduled '
            f"voting meeting on/before the HSR deadline "
            f"({html.escape(_fmt_date(hsr))}) falls after the comment/reply "
            f"window. "
            + (f"Earliest possible meeting is <b>{html.escape(_fmt_date(viable))}</b>"
               if viable else "No scheduled meeting qualifies")
            + " — the transaction may lose HSR clearance before the CPUC can vote.</p>"
        )
    else:
        hs.append(
            f"<p><b>Earliest viable voting meeting: "
            f"{html.escape(_fmt_date(viable))}</b> (before the "
            f"{html.escape(_fmt_date(hsr))} HSR deadline).</p>"
        )
    hs.append(
        "<p><i>Dates are calendar-day estimates; a deadline on a weekend or "
        "holiday rolls to the next business day per CPUC Rule 1.15. The PD text "
        "and CPUC Daily Calendar are authoritative — verify the extracted "
        "values.</i></p>"
    )
    html_block = "".join(hs)

    return text_block, html_block


def build_proceeding_email(matches: list, now: datetime, kind: str = "proposed_decision",
                           config: dict = None):
    """Return (subject, text_body, html_body) for an A2507016 alert.

    kind="proposed_decision" → the ALJ's Proposed Decision.
    kind="alternate"         → an Alternate Proposed Decision (a Commissioner
                               proposing a different outcome than the ALJ).
    One email covers all newly-detected documents of that kind (usually one).
    """
    detected_str = now.strftime("%Y-%m-%d %H:%M:%S %Z")
    proc = f"{PROCEEDING_ID} ({PROCEEDING_LABEL})"

    if kind == "alternate":
        subject = f"CPUC ALERT — Alternate Proposed Decision in Proceeding {proc}"
        intro_text = (
            f"An ALTERNATE Proposed Decision for proceeding {proc} has been "
            f"posted to the CPUC Decisions and Resolutions for Public Comment "
            f"list.\n\n"
            f"WHAT THIS MEANS: An Alternate Proposed Decision is filed by a "
            f"Commissioner (not the assigned Administrative Law Judge) who is "
            f"proposing a DIFFERENT outcome than the ALJ's Proposed Decision, "
            f"ahead of the Commission's vote. The Commission may ultimately "
            f"adopt the ALJ's version, the alternate, or neither.\n"
        )
        intro_html = (
            f"<p>An <b>Alternate Proposed Decision</b> for proceeding "
            f"{html.escape(proc)} has been posted to the CPUC Decisions and "
            f"Resolutions for Public Comment list.</p>"
            f"<p><b>What this means:</b> An Alternate Proposed Decision is filed "
            f"by a <b>Commissioner</b> (not the assigned Administrative Law "
            f"Judge) who is proposing a <b>different outcome</b> than the ALJ's "
            f"Proposed Decision, ahead of the Commission's vote. The Commission "
            f"may ultimately adopt the ALJ's version, the alternate, or "
            f"neither.</p>"
        )
    else:
        subject = f"CPUC ALERT — Proposed Decision in Proceeding {proc}"
        intro_text = (
            f"A Proposed Decision matching proceeding {proc} has been posted to "
            f"the CPUC Decisions and Resolutions for Public Comment list.\n"
        )
        intro_html = (
            f"<p>A Proposed Decision matching proceeding {html.escape(proc)} has "
            f"been posted to the CPUC Decisions and Resolutions for Public "
            f"Comment list.</p>"
        )

    parts = [intro_text]
    doc_html = ""
    for i, m in enumerate(matches, 1):
        pdf = m.get("pdf_url") or "(no direct PDF link found)"
        title = m.get("title", "")
        posted = m.get("published_date", "unknown")
        heading = f"Document {i}:" if len(matches) > 1 else "Document:"
        parts.append(
            f"{heading}\n"
            f"  Title:          {title}\n"
            f"  Date posted:    {posted}\n"
            f"  PDF link:       {pdf}\n"
        )
        doc_html += (
            f"<p><b>{heading}</b><br>"
            f"<b>Title:</b> {html.escape(title)}<br>"
            f"<b>Date posted:</b> {html.escape(str(posted))}<br>"
            f"<b>PDF link:</b> {_pdf_html(pdf)}</p>"
        )
        # Procedural timeline (comment/reply windows -> viable voting meeting vs
        # the HSR deadline). Applies to both the ALJ PD and an Alternate PD.
        if config is not None:
            t_text, t_html = build_pd_timeline_blocks(m, config, now, kind=kind)
            parts.append(t_text)
            doc_html += t_html
    parts.append(
        f"Detected at:    {detected_str} (Pacific time)\n"
        f"Source:         {PROPOSED_DECISIONS_URL}\n"
    )
    text_body = "\n".join(parts)

    html_body = (
        f"{intro_html}"
        f"{doc_html}"
        f"<p><b>Detected at:</b> {html.escape(detected_str)} (Pacific time)<br>"
        f"<b>Source:</b> {_pdf_html(PROPOSED_DECISIONS_URL)}</p>"
    )
    return subject, text_body, html_body


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


def agenda_interval(days_until: int, frequent: bool = False):
    """Required minimum interval for Phase 1 (agenda), or None if outside the
    window. Eligibility-aware:

    * Until A2507016 is agenda-eligible — i.e. its ALJ PD has dropped AND the
      comment period plus the reply period (unless waived) have expired — the
      proceeding legally cannot be placed on a voting agenda, so a once-a-day
      agenda check is plenty (`frequent=False`).
    * Once it is agenda-eligible, the agenda that carries A2507016 could publish
      at any point in the run-up (the "~10 days before the meeting" timing is only
      customary, not a rule), so poll frequently and uniformly (`frequent=True`).
    """
    if days_until > 20:
        return None
    return FIFTEEN_MIN if frequent else ONE_DAY


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

    # Cadence depends on whether A2507016 is agenda-eligible yet: daily until its
    # comment/reply windows close (it can't be agendized before then), frequent
    # after. The eligibility date is computed and stored when the PD is detected.
    elig_str = state.get("proceeding", {}).get("agenda_eligible_date")
    eligible = parse_iso_date(elig_str) if elig_str else None
    frequent = eligible is not None and now.date() >= eligible
    if frequent:
        pd_note = "A2507016 agenda-eligible — frequent"
    elif eligible is not None:
        pd_note = f"A2507016 windows open until {eligible} — daily"
    else:
        pd_note = "pre-PD — daily"

    interval = agenda_interval(days_until, frequent)
    if interval is None:
        log(f"Agenda: {days_until} days out (>20) — not checking yet.")
        return False

    if not is_due(state["agenda"]["last_checked"], interval, now):
        log(
            f"Agenda: not due yet ({days_until} days out, interval {interval}, "
            f"{pd_note}). Last checked {state['agenda']['last_checked']}."
        )
        return False

    log(f"Agenda: checking ({days_until} days out, interval {interval}, {pd_note}).")
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

    subject, body, html_body = build_email(
        "agenda", result, target, now, proceeding_on_agenda=on_agenda
    )
    try:
        send_email(subject, body, config, html_body=html_body)
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

    subject, body, html_body = build_email("hold_list", result, target, now)
    try:
        send_email(subject, body, config, html_body=html_body)
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

    # Classify each row as an ALJ Proposed Decision or an Alternate Proposed
    # Decision (or skip). Each kind gets its own alert.
    matches = []
    for r in results:
        kind = classify_proceeding_entry(r["row_text"])
        if kind:
            matches.append((kind, r))
    log(
        f"Proceeding {PROCEEDING_ID}: {len(results)} PD list entries, "
        f"{len(matches)} match the proceeding."
    )

    seen = pstate["seen"]
    new_matches = [(k, r) for (k, r) in matches if proceeding_signature(r) not in seen]
    if not new_matches:
        if matches:
            log(f"Proceeding {PROCEEDING_ID}: match(es) already alerted — nothing new.")
        return

    # Group newly-detected documents by kind and send one alert per kind.
    by_kind = {}
    for k, r in new_matches:
        by_kind.setdefault(k, []).append(r)

    labels = {"alternate": "Alternate Proposed Decision", "proposed_decision": "Proposed Decision"}
    sent_any = False
    for kind, entries in by_kind.items():
        # Precompute each doc's schedule once: reused by the email builder (no
        # re-reading the PDF) and used to update the agenda-eligibility date.
        for r in entries:
            r["_schedule"] = pd_schedule(r, now, kind)
        subject, body, html_body = build_proceeding_email(entries, now, kind=kind, config=config)
        try:
            send_email(subject, body, config, html_body=html_body)
        except Exception as exc:
            log(
                f"Proceeding {PROCEEDING_ID}: {labels[kind]} found but email "
                f"failed: {exc!r}. Will retry next run."
            )
            continue  # leave these unseen so they retry next run
        for r in entries:
            seen.append(proceeding_signature(r))
            # Track the LATEST window-close date across all detected decisions:
            # the item stays agenda-ineligible until every pending window expires.
            elig = r["_schedule"].get("eligible_date")
            if elig:
                cur = pstate.get("agenda_eligible_date")
                cur_d = parse_iso_date(cur) if cur else None
                if cur_d is None or elig > cur_d:
                    pstate["agenda_eligible_date"] = elig.isoformat()
                    log(
                        f"Proceeding {PROCEEDING_ID}: agenda-eligibility date set "
                        f"to {elig.isoformat()} (agenda cadence stays daily until then)."
                    )
        sent_any = True
        log(
            f"Proceeding {PROCEEDING_ID}: ALERT sent for {len(entries)} new "
            f"{labels[kind]}(s)."
        )

    if sent_any:
        pstate["detected_at"] = now.isoformat()


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

    # Optional: restrict the TEST_* preview emails to a single address (kept in
    # the TEST_RECIPIENT secret) so previewing never emails the real
    # ALERT_EMAIL subscriber list. Empty -> falls back to ALERT_EMAIL.
    test_to = os.getenv("TEST_RECIPIENT", "").strip() or None

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
                recipients_override=test_to,
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

    # On-demand sample alert (TEST_ALERT=agenda|agenda-notfound|
    # agenda-undetermined|holdlist|proceeding). Builds a real alert email from
    # sample data and sends it (subject prefixed "[TEST]"), then exits — so you
    # can preview the actual formatting in your inbox without a real detection.
    test_alert = os.getenv("TEST_ALERT", "").strip().lower()
    if test_alert and test_alert != "none":
        log(f"TEST_ALERT={test_alert} — sending a sample alert email and exiting.")
        sample_target = {"date": "2026-07-16", "agenda_number": "3584"}
        try:
            if test_alert.startswith("agenda"):
                on_agenda = {
                    "agenda": True,
                    "agenda-found": True,
                    "agenda-notfound": False,
                    "agenda-undetermined": None,
                }.get(test_alert, True)
                sample = {
                    "title": "Current Meeting Agenda for July 16, 2026 (Agenda #3584)",
                    "pdf_url": "https://docs.cpuc.ca.gov/PublishedDocs/Published/G000/M609/K934/609934454.PDF",
                    "published_date": "07/06/2026",
                    "meeting_date": parse_iso_date("2026-07-16"),
                    "agenda_number": "3584",
                }
                subject, body, html_body = build_email(
                    "agenda", sample, sample_target, now, proceeding_on_agenda=on_agenda
                )
            elif test_alert in ("holdlist", "hold_list", "hold-list"):
                sample = {
                    "title": "Hold List for July 16, 2026 (Agenda 3584) (Final)",
                    "pdf_url": "https://docs.cpuc.ca.gov/PublishedDocs/Published/G000/M610/K111/610111222.PDF",
                    "published_date": "07/13/2026",
                    "meeting_date": parse_iso_date("2026-07-16"),
                    "agenda_number": "3584",
                }
                subject, body, html_body = build_email(
                    "hold_list", sample, sample_target, now
                )
            elif test_alert in ("proceeding", "pd", "proposed", "proposeddecision",
                                 "alternate", "apd"):
                is_alt = test_alert in ("alternate", "apd")
                sample_matches = [{
                    "title": (
                        "Alternate Proposed Decision of Commissioner (Charter Communications / Cox)"
                        if is_alt else
                        "Proposed Decision of ALJ (Charter Communications / Cox)"
                    ),
                    "pdf_url": "https://docs.cpuc.ca.gov/PublishedDocs/Published/G000/M620/K333/620333444.PDF",
                    "published_date": now.strftime("%m/%d/%Y"),
                    "row_text": "Proposed Decision ... Proceeding: A2507016",
                }]
                subject, body, html_body = build_proceeding_email(
                    sample_matches, now,
                    kind="alternate" if is_alt else "proposed_decision",
                    config=config,
                )
            else:
                log(
                    f"TEST_ALERT: unknown value '{test_alert}'. Use one of: "
                    "agenda, agenda-notfound, agenda-undetermined, holdlist, "
                    "proceeding, alternate."
                )
                return 1

            send_email(
                "[TEST] " + subject, body, config,
                html_body=html_body, recipients_override=test_to,
            )
            log("TEST_ALERT: sample alert sent successfully.")
            return 0
        except Exception as exc:
            log(f"TEST_ALERT FAILED: {exc!r}")
            return 1

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
