"""Push a lecture's action items into a Notion to-do database.

    intake notion --check          # show what it matched in your database
    intake notion --setup          # add Due / Course / Source if missing

Nothing here is required. With NOTION_TOKEN unset the pipeline skips Notion
entirely and behaves exactly as it did before.

Notion split databases from data sources in API version 2025-09-03: a database
is now a container whose child data source holds the schema and the rows. So a
database id gets resolved to a data source id first, and pages are parented to
the data source, not the database.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime
from pathlib import Path

import requests

from intake import config, tasktext

API = "https://api.notion.com/v1"
TIMEOUT = 30

# A Notion id is 32 hex characters, written bare or as a dashed UUID.
#
# Both patterns are anchored so they can't match part of the page title that
# Notion puts in the URL ahead of the id. A title like "Deface Added Beef
# Cafe" is hex all the way through, and a loose pattern happily matched the
# title plus the front of the real id and returned a database that doesn't
# exist.
UUID_RE = re.compile(
    r"(?<![0-9a-fA-F])"
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"(?![0-9a-fA-F])"
)
HEX32_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])")

# How property names are matched when the database isn't explicitly mapped.
# First hit wins, so the more specific names come first.
DUE_HINTS = ("due", "deadline", "when", "date")
COURSE_HINTS = ("course", "class", "subject")
KIND_HINTS = ("type", "kind", "category", "label")
SOURCE_HINTS = ("source", "lecture", "notes", "link", "url", "reference")


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


class NotionError(RuntimeError):
    """A Notion request failed in a way worth showing the user."""


def enabled() -> bool:
    """Whether Notion is configured at all."""
    return bool(config.NOTION_TOKEN and config.NOTION_DATABASE)


def extract_id(value: str) -> str:
    """Pull a Notion id out of a URL, or pass a bare id through.

    The query string goes first, since that is where the ?v= view id lives and
    it looks exactly like a database id. Of what remains, the last match wins:
    Notion puts the id at the end of the path, after the workspace and the
    page title.
    """
    path = str(value or "").split("?")[0].split("#")[0]
    found = list(UUID_RE.finditer(path)) + list(HEX32_RE.finditer(path))
    if not found:
        raise NotionError(
            f"could not find a Notion id in {value!r}. Open the database in "
            f"Notion, copy the URL from your browser, and paste the whole "
            f"thing."
        )
    return max(found, key=lambda m: m.start()).group(0).replace("-", "")


def _request(method: str, path: str, payload: dict | None = None) -> dict:
    if not config.NOTION_TOKEN:
        raise NotionError(
            "NOTION_TOKEN is not set. Create an internal integration at "
            "notion.so/my-integrations, copy its secret into .env, and share "
            "your to-do database with it."
        )
    response = requests.request(
        method, f"{API}{path}",
        headers={
            "Authorization": f"Bearer {config.NOTION_TOKEN}",
            "Notion-Version": config.NOTION_VERSION,
            "Content-Type": "application/json",
        },
        json=payload, timeout=TIMEOUT,
    )
    if response.status_code >= 400:
        detail = ""
        try:
            body = response.json()
            detail = body.get("message") or body.get("code") or ""
        except ValueError:
            detail = response.text[:300]
        if response.status_code == 404:
            detail += (
                "  (a 404 usually means the integration hasn't been given "
                "access: open the database in Notion, Connections, and add "
                "your integration)"
            )
        raise NotionError(f"Notion returned {response.status_code}: {detail}")
    return response.json()


def resolve_data_source(database: str = "") -> tuple[str, str]:
    """(data_source_id, title) for the configured database.

    Since 2025-09-03 the schema and rows live on a data source hanging off the
    database, so this hop is required before anything can be read or written.
    """
    # Check the token first: a missing one is the likelier problem, and
    # complaining about the database id sends you looking in the wrong place.
    if not config.NOTION_TOKEN:
        raise NotionError(
            "NOTION_TOKEN is not set. Create an internal integration at "
            "notion.so/my-integrations, put its secret in .env, then share "
            "your to-do database with it (database ... menu, Connections)."
        )
    if not (database or config.NOTION_DATABASE):
        raise NotionError(
            "NOTION_DATABASE is not set. Paste your to-do database's URL "
            "into .env."
        )
    database_id = extract_id(database or config.NOTION_DATABASE)
    info = _request("GET", f"/databases/{database_id}")
    sources = info.get("data_sources") or []
    if not sources:
        raise NotionError(
            f"database {database_id} reports no data sources, so there is "
            f"nowhere to add tasks."
        )
    if len(sources) > 1:
        log(f"  database has {len(sources)} data sources; using "
            f"{sources[0].get('name')!r}")
    title = "".join(t.get("plain_text", "") for t in info.get("title", []))
    return sources[0]["id"], title or "(untitled)"


def get_schema(data_source_id: str) -> dict:
    """The data source's property definitions, keyed by property name."""
    return _request("GET", f"/data_sources/{data_source_id}").get("properties", {})


