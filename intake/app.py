"""Syllabus as a Mac app: the same panel, in a window of its own, kept running.

    intake app              # the panel in a window instead of a browser tab
    intake app --hidden     # start in the menu bar, no window (the login item)
    intake app --port 5199  # against a different port

Nothing about the pipeline changes here. This module starts the panel from
gui.py on a thread, waits for it to answer, and puts a pywebview window
(WKWebView, the system's web engine) in front of it. The window has the
main thread, because macOS gives windows to nothing else; the panel, the
relay socket, and the recorder run exactly as they do under `intake panel`.

The app stays resident. Closing the window hides it; the process lives on
behind a menu bar item that shows whether a recording is running, starts
and stops one, opens the window again, and quits. While the window is open
the app has a Dock tile and a menu bar, so Cmd-V pastes into the key
fields; when it is hidden the app drops back to the menu bar alone. A
launch agent (service.py) starts it hidden at login when asked.

Launching a second copy while one is running only shows the first one's
window: the newcomer asks over loopback and exits. When the panel on the
port belongs to something else, `intake panel` or the Terminal install's
launch agent, this one starts no server: it opens its window on the panel
that is running, and its menu offers to take over, which moves the launch
agent to the app. Two panels against one home would fight over the
microphone and the watcher. If the port is held by something that is not
a Syllabus panel at all, it says so and exits.

Quitting does not stop a recording: ffmpeg runs in its own session and the
next panel picks it up, and the watcher is detached the same way.

Syllabus.app is this module frozen with PyInstaller; see packaging/. From a
checkout or a pipx install it needs the `app` extra: pip install '.[app]'.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable

from intake import config, service

HOST = "127.0.0.1"

# A comfortable size for the dashboard's grid; the pages already lay
# themselves out for anything from a phone up.
WINDOW_SIZE = (1180, 820)
MIN_SIZE = (720, 520)

# How long to give the panel to bind before giving up on it.
STARTUP_SECONDS = 15.0

# The menu bar glyph: the icon's mark as a template image, so macOS draws it
# in whatever the menu bar's color is. Beside the icon in static/.
MENUBAR_IMAGE = config.PACKAGE_DIR / "static" / "menubar@2x.png"


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def title() -> str:
    """The window's title: the product's name, nothing after it."""
    return config.PROFILE.title


# --- Finding the panel that is already there --------------------------------

def probe(port: int, host: str = HOST, timeout: float = 1.5) -> dict | None:
    """The status a Syllabus panel on `port` reports, or None if there is none.

    A panel is recognized by the shape of its status answer, not by the port
    being open: anything can hold a port. The same request the page polls
    every few seconds, so asking costs the running panel nothing new.
    """
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/status",
                                    timeout=timeout) as response:
            data = json.load(response)
    except Exception:
        return None
    if isinstance(data, dict) and "watcher" in data and "configured" in data:
        return data
    return None


def post(port: int, path: str, payload: dict | None = None, host: str = HOST,
         timeout: float = 5.0) -> dict:
    """POST to the panel on `port` and return its JSON. Raises on failure."""
    body = json.dumps(payload or {}).encode()
    request = urllib.request.Request(f"http://{host}:{port}{path}", data=body,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def page_for(status: dict | None) -> str:
    """Which page to open on a running panel: Setup until it is configured."""
    return "" if status and status.get("configured") else "setup"


def plan(port: int, host: str = HOST,
         probe_panel: Callable[..., dict | None] = probe,
         port_busy: Callable[..., bool] = service.port_answers) -> tuple[str, str]:
    """Decide what this launch does.

    ("show", url)    a copy of this app is running: ask it to show its window
    ("attach", url)  a panel without a window is running: be its window
    ("serve", "")    nothing is running: start the panel here
    ("busy", why)    the port is held by something else

    Pure apart from the two probes, so the decision can be tested without a
    server on either side of it.
    """
    status = probe_panel(port, host)
    if status is not None:
        url = f"http://{host}:{port}/{page_for(status)}"
        return ("show" if status.get("window") else "attach"), url
    if port_busy(port):
        return "busy", (f"port {port} is in use by something that is not a "
                        f"{config.PROFILE.title} panel; stop it or choose another "
                        f"port with --port")
    return "serve", ""


def ask_to_show(port: int, host: str = HOST) -> bool:
    """Tell the running copy of the app to show its window."""
    try:
        return bool(post(port, "/api/window/show", host=host).get("ok"))
    except Exception:
        return False


def start_panel(port: int, host: str = HOST, wait: float = STARTUP_SECONDS) -> str:
    """Run the panel on a thread and return the page to open once it answers."""
    from intake import gui
    url = gui.prepare(host, port)
    threading.Thread(target=gui.serve, args=(host, port), name="panel",
                     daemon=True).start()
    if not service.wait_for_port(port, wait):
        raise RuntimeError(f"the panel did not start listening on port {port} "
                           f"within {wait:.0f} seconds")
    return url


# --- The menu bar item's menu, as data ----------------------------------------
#
# Built fresh each time the menu opens, from the panel's status. Kept as
# plain dicts so what the menu says can be tested without Cocoa.

def recording_line(status: dict | None) -> str:
    """One line on the recording, for the menu: "Recording ACCT-4321 · 12:03"."""
    rec = (status or {}).get("recording") or {}
    if not rec.get("active"):
        return "Not recording"
    minutes, seconds = divmod(int(rec.get("elapsed") or 0), 60)
    course = rec.get("course") or ""
    clock = f"{minutes}:{seconds:02d}"
    if rec.get("stalled"):
        return f"Recording {course} · {clock} · the microphone is silent".replace("  ", " ")
    return f"Recording {course} · {clock}".replace("  ", " ")


def menu_items(status: dict | None, attached: bool, login_installed: bool) -> list[dict]:
    """What the menu bar menu shows. Each item: title, action, enabled, checked.

    An action of None is a line of information; "-" is a separator.
    """
    rec = (status or {}).get("recording") or {}
    active = bool(rec.get("active"))
    reachable = status is not None
    items = [
        {"title": f"Open {title()}", "action": "open", "enabled": True, "checked": False},
        {"title": "-", "action": "-", "enabled": False, "checked": False},
        {"title": recording_line(status) if reachable else "The panel is not answering",
         "action": None, "enabled": False, "checked": False},
    ]
    if active:
        items.append({"title": "Stop Recording", "action": "record_stop",
                      "enabled": True, "checked": False})
    elif reachable and (status.get("consent") or {}).get("given") is False:
        # The recorder refuses until permission to record is on file, and the
        # question is on the dashboard, so this opens it rather than failing
        # quietly into the log.
        items.append({"title": "Confirm Permission to Record…", "action": "open",
                      "enabled": True, "checked": False})
    else:
        now = (status or {}).get("now_class") or ""
        items.append({"title": f"Record {now}" if now else "Start Recording",
                      "action": "record_start",
                      "enabled": reachable and bool((status or {}).get("configured")),
                      "checked": False})
    items.append({"title": "-", "action": "-", "enabled": False, "checked": False})
    update = (status or {}).get("update") or {}
    if update.get("available") and update.get("how") == "download":
        items.append({"title": f"Download {title()} {update.get('version')}…",
                      "action": "update", "enabled": True, "checked": False})
    if attached:
        items.append({"title": f"Run {title()} from this app instead…",
                      "action": "takeover", "enabled": True, "checked": False})
    else:
        items.append({"title": f"Start {title()} when I log in", "action": "login_toggle",
                      "enabled": True, "checked": login_installed})
    items.append({"title": "-", "action": "-", "enabled": False, "checked": False})
    items.append({"title": f"Quit {title()}", "action": "quit", "enabled": True,
                  "checked": False})
    return items


# --- Taking over from a Terminal install --------------------------------------

def takeover(port: int, say: Callable[[str], None] = log,
             run=None, wait_free: float = 10.0) -> int:
    """Move the launch agent from `intake panel` to this app.

    Stops the agent that runs the panel now, waits for its port to go quiet,
    installs the agent that starts this app hidden at login (service.py
    writes it for the program running now, which is this app), and asks the
    new copy to show its window. The recording and the watcher, if any,
    survive: both run detached from the panel that started them.
    """
    kwargs = {} if run is None else {"run": run}
    service.uninstall(say=say, **kwargs)
    deadline = time.monotonic() + wait_free
    while service.port_answers(port) and time.monotonic() < deadline:
        time.sleep(0.25)
    if service.port_answers(port):
        say(f"the panel on port {port} did not stop; nothing else was changed")
        return 1
    rc = service.install(say=say, **kwargs)
    if rc == 0 and not ask_to_show(port):
        say("the app is running from its launch agent now; open it from the menu bar")
    return rc


# --- The resident app ---------------------------------------------------------

class Resident:
    """The window, the menu bar item, and what closing and quitting do.

    Everything that touches Cocoa runs on the main thread: pywebview owns
    that thread once start() is called, and hands us callbacks on others,
    so AppHelper.callAfter is how anything gets back onto it.
    """

    def __init__(self, url: str, port: int, attached: bool, hidden: bool):
        self.url = url
        self.port = port
        self.attached = attached
        self.hidden = hidden
        self.quitting = False
        self.window = None
        self._status_item = None
        self._controller = None

    # -- lifecycle --

    def run(self) -> None:
        import webview  # pywebview; imported here so the CLI never needs it
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
        if self.hidden:
            # pywebview makes the process a regular app at import; a login
            # item should appear in the menu bar only, with no Dock tile.
            NSApplication.sharedApplication().setActivationPolicy_(
                NSApplicationActivationPolicyAccessory)
        self.window = webview.create_window(
            title(), self.url, width=WINDOW_SIZE[0], height=WINDOW_SIZE[1],
            min_size=MIN_SIZE, hidden=self.hidden)
        self.window.events.closing += self._on_closing
        webview.start(self._on_started)

    def _on_started(self) -> None:
        """Called by pywebview on a worker thread once the window exists."""
        from PyObjCTools import AppHelper
        AppHelper.callAfter(self._install_menubar)

    def _on_closing(self) -> bool:
        """The window's close button and Cmd-W land here, and so do Cmd-Q and
        a quit from the system (logout, shutdown), since pywebview asks the
        same question for both.

        Returning False keeps the window, and we hide it instead. A quit is
        let through: refusing it would cancel a logout. The menu bar's Quit
        sets `quitting` first; Cmd-Q and the system are told apart by who is
        asking, pywebview's applicationShouldTerminate.
        """
        if self.quitting or _asked_by_terminate():
            return True
        from PyObjCTools import AppHelper
        AppHelper.callAfter(self._hide_main)
        return False

    # -- the window --

    def show(self) -> None:
        from PyObjCTools import AppHelper
        AppHelper.callAfter(self._show_main)

    def _show_main(self) -> None:
        from AppKit import NSApplication, NSApplicationActivationPolicyRegular
        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
        if self.window is not None:
            self.window.show()
        app.activateIgnoringOtherApps_(True)
        self.hidden = False

    def _hide_main(self) -> None:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
        if self.window is not None:
            self.window.hide()
        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyAccessory)
        self.hidden = True

    # -- the menu bar --

    def _install_menubar(self) -> None:
        from AppKit import (NSImage, NSMenu, NSStatusBar, NSVariableStatusItemLength)
        from Foundation import NSMakeSize
        self._controller = _MenuController.alloc().initWithResident_(self)
        item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        image = NSImage.alloc().initWithContentsOfFile_(str(MENUBAR_IMAGE))
        if image is not None:
            image.setSize_(NSMakeSize(18, 18))
            image.setTemplate_(True)
            item.button().setImage_(image)
        else:
            item.button().setTitle_(title())
        item.button().setToolTip_(title())
        menu = NSMenu.alloc().initWithTitle_(title())
        menu.setDelegate_(self._controller)
        item.setMenu_(menu)
        self._status_item = item

    def rebuild_menu(self, menu) -> None:
        """Fill `menu` from the panel's status. Main thread, as the menu opens."""
        from AppKit import NSMenuItem
        menu.removeAllItems()
        status = probe(self.port, timeout=1.0)
        installed = service.runs_this_program()
        for spec in menu_items(status, self.attached, installed):
            if spec["action"] == "-":
                menu.addItem_(NSMenuItem.separatorItem())
                continue
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                spec["title"], "perform:" if spec["action"] else None, "")
            item.setEnabled_(bool(spec["enabled"]))
            item.setState_(1 if spec["checked"] else 0)
            if spec["action"]:
                item.setTarget_(self._controller)
                item.setRepresentedObject_(spec["action"])
            menu.addItem_(item)

    def perform(self, action: str) -> None:
        """A menu item was chosen. Main thread."""
        if action == "open":
            self._show_main()
        elif action == "record_start":
            self._call("/api/record/start")
        elif action == "record_stop":
            self._call("/api/record/stop")
        elif action == "login_toggle":
            if service.runs_this_program():
                service.uninstall(say=log)
            else:
                service.install(say=log, wait=0)
        elif action == "takeover":
            if _confirm(f"Run {title()} from this app instead of the Terminal install?",
                        "The panel that is running now stops, this app takes its "
                        "place and starts at login from now on. A recording or a "
                        "lecture being processed is not interrupted.",
                        "Take over"):
                if takeover(self.port) == 0:
                    self.quit(confirm=False)
        elif action == "update":
            import webbrowser
            status = probe(self.port, timeout=1.0) or {}
            url = ((status.get("update") or {}).get("url")) or ""
            if url:
                webbrowser.open(url)
        elif action == "quit":
            self.quit()

    def _call(self, path: str) -> None:
        try:
            post(self.port, path)
        except Exception as exc:
            log(f"{title()}: {path} failed: {exc}")

    def quit(self, confirm: bool = True) -> None:
        from AppKit import NSApplication
        status = probe(self.port, timeout=1.0) if confirm else None
        if status and (status.get("recording") or {}).get("active"):
            if not _confirm(f"Quit {title()} while recording?",
                            "The recording keeps going on its own. Open "
                            f"{title()} again to stop it, or it stops at its "
                            "time limit and is filed then.", "Quit"):
                return
        self.quitting = True
        NSApplication.sharedApplication().terminate_(None)


