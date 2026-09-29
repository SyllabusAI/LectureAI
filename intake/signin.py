"""The gate in front of the panel, when it is reached from somewhere else.

The panel listens on this Mac only. One road leads to it from elsewhere:
the account service's relay (relay.py), where the service itself is the
sign-in and names the viewer in every request. This module is the gate.
Requests from this Mac are never gated, so http://127.0.0.1:5173 keeps
working whatever the relay is doing.

Who may enter is decided by the Syllabus account this Mac is signed in to
(account.py): its owner, and nobody else. The service decides that before
it relays anything, and names the viewer in the frame; the panel checks
that the name still matches the account this Mac holds, and trusts nothing
else.

The panel has no sign-in of its own any more. Until 2026-09-14 it ran
Google's sign-in itself against an allowlist in .env; until 2026-09-15 it
ran a sign-in through the account service for anyone arriving over a
Cloudflare Tunnel, with a session in a signed cookie. The tunnel, its
hostname, and its PANEL_PUBLIC_URL and PANEL_SECRET_KEY settings were
retired on 2026-09-15, and that code went with them.
"""

from __future__ import annotations

import secrets

from flask import Flask, g, jsonify, request

from intake import account, config, relay


def _page(message: str, code: int = 200):
    return (f"<!doctype html><title>{config.PROFILE.title}</title>"
            f"<div style='font: 15px/1.5 system-ui; max-width: 40em; margin: 4em auto'>"
            f"<p>{message}</p></div>"), code


def refuse(code: int, message: str):
    """A refusal shaped for the caller: JSON for the API, a page otherwise."""
    if request.path.startswith("/api/"):
        return jsonify({"error": message}), code
    return _page(message, code)


# --- The request at hand -----------------------------------------------------

def via_relay() -> bool:
    """Whether this request arrived over the account service's relay.

    relay.py runs a relayed request against the app in process and marks
    the WSGI environ. A request from the network can set HTTP_* keys and
    nothing else, so the mark cannot be forged from outside.
    """
    return bool(request.environ.get(relay.RELAY_KEY))


# --- Requests from somewhere else on the web ---------------------------------

# Methods that can change something. GET and HEAD are deliberately not here:
# a page elsewhere can make a browser issue those whatever we do, and what
# keeps it from reading the answer is the same-origin policy, not us.
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# What the panel's own pages send on every call. This is the load-bearing
# check: a browser will only send form-urlencoded, multipart or text/plain to
# another origin without asking permission first, and the panel answers no
# such question, so requiring JSON is on its own enough to stop a page on
# another site from acting here.
JSON_TYPE = "application/json"

# Names this Mac answers to. Anything else means a browser resolved somebody
# else's hostname to 127.0.0.1 and now believes their page and this panel are
# the same origin, which is DNS rebinding.
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}

# Set by `intake panel --expose`, which is an explicit decision to serve
# something other than this Mac. The name the panel is then reached by is
# whatever the operator put in front of it, so there is nothing here to
# compare a Host against. The checks that do not depend on Host still run.
EXPOSED = False


def _host_name() -> str:
    """The host the request was addressed to, without its port."""
    host = request.headers.get("Host", "")
    if host.startswith("[") and "]" in host:      # [::1]:5173
        return host[:host.index("]") + 1]
    return host.rsplit(":", 1)[0] if ":" in host else host


def _our_origins() -> set[str]:
    """The origins the panel's own pages are served from."""
    host = request.headers.get("Host", "")
    if not host:
        return set()
    return {f"http://{host}".lower(), f"https://{host}".lower()}


def cross_site() -> str:
    """Why this request looks like it came from another site, or "".

    The panel has no authentication for local requests, by design: anything
    running as this user could do all of it directly anyway. A web page in a
    browser is not that. It cannot read the panel's answers, but until now it
    could still make the panel act — start the microphone, spawn a watcher,
    SIGTERM one, throw away the Drive token, sign the Mac out — by posting a
    plain form at it, which needs nobody's permission.

    A client that is not a browser sends no Origin and no Sec-Fetch-Site.
    That is allowed on purpose: curl and the CLI are not what is being
    defended against, and they cannot be tricked into acting on behalf of a
    page somebody else wrote.
    """
    if via_relay():
        # The account service built this request in process. It carries no
        # browser headers at all, and the service's own gate already decided
        # who is allowed to reach this panel.
        return ""

    host = _host_name().lower()
    if host and not EXPOSED and host not in LOCAL_HOSTS:
        return (f"this panel answers to localhost on this Mac, and {host} is "
                f"somebody else's name for it")

    if request.method not in UNSAFE_METHODS:
        return ""
    if request.headers.get("Sec-Fetch-Site", "").lower() in ("cross-site", "same-site"):
        return "that came from another site"
    origin = request.headers.get("Origin", "")
    if origin and origin.lower() not in _our_origins():
        return f"that came from {origin}"
    kind = (request.content_type or "").split(";")[0].strip().lower()
    if kind != JSON_TYPE:
        return (f"this panel only takes {JSON_TYPE}, which a page on another "
                f"site cannot send here without asking permission first")
    return ""


