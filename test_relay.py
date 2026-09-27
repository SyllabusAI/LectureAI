"""Tests for relay.py: the panel on the web through the account service.

The socket is a scripted fake, so nothing here reaches the network; the
requests it carries are run against a small echo app and against the real
panel app, both through Flask's test client. From the project root:

    .venv/bin/python test_relay.py
"""
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_home import fresh_home  # noqa: E402

HOME = fresh_home()  # before config is imported

from flask import Flask, jsonify, request  # noqa: E402

from intake import account, config, gui, relay  # noqa: E402


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
SERVICE = "https://accounts.test"
config.ACCOUNTS_URL = SERVICE

ME = account.Account("syd_t", "a1", "me@example.com", "Me", "d1", "Test Mac",
                     "syllabus", SERVICE, "2026-09-14T09:00:00")


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def unb64(frames) -> bytes:
    return b"".join(base64.b64decode(f.get("body") or "") for f in frames)


def req(path, method="GET", query="", body="", headers=None, base="/p/d1",
        viewer=None, rid="r1"):
    return {"t": "req", "id": rid, "method": method, "path": path, "query": query,
            "headers": headers or {}, "base": base,
            "viewer": viewer if viewer is not None else {"email": "Me@Example.com", "account_id": "a1"},
            "body": b64(body) if body else ""}


# --- An echo app: what a relayed request looks like from inside Flask ------------

echo = Flask("echo")


@echo.route("/e/<path:rest>", methods=["GET", "POST"])
def _echo(rest):
    return jsonify({
        "rest": rest, "script_root": request.script_root, "url": request.url,
        "method": request.method, "query": request.query_string.decode(),
        "body": request.get_data().decode(), "cookie": request.headers.get("Cookie"),
        "forwarded": request.headers.get("X-Forwarded-For"), "accept": request.headers.get("Accept"),
        "relay": bool(request.environ.get(relay.RELAY_KEY)),
        "viewer": request.environ.get(relay.VIEWER_KEY),
        "viewer_account": request.environ.get(relay.VIEWER_ACCOUNT_KEY),
        "secure": request.is_secure,
    })


@echo.get("/big")
def _big():
    return "x" * 10_000, 200, {"Content-Type": "text/plain", "X-Internal": "hide me"}


@echo.get("/nothing")
def _nothing():
    return "", 304, {"ETag": '"abc"'}


@echo.get("/boom")
def _boom():
    raise RuntimeError("no")


def t1():
    frames = relay.serve(echo, req("/e/x", query="y=1", headers={
        "Accept": "application/json", "Cookie": "session=stolen", "X-Forwarded-For": "1.2.3.4",
        "X-Request-Id": "abc"}))
    assert len(frames) == 1, frames
    f = frames[0]
    assert f["t"] == "res" and f["id"] == "r1" and f["status"] == 200 and f["more"] is False, f
    assert f["headers"].get("Content-Type", "").startswith("application/json"), f["headers"]
    got = json.loads(unb64(frames))
    assert got["rest"] == "x" and got["query"] == "y=1" and got["method"] == "GET", got
    assert got["script_root"] == "/p/d1", got
    assert got["url"] == "https://accounts.test/p/d1/e/x?y=1", got
    assert got["relay"] is True and got["viewer"] == "me@example.com" and got["viewer_account"] == "a1", got
    assert got["secure"] is True, got
    assert got["cookie"] is None and got["forwarded"] is None, "cookies and proxy headers must not cross"
    assert got["accept"] == "application/json", got
results.append(run("a relayed request runs in process with the base path, the viewer, and only safe headers", t1))


def t2():
    frames = relay.serve(echo, req("/e/post", method="POST", body='{"course":"ACCT-4321"}',
                                   headers={"Content-Type": "application/json"}))
    got = json.loads(unb64(frames))
    assert got["method"] == "POST" and got["body"] == '{"course":"ACCT-4321"}', got
results.append(run("a POST body arrives intact", t2))