def _asked_by_terminate() -> bool:
    """Whether the closing question came from the app being asked to quit,
    rather than from the window being closed."""
    frame = sys._getframe()
    while frame is not None:
        if frame.f_code.co_name == "applicationShouldTerminate_":
            return True
        frame = frame.f_back
    return False


def _confirm(message: str, detail: str, ok: str) -> bool:
    """A native two-button alert on the main thread. True for the first button."""
    from AppKit import NSAlert
    alert = NSAlert.alloc().init()
    alert.setMessageText_(message)
    alert.setInformativeText_(detail)
    alert.addButtonWithTitle_(ok)
    alert.addButtonWithTitle_("Cancel")
    return alert.runModal() == 1000  # NSAlertFirstButtonReturn


_controller_class = None


def _menu_controller_class():
    """The Objective-C object the menu talks to. Built on first use so that
    importing this module never needs Cocoa (tests, the CLI), and built once:
    the Objective-C runtime refuses a second class of the same name."""
    global _controller_class
    if _controller_class is not None:
        return _controller_class
    import objc
    from AppKit import NSObject

    class MenuController(NSObject):
        def initWithResident_(self, resident):
            self = objc.super(MenuController, self).init()
            if self is None:
                return None
            self.resident = resident
            return self

        def menuNeedsUpdate_(self, menu):
            self.resident.rebuild_menu(menu)

        def perform_(self, sender):
            self.resident.perform(sender.representedObject())

    _controller_class = MenuController
    return MenuController


