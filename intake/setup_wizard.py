"""`intake setup`: the interactive first run.

Asks for the two API keys, the microphone, the class schedule, optionally
Notion, and once, whether the person has permission to record, then writes
.env, schedule.toml and consent.json into the home directory. Never
touches the code directory. Run it again and it shows what is there, so one
value can be changed without retyping the rest.

Every prompt goes through `ask` and every message through `say`, so the whole
flow can be driven by a test with scripted answers and a temp home.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import sys
from pathlib import Path
from typing import Callable

from dotenv import dotenv_values

from intake import config, tools

Ask = Callable[[str], str]
Say = Callable[[str], None]

NOTION_SKIP_MARKER = "# notion: skipped in setup"

# Keys the wizard owns. Anything else already in .env is carried over
# untouched under its own heading, so a hand-added override survives a rerun.
MANAGED_KEYS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "RECORD_DEVICE",
                "NOTION_TOKEN", "NOTION_DATABASE")

ENV_TEMPLATE = """\
# {title} settings. Written by `intake setup`; safe to edit by hand.
# Rerun `intake setup` to change one value without retyping the rest.

OPENAI_API_KEY={openai}
ANTHROPIC_API_KEY={anthropic}

# Which microphone to record from: a substring of the device name as ffmpeg
# reports it (intake record --list-devices). Names beat indices, which get
# reshuffled whenever a phone or headset connects.
RECORD_DEVICE={device}

# Optional. Send lecture action items to a Notion to-do database as dated
# tasks. With both unset the pipeline skips Notion entirely. Create an
# integration at notion.so/my-integrations, then share your database with it
# (database ... menu, Connections), or every call returns 404.
{notion}
"""

EXTRA_TEMPLATE = """
# Other settings kept from your previous .env.
{extras}
"""


class UnsafeSetting(ValueError):
    """A settings value that cannot be written to .env as one line."""


# A settings value is one line of a .env file. A newline in one does not
# corrupt the file, which would at least be noticed: it ends that setting and
# starts another, so "MacBook\nACCOUNTS_URL=https://collector.invalid" saved as
# a microphone silently moved where this Mac sends its account token. Carriage
# returns and the other control characters are refused with it: none of them
# belong in any value this program writes, and deciding case by case what each
# one does to a parser is how the newline was missed in the first place.
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def check_setting(name: str, value: str) -> str:
    """`value`, or raise UnsafeSetting saying why it cannot be written."""
    found = _CONTROL.search(value)
    if found:
        shown = repr(found.group())[1:-1]
        raise UnsafeSetting(
            f"{name} cannot contain {shown}; it is written as one line of a "
            f"settings file and a line break there would start a new setting")
    return value


def _quote(value: str) -> str:
    """Quote a value for .env.

    Everything is quoted, not only what looks dangerous. Deciding which
    characters need it means keeping a list of what dotenv treats specially,
    and that list was wrong: an unquoted ${OPENAI_API_KEY} in the microphone
    field came back out of the file as the key itself. Quoting unconditionally
    needs no such list to be right.

    $ is deliberately NOT escaped. What made ${OPENAI_API_KEY} dangerous was
    interpolation on the way in, which is off in both readers now; escaping it
    here as well would write a backslash into the value and hand a microphone
    called "$5 mic" back as "\\$5 mic".
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"' 


def mask(secret: str) -> str:
    """Enough of a key to recognize it, never enough to use it."""
    if not secret:
        return "not set"
    if len(secret) <= 10:
        return "set"
    return f"{secret[:6]}...{secret[-4:]}"


def render_env(values: dict[str, str], notion_skipped: bool) -> str:
    """The text of .env for these values.

    Every value is checked before any of it is rendered, so a bad one cannot
    produce a half-written file.
    """
    for name, value in sorted(values.items()):
        if value is not None:
            check_setting(name, value)
    if notion_skipped or not (values.get("NOTION_TOKEN") or values.get("NOTION_DATABASE")):
        notion = (f"{NOTION_SKIP_MARKER}\n"
                  f"# NOTION_TOKEN=ntn_...\n"
                  f"# NOTION_DATABASE=https://www.notion.so/...")
    else:
        notion = (f"NOTION_TOKEN={_quote(values.get('NOTION_TOKEN', ''))}\n"
                  f"NOTION_DATABASE={_quote(values.get('NOTION_DATABASE', ''))}")

    text = ENV_TEMPLATE.format(
        title=config.PROFILE.title,
        openai=_quote(values.get("OPENAI_API_KEY", "")),
        anthropic=_quote(values.get("ANTHROPIC_API_KEY", "")),
        device=_quote(values.get("RECORD_DEVICE", "")),
        notion=notion,
    )
    extras = {k: v for k, v in values.items()
              if k not in MANAGED_KEYS and v is not None}
    if extras:
        text += EXTRA_TEMPLATE.format(
            extras="\n".join(f"{k}={_quote(v)}" for k, v in sorted(extras.items()))
        )
    return text


