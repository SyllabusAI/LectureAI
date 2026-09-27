"""The panel on the web, through the account service, with nothing to set up.

Once this Mac is signed in to a Syllabus account (account.py), its panel has
an address on the account service:

    https://syllabusaccounts.maincoursemedia.com/p/<device>/

This module is how requests to that address reach the panel. The panel
opens a WebSocket to the service, authenticated with its device token, and
keeps it open from a thread inside the panel process. The service holds the
other end and sends each browser request down it as a JSON frame; the frame
is run against the Flask app right here, in process, and the answer goes
back up the socket. No port is opened, nothing is installed, no hostname or
tunnel is configured.

    to us        {t:"req", id, method, path, query, headers, viewer:{email, account_id}, base, body}
    from us      {t:"res", id, status, headers, body, more?}   then   {t:"chunk", id, body, more?}
                 {t:"hello", name, version}   once, when the socket opens
    to us        {t:"welcome", device, chunk_bytes, max_response_bytes, timeout_ms, panel_url?}

panel_url, when the service sends it, is where browsers reach this panel.
It may be on a host of the service's own rather than on ACCOUNTS_URL, so
that the panel's pages are never same-origin with the account pages; the
address under ACCOUNTS_URL still works and hands over to it.

Bodies are base64. A message may not exceed 1 MiB on the service's side,
so a long answer is split at the chunk size the welcome frame names.

Trust runs in one direction. The service decides who may reach the address
(only the account's owner) and names the viewer in every frame. The panel
believes it because the frame arrived on a socket the panel itself opened
with its own token, and marks the request with WSGI environ keys that no
network request can set (signin.py reads them). Cookies, Cf-* and
X-Forwarded-* headers are never carried over, so the tunnel's gate cannot
be confused by a relayed request either.

The socket reconnects on its own with a growing pause, and nothing here runs
at all without an account: a panel with no account.json is exactly what it
was. The Setup page and `intake doctor` read status() for what is going on.
"""

from __future__ import annotations

import base64
import json
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlsplit

from intake import __version__, account, config

RELAY_KEY = "intake.relay"                 # environ: this request came over the relay
VIEWER_KEY = "intake.viewer"               # environ: the viewer's email
VIEWER_ACCOUNT_KEY = "intake.viewer_account"  # environ: the viewer's account id

CONNECT_PATH = "/relay/connect"
PANEL_PREFIX = "/p/"

# Only these travel from the browser to the panel; the service already
# strips the rest, and this is the second fence.
REQUEST_HEADERS = {"content-type", "accept", "accept-language", "if-none-match", "if-modified-since"}
RESPONSE_HEADERS = {"content-type", "cache-control", "etag", "last-modified", "location", "vary", "content-language"}

DEFAULT_CHUNK_BYTES = 256 * 1024
PING_SECONDS = 30
# Two keepalives, both at PING_SECONDS. run_forever(ping_interval=...) sends
# WebSocket ping frames: the Cloudflare runtime answers those itself, which
# lets this end notice a dead link, but they leave nothing the service's
# Durable Object can read. So the panel also sends the text message HEARTBEAT,
# which the object's auto-response answers with HEARTBEAT_REPLY without waking
# and timestamps. Once a panel has sent one, the service treats 90 seconds
# without one (PANEL_SILENCE_MS in syllabus-accounts src/panel-relay.ts) as a
# dead socket. The body must be exactly this: the auto-response matches the
# whole message. If the pace changes, change PANEL_SILENCE_MS with it.
HEARTBEAT = "ping"
HEARTBEAT_REPLY = "pong"
# Reconnect pauses: doubled each failure, capped, with jitter, reset after a
# connection that held for a while.
MIN_BACKOFF = 1.0
MAX_BACKOFF = 60.0
STEADY_SECONDS = 60.0
# With no account there is nothing to connect to; look again this often.
NO_ACCOUNT_WAIT = 5.0
WORKERS = 4


def _say(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} relay: {msg}", file=sys.stderr, flush=True)


def state_file():
    """Where the running panel leaves its connection state for `intake doctor`,
    which never talks to the panel or the network."""
    return config.WORK_DIR / "relay.json"


