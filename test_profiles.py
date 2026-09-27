"""Tests for the two profiles, Syllabus and Sous, and how one is chosen.

Runs against a throwaway root: no network, no keys, no microphone, and
nothing under ~/.intake is read or written. From the project root:

    .venv/bin/python test_profiles.py
"""
import contextlib
import io
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_home import SAMPLE_SCHEDULE, fresh_home  # noqa: E402

HOME = fresh_home()  # before config is imported
ROOT = HOME.parent

from intake import cli, config, doctor, profiles, record, schemas, summarize  # noqa: E402
from intake import consent, setup_wizard  # noqa: E402

# The migration check looks for an older install next to the code, and during
# tests "the code" is this checkout. Point it at an empty fake checkout.
FAKE_CODE_ROOT = Path(tempfile.mkdtemp(prefix="intake-fake-checkout-"))
(FAKE_CODE_ROOT / "pyproject.toml").write_text("[project]\nname = 'x'\n")
config.CODE_ROOT = FAKE_CODE_ROOT


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


results = []

SYL, SOUS = profiles.SYLLABUS, profiles.SOUS

# --- the registry ----------------------------------------------------------

def t1():
    assert set(profiles.PROFILES) == {"syllabus", "sous"}
    assert profiles.DEFAULT_PROFILE is SYL
    assert profiles.get("sous") is SOUS and profiles.get(" Syllabus ") is SYL
    try:
        profiles.get("lectureai")
    except profiles.ProfileError as exc:
        assert "syllabus" in str(exc) and "sous" in str(exc), exc
    else:
        raise AssertionError("an unknown name must be refused")
results.append(run("two profiles exist, syllabus is the default, unknown names are refused", t1))


def t2():
    assert SYL.home_subdir != SOUS.home_subdir
    assert SYL.panel_port != SOUS.panel_port, "both panels must be able to run at once"
    assert SYL.drive_root_folder != SOUS.drive_root_folder
    assert SYL.schedule_filename != SOUS.schedule_filename
    assert SYL.filename_prefix != SOUS.filename_prefix
    assert SYL.notion_target != SOUS.notion_target
    assert SYL.summary_schema is not SOUS.summary_schema
    assert SYL.summary_prompt != SOUS.summary_prompt
results.append(run("sous and syllabus differ in home, port, folder, schedule, prefix, target, and schema", t2))


def t3():
    # Existing summaries were filed under the folder LectureAI created.
    assert SYL.drive_root_folder == "Lecture Notes", SYL.drive_root_folder
    assert SYL.panel_port == 5173, "the panel's port before profiles"
    assert SYL.schedule_filename == "schedule.toml"
    assert SYL.filename_prefix == "lecture" and SYL.fallback_slug == "Lecture-Notes"
    assert SYL.notion_target == "weekly"
    assert SYL.summary_schema is schemas.LectureSummary
    assert SYL.title == "Syllabus" and SOUS.title == "Sous"
results.append(run("syllabus keeps every value LectureAI had, so nothing already filed is orphaned", t3))


# --- the layout ------------------------------------------------------------

def t4():
    syl = config.paths_for(SYL, ROOT)
    sous = config.paths_for(SOUS, ROOT)
    assert syl["HOME_DIR"] == ROOT / "syllabus" and sous["HOME_DIR"] == ROOT / "sous"
    assert set(syl) == set(sous), "both profiles have the same set of paths"
    for name in syl:
        assert syl[name] != sous[name], f"{name} is shared between profiles"
        assert syl["HOME_DIR"] in syl[name].parents or syl[name] == syl["HOME_DIR"], name
        assert sous["HOME_DIR"] in sous[name].parents or sous[name] == sous["HOME_DIR"], name
    for name in ("ENV_FILE", "INBOX_DIR", "PROCESSED_DIR", "TOKEN_FILE", "LOG_FILE"):
        assert name in syl, f"{name} must be a profile path"
    assert syl["SCHEDULE_FILE"].name == "schedule.toml"
    assert sous["SCHEDULE_FILE"].name == SOUS.schedule_filename
