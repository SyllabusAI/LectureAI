"""Tests for intake/app.py and packaging/entry.py: what a launch decides to do.

No window is opened, no server is started by the app itself. A panel is
stood up on an ephemeral port only to check that the probe recognizes one.
From the project root:

    .venv/bin/python test_app.py
"""
import socket
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "packaging"))

from _test_home import fresh_home  # noqa: E402

fresh_home()  # before config is imported, so nothing touches ~/.intake

from werkzeug.serving import make_server  # noqa: E402

import entry  # noqa: E402  packaging/entry.py
from intake import app, gui  # noqa: E402


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


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_probe_finds_a_panel():
    server = make_server("127.0.0.1", 0, gui.app, threaded=True)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status = app.probe(port)
        assert status is not None, "a running panel was not recognized"
        assert status["configured"] is False, "the test home has no keys"
        assert app.page_for(status) == "setup"
        action, url = app.plan(port)
        assert action == "attach", action
        assert url == f"http://127.0.0.1:{port}/setup", url
    finally:
        server.shutdown()


def test_probe_ignores_a_closed_port():
    port = free_port()
    assert app.probe(port) is None
    action, detail = app.plan(port)
    assert action == "serve", (action, detail)


def test_probe_ignores_something_else():
    # A listener that answers nothing a panel would.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(5)
        port = s.getsockname()[1]
        assert app.probe(port, timeout=0.5) is None
        action, detail = app.plan(port)
        assert action == "busy", action
        assert str(port) in detail and "--port" in detail, detail


def test_page_for():
    assert app.page_for(None) == "setup"
    assert app.page_for({"configured": False}) == "setup"
    assert app.page_for({"configured": True}) == ""


def test_title_and_size():
    assert app.title() == "Syllabus"
    assert app.WINDOW_SIZE[0] >= app.MIN_SIZE[0]
    assert app.WINDOW_SIZE[1] >= app.MIN_SIZE[1]


def test_entry_dispatch():
    seen = {}

    def fake(name, code):
        def call(args):
            seen[name] = args
            return code
        return call

    code = entry.main([], run_cli=fake("cli", 9), run_app=fake("app", 3))
    assert seen == {"app": []}, seen
    assert code == 3, code

    seen.clear()
    code = entry.main(["doctor", "--help"], run_cli=fake("cli", 7), run_app=fake("app", 3))
    assert seen == {"cli": ["doctor", "--help"]}, seen
    assert code == 7, code


def test_entry_pins_the_profile():
    import os
    from intake import profiles
    os.environ.pop(profiles.PROFILE_ENV_VAR, None)
    entry.main([], run_app=lambda a: 0)
    assert os.environ[profiles.PROFILE_ENV_VAR] == "syllabus"


