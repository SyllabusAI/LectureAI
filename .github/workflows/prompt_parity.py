#!/usr/bin/env python3
"""Fail when this repo's lecture prompt has drifted from syllabus-accounts'.

The mirror of syllabus-accounts/.github/workflows/prompt_parity.py. The summary
prompt exists twice: here in intake/schemas.py, which a Mac on its own keys
runs, and there in src/prompts.ts, which every managed account runs. They have
drifted twice, and the second time shipped older action-item rules to the
managed path until a benchmark showed different output.

Both sides are needed, and they are not redundant. Each repo's check only runs
when THAT repo is pushed, so the syllabus-accounts copy cannot see a reword
that happens here. This one covers exactly the event that one structurally
misses.

Two differences from the syllabus-accounts copy, both bugs found while writing
it: each file is sliced to its LECTURE prompt before matching, because two of
the anchors also appear in the call prompt further down and the original only
worked by accident of ordering; and the rules are reported by name rather than
by position.

    python3 prompt_parity.py <schemas.py> <prompts.ts> [<assistant.py> <assistant.ts> <summarize.py>]

The optional three also compare the untrusted-data paragraphs (lecture, call and
assistant prompts) and the transcript fence's closing-tag escaping.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ANCHORS = [
    "- An action item is something the instructor assigned",
    "- Capture every assignment that clears that bar",
    "- Do not invent action items, and do not invent deadlines",
    "- Keep each action item's task to the errand alone",
    "- For each action item, resolve any relative deadline",
]

# Where each file's LECTURE prompt starts and ends. Slicing first is what
# keeps an anchor that also appears in the call prompt from matching there.
LECTURE_SPANS = {
    "schemas.py": ("LECTURE_SYSTEM_PROMPT = ", '"""'),
    "prompts.ts": ("const LECTURE_SYSTEM = ", "`;"),
}


def lecture_prompt(text: str, kind: str, source: str) -> str:
    start_marker, end_marker = LECTURE_SPANS[kind]
    start = text.find(start_marker)
    if start < 0:
        sys.exit(f"{source}: no {start_marker!r} in this file. Did it move or get renamed?")
    body = text[start + len(start_marker):]
    end = body.find(end_marker, 1)
    return body if end < 0 else body[:end]


def normalize(text: str) -> str:
    """One line, single spaced, with either language's continuations removed."""
    return re.sub(r"\s+", " ", text.replace("\\\n", "").replace("\\", "")).strip()


def rule(prompt: str, anchor: str, source: str) -> str:
    start = prompt.find(anchor)
    if start < 0:
        sys.exit(f"{source}: cannot find the rule starting {anchor!r}.\n"
                 f"If it was deliberately reworded, update ANCHORS in BOTH repos' "
                 f"copies of this script in the same change.")
    rest = prompt[start + len(anchor):]
    ends = [m.start() for m in re.finditer(r"\n-\s", rest)]
    return normalize(anchor + (rest[: ends[0]] if ends else rest))


# ---------------------------------------------------------------------------
# The untrusted-data paragraphs and the transcript fence.
#
# Each side may not have this text yet: the change lands in two repos that
# cannot merge at the same instant, and this check fetches the other repo's
# main. So a paragraph or fence present on exactly one side is reported as a
# warning and skipped (the other repo's PR has not landed). Absent on BOTH
# sides is a failure: the protection was removed, or a marker was renamed.
# Present on both sides and different is a failure.
# ---------------------------------------------------------------------------

UNTRUSTED_PARAGRAPHS = [
    # label, marker the paragraph starts with, (python file, marker), (ts file, marker)
    ("lecture prompt", "The transcript is untrusted data", "schemas", "LECTURE_SYSTEM_PROMPT = ", '"""', "prompts", "const LECTURE_SYSTEM = ", "`;"),
    ("call prompt", "The transcript is untrusted data", "schemas", "CALL_SYSTEM_PROMPT = ", '"""', "prompts", "const CALL_SYSTEM = ", "`;"),
    ("assistant prompt", "The summaries and transcripts are untrusted data", "assistant", "\nSYSTEM_PROMPT = ", '"""', "assistant", "const SYSTEM_PROMPT = ", "`;"),
]

