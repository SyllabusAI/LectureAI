"""What Syllabus.app runs.

Double-clicked, the app has no arguments and opens the window (intake.app).
Given arguments it is the `syllabus` command, so the one binary inside the
bundle can also be the watcher and the recorder the panel starts, and a
Terminal can run `Syllabus.app/Contents/MacOS/Syllabus doctor`.

The profile is fixed to Syllabus; --profile on the command line still wins,
as it does everywhere.

`Syllabus --self-test [arm64|x86_64]` is for the release workflow, not for
people: inside the bundle it imports every compiled module the build
collected and the modules the app is made of, runs the bundled ffmpeg and
ffprobe, and exits non-zero if anything fails or if the process is not
running as the named architecture. That is how the workflow shows each slice
of the universal app works on its own kind of Mac.
"""

from __future__ import annotations

import os
import sys

SELF_TEST = "--self-test"

# What the app is made of, imported by name on top of every compiled module
# found in the bundle: the window, the panel, the watcher, the API clients,
# and the Drive client.
SELF_TEST_IMPORTS = (
    "intake.app", "intake.gui", "intake.cli", "intake.watch", "intake.record",
    "intake.upload", "intake.calendars", "webview", "AppKit", "WebKit",
    "watchdog.observers", "openai", "anthropic", "pydantic",
    "googleapiclient.discovery", "google_auth_oauthlib.flow", "ssl",
    "cryptography.hazmat.primitives.asymmetric.ed25519",
)


def compiled_modules(root: str) -> list[str]:
    """Dotted names of the extension modules (.so) under a bundle's root."""
    names = set()
    for folder, _dirs, files in os.walk(root):
        for f in files:
            if not f.endswith(".so"):
                continue
            rel = os.path.relpath(os.path.join(folder, f), root)
            parts = rel.split(os.sep)
            # The standard library's own, which PyInstaller keeps apart.
            if "lib-dynload" in parts:
                parts = parts[parts.index("lib-dynload") + 1:]
            parts[-1] = parts[-1].split(".", 1)[0]
            names.add(".".join(parts))
    return sorted(names)


def self_test(args: list[str]) -> int:
    import importlib
    import platform
    import subprocess

    machine = platform.machine()
    print(f"self-test: {sys.executable} running as {machine}, Python {platform.python_version()}")
    failed = []
    if args and args[0] != machine:
        failed.append(f"expected to run as {args[0]}, running as {machine}")
    root = getattr(sys, "_MEIPASS", None)
    if root is None:
        failed.append("not inside a built app (no sys._MEIPASS)")
    modules = compiled_modules(root) if root else []
    for name in [*modules, *SELF_TEST_IMPORTS]:
        try:
            importlib.import_module(name)
        except Exception as exc:  # every failure is reported, not only the first
            failed.append(f"import {name}: {type(exc).__name__}: {exc}")
    print(f"self-test: imported {len(modules)} compiled modules and {len(SELF_TEST_IMPORTS)} named ones")
    from intake import tools
    for tool in ("ffmpeg", "ffprobe"):
        path = tools.find(tool)
        bundled = tools.bundle_dir()
        if not path or bundled is None or os.path.dirname(path) != str(bundled):
            failed.append(f"{tool}: not the bundled copy ({path})")
            continue
        run = subprocess.run([path, "-hide_banner", "-version"], capture_output=True, text=True)
        if run.returncode != 0:
            failed.append(f"{tool} -version exited {run.returncode}: {run.stderr.strip()}")
        else:
            print(f"self-test: {run.stdout.splitlines()[0]}")
    for line in failed:
        print(f"self-test FAILED: {line}", file=sys.stderr)
    if not failed:
        print("self-test: ok")
    return 1 if failed else 0


def main(argv: list[str] | None = None, run_cli=None, run_app=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == [SELF_TEST]:
        return self_test(args[1:])
    from intake import profiles
    os.environ[profiles.PROFILE_ENV_VAR] = profiles.SYLLABUS.name
    if args:
        if run_cli is None:
            from intake import cli
            run_cli = cli.main
        return run_cli(args)
    if run_app is None:
        from intake import app
        run_app = app.main
    return run_app([])


if __name__ == "__main__":
    raise SystemExit(main())
