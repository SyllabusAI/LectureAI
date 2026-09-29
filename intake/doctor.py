"""`intake doctor`: one line per check, pass or fail, and the exact fix.

Nothing here touches the network. Every check reads the machine and the home
directory and says what it found, so this is safe to run at any time and is
the first thing to try when something misbehaves.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from intake import account, config, google_client, tools

MIN_PYTHON = (3, 11)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str          # what was found
    fix: str = ""        # what to do about it, shown only on failure
    required: bool = True
    # What to do about it in the panel, when that is not the same thing.
    # The page used to rewrite any fix mentioning `intake setup` into "fill in
    # the section above and save", which is right for a missing key and wrong
    # for anything the Setup form cannot do. A check that knows the difference
    # says so here instead of being guessed at by a regular expression.
    fix_web: str = ""


def check_python() -> Check:
    version = sys.version_info
    text = f"{version.major}.{version.minor}.{version.micro}"
    ok = (version.major, version.minor) >= MIN_PYTHON
    return Check("Python", ok, text,
                 f"needs {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer: brew install python@3.12, "
                 f"then reinstall with pipx")


def check_ffmpeg() -> Check:
    path = tools.find("ffmpeg")
    probe = tools.find("ffprobe")
    if path and probe:
        where = "bundled with Syllabus.app" if tools.bundled() else path
        return Check("ffmpeg", True, where)
    return Check("ffmpeg", False, "not found", tools.install_hint())


def check_home() -> Check:
    home = config.HOME_DIR
    source = config.HOME_SOURCE
    if source == "default":
        where = "default"
    elif source == config.LEGACY_HOME_ENV_VAR:
        where = (f"from ${source}, the old name; set ${config.HOME_ENV_VAR} "
                 f"instead, both work for now")
    else:
        where = f"from ${source}"
    return Check("home directory", home.is_dir(), f"{home} ({where})",
                 "intake setup")


def check_key(label: str, name: str) -> Check:
    value = getattr(config, name, "")
    if value:
        shown = f"{value[:7]}...{value[-4:]}" if len(value) > 14 else "set"
        return Check(label, True, shown)
    return Check(label, False, f"{name} is not set in {config.ENV_FILE}",
                 "intake setup")


def check_schedule() -> Check:
    try:
        loaded = config.load_schedule()
    except config.ScheduleError as exc:
        fix = "intake setup" if not config.SCHEDULE_FILE.exists() \
            else f"fix the row it names in {config.SCHEDULE_FILE}, or rerun intake setup"
        return Check("class schedule", False, str(exc), fix)
    count = len(loaded.meetings)
    courses = ", ".join(loaded.courses()) or "no courses"
    if count == 0:
        return Check("class schedule", False,
                     f"{config.SCHEDULE_FILE} has no classes",
                     "intake setup, and enter at least one class")
    return Check("class schedule", True,
                 f"{count} class meeting{'s' if count != 1 else ''}, "
                 f"{courses}, tolerance {loaded.tolerance_minutes} min")


def check_drive_client() -> Check:
    try:
        installed = google_client.client_config().get("installed", {})
    except (OSError, ValueError) as exc:
        return Check("Drive OAuth client", False, f"unreadable: {exc}",
                     f"delete {config.CREDENTIALS_FILE} to fall back to the bundled client")
    if installed.get("client_id") and installed.get("client_secret"):
        return Check("Drive OAuth client", True, google_client.describe())
    return Check("Drive OAuth client", False, "client config has no client_id",
                 f"delete {config.CREDENTIALS_FILE} to fall back to the bundled client")


def check_drive_token(now: datetime | None = None) -> Check:
    from intake import account
    token = config.TOKEN_FILE
    via_account = account.enabled() and account.load() is not None
    cached = account.drive_cached() if via_account else {}
    if via_account and cached.get("connected"):
        who = cached.get("google_email") or "the account's Google account"
        if cached.get("in_use") is False and token.exists():
            return Check("Drive authorization", True,
                         f"{token.name} present with a refresh token; the account's Drive "
                         f"({who}) is connected but not used because it "
                         f"{cached.get('in_use_detail') or 'was set aside'}")
        extra = f"; this Mac also has its own {token.name}" if token.exists() else ""
        return Check("Drive authorization", True,
                     f"through the Syllabus account, as {who} (last confirmed "
                     f"{cached.get('checked_at', 'unknown')}){extra}")
    if not token.exists():
        if via_account:
            return Check("Drive authorization", False,
                         f"the account has no Drive connection and there is no "
                         f"{token.name} on this Mac",
                         "connect Google Drive from the Setup page, or intake login")
        return Check("Drive authorization", False, f"no {token.name} yet",
                     "intake login")
    try:
        data = json.loads(token.read_text())
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        # A token file holding `[]` or `5` parsed fine and then broke on
        # .get(). Nothing catches that: the Setup page polls this through
        # /api/doctor, so it became a 500, and `intake doctor` a traceback.
        return Check("Drive authorization", False, f"{token} is not readable JSON",
                     f"delete it and run intake login")

    scopes = set(data.get("scopes") or [])
    if scopes and scopes != set(config.DRIVE_SCOPES):
        return Check("Drive authorization", False,
                     f"token was issued for different scopes ({', '.join(sorted(scopes))})",
                     "intake login")

    if data.get("refresh_token"):
        # The access token expires hourly by design; the refresh token is what
        # keeps the watcher going, so its presence is the real test.
        return Check("Drive authorization", True,
                     f"{token.name} present with a refresh token")

    expiry_text = data.get("expiry")
    if expiry_text:
        try:
            expiry = datetime.fromisoformat(expiry_text.replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            current = now or datetime.now(timezone.utc)
            if expiry > current:
                return Check("Drive authorization", True,
                             f"access token valid until {expiry:%Y-%m-%d %H:%M} UTC "
                             f"(no refresh token; will need intake login after that)")
            return Check("Drive authorization", False,
                         f"access token expired {expiry:%Y-%m-%d %H:%M} UTC and "
                         f"there is no refresh token", "intake login")
        except ValueError:
            pass
    return Check("Drive authorization", False,
                 f"{token.name} has neither a refresh token nor a readable expiry",
                 "intake login")


NOTION_SKIP_MARKER = "# notion: skipped"


def notion_skipped() -> bool:
    """Whether setup recorded a deliberate decision not to use Notion."""
    try:
        return NOTION_SKIP_MARKER in config.ENV_FILE.read_text().lower()
    except OSError:
        return False


def check_notion() -> Check:
    token, database = config.NOTION_TOKEN, config.NOTION_DATABASE
    if token and database:
        return Check("Notion", True, f"configured, target {config.NOTION_TARGET}",
                     required=False)
    if token or database:
        missing = "NOTION_DATABASE" if token else "NOTION_TOKEN"
        return Check("Notion", False, f"half configured: {missing} is missing",
                     "intake setup, or clear both NOTION_ values to skip Notion",
                     required=False)
    if notion_skipped():
        return Check("Notion", True, "skipped in setup (optional)", required=False)
    return Check("Notion", True, "not configured (optional)",
                 required=False)


def check_calendars() -> list[Check]:
    """One line per calendar that is switched on. Nothing when none is.

    Local only, like everything here: macOS's permission is read without
    asking for it, and Google Calendar's token is read from disk, never
    tried against Google. The Setup page's Test button does that.
    """
    from intake import calendars
    checks = []
    for key in calendars.KEYS:
        if not calendars.enabled(key):
            continue
        info = calendars.describe(key)
        label = info["label"]
        where = f"into {info['name']!r}"
        if info["state"] == "granted":
            checks.append(Check(label, True, f"{where}, {info['detail']}",
                                required=False))
        elif info["state"] == "unknown":
            checks.append(Check(label, True, f"{where}; {info['detail']}",
                                required=False))
        else:
            word = "google" if key == calendars.GOOGLE else (
                "reminders" if key == calendars.REMINDERS else "apple")
            checks.append(Check(
                label, False, f"{where}, but {info['detail']}",
                f"intake calendar --connect {word}", required=False,
                fix_web=f"switch {label} off and on again in Setup to connect it"))
    return checks


def check_microphone() -> Check:
    if tools.find("ffmpeg") is None:
        return Check("microphone", False, "cannot check without ffmpeg",
                     tools.install_hint())
    from intake import record
    devices = record.list_devices()
    if not devices:
        # The same conclusion record.py draws: no devices from a Mac that has
        # a microphone means the terminal was refused access.
        return Check("microphone", False, "ffmpeg found no audio input devices",
                     "System Settings > Privacy & Security > Microphone: enable it "
                     "for your terminal app, then rerun")
    try:
        _index, name = record.resolve_device(None)
        return Check("microphone", True,
                     f"{len(devices)} input{'s' if len(devices) != 1 else ''}, "
                     f"will record from {name}")
    except RuntimeError as exc:
        return Check("microphone", False, str(exc),
                     "intake setup and pick a microphone that is attached")


RETIRED_SIGNIN_KEYS = ("PANEL_GOOGLE_CLIENT_ID", "PANEL_GOOGLE_CLIENT_SECRET",
                       "PANEL_ALLOWED_EMAILS", "PANEL_PUBLIC_URL",
                       "PANEL_SECRET_KEY")


def _retired_signin_keys() -> list[str]:
    """Settings from the panel's old sign-ins still sitting in .env."""
    from intake import setup_wizard
    values = setup_wizard.read_env(config.ENV_FILE)
    return [k for k in RETIRED_SIGNIN_KEYS if values.get(k)]


