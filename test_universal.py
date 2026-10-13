"""Tests for packaging/universal.py, which joins the two builds of the app.

The rules that decide what may differ between the Apple silicon and the Intel
build run anywhere. The join itself needs lipo, codesign, and clang, so on a
Mac it builds two tiny bundles (an executable, a library, a Python-style
extension module, data, a symlink, a wheel's metadata, a zip in another
order), joins them, and checks the result; elsewhere those tests say they
were skipped. No network, no PyInstaller build. From the project root:

    .venv/bin/python test_universal.py
"""
import io
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "packaging"))

import universal  # noqa: E402  packaging/universal.py

ON_MAC = sys.platform == "darwin" and shutil.which("lipo") and shutil.which("clang")

# The stub executables carry no packed Python, so there is nothing for the
# archive comparison to read; it is tested on a real build by the workflow.
universal.archive_differences = lambda arm, x86: []


def run(label, fn):
    try:
        fn()
    except AssertionError as exc:
        print(f"FAIL  {label}\n      {exc}")
        return False
    except Exception as exc:
        print(f"FAIL  {label}\n      unexpected {type(exc).__name__}: {exc}")
        return False
    print(f"ok    {label}")
    return True


def test_macho_is_told_by_its_first_bytes():
    d = Path(tempfile.mkdtemp())
    cases = {
        "thin64": (b"\xcf\xfa\xed\xfe" + b"\0" * 28, True),
        "fat": (b"\xca\xfe\xba\xbe\x00\x00\x00\x02" + b"\0" * 24, True),
        "java": (b"\xca\xfe\xba\xbe\x00\x00\x00\x34" + b"\0" * 24, False),
        "text": (b"#!/bin/sh\necho hi\n", False),
        "empty": (b"", False),
    }
    for name, (data, want) in cases.items():
        (d / name).write_bytes(data)
        assert universal.is_macho(d / name) is want, name
    os.symlink("thin64", d / "link")
    assert universal.is_macho(d / "link") is False, "a symlink is not followed"


def test_tree_differences_are_all_listed():
    a = {"Contents": "dir", "Contents/a": "file", "Contents/l": "link:a", "Contents/x": "file"}
    b = {"Contents": "dir", "Contents/a": "dir", "Contents/l": "link:b", "Contents/y": "file"}
    problems = universal.compare_trees(a, b)
    assert len(problems) == 4, problems
    assert any("only in the arm64 build: Contents/x" in p for p in problems)
    assert any("only in the x86_64 build: Contents/y" in p for p in problems)


def test_what_may_differ():
    allowed = universal.ALLOWED_TO_DIFFER.search
    assert allowed("Contents/Frameworks/pydantic_core-2.50.1.dist-info/WHEEL")
    assert allowed("Contents/Frameworks/pydantic_core-2.50.1.dist-info/RECORD")
    assert allowed("Contents/_CodeSignature/CodeResources")
    assert not allowed("Contents/Frameworks/pydantic_core-2.50.1.dist-info/METADATA")
    assert not allowed("Contents/Resources/base_library.zip")
    assert not allowed("Contents/Info.plist")
    module = universal.MODULE_ALLOWED_TO_DIFFER.search
    assert universal.NOTES_JOINED.search("Contents/Resources/ffmpeg/BUILD.txt")
    assert not universal.NOTES_JOINED.search("Contents/Resources/ffmpeg/LICENSE.md")
    assert module("_sysconfigdata__darwin_darwin")
    assert not module("sysconfig") and not module("intake.cli")


def _zip(path, members):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in members:
            z.writestr(name, data)


def test_zips_are_compared_by_member():
    d = Path(tempfile.mkdtemp())
    _zip(d / "a.zip", [("codecs.pyc", b"1"), ("re/_parser.pyc", b"2")])
    _zip(d / "b.zip", [("re/_parser.pyc", b"2"), ("codecs.pyc", b"1")])
    assert universal.zip_differences(d / "a.zip", d / "b.zip") == [], "order alone is no difference"
    _zip(d / "c.zip", [("codecs.pyc", b"1"), ("re/_parser.pyc", b"3")])
    assert universal.zip_differences(d / "a.zip", d / "c.zip") == ["re/_parser.pyc"]
    (d / "plain").write_bytes(b"not a zip")
    assert universal.zip_differences(d / "a.zip", d / "plain") is None