class _LazyController:
    def __getattr__(self, name):
        return getattr(_menu_controller_class(), name)


_MenuController = _LazyController()


# --- Entry ---------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="intake app",
        description=f"{config.PROFILE.title} in a window of its own, kept running "
                    f"in the menu bar.")
    parser.add_argument("--port", type=int, default=config.PROFILE.panel_port,
                        help=f"{config.PROFILE.panel_port} for this profile")
    parser.add_argument("--hidden", action="store_true",
                        help="start in the menu bar without showing the window; "
                             "what the login item does")
    args = parser.parse_args(argv)

    action, detail = plan(args.port)
    if action == "busy":
        log(f"{config.PROFILE.title}: {detail}")
        return 1
    if action == "show":
        if ask_to_show(args.port):
            log(f"{config.PROFILE.title} is already running; showed its window")
            return 0
        action = "attach"  # it did not answer; be a window on it after all
    attached = action == "attach"
    if attached:
        log(f"{config.PROFILE.title}: a panel is already running on port {args.port}; "
            f"opening a window on it and starting nothing")
        url = detail
    else:
        try:
            url = start_panel(args.port)
        except RuntimeError as exc:
            log(f"{config.PROFILE.title}: {exc}")
            return 1
    resident = Resident(url, args.port, attached=attached, hidden=args.hidden)
    if not attached:
        from intake import gui
        gui.window_hooks["show"] = resident.show
    resident.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
