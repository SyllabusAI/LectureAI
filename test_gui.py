"""Tests for gui.py's log parsing, inbox listing, and API guard rails.

Uses Flask's test client against temporary directories: no server is started,
no microphone is opened, and no watcher is spawned. From the project root:

    .venv/bin/python test_gui.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_home import fresh_home  # noqa: E402

fresh_home()  # before config is imported, so nothing touches ~/.intake

from intake import config  # noqa: E402
from intake import cancellations, doctor, gui  # noqa: E402


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


tmp = Path(tempfile.mkdtemp())
results = []

# --- pipeline.log parsing -------------------------------------------------

# Real lines from pipeline.log: five fields on success, four on failure.
LOG = "\t".join(["2026-09-02T10:31:15", "ENTR-4306", "Dallas Baptist University 8.m4a",
                 "ENTR-4306_2026-09-02_Theories-Of-Leadership",
                 "https://docs.google.com/document/d/AAA/edit"]) + "\n" + \
      "\t".join(["2026-09-01T16:20:14", "ERROR", "corrupt-recording.m4a",
                 "Audio file might be corrupted or unsupported"]) + "\n" + \
      "\t".join(["2026-09-08T13:57:17", "ENTR-3306", "Dallas Baptist University 11.m4a",
                 "ENTR-3306_2026-09-08_Technology-And-Brain-Drain",
                 "https://docs.google.com/document/d/BBB/edit"]) + "\n"


def t1():
    log_file = tmp / "pipeline.log"
    log_file.write_text(LOG)
    config.LOG_FILE = log_file
    rows = gui._recent()
    assert len(rows) == 3, rows
    # Newest first, so the last line written comes back first.
    assert rows[0]["name"] == "ENTR-3306_2026-09-08_Technology-And-Brain-Drain", rows[0]
    assert rows[0]["url"].endswith("BBB/edit"), rows[0]
    assert rows[0]["error"] is None
results.append(run("parses successful lectures, newest first", t1))


def t2():
    config.LOG_FILE = tmp / "pipeline.log"
    err = [r for r in gui._recent() if r["error"]]
    assert len(err) == 1, err
    assert err[0]["course"] == "ERROR", err[0]
    assert "corrupted" in err[0]["error"], err[0]
    assert err[0]["source"] == "corrupt-recording.m4a", err[0]
results.append(run("a four-field error line is read as a failure", t2))


def t3():
    config.LOG_FILE = tmp / "pipeline.log"
    assert len(gui._recent(limit=2)) == 2
results.append(run("the recent list respects its limit", t3))


def t4():
    config.LOG_FILE = tmp / "does-not-exist.log"
    assert gui._recent() == []
    # A truncated or half-written line must not take the panel down.
    broken = tmp / "broken.log"
    broken.write_text("garbage\nalso\tgarbage\n\n")
    config.LOG_FILE = broken
    assert gui._recent() == []
results.append(run("a missing or malformed log yields nothing, not an error", t4))


def t4b():
    """The sixth field says what did not reach Notion; five-field lines predate it."""
    log_file = tmp / "warned.log"
    warned = "\t".join([
        "2026-09-09T09:54:13", "ENTR-4306", "ENTR-4306_2026-09-09_0859.m4a",
        "ENTR-4306_2026-09-09_Exploitation-Vs-Exploration",
        "https://docs.google.com/document/d/CCC/edit",
        "2 of 3 to-dos did not reach Notion: no week heading covers 2026-09-11",
    ]) + "\n"
    log_file.write_text(LOG + warned)
    config.LOG_FILE = log_file
    rows = gui._recent()
    assert rows[0]["warning"].startswith("2 of 3 to-dos"), rows[0]
    assert rows[0]["name"].endswith("Exploitation-Vs-Exploration"), rows[0]
    assert rows[0]["error"] is None, rows[0]
    # Lines written before the field existed still read as clean runs.
    assert all(r["warning"] == "" for r in rows[1:]), rows[1:]
results.append(run("a lecture with dropped to-dos carries a warning", t4b))


# --- inbox ----------------------------------------------------------------

def t5():
    inbox = tmp / "inbox"
    inbox.mkdir(exist_ok=True)
    (inbox / "lecture.m4a").write_bytes(b"x" * 2048)
    (inbox / "notes.txt").write_text("ignored")
    (inbox / ".DS_Store").write_bytes(b"ignored")
    (inbox / ".hidden.m4a").write_bytes(b"ignored")
    config.INBOX_DIR = inbox
    items = gui._inbox()
    assert [i["name"] for i in items] == ["lecture.m4a"], items
    assert items[0]["bytes"] == 2048, items
results.append(run("the inbox lists audio only, skipping dotfiles", t5))


# --- integrations ---------------------------------------------------------

def t6():
    config.OPENAI_API_KEY = "sk-test"
    config.ANTHROPIC_API_KEY = ""
    config.TOKEN_FILE = tmp / "missing-token.json"
    config.NOTION_TOKEN = ""
    config.NOTION_DATABASE = ""
    checks = gui._integrations()
    assert checks == {"managed": False, "openai": True, "anthropic": False,
                      "drive": False, "notion": False}, checks

    # Signed in, both keys are the service's. The Setup page must show them as
    # provided rather than as two empty fields somebody has to go and fill in.
    from intake import account
    account.save(account.Account(
        token="syd_x", account_id="a1", email="me@example.com", name="Me",
        device_id="d1", device_name="This Mac", profile="syllabus",
        url=config.ACCOUNTS_URL, claimed_at="2026-09-15T00:00:00Z"))
    config.OPENAI_API_KEY = config.ANTHROPIC_API_KEY = ""
    try:
        managed = gui._integrations()
        assert managed["managed"] is True, managed
        assert managed["openai"] and managed["anthropic"], managed
    finally:
        account.forget()
results.append(run("setup checks report what is configured, and who provides it", t6))


def t7():
    # Presence only. A key's value must never reach the browser.
    config.OPENAI_API_KEY = "sk-supersecret-value"
    assert all(isinstance(v, bool) for v in gui._integrations().values())
    assert "supersecret" not in repr(gui._integrations())
results.append(run("setup checks expose no secret values", t7))


# --- API guard rails ------------------------------------------------------

client = gui.app.test_client()


def t8():
    gui._recorder = None
    res = client.post("/api/record/stop", json={})
    assert res.status_code == 409, res.status_code
    assert res.get_json()["ok"] is False
results.append(run("stopping when not recording is a clean 409", t8))


def t9():
    gui._recorder = None
    res = client.post("/api/record/start", json={"course": "NOPE-0000"})
    assert res.status_code == 400, res.status_code
    assert "NOPE-0000" in res.get_json()["error"]
    # An invalid course must not leave a half-built recorder behind.
    assert gui._recorder is None
results.append(run("an unknown course is rejected before the mic opens", t9))


def t10():
    config.LOCK_FILE = tmp / "no-such.lock"
    res = client.post("/api/watcher/stop", json={})
    assert res.status_code == 409, res.status_code
results.append(run("stopping a watcher that isn't running is a clean 409", t10))


def t11():
    config.LOCK_FILE = tmp / "stale.lock"
    # A PID that no longer exists must read as "not running", not as running.
    config.LOCK_FILE.write_text("999999")
    assert gui._watcher_pid() is None
    config.LOCK_FILE.write_text("not-a-pid")
    assert gui._watcher_pid() is None
    config.LOCK_FILE = tmp / "no-such.lock"
    assert gui._watcher_pid() is None
results.append(run("a stale or corrupt lock file reads as stopped", t11))


def t12():
    config.LOG_FILE = tmp / "pipeline.log"
    config.INBOX_DIR = tmp / "inbox"
    config.LOCK_FILE = tmp / "no-such.lock"
    gui._recorder = None
    body = client.get("/api/status").get_json()
    for key in ("recording", "watcher", "inbox", "recent", "integrations",
                "courses", "now_class"):
        assert key in body, f"status is missing {key}"
    assert body["recording"]["active"] is False
    assert body["watcher"]["running"] is False
    assert isinstance(body["courses"], list) and body["courses"]
results.append(run("the status payload has everything the page reads", t12))


def t12b():
    import json as _json
    from intake import watch
    status = tmp / "status.json"
    config.STATUS_FILE = status
    config.LOCK_FILE = tmp / "no-such.lock"

    watch.write_status("transcribing", "lecture.m4a", "ACCT-4321", "part 3 of 10")
    raw = _json.loads(status.read_text())
    assert raw["stage"] == "transcribing" and raw["detail"] == "part 3 of 10", raw

    # The panel only trusts a status stamped by the watcher that is running.
    seen = gui._processing(raw["pid"])
    assert seen["stage"] == "transcribing", seen
    assert seen["file"] == "lecture.m4a" and seen["course"] == "ACCT-4321", seen
    assert seen["elapsed"] >= 0, seen
results.append(run("the watcher's status is read back by the panel", t12b))


def t12c():
    from intake import watch
    config.STATUS_FILE = tmp / "status.json"
    watch.write_status("summarizing", "lecture.m4a")
    # A watcher that died mid-lecture leaves this behind; a stale stage shown
    # forever is worse than showing nothing.
    assert gui._processing(None) is None, "reported progress with no watcher"
    assert gui._processing(424242) is None, "reported another process's status"

    config.STATUS_FILE.write_text("{ truncated")
    import os as _os
    assert gui._processing(_os.getpid()) is None, "a corrupt status file must not raise"

    watch.clear_status()
    assert not config.STATUS_FILE.exists()
    assert gui._processing(_os.getpid()) is None
results.append(run("stale, foreign, or corrupt status reads as idle", t12c))


def t12d():
    import os as _os
    from intake import watch
    config.STATUS_FILE = tmp / "status.json"
    watch.write_status("uploading", "lecture.m4a", "ACCT-4321")
    body = client.get("/api/status").get_json()
    assert "processing" in body, "status payload is missing processing"
    # No watcher is running in the test, so it must report idle.
    assert body["processing"] is None, body["processing"]
    watch.clear_status()
results.append(run("the status payload carries a processing field", t12d))


def t13():
    res = client.get("/")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    # The branding has to actually render, not just exist in Assets.
    assert "Lexend Deca" in html, "brand font missing"
    assert "#8C52FF" in html, "brand purple missing"
    assert "/static/icon.png" in html, "logo missing"
    assert "AI notetaking for organization and academics." in html, "tagline missing"
    assert ">Syllabus<" in html and "<title>Syllabus</title>" in html, "the name is not Syllabus"
    assert "LectureAI" not in html, "the old name is still on the page"
    setup_html = client.get("/setup").get_data(as_text=True)
    assert "<title>Syllabus setup</title>" in setup_html, setup_html[:300]
results.append(run("the page renders with the brand font, color, logo, and the profile's name", t13))


def t14():
    html = client.get("/").get_data(as_text=True)
    for choice in ("system", "light", "dark"):
        assert f'data-theme-choice="{choice}"' in html, f"{choice} theme button missing"
    assert 'localStorage.getItem("lectureai-theme")' in html, "theme is not remembered"
results.append(run("the page offers system, light, and dark", t14))


def t15():
    html = client.get("/").get_data(as_text=True)
    # The saved theme must be applied before the stylesheet is parsed,
    # otherwise the page paints the OS theme first and visibly corrects.
    script = html.find("lectureai-theme")
    style = html.find("<style")
    assert script != -1 and style != -1, "missing script or style"
    assert script < style, "theme script runs after the stylesheet; page will flash"
results.append(run("the saved theme is applied before first paint", t15))


def t16():
    html = client.get("/").get_data(as_text=True)
    # Both halves of every themed token live in one light-dark() call, so a
    # colour cannot be updated in one theme and forgotten in the other.
    assert "color-scheme: light dark" in html, "root does not follow the OS by default"
    for choice in ("light", "dark"):
        assert f':root[data-theme="{choice}"]' in html, f"no explicit {choice} override"
        assert f"color-scheme: {choice};" in html, f"{choice} does not set color-scheme"
    assert "light-dark(#faf9fc, #131019)" in html, "background is not theme-aware"
results.append(run("themes are driven by color-scheme, not a second palette", t16))


def t17():
    html = client.get("/").get_data(as_text=True)
    # .row sets display:flex, which outranks the browser's [hidden] rule, so
    # the idle processing row stayed on screen reading "Working" forever.
    assert "[hidden] { display: none !important; }" in html, \
        "hidden elements will stay visible"
results.append(run("[hidden] beats the display rules that broke it", t17))


# --- setup page -------------------------------------------------------------

setup_home = tmp / "setup-home"
setup_home.mkdir()


def point_config_at(home):
    config.HOME_DIR = home
    config.ENV_FILE = home / ".env"
    config.SCHEDULE_FILE = home / "schedule.toml"
    config.CANCELED_FILE = home / "canceled.json"
    config.TOKEN_FILE = home / "token.json"
    config.INBOX_DIR = home / "inbox"
    config.PROCESSED_DIR = home / "processed"
    config.WORK_DIR = home / ".work"
    config.reload()


# No microphone is opened: the device list is canned, the way test_record does it.
gui.recording.list_devices = lambda: [(0, "Someone's iPhone Microphone"),
                                      (1, "MacBook Pro Microphone")]
# And no caffeinate is started for the `sleep` that stands in for ffmpeg.
gui.recording._keep_awake = lambda pid: None


def t18():
    res = client.get("/setup")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "Lexend Deca" in html and "#8C52FF" in html, "setup page is off-brand"
    for step in ("API keys", "Microphone", "Class schedule", "Notion",
                 "Google Drive"):
        assert step in html, f"setup page is missing {step}"
    assert "/api/setup" in html and "/api/drive/login" in html
    # The panel itself must offer a way in.
    assert 'href="/setup"' in client.get("/").get_data(as_text=True)
results.append(run("the setup page renders with every step and the panel links to it", t18))


def t19():
    point_config_at(setup_home)
    # An empty home: no schedule, no keys. The panel must still answer.
    body = client.get("/api/status").get_json()
    assert body["courses"] == [] and body["configured"] is False, body
    assert body["now_class"] is None
    state = client.get("/api/setup").get_json()
    assert state["openai_set"] is False and state["schedule"] == [], state
    assert [d["name"] for d in state["devices"]] == ["Someone's iPhone Microphone",
                                                     "MacBook Pro Microphone"]
    assert state["drive"]["connected"] is False
    assert state["configured"] is False
results.append(run("with nothing configured the panel answers instead of failing", t19))


def t20():
    point_config_at(setup_home)
    res = client.post("/api/setup", json={
        "openai_key": "sk-openai-test-key-000000",
        "anthropic_key": "sk-ant-test-key-0000000000",
        "device": "MacBook Pro Microphone",
        "schedule": [{"day": "Tue", "start": 14, "course": "acct-4321"},
                     {"day": "Thursday", "start": "14", "course": "ACCT-4321"}],
        "tolerance": 30,
        "notion": {"enabled": False},
    })
    assert res.status_code == 200, res.get_json()
    out = res.get_json()
    assert out["ok"] and out["configured"] and out["classes"] == 2, out

    from intake import setup_wizard
    env = setup_wizard.read_env(setup_home / ".env")
    assert env["OPENAI_API_KEY"] == "sk-openai-test-key-000000", env
    assert env["RECORD_DEVICE"] == "MacBook Pro Microphone", env
    assert not env.get("NOTION_TOKEN"), env
    loaded = config.load_schedule(setup_home / "schedule.toml")
    assert loaded.by_slot == {("Tue", 14): "ACCT-4321", ("Thu", 14): "ACCT-4321"}, loaded.by_slot
    assert loaded.tolerance_minutes == 30
    # The running panel picks the new settings up without a restart.
    assert config.OPENAI_API_KEY == "sk-openai-test-key-000000"
    assert client.get("/api/status").get_json()["courses"] == ["ACCT-4321"]
    assert not (config.PACKAGE_DIR / ".env").exists()
results.append(run("saving the form writes .env and schedule.toml and reloads them", t20))


def t21():
    point_config_at(setup_home)
    state = client.get("/api/setup").get_json()
    # Masked, never the key itself.
    assert state["openai_set"] is True
    assert "sk-openai-test-key-000000" not in json.dumps(state), "key leaked to the page"
    assert state["openai"].startswith("sk-ope") and "..." in state["openai"], state["openai"]
    assert state["schedule"][0] == {"day": "Tue", "start": 14, "course": "ACCT-4321"}

    # Blank keys keep what is on file; one bad row is named.
    res = client.post("/api/setup", json={
        "openai_key": "", "anthropic_key": "", "device": "",
        "schedule": [{"day": "Mon", "start": 9, "course": "ENTR-4306"},
                     {"day": "Someday", "start": 9, "course": "X"}],
        "notion": {"enabled": False},
    })
    assert res.status_code == 400, res.get_json()
    assert res.get_json()["row"] == 2 and "row 2" in res.get_json()["error"]
    assert config.load_schedule(setup_home / "schedule.toml").by_slot == \
        {("Tue", 14): "ACCT-4321", ("Thu", 14): "ACCT-4321"}, "a rejected save changed the file"

    res = client.post("/api/setup", json={
        "openai_key": "", "anthropic_key": "", "device": "",
        "schedule": [{"day": "Mon", "start": 9, "course": "ENTR-4306"}],
        "notion": {"enabled": True, "token": "ntn_x", "database": ""},
    })
    assert res.status_code == 400 and "Notion" in res.get_json()["error"], res.get_json()

    res = client.post("/api/setup", json={
        "openai_key": "", "anthropic_key": "", "device": "",
        "schedule": [{"day": "Mon", "start": 9, "course": "ENTR-4306"}],
        "notion": {"enabled": False},
    })
    assert res.status_code == 200, res.get_json()
    from intake import setup_wizard
    env = setup_wizard.read_env(setup_home / ".env")
    assert env["OPENAI_API_KEY"] == "sk-openai-test-key-000000", "blank key wiped the saved one"
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-test-key-0000000000"
results.append(run("the page shows masked keys, keeps them on blank, and names a bad row", t21))


def t22():
    point_config_at(setup_home)
    res = client.post("/api/setup", json={"openai_key": "", "anthropic_key": "",
                                          "schedule": [], "notion": {"enabled": False}})
    assert res.status_code == 400 and "at least one class" in res.get_json()["error"]
    empty = tmp / "empty-home"; empty.mkdir()
    point_config_at(empty)
    res = client.post("/api/setup", json={"openai_key": "sk-only-one", "anthropic_key": "",
                                          "schedule": [{"day": "Mon", "start": 9, "course": "X-1"}],
                                          "notion": {"enabled": False}})
    assert res.status_code == 400 and "both API keys" in res.get_json()["error"]
    assert not (empty / ".env").exists(), "a rejected save must write nothing"
    point_config_at(setup_home)
results.append(run("no schedule or a missing key is refused before anything is written", t22))


def t22b():
    # The other half of t22: a signed-in Mac has no keys of its own to give,
    # and the page hides the fields that would collect them. Demanding them
    # here was a dead end with no way out of it, because the refusal came
    # before the schedule was written, so the Mac never became configured and
    # the panel reopened this same page every time.
    from intake import account
    managed_home = tmp / "managed-home"
    managed_home.mkdir()
    point_config_at(managed_home)
    account.save(account.Account(
        token="syd_m", account_id="a1", email="me@example.com", name="Me",
        device_id="d1", device_name="This Mac", profile="syllabus",
        url=config.ACCOUNTS_URL, claimed_at="2026-09-15T00:00:00Z"))
    try:
        res = client.post("/api/setup", json={
            "openai_key": "", "anthropic_key": "", "device": "MacBook Pro Microphone",
            "schedule": [{"day": "Mon", "start": 9, "course": "ENTR-4306"}],
            "notion": {"enabled": False},
        })
        assert res.status_code == 200, res.get_json()
        assert (managed_home / "schedule.toml").is_file(), "the schedule was not written"
        assert gui._configured(), "a signed-in Mac with classes is set up"
        assert not gui.start_url("127.0.0.1", 5173).endswith("/setup"), \
            "the panel should stop reopening Setup once the save lands"
        # Signed out with no keys on file, the demand is right and must stay.
        account.forget()
        res = client.post("/api/setup", json={
            "openai_key": "", "anthropic_key": "",
            "schedule": [{"day": "Mon", "start": 9, "course": "ENTR-4306"}],
            "notion": {"enabled": False},
        })
        assert res.status_code == 400 and "both API keys" in res.get_json()["error"]
    finally:
        account.forget()
        point_config_at(setup_home)
results.append(run("a signed-in Mac saves setup without keys of its own", t22b))


def t22c():
    # The CLI wizard is the other door into the same settings, and it was the
    # only way a managed Mac could get through at all. It must not ask for
    # keys the account already covers.
    from intake import account, setup_wizard
    asked = []
    wizard = setup_wizard.Wizard(home=tmp / "wizard-home")
    wizard.ask = lambda prompt: (asked.append(prompt), "")[1]
    wizard.say = lambda *a, **k: None
    account.save(account.Account(
        token="syd_w", account_id="a1", email="me@example.com", name="Me",
        device_id="d1", device_name="This Mac", profile="syllabus",
        url=config.ACCOUNTS_URL, claimed_at="2026-09-15T00:00:00Z"))
    try:
        values = {}
        wizard.ask_keys(values)
        assert asked == [], f"a managed Mac was asked for keys: {asked}"
        account.forget()
        wizard.ask_keys(values)
        assert any("OpenAI" in prompt for prompt in asked), asked
    finally:
        account.forget()
results.append(run("the wizard skips key prompts on a signed-in Mac", t22c))


def t22d():
    # Saving a course that would break the schedule file used to return
    # ok: true and leave a schedule.toml nothing could parse, with the working
    # one already gone. The row is named so the page can point at it.
    point_config_at(setup_home)
    good = client.post("/api/setup", json={
        "openai_key": "", "anthropic_key": "", "device": "",
        "schedule": [{"day": "Mon", "start": 9, "course": "ENTR-4306"}],
        "notion": {"enabled": False},
    })
    assert good.status_code == 200, good.get_json()
    before = (setup_home / "schedule.toml").read_text()
    for bad in ('HISTORY "A"', "A\\B", "../escaped", "MATH/101", "X\nY"):
        res = client.post("/api/setup", json={
            "openai_key": "", "anthropic_key": "", "device": "",
            "schedule": [{"day": "Mon", "start": 9, "course": "ENTR-4306"},
                         {"day": "Tue", "start": 14, "course": bad}],
            "notion": {"enabled": False},
        })
        body = res.get_json()
        assert res.status_code == 400, f"{bad!r} was saved: {body}"
        assert body.get("row") == 2, f"{bad!r} should name row 2: {body}"
        assert (setup_home / "schedule.toml").read_text() == before, \
            f"{bad!r} replaced the working schedule"
    assert config.parse_schedule(before).by_slot, "the kept schedule still parses"
results.append(run("a course that would corrupt the schedule is refused by the route", t22d))


def t22e():
    # The course picker was the one place on the dashboard that built markup
    # out of a course without escaping it, while the function right below it
    # escaped everything. The schedule refuses markup now, so this is the
    # second of the two layers rather than the only one.
    #
    # Asserted against the source text because this repo has no way to run its
    # templates; a JS harness would check the rendered node instead.
    html = client.get("/").get_data(as_text=True)
    start = html.index("function renderCourses(")
    body = html[start:html.index("\nfunction ", start + 1)]
    assert "escapeHtml(" in body, "renderCourses does not escape the course"
    assert "${c}" not in body, f"an unescaped course is still interpolated:\n{body}"
results.append(run("the course picker escapes the course it renders", t22e))


def t22f():
    # Every one of these used to be a 500 with an HTML body, which the page
    # cannot show at all: it parses the answer as JSON and gets a parse error
    # where the explanation should be.
    point_config_at(setup_home)
    cases = [
        ("/api/setup", [1], "JSON object"),
        ("/api/setup", "x", "JSON object"),
        ("/api/setup", 42, "JSON object"),
        ("/api/setup", {"schedule": 42}, "schedule must be a list"),
        ("/api/setup", {"schedule": [], "notion": [1]}, "notion must be an object"),
        ("/api/setup", {"schedule": ["Mon 9 X"]}, "class row 1"),
        ("/api/setup", {"device": 5}, "device must be text"),
        ("/api/record/start", {"course": 123}, "course must be text"),
    ]
    for url, body, expect in cases:
        res = client.post(url, json=body)
        assert res.status_code == 400, f"{body!r} -> {res.status_code}"
        assert res.headers["Content-Type"].startswith("application/json"), \
            f"{body!r} answered {res.headers['Content-Type']}"
        got = res.get_json()
        assert expect in got["error"], f"{body!r}: {got}"
        assert got["ok"] is False, got
results.append(run("a malformed request body is a JSON 400, not an HTML 500", t22f))


def t22g():
    # A shape problem must name itself. Reporting a missing API key for a
    # request whose schedule is the number 42 is true of the body and useless
    # to whoever sent it.
    point_config_at(setup_home)
    res = client.post("/api/setup", json={"schedule": 42})
    assert "schedule" in res.get_json()["error"], res.get_json()
results.append(run("a shape error names the field, not the next rule down", t22g))


def t22h():
    # int() accepted 1.8 and True and stored 1, which disagreed with both the
    # message it would otherwise show and the parser that reads the value back
    # off disk. The schedule file's rule is the rule.
    point_config_at(setup_home)

    def save(tolerance):
        return client.post("/api/setup", json={
            "openai_key": "", "anthropic_key": "", "device": "",
            "schedule": [{"day": "Mon", "start": 9, "course": "ENTR-4306"}],
            "tolerance": tolerance, "notion": {"enabled": False},
        })

    for bad in (1.8, True, False, "45", -1, None, [45]):
        res = save(bad)
        assert res.status_code == 400, f"tolerance {bad!r} was accepted"
        assert "whole number" in res.get_json()["error"], res.get_json()
    # Zero is a real setting: only a recording starting on the hour matches.
    for good in (0, 45, 120):
        assert save(good).status_code == 200, good
        assert config.load_schedule().tolerance_minutes == good, good
results.append(run("tolerance is refused rather than truncated", t22h))


def t23():
    point_config_at(setup_home)
    body = client.get("/api/doctor").get_json()
    names = [c["name"] for c in body["checks"]]
    for want in ("Python", "ffmpeg", "OpenAI key", "class schedule", "Drive authorization", "Notion"):
        assert want in names, f"doctor is missing {want}"
    assert "sk-openai-test-key-000000" not in json.dumps(body), "doctor leaked a key"
    drive = next(c for c in body["checks"] if c["name"] == "Drive authorization")
    assert drive["ok"] is False and drive["fix"] == "intake login"
    assert body["healthy"] is False
    assert "ok" not in body, "an ok field would read as a failed request on the page"
results.append(run("the checkup endpoint mirrors doctor without exposing secrets", t23))


def t24():
    point_config_at(setup_home)
    (setup_home / "token.json").write_text("{}")
    res = client.post("/api/drive/login", json={})
    assert res.status_code == 409, "must not start a login while a token exists"
    assert client.get("/api/setup").get_json()["drive"]["connected"] is True
    res = client.post("/api/drive/disconnect", json={})
    assert res.status_code == 200
    assert not (setup_home / "token.json").exists()
    assert client.get("/api/setup").get_json()["drive"]["connected"] is False
results.append(run("Drive login refuses to double up and disconnect removes the token", t24))


def t25():
    # Flask's run() reads a .env from the current directory by default, which
    # let a checkout's .env override the home directory's settings. The panel
    # must start with that off, on localhost, and without a schedule.
    captured = {}
    real_run, real_open = gui.app.run, gui._open_browser_later
    gui.app.run = lambda **kw: captured.update(kw)
    gui._open_browser_later = lambda url, delay=0: captured.update(opened=url)
    try:
        point_config_at(tmp / "another-empty-home")
        assert gui.main(["--no-browser", "--port", "5555"]) == 0
        assert captured["load_dotenv"] is False, captured
        assert captured["host"] == "127.0.0.1" and captured["port"] == 5555, captured
        assert "opened" not in captured, "--no-browser still opened a browser"
        captured.clear()
        assert gui.main(["--port", "5556"]) == 0
        assert captured["opened"].endswith(":5556/setup"), "an empty home must land on setup"
    finally:
        gui.app.run, gui._open_browser_later = real_run, real_open
        point_config_at(setup_home)
results.append(run("the panel starts without Flask's cwd .env, on localhost, landing on setup", t25))


def t26():
    # The panel that started a recording was closed; ffmpeg kept going. A new
    # panel must show that recording as live and be able to stop it, instead
    # of saying "Not recording" while the microphone is still open.
    import subprocess
    from datetime import datetime as _dt
    rec_mod = gui.recording
    state_file = config.RECORDING_STATE_FILE
    staging = config.WORK_DIR / "recording_20260910-123340.m4a"
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.write_bytes(b"x" * 500)
    proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
    rec_mod._write_state(proc.pid, staging, _dt(2026, 9, 10, 12, 33, 40),
                         "ENTR-3306", "MacBook Pro Microphone")
    real_owner = rec_mod._process_is_recording
    rec_mod._process_is_recording = lambda pid, path: pid == proc.pid
    gui._recorder = None
    try:
        body = client.get("/api/status").get_json()
        r = body["recording"]
        assert r["active"] is True, r
        assert r["resumed"] is True, r
        assert r["planned"] == "ENTR-3306_2026-09-10_1233.m4a", r
        assert r["bytes"] == 500 and r["elapsed"] > 60, r
        # Starting another one is refused while it runs.
        res = client.post("/api/record/start", json={})
        assert res.status_code == 409, res.status_code
        res = client.post("/api/record/stop", json={})
        assert res.status_code == 200, (res.status_code, res.get_json())
        out = res.get_json()
        assert out["name"] == "ENTR-3306_2026-09-10_1233.m4a", out
        assert (config.INBOX_DIR / out["name"]).exists()
        assert not state_file.exists() and not staging.exists()
        assert gui._recorder is None
        assert client.get("/api/status").get_json()["recording"]["active"] is False
    finally:
        rec_mod._process_is_recording = real_owner
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        state_file.unlink(missing_ok=True)
        staging.unlink(missing_ok=True)
        gui._recorder = None
results.append(run("a recording an earlier panel started is shown and can be stopped", t26))


def t27():
    # ffmpeg hit its time cap and exited while the panel was open. The next
    # status poll files the finished recording instead of leaving it in .work/.
    from datetime import datetime as _dt
    rec_mod = gui.recording
    staging = config.WORK_DIR / "recording_20260910-140000.m4a"
    staging.write_bytes(b"x" * 500)
    done = rec_mod.Recorder(course="ACCT-4321")
    done.started = _dt(2026, 9, 10, 14, 0, 0)
    done._staging = staging
    done._proc = rec_mod._ExternalProcess(2 ** 22 - 1)  # already gone
    gui._recorder = done
    body = client.get("/api/status").get_json()
    assert body["recording"]["active"] is False, body["recording"]
    assert gui._recorder is None
    filed = config.INBOX_DIR / "ACCT-4321_2026-09-10_1400.m4a"
    assert filed.exists(), sorted(p.name for p in config.INBOX_DIR.iterdir())
    filed.unlink()
results.append(run("a recording that ended on its own is filed on the next poll", t27))


def t28():
    # The watcher is running when its lock is held. A pid alone is not proof:
    # a watcher this panel started and never reaped stays a zombie that
    # answers kill(0), and the panel said "running" about one for an hour.
    import fcntl
    import os as _os
    lock = tmp / "held.lock"
    config.LOCK_FILE = lock
    lock.write_text(str(_os.getpid()))
    assert gui._watcher_pid() is None, "our own live pid, but nobody holds the lock"
    handle = lock.open("r+")
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert gui._watcher_pid() == _os.getpid(), "a held lock is a running watcher"
        body = client.get("/api/status").get_json()
        assert body["watcher"]["running"] is True and body["watcher"]["pid"] == _os.getpid()
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()
    assert gui._watcher_pid() is None
    assert client.get("/api/status").get_json()["watcher"]["running"] is False
    config.LOCK_FILE = tmp / "no-such.lock"
results.append(run("the watcher shows as running only while its lock is held", t28))


def t29():
    # A watcher the panel started is reaped once it exits, so it cannot sit
    # as a zombie for the life of the panel.
    import subprocess
    import time as _time
    child = subprocess.Popen(["true"], start_new_session=True)
    gui._children.append(child)
    deadline = _time.monotonic() + 5
    while child.poll() is None and _time.monotonic() < deadline:
        _time.sleep(0.05)
    child.wait(5)
    gui._children.append(subprocess.Popen(["true"], start_new_session=True))
    _time.sleep(0.3)
    gui._watcher_pid()
    assert child.returncode is not None
    assert gui._children == [], gui._children
results.append(run("watchers the panel started are reaped once they exit", t29))


def t30():
    # A watcher that loses the lock race must leave the winner's pid alone.
    # Opening the lock file with "w" truncated it before the flock, so every
    # loser wiped the pid, the panel read a held lock with no pid and said
    # "stopped", and the button started loser after loser.
    import fcntl
    from intake import watch
    lock = tmp / "race.lock"
    config.LOCK_FILE = lock
    lock.write_text("424242")
    holder = lock.open("r+")
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        try:
            watch.acquire_single_instance_lock()
        except RuntimeError as exc:
            assert "already holds" in str(exc), exc
        else:
            raise AssertionError("a second watcher took a held lock")
        assert lock.read_text() == "424242", f"loser wiped the pid: {lock.read_text()!r}"
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()
    # And the winner writes its own pid over whatever was there.
    import os as _os
    handle = watch.acquire_single_instance_lock()
    try:
        assert lock.read_text() == str(_os.getpid()), lock.read_text()
    finally:
        handle.close()
    config.LOCK_FILE = tmp / "no-such.lock"
results.append(run("a watcher that loses the lock race leaves the holder's pid intact", t30))


def t31():
    # A held lock whose pid never got written is still a running watcher.
    # The panel must not report it stopped, or it offers to start a second.
    import fcntl
    import os as _os
    lock = tmp / "empty-held.lock"
    config.LOCK_FILE = lock
    lock.write_text("")
    holder = lock.open("r+")
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert gui._watcher_pid() == _os.getpid(), gui._watcher_pid()
        assert client.get("/api/status").get_json()["watcher"]["running"] is True
        res = client.post("/api/watcher/start", json={})
        assert res.status_code == 409, (res.status_code, res.get_json())
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()
    assert gui._watcher_pid() is None
    config.LOCK_FILE = tmp / "no-such.lock"
results.append(run("a held lock with no pid in it still shows the watcher as running", t31))


def t32():
    # A watcher that dies right after starting is a failed start, and the
    # button should say why, not flash "started" over a process that is gone.
    import subprocess
    real_popen = gui.subprocess.Popen
    log = tmp / "watcher-gui.log"
    gui.WATCHER_LOG = log
    config.LOCK_FILE = tmp / "no-such.lock"

    def dying(args, **kw):
        return real_popen(["sh", "-c", "echo '[10:00:00] error: preflight refused' >&2; exit 1"], **kw)

    gui.subprocess.Popen = dying
    try:
        res = client.post("/api/watcher/start", json={})
    finally:
        gui.subprocess.Popen = real_popen
    body = res.get_json()
    assert res.status_code == 500, (res.status_code, body)
    assert body["error"] == "preflight refused", body
    assert gui._children == [], gui._children

    def living(args, **kw):
        return real_popen(["sleep", "30"], **kw)

    gui.subprocess.Popen = living
    try:
        res = client.post("/api/watcher/start", json={})
    finally:
        gui.subprocess.Popen = real_popen
    assert res.status_code == 200 and res.get_json()["ok"] is True, res.get_json()
    for child in gui._children:
        child.kill()
        child.wait(5)
    gui._children.clear()
results.append(run("a watcher that exits right after starting fails the start with its error", t32))


def t30():
    # A live recording with nothing on disk after the grace period is called
    # out in the status payload, and the page has somewhere to show it.
    from datetime import datetime as _dt, timedelta
    rec_mod = gui.recording

    class Alive:
        pid = 4242
        def poll(self):
            return None

    staging = config.WORK_DIR / "recording_20260911-085900.m4a"
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.write_bytes(b"x" * 44)
    rec = rec_mod.Recorder(course="ENTR-4306")
    rec.device_name = "MacBook Pro Microphone"
    rec.started = _dt.now() - timedelta(seconds=config.RECORD_NO_AUDIO_SECONDS + 5)
    rec._staging, rec._proc = staging, Alive()
    gui._recorder = rec
    try:
        r = client.get("/api/status").get_json()["recording"]
        assert r["active"] is True and r["stalled"] is True, r
        assert "No audio" in r["warning"] and "MacBook Pro Microphone" in r["warning"], r
        staging.write_bytes(b"x" * (config.RECORD_NO_AUDIO_BYTES + 1))
        r = client.get("/api/status").get_json()["recording"]
        assert r["stalled"] is False and r["warning"] == "", r
    finally:
        gui._recorder = None
        staging.unlink(missing_ok=True)
    html = client.get("/").get_data(as_text=True)
    assert "rec-warn" in html and "r.stalled" in html, "the page never shows the warning"
    assert "Closing the lid ends the recording" in html, "the page does not warn about the lid"
results.append(run("a stalled recording is reported to the page, which warns about the lid too", t30))


def t31():
    # A recording that captured nothing is written to pipeline.log by the
    # recorder, and the panel's recent list reads it as a failure.
    log_file = tmp / "recording-failures.log"
    config.LOG_FILE = log_file
    config.append_log_line("ERROR", "ENTR-4306_2026-09-11_0859.m4a",
                           "could not record from MacBook Pro Microphone: the file "
                           "holds no audio.\nffmpeg reported no error of its own.")
    rows = gui._recent()
    assert len(rows) == 1 and rows[0]["error"], rows
    assert rows[0]["source"] == "ENTR-4306_2026-09-11_0859.m4a", rows[0]
    assert "\n" not in log_file.read_text().rstrip("\n"), "a newline inside a field breaks the log"
    assert "no audio" in rows[0]["error"], rows[0]
results.append(run("a recording that captured nothing appears in the recent list as failed", t31))

def t32():
    # The panel has no login. Binding it beyond this Mac hands process control
    # and the keys in .env to anyone who can reach the port, so a non-loopback
    # --host is refused unless --expose says that was the intent.
    from intake import signin
    captured = {}
    real_run, real_open = gui.app.run, gui._open_browser_later
    was_exposed = signin.EXPOSED
    gui.app.run = lambda **kw: captured.update(kw)
    gui._open_browser_later = lambda url, delay=0: captured.update(opened=url)
    try:
        assert gui.main(["--no-browser", "--host", "0.0.0.0"]) == 2
        assert not captured, "the server started on 0.0.0.0 without --expose"
        assert gui.main(["--no-browser", "--host", "192.168.1.20", "--port", "5557"]) == 2
        assert not captured, "the server started on a LAN address without --expose"
        for host in ("127.0.0.1", "localhost", "::1", "127.0.0.2"):
            captured.clear()
            assert gui.main(["--no-browser", "--host", host, "--port", "5557"]) == 0, host
            assert captured["host"] == host, captured
        captured.clear()
        assert gui.main(["--no-browser", "--host", "0.0.0.0", "--expose"]) == 0
        assert captured["host"] == "0.0.0.0", captured
        # --expose stands the Host check down for the rest of the process,
        # which is right for a panel behind a proxy and has to be put back
        # here, or every test after this one runs as though it were exposed.
        assert signin.EXPOSED is True, "--expose must tell the gate it is proxied"
    finally:
        gui.app.run, gui._open_browser_later = real_run, real_open
        signin.EXPOSED = was_exposed
results.append(run("a non-localhost --host is refused unless --expose says so", t32))

def t34():
    # The launchd agent that keeps the panel running. launchctl is stubbed and
    # the plist goes to a temp folder, so nothing here touches this Mac.
    import plistlib
    from intake import service
    calls = []

    class Done:
        returncode = 0
        stdout = "\tstate = running\n\tpid = 4242\n"
        stderr = ""

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return Done()
    agents = tmp / "LaunchAgents"
    said = []
    real_answers = service.port_answers
    service.port_answers = lambda port, timeout=0.5: True
    try:
        rc = service.install(say=said.append, run=fake_run, agents_dir=agents, wait=1)
        assert rc == 0, said
        path = agents / "com.maincoursemedia.syllabus.panel.plist"
        assert path.exists(), list(agents.iterdir())
        data = plistlib.loads(path.read_bytes())
        assert data["Label"] == "com.maincoursemedia.syllabus.panel"
        assert data["ProgramArguments"][0] == sys.executable, data["ProgramArguments"]
        assert data["ProgramArguments"][1:] == ["-m", "intake.cli", "--profile", "syllabus", "panel", "--no-browser"]
        assert data["RunAtLoad"] is True and data["KeepAlive"] is True
        assert "/opt/homebrew/bin" in data["EnvironmentVariables"]["PATH"], "ffmpeg would not be found"
        assert data["EnvironmentVariables"]["INTAKE_PROFILE"] == "syllabus"
        assert data["StandardErrorPath"].endswith("panel.log")
        verbs = [c[1] for c in calls]
        assert verbs == ["bootout", "bootstrap", "kickstart"], verbs
        assert calls[1][2].startswith("gui/") and calls[1][3] == str(path), calls[1]
        assert calls[2][2] == "-k", "restart must replace a running copy"

        calls.clear()
        assert service.status(say=said.append, run=fake_run, agents_dir=agents) == 0
        assert any("pid 4242" in line for line in said), said
        assert any("answering" in line for line in said), said

        calls.clear()
        assert service.restart(say=said.append, run=fake_run, agents_dir=agents) == 0
        assert calls[0][1:3] == ["kickstart", "-k"], calls

        calls.clear()
        assert service.uninstall(say=said.append, run=fake_run, agents_dir=agents) == 0
        assert not path.exists()
        assert calls[0][1] == "bootout", calls
        assert service.restart(say=said.append, run=fake_run, agents_dir=agents) == 1, "restart with no agent must say so"
    finally:
        service.port_answers = real_answers
results.append(run("the service command writes a launchd agent for this interpreter and drives launchctl", t34))


def t35():
    """The Setup page's account card, against a scripted account service."""
    from intake import account
    calls = []

    def service(method, url, headers, body, timeout):
        calls.append((method, url.split("accounts.test", 1)[1], headers, body))
        path = calls[-1][1]
        if path == "/device/start":
            return 200, {"device_code": "dc-1", "user_code": "ABCD-EFGH",
                         "verification_uri": "https://accounts.test/device",
                         "verification_uri_complete": "https://accounts.test/device?code=ABCD-EFGH",
                         "expires_in": 900, "interval": 5}
        if path == "/me":
            return (200, {"account": {"email": "me@example.com"}}) if headers.get("Authorization") == "Bearer syd_t" \
                else (401, {"error": "invalid_token"})
        if path == "/device/revoke":
            return 200, {"ok": True}
        raise AssertionError(path)

    account.transport = service
    account.forget()
    account.cancel_claim()
    config.ACCOUNTS_URL = "off"
    try:
        with gui.app.test_client() as client:
            assert client.get("/api/account").get_json() == {"enabled": False, "signed_in": False}
            assert client.get("/api/status").get_json()["account"] == {"enabled": False, "signed_in": False}
            assert client.post("/api/account/claim", json={}).status_code == 503

            config.ACCOUNTS_URL = "https://accounts.test"
            a = client.get("/api/account").get_json()
            assert a["enabled"] and not a["signed_in"] and not a["claim"]["running"], a
            assert calls == [], "loading the card with no account makes no request"

            res = client.post("/api/account/claim", json={"name": "Test Mac"})
            assert res.status_code == 200, res.get_data(as_text=True)
            out = res.get_json()
            assert out["user_code"] == "ABCD-EFGH" and "device_code" not in out, out
            assert calls[-1][3] == {"name": "Test Mac", "profile": "syllabus"}, calls[-1]
            a = client.get("/api/account").get_json()
            assert a["claim"]["running"] and a["claim"]["user_code"] == "ABCD-EFGH", a
            assert client.post("/api/account/claim", json={}).status_code == 409, "one claim at a time"
            assert client.post("/api/account/cancel", json={}).status_code == 200
            assert not client.get("/api/account").get_json()["claim"]["running"]

            # Signed in: the status poll reads the file only, the card confirms with the service.
            account.save(account.Account("syd_t", "a1", "me@example.com", "Me", "d1", "Test Mac",
                                         "syllabus", "https://accounts.test", "x"))
            before = len(calls)
            st = client.get("/api/status").get_json()["account"]
            assert st["signed_in"] and st["email"] == "me@example.com" and "token" not in st, st
            assert len(calls) == before, "the dashboard poll never touches the network"
            a = client.get("/api/account").get_json()
            assert a["signed_in"] and a["check"] == {"state": "ok", "detail": "me@example.com"}, a
            assert "token" not in json.dumps(a), "the token never reaches the page"

            # Removed from the account page elsewhere: the next load signs this Mac out.
            account.save(account.Account("syd_gone", "a1", "me@example.com", "Me", "d1", "Test Mac",
                                         "syllabus", "https://accounts.test", "x"))
            a = client.get("/api/account").get_json()
            assert not a["signed_in"] and a["check"]["state"] == "revoked", a
            assert account.load() is None

            account.save(account.Account("syd_t", "a1", "me@example.com", "Me", "d1", "Test Mac",
                                         "syllabus", "https://accounts.test", "x"))
            assert client.post("/api/account/signout", json={}).status_code == 200
            assert account.load() is None and calls[-1][1] == "/device/revoke"
    finally:
        account.cancel_claim()
        account.forget()
        config.ACCOUNTS_URL = "off"
