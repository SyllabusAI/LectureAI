"""Settings, schedule, and paths for the pipeline, for the active profile.

Everything the pipeline reads or writes lives under one root, separate from
the code: $INTAKE_HOME if set, otherwise ~/.intake ($LECTUREAI_HOME, the
variable's name before the rename, is still honored when the new one is
unset). Inside it each profile has a home of its own, ~/.intake/syllabus/ or
~/.intake/sous/, holding that profile's .env, schedule, inbox, processed
folder, Drive token, and log. The code directory holds only code, so the same
install serves any Mac and the repo never fills up with recordings, tokens,
and logs.

Which profile this process is comes from $INTAKE_PROFILE, which the CLI sets
from --profile before importing this module; see profiles.py.
"""

from __future__ import annotations

import fcntl
import os
import re
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import dotenv_values

from intake import profiles
from intake.profiles import Profile

# --- Where things live -----------------------------------------------------

PACKAGE_DIR = Path(__file__).resolve().parent

# The checkout this package was loaded from, when it is one. Under pipx this
# is a site-packages directory and nothing below looks for anything in it.
CODE_ROOT = PACKAGE_DIR.parent

# Whether this is Syllabus.app, the package frozen with PyInstaller (see
# packaging/). There sys.executable is the app's one binary, which runs the
# intake command when given arguments and the window when given none, and
# has no -m flag to offer anyone.
FROZEN = bool(getattr(sys, "frozen", False))


def program(*args: str, profile: Profile | None = None) -> list[str]:
    """The command that runs `intake <args>` the way this process was run.

    From a checkout or a pipx install that is this interpreter with
    `-m intake.cli`, so a venv keeps its venv and pipx its own. Frozen, it
    is the app binary itself. Used wherever the panel or the service starts
    another copy of this program: the watcher, the launchd agent. The profile
    is always spelled out, so the child cannot land in another home.
    """
    profile = profile or PROFILE
    head = [sys.executable] if FROZEN else [sys.executable, "-m", "intake.cli"]
    return [*head, "--profile", profile.name, *args]


def program_cwd() -> Path:
    """Where to run program() from: the directory holding the package, so
    `-m intake.cli` resolves from a checkout as well as from site-packages.
    The frozen binary needs no such help and runs from the home directory."""
    return HOME_DIR if FROZEN else CODE_ROOT

HOME_ENV_VAR = "INTAKE_HOME"
# The variable's name before the rename to intake. Still read when the new one
# is unset, so an existing install keeps finding its data without anyone
# editing a shell profile. Set INTAKE_HOME instead; this one goes away later.
LEGACY_HOME_ENV_VAR = "LECTUREAI_HOME"
DEFAULT_HOME = Path("~/.intake")
# Where the data lived before the rename. A default install with nothing in
# ~/.intake yet is offered a move from here (see legacy_files below).
OLD_DEFAULT_HOME = Path("~/.lectureai")


def home_source(env: dict | None = None) -> str:
    """Which setting decides the home: the env var's name, or "default"."""
    source = os.environ if env is None else env
    for name in (HOME_ENV_VAR, LEGACY_HOME_ENV_VAR):
        if (source.get(name) or "").strip():
            return name
    return "default"


def resolve_home(env: dict | None = None) -> Path:
    """The data root: $INTAKE_HOME if set, else $LECTUREAI_HOME, else ~/.intake.

    Each profile's home is a folder inside this root (see profile_home).
    Takes the environment as an argument so tests can resolve against a fake
    one without touching the process environment.
    """
    source = os.environ if env is None else env
    name = home_source(source)
    chosen = DEFAULT_HOME if name == "default" else Path(source[name].strip())
    return chosen.expanduser().resolve()


ROOT_DIR = resolve_home()
HOME_SOURCE = home_source()

# --- The active profile ----------------------------------------------------
#
# Each profile keeps its own home under the root, ~/.intake/syllabus/ and
# ~/.intake/sous/, each with its own .env, inbox, processed folder, token,
# and log, so the two can never share a recording or a credential. Which one
# this process is comes from $INTAKE_PROFILE; anything else gets the default.

PROFILE_ENV_VAR = profiles.PROFILE_ENV_VAR
PROFILE: Profile = profiles.select(env=os.environ)
# Left in the environment so anything this process starts (the watcher the
# panel spawns) lands in the same profile.
os.environ[PROFILE_ENV_VAR] = PROFILE.name


def profile_home(profile: Profile, root: Path | None = None) -> Path:
    """Where `profile` keeps its data: the root plus the profile's own folder."""
    return (ROOT_DIR if root is None else Path(root)) / profile.home_subdir


def paths_for(profile: Profile, root: Path | None = None) -> dict[str, Path]:
    """Every data path of `profile`, keyed by the name this module binds it to.

    Pure, so two profiles' layouts can be compared without activating either.
    """
    home = profile_home(profile, root)
    work = home / ".work"          # scratch space for compressed/split audio
    return {
        "HOME_DIR": home,
        "ENV_FILE": home / ".env",
        "INBOX_DIR": home / "inbox",
        "PROCESSED_DIR": home / "processed",
        "WORK_DIR": work,
        "LOG_FILE": home / "pipeline.log",
        # What the watcher is doing right now, for the control panel to read.
        # Written during a run and removed at the end; pipeline.log only gets a
        # line once a recording is finished, which leaves the whole
        # transcription invisible.
        "STATUS_FILE": work / "status.json",
        "LOCK_FILE": home / ".watcher.lock",   # guards against two watchers at once
        # The recording in progress, if any: ffmpeg's pid, where it is writing,
        # when it began. ffmpeg is started in its own session so it survives
        # whoever started it; this file is how the next panel or CLI finds it
        # again and stops it properly instead of leaving a recording with
        # nobody at the controls.
        "RECORDING_STATE_FILE": work / "recording.json",
        # A Google OAuth client placed here overrides the one bundled with the
        # package (see google_client.py). Almost nobody needs to.
        "CREDENTIALS_FILE": home / "credentials.json",
        "TOKEN_FILE": home / "token.json",
        # This panel's place in a Syllabus account: the device token and who
        # it belongs to (see account.py). Absent for a panel with no account.
        "ACCOUNT_FILE": home / "account.json",
        "SCHEDULE_FILE": home / profile.schedule_filename,
        # Classes that did not meet: the schedule says a class was expected,
        # this file says it was called off. Kept beside the schedule because
        # it is read the same way, as a correction to it (see
        # cancellations.py).
        "CANCELED_FILE": home / "canceled.json",
        # That the person confirmed they have permission to record, and when
        # (see consent.py). No recording starts without it.
        "CONSENT_FILE": home / "consent.json",
        # The id of the app's Drive root folder, cached so renaming or moving
        # the folder in Drive doesn't matter.
        "DRIVE_ROOT_CACHE": home / ".drive_root",
    }