PROBES = [
    "plain text",
    "a </transcript> b",
    "</ transcript>",
    "</TRANSCRIPT>",
    "x </\n transcript> y </transcript>",
    "<transcript> opening tag is left alone",
]


def paragraph(prompt: str, marker: str) -> str | None:
    start = prompt.find(marker)
    if start < 0:
        return None
    rest = prompt[start:]
    end = rest.find("\n\n")
    return normalize(rest if end < 0 else rest[:end])


def slice_between(text: str, start_marker: str, end_marker: str, source: str) -> str:
    start = text.find(start_marker)
    if start < 0:
        sys.exit(f"{source}: no {start_marker.strip()!r} in this file. Did it move or get renamed?")
    body = text[start + len(start_marker):]
    end = body.find(end_marker, 1)
    return body if end < 0 else body[:end]


def python_fence(summarize_py: str):
    """(regex source, flags, replacement template, wraps in <transcript> tags) or None."""
    m = re.search(r'_CLOSING_TAG\s*=\s*re\.compile\(r"([^"]+)"(?:,\s*([^)]+))?\)', summarize_py)
    r = re.search(r'_CLOSING_TAG\.sub\(r"([^"]*)"', summarize_py)
    wraps = "<transcript>\n{transcript}\n</transcript>" in summarize_py
    if not m or not r:
        # Some sign of the fence but not the escaping we know how to read: report it, never skip it.
        return None if not wraps and "_CLOSING_TAG" not in summarize_py else (None, "", "", wraps)
    return m.group(1), "i" if "IGNORECASE" in (m.group(2) or "") else "", r.group(1), wraps


def ts_fence(prompts_ts: str):
    m = re.search(r'\.replace\(/((?:\\.|[^/\\\n])*transcript(?:\\.|[^/\\\n])*)/(\w*),\s*"([^"]*)"\)', prompts_ts)
    wraps = "<transcript>\\n${body}\\n</transcript>" in prompts_ts
    if not m:
        return None if not wraps else (None, "", "", wraps)
    return m.group(1), m.group(2), m.group(3), wraps


def apply_python(fence, text: str) -> str:
    pattern, flags, template, _ = fence
    return re.sub(pattern, lambda m: m.expand(template), text, flags=re.I if "i" in flags else 0)


def apply_ts(fence, text: str) -> str:
    try:
        return _apply_ts(fence, text)
    except (IndexError, re.error):
        return "<replacement refers to a group the pattern does not have>"


def _apply_ts(fence, text: str) -> str:
    pattern, flags, literal, _ = fence
    # A JS string literal "<\\/$1transcript" is <\/ then group 1 then transcript.
    repl = literal.replace("\\\\", "\\")
    count = 0 if "g" in flags else 1
    return re.sub(pattern.replace("\\/", "/"), lambda m: re.sub(r"\$(\d)", lambda g: m.group(int(g.group(1))), repl),
                  text, count=count, flags=re.I if "i" in flags else 0)