results.append(run("the Setup page claims this Mac into an account and signs it out", t35))


def t36():
    """The panel has no sign-in of its own any more: the tunnel's road is gone.

    What replaced it is the relay (t?? in test_relay.py), where the account
    service is the sign-in. All that is asserted here is that the old road
    is really closed and did not leave a hole behind it.
    """
    from intake import account, signin
    account.cancel_claim()
    config.ACCOUNTS_URL = "https://accounts.test"
    try:
        account.forget()
        with gui.app.test_client() as client:
            # The tunnel's routes are gone, not merely refused.
            for path in ("/login", "/account/callback?state=x&code=y", "/logout"):
                assert client.get(path).status_code == 404, (path, client.get(path).status_code)

            # Cf-Ray used to stand the Host check down, so that the tunnel's
            # own hostname could reach the panel. Nothing does that now: a
            # request addressed to a name that is not this Mac's is refused
            # whoever forwarded it.
            for headers in ({"Host": "syllabus.maincoursemedia.com"},
                            {"Host": "syllabus.maincoursemedia.com", "Cf-Ray": "8a1b2c3d4e5f-DFW"}):
                res = client.get("/api/status", headers=headers)
                assert res.status_code == 403, (headers, res.status_code)
                assert "somebody else's name for it" in res.get_json()["error"], res.get_json()

            # This Mac is never gated, with or without an account.
            assert client.get("/api/status").status_code == 200
            assert client.get("/api/status").get_json()["signed_in_as"] == ""
            account.save(account.Account("syd_t", "a1", "owner@example.com", "Owner", "d1", "Test Mac",
                                         "syllabus", "https://accounts.test", "x"))
            assert client.get("/api/status").status_code == 200

            # Nothing to say about a web sign-in that no longer exists, until
            # .env still carries its settings.
            assert doctor.check_web_signin() is None
            config.ENV_FILE.write_text("OPENAI_API_KEY=x\nPANEL_PUBLIC_URL=https://panel.example.com\n")
            check = doctor.check_web_signin()
            assert check is not None and "PANEL_PUBLIC_URL" in check.detail, check
            assert "can be deleted" in check.detail, check
            config.ENV_FILE.unlink()
    finally:
        account.forget()
        config.ACCOUNTS_URL = "off"