def read_state_file() -> dict:
    try:
        data = json.loads(state_file().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- Addresses ---------------------------------------------------------------

# Where the service said, in its welcome frame, that each device's panel is
# published. The service may serve panels from a host of its own rather than
# from ACCOUNTS_URL (PANEL_ORIGIN in syllabus-accounts), so that a panel page
# is never same-origin with the account pages; only the service knows which.
_published: dict[str, str] = {}
_published_lock = threading.Lock()


def published_url(url, device_id: str) -> str:
    """`url`, if it is a believable address for this device's panel, else "".

    The address is shown on the Setup page and in `intake doctor`, so it has
    to be a plain web address for exactly this device: https (http only when
    the account service itself is plain http, which is local development),
    no credentials, no query, and the path /p/<device>/. Which host is the
    service's business.
    """
    if not isinstance(url, str) or not device_id or len(url) > 512:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    allowed = {"https"} | ({"http"} if account.url().startswith("http://") else set())
    if parts.scheme not in allowed or not parts.hostname or "@" in parts.netloc:
        return ""
    if parts.query or parts.fragment or parts.path != PANEL_PREFIX + device_id + "/":
        return ""
    return url


def _remember_published(device_id: str, url: str) -> None:
    with _published_lock:
        _published[device_id] = url


def _told(device_id: str) -> str:
    """What the service last said about this device, here or in the state file."""
    with _published_lock:
        told = _published.get(device_id, "")
    if told:
        return told
    # `intake doctor` is another process; the running panel leaves it there.
    state = read_state_file()
    if state.get("device") == device_id:
        return published_url(state.get("published_url"), device_id)
    return ""


def panel_url(acct: account.Account | None = None) -> str:
    """Where this Mac's panel is published, or '' without an account.

    The address the service named in its welcome frame when it named one,
    and otherwise the one it has always been, under ACCOUNTS_URL. Both work:
    the ACCOUNTS_URL address hands a signed-in owner over to the panel host.
    """
    acct = acct if acct is not None else (account.load() if account.enabled() else None)
    if acct is None or not acct.device_id:
        return ""
    return _told(acct.device_id) or account.url() + PANEL_PREFIX + acct.device_id + "/"


def socket_url() -> str:
    """The WebSocket address of the relay: the service's URL, wss instead of https."""
    base = account.url()
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    return base + CONNECT_PATH


# --- Serving one relayed request -----------------------------------------------

def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii") if data else ""


def serve(app, frame: dict, chunk_bytes: int = DEFAULT_CHUNK_BYTES) -> list[dict]:
    """Run one request frame against the Flask app; the frames to send back.

    The request is made through the app's test client, in process: no
    socket, no port, and the WSGI environ carries the relay marker and the
    viewer that signin.gate reads. SCRIPT_NAME is the base path the service
    publishes the panel under, so request.script_root and the links the
    pages write come out right.
    """
    rid = str(frame.get("id", ""))
    try:
        method = str(frame.get("method") or "GET").upper()
        path = str(frame.get("path") or "/")
        if not path.startswith("/") or path.startswith("//"):
            return [{"t": "res", "id": rid, "status": 400, "headers": {}, "body": ""}]
        viewer = frame.get("viewer") or {}
        headers = {k: v for k, v in (frame.get("headers") or {}).items()
                   if isinstance(k, str) and k.lower() in REQUEST_HEADERS}
        body = base64.b64decode(frame.get("body") or "")
        host = urlsplit(account.url()).netloc or "relay"
        base = str(frame.get("base") or "")
        environ = {
            RELAY_KEY: True,
            VIEWER_KEY: str(viewer.get("email") or "").lower(),
            VIEWER_ACCOUNT_KEY: str(viewer.get("account_id") or ""),
            "REMOTE_ADDR": "127.0.0.1",
        }
        client = app.test_client(use_cookies=False)
        res = client.open(path, method=method, query_string=str(frame.get("query") or ""),
                          headers=headers, data=body, base_url=f"https://{host}{base}",
                          environ_overrides=environ)
        data = b"" if res.status_code in (204, 304) else res.get_data()
        out_headers = {k: v for k, v in res.headers.items() if k.lower() in RESPONSE_HEADERS}
        status = res.status_code
    except Exception as exc:  # the answer to the browser is a 502, never a dead socket
        _say(f"request {rid} failed: {type(exc).__name__}: {exc}")
        return [{"t": "res", "id": rid, "status": 502,
                 "headers": {"Content-Type": "text/plain; charset=utf-8"},
                 "body": _b64(b"The panel could not answer that request.")}]

    chunk = max(1024, int(chunk_bytes or DEFAULT_CHUNK_BYTES))
    first, rest = data[:chunk], data[chunk:]
    frames = [{"t": "res", "id": rid, "status": status, "headers": out_headers,
               "body": _b64(first), "more": bool(rest)}]
    while rest:
        part, rest = rest[:chunk], rest[chunk:]
        frames.append({"t": "chunk", "id": rid, "body": _b64(part), "more": bool(rest)})
    return frames


# --- The socket, and keeping it open ---------------------------------------------

class _RealSocket:
    """websocket-client's WebSocketApp behind the small interface the loop uses.

    Imported lazily: an install that predates the dependency should still
    run its panel, with the relay reporting why it is off.
    """

    def __init__(self, url: str, headers: list[str], on_open, on_message, on_close, on_error):
        import websocket  # websocket-client
        self._app = websocket.WebSocketApp(
            url, header=headers,
            on_open=lambda ws: on_open(),
            on_message=lambda ws, msg: on_message(msg),
            on_close=lambda ws, code, reason: on_close(code, reason),
            on_error=lambda ws, err: on_error(err),
        )

    def run_forever(self) -> None:
        self._app.run_forever(ping_interval=PING_SECONDS, ping_timeout=PING_SECONDS // 3)

    def send(self, text: str) -> None:
        self._app.send(text)

    def close(self) -> None:
        self._app.close()


class Relay:
    """The panel's end of the relay: one thread, one socket at a time."""

    def __init__(self, app, socket_factory=_RealSocket, sleep=time.sleep, now=time.time,
                 heartbeat_seconds: float = PING_SECONDS):
        self.app = app
        self._heartbeat_seconds = heartbeat_seconds
        self._factory = socket_factory
        self._sleep = sleep
        self._now = now
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._socket = None
        # Set when the current connection is over; wakes its heartbeat thread.
        self._conn_done: threading.Event | None = None
        self._heartbeat: threading.Thread | None = None
        self._pool: ThreadPoolExecutor | None = None
        self.chunk_bytes = DEFAULT_CHUNK_BYTES
        self._state = {"state": "off", "url": "", "since": "", "error": "", "detail": "",
                       "connected_at": "", "attempts": 0, "requests": 0, "last_request_at": ""}
        # Attempts in a row that never got a socket open, and when the run
        # began. Counted so the log can account for a gap without writing a
        # line a minute through an outage nobody is awake for.
        self._failures = 0
        self._failing_since = 0.0
        # Whether the attempt now in progress ever reached on_open.
        self._opened = False

    # -- what the pages and the doctor read --

    def status(self) -> dict:
        with self._lock:
            out = dict(self._state)
        out["enabled"] = account.enabled()
        return out

    def _set(self, **fields) -> None:
        with self._lock:
            changed = "state" in fields and fields["state"] != self._state["state"]
            if changed:
                self._state["since"] = _iso(self._now())
            self._state.update(fields)
            snapshot = dict(self._state)
        if changed or "error" in fields:
            self._write_state(snapshot)

    def _write_state(self, snapshot: dict) -> None:
        """Leave the state where the doctor can read it. Best effort."""
        try:
            path = state_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            snapshot["written_at"] = _iso(self._now())
            path.write_text(json.dumps(snapshot, indent=2) + "\n")
        except OSError:
            pass

    # -- lifecycle --

    def start(self) -> bool:
        """Start the thread, unless accounts are off. Idempotent."""
        if not account.enabled():
            self._set(state="off", detail="accounts are turned off (ACCOUNTS_URL)")
            return False
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop.clear()
        self._pool = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="relay")
        self._thread = threading.Thread(target=self.run, name="relay", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        """Close the socket and end the thread."""
        self._stop.set()
        done = self._conn_done
        if done is not None:
            done.set()
        self.disconnect()
        if self._pool is not None:
            self._pool.shutdown(wait=False)

    def disconnect(self) -> None:
        """Drop the socket; the loop reconnects (or idles) on its own.

        Called when this Mac signs out of or in to an account, so a socket
        that belonged to the old device does not linger.
        """
        sock = self._socket
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

    def run(self, rounds: int | None = None) -> None:
        """Connect, serve until the socket drops, pause, repeat.

        `rounds` bounds the loop for the tests; the panel runs it forever.
        """
        backoff = MIN_BACKOFF
        done = 0
        while not self._stop.is_set() and (rounds is None or done < rounds):
            done += 1
            acct = account.load()
            if acct is None:
                self._set(state="off", url="", error="", detail="this Mac is not signed in to an account")
                self._sleep(NO_ACCOUNT_WAIT)
                continue
            self._set(state="connecting" if self._state["attempts"] == 0 else "reconnecting",
                      url=panel_url(acct), attempts=self._state["attempts"] + 1)
            started = self._now()
            reason = self._connect_once(acct)
            held = self._now() - started
            if self._stop.is_set():
                break
            if held >= STEADY_SECONDS:
                backoff = MIN_BACKOFF
            pause = backoff + random.uniform(0, backoff / 2)
            if reason == "unauthorized":
                pause = MAX_BACKOFF  # the token is refused; nothing to gain by hurrying
            self._set(state="reconnecting", error=reason,
                      detail=f"trying again in {round(pause)} s")

            # An attempt that never opened used to say nothing at all, which
            # is why a four-minute gap in the log read as one pause rather
            # than the six failed attempts it was. Only some of them are
            # written: the first, then every tenth, so a Mac that is simply
            # asleep or off the network does not fill the log overnight.
            if not self._opened:
                if not self._failures:
                    self._failing_since = started
                self._failures += 1
                if self._failures == 1 or self._failures % 10 == 0:
                    since = round(self._now() - self._failing_since)
                    run = f"; {self._failures} in a row over {since}s" \
                        if self._failures > 1 else ""
                    _say(f"could not connect: {reason}{run}. "
                         f"Trying again in {round(pause)}s")

            self._sleep(pause)
            backoff = min(MAX_BACKOFF, backoff * 2)
        self._set(state="stopped" if self._stop.is_set() else self._state["state"])

    def _connect_once(self, acct: account.Account) -> str:
        """One connection, start to finish. Returns why it ended."""
        outcome = {"reason": "the connection closed"}
        self._opened = False
        done = threading.Event()
        heartbeat: list[threading.Thread] = []

        def on_open() -> None:
            self._opened = True
            self._set(state="connected", error="", detail="", connected_at=_iso(self._now()))
            if self._failures:
                # What the gap in this log was. Without it, a run of failed
                # attempts looked like one long unexplained silence.
                waited = round(self._now() - self._failing_since)
                tries = "attempt" if self._failures == 1 else "attempts"
                _say(f"connected after {self._failures} failed {tries} over {waited}s; "
                     f"this panel is at {panel_url(acct)}")
            else:
                _say(f"connected; this panel is at {panel_url(acct)}")
            self._failures = 0
            self._send({"t": "hello", "name": acct.device_name, "version": __version__})
            if not heartbeat and not done.is_set():  # one per connection
                t = threading.Thread(target=self._beat, args=(sock, done),
                                     name="relay-heartbeat", daemon=True)
                heartbeat.append(t)
                self._heartbeat = t
                t.start()

        def on_message(message) -> None:
            self._on_message(message)

        def on_close(code, reason) -> None:
            if code:
                outcome["reason"] = \
                    f"the connection closed ({code}{' ' + reason if reason else ''})"
            elif outcome.get("explained"):
                # on_error already said something specific and websocket-client
                # calls it before this; "refused the token" or "connection
                # refused" is the answer somebody needs, and a bare close would
                # write it over with nothing.
                return
            else:
                # No close frame at all. The socket died rather than being
                # closed, which is what a dropped link, a sleeping Mac or a
                # ping timeout looks like from this end, and is worth telling
                # apart from the service closing it on purpose.
                outcome["reason"] = "the connection dropped without closing"

        def on_error(err) -> None:
            status = getattr(err, "status_code", None)
            outcome["explained"] = True
            if status in (401, 403):
                outcome["reason"] = "unauthorized"
                _say("the account service refused this Mac's token")
            elif status:
                outcome["reason"] = f"the account service answered {status}"
            else:
                outcome["reason"] = f"{type(err).__name__}: {err}".strip(": ")

        try:
            sock = self._factory(socket_url(), ["Authorization: Bearer " + acct.token],
                                 on_open, on_message, on_close, on_error)
        except Exception as exc:  # websocket-client missing, most likely
            self._set(state="off", error=f"{type(exc).__name__}: {exc}",
                      detail="the relay cannot start; reinstall Syllabus to get its dependencies")
            _say(f"cannot start: {exc}")
            self._stop.set()
            return "cannot start"
        self._socket = sock
        self._conn_done = done
        try:
            sock.run_forever()
        except Exception as exc:
            outcome["reason"] = f"{type(exc).__name__}: {exc}"
        finally:
            self._socket = None
            done.set()
            self._conn_done = None
            for t in heartbeat:
                # It wakes on `done`; a send stuck on a dead socket is the only
                # thing that could hold it, and it is a daemon either way.
                t.join(timeout=5)
            self._heartbeat = None
        if self._state["state"] == "connected":
            _say(f"disconnected: {outcome['reason']}")
        return outcome["reason"]

    def _beat(self, sock, done: threading.Event) -> None:
        """Send the text heartbeat on this connection until it ends.

        Holds _send_lock like every other send, so a beat is always a whole
        message of its own. It can land between two chunks of an answer,
        which is fine: chunks carry their id, and the service reads a bare
        `ping` as the heartbeat, never as part of a frame.

        A failed send ends the connection. The service stops trusting a
        socket that goes 90 seconds without a beat, so one that cannot send
        them is only waiting to be dropped; closing it now lets the loop reconnect
        instead of waiting on the protocol ping to time out. It says so
        once, not every tick.
        """
        while not done.wait(self._heartbeat_seconds):
            if self._stop.is_set() or self._socket is not sock:
                return
            try:
                with self._send_lock:
                    sock.send(HEARTBEAT)
            except Exception as exc:
                _say(f"heartbeat stopped: could not send: {type(exc).__name__}: {exc}")
                try:
                    sock.close()
                except Exception:
                    pass
                return

    # -- frames --

    def _send(self, frame: dict) -> None:
        sock = self._socket
        if sock is None:
            return
        with self._send_lock:
            sock.send(json.dumps(frame, separators=(",", ":")))

    def _on_message(self, message) -> None:
        if isinstance(message, bytes) or message == HEARTBEAT_REPLY:
            return  # the service's answer to the heartbeat, nothing to do
        try:
            frame = json.loads(message)
        except ValueError:
            return
        if not isinstance(frame, dict):
            return
        kind = frame.get("t")
        if kind == "welcome":
            try:
                self.chunk_bytes = max(1024, int(frame.get("chunk_bytes") or DEFAULT_CHUNK_BYTES))
            except (TypeError, ValueError):
                pass
            self._welcomed(frame)
            return
        if kind != "req":
            return
        # Each request on its own worker, so a slow one (stopping a recording,
        # listing microphones) never holds up the status poll behind it.
        if self._pool is not None:
            self._pool.submit(self._handle, frame)
        else:
            self._handle(frame)

    def _welcomed(self, frame: dict) -> None:
        """Take the panel's address from the welcome, if it names a good one."""
        acct = account.load()
        if acct is None or str(frame.get("device") or "") != acct.device_id:
            return
        told = published_url(frame.get("panel_url"), acct.device_id)
        if not told:
            return
        before = panel_url(acct)
        _remember_published(acct.device_id, told)
        self._set(url=told, device=acct.device_id, published_url=told)
        with self._lock:
            snapshot = dict(self._state)
        self._write_state(snapshot)
        if told != before:
            _say(f"the account service publishes this panel at {told}")

    def _handle(self, frame: dict) -> None:
        with self._lock:
            self._state["requests"] += 1
            self._state["last_request_at"] = _iso(self._now())
        for out in serve(self.app, frame, self.chunk_bytes):
            try:
                self._send(out)
            except Exception as exc:
                _say(f"could not send an answer: {exc}")
                return


# --- The one relay of this panel process -----------------------------------------

_relay: Relay | None = None


def start(app) -> Relay | None:
    """Start the panel's relay, once. None when accounts are off."""
    global _relay
    if _relay is None:
        _relay = Relay(app)
    return _relay if _relay.start() else None


def current() -> Relay | None:
    return _relay


def status() -> dict:
    """What the Setup page and the doctor show. Safe with no relay at all."""
    if _relay is None:
        acct = account.load() if account.enabled() else None
        return {"enabled": account.enabled(), "state": "off", "url": panel_url(acct),
                "since": "", "error": "", "detail": "", "connected_at": "",
                "attempts": 0, "requests": 0, "last_request_at": ""}
    return _relay.status()


def reconnect() -> None:
    """The account changed: drop the socket so the loop picks up the new state."""
    if _relay is not None:
        _relay.disconnect()
