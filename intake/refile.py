"""Move a filed lecture to the course it really belongs to.

A lecture filed under the wrong course used to stay there: the audio is
usually deleted once it is uploaded, so it cannot be run again, and putting
it right meant renaming and moving two files in Drive by hand and leaving the
dashboard wrong for good, because pipeline.log still said the old course.

    intake refile ENTR-3306_2026-10-01_Producer-Surplus-And-Market --course ECON-2301

does all of it in one step, and the dashboard's Move action calls the same
function. The summary and the transcript are renamed and moved in place, so
every link to them keeps working; the course in the summary's heading is
corrected; and the lecture's line in pipeline.log is rewritten, so the week,
the charts and the study assistant follow.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from intake import config, insights
from intake import upload as drive

# The id in a Drive or Docs link: .../d/<id>/... or ...?id=<id>.
_LINK_ID = re.compile(r"(?:/d/|[?&]id=)([A-Za-z0-9_-]{10,})")


class RefileError(ValueError):
    """The lecture cannot be moved, and why, in words for the person asking."""


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _lines() -> list[str]:
    if not config.LOG_FILE.exists():
        return []
    return config.LOG_FILE.read_text(errors="replace").splitlines()


def _is_lecture(fields: list[str]) -> bool:
    return len(fields) in (5, 6, 7) and fields[1] != "ERROR"


def find(name: str) -> tuple[int, list[str]]:
    """The newest pipeline.log line for the lecture called `name`.

    The newest, because a lecture that was run twice has two lines and the
    later one is the file Drive holds now.
    """
    for index, line in reversed(list(enumerate(_lines()))):
        fields = line.split("\t")
        if _is_lecture(fields) and fields[3] == name:
            return index, fields
    raise RefileError(f"no filed lecture is called {name}")


def renamed(name: str, old: str, new: str) -> str:
    """`name` with its course prefix swapped, or unchanged if it has none."""
    if name.startswith(f"{old}_"):
        return f"{new}{name[len(old):]}"
    return name


def _child_folder(service, name: str, parent_id: str) -> str | None:
    """A folder that already exists, without creating one to look in."""
    query = (
        f"name = '{drive._escape(name)}' and mimeType = '{drive.FOLDER_MIME}' "
        f"and '{drive._escape(parent_id)}' in parents and trashed = false"
    )
    found = service.files().list(
        q=query, fields="files(id, name)", pageSize=1,
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute().get("files", [])
    return found[0]["id"] if found else None


def _move(service, file_id: str, parents: list[str], folder_id: str, name: str) -> dict:
    """Rename and move one file. The id, and so every link to it, is kept."""
    return service.files().update(
        fileId=file_id, body={"name": name},
        addParents=folder_id, removeParents=",".join(p for p in parents if p != folder_id),
        fields="id, name, webViewLink", supportsAllDrives=True,
    ).execute()


def _free(service, name: str, folder_id: str, suffix: str) -> str:
    """`name` if nothing in the destination holds it, else one that is free.

    Two lectures of one class on one day share a name, and the course being
    moved into may already have the other one.
    """
    if drive._find_file(service, name, folder_id) is None:
        return name
    return drive._free_name(service, name, folder_id, suffix)


def _fix_heading(service, doc_id: str, old: str, new: str, day: str) -> bool:
    """Swap the course in the summary's "COURSE: DATE" heading.

    Through an HTML round trip, which is how Drive keeps a Doc's formatting
    when its content is replaced. Only the heading is touched: the first
    exact "OLD: DATE", the line render_markdown writes at the top. A lecture
    that never says the old course is left as it is.
    """
    from googleapiclient.http import MediaInMemoryUpload

    html = service.files().export(fileId=doc_id, mimeType="text/html").execute()
    if isinstance(html, bytes):
        html = html.decode("utf-8")
    before, after = f"{old}: {day}", f"{new}: {day}"
    if before not in html:
        return False
    media = MediaInMemoryUpload(html.replace(before, after, 1).encode("utf-8"),
                                mimetype="text/html", resumable=False)
    service.files().update(fileId=doc_id, media_body=media, fields="id",
                           supportsAllDrives=True).execute()
    return True


def _rewrite_log(index: int, fields: list[str]) -> None:
    """Replace one line of pipeline.log, keeping every other line as it was.

    Written beside the log and swapped in, so a crash halfway leaves the old
    log rather than half of one. A line the watcher appends while this runs
    is kept, because the file is read again right before the swap.
    """
    lines = _lines()
    lines[index] = "\t".join(fields)
    staging = config.LOG_FILE.with_suffix(".refile")
    staging.write_text("\n".join(lines) + "\n")
    os.replace(staging, config.LOG_FILE)


def _calendar_items(key: str, old: str, new: str) -> int:
    """Point the calendar ledger's entries for this lecture at the new course.

    The ledger is how a later lecture recognizes a to-do already filed, so it
    should name the course the lecture now belongs to. The items themselves
    are already in the calendar with the old course in their titles; those
    are counted so the caller can say so.
    """
    from intake import calendars

    if not key or not config.CALENDAR_LEDGER.exists():
        return 0
    ledger = calendars.Ledger.load()
    moved = 0
    for entry in ledger.items:
        if entry.get("lecture") == key and entry.get("course") == old:
            entry["course"] = new
            moved += 1
    if moved:
        ledger.save()
    return moved


def refile(name: str, course: str, *, interactive: bool = False) -> dict:
    """Move the lecture called `name` to `course`. Returns what changed.

    Raises RefileError for anything the person asking can fix: a course that
    is not on the schedule, a lecture that is not in the log, a summary that
    is no longer in Drive.
    """
    known = {code.upper(): code for code in config.courses()}
    if course.upper() not in known:
        raise RefileError(f"{course} is not on your schedule")
    course = known[course.upper()]

    index, fields = find(name)
    old = fields[1]
    if old == course:
        raise RefileError(f"{name} is already filed under {course}")
    day = insights.lecture_date(name, fields[0])
    match = _LINK_ID.search(fields[4])
    if not match:
        raise RefileError(f"the log has no Drive link for {name}")
    doc_id = match.group(1)

    service = drive.get_service(interactive)
    root = drive.ensure_root_folder(service)
    try:
        doc = service.files().get(
            fileId=doc_id, fields="id, name, parents, mimeType, appProperties, trashed",
            supportsAllDrives=True,
        ).execute()
    except drive.HttpError as exc:
        if exc.status_code in (403, 404):
            raise RefileError(f"the summary for {name} is no longer in Drive") from None
        raise
    if doc.get("trashed"):
        raise RefileError(f"the summary for {name} is in the Drive trash")

    key = (doc.get("appProperties") or {}).get(drive.RECORDING_KEY_PROPERTY, "")
    suffix = key[11:16].replace(":", "") if len(key) >= 16 else ""
    target = drive.ensure_folder(service, course, root)
    target_transcripts = drive.ensure_folder(service, config.TRANSCRIPT_SUBFOLDER, target)

    is_doc = doc.get("mimeType") == drive.GOOGLE_DOC_MIME
    new_doc_name = _free(service, renamed(doc["name"], old, course), target, suffix)
    moved = _move(service, doc_id, doc.get("parents", []), target, new_doc_name)
    log(f"  moved {doc['name']} to {course}/ as {new_doc_name}")
    new_stem = new_doc_name.removesuffix(".md")

    warnings = []
    transcript = None
    old_folder = _child_folder(service, old, root)
    old_transcripts = old_folder and _child_folder(service, config.TRANSCRIPT_SUBFOLDER, old_folder)
    if old_transcripts:
        transcript = (drive._find_by_recording(service, key, old_transcripts) if key else None) \
            or drive._find_file(service, f"{name}.txt", old_transcripts)
    if transcript:
        info = service.files().get(fileId=transcript["id"], fields="id, name, parents",
                                   supportsAllDrives=True).execute()
        _move(service, info["id"], info.get("parents", []), target_transcripts,
              _free(service, f"{new_stem}.txt", target_transcripts, suffix))
        log(f"  moved its transcript to {course}/{config.TRANSCRIPT_SUBFOLDER}/")
    else:
        warnings.append(f"its transcript was not found in {old}/{config.TRANSCRIPT_SUBFOLDER}/, "
                        "so only the summary moved")

    if is_doc:
        try:
            if _fix_heading(service, doc_id, old, course, day):
                log(f"  heading now reads {course}: {day}")
        except Exception as exc:  # the files have moved; the heading is cosmetic
            warnings.append(f"the summary's heading still says {old} ({exc})")

    fields[1] = course
    fields[3] = new_stem
    _rewrite_log(index, fields)

    filed = _calendar_items(key, old, course)
    actions = insights.parse_measures(fields[6]).get("actions", 0) if len(fields) == 7 else 0
    if filed or actions:
        warnings.append(f"to-dos already sent to Notion or a calendar still say {old}")

    return {"from": old, "course": course, "name": new_stem,
            "url": moved.get("webViewLink", fields[4]), "warnings": warnings}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="intake refile",
        description="Move a filed lecture to another course: Drive, its heading, and the log.")
    parser.add_argument("name", help="the lecture's name, as the dashboard and Drive show it")
    parser.add_argument("--course", required=True, help="the course it belongs to")
    args = parser.parse_args(argv)
    try:
        out = refile(args.name, args.course, interactive=sys.stdin.isatty())
    except RefileError as exc:
        log(f"error: {exc}")
        return 1
    log(f"done: {out['name']} is filed under {out['course']}; its links are unchanged")
    for warning in out["warnings"]:
        log(f"  note: {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