def _install_paths(profile: Profile) -> None:
    """Bind the profile's paths to this module's names (HOME_DIR, INBOX_DIR, ...)."""
    globals().update(paths_for(profile))
    # Kept for anything that still spells the old name. New code should say
    # HOME_DIR, which is what this has always meant: where the data goes.
    globals()["BASE_DIR"] = globals()["HOME_DIR"]


# The names _install_paths defines, listed so a reader can find them here.
# Their values are the active profile's; see paths_for above.
HOME_DIR: Path
BASE_DIR: Path
ENV_FILE: Path
INBOX_DIR: Path
PROCESSED_DIR: Path
WORK_DIR: Path
LOG_FILE: Path
STATUS_FILE: Path
LOCK_FILE: Path
RECORDING_STATE_FILE: Path
CREDENTIALS_FILE: Path
TOKEN_FILE: Path
ACCOUNT_FILE: Path
SCHEDULE_FILE: Path
CANCELED_FILE: Path
CONSENT_FILE: Path
DRIVE_ROOT_CACHE: Path
_install_paths(PROFILE)


def ensure_home() -> Path:
    """Create the home directory and its subfolders. Safe to call repeatedly."""
    for directory in (HOME_DIR, INBOX_DIR, PROCESSED_DIR, WORK_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    # This tree holds .env, account.json and token.json. 0700 is what the
    # files inside it already claim to be; a 0755 directory around a 0600 file
    # is not a hole by itself, but it is the difference between "nobody else
    # can read this" and "nobody else can read this yet".
    for directory in (HOME_DIR, WORK_DIR):
        try:
            directory.chmod(0o700)
        except OSError:
            pass
    return HOME_DIR


def write_private(path: Path, text: str) -> None:
    """Write `text` to `path` as a file only this user can read.

    Private from the first byte, not from a moment later. The ordinary
    write_text() creates the file with whatever the umask allows, which is
    0644 for the usual 022, and a chmod on the next line closes it again; a
    reader in between gets the whole secret, and a write that fails before the
    chmod leaves it open indefinitely. This was also why refreshing a Drive
    token left an existing 0644 file at 0644: the chmod was on the
    authorization path, not the refresh path.

    The write is atomic as well. A crash partway through used to leave a
    truncated credential where a working one had been, which reads as "signed
    out" rather than as an error. The temporary file is created 0600 by
    mkstemp and os.replace carries that mode onto the destination, which also
    repairs a file that an older version left too open.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        # Replaces a symlink rather than following one, which is the behavior
        # wanted here: a link left in place of a credential file is not a
        # reason to write the credential wherever it points.
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


ensure_home()

# --- Settings from .env ---------------------------------------------------
#
# The profile's .env is read into a dict, never loaded into the process
# environment, so switching profiles in one process cannot carry a key from
# one .env into the other. A value in the file wins over one in the
# environment, as reload() has always had it; the environment fills in for
# anything the file leaves out, and the profile supplies the defaults.


def env_defaults(profile: Profile) -> dict[str, str]:
    """Every .env setting, with the default `profile` gives it."""
    return {
        "OPENAI_API_KEY": "",
        "ANTHROPIC_API_KEY": "",
        # Only needed when TRANSCRIBE_MODEL names that provider.
        "DEEPGRAM_API_KEY": "",
        "GROQ_API_KEY": "",
        "DRIVE_ROOT_FOLDER_NAME": profile.drive_root_folder,
        "DRIVE_PARENT_FOLDER_ID": "",
        "NOTION_TOKEN": "",
        "NOTION_DATABASE": "",
        "NOTION_VERSION": "2026-03-11",
        "NOTION_TARGET": profile.notion_target,
        "NOTION_PROP_DUE": "",
        "NOTION_PROP_COURSE": "",
        "NOTION_PROP_KIND": "",
        "NOTION_PROP_SOURCE": "",
        "RECORD_DEVICE": "MacBook Pro Microphone",
        "ACCOUNTS_URL": "https://syllabusaccounts.maincoursemedia.com",
        "DRIVE_SOURCE": "auto",
    }


# Settings that come from .env, with their defaults. reload() re-reads these
# so a long-running panel sees what setup just wrote without a restart.
ENV_SETTINGS = env_defaults(PROFILE)


def _read_env(path: Path) -> dict[str, str]:
    """The .env file's values, or nothing when there is no file yet.

    interpolate=False: dotenv resolves ${OTHER_SETTING} inside a value by
    default, which made every non-secret setting a place to read a secret one
    from. A settings file holds text, not expressions.
    """
    if not path.exists():
        return {}
    return {k: v for k, v in dotenv_values(path, interpolate=False).items() if v is not None}


_ENV = _read_env(ENV_FILE)


def _setting(name: str) -> str:
    """One .env setting: the file, else the environment, else the default."""
    value = _ENV.get(name)
    if value is None:
        value = os.environ.get(name, ENV_SETTINGS[name])
    return value or ENV_SETTINGS[name]


# --- API keys -------------------------------------------------------------

OPENAI_API_KEY = _setting("OPENAI_API_KEY")
ANTHROPIC_API_KEY = _setting("ANTHROPIC_API_KEY")
DEEPGRAM_API_KEY = _setting("DEEPGRAM_API_KEY")
GROQ_API_KEY = _setting("GROQ_API_KEY")

# --- Migration from an older install ---------------------------------------
#
# Three kinds of older install leave data where this version does not look:
# one from before the home directory existed kept everything next to the code,
# one from before the rename kept it in ~/.lectureai, and one from before
# profiles kept it flat in ~/.intake itself. Any of them is offered a move into
# the profile's home the first time the CLI runs against an empty one, and the
# same code does the moving.

# Data files an older install kept next to the code.
LEGACY_FILES = (".env", "token.json", ".drive_root", "pipeline.log")
# The old home held these as well; setup wrote them there, never to a checkout.
LEGACY_HOME_FILES = LEGACY_FILES + ("schedule.toml", "credentials.json")
LEGACY_DIRS = ("inbox", "processed")


def old_default_home() -> Path | None:
    """~/.lectureai, when this install uses the new default and it is on disk.

    Only the default home is a candidate. Someone who set $INTAKE_HOME or
    $LECTUREAI_HOME has said where their data is, and the tests, which always
    set one, must never be offered the real thing.
    """
    if HOME_SOURCE != "default":
        return None
    old = OLD_DEFAULT_HOME.expanduser().resolve()
    if old == HOME_DIR or not old.is_dir():
        return None
    return old


def flat_home() -> Path | None:
    """The root itself, when this profile's data may still sit there flat.

    Before profiles, everything lived directly in ~/.intake (or $INTAKE_HOME),
    which is where the syllabus data of any install from then still is. Only
    the default profile is offered it: that data is lecture data, and moving
    it into another profile's home is the sharing the layout exists to prevent.
    """
    if PROFILE.name != profiles.DEFAULT_PROFILE.name:
        return None
    if ROOT_DIR == HOME_DIR or not ROOT_DIR.is_dir():
        return None
    return ROOT_DIR


def legacy_roots() -> list[tuple[Path, tuple[str, ...]]]:
    """Where an older install may have left data, and the file names to look for."""
    roots: list[tuple[Path, tuple[str, ...]]] = []
    old_home = old_default_home()
    if old_home is not None:
        roots.append((old_home, LEGACY_HOME_FILES))
    flat = flat_home()
    if flat is not None:
        roots.append((flat, LEGACY_HOME_FILES))
    if (CODE_ROOT / "pyproject.toml").exists() and CODE_ROOT != HOME_DIR:
        roots.append((CODE_ROOT, LEGACY_FILES))
    return roots


def data_files_in(root: Path, names: tuple[str, ...]) -> list[Path]:
    """The named files plus the recordings in inbox/ and processed/ under root."""
    found = [root / name for name in names if (root / name).is_file()]
    for sub in LEGACY_DIRS:
        folder = root / sub
        if folder.is_dir():
            found += sorted(p for p in folder.iterdir()
                            if p.is_file() and not p.name.startswith("."))
    return found


def legacy_root(path: Path) -> Path:
    """The older-install directory a path from legacy_files() came from."""
    for root, _names in legacy_roots():
        if root in path.parents:
            return root
    raise ValueError(f"{path} is not inside an older install")


def _legacy_found() -> list[Path]:
    """Every data file an older install left where this one does not look."""
    found: list[Path] = []
    for root, names in legacy_roots():
        found += data_files_in(root, names)
    return found


def legacy_files() -> list[Path]:
    """Leftovers worth *offering* to move.

    Only while the home directory has no .env: once it does, the wizard and
    the CLI stop interrupting, because a prompt on every run is how a prompt
    gets ignored.
    """
    if ENV_FILE.exists():
        return []
    return _legacy_found()


def legacy_leftovers() -> list[Path]:
    """Older data this install does not already have its own copy of.

    Reporting and offering are different questions, and conflating them is how
    the migration became unreachable: saving Setup writes .env, which silenced
    the offer, and saving Setup is also the one action that moves nothing.
    Somebody who set up before migrating kept their recordings in a directory
    nothing reads, with nothing telling them so.

    A file the home already holds is excluded, because that one was superseded
    rather than stranded: the migration keeps the newer copy on purpose and
    leaves the old one be, and repeating that forever is nagging about a
    decision already taken.
    """
    return [path for path in _legacy_found()
            if not (HOME_DIR / path.relative_to(legacy_root(path))).exists()]


# --- Google Drive ---------------------------------------------------------

# drive.file is the only Drive scope Google treats as non-sensitive: it needs
# no app verification, and its refresh tokens don't expire after 7 days the way
# a Testing-mode app's do. The tradeoff is that it only reaches files this app
# created — so the app creates its own root folder rather than writing into one
# you made by hand. Move that folder anywhere in Drive afterward; per-file
# access follows it. Delete token.json and re-authorize if you change this.
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]

# The app creates this folder in My Drive on first upload and files courses
# (or clients) under it. The name is the profile's, "Lecture Notes" for
# syllabus, unless .env says otherwise. Its id is cached in DRIVE_ROOT_CACHE
# so renaming or moving the folder in Drive doesn't matter.
DRIVE_ROOT_FOLDER_NAME = _setting("DRIVE_ROOT_FOLDER_NAME")

# Optional override: pin a specific folder id instead. Only works for a folder
# this app created, given the drive.file scope above.
DRIVE_PARENT_FOLDER_ID = _setting("DRIVE_PARENT_FOLDER_ID")

# --- Models ---------------------------------------------------------------

# Which transcription provider runs. Every name in providers.PROVIDERS works:
# "whisper-1", "groq/whisper-large-v3", "deepgram/nova-3". The chunking limits
# travel with the model, so swapping this swaps them too; an unlisted name is
# treated as an OpenAI model on the default limits below.
TRANSCRIBE_MODEL = "gpt-4o-mini-transcribe"
CLAUDE_MODEL = "claude-sonnet-5"

# The study assistant's model, kept separate from the summarizer's so the two
# can move independently: a summary is one constrained call per lecture, an
# assistant session is many turns over a much larger context, and they will
# not always want the same model.
#
# Sonnet 5 rather than Opus 5, and that is a costing decision. HOME-STRETCH.md
# prices Pro at $25 on a Sonnet assistant, netting $13.22 at a 53% margin; the
# same tier on an Opus assistant nets $7.27 at 29%, which is the thin margin
# the plan was written to avoid. Opus is roughly 2.5x the input price and 2.5x
# the output. Changing this line changes the published price.
ASSISTANT_MODEL = "claude-sonnet-5"

# --- Audio handling -------------------------------------------------------

AUDIO_EXTENSIONS = {".m4a", ".mp3", ".wav"}

# The four constants below are the DEFAULT PROVIDER's limits: OpenAI's
# transcription endpoint with a gpt-4o transcribe model. They are not facts
# about audio, and nothing outside providers.py should read them. Deepgram,
# for one, takes 2GB and never splits on duration.

WHISPER_LIMIT_BYTES = 25 * 1024 * 1024   # hard OpenAI file-size limit
COMPRESS_THRESHOLD_BYTES = 24 * 1024 * 1024  # compress before we get close to it

# The gpt-4o transcribe models cap their OUTPUT near 2000 tokens (~1740 words)
# and truncate silently rather than erroring, so long audio must be split on
# duration as well as size. Measured against a real lecture: clean through
# 12 min at 145 words/min, truncated at 15 min and beyond.
#
# 8 minutes leaves headroom for fast talkers (safe to roughly 210 words/min).
# Lower it if you see truncation warnings; raise it toward 12 for slow ones.
# This is the gpt-4o limit only. Whisper and Deepgram have no output cap, and
# providers.py gives them their own, longer chunks.
CHUNK_SECONDS = 8 * 60

# A chunk coming back at or above this word count probably got cut off.
TRUNCATION_WORD_THRESHOLD = 1700

# --- Output handling ---

# Upload the summary as a real Google Doc rather than a .md file.
SUMMARY_AS_GOOGLE_DOC = True

# Transcripts go in this subfolder of the course folder, keeping the course
# folder itself a clean list of study notes. Set to "" to file them alongside.
TRANSCRIPT_SUBFOLDER = "Transcripts"

# Delete the recording once both uploads succeed, instead of archiving it in
# processed/. Set to False to keep the originals.
DELETE_ORIGINAL_AFTER_UPLOAD = True

# --- Notion (notion_tasks.py) ---------------------------------------------

# Internal integration secret from notion.so/my-integrations. Leave it unset
# and the pipeline simply skips Notion; nothing else changes.
NOTION_TOKEN = _setting("NOTION_TOKEN")

# The to-do database action items are added to. Paste the whole Notion URL;
# the id is pulled out of it.
NOTION_DATABASE = _setting("NOTION_DATABASE")

# Pinned deliberately. 2025-09-03 split databases from data sources, changing
# the page parent and the query endpoint, so the version and the request
# shapes in notion_tasks.py have to move together.
NOTION_VERSION = _setting("NOTION_VERSION")

# Where action items go: "weekly" writes a checkbox into the day column of
# the weekly page, which is the list you actually tick; "database" creates a
# row with Due/Course/Source fields instead. They are different surfaces, and
# a row is invisible from the weekly page. The default is the profile's:
# weekly for syllabus, database for sous.
NOTION_TARGET = _setting("NOTION_TARGET")

# Optional overrides if the automatic property matching picks wrong. Each is
# the exact property name in your database.
NOTION_PROP_DUE = _setting("NOTION_PROP_DUE")
NOTION_PROP_COURSE = _setting("NOTION_PROP_COURSE")
NOTION_PROP_KIND = _setting("NOTION_PROP_KIND")
NOTION_PROP_SOURCE = _setting("NOTION_PROP_SOURCE")

# --- Recording (record.py) ------------------------------------------------

# Which microphone to record from. Either a substring of the device name as
# ffmpeg reports it ("MacBook Pro") or an avfoundation index ("1").
#
# Prefer a name. Indices are assigned in connection order, so plugging in a
# headset or waking a nearby iPhone renumbers them: on this Mac index 0 is
# often the iPhone's mic rather than the built-in one, and a lecture recorded
# through a phone that then leaves the room is a lecture you don't have.
RECORD_DEVICE = _setting("RECORD_DEVICE")

# --- The panel on the web (gui.py) ------------------------------------------
# The panel itself only ever listens on this Mac. To reach it from elsewhere
# it connects out to the account service's relay (relay.py), and the service
# is the login in front of it: the account's owner may enter and nobody
# else. A Mac with no account has no address on the web at all. Requests
# from this Mac are never gated. (The panel's own sign-ins were retired:
# PANEL_GOOGLE_CLIENT_ID, PANEL_GOOGLE_CLIENT_SECRET and PANEL_ALLOWED_EMAILS
# with the Google allowlist on 2026-09-14, then PANEL_PUBLIC_URL and
# PANEL_SECRET_KEY with the Cloudflare Tunnel on 2026-09-15. None are read;
# the doctor says so if they are still sitting in .env.)
# The Syllabus account service this panel can be claimed into (account.py).
# The default is the real one; set it to "off" to hide accounts entirely,
# or to a dev server's address to test against that.
ACCOUNTS_URL = _setting("ACCOUNTS_URL")
# Which Drive credentials the uploader uses when this Mac is signed in to an
# account that has a Drive grant. "auto" prefers the account's grant unless
# it cannot see this Mac's existing Drive folder (the folder was created by
# a different Google Cloud project, so drive.file does not reach it), in
# which case this Mac's own token.json is kept so nothing is filed twice.
# "account" and "local" force one or the other.
DRIVE_SOURCE = _setting("DRIVE_SOURCE")

# Names to fall back through if RECORD_DEVICE matches nothing attached.
RECORD_DEVICE_FALLBACKS = ("MacBook Pro Microphone", "Built-in", "Microphone")

# Mono at 16 kHz is what the transcription models resample to anyway, so
# anything richer is bytes spent on quality that gets discarded.
RECORD_SAMPLE_RATE = 16000
RECORD_CHANNELS = 1
RECORD_BITRATE = "64k"

# Stop on your own; this only guards against a recorder left running all night.
RECORD_MAX_MINUTES = 240

# When to say the microphone is delivering nothing. ffmpeg writes the m4a
# header at once and flushes audio in 32KB blocks, so at 64kbps a live mic
# has put well over 4KB on disk within a few seconds. A recording still under
# that after a minute is capturing silence from a blocked device: a lecture
# once ran 50 minutes that way with the timer ticking and nothing on disk.
RECORD_NO_AUDIO_SECONDS = 60
RECORD_NO_AUDIO_BYTES = 4096

# --- Watcher ---

# Phone sync writes incrementally, so a file isn't ready the moment it appears.
STABILITY_SECONDS = 10        # size must hold steady this long
STABILITY_POLL_SECONDS = 2    # how often to re-check the size
STABILITY_TIMEOUT_SECONDS = 3600  # give up waiting on a file still growing

# --- Class schedule -------------------------------------------------------
#
# The schedule is a file in the home directory (schedule.toml for syllabus;
# the profile names it), written by `intake setup` and editable by hand: one
# row per class meeting with the
# day, the start hour, and the course code. It is read on first use and
# cached, so a missing or broken file is reported by whichever command needs
# it rather than by every import.

DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

_DAY_ALIASES = {
    "mon": "Mon", "monday": "Mon",
    "tue": "Tue", "tues": "Tue", "tuesday": "Tue",
    "wed": "Wed", "weds": "Wed", "wednesday": "Wed",
    "thu": "Thu", "thur": "Thu", "thurs": "Thu", "thursday": "Thu",
    "fri": "Fri", "friday": "Fri",
    "sat": "Sat", "saturday": "Sat",
    "sun": "Sun", "sunday": "Sun",
}

# How far a recording's START may sit from a class start and still match.
# Measured from the start, not the file mtime, so this must stay well under
# the gap between back-to-back classes: with one class at 12:00 and the next
# at 14:00 on the same day, anything near 60 makes the two windows meet.
DEFAULT_TOLERANCE_MINUTES = 45

UNKNOWN_COURSE = "UNKNOWN"

# Matches a course code in a filename: "acct-4321", "ACCT4321", "acct_4321".
COURSE_CODE_RE = re.compile(r"([A-Za-z]{2,4})[-_ ]?(\d{4})")

# Long enough for "Introduction to Financial Accounting II", short enough that
# it cannot push a generated filename past what a filesystem will take.
MAX_COURSE_LENGTH = 64

# A course code is typed by the user and then used as a TOML value, a path
# component, a Drive folder name and the text of an <option>. These are the
# characters that break one of those: the control codes, the quote and
# backslash that TOML would have to escape, and the two path separators.
# Everything else, including spaces, parentheses and periods, is left alone —
# real course names have all three.
COURSE_UNSAFE_RE = re.compile(r'[\x00-\x1f\x7f"\\/]')


class ScheduleError(RuntimeError):
    """The schedule file is missing or cannot be read."""


@dataclass(frozen=True)
class Meeting:
    day: str      # "Mon" .. "Sun"
    hour: int     # start hour, 24h local time
    course: str


@dataclass(frozen=True)
class Schedule:
    meetings: tuple[Meeting, ...]
    tolerance_minutes: int = DEFAULT_TOLERANCE_MINUTES

    @property
    def by_slot(self) -> dict[tuple[str, int], str]:
        """(day, start hour) -> course code, the shape the matching uses."""
        return {(m.day, m.hour): m.course for m in self.meetings}

    def courses(self) -> list[str]:
        return sorted({m.course for m in self.meetings})


def normalize_day(raw: object) -> str:
    """"tuesday", "Tues", "TUE" -> "Tue". Raises ValueError for anything else."""
    key = str(raw).strip().lower()
    if key not in _DAY_ALIASES:
        raise ValueError(f"day must be one of {', '.join(DAYS)}, not {raw!r}")
    return _DAY_ALIASES[key]


def normalize_hour(raw: object) -> int:
    """A start hour as an int 0..23. Accepts 14, "14", "14:00", "2pm"."""
    if isinstance(raw, bool):
        raise ValueError(f"start hour must be a number 0-23, not {raw!r}")
    if isinstance(raw, int):
        hour = raw
    else:
        text = str(raw).strip().lower().replace(" ", "")
        match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(am|pm)?", text)
        if not match:
            raise ValueError(f"start hour must be a number 0-23, not {raw!r}")
        hour = int(match.group(1))
        if match.group(2) and match.group(2) != "00":
            raise ValueError(
                f"start hour must be a whole hour ({raw!r} has minutes); the "
                f"tolerance window covers classes that start on the half hour"
            )
        suffix = match.group(3)
        if suffix == "pm" and hour < 12:
            hour += 12
        elif suffix == "am" and hour == 12:
            hour = 0
    if not 0 <= hour <= 23:
        raise ValueError(f"start hour must be 0-23, not {raw!r}")
    return hour


def normalize_course(raw: object) -> str:
    """"acct-4321", "ACCT4321", "acct_4321" -> "ACCT-4321".

    Anything shaped like a course code is written the one way the filename
    fallback and the Drive folders expect. A code with another shape is kept
    as typed, provided it is safe to put in a filename and a schedule file.

    This is the write path, and it refuses rather than repairs. A course being
    typed into Setup has somebody sitting in front of it who can fix it, and
    silently changing what they typed would file their lectures somewhere they
    did not ask for. Text that is already on disk goes through safe_course()
    instead, which never refuses.
    """
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        raise ValueError(f"course code must be text, not {type(raw).__name__}")
    text = str(raw).strip()
    if not text:
        raise ValueError("course code is empty")
    if len(text) > MAX_COURSE_LENGTH:
        raise ValueError(
            f"course code is longer than {MAX_COURSE_LENGTH} characters")
    found = COURSE_UNSAFE_RE.search(text)
    if found:
        shown = repr(found.group())
        raise ValueError(
            f"course code cannot contain {shown}; it becomes a filename and a "
            f"folder name, so a slash or a quote would break them"
        )
    if text.startswith("."):
        # ".." walks out of the inbox; a leading dot also hides the file.
        raise ValueError("course code cannot start with a period")
    match = COURSE_CODE_RE.fullmatch(text)
    if match:
        return f"{match.group(1).upper()}-{match.group(2)}"
    return text


def safe_course(raw: object) -> str:
    """A course repaired into something safe to use. Never raises.

    The read path, and the last line before a course becomes a real path. A
    schedule.toml can be edited by hand, can arrive from the account service,
    and on an install predating normalize_course's checks can hold text that
    would break a filename. Refusing the whole file over one bad row would
    take the panel down for somebody else's typo, so the row is repaired and
    the rest of the timetable keeps working.

    Returns UNKNOWN_COURSE when nothing usable is left, which is the same
    thing the pipeline already does with a recording it cannot place.
    """
    text = "" if raw is None else str(raw)
    text = COURSE_UNSAFE_RE.sub("-", text)
    text = " ".join(text.split()).lstrip(".").strip()
    text = text[:MAX_COURSE_LENGTH].strip()
    if not text:
        return UNKNOWN_COURSE
    try:
        return normalize_course(text)
    except ValueError:
        return UNKNOWN_COURSE


def parse_schedule(text: str, source: str = "schedule.toml") -> Schedule:
    """Parse the TOML text of a schedule file into a Schedule.

    Raises ScheduleError naming the row that is wrong, so a typo in one line
    points at that line rather than at the whole file.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ScheduleError(f"{source} is not valid TOML: {exc}") from None

    tolerance = data.get("tolerance_minutes", DEFAULT_TOLERANCE_MINUTES)
    if isinstance(tolerance, bool) or not isinstance(tolerance, int) or tolerance < 0:
        raise ScheduleError(
            f"{source}: tolerance_minutes must be a whole number of minutes, "
            f"not {tolerance!r}"
        )

    rows = data.get("classes")
    if rows is None:
        raise ScheduleError(
            f"{source} has no `classes` list. Each class meeting is one row "
            f"like  {{ day = \"Tue\", start = 14, course = \"ACCT-4321\" }}"
        )
    if not isinstance(rows, list):
        raise ScheduleError(f"{source}: `classes` must be a list of rows")

    meetings: list[Meeting] = []
    for n, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ScheduleError(
                f"{source}: class row {n} should look like "
                f"{{ day = \"Tue\", start = 14, course = \"ACCT-4321\" }}, "
                f"not {row!r}"
            )
        missing = [k for k in ("day", "start", "course") if k not in row]
        if missing:
            raise ScheduleError(
                f"{source}: class row {n} is missing {', '.join(missing)}"
            )
        try:
            # A course carrying a stray slash still names a real class, so it
            # is repaired rather than refused: a file written before these
            # checks existed has to keep loading. A row with no course at all
            # is different in kind — there is nothing to repair, and inventing
            # UNKNOWN for it would add a class meeting that quietly collects
            # every recording made at that hour.
            if not str(row["course"] if row["course"] is not None else "").strip():
                raise ValueError("course code is empty")
            meetings.append(Meeting(
                normalize_day(row["day"]),
                normalize_hour(row["start"]),
                safe_course(row["course"]),
            ))
        except ValueError as exc:
            raise ScheduleError(f"{source}: class row {n}: {exc}") from None

    return Schedule(tuple(meetings), tolerance)


def load_schedule(path: Path | None = None) -> Schedule:
    """Read the schedule file. Raises ScheduleError if missing or malformed."""
    file = Path(path) if path is not None else SCHEDULE_FILE
    if not file.exists():
        raise ScheduleError(
            f"no class schedule at {file}. Run:  intake setup"
        )
    return parse_schedule(file.read_text(), file.name)


SCHEDULE_TEMPLATE = """\
# {title} class schedule. One row per class meeting.
#
# day     Mon Tue Wed Thu Fri Sat Sun
# start   the hour the class begins, 24-hour clock (14 means 2pm)
# course  the code used for the Drive folder and filenames
#
# Edit this file by hand or rerun `intake setup`. Only courses listed here
# are recognized, so a stray number in a filename cannot invent a folder.

classes = [
{rows}
]

# How far a recording's START may sit from a class start and still match.
# Measured from the start, not the file mtime, so this must stay well under
# the gap between back-to-back classes: with one class at 12:00 and the next
# at 14:00 on the same day, anything near 60 makes the two windows meet and
# the wrong course wins.
tolerance_minutes = {tolerance}
"""


_TOML_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\t": "\\t",
                 "\n": "\\n", "\f": "\\f", "\r": "\\r"}


