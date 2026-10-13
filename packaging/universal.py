#!/usr/bin/env python3
"""Join an Apple silicon and an Intel build of Syllabus.app into one app.

    python packaging/universal.py merge <arm64.app> <x86_64.app> <out.app>
    python packaging/universal.py check <app>

Why two builds and lipo, not PyInstaller's target_arch="universal2": that
needs a universal2 Python and a universal2 wheel of every compiled
dependency, and several of ours (pydantic-core, jiter, markupsafe, cffi) ship
only separate arm64 and x86_64 wheels, while recent cryptography ships no
Intel wheel at all. Two native builds, each by a Python of its own
architecture, each one exactly what PyInstaller makes and tests on that kind
of Mac, joined file by file, have no such gaps.

`merge` walks both bundles side by side and refuses, listing every problem
rather than the first, when:
  - a file, folder, or symlink is in one bundle and not the other, or a
    symlink points somewhere different;
  - a Mach-O file (executable, .so, .dylib, framework binary) is Mach-O on one
    side only, or lacks that side's architecture;
  - any other file differs, except a wheel's own tag and hash list
    (*.dist-info/WHEEL and RECORD), which name the architecture by design,
    and a code seal (_CodeSignature/CodeResources), which is made again; a
    zip file (base_library.zip) is compared member by member, and ffmpeg's
    BUILD.txt, if each side built its own ffmpeg, keeps both sides' notes;
  - the Python code PyInstaller packed into the two main executables differs.
Every Mach-O becomes one file with both slices (`lipo -create`, arm64 from
the Apple silicon build and x86_64 from the Intel one, even where a wheel
was universal already); everything else is copied from the arm64 side. The
result is sealed ad hoc, as PyInstaller seals a build, ready for
packaging/sparkle.sh and packaging/sign.sh.

`check` is what the release workflow asks of the final app: every Mach-O in
it has both an arm64 and an x86_64 slice, and no slice needs a newer macOS
than LSMinimumSystemVersion in its Info.plist.
"""

from __future__ import annotations

import filecmp
import os
import plistlib
import re
import shutil
import struct
import subprocess
import sys
import zipfile
from pathlib import Path

ARCHS = ("arm64", "x86_64")
MAIN_EXECUTABLE = "Contents/MacOS/Syllabus"

# Files that are allowed to differ between the two builds, and why: a wheel's
# WHEEL file names its platform tag and its RECORD lists the hashes of its
# compiled files, so both differ for every compiled package by design. A
# code signature's CodeResources lists the hashes of the files it seals, and
# every seal is made again after the join.
ALLOWED_TO_DIFFER = re.compile(r"(\.dist-info/(WHEEL|RECORD)|/_CodeSignature/CodeResources)$")

# Build notes that may differ, and are kept from both sides, one after the
# other: ffmpeg's BUILD.txt, when each build compiled its own ffmpeg (a run
# that publishes nothing, before the universal ffmpeg release is pinned; the
# pinned tarball is the same file on both sides).
NOTES_JOINED = re.compile(r"/ffmpeg/BUILD\.txt$")

# Python modules (packed in the main executable or in a zip) that may differ:
# the build configuration module records the architecture the interpreter
# was compiled for. The other slice's copy is unused at run time.
MODULE_ALLOWED_TO_DIFFER = re.compile(r"(^|/)_sysconfigdata_")

