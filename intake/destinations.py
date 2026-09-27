"""Everywhere a lecture's action items can go, and filing to each of them.

The watcher used to call Notion by name. It now calls file_all(), which asks
every destination here whether it is switched on and files to each one that
is, in order: Notion, then Apple Calendar, Apple Reminders, Google Calendar.

Two rules hold for every destination, and are enforced here rather than
trusted to each module:

- Nothing raises out of file_all. The lecture is already safe in Drive when
  this runs, so an outage or a misconfiguration costs at most that one
  destination's items, which are still in the summary Doc.
- One destination failing never stops the next one.

What did not arrive is recorded with the lecture, one line per destination,
exactly the way Notion's line always was, so the panel shows it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from intake import calendars, notion_tasks

# Each destination's line is capped at this, as Notion's always was, so one
# chatty error cannot push the others out of the log field.
WARNING_LIMIT = 300

# Between destinations' lines in the one field pipeline.log has for them.
SEPARATOR = " | "


@dataclass(frozen=True)
class Destination:
    key: str
    label: str
    stage: str                          # what the panel's progress shows
    enabled: Callable[[], bool]
    push: Callable[[list, str, str, str], dict]   # items, course, url, lecture
    warning: Callable[[dict | None, int], str]
    summary: Callable[[dict], str]      # the log line after a push


# --- Notion, exactly as the watcher always called it -----------------------------

def _notion_enabled() -> bool:
    # Looked up at call time, so a test that swaps notion_tasks.enabled is
    # still the one consulted.
    return notion_tasks.enabled()


def _notion_push(items: list, course: str, source_url: str, lecture: str) -> dict:
    return notion_tasks.push(items, course, source_url=source_url)


def _notion_summary(outcome: dict) -> str:
    return (f"notion: {outcome['added']} added, {outcome['skipped']} already "
            f"there, {outcome['failed']} failed")


NOTION = Destination(
    key="notion", label="Notion", stage="notion",
    enabled=_notion_enabled, push=_notion_push,
    warning=lambda outcome, total: notion_tasks.outcome_warning(outcome, total),
    summary=_notion_summary,
)


# --- Calendars --------------------------------------------------------------------

def _calendar(key: str) -> Destination:
    def push(items: list, course: str, source_url: str, lecture: str) -> dict:
        return calendars.push(key, items, course, source_url, lecture)

    def summary(outcome: dict) -> str:
        line = (f"{calendars.LABELS[key].lower()}: {outcome['added']} added, "
                f"{outcome['skipped']} already there, {outcome['failed']} failed")
        if outcome.get("undated"):
            line += (f", {outcome['undated']} left off because "
                     f"{'it has' if outcome['undated'] == 1 else 'they have'} no due date")
        return line

    return Destination(
        key=key, label=calendars.LABELS[key], stage="calendar",
        enabled=lambda: calendars.enabled(key), push=push,
        warning=lambda outcome, total: calendars.outcome_warning(key, outcome, total),
        summary=summary,
    )


def registry() -> list[Destination]:
    """Every destination, in the order they are filed to."""
    return [NOTION, *(_calendar(key) for key in calendars.KEYS)]


def _collapse(text: str) -> str:
    """One line, capped: it goes into a tab-separated log."""
    return " ".join(str(text).split())[:WARNING_LIMIT]


def file_all(items: list[dict], course: str, source_url: str = "",
             lecture: str = "", status: Callable | None = None,
             log: Callable[[str], None] = print,
             only: list[Destination] | None = None) -> dict:
    """File the lecture's action items to every destination that is on.

    Returns {"outcomes": {key: result}, "warnings": {key: line},
    "warning": every line joined, for the log}. Never raises.
    """
    outcomes: dict[str, dict] = {}
    warnings: dict[str, str] = {}
    total = len(items)
    any_on = False
    for dest in (only if only is not None else registry()):
        try:
            on = dest.enabled()
        except Exception as exc:  # a broken setting is that destination's problem
            log(f"  {dest.label}: could not tell whether it is on ({exc})")
            continue
        if not on:
            continue
        any_on = True
        # Notion has always been called even for a lecture with no to-dos
        # (and returns without a request); a calendar has nothing to do.
        if not items and dest.key != NOTION.key:
            continue
        if status:
            status(dest.stage, f"{total} action items")
        try:
            outcome = dest.push(items, course, source_url, lecture)
            outcomes[dest.key] = outcome
            log(f"  {dest.summary(outcome)}")
            line = dest.warning(outcome, total)
        except Exception as exc:
            log(f"  {dest.label.lower()}: skipped ({exc})")
            line = f"{dest.label} was not reached: {exc}"
        line = _collapse(line)
        if line:
            warnings[dest.key] = line

    if not any_on and items:
        log(f"  {total} action items (Notion not configured)")

    return {"outcomes": outcomes, "warnings": warnings,
            "warning": SEPARATOR.join(warnings.values())}
