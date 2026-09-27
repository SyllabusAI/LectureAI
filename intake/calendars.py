"""Put a lecture's dated to-dos on a calendar: Apple Calendar, Apple
Reminders, or Google Calendar.

    intake calendar --check                 what each calendar is set to, and
                                            whether this Mac may write to it
    intake calendar --connect apple         ask macOS for calendar access
    intake calendar --connect reminders     ask macOS for reminders access
    intake calendar --connect google        sign in to Google Calendar
    intake calendar --enable google         switch one on (--disable: off)

Each one is optional, off until switched on, and independent of Notion and of
the others. The watcher files to every one that is on, after Drive, and one
failing never costs the lecture or the rest (see destinations.py).

Apple Calendar and Apple Reminders go through EventKit, reached from Python
with PyObjC. Syllabus.app already carries PyObjC for its window, so EventKit
adds one small framework wrapper to the bundle, and it is the only route that
can read a macOS permission without asking for it: the Setup page and the
doctor can say "denied" without putting a prompt on screen. An install
without the wrapper (pipx without the [calendar] extra) falls back to
AppleScript through osascript for Apple Calendar, which needs nothing
installed but can only find out whether it is allowed by trying.

Google Calendar uses the calendar.app.created scope with a token of its own,
calendar_token.json. That scope reaches only calendars this app created,
which is why it files into a calendar named after the profile rather than
your main one. Syllabus Macs signed in to an account still sign in to Google
Calendar here, with the Mac's own client; routing it through the account
service the way Drive is routed is a possible follow-up.

An item with no due date goes on no calendar: there is no day to put it on.
The watcher's log counts them. An item whose date was assumed (the next class
meeting, see summarize.normalize_actions) goes on that day, and its notes say
the date was assumed.

Nothing is ever filed twice. Every item that reaches a calendar is recorded
in calendar_items.json in the profile's home, and a later run skips anything
already there for the same lecture, or for the same course and due date,
compared by meaning (tasktext.same) rather than by exact wording. An event
you delete by hand stays deleted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable

from intake import config, tasktext


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


class CalendarError(RuntimeError):
    """A calendar could not be written in a way worth showing the user."""


# --- Which calendars exist, and what they are called ---------------------------

APPLE = "apple_calendar"
REMINDERS = "apple_reminders"
GOOGLE = "google_calendar"
KEYS = (APPLE, REMINDERS, GOOGLE)

LABELS = {APPLE: "Apple Calendar", REMINDERS: "Apple Reminders",
          GOOGLE: "Google Calendar"}

# The .env setting that switches each one on, and the one naming where it files.
SETTING = {APPLE: "APPLE_CALENDAR", REMINDERS: "APPLE_REMINDERS",
           GOOGLE: "GOOGLE_CALENDAR"}
NAME_SETTING = {APPLE: "APPLE_CALENDAR_NAME", REMINDERS: "APPLE_REMINDERS_LIST",
                GOOGLE: "GOOGLE_CALENDAR_NAME"}

# What `intake calendar --connect <word>` accepts for each.
ALIASES = {"apple": APPLE, "calendar": APPLE, APPLE: APPLE,
           "reminders": REMINDERS, REMINDERS: REMINDERS,
           "google": GOOGLE, GOOGLE: GOOGLE}

_TRUE = {"1", "on", "yes", "true", "y"}


def is_on(value: str | None) -> bool:
    return str(value or "").strip().lower() in _TRUE


def enabled(key: str) -> bool:
    """Whether this calendar is switched on in .env."""
    return is_on(getattr(config, SETTING[key], ""))


def calendar_name(key: str) -> str:
    """The calendar (or reminders list) this one files into."""
    name = str(getattr(config, NAME_SETTING[key], "") or "").strip()
    return name or config.PROFILE.title


# --- One to-do, as a calendar sees it -------------------------------------------

@dataclass(frozen=True)
class Entry:
    title: str
    notes: str
    due: date
    at: datetime | None     # a time of day, when the item carried one
    url: str

    @property
    def all_day(self) -> bool:
        return self.at is None


def parse_due(text: str) -> tuple[date, datetime | None] | None:
    """(day, time or None) from a due date, or None when there is no date.

    The summary schema asks for YYYY-MM-DD, so today every item is all day.
    A time after it ("2026-09-30T23:59" or "2026-09-30 23:59") is honored,
    so a deadline with an hour lands on that hour if one ever comes through.
    """
    text = str(text or "").strip()
    if not text:
        return None
    for fmt, timed in (("%Y-%m-%dT%H:%M", True), ("%Y-%m-%d %H:%M", True),
                       ("%Y-%m-%dT%H:%M:%S", True), ("%Y-%m-%d", False)):
        try:
            parsed = datetime.strptime(text[:19] if timed else text[:10], fmt)
        except ValueError:
            continue
        return parsed.date(), (parsed if timed else None)
    return None


def entry_for(item: dict, course: str, source_url: str = "") -> Entry | None:
    """The calendar entry for one action item, or None when it has no date."""
    due = parse_due(item.get("due_date", ""))
    if due is None:
        return None
    task = tasktext.clean(str(item.get("task", ""))) or str(item.get("task", ""))[:140]
    known = course and course != config.UNKNOWN_COURSE
    title = f"{course}: {task}" if known else task

    notes = []
    detail = tasktext.strip_markdown(str(item.get("detail", "") or ""))
    if detail:
        notes.append(detail)
    if known:
        notes.append(f"{config.PROFILE.subject_label}: {course}")
    if item.get("date_source") == "assumed":
        notes.append("No date was given, so this is set for the next "
                     "scheduled meeting.")
    if source_url:
        notes.append(f"Notes: {source_url}")
    return Entry(title=title, notes="\n".join(notes), due=due[0], at=due[1],
                 url=source_url)


# How long a timed entry runs. It ends at the due time, so the block on the
# calendar sits right before the deadline rather than after it.
TIMED_MINUTES = 30


def span(entry: Entry) -> tuple[datetime, datetime]:
    """Start and end, local time. All day runs midnight to midnight."""
    if entry.at is None:
        start = datetime(entry.due.year, entry.due.month, entry.due.day)
        return start, start + timedelta(days=1)
    return entry.at - timedelta(minutes=TIMED_MINUTES), entry.at


# --- What already went where -----------------------------------------------------

class Ledger:
    """calendar_items.json: every item filed to a calendar, and Google's ids.

    Read once per push and written after every item that lands, so a run
    that dies halfway through still remembers the half it finished.
    """

    def __init__(self, data: dict | None = None):
        data = data if isinstance(data, dict) else {}
        items = data.get("items")
        self.items: list[dict] = [i for i in items if isinstance(i, dict)] \
            if isinstance(items, list) else []
        calendars = data.get("calendars")
        self.calendars: dict = calendars if isinstance(calendars, dict) else {}

    @classmethod
    def load(cls) -> "Ledger":
        try:
            return cls(json.loads(config.CALENDAR_LEDGER.read_text()))
        except (OSError, ValueError):
            return cls()

    def save(self) -> None:
        config.write_private(config.CALENDAR_LEDGER, json.dumps(
            {"items": self.items, "calendars": self.calendars}, indent=1))

    def find(self, dest: str, lecture: str, course: str, due: str,
             task: str) -> dict | None:
        """An entry this item repeats, or None.

        The same lecture counts whatever the date says: a re-run that moved
        a deadline is still the same to-do. Another lecture counts when it
        filed the same errand for the same course on the same day, which is
        an assignment brought up in two class periods.
        """
        for entry in self.items:
            if entry.get("dest") != dest:
                continue
            same_lecture = bool(lecture) and entry.get("lecture") == lecture
            same_day = entry.get("course") == course and entry.get("due") == due
            if (same_lecture or same_day) and tasktext.same(task, entry.get("task", "")):
                return entry
        return None

    def add(self, dest: str, lecture: str, course: str, due: str, task: str,
            item_id: str) -> None:
        self.items.append({
            "dest": dest, "lecture": lecture, "course": course, "due": due,
            "task": task, "id": item_id,
            "filed": datetime.now().isoformat(timespec="seconds"),
        })


def item_uid(dest: str, lecture: str, task: str) -> str:
    """A stable id for one item, used where the calendar lets us choose ids.

    Hex is a subset of the base32hex alphabet Google requires for event ids,
    so a second attempt to create the same event is refused by Google itself
    even if the ledger were lost.
    """
    raw = f"{config.PROFILE.name}|{dest}|{lecture}|{tasktext.key(task)}"
    return "sb" + hashlib.sha256(raw.encode()).hexdigest()[:40]


# --- Access, as each backend reports it -------------------------------------------
#
# granted          may write
# not_determined   nobody has been asked yet
# denied           the person said no, or turned it off in System Settings
# restricted       a profile or parental control forbids it
# write_only       may add events but not read calendars (not enough: the
#                  calendar has to be found by name)
# unknown          cannot be read without asking (AppleScript)
# unavailable      this install cannot reach it at all

PRIVACY_PANE = {
    APPLE: "System Settings > Privacy & Security > Calendars",
    REMINDERS: "System Settings > Privacy & Security > Reminders",
}


def _denied_detail(key: str) -> str:
    return (f"Syllabus is not allowed to use {LABELS[key].split()[-1]}. Turn it "
            f"on in {PRIVACY_PANE[key]}, then switch {LABELS[key]} off and on "
            f"again in Setup.")


class EventKitBackend:
    """Apple Calendar or Apple Reminders through EventKit."""

    via = "EventKit"

    def __init__(self, key: str):
        self.key = key
        self._store = None
        self._container = None

    @staticmethod
    def module():
        """EventKit, or None when this install cannot load it."""
        if sys.platform != "darwin":
            return None
        try:
            import EventKit  # noqa: PLC0415  (lazy: the core install lacks it)
        except ImportError:
            return None
        return EventKit

    @property
    def entity(self) -> int:
        return 1 if self.key == REMINDERS else 0   # EKEntityTypeReminder / Event

    def store(self):
        if self._store is None:
            self._store = self.module().EKEventStore.alloc().init()
        return self._store

    def access(self) -> tuple[str, str]:
        ek = self.module()
        status = int(ek.EKEventStore.authorizationStatusForEntityType_(self.entity))
        word = {0: "not_determined", 1: "restricted", 2: "denied",
                3: "granted", 4: "write_only"}.get(status, "unknown")
        what = "reminders" if self.key == REMINDERS else "calendars"
        detail = {
            "granted": f"allowed to use your {what}",
            "not_determined": f"macOS has not asked yet; switching it on in Setup asks",
            "denied": _denied_detail(self.key),
            "restricted": f"a device profile or Screen Time blocks access to {what}",
            "write_only": (f"allowed to add events but not to see your calendars, "
                           f"which it needs to find the {calendar_name(self.key)!r} "
                           f"calendar. Choose Full Access in {PRIVACY_PANE[self.key]}."),
        }.get(word, f"macOS reported access status {status}")
        return word, detail

    def request_access(self, timeout: float = 120.0) -> tuple[str, str]:
        """Show macOS's prompt, if it has not been answered, and wait for it."""
        store = self.store()
        answered = threading.Event()

        def done(*_args):
            answered.set()

        if self.key == REMINDERS and hasattr(store, "requestFullAccessToRemindersWithCompletion_"):
            store.requestFullAccessToRemindersWithCompletion_(done)
        elif self.key != REMINDERS and hasattr(store, "requestFullAccessToEventsWithCompletion_"):
            store.requestFullAccessToEventsWithCompletion_(done)
        else:  # macOS 13 and older
            store.requestAccessToEntityType_completion_(self.entity, done)
        answered.wait(timeout)
        # A store made before the grant keeps seeing no calendars.
        self._store = None
        return self.access()

    def _find_container(self, name: str):
        if self._container is not None and self._container.title() == name:
            return self._container
        ek = self.module()
        store = self.store()
        for cal in store.calendarsForEntityType_(self.entity) or []:
            if cal.title() == name and cal.allowsContentModifications():
                self._container = cal
                return cal
        cal = ek.EKCalendar.calendarForEntityType_eventStore_(self.entity, store)
        cal.setTitle_(name)
        default = (store.defaultCalendarForNewReminders() if self.key == REMINDERS
                   else store.defaultCalendarForNewEvents())
        source = default.source() if default is not None else None
        if source is None:
            wanted = (ek.EKSourceTypeCalDAV, ek.EKSourceTypeLocal)
            source = next((s for s in store.sources() or [] if s.sourceType() in wanted), None)
        if source is None:
            raise CalendarError(f"found no account to create {name!r} in")
        cal.setSource_(source)
        ok, error = store.saveCalendar_commit_error_(cal, True, None)
        if not ok:
            raise CalendarError(f"could not create {name!r}: {_ns_error(error)}")
        self._container = cal
        return cal

    def create(self, name: str, entry: Entry, uid: str) -> str:
        import Foundation  # noqa: PLC0415
        ek = self.module()
        store = self.store()
        container = self._find_container(name)
        if self.key == REMINDERS:
            reminder = ek.EKReminder.reminderWithEventStore_(store)
            reminder.setTitle_(entry.title)
            reminder.setNotes_(entry.notes)
            parts = Foundation.NSDateComponents.alloc().init()
            parts.setYear_(entry.due.year)
            parts.setMonth_(entry.due.month)
            parts.setDay_(entry.due.day)
            if entry.at is not None:
                parts.setHour_(entry.at.hour)
                parts.setMinute_(entry.at.minute)
            reminder.setDueDateComponents_(parts)
            if entry.url:
                reminder.setURL_(Foundation.NSURL.URLWithString_(entry.url))
            reminder.setCalendar_(container)
            ok, error = store.saveReminder_commit_error_(reminder, True, None)
            if not ok:
                raise CalendarError(_ns_error(error))
            return str(reminder.calendarItemIdentifier())

        start, end = span(entry)
        event = ek.EKEvent.eventWithEventStore_(store)
        event.setTitle_(entry.title)
        event.setNotes_(entry.notes)
        event.setAllDay_(entry.all_day)
        event.setStartDate_(Foundation.NSDate.dateWithTimeIntervalSince1970_(start.timestamp()))
        # EventKit's all-day end is the last day, not the day after.
        last = start if entry.all_day else end
        event.setEndDate_(Foundation.NSDate.dateWithTimeIntervalSince1970_(last.timestamp()))
        if entry.url:
            event.setURL_(Foundation.NSURL.URLWithString_(entry.url))
        event.setCalendar_(container)
        ok, error = store.saveEvent_span_commit_error_(event, ek.EKSpanThisEvent, True, None)
        if not ok:
            raise CalendarError(_ns_error(error))
        return str(event.eventIdentifier())