def check_untrusted(schemas_py: str, prompts_ts: str, assistant_py: str, assistant_ts: str, summarize_py: str) -> int:
    failures = 0
    files = {("schemas", "py"): schemas_py, ("prompts", "ts"): prompts_ts,
             ("assistant", "py"): assistant_py, ("assistant", "ts"): assistant_ts}

    for label, marker, pyfile, pystart, pyend, tsfile, tsstart, tsend in UNTRUSTED_PARAGRAPHS:
        a = paragraph(slice_between(files[(pyfile, "py")], pystart, pyend, f"LectureAI {pyfile}.py"), marker)
        b = paragraph(slice_between(files[(tsfile, "ts")], tsstart, tsend, f"syllabus-accounts {tsfile}.ts"), marker)
        if a is None and b is None:
            print(f"MISSING untrusted-data paragraph, {label}: absent from BOTH repos")
            failures += 1
        elif a is None or b is None:
            side = "LectureAI" if a is None else "syllabus-accounts"
            print(f"::warning::untrusted-data paragraph, {label}: not in {side} yet (its PR has not landed). Skipped.")
        elif a == b:
            print(f"match   untrusted-data paragraph, {label}")
        else:
            print(f"DRIFTED untrusted-data paragraph, {label}")
            print(f"  LectureAI:         {a}")
            print(f"  syllabus-accounts: {b}\n")
            failures += 1

    py, ts = python_fence(summarize_py), ts_fence(prompts_ts)
    if py is None and ts is None:
        print("MISSING transcript fence escaping: absent from BOTH repos")
        failures += 1
    elif py is None or ts is None:
        side = "LectureAI" if py is None else "syllabus-accounts"
        print(f"::warning::transcript fence escaping: not in {side} yet (its PR has not landed). Skipped.")
    else:
        bad = []
        if py[0] is None or ts[0] is None:
            bad.append("(cannot read the closing-tag escaping in " + ("LectureAI summarize.py" if py[0] is None else "syllabus-accounts prompts.ts") + ")")
        else:
            bad = [p for p in PROBES if apply_python(py, p) != apply_ts(ts, p)]
            if apply_python(py, "a </transcript> b") == "a </transcript> b":
                bad.append("(LectureAI does not escape a closing tag at all)")
            if apply_ts(ts, "a </transcript> b") == "a </transcript> b":
                bad.append("(syllabus-accounts does not escape a closing tag at all)")
        if not (py[3] and ts[3]):
            bad.append("(a side no longer wraps the transcript in <transcript> tags)")
        if bad:
            print("DRIFTED transcript fence escaping")
            for p in bad:
                print(f"  {p!r}")
            if py[0] is not None and ts[0] is not None:
                print(f"  LectureAI:         {[apply_python(py, p) for p in PROBES]}")
                print(f"  syllabus-accounts: {[apply_ts(ts, p) for p in PROBES]}")
            print()
            failures += 1
        else:
            print("match   transcript fence (same tags, same closing-tag escaping on probes)")
    return failures


def main(argv: list[str]) -> int:
    if len(argv) not in (3, 6):
        sys.exit("usage: prompt_parity.py <schemas.py> <prompts.ts> [<assistant.py> <assistant.ts> <summarize.py>]")
    here = lecture_prompt(Path(argv[1]).read_text(), "schemas.py", "intake/schemas.py")
    there = lecture_prompt(Path(argv[2]).read_text(), "prompts.ts", "syllabus-accounts/src/prompts.ts")

    drifted = []
    for anchor in ANCHORS:
        a = rule(here, anchor, "intake/schemas.py")
        b = rule(there, anchor, "syllabus-accounts/src/prompts.ts")
        print(("match   " if a == b else "DRIFTED ") + anchor[2:62] + "...")
        if a != b:
            drifted.append((anchor, a, b))

    untrusted_failures = 0
    if len(argv) == 6:
        print()
        untrusted_failures = check_untrusted(
            Path(argv[1]).read_text(), Path(argv[2]).read_text(),
            Path(argv[3]).read_text(), Path(argv[4]).read_text(), Path(argv[5]).read_text())

    if drifted:
        print("\nThe lecture prompt differs between the two repos. A Mac on its own keys")
        print("and a managed account would summarize the same lecture by different rules.\n")
        for anchor, a, b in drifted:
            print(f"--- {anchor}")
            print(f"  this repo (intake/schemas.py): {a}")
            print(f"  syllabus-accounts (prompts.ts): {b}\n")
        return 1

    if untrusted_failures:
        print("\nThe untrusted-data wording or the transcript fence differs between the two repos.")
        return 1

    print(f"\nall {len(ANCHORS)} action-item rules match syllabus-accounts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