results.append(run("the panel's own web sign-in is gone, and left no hole behind it", t36))


def t37():
    """The Setup page's Drive card and button, when this Mac is signed in to an account."""
    from intake import account
    grant = {"connected": False}

    def service(method, url, headers, body, timeout):
        path = url.split("accounts.test", 1)[1]
        if path == "/me":
            return 200, {"account": {"email": "me@example.com"}}
        if path == "/drive/status":
            return 200, {"connected": grant["connected"], "google_email": "me@gmail.com" if grant["connected"] else ""}
        if path == "/settings/schedule":
            return 404, {"error": "not_found"}
        raise AssertionError(path)

    account.transport = service
    account.forget_drive_token()
    config.ACCOUNTS_URL = "https://accounts.test"
    config.TOKEN_FILE = tmp / "no-token.json"
    try:
        with gui.app.test_client() as client:
            # Not signed in: the Drive card is as it always was.
            account.forget()
            d = client.get("/api/setup").get_json()["drive"]
            assert d["account"] == {"available": False} and not d["connected"], d

            account.save(account.Account("syd_t", "a1", "me@example.com", "Me", "d1", "Test Mac",
                                         "syllabus", "https://accounts.test", "x"))
            d = client.get("/api/setup").get_json()["drive"]
            assert d["account"]["available"] and not d["account"]["connected"], d
            assert d["account"]["connect_url"] == "https://accounts.test/drive/connect", d
            assert client.get("/api/status").get_json()["integrations"]["drive"] is False

            # The button sends the browser to the account page instead of running the local flow.
            res = client.post("/api/drive/login", json={})
            assert res.status_code == 200 and res.get_json()["open"] == "https://accounts.test/drive/connect", res.get_json()
            assert not gui._drive_login["running"], "no local OAuth flow was started"

            grant["connected"] = True
            d = client.get("/api/setup").get_json()["drive"]
            assert d["account"]["connected"] and d["account"]["google_email"] == "me@gmail.com", d
            # The status poll reads the cached answer, never the network.
            assert client.get("/api/status").get_json()["integrations"]["drive"] is True
            check = doctor.check_drive_token()
            assert check.ok and "through the Syllabus account" in check.detail and "me@gmail.com" in check.detail, check
    finally:
        account.forget()
        account.forget_drive_token()
        config.ACCOUNTS_URL = "off"