def check_web_signin() -> Check | None:
    """Settings from a sign-in the panel no longer has. Nothing when clean.

    The panel has no sign-in of its own any more: the account service's
    relay is the login, and check_panel_web() reports that. All that is
    left to say here is that .env still carries keys nothing reads.
    """
    leftover = _retired_signin_keys()
    if not leftover:
        return None
    return Check("web sign-in", True,
                 f"{', '.join(leftover)} in .env belong to the panel's old "
                 f"sign-ins, which are gone, and can be deleted")


def check_panel_web() -> Check | None:
    """The panel's address on the web, and whether the socket behind it is up.

    Nothing without an account. The state comes from .work/relay.json, which
    the running panel writes whenever the connection changes, so this is
    what the panel last said, stamped with when it said it; the doctor
    never asks the panel or the network.
    """
    from intake import account, relay
    if not account.enabled():
        return None
    acct = account.load()
    if acct is None:
        return None
    url = relay.panel_url(acct)
    state = relay.read_state_file()
    how = state.get("state", "")
    stamp = state.get("written_at") or state.get("since") or ""
    if not state:
        return Check("panel on the web", True,
                     f"{url}; the panel connects when it runs, and no panel has "
                     f"recorded its connection yet", required=False)
    if how == "connected":
        return Check("panel on the web", True,
                     f"{url}; connected since {state.get('connected_at') or stamp}", required=False)
    why = state.get("error") or state.get("detail") or how or "not connected"
    return Check("panel on the web", False,
                 f"{url}; not connected as of {stamp}: {why}",
                 "check `intake service status`; the panel reconnects on its own once it "
                 "is running and online", required=False)