results.append(run("every data path of each profile sits inside its own home, and none is shared", t4))


def t5():
    assert config.PROFILE is SYL, config.PROFILE
    assert config.ROOT_DIR == ROOT.resolve() and config.HOME_DIR == HOME.resolve()
    assert config.HOME_DIR == config.ROOT_DIR / "syllabus"
    assert config.resolve_home({"INTAKE_HOME": str(ROOT)}) == ROOT.resolve(), \
        "resolve_home is the root; the profile folder is added on top"
    assert config.DRIVE_ROOT_FOLDER_NAME == "Lecture Notes"
    assert config.NOTION_TARGET == "weekly"
    assert os.environ.get("INTAKE_PROFILE") == "syllabus", \
        "the profile is left in the environment for the processes the panel starts"
results.append(run("with nothing chosen, config is the syllabus profile under <root>/syllabus", t5))


# --- selection -------------------------------------------------------------

def t6():
    assert profiles.select(None, {}) is SYL
    assert profiles.select(None, {"INTAKE_PROFILE": "sous"}) is SOUS
    assert profiles.select("syllabus", {"INTAKE_PROFILE": "sous"}) is SYL, "the flag wins"
    assert profiles.select("", {"INTAKE_PROFILE": "sous"}) is SOUS, "an empty flag is no flag"
    assert profiles.select(None, {"INTAKE_PROFILE": "  "}) is SYL
    for bad in ({"INTAKE_PROFILE": "nope"},):
        try:
            profiles.select(None, bad)
        except profiles.ProfileError:
            pass
        else:
            raise AssertionError(f"{bad} should have been refused")
results.append(run("selection order is --profile, then $INTAKE_PROFILE, then syllabus", t6))


def t7():
    assert profiles.extract_flag(["doctor"]) == (None, ["doctor"])
    assert profiles.extract_flag(["--profile", "sous", "doctor"]) == ("sous", ["doctor"])
    assert profiles.extract_flag(["doctor", "--profile=sous"]) == ("sous", ["doctor"])
    assert profiles.extract_flag(["record", "--profile", "sous", "--minutes", "5"]) == \
        ("sous", ["record", "--minutes", "5"])
    try:
        profiles.extract_flag(["doctor", "--profile"])
    except profiles.ProfileError as exc:
        assert "needs a name" in str(exc), exc
    else:
        raise AssertionError("a bare --profile should be refused")
results.append(run("--profile is accepted anywhere on the line, as --profile X or --profile=X", t7))


def t8():
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        assert cli.main(["--profile", "nope", "doctor"]) == 2
    assert "unknown profile" in err.getvalue() and "Traceback" not in err.getvalue()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert cli.main(["--help"]) == 0
    assert "Profile: syllabus" in out.getvalue() and str(config.HOME_DIR) in out.getvalue()
    assert "--profile" in out.getvalue()
results.append(run("the CLI refuses an unknown profile readably and names the active one in --help", t8))


# --- what the pipeline reads from the profile --------------------------------

def t9():
    when = datetime(2026, 9, 6, 3, 0)  # a Sunday: no class meets
    assert record.output_name(when) == "lecture_2026-09-06_0300.m4a"
    assert summarize.fallback_slug() == "Lecture-Notes"
    assert summarize.slugify_topic("") == "Lecture-Notes"
    assert summarize.user_message("words", "ACCT-4321", "2026-09-06").startswith(
        "Course: ACCT-4321\nDate: 2026-09-06\n\nLecture transcript:\n\nwords")
    assert summarize.SYSTEM_PROMPT == schemas.LECTURE_SYSTEM_PROMPT
    assert summarize.LectureSummary is schemas.LectureSummary
    assert summarize.ACTION_KINDS == {"assignment", "reading", "quiz", "exam", "project", "other"}
    assert set(schemas.CallSummary.ACTION_KINDS) != set(schemas.LectureSummary.ACTION_KINDS)
    for schema in (schemas.LectureSummary, schemas.CallSummary):
        assert set(schema.model_fields) == {"summary_md", "topic_slug", "key_terms", "action_items"}, \
            f"{schema.__name__}: downstream reads these four keys"