def _find(properties: dict, hints: tuple[str, ...], types: tuple[str, ...],
          override: str = "", taken: set[str] | None = None) -> str | None:
    """Name of the property to use for one role, or None if there isn't one.

    `taken` holds properties already claimed by an earlier role. Without it a
    database with a single select property gave that property to both `course`
    and `kind`, and since both are written on create, the course code was
    immediately overwritten by the item type.
    """
    if override:
        if override in properties:
            return override
        raise NotionError(
            f"property {override!r} is not in the database. Available: "
            f"{', '.join(sorted(properties)) or '(none)'}"
        )
    claimed = taken or set()
    candidates = {name: spec for name, spec in properties.items()
                  if spec.get("type") in types and name not in claimed}
    for hint in hints:
        for name in candidates:
            if hint in name.lower():
                return name
    # A database with exactly one unclaimed property of the right type needs
    # no hint to be unambiguous.
    return next(iter(candidates)) if len(candidates) == 1 else None


def map_properties(properties: dict) -> dict:
    """Work out which property plays which role in this particular database.

    Databases are personal, so nothing is assumed beyond the title, which
    Notion guarantees exists exactly once. Anything unmatched is skipped rather
    than created: adding properties to someone's to-do list is not this
    script's business.
    """
    title = next((name for name, spec in properties.items()
                  if spec.get("type") == "title"), None)
    if not title:
        raise NotionError("database has no title property, which Notion requires")

    # Claimed in priority order, each role excluding what earlier ones took.
    # Course before kind: knowing which class a task belongs to matters more
    # than labelling it a reading, so it wins a single shared select.
    mapping = {"title": title}
    taken = {title}
    for role, hints, types, override in (
        ("due", DUE_HINTS, ("date",), config.NOTION_PROP_DUE),
        ("course", COURSE_HINTS, ("select", "multi_select", "status"),
         config.NOTION_PROP_COURSE),
        ("kind", KIND_HINTS, ("select", "multi_select"), config.NOTION_PROP_KIND),
        ("source", SOURCE_HINTS, ("url", "rich_text"), config.NOTION_PROP_SOURCE),
    ):
        found = _find(properties, hints, types, override, taken)
        mapping[role] = found
        if found:
            taken.add(found)
    return mapping


def _value_for(spec_type: str, value: str) -> dict:
    """Wrap a plain string in whatever shape the property type wants."""
    if spec_type == "select":
        return {"select": {"name": value}}
    if spec_type == "multi_select":
        return {"multi_select": [{"name": value}]}
    if spec_type == "status":
        return {"status": {"name": value}}
    if spec_type == "url":
        return {"url": value}
    return {"rich_text": [{"type": "text", "text": {"content": value[:2000]}}]}


# How many pages of 100 rows to read when comparing against what is already
# filed. A semester of one course fits inside this; a database large enough to
# exceed it would cost more to scan than a stray duplicate costs to delete.
MATCH_PAGES = 3