def test_show_when_the_app_is_already_running():
    """A second copy finds a panel with a window and asks for it, not for a second one."""
    fake = lambda port, host: {"watcher": {}, "configured": True, "window": True}
    action, url = app.plan(5173, probe_panel=fake, port_busy=lambda p: True)
    assert action == "show" and url == "http://127.0.0.1:5173/", (action, url)
    fake = lambda port, host: {"watcher": {}, "configured": True, "window": False}
    assert app.plan(5173, probe_panel=fake, port_busy=lambda p: True)[0] == "attach"

    # Through the panel itself: the hook registered by the resident app.
    gui.window_hooks.clear()
    client = gui.app.test_client()
    assert client.get("/api/status").get_json()["window"] is False
    res = client.post("/api/window/show", json={})
    assert res.status_code == 409, res.get_json()
    shown = []
    gui.window_hooks["show"] = lambda: shown.append(True)
    try:
        assert client.get("/api/status").get_json()["window"] is True
        assert client.post("/api/window/show", json={}).get_json() == {"ok": True}
        assert shown == [True]
        # Over a real socket, as the second copy does it.
        server = make_server("127.0.0.1", 0, gui.app, threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            assert app.ask_to_show(server.server_port) is True
            assert shown == [True, True]
        finally:
            server.shutdown()
    finally:
        gui.window_hooks.clear()
    assert app.ask_to_show(free_port()) is False


def test_recording_line():
    assert app.recording_line(None) == "Not recording"
    assert app.recording_line({"recording": {"active": False}}) == "Not recording"
    line = app.recording_line({"recording": {"active": True, "course": "ACCT-4321", "elapsed": 723.4}})
    assert line == "Recording ACCT-4321 · 12:03", line
    line = app.recording_line({"recording": {"active": True, "course": "", "elapsed": 5, "stalled": True}})
    assert line == "Recording · 0:05 · the microphone is silent", line


def test_menu_items():
    def titles(items):
        return [i["title"] for i in items]

    idle = {"recording": {"active": False}, "configured": True, "now_class": "ACCT-4321"}
    items = app.menu_items(idle, attached=False, login_installed=True)
    assert titles(items) == ["Open Syllabus", "-", "Not recording", "Record ACCT-4321", "-",
                             "Start Syllabus when I log in", "-", "Quit Syllabus"], titles(items)
    by_action = {i["action"]: i for i in items if i["action"]}
    assert by_action["login_toggle"]["checked"] is True
    assert by_action["record_start"]["enabled"] is True

    busy = {"recording": {"active": True, "course": "RELI-3304", "elapsed": 61}, "configured": True}
    items = app.menu_items(busy, attached=False, login_installed=False)
    assert "Stop Recording" in titles(items) and "Recording RELI-3304 · 1:01" in titles(items)
    assert {i["action"]: i for i in items if i["action"]}["login_toggle"]["checked"] is False

    # Not configured yet: recording cannot start. No class now: a plain label.
    fresh = {"recording": {"active": False}, "configured": False}
    items = app.menu_items(fresh, attached=False, login_installed=False)
    start = {i["action"]: i for i in items if i["action"]}["record_start"]
    assert start["title"] == "Start Recording" and start["enabled"] is False

    # Attached to a Terminal install's panel: offer to take over, no login toggle.
    items = app.menu_items(idle, attached=True, login_installed=False)
    actions = [i["action"] for i in items if i["action"] and i["action"] != "-"]
    assert "takeover" in actions and "login_toggle" not in actions, actions

    # A newer app is out: a download line, in the app only.
    newer = {**idle, "update": {"available": True, "version": "0.9.0", "how": "download", "url": "u"}}
    items = app.menu_items(newer, attached=False, login_installed=False)
    assert "Download Syllabus 0.9.0…" in titles(items), titles(items)
    items = app.menu_items({**newer, "update": {**newer["update"], "how": "pipx"}}, attached=False, login_installed=False)
    assert not any(i["action"] == "update" for i in items), "pipx installs are told on the page instead"

    # No permission to record on file: the recorder would refuse, so the menu
    # opens the dashboard, where the question is, instead of failing quietly.
    unconfirmed = {**idle, "consent": {"given": False}}
    items = app.menu_items(unconfirmed, attached=False, login_installed=False)
    assert "Confirm Permission to Record…" in titles(items), titles(items)
    assert not any(i["action"] == "record_start" for i in items), titles(items)
    confirm = next(i for i in items if i["title"] == "Confirm Permission to Record…")
    assert confirm["action"] == "open" and confirm["enabled"] is True, confirm
    items = app.menu_items({**idle, "consent": {"given": True}}, attached=False, login_installed=False)
    assert "Record ACCT-4321" in titles(items), titles(items)

    # The panel is gone: say so, and do not offer to record.
    items = app.menu_items(None, attached=False, login_installed=False)
    assert "The panel is not answering" in titles(items)
    assert {i["action"]: i for i in items if i["action"]}["record_start"]["enabled"] is False


def test_takeover_moves_the_agent():
    """Stop the Terminal install's agent, wait for its port to free, install ours."""
    from intake import service
    said, calls = [], []
    port = free_port()

    class Done:
        returncode, stdout, stderr = 0, "", ""

    def fake_run(cmd, **kw):
        calls.append(cmd[:2])
        return Done()

    real = (service.AGENTS_DIR, service.port_answers, app.ask_to_show)
    with tempfile.TemporaryDirectory() as tmp:
        agents = Path(tmp) / "LaunchAgents"
        agents.mkdir()
        service.AGENTS_DIR = agents
        service.plist_path.__defaults__ = (agents,)
        service.install.__defaults__ = (print, __import__("subprocess").run, agents, 10.0)
        service.uninstall.__defaults__ = (print, __import__("subprocess").run, agents)
        # The old panel answers once more, then the port is free, then the
        # new copy of the app, started by launchd, is answering.
        answers = iter([True, False, False])
        service.port_answers = lambda p, timeout=0.5: next(answers, True)
        app.ask_to_show = lambda p, host=app.HOST: True
        # An agent from the Terminal install is there first.
        (agents / "com.maincoursemedia.syllabus.panel.plist").write_bytes(b"<plist/>")
        try:
            rc = app.takeover(port, say=said.append, run=fake_run, wait_free=2)
        finally:
            service.AGENTS_DIR, service.port_answers, app.ask_to_show = real
            service.plist_path.__defaults__ = (service.AGENTS_DIR,)
            service.install.__defaults__ = (print, __import__("subprocess").run, service.AGENTS_DIR, 10.0)
            service.uninstall.__defaults__ = (print, __import__("subprocess").run, service.AGENTS_DIR)
        assert rc == 0, said
        verbs = [c[1] for c in calls]
        assert verbs == ["bootout", "bootout", "bootstrap", "kickstart"], verbs
        import plistlib
        data = plistlib.loads((agents / "com.maincoursemedia.syllabus.panel.plist").read_bytes())
        assert data["ProgramArguments"][0] == sys.executable, data["ProgramArguments"]
        assert any("installed" in line for line in said), said


def test_takeover_gives_up_if_the_old_panel_stays():
    from intake import service
    said, calls = [], []

    class Done:
        returncode, stdout, stderr = 0, "", ""

    real = service.port_answers
    service.port_answers = lambda p, timeout=0.5: True
    with tempfile.TemporaryDirectory() as tmp:
        agents = Path(tmp)
        service.uninstall.__defaults__ = (print, __import__("subprocess").run, agents)
        try:
            rc = app.takeover(5199, say=said.append,
                              run=lambda cmd, **kw: calls.append(cmd) or Done(), wait_free=0.3)
        finally:
            service.port_answers = real
            service.uninstall.__defaults__ = (print, __import__("subprocess").run, service.AGENTS_DIR)
    assert rc == 1 and any("did not stop" in line for line in said), said
    assert all(c[1] != "bootstrap" for c in calls), "must not install over a live panel"


def test_login_item_api():
    from intake import service
    client = gui.app.test_client()
    real = (service.installed, service.runs_this_program, service.install, service.uninstall)
    state = {"installed": False, "ours": True}
    service.installed = lambda profile=None, agents_dir=None: state["installed"]
    service.runs_this_program = lambda profile=None, agents_dir=None: state["installed"] and state["ours"]

    def fake_install(say=print, run=None, agents_dir=None, wait=10.0):
        state["installed"] = True
        say("installed")
        return 0

    def fake_uninstall(say=print, run=None, agents_dir=None):
        state["installed"] = False
        return 0

    service.install, service.uninstall = fake_install, fake_uninstall
    gui.window_hooks.clear()
    try:
        d = client.get("/api/login-item").get_json()
        assert d == {"installed": False, "other": False,
                     "label": "Start the Syllabus panel when I log in"}, d
        d = client.post("/api/login-item", json={"enabled": True}).get_json()
        assert d["ok"] is True and d["installed"] is True, d
        d = client.post("/api/login-item", json={"enabled": False}).get_json()
        assert d["ok"] is True and d["installed"] is False, d
        # Inside the app the label names the app; an agent from another
        # install is reported as "other".
        gui.window_hooks["show"] = lambda: None
        state.update(installed=True, ours=False)
        d = client.get("/api/login-item").get_json()
        assert d["label"] == "Start Syllabus when I log in" and d["other"] is True, d
    finally:
        service.installed, service.runs_this_program, service.install, service.uninstall = real
        gui.window_hooks.clear()


def test_service_reads_the_agent_back():
    from intake import service
    import plistlib
    with tempfile.TemporaryDirectory() as tmp:
        agents = Path(tmp)
        assert service.installed(agents_dir=agents) is False
        assert service.installed_program(agents_dir=agents) is None
        assert service.runs_this_program(agents_dir=agents) is False
        path = agents / "com.maincoursemedia.syllabus.panel.plist"
        path.write_bytes(plistlib.dumps({"ProgramArguments": ["/usr/bin/python3", "-m", "intake.cli"]}))
        assert service.installed(agents_dir=agents) is True
        assert service.installed_program(agents_dir=agents)[0] == "/usr/bin/python3"
        assert service.runs_this_program(agents_dir=agents) is False, "another interpreter's agent"
        path.write_bytes(plistlib.dumps({"ProgramArguments": [sys.executable, "-m", "intake.cli"]}))
        assert service.runs_this_program(agents_dir=agents) is True
        path.write_bytes(b"not a plist")
        assert service.installed_program(agents_dir=agents) is None


def test_menu_controller_under_cocoa():
    """The Objective-C side: the controller initializes and fills a real NSMenu.

    Needs pyobjc (the app extra); skipped where it is missing. No window, no
    status bar item, no run loop: an NSMenu can be built without any of them.
    """
    try:
        from AppKit import NSMenu
    except ImportError:
        print("      (pyobjc not installed; skipped)")
        return
    resident = app.Resident("http://127.0.0.1:5199/", 5199, attached=False, hidden=False)
    controller = app._MenuController.alloc().initWithResident_(resident)
    assert controller is not None and controller.resident is resident
    again = app._MenuController.alloc().initWithResident_(resident)
    assert again is not None, "the class must be defined once and reused"
    resident._controller = controller
    real_probe, real_runs = app.probe, app.service.runs_this_program
    app.probe = lambda port, host=app.HOST, timeout=1.5: {
        "watcher": {}, "configured": True, "now_class": "ACCT-4321",
        "recording": {"active": True, "course": "ACCT-4321", "elapsed": 65}}
    app.service.runs_this_program = lambda profile=None, agents_dir=None: True
    try:
        menu = NSMenu.alloc().initWithTitle_("test")
        controller.menuNeedsUpdate_(menu)
        titles = [menu.itemAtIndex_(i).title() or "-" for i in range(menu.numberOfItems())]
        assert titles == ["Open Syllabus", "-", "Recording ACCT-4321 · 1:05", "Stop Recording", "-",
                          "Start Syllabus when I log in", "-", "Quit Syllabus"], titles
        login = menu.itemAtIndex_(5)
        assert login.state() == 1 and login.target() is controller and login.representedObject() == "login_toggle"
        assert menu.itemAtIndex_(2).isEnabled() is False, "the recording line is information"
    finally:
        app.probe, app.service.runs_this_program = real_probe, real_runs


if __name__ == "__main__":
    results = [
        run("probe recognizes a running panel and attaches to it", test_probe_finds_a_panel),
        run("a closed port means start our own panel", test_probe_ignores_a_closed_port),
        run("a port held by something else is reported, not attached", test_probe_ignores_something_else),
        run("setup page until configured", test_page_for),
        run("window title and size", test_title_and_size),
        run("entry: no arguments is the app, arguments are the command", test_entry_dispatch),
        run("entry pins the syllabus profile", test_entry_pins_the_profile),
        run("a running copy of the app is asked for its window", test_show_when_the_app_is_already_running),
        run("the menu's recording line", test_recording_line),
        run("the menu's items follow the panel's status", test_menu_items),
        run("takeover moves the launch agent to the app", test_takeover_moves_the_agent),
        run("takeover stops if the old panel will not", test_takeover_gives_up_if_the_old_panel_stays),
        run("the Setup page's start-at-login switch", test_login_item_api),
        run("service reads the installed agent back", test_service_reads_the_agent_back),
        run("the menu controller builds a real menu under Cocoa", test_menu_controller_under_cocoa),
    ]
    print(f"\n{sum(results)}/{len(results)} passed")
    sys.exit(0 if all(results) else 1)