def check_account() -> Check | None:
    """Which Syllabus account this Mac belongs to. Nothing when accounts are off.

    Reads account.json only; the doctor never touches the network, so this
    cannot say whether the service still honors the token. The Setup page
    checks that when it loads.
    """
    from intake import account
    if not account.enabled():
        return None
    acct = account.load()
    if acct is None:
        return Check("Syllabus account", True,
                     "not signed in (optional); the Setup page has the sign-in",
                     required=False)
    return Check("Syllabus account", True,
                 f"{acct.email}; this Mac is \"{acct.device_name}\"", required=False)


def check_legacy() -> Check | None:
    """An older install's data, for as long as it is still sitting there.

    Reported whether or not this install is set up. It used to go quiet the
    moment the home had a .env, which meant saving Setup made the whole
    migration disappear from the doctor, the CLI and the wizard at once —
    and saving Setup is the one action that moves nothing.
    """
    found = config.legacy_leftovers()
    if not found:
        return None
    roots = " and ".join(sorted({str(config.legacy_root(p)) for p in found}))
    names = ", ".join(p.name for p in found[:4]) + (" ..." if len(found) > 4 else "")
    detail = f"{len(found)} data file(s) still in {roots}: {names}"
    if config.legacy_files():
        # Not set up here yet, so the wizard can still offer to do it.
        return Check("older install", False, detail,
                     f"intake setup (it offers to move them into {config.HOME_DIR})",
                     required=False,
                     fix_web="finish setup in a terminal with `intake setup`, "
                             "which offers to move them; saving this page does not")
    # Already set up, so nothing is going to offer any more. Say what to do
    # rather than say nothing, which is what this did before.
    return Check("older install", False, detail,
                 f"move them into {config.HOME_DIR} yourself; this install is "
                 f"already set up, so nothing will offer to do it for you",
                 required=False,
                 fix_web=f"move them into {config.HOME_DIR} yourself. Saving this "
                         f"page does not move recordings, and this install is "
                         f"already set up, so nothing will offer to")