def _toml_string(value: str) -> str:
    """`value` as a TOML basic string, quotes included.

    The schedule used to be built by interpolating the course straight into
    quoted TOML, so one holding a quote, a backslash or a newline wrote a file
    that no longer parsed — and it was written over the timetable that did.
    normalize_course refuses those characters now, but a serializer that
    depends on its caller having validated is one call site away from doing
    this again.
    """
    out = []
    for char in value:
        if char in _TOML_ESCAPES:
            out.append(_TOML_ESCAPES[char])
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append(f"\\u{ord(char):04X}")
        else:
            out.append(char)
    return '"' + "".join(out) + '"'


def render_schedule(meetings, tolerance_minutes: int = DEFAULT_TOLERANCE_MINUTES) -> str:
    """The text of a schedule file for these meetings.

    `meetings` is any iterable of Meeting or (day, hour, course) rows. Rows are
    written in weekday order so the file reads like a timetable.
    """
    normalized = []
    for row in meetings:
        # A Meeting used to be trusted and copied through unchecked, which let
        # a hand-built one carry text no typed course could ever get past.
        day, hour, course = (
            (row.day, row.hour, row.course) if isinstance(row, Meeting) else row)
        normalized.append(Meeting(normalize_day(day), normalize_hour(hour),
                                  normalize_course(course)))
    normalized.sort(key=lambda m: (DAYS.index(m.day), m.hour))
    quoted = [_toml_string(m.course) for m in normalized]
    width = max((len(q) for q in quoted), default=0)
    lines = [
        f'  {{ day = "{m.day}", start = {m.hour:>2}, '
        f'course = {q}{" " * (width - len(q))} }},'
        for m, q in zip(normalized, quoted)
    ]
    # The title only reaches a comment line, but a newline in it would comment
    # out the row below just the same.
    title = " ".join(str(PROFILE.title).split())
    return SCHEDULE_TEMPLATE.format(title=title, rows="\n".join(lines),
                                    tolerance=tolerance_minutes)


