"""Tests for the home directory resolver, the schedule file, and `intake setup`.

Everything runs against throwaway directories with scripted answers: no
network, no keys, no microphone, and nothing under ~/.intake or in this
checkout is read or written. From the project root:

    .venv/bin/python test_setup.py
"""
import os
import stat
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_home import SAMPLE_SCHEDULE, fresh_home  # noqa: E402

HOME = fresh_home()  # before config is imported

from intake import __version__, cli, config, consent, doctor, setup_wizard  # noqa: E402

# The migration check looks for an older install next to the code, and during
# tests "the code" is this checkout, which really does have one. Point it at
# an empty fake checkout so no test can ever offer to move the real files.
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

# --- home directory --------------------------------------------------------

def t1():
    got = config.resolve_home({"INTAKE_HOME": "/tmp/somewhere"})
    assert got == Path("/tmp/somewhere").resolve(), got
    got = config.resolve_home({"INTAKE_HOME": "~/lectures"})
    assert got == (Path.home() / "lectures").resolve(), got
results.append(run("$INTAKE_HOME wins, with ~ expanded", t1))


def t1b():
    got = config.resolve_home({"LECTUREAI_HOME": "/tmp/old-name"})
    assert got == Path("/tmp/old-name").resolve(), got
    got = config.resolve_home({"INTAKE_HOME": "/tmp/new-name", "LECTUREAI_HOME": "/tmp/old-name"})
    assert got == Path("/tmp/new-name").resolve(), "the new name must win"
    assert config.home_source({"LECTUREAI_HOME": "x"}) == "LECTUREAI_HOME"
    assert config.home_source({"INTAKE_HOME": "x", "LECTUREAI_HOME": "y"}) == "INTAKE_HOME"
    assert config.home_source({"INTAKE_HOME": "  ", "LECTUREAI_HOME": ""}) == "default"
    assert config.HOME_SOURCE == "INTAKE_HOME", config.HOME_SOURCE
results.append(run("$LECTUREAI_HOME, the old name, still works until $INTAKE_HOME is set", t1b))


def t2():
    for env in ({}, {"INTAKE_HOME": ""}, {"INTAKE_HOME": "   "},
                {"INTAKE_HOME": "", "LECTUREAI_HOME": ""}):
        got = config.resolve_home(env)
        assert got == (Path.home() / ".intake").resolve(), (env, got)
results.append(run("unset or blank falls back to ~/.intake", t2))


def t3():
    assert config.HOME_DIR == HOME.resolve(), config.HOME_DIR
    for path in (config.INBOX_DIR, config.PROCESSED_DIR, config.WORK_DIR,
                 config.LOG_FILE, config.LOCK_FILE, config.TOKEN_FILE,
                 config.CREDENTIALS_FILE, config.DRIVE_ROOT_CACHE,
                 config.SCHEDULE_FILE, config.ENV_FILE, config.STATUS_FILE):
        assert HOME.resolve() in path.parents, f"{path} is not under the home"
        assert config.PACKAGE_DIR not in path.parents, f"{path} is under the code"
results.append(run("every data path lives under the home, none under the code", t3))


def t4():
    for sub in ("inbox", "processed", ".work"):
        assert (HOME / sub).is_dir(), f"{sub} was not created"
    assert not (Path.home() / ".intake").exists() or \
        os.environ.get("INTAKE_HOME"), "tests must not create the real home"
results.append(run("importing config creates the home's subfolders", t4))


# --- schedule file ---------------------------------------------------------

def t5():
    loaded = config.parse_schedule(SAMPLE_SCHEDULE)
    assert len(loaded.meetings) == 8, loaded
    assert loaded.by_slot[("Tue", 14)] == "ACCT-4321", loaded.by_slot
    assert loaded.courses() == ["ACCT-4321", "ENTR-3306", "ENTR-4306", "RELI-3304"]
    assert loaded.tolerance_minutes == 45
results.append(run("a valid schedule file loads every row", t5))


def t6():
    text = 'classes = [ { day = "Mon", start = 9, course = "X-1000" } ]\n'
    loaded = config.parse_schedule(text)
    assert loaded.tolerance_minutes == config.DEFAULT_TOLERANCE_MINUTES == 45, loaded
results.append(run("tolerance defaults to 45 when the file leaves it out", t6))


def t7():
    text = ('classes = [ { day = "Mon", start = 9, course = "X-1000" } ]\n'
            'tolerance_minutes = 20\n')
    assert config.parse_schedule(text).tolerance_minutes == 20
    for bad in ("tolerance_minutes = -5", 'tolerance_minutes = "45"',
                "tolerance_minutes = true"):
        try:
            config.parse_schedule(f"classes = []\n{bad}\n")
        except config.ScheduleError as exc:
            assert "tolerance_minutes" in str(exc), exc
        else:
            raise AssertionError(f"{bad!r} should have been rejected")
results.append(run("tolerance is read from the file and type-checked", t7))


def t8():
    missing = HOME / "nope" / "schedule.toml"
    try:
        config.load_schedule(missing)
    except config.ScheduleError as exc:
        assert "intake setup" in str(exc), exc
        assert str(missing) in str(exc), exc
    else:
        raise AssertionError("a missing file should raise ScheduleError")
    assert issubclass(config.ScheduleError, RuntimeError), \
        "the module mains catch RuntimeError, so ScheduleError must be one"