def check_old_home() -> Check | None:
    """Only reported while ~/.lectureai, the home before the rename, exists.

    Before the move, check_legacy carries the files and the fix. Once the new
    home has its .env this is the line that says the move happened, or that
    the old folder still holds data nothing reads any more.
    """
    old = config.old_default_home()
    if old is None or not config.ENV_FILE.exists():
        return None
    left = config.data_files_in(old, config.LEGACY_HOME_FILES)
    if not left:
        return Check("older home", True,
                     f"{old} migrated into {config.HOME_DIR}; only scratch files "
                     f"remain there", required=False)
    return Check("older home", True,
                 f"{old} still holds {len(left)} data file(s) nothing reads now; "
                 f"{config.HOME_DIR} is the home in use", required=False)


def _key_checks() -> list[Check]:
    """The keys this Mac needs, which on a managed Mac is none of them.

    Signed in, transcription and summaries are the account service's calls on
    the account service's keys, so a missing OPENAI_API_KEY here is correct
    rather than broken and doctor must not report it as a fault.
    """
    if account.managed():
        acct = account.load()
        who = acct.email if acct else ""
        return [Check("API keys", True,
                      f"managed by the Syllabus account{f' ({who})' if who else ''}",
                      "")]
    return [check_key("OpenAI key", "OPENAI_API_KEY"),
            check_key("Anthropic key", "ANTHROPIC_API_KEY")]


def run_checks() -> list[Check]:
    checks = [
        check_python(),
        check_ffmpeg(),
        check_home(),
        *_key_checks(),
        check_schedule(),
        check_drive_client(),
        check_drive_token(),
        check_notion(),
        *check_calendars(),
        check_microphone(),
    ]
    for extra in (check_web_signin(), check_account(), check_panel_web(), check_legacy(),
                  check_old_home()):
        if extra:
            checks.append(extra)
    return checks


def render(checks: list[Check]) -> str:
    width = max(len(c.name) for c in checks)
    lines = []
    for c in checks:
        mark = "ok  " if c.ok else "FAIL"
        lines.append(f"{mark}  {c.name:<{width}}  {c.detail}")
        if not c.ok and c.fix:
            lines.append(f"{'':6}{'':{width}}  fix: {c.fix}")
    failed = [c for c in checks if not c.ok and c.required]
    optional = [c for c in checks if not c.ok and not c.required]
    lines.append("")
    if not failed and not optional:
        lines.append("Everything checks out.")
    elif not failed:
        lines.append(f"Ready to run. {len(optional)} optional item(s) need attention.")
    else:
        lines.append(f"{len(failed)} required check(s) failed. Fix those and rerun "
                     f"intake doctor.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="intake doctor",
        description="Check this install and say exactly how to fix what is wrong.",
    )
    parser.parse_args(argv)
    checks = run_checks()
    print(render(checks))
    return 0 if all(c.ok or not c.required for c in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