def _ns_error(error) -> str:
    if error is None:
        return "EventKit refused without saying why"
    try:
        return str(error.localizedDescription())
    except Exception:  # an NSError that cannot describe itself
        return str(error)


# Run with `osascript -e <this> <arguments>`. Every value arrives through argv
# rather than being pasted into the script, so a task can contain quotes,
# backslashes or AppleScript of its own and still be only text.
APPLESCRIPT_CREATE = """
on run argv
  set calName to item 1 of argv
  set theTitle to item 2 of argv
  set theNotes to item 3 of argv
  set theURL to item 4 of argv
  set isAllDay to (item 8 of argv) is "1"
  set startDate to current date
  set day of startDate to 1
  set year of startDate to ((item 5 of argv) as integer)
  set month of startDate to ((item 6 of argv) as integer)
  set day of startDate to ((item 7 of argv) as integer)
  set time of startDate to ((item 9 of argv) as integer)
  set endDate to startDate + ((item 10 of argv) as integer)
  tell application "Calendar"
    if not (exists calendar calName) then
      make new calendar with properties {name:calName}
    end if
    tell calendar calName
      set newEvent to make new event at end with properties {summary:theTitle, start date:startDate, end date:endDate, allday event:isAllDay, description:theNotes}
      if theURL is not "" then set url of newEvent to theURL
      return uid of newEvent
    end tell
  end tell
end run
"""

