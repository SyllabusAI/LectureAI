"""Permission to record: asked once per profile, before the first recording.

Recording a lecture or a call without the permission of the people in it can
break a school's policy or the law where it happens. Before this Mac records
anything, the person using it confirms, once, that they have that permission.
The answer is kept in the profile's home as consent.json, stamped with when it
was given, from where, and by which version, so it is asked once per profile
and not before every recording.

The gate itself is in record.Recorder.start, the one place every recording
begins (the CLI, the panel's record button, the menu bar item and the panel on
the web all end up there), so no front end can forget to ask. The front ends
check it early as well, only to say so more helpfully.

VERSION is the version of the statement. Changing what the person agrees to
means raising it, which asks everyone again; an older acknowledgment does not
cover words it never showed.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from intake import __version__, config

VERSION = 1

# What the person affirms, by profile. Shown next to the checkbox on the Setup
# page and the dashboard, and in `intake setup`, from this one place.
STATEMENTS = {
    "syllabus": ("I have permission to record my lectures from the instructor and "
                 "anyone else being recorded, as my school's policy and the law "
                 "where I record require."),
    "sous": ("I have permission to record my calls from everyone on them, as the "
             "law where each person is requires."),
}

LABEL = "I have permission to record"


class ConsentRequired(RuntimeError):
    """A recording was asked for before permission to record was confirmed."""


def statement() -> str:
    return STATEMENTS.get(config.PROFILE.name, STATEMENTS["syllabus"])


def path(home: Path | None = None) -> Path:
    return config.CONSENT_FILE if home is None else Path(home) / config.CONSENT_FILE.name


def load(home: Path | None = None) -> dict | None:
    """The acknowledgment on file, or None when there is none that counts.

    A file that is unreadable, not an object, not an agreement, or written for
    an older statement counts as no acknowledgment: the safe reading of a
    broken file is to ask again.
    """
    try:
        data = json.loads(path(home).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("agreed") is not True:
        return None
    version = data.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < VERSION:
        return None
    return data


def given(home: Path | None = None) -> bool:
    return load(home) is not None


def record(source: str, home: Path | None = None, by: str = "") -> dict:
    """Write the acknowledgment and return what was written.

    `source` is where it was given: "setup" for `intake setup`, "panel" for
    the Setup page or the dashboard on this Mac, "web" for the same pages
    reached through the account service's relay. `by` is who the relay
    named as the viewer, when it was the relay; this Mac has no way to know
    who is at its own keyboard, so it does not pretend to.
    """
    data = {
        "agreed": True,
        "version": VERSION,
        "statement": statement(),
        "agreed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "app_version": __version__,
        "source": source,
    }
    if by:
        data["by"] = by
    config.write_private(path(home), json.dumps(data, indent=2) + "\n")
    return data


def fix_command() -> str:
    """The exact command that asks the question, for this profile."""
    prefix = "intake" if config.PROFILE.name == "syllabus" \
        else f"intake --profile {config.PROFILE.name}"
    return f"{prefix} setup --consent"


def refusal() -> str:
    """Why a recording did not start, and how to fix it, for a terminal."""
    return ("Before this Mac records, confirm that you have permission to record. "
            f"Run:  {fix_command()}   or tick \"{LABEL}\" on the Setup page.")


def panel_refusal() -> str:
    """The same, for the panel, which has its own checkbox for it."""
    return ("Before this Mac records, confirm that you have permission to record: "
            f"tick \"{LABEL}\" and press Confirm.")


def require() -> None:
    """Raise ConsentRequired unless permission to record is on file."""
    if not given():
        raise ConsentRequired(refusal())


def summary() -> dict:
    """What the pages need: whether it is given, when, and the words."""
    data = load() or {}
    return {"given": bool(data), "agreed_at": data.get("agreed_at", ""),
            "statement": statement(), "label": LABEL}