def t3():
    frames = relay.serve(echo, req("/big"), chunk_bytes=4096)
    assert [f["t"] for f in frames] == ["res", "chunk", "chunk"], [f["t"] for f in frames]
    assert [f["more"] for f in frames] == [True, True, False], frames
    assert all(f["id"] == "r1" for f in frames)
    assert unb64(frames) == b"x" * 10_000
    assert "X-Internal" not in frames[0]["headers"], frames[0]["headers"]
    assert frames[0]["headers"]["Content-Type"].startswith("text/plain")
    # The floor keeps a silly chunk size from producing thousands of frames.
    assert len(relay.serve(echo, req("/big"), chunk_bytes=1)) == 10, "1 KB floor"
results.append(run("a long answer is split at the chunk size and carries only safe headers", t3))


def t4():
    frames = relay.serve(echo, req("/nothing"))
    assert frames[0]["status"] == 304 and frames[0]["body"] == "" and frames[0]["more"] is False, frames
    assert frames[0]["headers"].get("ETag") == '"abc"', frames[0]["headers"]
    frames = relay.serve(echo, req("//evil"))
    assert frames[0]["status"] == 400, frames
    frames = relay.serve(echo, req("/boom"))
    assert frames[0]["status"] == 500, frames
    frames = relay.serve(echo, {"t": "req", "id": "r9", "body": "%%%not base64%%%", "path": "/e/x"})
    assert frames[0]["status"] == 502 and frames[0]["id"] == "r9", frames
results.append(run("a 304 has no body; a bad path is 400; a failure is an answer, never a dead socket", t4))


# --- Against the real panel -----------------------------------------------------------

def t5():
    account.save(ME)
    try:
        frames = relay.serve(gui.app, req("/api/status"))
        assert frames[0]["status"] == 200, frames[0]
        status = json.loads(unb64(frames))
        assert status["signed_in_as"] == "me@example.com", status["signed_in_as"]
        assert status["signout_url"] == SERVICE + "/logout", status["signout_url"]
        assert status["relay"]["url"] == SERVICE + "/p/d1/", status["relay"]
        assert status["account"]["signed_in"] is True

        frames = relay.serve(gui.app, req("/"))
        html = unb64(frames).decode()
        assert 'const BASE = "/p/d1";' in html, "the page must know its base path"
        assert 'href="/p/d1/setup"' in html and 'src="/p/d1/static/icon.png"' in html, "links under the base"
        assert 'href="/setup"' not in html, "no absolute link may escape the base"

        frames = relay.serve(gui.app, req("/setup"))
        html = unb64(frames).decode()
        assert 'const BASE = "/p/d1";' in html and 'href="/p/d1/"' in html, "the setup page too"

        frames = relay.serve(gui.app, req("/static/icon.png"))
        assert frames[0]["status"] == 200 and frames[0]["headers"]["Content-Type"] == "image/png", frames[0]
        assert len(unb64(frames)) > 1000
    finally:
        account.forget()
results.append(run("the panel answers a relayed request as the viewer, with every link under its address", t5))


def t6():
    account.save(ME)
    try:
        wrong = relay.serve(gui.app, req("/api/status", viewer={"email": "x@example.com", "account_id": "a2"}))
        assert wrong[0]["status"] == 403, wrong[0]
        assert "does not own" in json.loads(unb64(wrong))["error"]
        nobody = relay.serve(gui.app, req("/api/status", viewer={}))
        assert nobody[0]["status"] == 403, nobody[0]
    finally:
        account.forget()
    signed_out = relay.serve(gui.app, req("/api/status"))
    assert signed_out[0]["status"] == 403, signed_out[0]
results.append(run("a viewer the relay names is refused unless they own this Mac's account", t6))