results.append(run("a missing schedule says so and points at intake setup", t8))


def t9():
    cases = {
        'classes = [ { day = "Funday", start = 9, course = "X-1000" } ]': "row 1",
        'classes = [ { day = "Mon", start = 9, course = "X" },\n'
        '            { day = "Tue", start = 25, course = "X" } ]': "row 2",
        'classes = [ { day = "Mon", course = "X-1000" } ]': "missing start",
        'classes = [ { day = "Mon", start = 9, course = "" } ]': "empty",
        'classes = [ "Mon 9 X-1000" ]': "row 1",
        'classes = [ { day = "Mon", start = "9:30", course = "X" } ]': "whole hour",
        'tolerance_minutes = 45': "classes",
        'this is not toml': "not valid TOML",
    }
    for text, expect in cases.items():
        try:
            config.parse_schedule(text)
        except config.ScheduleError as exc:
            assert expect in str(exc), f"{text!r}: {exc}"
        else:
            raise AssertionError(f"{text!r} should have been rejected")
results.append(run("a malformed row is rejected naming the row and the problem", t9))


def t10():
    loaded = config.parse_schedule(
        'classes = [ { day = "tuesday", start = "2pm", course = " ACCT-4321 " },\n'
        '            { day = "Thur", start = "14:00", course = "ACCT-4321" } ]')
    assert loaded.by_slot == {("Tue", 14): "ACCT-4321", ("Thu", 14): "ACCT-4321"}, \
        loaded.by_slot
results.append(run("day and hour spellings people actually type are accepted", t10))


def t11():
    text = config.render_schedule(
        [("Thu", 14, "ACCT-4321"), ("Mon", 9, "ENTR-4306"), ("Mon", 12, "RELI-3304")],
        tolerance_minutes=30)
    back = config.parse_schedule(text)
    assert back.by_slot == {("Mon", 9): "ENTR-4306", ("Mon", 12): "RELI-3304",
                            ("Thu", 14): "ACCT-4321"}, back.by_slot
    assert back.tolerance_minutes == 30
    # The reason the tolerance must stay small travels with the file.
    assert "back-to-back" in text and "tolerance_minutes = 30" in text
    lines = [l for l in text.splitlines() if l.strip().startswith("{")]
    assert len(lines) == 3, "one row per meeting"
results.append(run("a written schedule reads back identically, with its comment", t11))


def t12():
    # The pipeline's matching reads the loaded file, not a constant.
    path = HOME / "alt-schedule.toml"
    config.write_schedule([("Sun", 3, "TEST-0001")], path=path)
    config.set_schedule(config.load_schedule(path))
    try:
        assert config.infer_course(datetime(2026, 9, 6, 3, 10)) == "TEST-0001"
        assert config.infer_course_from_filename("x-test-0001.m4a") == "TEST-0001"
        assert config.infer_course_from_filename("x-acct-4321.m4a") == config.UNKNOWN_COURSE
        assert config.next_class_meeting("TEST-0001", "2026-09-06") == "2026-09-13"
        assert config.courses() == ["TEST-0001"]
        assert config.SCHEDULE == {("Sun", 3): "TEST-0001"}
        assert config.SCHEDULE_TOLERANCE_MINUTES == 45
    finally:
        config.set_schedule(None)
    assert config.infer_course(datetime(2026, 9, 8, 14, 0)) == "ACCT-4321"
results.append(run("course inference, filename fallback, and next meeting follow the file", t12))


# --- setup wizard ------------------------------------------------------------