# The join, on a Mac.

def _cc(arch, out, kind, src, minos="13.0"):
    flags = {"exe": [], "dylib": ["-dynamiclib"], "bundle": ["-bundle"]}[kind]
    # Built and signed outside the bundle: codesign takes a main executable
    # inside a half-made bundle for the bundle itself.
    tmp = Path(tempfile.mkdtemp()) / out.name
    subprocess.run(["clang", "-arch", arch, f"-mmacosx-version-min={minos}", *flags,
                    "-x", "c", "-", "-o", str(tmp)], input=src.encode(), check=True)
    # The Intel linker does not sign; a build PyInstaller makes always is.
    subprocess.run(["codesign", "--force", "--sign", "-", str(tmp)], check=True,
                   capture_output=True)
    shutil.move(tmp, out)


def fake_app(root: Path, arch: str, *, minos="13.0") -> Path:
    """A bundle laid out the way PyInstaller lays one out: code in Frameworks,
    everything else in Resources, symlinks between the two."""
    app = root / arch / "Syllabus.app"
    c = app / "Contents"
    for sub in ("MacOS", "Frameworks/pkg", "Resources/pkg", "Resources/pkg-1.0.dist-info"):
        (c / sub).mkdir(parents=True)
    (c / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleExecutable": "Syllabus", "CFBundleIdentifier": "com.example.test",
        "LSMinimumSystemVersion": "13.0"}))
    _cc(arch, c / "MacOS" / "Syllabus", "exe", "int main(void){return 0;}")
    _cc(arch, c / "Frameworks" / "libthing.dylib", "dylib", "int thing(void){return 1;}", minos)
    _cc(arch, c / "Frameworks" / "pkg" / "_ext.cpython-312-darwin.so", "bundle",
        "int ext(void){return 2;}")
    (c / "Resources" / "pkg" / "data.json").write_text("{}")
    os.symlink("../../Resources/pkg/data.json", c / "Frameworks" / "pkg" / "data.json")
    (c / "Resources" / "pkg-1.0.dist-info" / "WHEEL").write_text(
        f"Tag: cp312-cp312-macosx_11_0_{arch}\n")
    os.symlink("../Resources/pkg-1.0.dist-info", c / "Frameworks" / "pkg-1.0.dist-info")
    (c / "Resources" / "ffmpeg").mkdir()
    (c / "Resources" / "ffmpeg" / "BUILD.txt").write_text(f"ffmpeg for {arch}\n")
    members = [("a.pyc", b"a"), ("b.pyc", b"b")]
    _zip(c / "Resources" / "base_library.zip", members if arch == "arm64" else members[::-1])
    os.symlink("../Resources/base_library.zip", c / "Frameworks" / "base_library.zip")
    return app


def quiet(fn, *args):
    out, err = io.StringIO(), io.StringIO()
    old = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        return fn(*args), out.getvalue() + err.getvalue()
    finally:
        sys.stdout, sys.stderr = old


def test_join_makes_every_macho_universal():
    if not ON_MAC:
        print("      (needs a Mac with lipo and clang; skipped)")
        return
    d = Path(tempfile.mkdtemp())
    arm, x86 = fake_app(d, "arm64"), fake_app(d, "x86_64")
    out = d / "out" / "Syllabus.app"
    code, log = quiet(universal.merge, arm, x86, out)
    assert code == 0, log
    c = out / "Contents"
    for rel in ("MacOS/Syllabus", "Frameworks/libthing.dylib",
                "Frameworks/pkg/_ext.cpython-312-darwin.so"):
        assert sorted(universal.archs(c / rel)) == ["arm64", "x86_64"], rel
    assert os.readlink(c / "Frameworks" / "base_library.zip") == "../Resources/base_library.zip"
    assert (c / "Resources/pkg-1.0.dist-info/WHEEL").read_text().endswith("arm64\n")
    notes = (c / "Resources/ffmpeg/BUILD.txt").read_text()
    assert "ffmpeg for arm64" in notes and "ffmpeg for x86_64" in notes, notes
    assert os.access(c / "MacOS" / "Syllabus", os.X_OK)
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(out)], check=True)
    # This Mac's own slice runs.
    subprocess.run([str(c / "MacOS" / "Syllabus")], check=True)
    assert quiet(universal.check, out)[0] == 0


