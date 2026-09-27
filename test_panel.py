"""Runs the panel's own page scripts against a real DOM.

Every other suite here tests Python. These two pages are most of what the
user actually touches, and nothing in this repo could execute a line of
them: five of the defects a 2026-09-16 audit found lived entirely in
template JavaScript and went straight through a fully green suite, because
the only thing testing them was substring assertions on the served HTML.

This renders the pages through Flask, exactly as a browser receives them,
then hands them to node, which loads them into jsdom and runs their own
scripts. The assertions live in tests/panel/panel.test.mjs. From the
project root:

    .venv/bin/python test_panel.py

It needs node and one pinned package:

    npm ci --prefix tests/panel

Without those it says so and stops, rather than passing quietly. CI sets
SYLLABUS_REQUIRE_JS_TESTS=1, which turns a missing toolchain into a failure
rather than a skip: a harness that skips itself in CI is the thing it was
built to replace.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from _test_home import fresh_home  # noqa: E402

HOME = fresh_home()  # before config is imported, so nothing touches ~/.intake

from intake import gui  # noqa: E402

PANEL = HERE / "tests" / "panel"
REQUIRED = os.environ.get("SYLLABUS_REQUIRE_JS_TESTS") == "1"

# The device path the account service publishes this Mac under. A relayed
# page has to write every request beneath it, which is a class of bug the
# served-HTML assertions could not see at all.
RELAY_BASE = "/p/3XwJMPZ3_2RREYbt"


def toolchain_problem() -> str:
    """Why these tests cannot run, or "" if they can."""
    if shutil.which("node") is None:
        return "node is not on PATH"
    if not (PANEL / "node_modules" / "jsdom").is_dir():
        return f"jsdom is not installed; run:  npm ci --prefix {PANEL.relative_to(HERE)}"
    return ""


def render(client, path: str, base: str = "") -> str:
    """One page, as a browser would receive it.

    Through the test client rather than a real socket: no port to bind, no
    readiness to wait on, and the bytes are the same ones a socket would
    carry. `base` fills SCRIPT_NAME, which is where the page's own
    `const BASE = {{ base|tojson }}` comes from.
    """
    # SCRIPT_NAME carries the prefix; PATH_INFO stays the path within the app,
    # which is how the relay presents a request and where request.script_root
    # comes from.
    overrides = {"SCRIPT_NAME": base} if base else {}
    res = client.get(path, environ_overrides=overrides)
    assert res.status_code == 200, f"{path} rendered {res.status_code}"
    return res.get_data(as_text=True)


def capture(directory: Path) -> None:
    """Write the rendered pages, and real payloads for the page to be fed.

    The fixtures start as what the live routes actually returned, and each
    test overrides only the fields it cares about. Invented fixtures drift
    away from the routes they stand for; these cannot, because they are
    regenerated from the routes on every run.
    """
    client = gui.app.test_client()
    pages = {
        "index.html": render(client, "/"),
        "setup.html": render(client, "/setup"),
        "index.relayed.html": render(client, "/", RELAY_BASE),
        "setup.relayed.html": render(client, "/setup", RELAY_BASE),
    }
    for name, html in pages.items():
        (directory / name).write_text(html)

    routes = {}
    for path in ("/api/status", "/api/setup", "/api/doctor", "/api/account",
                 "/api/login-item", "/api/allowance", "/api/assistant",
                 "/api/calendar"):
        res = client.get(path)
        if res.status_code == 200 and res.is_json:
            routes[path] = res.get_json()
    (directory / "routes.json").write_text(json.dumps(routes, indent=1))


def main() -> int:
    problem = toolchain_problem()
    if problem:
        if REQUIRED:
            print(f"FAIL  the panel's page tests could not run: {problem}")
            print("      CI must never skip these; that is what they replace.")
            return 1
        print(f"SKIP  the panel's page tests: {problem}")
        print("      These are the only tests that execute the page scripts.")
        return 0

    directory = Path(tempfile.mkdtemp(prefix="panel-tests-"))
    try:
        capture(directory)
        result = subprocess.run(
            ["node", str(PANEL / "panel.test.mjs"), str(directory)],
            cwd=str(PANEL),
        )
        return result.returncode
    finally:
        shutil.rmtree(directory, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