def write_schedule(meetings, tolerance_minutes: int = DEFAULT_TOLERANCE_MINUTES,
                   path: Path | None = None) -> Path:
    """Write the schedule file and drop the cached copy.

    The rendered text is read back before it replaces anything. A schedule
    that cannot be parsed is the user's whole timetable gone, and the old one
    had already been overwritten by the time anyone found out, so the check
    that matters is the one that happens before the write. Rendering to a
    temporary file and renaming means an interrupted write cannot leave half
    a schedule behind either.
    """
    file = Path(path) if path is not None else SCHEDULE_FILE
    text = render_schedule(meetings, tolerance_minutes)
    parse_schedule(text, file.name)
    file.parent.mkdir(parents=True, exist_ok=True)
    temporary = file.with_name(f".{file.name}.new")
    temporary.write_text(text)
    temporary.replace(file)
    reload_schedule()
    return file


_schedule_cache: Schedule | None = None


def schedule() -> Schedule:
    """The loaded schedule, read from SCHEDULE_FILE on first use."""
    global _schedule_cache
    if _schedule_cache is None:
        _schedule_cache = load_schedule()
    return _schedule_cache


def reload_schedule() -> None:
    """Forget the cached schedule so the next call re-reads the file."""
    global _schedule_cache
    _schedule_cache = None