def t7():
    with gui.app.test_client() as client:
        res = client.get("/api/status")
        assert res.status_code == 200
        s = res.get_json()
        assert s["signed_in_as"] == "" and s["signout_url"] == "", s
        assert s["relay"]["state"] == "off" and s["relay"]["url"] == "", s["relay"]
        html = client.get("/").get_data(as_text=True)
        assert 'const BASE = "";' in html and 'href="/setup"' in html, "local pages are unchanged"
        # A forged mark from the network is just a header, not the environ key.
        res = client.get("/api/status", headers={"Intake-Relay": "1", "Intake-Viewer": "me@example.com"})
        assert res.get_json()["signed_in_as"] == ""
results.append(run("a local request is untouched: no viewer, no base path, no relay", t7))


# --- The loop that keeps the socket open -----------------------------------------------

class FakeSocket:
    """One scripted connection. `script` is what happens after the handshake."""

    def __init__(self, log, script):
        self.log = log
        self.script = script
        self.sent = []
        self.closed = False

    def factory(self, url, headers, on_open, on_message, on_close, on_error):
        self.log.append(("connect", url, headers))
        self._cb = (on_open, on_message, on_close, on_error)
        return self

    def run_forever(self):
        on_open, on_message, on_close, on_error = self._cb
        for step, arg in self.script:
            if step == "open":
                on_open()
            elif step == "message":
                on_message(json.dumps(arg))
            elif step == "close":
                on_close(*arg)
            elif step == "error":
                on_error(arg)

    def send(self, text):
        self.sent.append(json.loads(text))

    def close(self):
        self.closed = True


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds


def t8():
    account.save(ME)
    log = []
    sock = FakeSocket(log, [
        ("open", None),
        ("message", {"t": "welcome", "device": "d1", "chunk_bytes": 2048}),
        ("message", req("/e/hi", query="a=b", rid="q1")),
        ("message", {"t": "noise"}),
        ("close", (1006, "")),
    ])
    clock = Clock()
    try:
        r = relay.Relay(echo, socket_factory=sock.factory, sleep=clock.sleep, now=clock.now)
        assert r.status()["state"] == "off"
        r.run(rounds=1)
        assert log[0][1] == "wss://accounts.test/relay/connect", log[0]
        assert log[0][2] == ["Authorization: Bearer syd_t"], log[0]
        assert sock.sent[0]["t"] == "hello" and sock.sent[0]["name"] == "Test Mac", sock.sent[0]
        assert r.chunk_bytes == 2048, r.chunk_bytes
        answer = [f for f in sock.sent if f["t"] == "res"]
        assert len(answer) == 1 and answer[0]["id"] == "q1" and answer[0]["status"] == 200, sock.sent
        got = json.loads(base64.b64decode(answer[0]["body"]))
        assert got["rest"] == "hi" and got["query"] == "a=b" and got["viewer"] == "me@example.com", got
        st = r.status()
        assert st["state"] == "reconnecting" and st["url"] == SERVICE + "/p/d1/", st
        assert st["requests"] == 1 and st["connected_at"], st
        assert "1006" in st["error"], st["error"]
        assert len(clock.slept) == 1 and 1.0 <= clock.slept[0] <= 1.5, clock.slept
    finally:
        account.forget()
results.append(run("the loop connects with the token, says hello, honors the welcome, and answers requests", t8))


def t9():
    account.save(ME)
    clock = Clock()
    log = []
    sock = FakeSocket(log, [("close", (None, None))])
    try:
        r = relay.Relay(echo, socket_factory=sock.factory, sleep=clock.sleep, now=clock.now)
        r.run(rounds=4)
        assert len(log) == 4, log
        bounds = [(1, 1.5), (2, 3), (4, 6), (8, 12)]
        for pause, (lo, hi) in zip(clock.slept, bounds):
            assert lo <= pause <= hi, (clock.slept, bounds)
        assert r.status()["attempts"] == 4, r.status()

        # A connection that held for a while resets the pause.
        clock2 = Clock()

        class Steady(FakeSocket):
            def run_forever(self):
                clock2.t += 120
                super().run_forever()

        sock2 = Steady(log, [("open", None), ("close", (1000, "bye"))])
        r2 = relay.Relay(echo, socket_factory=sock2.factory, sleep=clock2.sleep, now=clock2.now)
        r2.run(rounds=3)
        assert all(1.0 <= p <= 1.5 for p in clock2.slept), clock2.slept
    finally:
        account.forget()
