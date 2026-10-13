#!/usr/bin/env python3
"""Pick one version of every package for both halves of the universal app.

    python packaging/pins.py > pins.txt          # on the Intel runner
    pip install --prefer-binary -r pins.txt      # then on both runners
    pip install --no-deps -e .

The release workflow builds Syllabus.app twice, on Apple silicon and on
Intel, and packaging/universal.py joins the two. Both must hold the same
version of every package, or the joined app pairs one version's Python code
with another's compiled module. Resolving separately on each runner does not
guarantee that: some packages publish wheels for one architecture only
(cryptography 49 and later have no Intel wheel for macOS), so the newest
version that installs cleanly differs between the two.

So the resolve happens once, on Intel, the side with fewer wheels, with
--prefer-binary so pip picks the newest version that has a wheel rather than
compiling a newer one from source. Its answer, written as exact pins, is
what both builds install. A pin with no wheel for Apple silicon would build
from source there, and universal.py fails the run if the result differs.
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXTRAS = "app,build"


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "report.json"
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--dry-run", "--quiet",
             "--ignore-installed", "--prefer-binary", "--report", str(report),
             f"{ROOT}[{EXTRAS}]"],
            check=True, stdout=sys.stderr)
        install = json.loads(report.read_text())["install"]
    pins = sorted(
        f"{item['metadata']['name']}=={item['metadata']['version']}"
        for item in install
        if item["metadata"]["name"].lower() != "intake"
    )
    print(f"# resolved by packaging/pins.py with Python {sys.version.split()[0]} "
          f"on {platform.machine()}")
    print("\n".join(pins))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