def write_env(path: Path, values: dict[str, str], notion_skipped: bool) -> Path:
    # Rendered first: an unsafe value raises before anything is created.
    config.write_private(path, render_env(values, notion_skipped))
    return path


def read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return {k: (v or "") for k, v in dotenv_values(path, interpolate=False).items()}


def parse_meeting_line(line: str) -> config.Meeting:
    """"Tue 14 ACCT-4321" -> Meeting. Commas are fine too."""
    parts = [p for p in line.replace(",", " ").split() if p]
    if len(parts) != 3:
        raise ValueError(
            "enter three things: day, start hour, course code, "
            "like  Tue 14 ACCT-4321"
        )
    day, hour, course = parts
    return config.Meeting(config.normalize_day(day), config.normalize_hour(hour),
                          config.normalize_course(course))


def _yes(answer: str, default: bool) -> bool:
    text = answer.strip().lower()
    if not text:
        return default
    return text in ("y", "yes")


class Wizard:
    def __init__(self, ask: Ask = input, say: Say = print,
                 home: Path | None = None, devices=None, allow_login: bool = True,
                 ask_secret: Ask | None = None):
        self.ask = ask
        # None means "decide when the prompt happens", which matters because
        # callers replace .ask after construction; deciding here left those
        # with a secret prompt still wired to the real terminal.
        self.ask_secret = ask_secret
        self.say = say
        self.home = Path(home) if home is not None else config.HOME_DIR
        self.env_file = self.home / ".env"
        self.schedule_file = self.home / config.SCHEDULE_FILE.name
        self.token_file = self.home / "token.json"
        # Injected for tests; otherwise asked of ffmpeg when needed.
        self._devices = devices
        self.allow_login = allow_login

    # --- prompts ------------------------------------------------------------

    def _secret_reader(self):
        """How to read a value that should not appear on screen.

        Keys are typed at a terminal that is often recorded, shared or simply
        visible, and a key echoed once stays in the scrollback for the rest of
        the session. getpass reads without echoing.

        Only the real terminal gets that treatment. A caller that supplied its
        own `ask`, or replaced it afterwards, is scripting the wizard and has
        nothing to hide from a screen; sending it to getpass instead would
        ignore its answers and block on stdin.
        """
        if self.ask_secret is not None:
            return self.ask_secret, False
        if self.ask is input:
            return getpass.getpass, True
        return self.ask, False

    def _prompt(self, label: str, current: str = "", secret: bool = False) -> str:
        shown = mask(current) if secret else current
        suffix = f" [{shown}]" if current else ""
        ask, hidden = self._secret_reader() if secret else (self.ask, False)
        hint = " (not shown as you type)" if hidden else ""
        try:
            answer = ask(f"{label}{suffix}{hint}: ").strip()
        except EOFError:
            answer = ""
        return answer or current

    def ask_keys(self, values: dict[str, str]) -> None:
        from intake import account

        # A signed-in Mac spends the account's keys. Asking for two it does
        # not have invites the user to invent an answer, and Enter through
        # both prompts is the only reason this wizard was a way around the
        # panel's own managed-account gate.
        if account.managed():
            self.say("")
            self.say("API keys are covered by your Syllabus account; nothing to enter.")
            return
        self.say("")
        self.say("API keys. Paste each one; Enter keeps the value shown.")
        values["OPENAI_API_KEY"] = self._prompt(
            "OpenAI API key (platform.openai.com/api-keys)",
            values.get("OPENAI_API_KEY", ""), secret=True)
        values["ANTHROPIC_API_KEY"] = self._prompt(
            "Anthropic API key (console.anthropic.com)",
            values.get("ANTHROPIC_API_KEY", ""), secret=True)

    def devices(self) -> list[tuple[int, str]]:
        if self._devices is not None:
            return self._devices
        if tools.find("ffmpeg") is None:
            return []
        from intake import record
        return record.list_devices()

    def ask_microphone(self, values: dict[str, str]) -> None:
        self.say("")
        current = values.get("RECORD_DEVICE", "")
        devices = self.devices()
        if not devices:
            if tools.find("ffmpeg") is None:
                self.say(f"Microphone: ffmpeg is not installed yet ({tools.install_hint()}), "
                         "so the list is empty. Keeping the default for now.")
            else:
                self.say("Microphone: ffmpeg found no audio inputs. If this Mac has "
                         "one, grant your terminal microphone access in System "
                         "Settings > Privacy & Security. Keeping the default for now.")
            values["RECORD_DEVICE"] = current or config.RECORD_DEVICE
            return

        self.say("Microphone. Pick a number, or Enter to keep the current one.")
        for index, name in devices:
            marker = "   <- current" if current and current.lower() in name.lower() else ""
            self.say(f"  [{index}] {name}{marker}")
        if not current:
            # The same preference record.py applies: the built-in mic by
            # name, never whatever happens to be index 0.
            for candidate in (config.RECORD_DEVICE, *config.RECORD_DEVICE_FALLBACKS):
                hit = next((n for _i, n in devices if candidate.lower() in n.lower()), None)
                if hit:
                    current = hit
                    break
            current = current or devices[0][1]
        while True:
            answer = self._prompt("Microphone", current)
            if answer.isdigit():
                match = next((n for i, n in devices if i == int(answer)), None)
                if match:
                    values["RECORD_DEVICE"] = match
                    return
                self.say(f"  no device numbered {answer}")
                continue
            matches = [n for _i, n in devices if answer.lower() in n.lower()]
            if len(matches) == 1:
                # Store the full name the substring picked out, so the file
                # reads unambiguously and a later rerun marks it as current.
                values["RECORD_DEVICE"] = matches[0]
                return
            if matches:
                values["RECORD_DEVICE"] = answer
                return
            self.say(f"  nothing attached matches {answer!r}; try a number from the list")

    def ask_schedule(self) -> tuple[list[config.Meeting], int]:
        self.say("")
        existing: config.Schedule | None = None
        if self.schedule_file.exists():
            try:
                existing = config.load_schedule(self.schedule_file)
            except config.ScheduleError as exc:
                self.say(f"Your current schedule file could not be read: {exc}")

        if existing and existing.meetings:
            self.say("Class schedule on file:")
            for m in sorted(existing.meetings,
                            key=lambda m: (config.DAYS.index(m.day), m.hour)):
                self.say(f"  {m.day} {m.hour:>2}:00  {m.course}")
            try:
                keep = self.ask("Keep this schedule? [Y/n] ")
            except EOFError:
                keep = ""
            if _yes(keep, default=True):
                return list(existing.meetings), existing.tolerance_minutes

        tolerance = existing.tolerance_minutes if existing else config.DEFAULT_TOLERANCE_MINUTES
        self.say("Class schedule. One meeting per line as: day, start hour "
                 "(24-hour clock), course code.")
        self.say("  For example:  Tue 14 ACCT-4321     Enter a blank line when done.")
        meetings: list[config.Meeting] = []
        while True:
            try:
                line = self.ask(f"  class {len(meetings) + 1}: ").strip()
            except EOFError:
                line = ""
            if not line:
                if meetings:
                    break
                self.say("  at least one class is needed for recordings to be filed")
                try:
                    again = self.ask("  add one now? [Y/n] ")
                except EOFError:
                    again = "n"
                if _yes(again, default=True):
                    continue
                break
            try:
                meetings.append(parse_meeting_line(line))
            except ValueError as exc:
                self.say(f"  {exc}")
        return meetings, tolerance

    def ask_notion(self, values: dict[str, str]) -> bool:
        """Returns True if Notion was deliberately skipped."""
        self.say("")
        token, database = values.get("NOTION_TOKEN", ""), values.get("NOTION_DATABASE", "")
        if token and database:
            self.say(f"Notion is set up (token {mask(token)}).")
            try:
                keep = self.ask("Keep it? [Y/n] ")
            except EOFError:
                keep = ""
            if _yes(keep, default=True):
                return False
        else:
            self.say("Notion is optional: each deadline a lecture mentions becomes a "
                     "task in a to-do database.")
            try:
                want = self.ask("Set up Notion now? [y/N] ")
            except EOFError:
                want = ""
            if not _yes(want, default=False):
                values["NOTION_TOKEN"] = ""
                values["NOTION_DATABASE"] = ""
                return True

        self.say("  Create an internal integration at notion.so/my-integrations, then "
                 "share your to-do database with it (... menu > Connections).")
        values["NOTION_TOKEN"] = self._prompt("  Notion integration secret", token, secret=True)
        values["NOTION_DATABASE"] = self._prompt("  Notion database URL", database)
        if not (values["NOTION_TOKEN"] and values["NOTION_DATABASE"]):
            self.say("  Both are needed; leaving Notion off for now.")
            values["NOTION_TOKEN"] = ""
            values["NOTION_DATABASE"] = ""
            return True
        return False

    def ask_consent(self) -> bool | None:
        """Ask once whether the person has permission to record.

        Returns True for a yes that still has to be written, False for a no,
        and None when it is already on file and nothing was asked. Only an
        explicit yes counts; Enter is a no, because agreeing to this by
        accident is the one answer that must not happen.
        """
        from intake import consent

        self.say("")
        on_file = consent.load(self.home)
        if on_file is not None:
            when = str(on_file.get("agreed_at", ""))[:10]
            self.say(f"Permission to record: confirmed{f' on {when}' if when else ''}.")
            return None
        self.say("Permission to record. Before this Mac records anything, confirm this:")
        self.say(f"  {consent.statement()}")
        try:
            answer = self.ask("Do you have that permission? [y/N] ")
        except EOFError:
            answer = ""
        if _yes(answer, default=False):
            return True
        self.say(f"  Not confirmed. Recording stays off until you run  "
                 f"{consent.fix_command()}  and answer yes.")
        return False

    def write_consent(self, agreed: bool | None) -> None:
        if agreed:
            from intake import consent
            consent.record("setup", home=self.home)
            self.say(f"Wrote {consent.path(self.home)}")

    def run_consent_only(self) -> int:
        """`intake setup --consent`: only the permission question."""
        self.say(f"{config.PROFILE.title} setup. Settings go in {self.home}")
        agreed = self.ask_consent()
        if agreed:
            self.home.mkdir(parents=True, exist_ok=True)
            self.write_consent(agreed)
            self.say("You can record now:  intake record")
        return 1 if agreed is False else 0

    def offer_login(self) -> None:
        if not self.allow_login:
            return
        self.say("")
        if self.token_file.exists():
            self.say("Google Drive is already authorized.")
            return
        try:
            answer = self.ask("Authorize Google Drive now? It opens a browser. [Y/n] ")
        except EOFError:
            answer = "n"
        if not _yes(answer, default=True):
            self.say("Skipped. Run `intake login` before the first recording.")
            return
        from intake import upload
        try:
            upload.get_credentials(interactive=True)
            self.say("Drive authorized.")
        except Exception as exc:  # the flow reports its own detail
            self.say(f"Drive authorization did not finish: {exc}")
            self.say("Run `intake login` to try again.")

    # --- the run ------------------------------------------------------------

    def run(self) -> int:
        self.say(f"{config.PROFILE.title} setup. Settings go in {self.home}")
        if self.env_file.exists():
            self.say("Existing settings found; Enter keeps any value shown in brackets.")

        if config.legacy_files() and self.home == config.HOME_DIR:
            from intake import cli
            self.say("")
            cli.offer_migration(ask=self.ask, say=self.say)

        values = read_env(self.env_file)
        self.ask_keys(values)
        self.ask_microphone(values)
        meetings, tolerance = self.ask_schedule()
        notion_skipped = self.ask_notion(values)
        agreed = self.ask_consent()

        self.home.mkdir(parents=True, exist_ok=True)
        for sub in ("inbox", "processed", ".work"):
            (self.home / sub).mkdir(exist_ok=True)
        write_env(self.env_file, values, notion_skipped)
        if meetings:
            config.write_schedule(meetings, tolerance, path=self.schedule_file)
        elif not self.schedule_file.exists():
            self.say("No schedule written. Recordings cannot be filed until you add one.")

        self.say("")
        self.say(f"Wrote {self.env_file}")
        if meetings:
            self.say(f"Wrote {self.schedule_file} ({len(meetings)} class meetings)")
        self.write_consent(agreed)

        self.offer_login()

        self.say("")
        self.say("Next:  intake doctor    to confirm everything is in place")
        self.say("       intake record    to record a lecture")
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="intake setup",
        description="Interactive first run: API keys, microphone, class schedule.",
    )
    parser.add_argument("--no-login", action="store_true",
                        help="do not offer to authorize Google Drive at the end")
    parser.add_argument("--consent", action="store_true",
                        help="only confirm that you have permission to record")
    args = parser.parse_args(argv)
    try:
        wizard = Wizard(allow_login=not args.no_login)
        return wizard.run_consent_only() if args.consent else wizard.run()
    except KeyboardInterrupt:
        print("\nsetup canceled; nothing was changed unless a 'Wrote' line "
              "appeared above", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
