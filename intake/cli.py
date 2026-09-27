"""The `intake` command: one entry point over every module.

    intake setup            first run: keys, microphone, schedule
    intake setup --consent  confirm you have permission to record
    intake doctor           check the install and say how to fix it
    intake record           record from this Mac's microphone
    intake watch            process the inbox until Ctrl-C
    intake watch --once F   process one file and exit
    intake panel            the control panel, and setup, in your browser
    intake app              the same panel in a window of its own
    intake service install  keep the panel running: now, and at every login
    intake service status   is that agent running
    intake login            authorize Google Drive
    intake notion --check   what the Notion integration sees
    intake notion --setup   add the Notion properties it needs
    intake --profile sous   any of the above, against the sous profile

Every subcommand hands its remaining arguments to the module it wraps, so
`intake record --minutes 80` is the same as `python record.py --minutes 80`
from a checkout.

Which profile runs is decided here, before config loads: the --profile flag,
then $INTAKE_PROFILE, then syllabus. The `syllabus` and `sous` commands are
this same entry point with the profile chosen, so `sous record` is
`intake --profile sous record`.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from intake import __version__, profiles

USAGE = __doc__.split("\n\n")[1]

COMMANDS = ("setup", "doctor", "record", "watch", "panel", "app", "service", "login",
            "notion")

# Subcommands that cannot do anything useful without a schedule, so they fail
# up front with one readable line rather than a traceback from deep inside.
NEEDS_SCHEDULE = {"record", "watch", "panel"}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def offer_migration(ask=input, say=log) -> bool:
    """Move an older install's data files into the home directory, if asked.

    Runs when the home directory has no .env yet and an older install still
    has data somewhere this version does not look: ~/.lectureai, the home
    before the rename; the flat ~/.intake from before profiles; or the
    checkout this code lives in. Returns True if files moved.
    """
    from intake import config

    found = config.legacy_files()
    if not found:
        return False

    by_root: dict[Path, list[Path]] = {}
    for path in found:
        by_root.setdefault(config.legacy_root(path), []).append(path)
    for root, paths in by_root.items():
        where = "next to the code in" if root == config.CODE_ROOT else "in"
        say(f"Found data from an older install {where} {root}:")
        for path in paths:
            say(f"  {path.relative_to(root)}")
    say(f"Data now lives in {config.HOME_DIR}.")
    try:
        answer = ask("Move these files there now? [Y/n] ").strip().lower()
    except EOFError:
        answer = "n"
    if answer not in ("", "y", "yes"):
        say("Left them where they are. Run `intake setup` to start fresh, or "
            "move them by hand.")
        return False

    config.ensure_home()
    for path in found:
        relative = path.relative_to(config.legacy_root(path))
        destination = config.HOME_DIR / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            say(f"  kept existing {relative} in the home directory; "
                f"left the old copy in place")
            continue
        shutil.move(str(path), str(destination))
        say(f"  moved {relative}")
    say("Done. Restart any watcher or panel that was already running.")
    return True


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    try:
        flag, args = profiles.extract_flag(args)
        profile = profiles.select(flag)
    except profiles.ProfileError as exc:
        log(f"intake: {exc}")
        return 2
    # Settled before config is imported, so its first load lands in the right
    # home and never creates the other profile's. Left in the environment so
    # anything this process starts (the watcher the panel spawns) inherits it.
    os.environ[profiles.PROFILE_ENV_VAR] = profile.name
    from intake import config
    if config.PROFILE.name != profile.name:
        config.activate(profile)

    if not args or args[0] in ("-h", "--help", "help"):
        print(f"usage: intake [--profile NAME] <command> [options]\n\n{USAGE}\n\n"
              f"Profile: {profile.name} (choose with --profile or "
              f"${profiles.PROFILE_ENV_VAR}; also the syllabus and sous commands).\n"
              f"Data lives in {config.HOME_DIR} (set ${config.HOME_ENV_VAR} to move it).")
        return 0
    if args[0] in ("-V", "--version", "version"):
        print(f"intake {__version__}")
        return 0

    command, rest = args[0], args[1:]
    if command not in COMMANDS:
        log(f"intake: unknown command {command!r}. "
            f"Commands: {', '.join(COMMANDS)}")
        return 2

    # An older install with its data somewhere else: offer the move once,
    # before any command goes looking for keys it will not find. Only when a
    # person is at the keyboard; a watcher started by the panel must not hang
    # on a prompt nobody sees.
    if command != "setup" and sys.stdin.isatty() and config.legacy_files():
        if offer_migration():
            config.reload()

    if command in NEEDS_SCHEDULE and "--list-devices" not in rest \
            and "--no-browser" not in rest and command != "panel" \
            and "-h" not in rest and "--help" not in rest:
        try:
            config.schedule()
        except config.ScheduleError as exc:
            log(f"error: {exc}")
            return 1

    if command == "setup":
        from intake import setup_wizard
        return setup_wizard.main(rest)
    if command == "doctor":
        from intake import doctor
        return doctor.main(rest)
    if command == "record":
        from intake import record
        return record.main(rest)
    if command == "watch":
        from intake import watch
        return watch.main(rest)
    if command == "panel":
        from intake import gui
        return gui.main(rest)
    if command == "app":
        from intake import app
        return app.main(rest)
    if command == "service":
        from intake import service
        return service.main(rest)
    if command == "login":
        from intake import upload
        return upload.main(["--login", *rest])
    if command == "notion":
        from intake import notion_tasks
        return notion_tasks.main(rest)
    return 2  # unreachable


# The two named commands. Each is `intake` with the profile already chosen;
# an explicit --profile on the command line still wins, as it does everywhere.

def syllabus() -> int:
    os.environ[profiles.PROFILE_ENV_VAR] = profiles.SYLLABUS.name
    return main()


def sous() -> int:
    os.environ[profiles.PROFILE_ENV_VAR] = profiles.SOUS.name
    return main()


if __name__ == "__main__":
    raise SystemExit(main())