results.append(run("reconnects back off, doubling to a cap, and reset after a steady connection", t9))


def t10():
    account.save(ME)
    clock = Clock()

    class Refused(Exception):
        status_code = 401

    sock = FakeSocket([], [("error", Refused("Handshake status 401"))])
    try:
        r = relay.Relay(echo, socket_factory=sock.factory, sleep=clock.sleep, now=clock.now)
        r.run(rounds=1)
        st = r.status()
        assert st["error"] == "unauthorized", st
        assert clock.slept == [relay.MAX_BACKOFF], clock.slept
    finally:
        account.forget()
results.append(run("a refused token waits the full pause rather than hammering the service", t10))


def t11():
    clock = Clock()
    log = []
    sock = FakeSocket(log, [])
    r = relay.Relay(echo, socket_factory=sock.factory, sleep=clock.sleep, now=clock.now)
    r.run(rounds=2)
    assert log == [], "no account, no connection"
    assert clock.slept == [relay.NO_ACCOUNT_WAIT] * 2, clock.slept
    st = r.status()
    assert st["state"] == "off" and st["url"] == "" and "not signed in" in st["detail"], st

    def missing(*a, **k):
        raise ImportError("No module named 'websocket'")
    account.save(ME)
    try:
        r2 = relay.Relay(echo, socket_factory=missing, sleep=clock.sleep, now=clock.now)
        r2.run()
        st = r2.status()
        assert st["state"] == "stopped" and "websocket" in st["error"], st
        assert "reinstall" in st["detail"], st
    finally:
        account.forget()
results.append(run("with no account the loop idles; a missing library stops it with a reason", t11))


def t12():
    saved = config.ACCOUNTS_URL
    try:
        assert relay.panel_url() == "", "no account, no address"
        account.save(ME)
        assert relay.panel_url() == SERVICE + "/p/d1/"
        assert relay.socket_url() == "wss://accounts.test/relay/connect"
        config.ACCOUNTS_URL = "http://localhost:8787"
        assert relay.socket_url() == "ws://localhost:8787/relay/connect"
        config.ACCOUNTS_URL = "off"
        assert relay.status()["enabled"] is False
        r = relay.Relay(echo, socket_factory=lambda *a: None)
        assert r.start() is False and r.status()["state"] == "off", r.status()
    finally:
        config.ACCOUNTS_URL = saved
        account.forget()
results.append(run("addresses follow the account service's URL, and nothing starts with accounts off", t12))

def t13():
    from intake import doctor
    account.save(ME)
    clock = Clock()
    try:
        relay.state_file().unlink(missing_ok=True)
        check = doctor.check_panel_web()
        assert check.ok and SERVICE + "/p/d1/" in check.detail and "no panel has recorded" in check.detail, check
        assert not check.required

        sock = FakeSocket([], [("open", None), ("close", (1006, ""))])
        r = relay.Relay(echo, socket_factory=sock.factory, sleep=clock.sleep, now=clock.now)
        r.run(rounds=1)
        written = relay.read_state_file()
        assert written["state"] == "reconnecting" and written["url"] == SERVICE + "/p/d1/", written
        assert written["written_at"] and written["connected_at"], written
        check = doctor.check_panel_web()
        assert not check.ok and "not connected as of" in check.detail and "1006" in check.detail, check
        assert "intake service status" in check.fix, check

        steady = FakeSocket([], [("open", None)])
        r2 = relay.Relay(echo, socket_factory=steady.factory, sleep=clock.sleep, now=clock.now)
        r2._connect_once(ME)  # the socket is "open" and stays that way
        assert relay.read_state_file()["state"] == "connected"
        check = doctor.check_panel_web()
        assert check.ok and "connected since" in check.detail, check
    finally:
        relay.state_file().unlink(missing_ok=True)
        account.forget()
    assert doctor.check_panel_web() is None, "no account, no line"