results.append(run("with an account, the Drive card connects through the account page", t37))


def t38():
    """The panel and the service spawn this program the way it was started."""
    from intake import profiles, service
    assert config.FROZEN is False, "tests run from a checkout"
    assert gui.watcher_command() == [sys.executable, "-m", "intake.cli",
                                     "--profile", "syllabus", "watch"]
    assert config.program_cwd() == config.CODE_ROOT
    assert service.program()[-2:] == ["panel", "--no-browser"]
    assert service.program(profiles.SOUS)[3:5] == ["--profile", "sous"]

    # Inside Syllabus.app the binary is the command: no -m, no module name,
    # and nothing to run from but the home directory.
    config.FROZEN = True
    try:
        assert gui.watcher_command() == [sys.executable, "--profile", "syllabus", "watch"]
        assert config.program_cwd() == config.HOME_DIR
        # The app's agent starts the app itself, hidden in the menu bar, and
        # launchd restarts it only after a crash, so Quit stays quit.
        assert service.program() == [sys.executable, "--profile", "syllabus",
                                     "app", "--hidden"]
        assert service.keep_alive() == {"SuccessfulExit": False}
    finally:
        config.FROZEN = False
    assert service.keep_alive() is True
results.append(run("watcher and agent commands, from a checkout and frozen", t38))