class Script:
    """Scripted answers for the wizard, in order. Records what was printed."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []
        self.said = []

    def ask(self, prompt):
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError(f"wizard asked more than expected: {prompt!r}")
        return self.answers.pop(0)

    def say(self, text):
        self.said.append(text)

    def output(self):
        return "\n".join(self.said)


DEVICES = [(0, "Someone's iPhone Microphone"), (1, "MacBook Pro Microphone"),
           (2, "USB Headset")]


def wizard(home, answers, devices=DEVICES):
    script = Script(answers)
    w = setup_wizard.Wizard(ask=script.ask, say=script.say, home=home,
                            devices=devices, allow_login=False)
    return w, script


def t13():
    home = Path(tempfile.mkdtemp())
    w, script = wizard(home, [
        "sk-openai-test-key-000000",   # OpenAI
        "sk-ant-test-key-0000000000",  # Anthropic
        "2",                           # microphone by number
        "Tue 14 ACCT-4321",            # schedule rows
        "thu 2pm acct-4321",
        "Mon 9 ENTR-4306",
        "",                            # blank line ends the schedule
        "n",                           # no Notion
        "y",                           # permission to record
    ])
    assert w.run() == 0
    assert script.answers == [], f"unused answers: {script.answers}"
    assert consent.given(home), "a yes to permission to record was not kept"

    env = setup_wizard.read_env(home / ".env")
    assert env["OPENAI_API_KEY"] == "sk-openai-test-key-000000", env
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-test-key-0000000000", env
    assert env["RECORD_DEVICE"] == "USB Headset", env
    assert not env.get("NOTION_TOKEN") and not env.get("NOTION_DATABASE"), env
    assert setup_wizard.NOTION_SKIP_MARKER in (home / ".env").read_text()

    loaded = config.load_schedule(home / "schedule.toml")
    assert loaded.by_slot == {("Tue", 14): "ACCT-4321", ("Thu", 14): "ACCT-4321",
                              ("Mon", 9): "ENTR-4306"}, loaded.by_slot
    for sub in ("inbox", "processed", ".work"):
        assert (home / sub).is_dir(), sub
results.append(run("first run writes .env and schedule.toml into the home", t13))


def t14():
    home = Path(tempfile.mkdtemp())
    w, _ = wizard(home, ["k1", "k2", "1", "Tue 14 ACCT-4321", "", "n", "y"])
    assert w.run() == 0
    for name in (".env", "consent.json"):
        mode = stat.S_IMODE((home / name).stat().st_mode)
        assert mode == 0o600, (name, oct(mode))
    written = {p.name for p in home.iterdir()}
    assert written == {".env", "schedule.toml", "consent.json", "inbox", "processed",
                       ".work"}, written
    assert not any(FAKE_CODE_ROOT.iterdir().__next__().name == ".env"
                   for _ in [0]), "wrote into the code directory"
    assert not (config.PACKAGE_DIR / ".env").exists()
    assert not (config.PACKAGE_DIR / "schedule.toml").exists()
results.append(run(".env is private to the user and nothing lands in the code dir", t14))


def t15():
    home = Path(tempfile.mkdtemp())
    w, _ = wizard(home, ["sk-first-key-0000000000", "sk-ant-first-000000000", "1",
                         "Tue 14 ACCT-4321", "Fri 12 RELI-3304", "", "n", "y"])
    assert w.run() == 0
    (home / ".env").write_text((home / ".env").read_text()
                               + "\nDRIVE_PARENT_FOLDER_ID=folder123\n")

    # Second run: change only the OpenAI key, keep everything else with Enter.
    w, script = wizard(home, ["sk-second-key-000000000", "", "", "", "n"])
    assert w.run() == 0
    assert script.answers == [], script.answers
    env = setup_wizard.read_env(home / ".env")
    assert env["OPENAI_API_KEY"] == "sk-second-key-000000000", env
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-first-000000000", env
    assert env["RECORD_DEVICE"] == "MacBook Pro Microphone", env
    assert env["DRIVE_PARENT_FOLDER_ID"] == "folder123", "hand-added setting was lost"
    loaded = config.load_schedule(home / "schedule.toml")
    assert len(loaded.meetings) == 2, loaded
    # The full keys are shown masked, never in the clear.
    assert "sk-ant-first-000000000" not in "".join(script.prompts + script.said)
    # Asked once per profile, not on every run.
    assert not any("permission" in p.lower() for p in script.prompts), script.prompts
    assert "Permission to record: confirmed" in script.output(), script.output()
results.append(run("rerunning changes one value and keeps the rest, keys masked", t15))


def t15b():
    # Enter is a no here, unlike every other prompt: agreeing by accident is
    # the one answer that must not happen. A no still saves the rest.
    home = Path(tempfile.mkdtemp())
    w, script = wizard(home, ["k1", "k2", "1", "Tue 14 ACCT-4321", "", "n", ""])
    assert w.run() == 0
    assert script.answers == [], script.answers
    assert (home / ".env").exists() and (home / "schedule.toml").exists()
    assert not consent.given(home) and not (home / "consent.json").exists()
    assert "intake setup --consent" in script.output(), script.output()
    assert consent.statement() in script.output(), "the words agreed to were never shown"

    # `intake setup --consent` asks only that, and a yes is kept with when,
    # from where, and by which version.
    w, script = wizard(home, ["yes"])
    assert w.run_consent_only() == 0
    assert script.answers == [] and len(script.prompts) == 1, script.prompts
    data = consent.load(home)
    assert data is not None, "a yes was not kept"
    assert data["agreed"] is True and data["version"] == consent.VERSION, data
    assert data["source"] == "setup" and data["app_version"] == __version__, data
    assert data["statement"] == consent.statement(), data
    assert datetime.fromisoformat(data["agreed_at"]).tzinfo is not None, data
    # Asking again once it is on file asks nothing.
    w, script = wizard(home, [])
    assert w.run_consent_only() == 0 and script.prompts == [], script.prompts

    # A no from --consent is a failed command, so a script can tell.
    other = Path(tempfile.mkdtemp())
    w, _ = wizard(other, ["n"])
    assert w.run_consent_only() == 1 and not consent.given(other)
results.append(run("permission to record needs a real yes, is asked once, and has its own command", t15b))


def t15c():
    # A file that does not say yes, for this version of the words, is no
    # acknowledgment at all: the safe reading of a broken file is to ask again.
    home = Path(tempfile.mkdtemp())
    target = home / "consent.json"
    for text in ("", "not json", "[]", '{"agreed": "yes", "version": 1}',
                 '{"agreed": true}', '{"agreed": true, "version": 0}',
                 '{"agreed": true, "version": true}', '{"agreed": false, "version": 1}'):
        target.write_text(text)
        assert not consent.given(home), f"{text!r} counted as permission to record"
    target.write_text('{"agreed": true, "version": %d}' % consent.VERSION)
    assert consent.given(home)
results.append(run("only a clear yes to the current words counts as permission", t15c))


def t16():
    home = Path(tempfile.mkdtemp())
    w, script = wizard(home, [
        "k1", "k2", "1",
        "Tuesday",                     # not enough fields
        "Tue 27 ACCT-4321",            # bad hour
        "Tue 14 ACCT-4321",            # good
        "",
        "y",                           # Notion yes
        "ntn_secret_000000000000",
        "https://www.notion.so/me/Tasks-a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6?v=x",
        "y",                           # permission to record
    ])
    assert w.run() == 0
    out = script.output()
    assert "three things" in out, out
    assert "0-23" in out, out
    env = setup_wizard.read_env(home / ".env")
    assert env["NOTION_TOKEN"] == "ntn_secret_000000000000", env
    assert env["NOTION_DATABASE"].startswith("https://www.notion.so/"), env
    assert setup_wizard.NOTION_SKIP_MARKER not in (home / ".env").read_text()
    assert config.load_schedule(home / "schedule.toml").by_slot == {("Tue", 14): "ACCT-4321"}
results.append(run("bad schedule rows are re-asked, and Notion can be set up", t16))


def t17():
    home = Path(tempfile.mkdtemp())
    w, script = wizard(home, ["k1", "k2", "Tue 14 ACCT-4321", "", "n", "y"], devices=[])
    assert w.run() == 0
    assert script.answers == [], script.answers
    env = setup_wizard.read_env(home / ".env")
    assert env["RECORD_DEVICE"] == config.RECORD_DEVICE, env
    assert "Microphone" in script.output()
results.append(run("with no devices to list, setup keeps going with the default mic", t17))


def t17b():
    home = Path(tempfile.mkdtemp())
    w, _ = wizard(home, ["k1", "k2", "macbook", "Tue 14 ACCT-4321", "", "n", "y"])
    assert w.run() == 0
    env = setup_wizard.read_env(home / ".env")
    # A substring that picks out one device is stored as that device's name.
    assert env["RECORD_DEVICE"] == "MacBook Pro Microphone", env
    w, _ = wizard(home, ["", "", "Microphone", "", "n"])
    assert w.run() == 0
    # One that matches several stays a substring, which is what config expects.
    assert setup_wizard.read_env(home / ".env")["RECORD_DEVICE"] == "Microphone"
results.append(run("a unique mic substring is stored as the full device name", t17b))


def t18():
    home = Path(tempfile.mkdtemp())
    values = {"OPENAI_API_KEY": "a b#c", "ANTHROPIC_API_KEY": 'q"uote',
              "RECORD_DEVICE": "Trace’s Mic", "NOTION_TOKEN": "", "NOTION_DATABASE": ""}
    setup_wizard.write_env(home / ".env", values, notion_skipped=True)
    back = setup_wizard.read_env(home / ".env")
    for key, want in values.items():
        assert back.get(key, "") == want, (key, back.get(key))
results.append(run("values with spaces, hashes, and quotes survive the .env round trip", t18))


# --- migration -----------------------------------------------------------------

def t19():
    checkout = Path(tempfile.mkdtemp())
    (checkout / "pyproject.toml").write_text("")
    (checkout / ".env").write_text("OPENAI_API_KEY=old\n")
    (checkout / "token.json").write_text("{}")
    (checkout / "inbox").mkdir()
    (checkout / "inbox" / "lecture.m4a").write_bytes(b"x")
    (checkout / "inbox" / ".DS_Store").write_bytes(b"x")

    saved = (config.CODE_ROOT, config.HOME_DIR, config.ENV_FILE)
    home = Path(tempfile.mkdtemp())
    config.CODE_ROOT, config.HOME_DIR, config.ENV_FILE = checkout, home, home / ".env"
    try:
        found = {p.relative_to(checkout).as_posix() for p in config.legacy_files()}
        assert found == {".env", "token.json", "inbox/lecture.m4a"}, found

        script = Script(["n"])
        assert cli.offer_migration(ask=script.ask, say=script.say) is False
        assert (checkout / ".env").exists(), "declining must move nothing"

        script = Script([""])  # Enter means yes
        assert cli.offer_migration(ask=script.ask, say=script.say) is True
        assert (home / ".env").read_text() == "OPENAI_API_KEY=old\n"
        assert (home / "token.json").exists()
        assert (home / "inbox" / "lecture.m4a").exists()
        assert not (checkout / ".env").exists()
        # Once the home has a .env, nothing left in the checkout is offered.
        assert config.legacy_files() == []
    finally:
        config.CODE_ROOT, config.HOME_DIR, config.ENV_FILE = saved
results.append(run("an old install next to the code is offered, then moved, once", t19))


def t19b():
    old = Path(tempfile.mkdtemp(prefix="intake-old-home-")).resolve()
    for name, text in ((".env", "OPENAI_API_KEY=old\n"), ("token.json", "{}"),
                       ("schedule.toml", SAMPLE_SCHEDULE), (".drive_root", "id")):
        (old / name).write_text(text)
    (old / "inbox").mkdir()
    (old / "inbox" / "lecture.m4a").write_bytes(b"x")
    (old / ".work").mkdir()
    (old / ".work" / "recording_x.m4a").write_bytes(b"x")   # scratch: not offered

    saved = (config.HOME_SOURCE, config.OLD_DEFAULT_HOME, config.HOME_DIR, config.ENV_FILE)
    home = Path(tempfile.mkdtemp())
    config.HOME_SOURCE, config.OLD_DEFAULT_HOME = "default", old
    config.HOME_DIR, config.ENV_FILE = home, home / ".env"
    try:
        found = {p.relative_to(old).as_posix() for p in config.legacy_files()}
        assert found == {".env", "token.json", "schedule.toml", ".drive_root",
                         "inbox/lecture.m4a"}, found
        waiting = doctor.check_legacy()
        assert waiting is not None and not waiting.ok and str(old) in waiting.detail, waiting
        assert doctor.check_old_home() is None, "no 'migrated' line before the move"

        script = Script([""])
        assert cli.offer_migration(ask=script.ask, say=script.say) is True
        assert str(old) in script.output() and str(home) in script.output()
        assert (home / ".env").read_text() == "OPENAI_API_KEY=old\n"
        assert config.load_schedule(home / "schedule.toml").by_slot[("Tue", 14)] == "ACCT-4321"
        assert (home / "token.json").exists() and (home / "inbox" / "lecture.m4a").exists()
        assert not (old / ".env").exists()
        assert (old / ".work" / "recording_x.m4a").exists(), "scratch must stay put"

        assert config.legacy_files() == [] and doctor.check_legacy() is None
        migrated = doctor.check_old_home()
        assert migrated is not None and migrated.ok and not migrated.required, migrated
        assert migrated.name == "older home", migrated.name
        assert str(old) in migrated.detail and str(home) in migrated.detail, migrated.detail
        rendered = doctor.render([doctor.check_home(), migrated])
        assert "ok    older home" in rendered, rendered

        # Data left behind after a declined move is mentioned, not flagged.
        (old / "token.json").write_text("{}")
        leftover = doctor.check_old_home()
        assert leftover.ok and "1 data file" in leftover.detail, leftover

        # With a variable set, the old default is nobody's business.
        config.HOME_SOURCE = "INTAKE_HOME"
        (home / ".env").unlink()
        assert config.old_default_home() is None
        assert config.legacy_files() == [] and doctor.check_old_home() is None
    finally:
        (config.HOME_SOURCE, config.OLD_DEFAULT_HOME, config.HOME_DIR, config.ENV_FILE) = saved
results.append(run("the home from before the rename is offered, moved, then reported as migrated", t19b))


def t20():
    # A pipx install has no pyproject next to the package, so never offers.
    saved = config.CODE_ROOT
    config.CODE_ROOT = Path(tempfile.mkdtemp())
    (config.CODE_ROOT / ".env").write_text("x")
    try:
        assert config.legacy_files() == []
    finally:
        config.CODE_ROOT = saved
results.append(run("an installed package never mistakes site-packages for a checkout", t20))


# --- doctor ----------------------------------------------------------------------

def t21():
    saved = (config.OPENAI_API_KEY, config.ANTHROPIC_API_KEY, config.TOKEN_FILE,
             config.SCHEDULE_FILE)
    try:
        config.OPENAI_API_KEY = ""
        config.ANTHROPIC_API_KEY = "sk-ant-test-key-0000000000"
        config.TOKEN_FILE = HOME / "no-token.json"
        config.SCHEDULE_FILE = HOME / "schedule.toml"
        checks = {c.name: c for c in (doctor.check_key("OpenAI key", "OPENAI_API_KEY"),
                                      doctor.check_key("Anthropic key", "ANTHROPIC_API_KEY"),
                                      doctor.check_schedule(), doctor.check_drive_token(),
                                      doctor.check_drive_client(), doctor.check_notion())}
        assert not checks["OpenAI key"].ok and checks["OpenAI key"].fix == "intake setup"
        assert checks["Anthropic key"].ok
        assert "0000000000" not in checks["Anthropic key"].detail, "key shown in full"
        assert checks["class schedule"].ok and "8 class meetings" in checks["class schedule"].detail
        assert not checks["Drive authorization"].ok
        assert checks["Drive authorization"].fix == "intake login"
        assert checks["Drive OAuth client"].ok, checks["Drive OAuth client"]
        assert checks["Notion"].ok and not checks["Notion"].required

        (HOME / "no-token.json").write_text(
            '{"refresh_token": "r", "scopes": ["https://www.googleapis.com/auth/drive.file"]}')
        assert doctor.check_drive_token().ok
        (HOME / "no-token.json").write_text(
            '{"token": "t", "expiry": "2020-01-01T00:00:00Z", '
            '"scopes": ["https://www.googleapis.com/auth/drive.file"]}')
        assert not doctor.check_drive_token().ok, "expired token with no refresh must fail"

        # A token file that parses but is not an object used to get past the
        # try/except and break on .get(). Nothing catches that, so the Setup
        # page's checkup poll became a 500 and `intake doctor` a traceback.
        for shape in ("[]", "5", '"x"', "null"):
            (HOME / "no-token.json").write_text(shape)
            check = doctor.check_drive_token()
            assert not check.ok, shape
            assert "readable JSON" in check.detail, (shape, check.detail)

        config.SCHEDULE_FILE = HOME / "broken.toml"
        config.SCHEDULE_FILE.write_text('classes = [ { day = "Nope", start = 1, course = "X" } ]')
        broken = doctor.check_schedule()
        assert not broken.ok and "row 1" in broken.detail, broken

        rendered = doctor.render(list(checks.values()))
        assert "FAIL  OpenAI key" in rendered and "fix: intake setup" in rendered
        assert "ok    Anthropic key" in rendered
    finally:
        (config.OPENAI_API_KEY, config.ANTHROPIC_API_KEY, config.TOKEN_FILE,
         config.SCHEDULE_FILE) = saved
results.append(run("doctor reports each check with its fix and never a full key", t21))


def t22():
    # The whole CLI, driven without a schedule: readable, no traceback.
    saved = config.SCHEDULE_FILE
    config.SCHEDULE_FILE = HOME / "absent.toml"
    config.reload_schedule()
    import io, contextlib
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            code = cli.main(["record", "--minutes", "1"])
        assert code == 1, code
        assert "intake setup" in err.getvalue(), err.getvalue()
        assert "Traceback" not in err.getvalue()
        with contextlib.redirect_stderr(io.StringIO()):
            assert cli.main(["bogus"]) == 2
    finally:
        config.SCHEDULE_FILE = saved
        config.reload_schedule()
results.append(run("the CLI refuses to record without a schedule and says what to do", t22))


def t23():
    # The write path refuses. Every one of these was accepted before, saved
    # with ok: true, and left a schedule.toml that no longer parsed — or a
    # course that became a path component pointing out of the inbox.
    breaks_toml = ['HISTORY "A"', "A\\B", "X\nY", 'A"}]\nclasses=[', "X\tY"]
    breaks_paths = ["../escaped", "/tmp/absolute", "MATH/101", "..", ".", ".hidden"]
    for bad in breaks_toml + breaks_paths:
        try:
            config.normalize_course(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{bad!r} was accepted as a course")
    for bad, why in ((None, "text"), (True, "text"), (["X"], "text"),
                     ("", "empty"), ("   ", "empty"), ("X" * 65, "longer")):
        try:
            config.normalize_course(bad)
        except ValueError as exc:
            assert why in str(exc), f"{bad!r}: {exc}"
        else:
            raise AssertionError(f"{bad!r} was accepted as a course")
    # And the names people actually have keep working, punctuation included.
    for good, want in (("acct-4321", "ACCT-4321"), ("ACCT4321", "ACCT-4321"),
                       ("acct_4321", "ACCT-4321"), ("MATH 101", "MATH 101"),
                       ("Biology (Lab)", "Biology (Lab)"),
                       ("St. Thomas Seminar", "St. Thomas Seminar")):
        assert config.normalize_course(good) == want, good
results.append(run("a course that would break a file or a path is refused", t23))


def t24():
    # The read path repairs instead. A schedule written before those checks
    # existed, or edited by hand, still has to load: refusing the file would
    # take the panel down over one row.
    text = ('classes = [ { day = "Mon", start = 9, course = "MATH/101" },\n'
            '            { day = "Tue", start = 14, course = "../escaped" } ]\n'
            "tolerance_minutes = 45\n")
    loaded = config.parse_schedule(text)
    assert set(loaded.by_slot) == {("Mon", 9), ("Tue", 14)}, loaded.by_slot
    for course in loaded.by_slot.values():
        assert "/" not in course and not course.startswith("."), course
    # Nothing left to repair means UNKNOWN rather than a crash.
    assert config.safe_course("..") == config.UNKNOWN_COURSE
    assert config.safe_course("") == config.UNKNOWN_COURSE
    assert config.safe_course(None) == config.UNKNOWN_COURSE
    # But a row with no course at all is malformed, not repairable: inventing
    # UNKNOWN would add a meeting that collects every recording at that hour.
    try:
        config.parse_schedule('classes = [ { day = "Mon", start = 9, course = "" } ]')
    except config.ScheduleError as exc:
        assert "empty" in str(exc), exc
    else:
        raise AssertionError("a row with no course should be refused")
results.append(run("a schedule file with unsafe course text is repaired, not refused", t24))


def t25():
    # A Meeting used to skip normalization on the way out, so one built by
    # hand could write text no typed course could ever reach.
    sneaky = config.Meeting.__new__(config.Meeting)
    object.__setattr__(sneaky, "day", "Mon")
    object.__setattr__(sneaky, "hour", 9)
    object.__setattr__(sneaky, "course", 'X" }, { day = "Tue", start = 14, course = "Y')
    try:
        config.render_schedule([sneaky])
    except ValueError:
        pass
    else:
        raise AssertionError("a hand-built Meeting bypassed the course checks")
results.append(run("a Meeting is normalized on the way out, not trusted", t25))


def t26():
    # A save that cannot be read back must not replace the one that could.
    home = Path(tempfile.mkdtemp(prefix="intake-sched-"))
    target = home / "schedule.toml"
    config.write_schedule([("Mon", 9, "ENTR-4306")], 45, path=target)
    before = target.read_text()
    assert config.parse_schedule(before).by_slot == {("Mon", 9): "ENTR-4306"}
    try:
        config.write_schedule([("Mon", 9, 'HISTORY "A"')], 45, path=target)
    except (ValueError, config.ScheduleError):
        pass
    else:
        raise AssertionError("a corrupting schedule was written")
    assert target.read_text() == before, "the good schedule was overwritten"
    # No half-written leftovers from the rename either.
    strays = [p.name for p in home.iterdir() if p.name != "schedule.toml"]
    assert strays == [], strays
results.append(run("a schedule that would not parse never replaces one that does", t26))


def t27():
    # Text that is legal but awkward has to survive the trip to disk and back,
    # which is what the escaping is for.
    for course in ("Biology (Lab)", "St. Thomas Seminar", "Intro to Econ II",
                   "MATH 101", "A'B"):
        text = config.render_schedule([("Mon", 9, course)], 45)
        back = config.parse_schedule(text)
        assert back.by_slot == {("Mon", 9): course}, (course, back.by_slot)
results.append(run("legal course text round-trips through the schedule file", t27))


def t28():
    # Saving Setup writes .env, which used to silence the migration offer in
    # the doctor, the CLI and the wizard at once. Saving Setup is also the one
    # action that moves nothing, so a user who set up before migrating kept
    # their recordings in a directory nothing reads, with nothing saying so.
    root = Path(tempfile.mkdtemp(prefix="intake-legacy-"))
    old = root / ".lectureai"
    (old / "inbox").mkdir(parents=True)
    (old / "inbox" / "old-lecture.m4a").write_bytes(b"x")
    (old / "pipeline.log").write_text("a lecture\n")
    home = root / "syllabus"
    home.mkdir()
    saved = (config.HOME_DIR, config.ENV_FILE, config.SCHEDULE_FILE,
             config.INBOX_DIR, config.PROCESSED_DIR, config.WORK_DIR)
    real_roots = config.legacy_roots
    config.HOME_DIR = home
    config.ENV_FILE = home / ".env"
    config.legacy_roots = lambda: [(old, config.LEGACY_HOME_FILES)]
    try:
        before = doctor.check_legacy()
        assert before is not None and not before.ok, before
        assert "intake setup" in before.fix, before.fix
        assert before.fix_web and "does not" in before.fix_web, before.fix_web

        # What saving the Setup page does, and nothing else.
        config.ENV_FILE.write_text("OPENAI_API_KEY=x\n")
        after = doctor.check_legacy()
        assert after is not None, "the migration went silent after a save"
        assert "old-lecture.m4a" in after.detail or "pipeline.log" in after.detail, after.detail
        # It no longer promises an offer that will never come.
        assert "intake setup" not in after.fix, after.fix
        assert "yourself" in after.fix, after.fix
        assert after.fix_web and "Saving this page does not" in after.fix_web, after.fix_web

        # A file the home already holds was superseded, not stranded: the
        # migration keeps the newer copy on purpose, and saying so forever
        # would be nagging about a decision already taken.
        (home / "pipeline.log").write_text("the current one\n")
        (home / "inbox").mkdir()
        (home / "inbox" / "old-lecture.m4a").write_bytes(b"x")
        assert doctor.check_legacy() is None, doctor.check_legacy()
    finally:
        config.legacy_roots = real_roots
        (config.HOME_DIR, config.ENV_FILE, config.SCHEDULE_FILE,
         config.INBOX_DIR, config.PROCESSED_DIR, config.WORK_DIR) = saved
        config.reload()
results.append(run("older data is still reported after setup is saved", t28))


# --- SEC-01 and SEC-02: a settings value is text, and nothing else ----------

def t29():
    """A reference to a secret stays a reference, and never resolves.

    dotenv interpolates ${OTHER} inside a value by default, so saving a
    microphone called "${OPENAI_API_KEY}" made the next /api/setup hand the
    whole key back as the device name, beside the masked key field.
    """
    home = Path(tempfile.mkdtemp())
    env = home / ".env"
    env.write_text(setup_wizard.render_env(
        {"OPENAI_API_KEY": "sk-SYNTHETIC-KEY-abcdef", "ANTHROPIC_API_KEY": "x",
         "RECORD_DEVICE": "${OPENAI_API_KEY}"}, notion_skipped=True))
    back = setup_wizard.read_env(env)
    assert back["RECORD_DEVICE"] == "${OPENAI_API_KEY}", back["RECORD_DEVICE"]
    assert "SYNTHETIC" not in back["RECORD_DEVICE"], "the key resolved into the device"
    # config's own reader is a separate call site and needs the same setting.
    assert config._read_env(env)["RECORD_DEVICE"] == "${OPENAI_API_KEY}"
results.append(run("a secret reference in a setting stays literal", t29))


def t30():
    """A newline cannot smuggle a second setting into the file."""
    values = {"OPENAI_API_KEY": "k", "ANTHROPIC_API_KEY": "x",
              "RECORD_DEVICE": "MacBook\nACCOUNTS_URL=https://collector.invalid"}
    try:
        setup_wizard.render_env(values, notion_skipped=True)
        raise AssertionError("a newline was written into the settings file")
    except setup_wizard.UnsafeSetting as exc:
        assert "RECORD_DEVICE" in str(exc), exc
    # A carriage return and the other control characters go the same way.
    for bad in ("a\rb", "a\x00b", "a\x1bb"):
        values["RECORD_DEVICE"] = bad
        try:
            setup_wizard.render_env(values, notion_skipped=True)
            raise AssertionError(f"{bad!r} was accepted")
        except setup_wizard.UnsafeSetting:
            pass
results.append(run("a newline in a setting is refused, not written", t30))


def t31():
    """Quoting everything did not break the values people really have.

    A guard on the fix rather than a reproduction of the bug: these all
    round-tripped before too. Quoting unconditionally is the sort of change
    that works on the dangerous input and quietly mangles the ordinary one.
    """
    home = Path(tempfile.mkdtemp())
    env = home / ".env"
    for value in ('MacBook Pro Microphone', 'Scarlett 2i2 "USB"', 'mic $5 #1',
                  "back\\slash", "a'quote", "", "Trace\u2019s AirPods"):
        env.write_text(setup_wizard.render_env(
            {"OPENAI_API_KEY": "k", "ANTHROPIC_API_KEY": "x",
             "RECORD_DEVICE": value}, notion_skipped=True))
        got = setup_wizard.read_env(env)["RECORD_DEVICE"]
        assert got == value, f"{value!r} came back as {got!r}"
results.append(run("ordinary microphone names round-trip exactly", t31))


# --- SEC-08: a secret file is private from its first byte ------------------

def t32():
    """No moment exists when a secret file is readable by anyone else.

    write_text() creates at 0644 under the usual 022 umask and a chmod on the
    next line narrows it; a reader in between gets the whole file, and a write
    that dies before the chmod leaves it open for good.
    """
    saved_umask = os.umask(0o022)
    try:
        home = Path(tempfile.mkdtemp())
        target = home / "deeper" / "secret.txt"
        modes = []

        real_replace = os.replace

        def watching_replace(src, dst):
            # The mode the file carries at the instant it becomes the target.
            modes.append(stat.S_IMODE(os.stat(src).st_mode))
            return real_replace(src, dst)

        os.replace = watching_replace
        try:
            config.write_private(target, "sk-SYNTHETIC\n")
        finally:
            os.replace = real_replace

        assert modes == [0o600], f"created as {[oct(m) for m in modes]}"
        assert stat.S_IMODE(target.stat().st_mode) == 0o600, oct(target.stat().st_mode)
        assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
        assert target.read_text() == "sk-SYNTHETIC\n"

        # A file an older version left wide open is repaired by the next write.
        target.chmod(0o644)
        config.write_private(target, "sk-SYNTHETIC-2\n")
        assert stat.S_IMODE(target.stat().st_mode) == 0o600, oct(target.stat().st_mode)

        # A failed write leaves the previous contents, not a truncated file.
        try:
            config.write_private(target, None)  # type: ignore[arg-type]
        except TypeError:
            pass
        assert target.read_text() == "sk-SYNTHETIC-2\n", target.read_text()
        leftovers = [f.name for f in target.parent.iterdir() if f.name != target.name]
        assert leftovers == [], f"a temporary file was left behind: {leftovers}"
    finally:
        os.umask(saved_umask)
results.append(run("a secret file is 0600 from creation, and replaced atomically", t32))


def t33():
    """Every writer of a secret goes through it, not around it."""
    import inspect
    from intake import account, signin, upload
    for module in (setup_wizard, account, signin, upload):
        source = inspect.getsource(module)
        for line in source.splitlines():
            stripped = line.strip()
            if ".write_text(" not in stripped or stripped.startswith("#"):
                continue
            # DRIVE_ROOT_CACHE holds a folder id, which is not a secret.
            assert "DRIVE_ROOT_CACHE" in stripped, (
                f"{module.__name__} writes a file without write_private: {stripped}")
results.append(run("no secret is written with a plain write_text", t33))


def t_consent_doctor():
    # The doctor and the Setup checkup say whether this Mac may record, and
    # how to fix it in each place. Required: without it nothing records.
    config.CONSENT_FILE.unlink(missing_ok=True)
    missing = doctor.check_consent()
    assert not missing.ok and missing.required, missing
    assert missing.name == "permission to record", missing.name
    assert missing.fix == "intake setup --consent", missing.fix
    assert "I have permission to record" in missing.fix_web, missing.fix_web
    assert "permission to record" in [c.name for c in doctor.run_checks()]
    assert "fix: intake setup --consent" in doctor.render([missing])
    try:
        consent.record("setup")
        given = doctor.check_consent()
        assert given.ok, given
        assert given.detail.startswith("confirmed on 20"), given.detail
    finally:
        config.CONSENT_FILE.unlink(missing_ok=True)
results.append(run("doctor reports permission to record, and the command that fixes it", t_consent_doctor))


print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
