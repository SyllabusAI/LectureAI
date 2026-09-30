"""What the panel's dashboard is drawn from: pipeline.log read against the schedule.

Nothing here is stored. The log and the schedule are the only inputs, so the
tiles and charts cannot drift from what was actually filed, and a line
removed from the log disappears from the charts on the next poll.

Two things the log knows that the panel did not use before:

- The stem of a filed lecture starts with the course and the class date
  (ACCT-4321_2026-09-10_Job-Order-Costing). That date is the lecture's; the
  line's own timestamp is only when processing finished, which for a
  recording synced from a phone can be days later.
- A seventh field, added in September 2026, carries a small JSON object of
  measurements: seconds of audio, words in the transcript, to-dos and key
  terms found. Lines without it are read as before and count as lectures
  with nothing measured.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from intake import config

# ACCT-4321_2026-09-10_Job-Order-Costing -> the class date.
STEM_RE = re.compile(r"^(?P<course>.+?)_(?P<date>\d{4}-\d{2}-\d{2})_")

# The schedule knows when a class starts, not how long it runs. 90 minutes is
# the longest common block, so a meeting is "in progress" for that long and
# counts as missed only after it, plus the hour the pipeline needs to
# transcribe and file a recording that was stopped on time.
CLASS_MINUTES = 90
FILING_GRACE_MINUTES = 60

# Weeks of history the per-week chart shows, the current one included.
CHART_WEEKS = 8

MEASURES = ("seconds", "words", "actions", "terms")

# Far past any real lecture (a thousand years of audio), and small enough that
# what comes out still fits anywhere an int is expected downstream.
MAX_MEASURE = 2 ** 53


def parse_measures(text: str) -> dict:
    """The seventh field as numbers, or nothing if it is not what we wrote."""
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for key in MEASURES:
        value = data.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        # JSON has no infinity, but json.loads reads 1e999 as one anyway, and
        # int(inf) raises. This runs inside parse_log, which /api/status
        # reaches before anything else it returns, so one bad measurement in
        # one old line took the whole panel down: no recording state, no
        # Record button, no watcher, no inbox. A number nobody can use is not
        # worth any of that, so it is dropped and the rest of the line stands.
        if value != value or value in (float("inf"), float("-inf")):
            continue
        if not 0 <= value <= MAX_MEASURE:
            continue
        out[key] = int(value)
    return out


def lecture_date(name: str, when: str) -> str:
    """The class date a filed lecture belongs to, YYYY-MM-DD."""
    match = STEM_RE.match(name)
    if match:
        try:
            return date.fromisoformat(match.group("date")).isoformat()
        except ValueError:
            pass
    return when[:10]


def parse_log(text: str) -> list[dict]:
    """Every line of pipeline.log as a row, oldest first.

    Success lines carry five, six, or seven tab-separated fields and error
    lines four, so the field count is what distinguishes them. The sixth
    field, when present, says what did not reach Notion; the seventh holds
    the measurements. Anything else on a line is skipped, not raised: a
    truncated line must not take the panel down.
    """
    rows = []
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) in (5, 6, 7):
            when, course, source, name, url = fields[:5]
            row = {"when": when, "course": course, "source": source,
                   "name": name, "url": url, "error": None,
                   "warning": fields[5] if len(fields) >= 6 else "",
                   "date": lecture_date(name, when)}
            row.update({key: 0 for key in MEASURES})
            if len(fields) == 7:
                row.update(parse_measures(fields[6]))
            rows.append(row)
        elif len(fields) == 4 and fields[1] == "ERROR":
            when, _, source, message = fields
            rows.append({"when": when, "course": "ERROR", "source": source,
                         "name": "", "url": "", "error": message,
                         "warning": "", "date": when[:10]})
    return rows


@dataclass(frozen=True)
class _Slot:
    """One class meeting on one particular date."""
    course: str
    day: date
    start: datetime

    @property
    def ends(self) -> datetime:
        return self.start + timedelta(minutes=CLASS_MINUTES)

    @property
    def due(self) -> datetime:
        """When a recording of it should have been filed."""
        return self.ends + timedelta(minutes=FILING_GRACE_MINUTES)


def _monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _slots(meetings, monday: date) -> list[_Slot]:
    """That week's meetings in calendar order."""
    ordered = sorted(meetings, key=lambda m: m.sort_key)
    return [_Slot(m.course, monday + timedelta(days=config.DAYS.index(m.day)),
                  datetime.combine(monday + timedelta(days=config.DAYS.index(m.day)),
                                   time(hour=m.hour, minute=m.minute)))
            for m in ordered]