def set_schedule(value: Schedule | dict | None) -> None:
    """Install a schedule without a file, for tests and for --course overrides.

    Accepts a Schedule, the old (day, hour) -> course dict, or None to go
    back to reading the file.
    """
    global _schedule_cache
    if isinstance(value, dict):
        value = Schedule(tuple(Meeting(d, h, c) for (d, h), c in value.items()))
    _schedule_cache = value


def courses() -> list[str]:
    """Every course code in the schedule, sorted."""
    return schedule().courses()


def __getattr__(name: str):
    # The schedule used to be two module constants. Anything still reading
    # them gets the live values from the file.
    if name == "SCHEDULE":
        return schedule().by_slot
    if name == "SCHEDULE_TOLERANCE_MINUTES":
        return schedule().tolerance_minutes
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def recording_start(
    file_mtime: float | datetime, duration_seconds: float | None = None
) -> datetime:
    """When the recording began.

    A file's mtime is when recording *stopped*, so for an 80 minute class it
    lands 80 minutes after the class began — closer to the next class on the
    calendar than to its own. Backing out the audio duration removes that bias
    and is what makes back-to-back classes distinguishable.
    """
    when = (
        file_mtime
        if isinstance(file_mtime, datetime)
        else datetime.fromtimestamp(file_mtime)
    )
    if duration_seconds:
        when -= timedelta(seconds=duration_seconds)
    return when