results.append(run("under syllabus the names, slug, prompt, and schema are the lecture ones", t9))


def t10():
    # Under syllabus the doctor's checks are the ones the intake rename shipped
    # with, in that order, with the same fix strings.
    names = [c.name for c in doctor.run_checks() if c.name not in ("older install", "older home")]
    assert names == ["Python", "ffmpeg", "home directory", "OpenAI key", "Anthropic key",
                     "class schedule", "Drive OAuth client", "Drive authorization",
                     "Notion", "microphone", "permission to record",
                     "Syllabus account"], names
    home = doctor.check_home()
    assert home.detail == f"{config.HOME_DIR} (default)".replace("(default)", "(from $INTAKE_HOME)"), home
    assert doctor.check_key("OpenAI key", "OPENAI_API_KEY").fix == "intake setup"
    assert doctor.check_drive_token().fix == "intake login"
    assert doctor.check_consent().fix == "intake setup --consent"
    rendered = doctor.render(doctor.run_checks())
    assert "profile" not in rendered.lower(), "doctor's output gained a line it did not have"
results.append(run("syllabus doctor runs the same checks, in the same order, with the same fixes", t10))


# --- switching profiles in one process -------------------------------------

def t11():
    # A syllabus .env with a Notion secret in it must not follow us into sous.
    syl_env = config.ENV_FILE
    syl_env.write_text("OPENAI_API_KEY=syl-openai\nNOTION_TOKEN=syl-notion\n")
    config.reload()
    assert config.OPENAI_API_KEY == "syl-openai" and config.NOTION_TOKEN == "syl-notion"
    saved_home = config.HOME_DIR
    consent.record("setup")
    try:
        config.activate("sous")
        assert config.PROFILE is SOUS
        assert config.HOME_DIR == ROOT.resolve() / "sous" and config.HOME_DIR.is_dir()
        assert config.INBOX_DIR == config.HOME_DIR / "inbox" and config.INBOX_DIR.is_dir()
        assert config.ENV_FILE == config.HOME_DIR / ".env" and not config.ENV_FILE.exists()
        assert config.TOKEN_FILE == config.HOME_DIR / "token.json"
        assert config.SCHEDULE_FILE == config.HOME_DIR / SOUS.schedule_filename
        assert config.OPENAI_API_KEY == "" and config.NOTION_TOKEN == "", \
            "a key from the syllabus .env leaked into sous"
        assert config.DRIVE_ROOT_FOLDER_NAME == SOUS.drive_root_folder
        assert config.NOTION_TARGET == "database"
        assert config.ENV_SETTINGS["NOTION_TARGET"] == "database"
        assert os.environ["INTAKE_PROFILE"] == "sous"
        # The schedule it writes is named its way, headed its way.
        written = config.write_schedule([("Mon", 9, "ACME-0001")])
        assert written == config.SCHEDULE_FILE and written.name == "calls.toml"
        assert written.read_text().startswith("# Sous class schedule")
        # The pipeline follows.
        assert record.output_name(datetime(2026, 9, 6, 3, 0)) == "call_2026-09-06_0300.m4a"
        assert summarize.fallback_slug() == "Call-Notes"
        assert summarize.user_message("w", "ACME", "2026-09-06").startswith(
            "Client: ACME\nDate: 2026-09-06\n\nCall transcript:")
        assert config.PROFILE.summary_schema is schemas.CallSummary
        assert config.PROFILE.summary_prompt == schemas.CALL_SYSTEM_PROMPT
        items = summarize.normalize_actions(
            [{"task": "Send proposal", "due_date": "2026-09-10", "kind": "deliverable"},
             {"task": "Read chapter 7", "due_date": "2026-09-10", "kind": "reading"}],
            "ACME", "2026-09-06")
        assert [i["kind"] for i in items] == ["deliverable", "other"], items
        # Sous never had a flat home, so nothing at the root is offered to it.
        (ROOT / ".env").write_text("OPENAI_API_KEY=flat\n")
        assert config.flat_home() is None and config.legacy_files() == []
        assert setup_wizard.render_env({"OPENAI_API_KEY": "k"}, True).startswith("# Sous settings")
        assert setup_wizard.Wizard(ask=lambda _: "", say=lambda _: None).schedule_file.name == "calls.toml"
        # Permission to record is per profile: syllabus has it, sous does
        # not, and sous is told to fix it under its own name, in its own words.
        assert config.CONSENT_FILE == config.HOME_DIR / "consent.json"
        assert not consent.given(), "syllabus's permission to record leaked into sous"
        assert consent.fix_command() == "intake --profile sous setup --consent"
        assert "calls" in consent.statement() and "lectures" not in consent.statement()
    finally:
        config.activate(SYL)
        (ROOT / ".env").unlink(missing_ok=True)
    assert config.PROFILE is SYL and config.HOME_DIR == saved_home
    assert consent.given() and "lectures" in consent.statement()
    config.CONSENT_FILE.unlink()
    assert config.OPENAI_API_KEY == "syl-openai" and config.NOTION_TOKEN == "syl-notion"
    assert config.NOTION_TARGET == "weekly" and config.DRIVE_ROOT_FOLDER_NAME == "Lecture Notes"
    assert record.output_name(datetime(2026, 9, 6, 3, 0)) == "lecture_2026-09-06_0300.m4a"
    syl_env.unlink()
    config.reload()