def _parse_date(text: str) -> date | None:
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def compute(rows: list[dict], schedule: config.Schedule | None,
            now: datetime | None = None,
            canceled: "set[tuple[str, str]] | dict[tuple[str, str], str] | None" = None
            ) -> dict:
    """Everything the dashboard shows, from the log rows and the schedule.

    Takes them as arguments so it can be tested against a fake log and a
    fake week without touching a home directory or the clock.

    `canceled` holds the (course, class date) pairs that did not meet (see
    cancellations.py). Those meetings are not expected to have a recording,
    so they leave the streak, the coverage rate, and the week's target
    alone. A recording filed against one anyway still counts for the
    student: the cancellation only ever excuses an absence, it never
    discards a lecture.
    """
    now = now or datetime.now()
    today = now.date()
    this_monday = _monday(today)

    lectures = [r for r in rows if r["error"] is None]
    failures = [r for r in rows if r["error"] is not None]
    meetings = list(schedule.meetings) if schedule else []
    tolerance = schedule.tolerance_minutes if schedule else config.DEFAULT_TOLERANCE_MINUTES

    # Colors follow the schedule's course order, so a course keeps its color
    # whatever was recorded this week. Courses that appear in the log but not
    # in the schedule (a code dropped after the semester turned) come after.
    scheduled_codes = sorted({m.course for m in meetings})
    stray = sorted({r["course"] for r in lectures}
                   - set(scheduled_codes) - {config.UNKNOWN_COURSE})
    order = scheduled_codes + stray

    # The lecture that covers a (course, class date). The latest filing wins,
    # so a re-run replaces its predecessor here the way it does in Drive.
    filed: dict[tuple[str, str], dict] = {}
    for r in lectures:
        filed[(r["course"], r["date"])] = r

    def covered(slot: _Slot) -> dict | None:
        return filed.get((slot.course, slot.day.isoformat()))

    # A set of (course, class date) pairs, or a mapping of those to the
    # note the student left. Both are accepted so a caller with nothing to
    # say does not have to invent an empty note for every row.
    note_for = dict(canceled) if isinstance(canceled, dict) else {}
    off = set(note_for) if note_for else set(canceled or ())

    def called_off(slot: _Slot) -> bool:
        """Whether this meeting was called off and stayed that way."""
        return (slot.course, slot.day.isoformat()) in off and covered(slot) is None

    def expected(slot: _Slot) -> bool:
        """Whether a recording of this meeting is something to count on."""
        return not called_off(slot)

    # --- per course ---------------------------------------------------------
    courses = []
    for i, code in enumerate(order):
        mine = [r for r in lectures if r["course"] == code]
        courses.append({
            "code": code, "slot": i, "count": len(mine),
            "minutes": round(sum(r["seconds"] for r in mine) / 60),
            "words": sum(r["words"] for r in mine),
            "actions": sum(r["actions"] for r in mine),
            "last": max((r["date"] for r in mine), default=""),
            "meetings": sum(1 for m in meetings if m.course == code),
        })
    unplaced = [r for r in lectures if r["course"] == config.UNKNOWN_COURSE]
    if unplaced:
        courses.append({
            "code": config.UNKNOWN_COURSE, "slot": -1, "count": len(unplaced),
            "minutes": round(sum(r["seconds"] for r in unplaced) / 60),
            "words": sum(r["words"] for r in unplaced),
            "actions": sum(r["actions"] for r in unplaced),
            "last": max(r["date"] for r in unplaced), "meetings": 0,
        })

    # The week of the first filed lecture: the schedule is only expected to
    # have been met from then on, so weeks before it carry no target.
    first = min((d for d in (_parse_date(r["date"]) for r in lectures)
                 if d is not None), default=None)
    first_monday = _monday(first) if first else None

    # --- lectures per week, oldest first --------------------------------------
    weeks = []
    for back in range(CHART_WEEKS - 1, -1, -1):
        monday = this_monday - timedelta(weeks=back)
        sunday = monday + timedelta(days=6)
        counts: dict[str, int] = {}
        for r in lectures:
            d = _parse_date(r["date"])
            if d is not None and monday <= d <= sunday:
                counts[r["course"]] = counts.get(r["course"], 0) + 1
        # What that week asked for: its meetings, less the ones called off.
        target = (sum(1 for slot in _slots(meetings, monday) if expected(slot))
                  if first_monday and monday >= first_monday else 0)
        weeks.append({"start": monday.isoformat(), "counts": counts,
                      "total": sum(counts.values()),
                      "scheduled": target,
                      "current": monday == this_monday})

    # --- this week, as a grid -------------------------------------------------
    # Monday to Friday always, weekend days only when a class meets then,
    # and no days at all without a schedule: the page then asks for one.
    days_with_class = {m.day for m in meetings}
    shown = [d for d in config.DAYS
             if config.DAYS.index(d) < 5 or d in days_with_class] if meetings else []
    week_slots = _slots(meetings, this_monday)
    days = []
    for name in shown:
        the_date = this_monday + timedelta(days=config.DAYS.index(name))
        classes = []
        for slot in week_slots:
            if slot.day != the_date:
                continue
            hit = covered(slot)
            live_from = slot.start - timedelta(minutes=tolerance)
            classes.append({
                "course": slot.course, "hour": slot.start.hour,
                "minute": slot.start.minute,
                "recorded": hit is not None,
                "url": hit["url"] if hit else "",
                "name": hit["name"] if hit else "",
                "now": live_from <= now < slot.ends,
                "missed": hit is None and now >= slot.due and expected(slot),
                "canceled": called_off(slot),
                "note": note_for.get((slot.course, slot.day.isoformat()), ""),
            })
        days.append({"day": name, "date": the_date.isoformat(),
                     "today": the_date == today, "classes": classes})

    # --- coverage and streak --------------------------------------------------
    # Every meeting from the week of the first filed lecture up to now that
    # has had time to be filed, in order, and whether it was.
    history: list[bool] = []
    excused = 0
    if first_monday is not None and meetings:
        monday = first_monday
        while monday <= this_monday:
            for slot in _slots(meetings, monday):
                if slot.due > now:
                    continue
                if expected(slot):
                    history.append(covered(slot) is not None)
                else:
                    excused += 1
            monday += timedelta(weeks=1)
    streak = 0
    for hit in reversed(history):
        if not hit:
            break
        streak += 1

    due_this_week = [s for s in week_slots if s.due <= now and expected(s)]
    off_this_week = [s for s in week_slots if called_off(s)]
    this_week_rows = [r for r in lectures
                      if (d := _parse_date(r["date"])) is not None
                      and this_monday <= d <= this_monday + timedelta(days=6)]

    return {
        "courses": courses,
        "weeks": weeks,
        "week": {"start": this_monday.isoformat(), "days": days},
        "totals": {
            "lectures": len(lectures),
            "failures": len(failures),
            "courses": len(order),
            "minutes": round(sum(r["seconds"] for r in lectures) / 60),
            "words": sum(r["words"] for r in lectures),
            "actions": sum(r["actions"] for r in lectures),
            "first": first.isoformat() if first else "",
            "this_week": len(this_week_rows),
            "scheduled_this_week": sum(1 for s in week_slots if expected(s)),
            "due_this_week": len(due_this_week),
            "covered_this_week": sum(1 for s in due_this_week if covered(s)),
            "canceled_this_week": len(off_this_week),
            "due": len(history),
            "covered": sum(history),
            "streak": streak,
            "canceled": excused,
        },
    }