results.append(run("the doctor reads the state the panel last wrote, without the network", t13))


def t14():
    account.save(ME)
    saved = account.transport
    account.transport = lambda method, url, headers, body, timeout: (200, {"account": {"id": "a1", "email": "me@example.com"}})
    try:
        with gui.app.test_client() as client:
            out = client.get("/api/account").get_json()
            assert out["signed_in"] and out["relay"]["url"] == SERVICE + "/p/d1/", out.get("relay")
            assert out["relay"]["state"] in ("off", "connecting", "reconnecting", "connected", "stopped"), out["relay"]
            html = client.get("/setup").get_data(as_text=True)
            assert 'id="accountRelay"' in html and "renderRelay" in html
    finally:
        account.transport = saved
        account.forget()
results.append(run("the Setup page's account card is told the address and the connection state", t14))

def t15():
    # An attempt that never opened used to say nothing at all. That is why a
    # four-minute hole in panel.log read as one long pause rather than the
    # handful of failed attempts it was, and why the disconnect could not be
    # diagnosed from the log it was recorded in.
    import contextlib
    import io

    class Flaky:
        """Refuses `fails` times, then opens and is dropped without a close frame."""

        def __init__(self, fails):
            self.left = fails

        def factory(self, url, headers, on_open, on_message, on_close, on_error):
            outer = self

            class Sock:
                def run_forever(self):
                    if outer.left > 0:
                        outer.left -= 1
                        on_error(ConnectionRefusedError("connection refused"))
                        on_close(None, None)
                    else:
                        on_open()
                        on_close(None, None)

                def send(self, text):
                    pass

                def close(self):
                    pass

            return Sock()

    account.save(ME)
    try:
        clock = Clock()
        r = relay.Relay(echo, socket_factory=Flaky(6).factory,
                        sleep=clock.sleep, now=clock.now)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            r.run(rounds=7)
        lines = [l.split("relay: ", 1)[-1] for l in err.getvalue().splitlines()]

        failed = [l for l in lines if l.startswith("could not connect")]
        assert failed, f"a failed attempt logged nothing: {lines}"
        assert "ConnectionRefusedError" in failed[0], \
            f"the reason on_error gave was written over: {failed[0]}"
        assert "Trying again in" in failed[0], failed[0]

        # The gap is accounted for, so a hole in the log is explainable.
        joined = [l for l in lines if l.startswith("connected after")]
        assert joined and "6 failed attempts" in joined[0], \
            f"the run of failures was not summarized: {lines}"

        # Six failures write one line, not six: a Mac asleep overnight must
        # not fill the log with a line a minute.
        assert len(failed) == 1, f"one line per attempt is too many: {failed}"

        # A socket that died is told apart from one the service closed.
        dropped = [l for l in lines if "dropped without closing" in l]
        assert dropped, f"a socket that died reads the same as a clean close: {lines}"
    finally:
        account.forget()
results.append(run("a failed reconnect says so, once, with its reason", t15))


def t16():
    # The other half of why that log was unreadable: the dashboard asks for
    # /api/status every second, and every one of them was a line. A real
    # panel.log was 3.7 MB of them.
    quiet = gui._QuietPolling()

    class Line:
        def __init__(self, message):
            self.message = message

        def getMessage(self):
            return self.message

    assert not quiet.filter(Line('127.0.0.1 - - [x] "GET /api/status HTTP/1.1" 200 -'))
    # Anything that is not a healthy poll still gets written.
    assert quiet.filter(Line('127.0.0.1 - - [x] "GET /api/status HTTP/1.1" 500 -'))
    assert quiet.filter(Line('127.0.0.1 - - [x] "POST /api/record/start HTTP/1.1" 200 -'))
    assert quiet.filter(Line('127.0.0.1 - - [x] "GET /setup HTTP/1.1" 200 -'))