def _course_filter(mapping: dict, schema: dict, course: str) -> dict | None:
    """A filter narrowing the query to one course, if the database has one.

    UNKNOWN is not a course, it is every lecture nothing could be matched to.
    Scoping the query to it would compare a task against every other
    unrelated lecture ever filed there with no due date to narrow it, so two
    different, unrelated classes that both landed in UNKNOWN could get merged
    into one Notion row the moment their wording looked similar enough.
    Falling through to the due-date filter below keeps the comparison to
    tasks actually due the same day.
    """
    name = mapping.get("course")
    if not (name and course and course != config.UNKNOWN_COURSE):
        return None
    kind = schema.get(name, {}).get("type")
    if kind == "select":
        return {"property": name, "select": {"equals": course}}
    if kind == "multi_select":
        return {"property": name, "multi_select": {"contains": course}}
    if kind == "status":
        return {"property": name, "status": {"equals": course}}
    return None


def _existing_titles(data_source_id: str, mapping: dict, schema: dict,
                     course: str, due: str) -> list[str]:
    """Titles already in the database that this task could be a repeat of.

    Scoped to the course when the database records one, because that is the
    narrowest filter Notion can apply that still holds every candidate. It is
    deliberately not scoped by due date: an instructor who pushes a deadline
    back is exactly the case that used to produce a second row.
    """
    payload: dict = {"page_size": 100}
    scope = _course_filter(mapping, schema, course)
    if scope:
        payload["filter"] = scope
    elif mapping.get("due") and due:
        payload["filter"] = {"property": mapping["due"], "date": {"equals": due}}

    titles: list[str] = []
    for _ in range(MATCH_PAGES):
        found = _request("POST", f"/data_sources/{data_source_id}/query", payload)
        for row in found.get("results", []):
            prop = row.get("properties", {}).get(mapping["title"], {})
            titles.append("".join(t.get("plain_text", "")
                                  for t in prop.get("title", [])))
        cursor = found.get("next_cursor")
        if not (found.get("has_more") and cursor):
            break
        payload = dict(payload, start_cursor=cursor)
    return titles


def _already_there(data_source_id: str, mapping: dict, schema: dict, task: str,
                   course: str, due: str) -> bool:
    """Whether this task is already in the database.

    Matched on meaning rather than on the exact string. An instructor who
    brings the same assignment up across three class periods gets summarized
    three times and never in quite the same words, so an equality check on the
    title filed it three times. Nothing is written into the database to make
    this work: the comparison happens here, against what is already filed.
    """
    return any(tasktext.same(task, title)
               for title in _existing_titles(data_source_id, mapping, schema,
                                             course, due))


def add_task(data_source_id: str, mapping: dict, schema: dict, item: dict,
             course: str, source_url: str = "") -> str:
    """Create one to-do page. Returns its Notion URL.

    The title carries the errand alone. Whatever context came with it goes in
    the row's body, a click away, so the list stays readable at a glance.
    """
    properties: dict = {
        mapping["title"]: {
            "title": [{"type": "text",
                       "text": {"content": tasktext.clean(item["task"])}}]
        }
    }
    if mapping["due"] and item.get("due_date"):
        properties[mapping["due"]] = {"date": {"start": item["due_date"]}}
    if mapping["course"]:
        properties[mapping["course"]] = _value_for(
            schema[mapping["course"]]["type"], course
        )
    if mapping["kind"] and item.get("kind"):
        properties[mapping["kind"]] = _value_for(
            schema[mapping["kind"]]["type"], item["kind"]
        )
    if mapping["source"] and source_url:
        properties[mapping["source"]] = _value_for(
            schema[mapping["source"]]["type"], source_url
        )

    payload: dict = {
        "parent": {"type": "data_source_id", "data_source_id": data_source_id},
        "properties": properties,
    }
    detail = tasktext.strip_markdown(item.get("detail", "") or "")
    if detail:
        payload["children"] = [{
            "object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [
                {"type": "text", "text": {"content": detail[:2000]}}
            ]},
        }]

    page = _request("POST", "/pages", payload)
    return page.get("url", "")