results.append(run("activating sous moves every path, default, name, and prompt, and leaks no key back or forth", t11))


def t12():
    # Data from before profiles sits flat in the root. Syllabus is offered it.
    for name, text in ((".env", "OPENAI_API_KEY=flat\n"), ("schedule.toml", SAMPLE_SCHEDULE),
                       ("token.json", "{}"), (".drive_root", "id"), ("pipeline.log", "")):
        (ROOT / name).write_text(text)
    (ROOT / "inbox").mkdir()
    (ROOT / "inbox" / "lecture.m4a").write_bytes(b"x")
    (ROOT / ".work").mkdir(exist_ok=True)
    (ROOT / ".work" / "scratch.m4a").write_bytes(b"x")
    (ROOT / ".watcher.lock").write_text("1")
    assert not config.ENV_FILE.exists(), "the offer only happens before the home has a .env"
    try:
        assert config.flat_home() == ROOT.resolve()
        found = {p.relative_to(ROOT.resolve()).as_posix() for p in config.legacy_files()}
        assert found == {".env", "schedule.toml", "token.json", ".drive_root", "pipeline.log",
                         "inbox/lecture.m4a"}, found
        assert not (HOME / "inbox" / "lecture.m4a").exists()
        waiting = doctor.check_legacy()
        assert waiting is not None and not waiting.ok and str(ROOT.resolve()) in waiting.detail
        assert str(config.HOME_DIR) in waiting.fix

        said = []
        assert cli.offer_migration(ask=lambda _: "", say=said.append) is True
        assert (HOME / ".env").read_text() == "OPENAI_API_KEY=flat\n"
        assert (HOME / "token.json").exists() and (HOME / "inbox" / "lecture.m4a").exists()
        assert (HOME / ".drive_root").read_text() == "id"
        assert not (ROOT / ".env").exists() and not (ROOT / "inbox" / "lecture.m4a").exists()
        assert (ROOT / ".work" / "scratch.m4a").exists(), "scratch stays put"
        assert (ROOT / ".watcher.lock").exists(), "the lock is not data"
        assert config.legacy_files() == [] and doctor.check_legacy() is None
        # The schedule that was already there, seeded by fresh_home, was kept.
        assert any("kept existing schedule.toml" in line for line in said), said
    finally:
        for path in (HOME / ".env", HOME / "token.json", HOME / ".drive_root",
                     HOME / "pipeline.log", HOME / "inbox" / "lecture.m4a",
                     ROOT / "schedule.toml", ROOT / ".watcher.lock"):
            if path.exists():
                path.unlink()
        config.reload()