APPLESCRIPT_PROBE = 'tell application "Calendar" to get name of calendars'


class AppleScriptBackend:
    """Apple Calendar through Calendar.app, for an install without EventKit."""

    via = "AppleScript"

    def __init__(self, key: str = APPLE):
        self.key = key

    @staticmethod
    def available() -> bool:
        return sys.platform == "darwin" and shutil.which("osascript") is not None

    @staticmethod
    def _run(script: str, *args: str, timeout: float = 60.0) -> str:
        try:
            done = subprocess.run(["osascript", "-e", script, *args],
                                  capture_output=True, text=True, timeout=timeout,
                                  stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise CalendarError("Calendar did not answer; if macOS is asking "
                                "for permission, answer it and try again")
        if done.returncode != 0:
            err = (done.stderr or "").strip()
            if "-1743" in err:
                raise CalendarError(
                    "Syllabus is not allowed to control Calendar. Turn it on in "
                    "System Settings > Privacy & Security > Automation.")
            raise CalendarError(f"Calendar said: {err[-300:] or 'unknown error'}")
        return done.stdout.strip()

    def access(self) -> tuple[str, str]:
        return "unknown", ("uses Calendar through AppleScript, which cannot tell "
                           "whether it is allowed without asking; Test asks")

    def request_access(self, timeout: float = 120.0) -> tuple[str, str]:
        try:
            self._run(APPLESCRIPT_PROBE, timeout=timeout)
        except CalendarError as exc:
            return "denied", str(exc)
        return "granted", "allowed to control Calendar"

    def create(self, name: str, entry: Entry, uid: str) -> str:
        start, end = span(entry)
        seconds = start.hour * 3600 + start.minute * 60
        return self._run(APPLESCRIPT_CREATE, name, entry.title, entry.notes,
                         entry.url, str(start.year), str(start.month),
                         str(start.day), "1" if entry.all_day else "0",
                         str(seconds), str(int((end - start).total_seconds())))


GOOGLE_API = "https://www.googleapis.com/calendar/v3"
GOOGLE_TIMEOUT = 30


class GoogleBackend:
    """Google Calendar, in a secondary calendar this app creates."""

    via = "Google Calendar API"

    def __init__(self, key: str = GOOGLE):
        self.key = key
        self._http = None

    # Token handling mirrors upload.get_credentials, against its own file.
    def _token_data(self) -> dict | None:
        try:
            data = json.loads(config.CALENDAR_TOKEN_FILE.read_text())
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def access(self) -> tuple[str, str]:
        data = self._token_data()
        if data is None:
            if config.CALENDAR_TOKEN_FILE.exists():
                return "denied", (f"{config.CALENDAR_TOKEN_FILE.name} is not readable; "
                                  f"connect Google Calendar again")
            return "not_determined", "not signed in to Google Calendar yet"
        scopes = set(data.get("scopes") or [])
        if scopes and not set(config.CALENDAR_SCOPES) <= scopes:
            return "denied", "the saved sign-in is for a different permission; connect again"
        if not data.get("refresh_token"):
            return "denied", "the saved sign-in cannot renew itself; connect again"
        return "granted", f"signed in, {config.CALENDAR_TOKEN_FILE.name} on this Mac"

    def request_access(self, timeout: float = 300.0) -> tuple[str, str]:
        """The Desktop OAuth flow in the browser, like `intake login`."""
        from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: PLC0415
        from intake import google_client  # noqa: PLC0415
        flow = InstalledAppFlow.from_client_config(
            google_client.client_config(), config.CALENDAR_SCOPES)
        try:
            creds = flow.run_local_server(port=0, timeout_seconds=int(timeout))
        except Exception as exc:  # timed out, closed, or refused in the browser
            return "not_determined", f"the Google sign-in was not finished ({exc})"
        if creds is None:
            return "not_determined", "the Google sign-in was not finished"
        config.write_private(config.CALENDAR_TOKEN_FILE, creds.to_json())
        self._http = None
        return self.access()

    def credentials(self):
        from google.auth.transport.requests import Request  # noqa: PLC0415
        from google.oauth2.credentials import Credentials  # noqa: PLC0415
        state, detail = self.access()
        if state != "granted":
            raise CalendarError(f"Google Calendar is not connected: {detail}")
        creds = Credentials.from_authorized_user_file(
            str(config.CALENDAR_TOKEN_FILE), config.CALENDAR_SCOPES)
        if not creds.valid:
            try:
                creds.refresh(Request())
            except Exception as exc:
                raise CalendarError(f"could not renew the Google Calendar sign-in "
                                    f"({exc}); connect it again in Setup")
            config.write_private(config.CALENDAR_TOKEN_FILE, creds.to_json())
        return creds

    def http(self):
        """A requests-style session that signs every call."""
        if self._http is None:
            from google.auth.transport.requests import AuthorizedSession  # noqa: PLC0415
            self._http = AuthorizedSession(self.credentials())
        return self._http

    def _call(self, method: str, path: str, body: dict | None = None):
        try:
            response = self.http().request(method, f"{GOOGLE_API}{path}", json=body,
                                           timeout=GOOGLE_TIMEOUT)
        except CalendarError:
            raise
        except Exception as exc:
            raise CalendarError(f"could not reach Google Calendar: {exc}")
        return response

    @staticmethod
    def _problem(response) -> str:
        try:
            error = response.json().get("error", {})
            message = error.get("message") if isinstance(error, dict) else str(error)
        except ValueError:
            message = (response.text or "")[:200]
        return f"Google Calendar returned {response.status_code}: {message or 'no detail'}"

    def calendar_id(self, name: str, ledger: Ledger, fresh: bool = False) -> str:
        """The id of this app's calendar called `name`, created when missing.

        calendar.app.created cannot list your other calendars, so the id is
        remembered in the ledger rather than searched for.
        """
        known = ledger.calendars.get(f"google:{name}")
        if known and not fresh:
            return known
        response = self._call("POST", "/calendars", {
            "summary": name,
            "description": f"Deadlines from {config.PROFILE.title}.",
        })
        if response.status_code >= 400:
            raise CalendarError(self._problem(response))
        cal_id = response.json().get("id", "")
        if not cal_id:
            raise CalendarError("Google Calendar created a calendar but gave no id")
        ledger.calendars[f"google:{name}"] = cal_id
        ledger.save()
        return cal_id

    def create(self, name: str, entry: Entry, uid: str, ledger: Ledger | None = None) -> str:
        ledger = ledger if ledger is not None else Ledger.load()
        body: dict = {"id": uid, "summary": entry.title, "description": entry.notes}
        start, end = span(entry)
        if entry.all_day:
            body["start"] = {"date": start.date().isoformat()}
            body["end"] = {"date": end.date().isoformat()}
        else:
            body["start"] = {"dateTime": start.astimezone().isoformat()}
            body["end"] = {"dateTime": end.astimezone().isoformat()}
        if entry.url.startswith(("https://", "http://")):
            body["source"] = {"title": "Notes", "url": entry.url}

        cal_id = self.calendar_id(name, ledger)
        response = self._call("POST", f"/calendars/{cal_id}/events", body)
        if response.status_code == 404:
            # The calendar was deleted in Google Calendar. Make it again, once.
            cal_id = self.calendar_id(name, ledger, fresh=True)
            response = self._call("POST", f"/calendars/{cal_id}/events", body)
        if response.status_code == 409:
            return uid   # this exact event already exists: filed on an earlier run
        if response.status_code >= 400:
            raise CalendarError(self._problem(response))
        return response.json().get("id", uid)

    def test(self) -> tuple[str, str]:
        """A live check: renew the sign-in and make one cheap read."""
        state, detail = self.access()
        if state != "granted":
            return state, detail
        ledger = Ledger.load()
        known = ledger.calendars.get(f"google:{calendar_name(self.key)}")
        if not known:
            self.credentials()
            return "granted", (f"signed in; the {calendar_name(self.key)!r} calendar is "
                               f"created with the first deadline")
        response = self._call("GET", f"/calendars/{known}")
        if response.status_code == 404:
            return "granted", (f"signed in; the {calendar_name(self.key)!r} calendar was "
                               f"deleted and is made again with the next deadline")
        if response.status_code >= 400:
            return "error", self._problem(response)
        return "granted", f"signed in and able to see {calendar_name(self.key)!r}"


def backend(key: str):
    """The backend that reaches this calendar here, or None if nothing can."""
    if key == GOOGLE:
        return GoogleBackend()
    if EventKitBackend.module() is not None:
        return EventKitBackend(key)
    if key == APPLE and AppleScriptBackend.available():
        return AppleScriptBackend()
    return None


def unavailable_detail(key: str) -> str:
    if sys.platform != "darwin":
        return f"{LABELS[key]} needs macOS"
    return (f"{LABELS[key]} needs EventKit, which this install lacks; install it "
            f"with  pipx inject intake pyobjc-framework-EventKit")


# --- Filing -----------------------------------------------------------------------

def _report(problems: list[tuple[str, str]]) -> dict:
    notes, reasons = [], []
    for task, reason in problems:
        notes.append(f"{task[:50]}: {reason}")
        if reason not in reasons:
            reasons.append(reason)
    return {"failed": len(problems), "notes": notes, "reasons": reasons}


def push(key: str, items: list[dict], course: str, source_url: str = "",
         lecture: str = "", reach: Callable | None = None) -> dict:
    """File a lecture's dated action items on one calendar.

    Raises CalendarError only when the calendar cannot be reached at all
    (not installed, not allowed); one item failing is recorded and the next
    one still goes. `reach` swaps the backend, for tests.
    """
    result = dict(_report([]), added=0, skipped=0, undated=0, ids=[])
    if not items:
        return result
    chosen = reach(key) if reach else backend(key)
    if chosen is None:
        raise CalendarError(unavailable_detail(key))
    state, detail = chosen.access()
    if state in ("denied", "restricted", "write_only"):
        raise CalendarError(detail)
    if state == "not_determined":
        raise CalendarError(
            f"{LABELS[key]} has not been allowed yet. Switch it off and on again "
            f"in Setup to connect it.")

    name = calendar_name(key)
    ledger = Ledger.load()
    added, skipped, undated, ids = 0, 0, 0, []
    problems: list[tuple[str, str]] = []
    for item in items:
        task = str(item.get("task", ""))
        entry = entry_for(item, course, source_url)
        if entry is None:
            undated += 1
            continue
        due = entry.due.isoformat()
        if ledger.find(key, lecture, course, due, task):
            skipped += 1
            log(f"  already on {LABELS[key]}: {task[:60]}")
            continue
        uid = item_uid(key, lecture, task)
        try:
            if isinstance(chosen, GoogleBackend):
                item_id = chosen.create(name, entry, uid, ledger)
            else:
                item_id = chosen.create(name, entry, uid)
        except Exception as exc:
            # CalendarError or whatever EventKit threw: one item never costs
            # the rest of the lecture's.
            problems.append((task, str(exc) or type(exc).__name__))
            log(f"  could not add {task[:60]} to {LABELS[key]}: {exc}")
            continue
        ledger.add(key, lecture, course, due, task, item_id)
        try:
            ledger.save()
        except OSError as exc:
            log(f"  could not record {task[:40]!r} as filed ({exc}); a re-run "
                f"may add it again")
        ids.append(item_id)
        added += 1
        log(f"  added to {LABELS[key]} ({due}): {task[:60]}")
    return dict(_report(problems), added=added, skipped=skipped,
                undated=undated, ids=ids)


def outcome_warning(key: str, outcome: dict | None, total: int) -> str:
    """One line naming what never reached this calendar, or "" when all did.

    Undated items are not a failure: a calendar has no day to put them on.
    """
    if not outcome or not outcome.get("failed"):
        return ""
    reasons = outcome.get("reasons") or []
    line = f"{outcome['failed']} of {total} to-dos did not reach {LABELS[key]}"
    if reasons:
        line += ": " + "; ".join(reasons[:2])
        if len(reasons) > 2:
            line += f"; and {len(reasons) - 2} more"
    return " ".join(line.split())


# --- Settings ---------------------------------------------------------------------

def write_settings(updates: dict[str, str]) -> None:
    """Change calendar settings in .env and leave everything else alone."""
    from intake import setup_wizard  # noqa: PLC0415
    allowed = set(SETTING.values()) | set(NAME_SETTING.values())
    unknown = set(updates) - allowed
    if unknown:
        raise ValueError(f"not a calendar setting: {', '.join(sorted(unknown))}")
    values = setup_wizard.read_env(config.ENV_FILE)
    values.update(updates)
    notion_on = bool(values.get("NOTION_TOKEN") and values.get("NOTION_DATABASE"))
    config.ensure_home()
    setup_wizard.write_env(config.ENV_FILE, values, notion_skipped=not notion_on)
    config.reload()


_NAME_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,60}$")