def _report(problems: list[tuple[str, str]]) -> dict:
    """The failure half of a push result, from (task, reason) pairs.

    `notes` reads as a sentence per item, for a log a person is scanning.
    `reasons` holds the distinct causes on their own, which is what the run
    summary shows: five items dropped for one missing week heading is one
    thing to fix, not five.
    """
    notes, reasons = [], []
    for task, reason in problems:
        notes.append(f"{task[:50]}: {reason}")
        if reason not in reasons:
            reasons.append(reason)
    return {"failed": len(problems), "notes": notes, "reasons": reasons}


def outcome_warning(outcome: dict | None, total: int) -> str:
    """One line naming what never reached Notion, or "" when nothing did.

    The watcher records this alongside the lecture so the control panel can
    show it. Without it, a lecture whose action items were dropped looked
    exactly like one that filed all of them, and the only trace was a line on
    the watcher's stderr that nobody reads.

    Collapsed to single spaces because it goes into a tab-separated log.
    """
    if not outcome or not outcome.get("failed"):
        return ""
    reasons = outcome.get("reasons") or []
    line = f"{outcome['failed']} of {total} to-dos did not reach Notion"
    if reasons:
        line += ": " + "; ".join(reasons[:2])
        if len(reasons) > 2:
            line += f"; and {len(reasons) - 2} more"
    return " ".join(line.split())


def push(items: list[dict], course: str, source_url: str = "",
         dry_run: bool = False) -> dict:
    """Add a lecture's action items to Notion.

    Two shapes of destination exist and they are not interchangeable. The
    checklist people actually tick lives in page blocks: a weekly page with a
    column per day. Database rows are a separate surface with real Due and
    Course fields, invisible from the weekly page and vice versa. NOTION_TARGET
    picks which one, defaulting to the weekly page because that is the one you
    look at.
    """
    if not items:
        return dict(_report([]), added=0, skipped=0, urls=[])

    if config.NOTION_TARGET == "weekly":
        return push_to_weekly(items, course, source_url, dry_run)

    data_source_id, db_title = resolve_data_source()
    schema = get_schema(data_source_id)
    mapping = map_properties(schema)

    if dry_run:
        log(f"  would add to {db_title!r}, mapped as: "
            + ", ".join(f"{role}={name!r}" for role, name in mapping.items()))

    added, skipped, urls = 0, 0, []
    problems: list[tuple[str, str]] = []
    for item in items:
        label = f"{item['task'][:60]}"
        try:
            if _already_there(data_source_id, mapping, schema, item["task"],
                              course, item.get("due_date", "")):
                skipped += 1
                log(f"  already in Notion: {label}")
                continue
            if dry_run:
                added += 1
                log(f"  would add: {label} (due {item.get('due_date') or 'none'})")
                continue
            urls.append(add_task(data_source_id, mapping, schema, item,
                                 course, source_url))
            added += 1
            log(f"  added to Notion: {label}")
        except NotionError as exc:
            # One bad item must not cost the rest of the lecture's tasks.
            problems.append((item["task"], str(exc)))
            log(f"  could not add {label}: {exc}")

    return dict(_report(problems), added=added, skipped=skipped, urls=urls)


# --- Weekly page ----------------------------------------------------------
#
# The to-do database holds pages of day columns with checkboxes. That is where
# the checklist actually lives; the database rows are a different surface
# entirely, which is why a task can be filed correctly as a row and still be
# nowhere you would ever see it.
#
# A week is a HEADING, not a page. One page commonly stacks several weeks --
# "Sep 14 - 20" and its columns, then "Sep 9 - Sep 13" and its columns -- and
# people add the new week above the old one rather than starting a new page.
# So a heading owns the column_list blocks that follow it, up to the next
# heading, and matching a due date means matching a section inside a page.

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}