def infer_course(
    file_mtime: float | datetime, duration_seconds: float | None = None
) -> str:
    """Map a recording to a course code from the schedule.

    Matches the class whose start is nearest the recording's start, within
    the schedule's tolerance. Pass duration_seconds whenever you have it;
    without it this falls back to comparing against the mtime, which skews
    toward the following class.
    """
    when = recording_start(file_mtime, duration_seconds)
    day = when.strftime("%a")  # "Mon", "Tue", ...
    current = schedule()

    best_course = UNKNOWN_COURSE
    best_delta = None

    for (sched_day, sched_hour), course in current.by_slot.items():
        if sched_day != day:
            continue
        start = when.replace(hour=sched_hour, minute=0, second=0, microsecond=0)
        delta_minutes = abs((when - start).total_seconds()) / 60
        if delta_minutes > current.tolerance_minutes:
            continue
        if best_delta is None or delta_minutes < best_delta:
            best_delta = delta_minutes
            best_course = course

    return best_course


def infer_course_from_filename(filename: str) -> str:
    """Pull a known course code out of a filename, or UNKNOWN_COURSE.

    Only codes that appear in the schedule are accepted, so a date or a random
    number in the name can't invent a course folder.
    """
    known = {code.upper(): code for code in courses()}
    for match in COURSE_CODE_RE.finditer(filename):
        candidate = f"{match.group(1).upper()}-{match.group(2)}"
        if candidate in known:
            return known[candidate]
    return UNKNOWN_COURSE