_THIN_MAGIC = {b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce"}
_FAT_MAGIC = {b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"}


def is_macho(path: Path) -> bool:
    """True for a thin or fat Mach-O file, by its first bytes."""
    if path.is_symlink() or not path.is_file():
        return False
    with open(path, "rb") as fh:
        head = fh.read(8)
    if head[:4] in _THIN_MAGIC:
        return True
    if head[:4] in _FAT_MAGIC and len(head) == 8:
        # A Java class file starts with the same four bytes; its next field
        # is a version number of 45 or more, never a small slice count.
        return 0 < struct.unpack(">I", head[4:])[0] < 32
    return False


def archs(path: Path) -> list[str]:
    out = subprocess.run(["lipo", "-archs", str(path)], capture_output=True, text=True, check=True)
    return out.stdout.split()


def walk(root: Path) -> dict[str, str]:
    """Every entry under root, relative path to its kind: dir, file, or link:<target>."""
    entries = {}
    for folder, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            p = Path(folder) / name
            rel = p.relative_to(root).as_posix()
            if p.is_symlink():
                entries[rel] = "link:" + os.readlink(p)
            elif p.is_dir():
                entries[rel] = "dir"
            else:
                entries[rel] = "file"
    return entries


def compare_trees(a: dict[str, str], b: dict[str, str]) -> list[str]:
    problems = []
    for rel in sorted(a.keys() - b.keys()):
        problems.append(f"only in the arm64 build: {rel}")
    for rel in sorted(b.keys() - a.keys()):
        problems.append(f"only in the x86_64 build: {rel}")
    for rel in sorted(a.keys() & b.keys()):
        if a[rel] != b[rel]:
            problems.append(f"not the same kind of entry: {rel} ({a[rel]} / {b[rel]})")
    return problems


def slice_of(path: Path, arch: str, scratch: Path) -> Path:
    """A thin file holding `arch` from path, which may be thin or fat."""
    have = archs(path)
    if have == [arch]:
        return path
    out = scratch / f"{len(list(scratch.iterdir()))}-{arch}"
    subprocess.run(["lipo", str(path), "-thin", arch, "-output", str(out)], check=True)
    return out


def archive_differences(arm_exe: Path, x86_exe: Path) -> list[str]:
    """What differs between the Python packed into the two main executables.

    Only one copy is ever read: PyInstaller's bootloader looks for its archive
    from the end of the file, so in a universal executable both slices run
    the code packed into the last one. That is safe only if the two agree.
    """
    try:
        from PyInstaller.archive.readers import CArchiveReader
    except ImportError:
        return ["PyInstaller is not installed here, so the packed Python cannot be compared"]

    def contents(exe: Path) -> tuple[dict[str, bytes], list[str]]:
        pkg = CArchiveReader(str(exe))
        out = {}
        for name, entry in pkg.toc.items():
            if entry[-1] == "z":  # the PYZ: every pure Python module
                pyz = pkg.open_embedded_archive(name)
                for mod in pyz.toc:
                    out[f"{name}:{mod}"] = pyz.extract(mod, raw=True)
            else:
                out[name] = pkg.extract(name)
        return out, sorted(pkg.options)

    a, a_opts = contents(arm_exe)
    b, b_opts = contents(x86_exe)
    problems = [f"packed only in the arm64 build: {n}" for n in sorted(a.keys() - b.keys())]
    problems += [f"packed only in the x86_64 build: {n}" for n in sorted(b.keys() - a.keys())]
    for name in sorted(a.keys() & b.keys()):
        if a[name] != b[name] and not MODULE_ALLOWED_TO_DIFFER.search(name.split(":")[-1]):
            problems.append(f"packed differently: {name}")
    if a_opts != b_opts:
        problems.append(f"bootloader options differ: {a_opts} / {b_opts}")
    return problems


def zip_differences(a: Path, b: Path) -> list[str] | None:
    """Members that differ between two zip files, or None if either is not one.

    PyInstaller writes base_library.zip in no fixed order, so two builds of
    the same code can differ byte for byte and still hold identical members.
    """
    if not (zipfile.is_zipfile(a) and zipfile.is_zipfile(b)):
        return None
    with zipfile.ZipFile(a) as za, zipfile.ZipFile(b) as zb:
        na, nb = set(za.namelist()), set(zb.namelist())
        out = [f"{m} only in the arm64 build" for m in sorted(na - nb)]
        out += [f"{m} only in the x86_64 build" for m in sorted(nb - na)]
        out += [m for m in sorted(na & nb)
                if za.read(m) != zb.read(m) and not MODULE_ALLOWED_TO_DIFFER.search(m)]
    return out


def merge(arm: Path, x86: Path, out: Path) -> int:
    for app, arch in ((arm, "arm64"), (x86, "x86_64")):
        exe = app / MAIN_EXECUTABLE
        if not exe.is_file():
            print(f"universal.py: no {MAIN_EXECUTABLE} in {app}", file=sys.stderr)
            return 1
        if archs(exe) != [arch]:
            print(f"universal.py: {exe} is {' '.join(archs(exe))}, expected {arch}", file=sys.stderr)
            return 1

    a, b = walk(arm), walk(x86)
    problems = compare_trees(a, b)
    if problems:
        return report(problems)

    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    scratch = out.parent / (out.name + ".lipo-scratch")
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir()

    joined = copied = 0
    differing = []
    try:
        for rel in sorted(a):  # sorted: a folder comes before what it holds
            src_a, src_b, dst = arm / rel, x86 / rel, out / rel
            kind = a[rel]
            if kind == "dir":
                dst.mkdir()
                continue
            if kind.startswith("link:"):
                os.symlink(kind[len("link:"):], dst)
                continue
            ma, mb = is_macho(src_a), is_macho(src_b)
            if ma or mb:
                if not (ma and mb):
                    problems.append(f"Mach-O on one side only: {rel}")
                    continue
                if "arm64" not in archs(src_a):
                    problems.append(f"no arm64 slice in the arm64 build: {rel} ({' '.join(archs(src_a))})")
                    continue
                if "x86_64" not in archs(src_b):
                    problems.append(f"no x86_64 slice in the x86_64 build: {rel} ({' '.join(archs(src_b))})")
                    continue
                subprocess.run(["lipo", "-create",
                                str(slice_of(src_a, "arm64", scratch)),
                                str(slice_of(src_b, "x86_64", scratch)),
                                "-output", str(dst)], check=True)
                shutil.copymode(src_a, dst)
                joined += 1
                continue
            if not filecmp.cmp(src_a, src_b, shallow=False) and NOTES_JOINED.search(rel):
                dst.write_bytes(b"arm64 slice:\n" + src_a.read_bytes() +
                                b"\nx86_64 slice:\n" + src_b.read_bytes())
                shutil.copymode(src_a, dst)
                differing.append(rel)
                continue
            if not filecmp.cmp(src_a, src_b, shallow=False):
                members = zip_differences(src_a, src_b)
                if ALLOWED_TO_DIFFER.search(rel) or members == []:
                    differing.append(rel)
                elif members:
                    problems += [f"differs between the two builds: {rel}: {m}" for m in members]
                    continue
                else:
                    problems.append(f"differs between the two builds: {rel}")
                    continue
            shutil.copy2(src_a, dst)
            copied += 1
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    problems += archive_differences(arm / MAIN_EXECUTABLE, x86 / MAIN_EXECUTABLE)
    if problems:
        shutil.rmtree(out, ignore_errors=True)
        return report(problems)

    # Sealed ad hoc, the way PyInstaller leaves a build: each Mach-O, then the
    # bundle. packaging/sign.sh replaces all of it for a Developer ID build.
    for rel, kind in sorted(a.items()):
        if kind == "file" and is_macho(out / rel):
            subprocess.run(["codesign", "--force", "--sign", "-", str(out / rel)],
                           check=True, capture_output=True)
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(out)], check=True)
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(out)], check=True)

    print(f"joined {joined} Mach-O files and copied {copied} others into {out}")
    if differing:
        print(f"  {len(differing)} files differ only by design (wheel tags, seals, zip order, build notes); "
              f"kept the arm64 copies: {', '.join(differing)}")
    print(f"  the main executable's slices, in file order: {' '.join(archs(out / MAIN_EXECUTABLE))}")
    return check(out)