# "Sep 9 - Sep 13", "Sep 9 – 13", "September 28 - October 4". Any dash.
WEEK_RANGE_RE = re.compile(
    r"([A-Za-z]{3,9})\.?\s+(\d{1,2})\s*[-–—]+\s*(?:([A-Za-z]{3,9})\.?\s+)?(\d{1,2})"
)


def _plain(block: dict) -> str:
    """The text of a block, whatever kind of block it is."""
    body = block.get(block.get("type", ""), {}) or {}
    return "".join(part.get("plain_text", "") for part in body.get("rich_text", []))


def _children(block_id: str) -> list[dict]:
    return _request("GET", f"/blocks/{block_id}/children?page_size=100").get("results", [])


def parse_week_range(text: str, near: date) -> tuple[date, date] | None:
    """Turn a week heading into a date range, or None if it isn't one.

    Notion headings carry no year, so the year is taken from the date we are
    filing and checked against its neighbours. That is what keeps the last
    week of December working.
    """
    match = WEEK_RANGE_RE.search(text or "")
    if not match:
        return None
    start_month_name, start_day, end_month_name, end_day = match.groups()
    start_month = MONTHS.get(start_month_name[:3].lower())
    end_month = MONTHS.get((end_month_name or start_month_name)[:3].lower())
    if not start_month or not end_month:
        return None

    for year in (near.year, near.year - 1, near.year + 1):
        try:
            start = date(year, start_month, int(start_day))
            end = date(year, end_month, int(end_day))
        except ValueError:
            continue
        # A range that ends before it starts has rolled into the new year.
        if end < start:
            try:
                end = date(year + 1, end_month, int(end_day))
            except ValueError:
                continue
        if start <= near <= end:
            return start, end
    return None


def week_sections(page_id: str) -> list[dict]:
    """The week sections on one page, in the order they appear.

    Each heading starts a section that owns every column_list after it until
    the next heading. Blocks ahead of the first heading (the page's intro
    paragraph) belong to no week and are ignored.
    """
    sections: list[dict] = []
    for block in _children(page_id):
        if block["type"].startswith("heading"):
            sections.append({"heading": _plain(block), "column_lists": []})
        elif block["type"] == "column_list" and sections:
            sections[-1]["column_lists"].append(block["id"])
    return sections


def weekly_pages() -> list[dict]:
    """Every page in the database, with each week section inside it.

    A few requests per page, so this stays cheap only because a to-do database
    holds a handful of weeks, not thousands of rows.
    """
    ds, _ = resolve_data_source()
    rows = _request("POST", f"/data_sources/{ds}/query", {"page_size": 100})
    pages = []
    for row in rows.get("results", []):
        title_prop = next((p for p in row["properties"].values()
                           if p.get("type") == "title"), {})
        title = "".join(t.get("plain_text", "") for t in title_prop.get("title", []))
        sections = week_sections(row["id"])
        pages.append({
            "id": row["id"],
            "title": title,
            "sections": sections,
            # The first heading, kept for messages that name the page.
            "heading": sections[0]["heading"] if sections else "",
        })
    return pages


def find_week_page(due: date) -> dict | None:
    """The week section covering `due`, or None.

    Returns the page it lives on plus that section's own column_lists, so the
    day column is looked up inside the right week rather than in whichever
    week happens to sit highest on the page.

    Returning None rather than guessing is deliberate: writing a task into the
    wrong week is worse than not writing it, because you would not notice.
    """
    for page in weekly_pages():
        for section in page["sections"]:
            span = parse_week_range(section["heading"], due)
            if span:
                return dict(page, span=span, heading=section["heading"],
                            column_lists=section["column_lists"])
    return None