# Everything the panel can be made to do, and what a page on another site
# gets when it tries. The panel has no login for local requests by design,
# which is fine for a process running as this user and was not fine for a web
# page: a plain form post needs nobody's permission and the side effect
# happens whether or not the answer can be read.
MUTATIONS = ["/api/record/start", "/api/record/stop", "/api/watcher/start",
             "/api/watcher/stop", "/api/login-item", "/api/drive/login",
             "/api/drive/disconnect", "/api/account/claim", "/api/account/cancel",
             "/api/account/signout", "/api/setup", "/api/window/show"]


def t39():
    # The audit's own reproduction, then the same thing against every other
    # route that changes something. It was one finding about signout; it was
    # nine routes, including one that turns on the microphone and one that
    # spawns a detached process.
    point_config_at(setup_home)
    for path in MUTATIONS:
        res = client.post(path, data="x=1",
                          content_type="application/x-www-form-urlencoded",
                          headers={"Origin": "https://untrusted.invalid",
                                   "Sec-Fetch-Site": "cross-site"})
        assert res.status_code == 403, f"{path} answered {res.status_code}"
        assert "Refused" in res.get_json()["error"], (path, res.get_json())
results.append(run("no route can be driven by a form from another site", t39))


def t40():
    # Three checks, each enough on its own, because each fails differently:
    # a browser that stops sending Origin, one that never sent
    # Sec-Fetch-Site, and a form post that carries neither.
    point_config_at(setup_home)
    alone = [
        ("a foreign Origin with a JSON body",
         dict(json={}, headers={"Origin": "https://untrusted.invalid"})),
        ("a form body with no Origin at all",
         dict(data="x=1", content_type="application/x-www-form-urlencoded")),
        ("Sec-Fetch-Site saying cross-site",
         dict(json={}, headers={"Sec-Fetch-Site": "cross-site"})),
        ("a text/plain body, which needs no permission either",
         dict(data="{}", content_type="text/plain")),
    ]
    for label, kwargs in alone:
        res = client.post("/api/account/signout", **kwargs)
        assert res.status_code == 403, f"{label}: {res.status_code}"