def course_note(path: str | Path) -> Path:
    """Where a recording's explicitly chosen course is kept.

    Beside the audio rather than inside it: the file moves from .work to
    inbox and sometimes on to processed, and a sidecar moves with it if we
    move it. The suffix is outside AUDIO_EXTENSIONS, so the watcher never
    mistakes one for a recording.
    """
    path = Path(path)
    return path.with_suffix(path.suffix + ".course")


def remember_course(path: str | Path, course: str) -> None:
    """Record that somebody chose this course for this recording.

    Only ever called when the choice was explicit. An inferred course is not
    written here, because the whole value of this file is that its presence
    means somebody decided.
    """
    try:
        course_note(path).write_text(safe_course(course))
    except OSError:
        # A lecture is not worth losing over a note about it.
        pass


def chosen_course(path: str | Path) -> str:
    """The course somebody picked for this recording, or "" if nobody did."""
    try:
        text = course_note(path).read_text()
    except OSError:
        return ""
    course = safe_course(text)
    return "" if course == UNKNOWN_COURSE and not text.strip() else course


def forget_course(path: str | Path) -> None:
    """Drop the note, once the recording it belongs to has been dealt with."""
    course_note(path).unlink(missing_ok=True)


def resolve_course(
    path: str | Path,
    file_mtime: float | datetime,
    duration_seconds: float | None = None,
) -> tuple[str, str]:
    """Best guess at the course, plus which signal produced it.

    An explicit choice wins, because it is the only signal that is somebody
    saying what they meant rather than us inferring it. The course picker
    offered a destination that processing then overruled: a lecture recorded
    under one course during another's scheduled hour was filed, named and
    pushed to Notion under the scheduled one.

    Then the schedule, when it matches. The filename is the last fallback,
    which saves recordings whose mtime is the time they were copied rather
    than the time they were recorded — a plain `cp` does exactly that.
    """
    chosen = chosen_course(path)
    if chosen and chosen != UNKNOWN_COURSE:
        return chosen, "chosen"

    scheduled = infer_course(file_mtime, duration_seconds)
    if scheduled != UNKNOWN_COURSE:
        return scheduled, "schedule"

    from_name = infer_course_from_filename(Path(path).name)
    if from_name != UNKNOWN_COURSE:
        return from_name, "filename"

    return UNKNOWN_COURSE, "none"


def lecture_date(
    file_mtime: float | datetime, duration_seconds: float | None = None
) -> str:
    """YYYY-MM-DD of the class, used in output filenames."""
    return recording_start(file_mtime, duration_seconds).strftime("%Y-%m-%d")