def find_day_column(page: dict | str, due: date) -> str | None:
    """The id of the column whose heading names `due`'s weekday.

    Takes the section dict from `find_week_page` so the search is confined to
    that week's columns. A bare page id is still accepted, and then every
    column on the page is in scope -- which is only unambiguous when the page
    holds a single week.
    """
    if isinstance(page, str):
        column_lists = [b["id"] for b in _children(page)
                        if b["type"] == "column_list"]
    else:
        column_lists = page["column_lists"]

    wanted = WEEKDAYS[due.weekday()]
    for column_list in column_lists:
        for column in _children(column_list):
            for child in _children(column["id"]):
                if not child["type"].startswith("heading"):
                    continue
                # Headings carry stray spaces and emoji, and say "Tues" and
                # "Thur" rather than "Tue" and "Thu".
                letters = "".join(c for c in _plain(child) if c.isalpha()).lower()
                if letters.startswith(wanted):
                    return column["id"]
    return None


def _column_todos(column_id: str) -> list[dict]:
    return [b for b in _children(column_id) if b["type"] == "to_do"]


# The link back to the notes, as one character rather than a word. A day
# column is narrow, and "  notes" pushed most checkboxes onto a second line
# for no information the arrow does not already carry.
NOTES_GLYPH = " ↗"


def _todo_text(task: str, course: str, source_url: str = "") -> list[dict]:
    """Rich text for one checkbox: the course, the task, and a link to notes.

    Three separate spans, not one string. The course is set in gray so the eye
    skips it and lands on the task, and the task itself is cleaned on the way
    in: Notion's rich_text does no markdown parsing, so an asterisk the model
    wrote is an asterisk the checkbox shows.
    """
    parts: list[dict] = []
    if course:
        parts.append({
            "type": "text", "text": {"content": f"{course} "},
            "annotations": {"bold": True, "color": "gray"},
        })
    parts.append({"type": "text",
                  "text": {"content": tasktext.clean(task) or task[:200]}})
    if source_url:
        parts.append({"type": "text",
                      "text": {"content": NOTES_GLYPH,
                               "link": {"url": source_url}}})
    return parts


def add_to_day(column_id: str, task: str, course: str, source_url: str = "") -> str:
    """Put a checkbox in a day column. Fills a blank one before adding more.

    The weekly template ships three empty checkboxes per day. Filling those
    first keeps the column looking like the one you set up, instead of
    stacking new items underneath a row of blanks.
    """
    for existing in _column_todos(column_id):
        if not _plain(existing).strip():
            _request("PATCH", f"/blocks/{existing['id']}",
                     {"to_do": {"rich_text": _todo_text(task, course, source_url)}})
            return existing["id"]

    created = _request("PATCH", f"/blocks/{column_id}/children", {
        "children": [{"object": "block", "type": "to_do",
                      "to_do": {"rich_text": _todo_text(task, course, source_url),
                                "checked": False}}]
    })
    return created.get("results", [{}])[0].get("id", "")


def _already_on_page(page_id: str, task: str, course: str) -> bool:
    """Whether this task is already a checkbox somewhere on the weekly page.

    The whole page, deliberately, not just the week being filed: a task you
    ticked off or dragged into a neighbouring day must not come back on the
    next run of the same lecture.
    """
    for block in _children(page_id):
        if block["type"] != "column_list":
            continue
        for column in _children(block["id"]):
            for todo in _column_todos(column["id"]):
                existing = _plain(todo).strip()
                if not existing:
                    continue
                # Drop what this module put around the task before comparing:
                # the course prefix (whatever course it came from) and the
                # link glyph on the end.
                existing = tasktext.strip_course_prefix(
                    existing.removeprefix(f"{course} ").strip()
                ).removesuffix(NOTES_GLYPH.strip()).strip()
                if tasktext.same(task, existing):
                    return True
    return False