results.append(run("each cross-site check refuses on its own", t40))


def t41():
    # DNS rebinding: a page whose own hostname resolves to this Mac is
    # same-origin with the panel as far as the browser is concerned, so it
    # can read answers as well as send requests. The Host header is what
    # gives it away, and this one applies to reads too.
    point_config_at(setup_home)
    rebound = client.get("/api/status", headers={"Host": "evil.example"})
    assert rebound.status_code == 403, f"evil.example answered {rebound.status_code}"
    for ours in ("127.0.0.1:5173", "localhost:5173", "localhost", "[::1]:5173"):
        res = client.get("/api/status", headers={"Host": ours})
        assert res.status_code == 200, f"{ours} answered {res.status_code}"
    # Behind a proxy the name is the operator's, so that one check stands
    # down. The checks that do not depend on the name must not.
    from intake import signin
    signin.EXPOSED = True
    try:
        proxied = client.get("/api/status", headers={"Host": "panel.example"})
        assert proxied.status_code == 200, f"exposed answered {proxied.status_code}"
        res = client.post("/api/account/signout", data="x=1",
                          content_type="application/x-www-form-urlencoded",
                          headers={"Host": "panel.example"})
        assert res.status_code == 403, "exposing the panel must not unlock form posts"
    finally:
        signin.EXPOSED = False
results.append(run("a request addressed to somebody else's hostname is refused", t41))


def t42():
    # What must keep working: the panel's own pages, and the clients that are
    # not browsers at all and so cannot be aimed at us by a web page.
    point_config_at(setup_home)
    same = client.post("/api/account/signout", json={},
                       headers={"Origin": "http://localhost",
                                "Sec-Fetch-Site": "same-origin"})
    assert same.status_code == 200, same.get_json()
    assert client.post("/api/account/signout", json={}).status_code == 200
    assert client.get("/api/status").status_code == 200
results.append(run("the panel's own page and the CLI are unaffected", t42))