# --- The gate ----------------------------------------------------------------

def relay_viewer() -> str:
    """Who the relay says is looking, if that is this Mac's account's owner.

    The service only relays the owner, so a mismatch means account.json
    changed under a socket that belonged to the previous account. Nobody
    then.
    """
    acct = account.load() if account.enabled() else None
    email = str(request.environ.get(relay.VIEWER_KEY, "")).lower()
    if acct is None or not email:
        return ""
    return email if request.environ.get(relay.VIEWER_ACCOUNT_KEY) == acct.account_id else ""


def gate():
    """Run before every request. None lets it through."""
    g.viewer = ""
    g.relayed = False
    # A fresh nonce for every request, refused ones included, so that a page
    # and the policy that admits its scripts are always from the same answer.
    g.csp_nonce = secrets.token_urlsafe(18)
    # Before anything about who is looking: a request that came from another
    # site is refused whether it is local or relayed.
    elsewhere = cross_site()
    if elsewhere:
        return refuse(403, f"Refused: {elsewhere}.")
    if via_relay():
        # The account service already decided who may reach this panel's
        # address and named them in the frame; that is the sign-in.
        who = relay_viewer()
        if not who:
            return refuse(403, "That account does not own this Syllabus.")
        g.viewer = who
        g.relayed = True
    return None


#: Paths whose answers name the account, describe the settings, or carry a
#: credential. Nothing here should sit in a cache: not the browser's, and not
#: whatever is between a phone and this Mac when the panel is reached through
#: the relay.
_NO_STORE_PREFIXES = ("/api/setup", "/api/account", "/api/drive", "/api/doctor",
                      "/setup", "/account")


def no_store(response):
    """Mark answers that are about this install rather than about the app."""
    path = request.path
    if any(path == p or path.startswith(p + "/") for p in _NO_STORE_PREFIXES):
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("Pragma", "no-cache")
    return response


def content_security_policy(nonce: str) -> str:
    """What a page of this panel may load and run.

    The pages carry two scripts and one stylesheet of their own, each marked
    with this response's nonce, so nothing else can run: no injected
    <script>, no javascript: link, no handler written into markup (the pages
    assign handlers from script, which a policy does not touch). Everything
    the page fetches goes to the panel itself, which is the same origin
    whether it is opened on this Mac or through the relay, where the base
    path is only a prefix. The only outside origins are Google Fonts.

    style-src-attr keeps inline style="..." attributes, because the dashboard
    writes each course's color into the markup it builds; those cannot run
    anything. Styles from a <style> element or a stylesheet still need the
    nonce or Google Fonts.
    """
    return "; ".join([
        "default-src 'none'",
        f"script-src 'nonce-{nonce}'",
        f"style-src-elem 'nonce-{nonce}' https://fonts.googleapis.com",
        "style-src-attr 'unsafe-inline'",
        "font-src https://fonts.gstatic.com",
        "img-src 'self' data:",
        "connect-src 'self'",
        "form-action 'self'",
        "base-uri 'none'",
        "object-src 'none'",
        "frame-ancestors 'none'",
    ])


def _security_headers(response):
    """Put the policy on every answer. relay.RESPONSE_HEADERS must name
    Content-Security-Policy too, or the relay drops it on the way out."""
    nonce = getattr(g, "csp_nonce", "")
    if nonce:
        response.headers["Content-Security-Policy"] = content_security_policy(nonce)
    return response


def install(app: Flask) -> None:
    app.before_request(gate)
    app.after_request(no_store)
    app.after_request(_security_headers)
    app.context_processor(lambda: {"csp_nonce": getattr(g, "csp_nonce", "")})