def push_to_weekly(items: list[dict], course: str, source_url: str = "",
                   dry_run: bool = False) -> dict:
    """Add each action item as a checkbox under its due day."""
    added, skipped = 0, 0
    # (task, reason) rather than a formatted string, so the caller can report
    # the reason on its own instead of parsing it back out of a sentence.
    problems: list[tuple[str, str]] = []

    for item in items:
        task = item["task"]
        due_text = item.get("due_date") or ""
        try:
            due = datetime.strptime(due_text, "%Y-%m-%d").date()
        except ValueError:
            problems.append((task, "no due date, so no day to file it under"))
            continue

        page = find_week_page(due)
        if not page:
            problems.append((task, f"no week heading covers {due_text}. Add a "
                                   f"heading naming that week (e.g. "
                                   f"\"Sep 21 - 27\") above its day columns."))
            continue

        column = find_day_column(page, due)
        if not column:
            problems.append((task, f"{page['heading']!r} in {page['title']!r} "
                                   f"has no {due.strftime('%A')} column"))
            continue

        try:
            if _already_on_page(page["id"], task, course):
                skipped += 1
                log(f"  already on the weekly page: {task[:60]}")
                continue
            if dry_run:
                added += 1
                log(f"  would add {task[:50]} under {due.strftime('%a')} "
                    f"of {page['heading']!r}")
                continue
            add_to_day(column, task, course, source_url)
            added += 1
            log(f"  added under {due.strftime('%a')} ({due_text}) of "
                f"{page['heading']!r}: {task[:60]}")
        except NotionError as exc:
            problems.append((task, str(exc)))

    report = _report(problems)
    for note in report["notes"]:
        log(f"  could not file {note}")
    return dict(report, added=added, skipped=skipped)


def _course_options() -> list[dict]:
    """Seed the Course select with the courses already in the schedule."""
    colors = ("blue", "green", "orange", "purple", "pink", "brown", "red")
    codes = config.courses()
    return [{"name": code, "color": colors[i % len(colors)]}
            for i, code in enumerate(codes)]


def setup_properties(dry_run: bool = False) -> dict:
    """Add the properties this script needs, for roles the database lacks.

    Only ever adds. An existing property is never included in the request, so
    nothing already in the database can be renamed, retyped, or removed by
    this (Notion deletes a property when it is sent as null, which is why the
    request is built from scratch rather than from the current schema).

    A role already covered by a differently named property is left alone: a
    database with "Deadline" does not also get a "Due".
    """
    if config.NOTION_TARGET == "weekly":
        raise NotionError(
            "NOTION_TARGET is 'weekly', so tasks are written as checkboxes in "
            "the weekly page and Due/Course/Source rows are never used. Adding "
            "them would just be clutter. Set NOTION_TARGET=database first if "
            "you actually want rows."
        )

    data_source_id, title = resolve_data_source()
    schema = get_schema(data_source_id)
    mapping = map_properties(schema)

    wanted = {
        "due": ("Due", {"type": "date", "date": {}}),
        "course": ("Course", {"type": "select",
                              "select": {"options": _course_options()}}),
        "source": ("Source", {"type": "url", "url": {}}),
    }

    additions, skipped = {}, []
    for role, (name, spec) in wanted.items():
        if mapping.get(role):
            skipped.append(f"{role}: already using {mapping[role]!r}")
            continue
        if name in schema:
            skipped.append(
                f"{role}: a property called {name!r} exists but is a "
                f"{schema[name].get('type')}, so it was left alone"
            )
            continue
        additions[name] = spec

    for note in skipped:
        log(f"  skipped {note}")

    if not additions:
        log("  nothing to add; the database already has what it needs")
        return {"added": [], "database": title}

    if dry_run:
        log(f"  would add to {title!r}: {', '.join(sorted(additions))}")
        return {"added": sorted(additions), "database": title, "dry_run": True}

    _request("PATCH", f"/data_sources/{data_source_id}",
             {"properties": additions})
    log(f"  added to {title!r}: {', '.join(sorted(additions))}")
    return {"added": sorted(additions), "database": title}