def clean_name(name: str) -> str:
    """A calendar name that is safe to store, or ValueError."""
    name = " ".join(str(name or "").split())
    if not _NAME_RE.match(name):
        raise ValueError("a calendar name is 1 to 60 characters on one line")
    return name


def describe(key: str) -> dict:
    """Everything the Setup page and `--check` show about one calendar.

    Local only: reads permission and token state, never asks macOS for
    anything and never calls Google.
    """
    chosen = backend(key)
    if chosen is None:
        state, detail, via = "unavailable", unavailable_detail(key), ""
    else:
        try:
            state, detail = chosen.access()
        except Exception as exc:  # a broken EventKit load must not break Setup
            state, detail = "unavailable", f"could not read access: {exc}"
        via = chosen.via
    return {
        "key": key, "label": LABELS[key], "enabled": enabled(key),
        "name": calendar_name(key), "state": state, "detail": detail, "via": via,
    }


def describe_all() -> list[dict]:
    return [describe(key) for key in KEYS]


def connect(key: str) -> tuple[str, str]:
    """Ask for access: macOS's prompt, or Google's sign-in in the browser."""
    chosen = backend(key)
    if chosen is None:
        return "unavailable", unavailable_detail(key)
    return chosen.request_access()


def test(key: str) -> tuple[str, str]:
    """Whether filing would work right now. Writes nothing."""
    chosen = backend(key)
    if chosen is None:
        return "unavailable", unavailable_detail(key)
    if isinstance(chosen, GoogleBackend):
        return chosen.test()
    if isinstance(chosen, AppleScriptBackend):
        return chosen.request_access()
    state, detail = chosen.access()
    if state != "granted":
        return state, detail
    found = any(cal.title() == calendar_name(key)
                for cal in chosen.store().calendarsForEntityType_(chosen.entity) or [])
    where = "list" if key == REMINDERS else "calendar"
    return "granted", (f"allowed; the {calendar_name(key)!r} {where} "
                       + ("is there" if found else "is created with the first deadline"))