def min_macos(path: Path, arch: str) -> str | None:
    """The minimum macOS a slice declares (LC_BUILD_VERSION or LC_VERSION_MIN_MACOSX)."""
    out = subprocess.run(["otool", "-arch", arch, "-l", str(path)],
                         capture_output=True, text=True, check=True).stdout
    m = re.search(r"cmd LC_BUILD_VERSION.*?\n\s*minos (\S+)", out, re.S) or \
        re.search(r"cmd LC_VERSION_MIN_MACOSX.*?\n\s*version (\S+)", out, re.S)
    return m.group(1) if m else None


def version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def check(app: Path) -> int:
    """Every Mach-O in app has both slices, each no newer than the app's minimum macOS."""
    with open(app / "Contents" / "Info.plist", "rb") as fh:
        minimum = plistlib.load(fh)["LSMinimumSystemVersion"]
    problems, count = [], 0
    for rel, kind in sorted(walk(app).items()):
        path = app / rel
        if kind != "file" or not is_macho(path):
            continue
        count += 1
        have = archs(path)
        missing = [a for a in ARCHS if a not in have]
        if missing:
            problems.append(f"{rel}: {' '.join(have)} (no {' '.join(missing)})")
            continue
        for arch in ARCHS:
            needs = min_macos(path, arch)
            if needs and version_tuple(needs) > version_tuple(minimum):
                problems.append(f"{rel}: the {arch} slice needs macOS {needs}, the app promises {minimum}")
    if count == 0:
        problems.append(f"no Mach-O files found in {app}")
    if problems:
        return report(problems)
    print(f"all {count} Mach-O files in {app} have {' and '.join(ARCHS)} slices "
          f"and run on macOS {minimum} or newer")
    return 0


def report(problems: list[str]) -> int:
    for p in problems:
        print(f"universal.py: {p}", file=sys.stderr)
    print(f"universal.py: {len(problems)} problem(s)", file=sys.stderr)
    return 1


def main(argv: list[str]) -> int:
    if len(argv) == 4 and argv[0] == "merge":
        return merge(Path(argv[1]).resolve(), Path(argv[2]).resolve(), Path(argv[3]).resolve())
    if len(argv) == 2 and argv[0] == "check":
        return check(Path(argv[1]).resolve())
    print(__doc__.split("\n\n")[1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