def describe_weekly() -> str:
    """What the weekly pages look like, and where today's tasks would land."""
    from datetime import timedelta
    lines = []
    pages = weekly_pages()
    if not pages:
        return "no pages found in the database"

    total = sum(len(page["sections"]) for page in pages)
    lines.append(f"{len(pages)} page(s), {total} week heading(s):")
    for page in pages:
        lines.append(f"  {page['title']!r}")
        if not page["sections"]:
            lines.append("    (no headings, so no week can be matched)")
        for section in page["sections"]:
            # Headings carry no year, so probe outward from today rather than
            # from a year ago: the nearest reading is the one a person means.
            span = None
            for probe in sorted(range(-370, 371), key=abs):
                span = parse_week_range(section["heading"],
                                        date.today() + timedelta(days=probe))
                if span:
                    break
            window = (f"{span[0]} to {span[1]}" if span
                      else "no date range in this heading")
            columns = len(section["column_lists"])
            lines.append(f"    {section['heading']!r}  ({window}, "
                         f"{columns} column block(s))")

    lines += ["", "where the next seven days would go:"]
    for offset in range(7):
        due = date.today() + timedelta(days=offset)
        page = find_week_page(due)
        if not page:
            lines.append(f"  {due} {due.strftime('%a')}  no week heading covers this")
            continue
        column = find_day_column(page, due)
        where = "column found" if column else "NO matching day column"
        lines.append(f"  {due} {due.strftime('%a')}  {page['heading']!r} in "
                     f"{page['title']!r}: {where}")
    return "\n".join(lines)


def describe() -> str:
    """Human-readable report of what this script sees in your database."""
    if config.NOTION_TARGET == "weekly":
        data_source_id, db_title = resolve_data_source()
        return (f"database:    {db_title}\ntarget:      weekly page columns "
                f"(NOTION_TARGET=weekly)\n\n" + describe_weekly())

    data_source_id, db_title = resolve_data_source()
    schema = get_schema(data_source_id)
    mapping = map_properties(schema)

    lines = [f"database:    {db_title}", f"data source: {data_source_id}", "",
             "properties found:"]
    for name, spec in sorted(schema.items()):
        roles = [r for r, n in mapping.items() if n == name]
        marker = f"   <- {', '.join(roles)}" if roles else ""
        lines.append(f"  {name} ({spec.get('type')}){marker}")

    lines += ["", "mapping:"]
    for role in ("title", "due", "course", "kind", "source"):
        target = mapping.get(role)
        lines.append(f"  {role:7} {target if target else '(none: will be skipped)'}")
    if not mapping["due"]:
        lines.append("")
        lines.append("  No date property matched, so due dates cannot be set. "
                     "Set NOTION_PROP_DUE in .env to the exact name.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="intake notion",
        description="Send lecture action items to a Notion to-do database."
    )
    parser.add_argument("--check", action="store_true",
                        help="show the database and property mapping, then exit")
    parser.add_argument("--setup", action="store_true",
                        help="add any missing Due / Course / Source properties")
    parser.add_argument("--transcript", help="a transcript to summarize and push")
    parser.add_argument("--course", help="course code, e.g. ACCT-4321")
    parser.add_argument("--date", help="lecture date, YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would be added without adding it")
    args = parser.parse_args(argv)

    try:
        if args.setup:
            setup_properties(dry_run=args.dry_run)
            if not args.dry_run:
                print()
                print(describe())
            return 0

        if args.check:
            print(describe())
            return 0

        if not (args.transcript and args.course and args.date):
            parser.error("--transcript, --course and --date are required "
                         "unless --check or --setup is given")

        from intake import summarize
        text = Path(args.transcript).expanduser().read_text()
        result = summarize.summarize(text, args.course, args.date)
        outcome = push(result["action_items"], args.course,
                       dry_run=args.dry_run)
        log(f"  {outcome['added']} added, {outcome['skipped']} already there, "
            f"{outcome['failed']} failed")
    except (NotionError, config.ScheduleError) as exc:
        log(f"error: {exc}")
        return 1
    except requests.RequestException as exc:
        log(f"error: could not reach Notion: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