# --- The command ------------------------------------------------------------------

def render_check() -> str:
    lines = []
    for info in describe_all():
        on = "on " if info["enabled"] else "off"
        via = f" via {info['via']}" if info["via"] else ""
        lines.append(f"{on}  {info['label']:<16} into {info['name']!r}{via}")
        lines.append(f"     {info['state']}: {info['detail']}")
    lines.append("")
    lines.append("Items with no due date are never put on a calendar.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="intake calendar",
        description="Put each lecture's deadlines on Apple Calendar, Apple "
                    "Reminders, or Google Calendar.")
    parser.add_argument("--check", action="store_true",
                        help="show each calendar's setting and access, then exit")
    parser.add_argument("--connect", metavar="WHICH",
                        help="apple, reminders, or google: ask for access")
    parser.add_argument("--test", metavar="WHICH",
                        help="apple, reminders, or google: check that filing would work")
    parser.add_argument("--enable", metavar="WHICH", help="switch one on")
    parser.add_argument("--disable", metavar="WHICH", help="switch one off")
    parser.add_argument("--name", metavar="NAME",
                        help="with --enable: the calendar or list to file into")
    args = parser.parse_args(argv)

    def which(word: str) -> str:
        key = ALIASES.get(word.strip().lower())
        if not key:
            parser.error(f"{word!r}: choose apple, reminders, or google")
        return key

    try:
        if args.connect:
            key = which(args.connect)
            state, detail = connect(key)
            print(f"{LABELS[key]}: {state}: {detail}")
            return 0 if state == "granted" else 1
        if args.test:
            key = which(args.test)
            state, detail = test(key)
            print(f"{LABELS[key]}: {state}: {detail}")
            return 0 if state == "granted" else 1
        if args.enable or args.disable:
            key = which(args.enable or args.disable)
            updates = {SETTING[key]: "on" if args.enable else ""}
            if args.enable and args.name:
                updates[NAME_SETTING[key]] = clean_name(args.name)
            write_settings(updates)
            print(f"{LABELS[key]} is {'on' if args.enable else 'off'}.")
            if args.enable and describe(key)["state"] != "granted":
                print(f"  It still needs access: intake calendar --connect "
                      f"{args.enable}")
            return 0
        print(render_check())
        return 0
    except (CalendarError, ValueError) as exc:
        log(f"error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