def t43():
    # Two clients pressing Record together. The panel serves the local browser
    # with threaded=True and the relay runs relayed requests against this same
    # app from a pool of four, so a phone on the web and this Mac's browser
    # are two genuine clients. Both guards that existed were checks without
    # mutual exclusion, so both starts passed both of them: with real
    # subprocesses that was two live captures on one microphone, and the panel
    # could only ever stop one of them.
    import os
    import signal
    import subprocess
    import threading
    point_config_at(setup_home)
    rec_mod = gui.recording
    real_cmd = rec_mod._ffmpeg_command
    started = []

    def stub(device_index, destination, limit):
        destination.write_text("AUDIO")
        started.append(destination)
        return ["sleep", "20"]

    def _our_sleeps():
        # Only this process's children. A pgrep across the whole machine
        # counted, and then SIGKILLed, any `sleep 20` anyone else was running,
        # which on a machine running two suites at once is a failure here and
        # a killed process somewhere else.
        return subprocess.run(["pgrep", "-P", str(os.getpid()), "-f", "^sleep 20"],
                              capture_output=True, text=True).stdout.split()

    rec_mod._ffmpeg_command = stub
    gui._recorder = None
    config.RECORDING_STATE_FILE.unlink(missing_ok=True)
    codes = []
    barrier = threading.Barrier(2)

    def fire():
        barrier.wait()
        codes.append(client.post("/api/record/start", json={}).status_code)

    try:
        threads = [threading.Thread(target=fire) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(codes) == [200, 409], f"both starts were accepted: {codes}"
        assert len(started) == 1, f"{len(started)} capture processes were opened"
        live = _our_sleeps()
        assert len(live) <= 1, f"{len(live)} captures left running: {live}"
    finally:
        for pid in _our_sleeps():
            try:
                os.kill(int(pid), signal.SIGKILL)
            except (OSError, ValueError):
                pass
        rec_mod._ffmpeg_command = real_cmd
        gui._recorder = None
        config.RECORDING_STATE_FILE.unlink(missing_ok=True)
results.append(run("two clients pressing Record open one microphone, not two", t43))


def t44():
    # The dashboard told a signed-in Mac with Drive connected "Nothing is set
    # up yet ... to add your keys and class schedule", then linked to a page
    # that says it needs no keys of its own and hides the fields. Both halves
    # of the sentence were wrong; only the schedule was missing.
    from intake import account
    home = tmp / "needs-home"
    home.mkdir()
    point_config_at(home)
    try:
        assert gui._setup_needs() == ["API keys", "a readable class schedule"], \
            gui._setup_needs()

        config.SCHEDULE_FILE.write_text(
            'classes = [ { day = "Mon", start = 9, course = "ENTR-4306" } ]\n'
            "tolerance_minutes = 45\n")
        config.reload()
        assert gui._setup_needs() == ["API keys"], gui._setup_needs()

        # Signed in: the account provides the keys, so nothing is missing.
        account.save(account.Account(
            token="syd_n", account_id="a1", email="me@example.com", name="Me",
            device_id="d1", device_name="This Mac", profile="syllabus",
            url=config.ACCOUNTS_URL, claimed_at="2026-09-15T00:00:00Z"))
        assert gui._setup_needs() == [], gui._setup_needs()
        assert client.get("/api/status").get_json()["setup_needs"] == []

        # A schedule that exists but cannot be read is not the same as none,
        # and the rolled-up boolean could not tell them apart.
        config.SCHEDULE_FILE.write_text("this is not toml")
        config.reload()
        assert gui._setup_needs() == ["a readable class schedule"], gui._setup_needs()
    finally:
        account.forget()
        point_config_at(setup_home)
results.append(run("the dashboard names what is missing, not everything", t44))


def t45():
    """The page can find out what is left without the status poll going out.

    The poll runs every couple of seconds and is file-only on purpose, so
    this is its own route. What it must never do is make a Mac that keeps
    its own keys wait on a network call it has no account for.
    """
    from intake import account
    home = tmp / "allowance-home"
    home.mkdir()
    point_config_at(home)
    asked = []
    real = account.transport
    try:
        # No account: answered from here, and nothing leaves the Mac.
        account.transport = lambda *a, **k: asked.append(a) or (200, {})
        body = client.get("/api/allowance").get_json()
        assert body["ok"] is False, body
        assert not asked, "a Mac with no account still called the service"

        account.save(account.Account(
            token="syd_n", account_id="a1", email="me@example.com", name="Me",
            device_id="d1", device_name="This Mac", profile="syllabus",
            url=config.ACCOUNTS_URL, claimed_at="2026-09-15T00:00:00Z"))
        left = {"period": "2026-09", "source": "trial",
                "audio_seconds": {"used": 11915, "allowance": 18000, "left": 6085},
                "summary_tokens": {"used": 123427, "allowance": 150000, "left": 26573},
                "recordable_seconds": 3524}
        account.transport = lambda method, url, headers, body, timeout: (
            asked.append((method, url, headers.get("Authorization"))) or (200, left))
        body = client.get("/api/allowance").get_json()
        assert body["ok"] is True, body
        assert body["recordable_seconds"] == 3524, body
        assert asked[-1][:2] == ("GET", config.ACCOUNTS_URL + "/proxy/usage"), asked[-1]
        assert asked[-1][2] == "Bearer syd_n", "the device token is what identifies the Mac"

        # A service that cannot be reached is not an allowance of zero, and
        # the page is told the difference so it can stay quiet.
        def boom(*a, **k):
            raise OSError("connection refused")
        account.transport = boom
        body = client.get("/api/allowance").get_json()
        assert body["ok"] is False and "OSError" in body["error"], body
    finally:
        account.transport = real
        account.forget()
        point_config_at(setup_home)
results.append(run("the page can ask what is left, and a failure is not a zero", t45))

def t46():
    # A class the professor called off is not a lapse in the habit the
    # streak measures, so the panel lets the student say so.
    from _test_home import SAMPLE_SCHEDULE
    home = tmp / "canceled-home"
    home.mkdir()
    (home / "schedule.toml").write_text(SAMPLE_SCHEDULE)
    point_config_at(home)
    log = home / "pipeline.log"
    log.write_text("\t".join([
        "2026-09-08T15:36:18", "ACCT-4321", "lecture.m4a",
        "ACCT-4321_2026-09-08_Job-Order-Costing", "https://drive.test/a"]) + "\n")
    config.LOG_FILE = log
    try:
        before = client.get("/api/status").get_json()["insights"]["totals"]
        # Tuesday's ACCT-4321 was recorded, so there is nothing to excuse.
        res = client.post("/api/class/cancel",
                          json={"course": "ACCT-4321", "date": "2026-09-08"})
        assert res.status_code == 400, res.get_json()
        assert "already filed" in res.get_json()["error"], res.get_json()

        # A class that is not on the schedule that day cannot be called off.
        res = client.post("/api/class/cancel",
                          json={"course": "ACCT-4321", "date": "2026-09-07"})
        assert res.status_code == 400 and "does not meet" in res.get_json()["error"], res.get_json()
        res = client.post("/api/class/cancel",
                          json={"course": "ACCT-4321", "date": "the 9th"})
        assert res.status_code == 400 and "not a date" in res.get_json()["error"], res.get_json()

        # Thursday's did not meet. It is written down, and repeating it is
        # not an error or a second row.
        for _ in range(2):
            res = client.post("/api/class/cancel",
                              json={"course": "ACCT-4321", "date": "2026-09-10",
                                    "note": "  campus   closed  "})
            assert res.status_code == 200, res.get_json()
        rows = cancellations.load()
        assert len(rows) == 1, rows
        assert rows[0]["course"] == "ACCT-4321" and rows[0]["date"] == "2026-09-10", rows
        assert rows[0]["note"] == "campus closed", "the note is stored as one line"
        assert rows[0]["when"], "a cancellation records when it was made"

        # The dashboard counts it as excused rather than missed: one fewer
        # class due, and the same number covered. (The week grid always
        # draws the current week, so what it looks like for a date in the
        # past is test_insights' business, not this one's.)
        after = client.get("/api/status").get_json()["insights"]["totals"]
        assert after["canceled"] == 1, after
        assert after["due"] == before["due"] - 1, (before, after)
        assert after["covered"] == before["covered"], (before, after)

        # And it can be taken back, twice over, without an error.
        for _ in range(2):
            res = client.post("/api/class/restore",
                              json={"course": "ACCT-4321", "date": "2026-09-10"})
            assert res.status_code == 200, res.get_json()
        assert cancellations.load() == [], cancellations.load()
    finally:
        point_config_at(setup_home)
results.append(run("a class that did not meet can be excused, and un-excused", t46))


def t47():
    # The file behind it is the only stored state the dashboard has. A
    # damaged one must cost the streak, not the whole panel.
    home = tmp / "canceled-broken"
    home.mkdir()
    point_config_at(home)
    try:
        for junk in ("", "not json", "{}", '[1, "two", {"course": "X"}]',
                     '[{"course": "ACCT-4321", "date": "never"}]'):
            config.CANCELED_FILE.write_text(junk)
            assert cancellations.load() == [], junk
            assert client.get("/api/status").status_code == 200, junk
    finally:
        point_config_at(setup_home)
results.append(run("a damaged cancellations file is read as none, not as a crash", t47))


# --- panel.log rotation ----------------------------------------------------
#
# launchd appends the panel's stdout and stderr to panel.log and nothing ever
# trimmed it: one read was 3.7 MB and 54,000 lines.

from intake import logfiles  # noqa: E402


def t48():
    where = tmp / "rotation"
    where.mkdir()
    log = where / "panel.log"
    log.write_text("small\n")
    assert logfiles.rotate(log, max_bytes=100, backups=2) is False
    assert log.read_text() == "small\n" and not (where / "panel.log.1").exists()

    # Past the limit it moves aside, and the older copies shift down.
    for generation in ("first", "second", "third"):
        log.write_text(generation * 50)
        assert logfiles.rotate(log, max_bytes=100, backups=2) is True, generation
        assert not log.exists(), "the oversized log was left in place"
    assert (where / "panel.log.1").read_text().startswith("third")
    assert (where / "panel.log.2").read_text().startswith("second")
    assert not (where / "panel.log.3").exists(), "kept more copies than asked"
    # A log that is not there is not an error.
    assert logfiles.rotate(where / "missing.log", max_bytes=1) is False
results.append(run("panel.log rotates by size and keeps a bounded number of copies", t48))


def t49():
    # The launchd case: stdout and stderr ARE panel.log. After rotating, what
    # the process prints next must land in a fresh panel.log, not keep
    # filling the renamed copy.
    import subprocess
    where = tmp / "rotation-stdio"
    where.mkdir()
    log = where / "panel.log"
    log.write_text("x" * 500 + "\n")
    script = (
        "import sys; sys.path.insert(0, sys.argv[2]); from pathlib import Path\n"
        "from intake import logfiles\n"
        "print('before', flush=True)\n"
        "assert logfiles.rotate(Path(sys.argv[1]), max_bytes=100, backups=1)\n"
        "print('after', flush=True)\n"
        "print('after on stderr', file=sys.stderr, flush=True)\n"
    )
    with log.open("a") as handle:
        subprocess.run([sys.executable, "-c", script, str(log),
                        str(Path(__file__).resolve().parent)],
                       stdout=handle, stderr=handle, stdin=subprocess.DEVNULL,
                       check=True, timeout=60)
    old = (where / "panel.log.1").read_text()
    new = log.read_text()
    assert "before" in old and "after" not in old, old[-80:]
    assert new == "after\nafter on stderr\n", new
results.append(run("after rotating, a launchd-redirected stdout writes the fresh log", t49))


def t50():
    # The panel trims its log on the way up, in both `intake panel` and the
    # app, which share prepare().
    seen = []
    saved = (logfiles.keep_trimmed, gui.sync.sync_later, gui.relay.start,
             gui.updates.check_later)
    logfiles.keep_trimmed = lambda path, *a, **k: seen.append(path)
    gui.sync.sync_later = lambda *a, **k: None
    gui.relay.start = lambda *a, **k: None
    gui.updates.check_later = lambda *a, **k: None
    try:
        gui.prepare("127.0.0.1", 18765)
    finally:
        (logfiles.keep_trimmed, gui.sync.sync_later, gui.relay.start,
         gui.updates.check_later) = saved
    assert seen == [config.HOME_DIR / "panel.log"], seen
    # And the file it trims is the one launchd writes.
    from intake import service
    assert service.plist()["StandardOutPath"] == str(seen[0])
results.append(run("the panel trims panel.log as it starts", t50))


def t51():
    # Something other than this process rotates panel.log away (a second
    # `intake panel` from a terminal trims on its way up, then finds the port
    # taken). The running panel must come back to the fresh panel.log rather
    # than fill panel.log.1 forever, and the file it makes is private.
    import stat
    import subprocess
    where = tmp / "rotation-elsewhere"
    where.mkdir()
    log = where / "panel.log"
    log.write_text("")
    script = (
        "import os, sys, time; sys.path.insert(0, sys.argv[2]); from pathlib import Path\n"
        "from intake import logfiles\n"
        "log = Path(sys.argv[1])\n"
        "logfiles.keep_trimmed(log, max_bytes=10**9, every=0.05)\n"
        "print('before', flush=True)\n"
        "os.replace(log, log.with_name('panel.log.1'))\n"
        "deadline = time.monotonic() + 20\n"
        "while not log.exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.05)\n"
        "print('after', flush=True)\n"
        "print('after on stderr', file=sys.stderr, flush=True)\n"
    )
    with log.open("a") as handle:
        subprocess.run([sys.executable, "-c", script, str(log),
                        str(Path(__file__).resolve().parent)],
                       stdout=handle, stderr=handle, stdin=subprocess.DEVNULL,
                       check=True, timeout=60)
    old = (where / "panel.log.1").read_text()
    assert old == "before\n", old
    assert log.exists(), "the panel never came back to panel.log"
    assert log.read_text() == "after\nafter on stderr\n", log.read_text()
    assert stat.S_IMODE(log.stat().st_mode) & 0o077 == 0, oct(log.stat().st_mode)
results.append(run("a panel.log rotated by another process is reclaimed, privately", t51))


def t52():
    # There is no permission click-through before recording (removed in
    # 0.6.0; the Terms carry the user's responsibility for consent). In a home
    # with nothing acknowledged, Record starts straight away, Setup saves
    # without any permission field, neither payload carries one, the route
    # that took the acknowledgment is gone, and neither page shows a checkbox.
    import os
    import signal
    import subprocess
    point_config_at(setup_home)
    assert not (setup_home / "consent.json").exists()
    real_cmd = gui.recording._ffmpeg_command
    launched = []

    def stub(device_index, destination, limit):
        destination.write_text("AUDIO")
        launched.append(destination)
        return ["sleep", "20"]

    def _our_sleeps():
        return subprocess.run(["pgrep", "-P", str(os.getpid()), "-f", "^sleep 20"],
                              capture_output=True, text=True).stdout.split()

    gui.recording._ffmpeg_command = stub
    gui._recorder = None
    config.RECORDING_STATE_FILE.unlink(missing_ok=True)
    try:
        res = client.post("/api/record/start", json={})
        assert res.status_code == 200, (res.status_code, res.get_json())
        assert res.get_json()["ok"] is True, res.get_json()
        assert len(launched) == 1, "Record did not open the microphone"

        status = client.get("/api/status").get_json()
        assert "consent" not in status, status.get("consent")
        assert status["recording"]["active"] is True, status["recording"]
        assert "consent" not in client.get("/api/setup").get_json()

        form = {"openai_key": "sk-a", "anthropic_key": "sk-b", "device": "",
                "schedule": [{"day": "Mon", "start": 9, "course": "ENTR-4306"}],
                "notion": {"enabled": False}}
        res = client.post("/api/setup", json=form)
        assert res.status_code == 200, res.get_json()
        assert not (setup_home / "consent.json").exists(), "a save wrote an acknowledgment"

        assert client.post("/api/consent", json={"agree": True}).status_code in (404, 405)
        checks = {c["name"] for c in client.get("/api/doctor").get_json()["checks"]}
        assert "permission to record" not in checks, checks

        for page in ("/", "/setup"):
            html = client.get(page).get_data(as_text=True)
            assert "permission to record" not in html.lower(), f"{page} still asks for permission"
            assert 'id="consent' not in html, f"{page} still has the permission checkbox"
    finally:
        for pid in _our_sleeps():
            try:
                os.kill(int(pid), signal.SIGKILL)
            except (OSError, ValueError):
                pass
        gui.recording._ffmpeg_command = real_cmd
        gui._recorder = None
        config.RECORDING_STATE_FILE.unlink(missing_ok=True)
results.append(run("recording starts with no permission checkbox or acknowledgment anywhere", t52))


def t53():
    import re
    from intake import signin
    seen = set()
    for path in ("/", "/setup", "/api/status", "/nope"):
        res = client.get(path)
        csp = res.headers.get("Content-Security-Policy", "")
        assert csp, f"{path} carries no Content-Security-Policy"
        for want in ("default-src 'none'", "object-src 'none'", "base-uri 'none'",
                     "frame-ancestors 'none'", "connect-src 'self'", "form-action 'self'"):
            assert want in csp, f"{path}: {want!r} missing from {csp}"
        assert "unsafe-inline" not in csp.replace("style-src-attr 'unsafe-inline'", ""), csp
        assert "unsafe-eval" not in csp and "script-src 'self'" not in csp, csp
        nonce = re.search(r"script-src 'nonce-([^']+)'", csp).group(1)
        assert nonce not in seen, "a nonce was reused"
        seen.add(nonce)
        if path in ("/", "/setup"):
            html = res.get_data(as_text=True)
            # Every script and style element carries this response's nonce,
            # and nothing else in the page could run.
            tags = re.findall(r"<(?:script|style)\b[^>]*>", html)
            assert len(tags) >= 3, tags
            assert all(f'nonce="{nonce}"' in t for t in tags), tags
            assert not re.search(r"<[a-z][^>]*\son[a-z]+\s*=", html), "an inline handler"
            assert "javascript:" not in html
            assert "srcdoc" not in html and "<iframe" not in html
    # The policy names only the origins the pages use.
    origins = set(re.findall(r"https://[^\s;']+", signin.content_security_policy("x")))
    assert origins == {"https://fonts.googleapis.com", "https://fonts.gstatic.com"}, origins
    # A refused request still gets a policy of its own.
    refused = client.get("/api/status", headers={"Host": "evil.example"})
    assert refused.status_code == 403 and "Content-Security-Policy" in refused.headers
results.append(run("every panel answer carries a Content-Security-Policy with a fresh nonce that the pages use", t53))


def t54():
    from intake import relay, signin
    want = {k.lower(): v for k, v in signin.STATIC_SECURITY_HEADERS.items()}
    assert want["x-frame-options"] == "DENY" and want["x-content-type-options"] == "nosniff"
    assert want["referrer-policy"] == "same-origin" and want["x-permitted-cross-domain-policies"] == "none"
    for feature in ("camera", "geolocation", "payment", "microphone"):
        assert f"{feature}=()" in want["permissions-policy"], feature
    # 200s, a 404, and a refusal all carry them; the local panel does not send HSTS.
    for path, headers in (("/", {}), ("/setup", {}), ("/api/status", {}), ("/nope", {}),
                          ("/api/status", {"Host": "evil.example"})):
        res = client.get(path, headers=headers)
        for name, value in want.items():
            assert res.headers.get(name) == value, f"{path}: {name} is {res.headers.get(name)!r}"
        assert "Content-Security-Policy" in res.headers, "the CSP was clobbered"
        assert "Strict-Transport-Security" not in res.headers, "HSTS on plain http"
    # Every header set here is on the relay's whitelist, or the relay strips it.
    for name in list(want) + ["strict-transport-security", "content-security-policy"]:
        assert name in relay.RESPONSE_HEADERS, name
results.append(run("every panel answer carries the security headers, local ones without HSTS", t54))


def t55():
    from intake import signin
    res = client.get("/x", headers={"Host": "<b>x</b>.example"})
    assert res.status_code == 403
    assert b"<b>" not in res.data, res.data
    assert signin.html.escape("<") == "&lt;"
results.append(run("a refusal page escapes the host it names", t55))


print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