results.append(run("a healthy status poll is not worth a line, anything else is", t16))


def t17():
    # The service may publish panels on a host of its own (PANEL_ORIGIN in
    # syllabus-accounts), so the panel's pages are never same-origin with
    # the account pages. It says where in the welcome, and that is the
    # address the Setup page and the doctor should show.
    from intake import doctor
    account.save(ME)
    clock = Clock()
    panels = "https://panels.example/p/d1/"
    try:
        relay._published.clear()
        relay.state_file().unlink(missing_ok=True)
        assert relay.panel_url() == SERVICE + "/p/d1/", "nothing said yet: the address under ACCOUNTS_URL"

        sock = FakeSocket([], [
            ("open", None),
            ("message", {"t": "welcome", "device": "d1", "chunk_bytes": 2048, "panel_url": panels}),
            ("close", (1006, "")),
        ])
        r = relay.Relay(echo, socket_factory=sock.factory, sleep=clock.sleep, now=clock.now)
        r.run(rounds=1)
        assert relay.panel_url() == panels, relay.panel_url()
        assert r.status()["url"] == panels, r.status()
        # Another process (the doctor) reads it from the state file.
        relay._published.clear()
        assert relay.read_state_file()["published_url"] == panels, relay.read_state_file()
        assert relay.panel_url() == panels
        check = doctor.check_panel_web()
        assert panels in check.detail, check
    finally:
        relay._published.clear()
        relay.state_file().unlink(missing_ok=True)
        account.forget()
results.append(run("the panel's address is the one the service's welcome names", t17))


def t18():
    account.save(ME)
    clock = Clock()
    try:
        bad = [
            "javascript:alert(1)//p/d1/",
            "http://panels.example/p/d1/",        # plain http while the service is https
            "https://panels.example/p/d2/",       # another device
            "https://panels.example/p/d1/setup",  # not the panel's root
            "https://panels.example/p/d1/?x=1",
            "https://panels.example/p/d1/#x",
            "https://user@panels.example/p/d1/",
            "https:///p/d1/",
            "https://panels.example/p/d1/" + "x" * 600,
            42,
            None,
        ]
        for url in bad:
            assert relay.published_url(url, "d1") == "", url
        assert relay.published_url("https://panels.example/p/d1/", "d1") == "https://panels.example/p/d1/"

        # A welcome naming a bad address, or naming another device, changes nothing.
        relay._published.clear()
        relay.state_file().unlink(missing_ok=True)
        sock = FakeSocket([], [
            ("open", None),
            ("message", {"t": "welcome", "device": "d1", "panel_url": "javascript:alert(1)//p/d1/"}),
            ("message", {"t": "welcome", "device": "d2", "panel_url": "https://panels.example/p/d2/"}),
            ("message", {"t": "welcome", "device": "d1"}),
            ("close", (1006, "")),
        ])
        r = relay.Relay(echo, socket_factory=sock.factory, sleep=clock.sleep, now=clock.now)
        r.run(rounds=1)
        assert relay.panel_url() == SERVICE + "/p/d1/", relay.panel_url()
        assert r.status()["url"] == SERVICE + "/p/d1/", r.status()

        # A state file left by another device is not this one's address.
        relay.state_file().write_text(json.dumps({"device": "d2", "published_url": "https://panels.example/p/d2/"}))
        assert relay.panel_url() == SERVICE + "/p/d1/"

        # Local development: the service on plain http may name a plain http panel host.
        saved = config.ACCOUNTS_URL
        config.ACCOUNTS_URL = "http://localhost:8787"
        try:
            assert relay.published_url("http://127.0.0.1:8788/p/d1/", "d1") == "http://127.0.0.1:8788/p/d1/"
        finally:
            config.ACCOUNTS_URL = saved
    finally:
        relay._published.clear()
        relay.state_file().unlink(missing_ok=True)
        account.forget()
results.append(run("a welcome's address is believed only when it is a plain web address for this device", t18))


print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