results.append(run("a flat ~/.intake from before profiles is offered to syllabus, moved, and then quiet", t12))


def t13():
    # The named commands are the plain entry point with the profile chosen.
    saved_argv = sys.argv
    out = io.StringIO()
    try:
        sys.argv = ["sous", "--help"]
        with contextlib.redirect_stdout(out):
            assert cli.sous() == 0
        assert "Profile: sous" in out.getvalue() and str(ROOT.resolve() / "sous") in out.getvalue()
        assert config.PROFILE is SOUS
        out = io.StringIO()
        sys.argv = ["syllabus", "--help"]
        with contextlib.redirect_stdout(out):
            assert cli.syllabus() == 0
        assert "Profile: syllabus" in out.getvalue() and str(HOME.resolve()) in out.getvalue()
        assert config.PROFILE is SYL
        # An explicit flag still beats the command's own choice.
        out = io.StringIO()
        sys.argv = ["sous", "--profile", "syllabus", "--help"]
        with contextlib.redirect_stdout(out):
            assert cli.sous() == 0
        assert "Profile: syllabus" in out.getvalue()
    finally:
        sys.argv = saved_argv
        config.activate(SYL)
results.append(run("the sous and syllabus commands run intake with that profile, and --profile still wins", t13))


# --- action items: the two halves of the undated-assignment contract -------
#
# A lecture's only assignment was silently dropped in 2 of 8 runs because the
# prompt said "if the instructor never mentioned a deadline, return an empty
# list", conflating an undated assignment with no assignment. Sonnet 5 takes
# no temperature (sampling parameters return a 400), so an instruction the
# model can read two ways is re-decided on every run. These guard both halves:
# the prompt must not reintroduce the ambiguity, and the pipeline must keep an
# item that arrives without a date.

def t14():
    import re
    for prompt in (schemas.LECTURE_SYSTEM_PROMPT, schemas.CALL_SYSTEM_PROMPT):
        for sentence in re.split(r"(?<=[.:])\s+", prompt):
            low = sentence.lower()
            assert not ("deadline" in low and "empty list" in low), (
                "an action item with no deadline is still an action item; this "
                f"sentence makes a missing date a reason to drop it: {sentence!r}"
            )
results.append(run("no prompt makes a missing deadline a reason to return nothing", t14))


def t15():
    # An undated assignment survives and is dated to the next class meeting,
    # marked as an assumption rather than presented as something that was said.
    config.set_schedule({("Tue", 14): "ACCT-4321", ("Thu", 14): "ACCT-4321"})
    try:
        items = summarize.normalize_actions(
            [{"task": "Complete the individual project in Connect", "detail": "",
              "due_date": "", "kind": "project"}],
            "ACCT-4321", "2026-09-01")
        assert len(items) == 1, f"an undated assignment must not be dropped: {items}"
        assert items[0]["due_date"], "an undated item gets the next meeting"
        assert items[0]["date_source"] == "assumed", items[0]
        # A date the instructor actually gave is kept and marked as stated.
        stated = summarize.normalize_actions(
            [{"task": "Read chapter 3", "due_date": "2026-09-10", "kind": "reading"}],
            "ACCT-4321", "2026-09-01")
        assert stated[0]["due_date"] == "2026-09-10", stated
        assert stated[0]["date_source"] == "stated", stated
    finally:
        config.set_schedule(None)
results.append(run("an assignment with no date is kept and dated as an assumption", t15))


print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