def next_class_meeting(
    course: str, after: datetime | str, within_days: int = 21
) -> str | None:
    """The next date `course` meets after `after`, as YYYY-MM-DD.

    Used to date an action item the instructor never put a deadline on.
    Readings and problem sets are usually due the next time the class meets,
    which beats leaving the task undated and letting it sink in a to-do list
    sorted by date. Returns None for a course that isn't in the schedule, or
    one that doesn't meet again inside `within_days`.
    """
    if isinstance(after, str):
        try:
            after = datetime.strptime(after[:10], "%Y-%m-%d")
        except ValueError:
            return None

    meeting_days = {m.day for m in schedule().meetings if m.course == course}
    if not meeting_days:
        return None

    for offset in range(1, within_days + 1):
        candidate = after + timedelta(days=offset)
        if candidate.strftime("%a") in meeting_days:
            return candidate.strftime("%Y-%m-%d")
    return None


def recording_key(
    file_mtime: float | datetime, duration_seconds: float | None = None
) -> str:
    """Stable identity for one recording: the minute it started.

    Stamped onto the uploaded Drive files so a re-run can find and replace what
    it produced last time, even if the topic slug came back different and the
    filename changed with it. Two recordings can't start in the same minute, so
    matching keys mean the same lecture and differing keys mean different ones.

    Derived from the file's mtime and duration, both fixed once recording
    stops, so the same file always produces the same key.
    """
    return recording_start(file_mtime, duration_seconds).strftime("%Y-%m-%dT%H:%M")


def recording_time_suffix(
    file_mtime: float | datetime, duration_seconds: float | None = None
) -> str:
    """HHMM of the recording's start, to tell same-day recordings apart."""
    return recording_start(file_mtime, duration_seconds).strftime("%H%M")


def reload() -> None:
    """Re-read .env and the schedule file into this module.

    Values are taken from the file itself, not from os.environ, so a key the
    user just removed in the file actually goes away rather than lingering
    from the first load.
    """
    global _ENV
    _ENV = _read_env(ENV_FILE)
    for name in ENV_SETTINGS:
        globals()[name] = _setting(name)
    reload_schedule()


def activate(profile: Profile | str) -> Profile:
    """Switch this process to `profile`: its home, its .env, its schedule.

    The CLI settles the profile before this module is first imported, so this
    is for a process that already has it loaded, tests mostly. Nothing read
    from the previous profile's .env survives the switch.
    """
    global PROFILE, ENV_SETTINGS
    PROFILE = profiles.get(profile) if isinstance(profile, str) else profile
    os.environ[PROFILE_ENV_VAR] = PROFILE.name
    _install_paths(PROFILE)
    ENV_SETTINGS = env_defaults(PROFILE)
    ensure_home()
    reload()
    return PROFILE


def free_path(directory: Path, name: str) -> Path:
    """A path in `directory` that nothing is using yet.

    Moving a file onto a name something else already holds destroys what was
    there, without a word. Drive has had this guard since upload._free_name;
    the local moves never did, so an imported recording sharing a name with
    an older one replaced it.

    Never raises. Both callers run after a recording has been finalized and
    its state file cleared, so an exception here would leave audio in a
    temporary directory that nothing ever comes back for — which is the
    failure this is supposed to prevent, arrived at another way.
    """
    directory.mkdir(parents=True, exist_ok=True)
    if not (directory / name).exists():
        return directory / name
    stem, suffix = Path(name).stem, Path(name).suffix
    for n in range(2, 1000):
        candidate = directory / f"{stem}_{n}{suffix}"
        if not candidate.exists():
            return candidate
    # A thousand files of one name is not a real situation, but guessing
    # again forever is not an answer either.
    return directory / f"{stem}_{datetime.now():%Y%m%d%H%M%S%f}{suffix}"


def append_log_line(*fields: str) -> None:
    """Append one tab-separated record to pipeline.log.

    Successful lectures get five fields (time, course, source, name, URL);
    failures get four (time, ERROR, source, message). The panel's recent list
    reads both, so anything that goes wrong must land here to be seen at all.
    Tabs and newlines inside a field would break the next reader, so they are
    collapsed to spaces.
    """
    stamp = datetime.now().isoformat(timespec="seconds")
    clean = [" ".join(str(field).split()) for field in fields]
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a") as fh:
        fh.write("\t".join([stamp, *clean]) + "\n")


def watcher_pid() -> int | None:
    """PID of the watcher currently running, or None.

    The watcher holds an flock on LOCK_FILE for as long as it lives; the pid
    inside is only a label. Checking that pid with kill(0) is not enough: a
    watcher the panel spawned and never reaped answers kill(0) as a zombie
    for as long as the panel runs, and the panel then reports it as running
    and refuses to start another. The lock cannot lie that way, because a
    zombie has already closed its files.
    """
    try:
        handle = LOCK_FILE.open("r")
    except OSError:
        return None
    with handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            held = True
        else:
            fcntl.flock(handle, fcntl.LOCK_UN)
            held = False
        if not held:
            return None
        # The watcher takes the lock and then writes its pid, so a probe that
        # lands between the two reads an empty file. Give it a moment.
        for _ in range(3):
            handle.seek(0)
            text = handle.read().strip()
            if text.isdigit():
                return int(text)
            time.sleep(0.05)
    # Held, but nobody's pid is in it (a watcher from before the lock file
    # stopped being truncated by losers, or a write that never landed). The
    # lock is the truth: a held lock is a running watcher, and saying
    # "stopped" here is what made the panel start a second one. Ask the
    # kernel who has the file open instead.
    return _lock_holder_pid()


def _lock_holder_pid() -> int | None:
    """PID of a process holding LOCK_FILE open, via lsof, or None."""
    try:
        out = subprocess.run(
            ["lsof", "-t", str(LOCK_FILE)], capture_output=True, text=True,
            timeout=5, check=False,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    pids = [int(line) for line in out.split() if line.isdigit()]
    return pids[0] if pids else None


def require(name: str) -> str:
    """Fetch a required setting or fail with a readable message."""
    value = globals().get(name, "")
    if not value:
        raise RuntimeError(
            f"{name} is not set. Run:  intake setup   "
            f"(or add it to {ENV_FILE})"
        )
    return value