def test_join_refuses_a_file_on_one_side():
    if not ON_MAC:
        print("      (needs a Mac with lipo and clang; skipped)")
        return
    d = Path(tempfile.mkdtemp())
    arm, x86 = fake_app(d, "arm64"), fake_app(d, "x86_64")
    (x86 / "Contents" / "Resources" / "extra.txt").write_text("x")
    out = d / "out" / "Syllabus.app"
    code, log = quiet(universal.merge, arm, x86, out)
    assert code == 1 and "only in the x86_64 build: Contents/Resources/extra.txt" in log, log
    assert not out.exists(), "nothing is written when the lists differ"


def test_join_refuses_different_data():
    if not ON_MAC:
        print("      (needs a Mac with lipo and clang; skipped)")
        return
    d = Path(tempfile.mkdtemp())
    arm, x86 = fake_app(d, "arm64"), fake_app(d, "x86_64")
    (x86 / "Contents" / "Resources" / "pkg" / "data.json").write_text('{"v": 2}')
    _zip(x86 / "Contents" / "Resources" / "base_library.zip", [("a.pyc", b"A"), ("b.pyc", b"b")])
    out = d / "out" / "Syllabus.app"
    code, log = quiet(universal.merge, arm, x86, out)
    assert code == 1, log
    assert "Contents/Resources/pkg/data.json" in log, log
    assert "base_library.zip: a.pyc" in log, log
    assert not out.exists()


def test_join_refuses_a_missing_slice():
    if not ON_MAC:
        print("      (needs a Mac with lipo and clang; skipped)")
        return
    d = Path(tempfile.mkdtemp())
    arm, x86 = fake_app(d, "arm64"), fake_app(d, "x86_64")
    # The Intel build's library came out arm64.
    shutil.copy(arm / "Contents/Frameworks/libthing.dylib", x86 / "Contents/Frameworks/libthing.dylib")
    code, log = quiet(universal.merge, arm, x86, d / "out" / "Syllabus.app")
    assert code == 1 and "no x86_64 slice in the x86_64 build: Contents/Frameworks/libthing.dylib" in log, log
    # And two Apple silicon builds are not a universal app.
    code, log = quiet(universal.merge, arm, arm, d / "out" / "Syllabus.app")
    assert code == 1 and "expected x86_64" in log, log


def test_check_refuses_thin_or_too_new():
    if not ON_MAC:
        print("      (needs a Mac with lipo and clang; skipped)")
        return
    d = Path(tempfile.mkdtemp())
    arm = fake_app(d, "arm64")
    code, log = quiet(universal.check, arm)
    assert code == 1 and "MacOS/Syllabus: arm64 (no x86_64)" in log, log
    # A library built for a newer macOS than the app promises.
    x86 = fake_app(d, "x86_64", minos="14.0")
    out = d / "out" / "Syllabus.app"
    code, log = quiet(universal.merge, arm, x86, out)
    assert code == 1, log
    assert "libthing.dylib: the x86_64 slice needs macOS 14.0, the app promises 13.0" in log, log


if __name__ == "__main__":
    results = [
        run("Mach-O files are told by their first bytes, not their names",
            test_macho_is_told_by_its_first_bytes),
        run("every file, folder, and symlink that differs is listed",
            test_tree_differences_are_all_listed),
        run("only wheel tags, hash lists, and seals may differ", test_what_may_differ),
        run("zips are compared member by member, not byte by byte",
            test_zips_are_compared_by_member),
        run("the join gives every Mach-O both slices and keeps the rest",
            test_join_makes_every_macho_universal),
        run("a file in one build only stops the join", test_join_refuses_a_file_on_one_side),
        run("data that differs stops the join", test_join_refuses_different_data),
        run("a Mach-O without its side's slice stops the join", test_join_refuses_a_missing_slice),
        run("the check refuses a thin app and a slice newer than the app's macOS",
            test_check_refuses_thin_or_too_new),
    ]
    print(f"\n{sum(results)}/{len(results)} passed")
    sys.exit(0 if all(results) else 1)
