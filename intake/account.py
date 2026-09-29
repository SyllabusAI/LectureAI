"""A Syllabus account, and this panel's place in it.

Accounts live in a small web service (syllabusaccounts.maincoursemedia.com,
the syllabus-accounts repo). This module is the panel's side of it: claiming
an identity for the Mac the panel runs on, remembering it, and asking the
service who this panel is.

Claiming works like signing a TV in to a streaming service. The panel asks
the service for a code, shows it on the Setup page, and the person types it
into the service's website while signed in there with Google. The panel
polls until the service says the code was approved, and receives a token
that identifies this Mac from then on. That token, and who it belongs to,
is kept in account.json in the profile's home. The browser never sees the
token and the panel never sees the person's Google session.

None of this is required. With no account.json the panel is exactly what it
was: a single-user local tool. ACCOUNTS_URL=off in .env turns the feature
off altogether, and the Setup page then does not mention it.
"""

from __future__ import annotations

import json
import re
import socket
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

import requests
from google.auth import credentials as google_credentials

from intake import config

OFF_WORDS = {"off", "none", "no", "false", "0"}
TIMEOUT = 15
# The proxy's summarize call waits on Claude reading a whole lecture, which is
# tens of seconds and occasionally more. The 15 above is right for the control
# plane (/me, /device/poll) and far too short for that one, which fails as a
# client-side timeout after the tokens have already been spent.
SLOW_TIMEOUT = 300
# If the service says nothing about how often to poll, this is how often.
DEFAULT_INTERVAL = 5
DEFAULT_CODE_SECONDS = 900


def _say(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


@dataclass
class Account:
    """What account.json holds: the token and who it says this Mac is."""
    token: str
    account_id: str
    email: str
    name: str
    device_id: str
    device_name: str
    profile: str
    url: str
    claimed_at: str

    def public(self) -> dict:
        """The parts a page may see. Never the token."""
        return {"email": self.email, "name": self.name, "device_name": self.device_name,
                "device_id": self.device_id, "claimed_at": self.claimed_at}


# --- Settings and the file --------------------------------------------------

#: A service on this machine, for development. Everything else must be https.
_LOCAL_SERVICE = re.compile(r"^http://(localhost|127\.0\.0\.1)(:\d+)?$")


def url() -> str:
    return (config.ACCOUNTS_URL or "").strip().rstrip("/")


def enabled() -> bool:
    """Whether accounts are a thing for this install at all."""
    value = url()
    return bool(value) and value.lower() not in OFF_WORDS


def managed() -> bool:
    """Whether this Mac spends the service's keys rather than its own.

    True once it is signed in to a Syllabus account: transcription and
    summaries then go through the proxy, which holds the provider keys and
    meters the account (see providers.ProxyProvider). Everything that used to
    ask whether OPENAI_API_KEY is set asks this first, because on a managed
    Mac there is no such key and nothing is wrong with that.
    """
    return enabled() and load() is not None


def load() -> Account | None:
    """The account this panel was claimed into, or None. A broken file is None too."""
    path = config.ACCOUNT_FILE
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return Account(**{k: str(data.get(k, "")) for k in Account.__dataclass_fields__})
    except (OSError, ValueError, TypeError):
        return None


def save(account: Account) -> None:
    config.write_private(config.ACCOUNT_FILE, json.dumps(asdict(account), indent=2) + "\n")


def forget() -> None:
    """Drop this Mac's account, and everything cached on its behalf: the live
    Drive token in memory and the last-known grant state on disk, both of which
    belong to the account being left."""
    forget_drive_token()
    for path in (config.ACCOUNT_FILE, _drive_cache_file()):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def device_name() -> str:
    """What this Mac calls itself, for the account page: 'Trace's MacBook Pro'."""
    try:
        # By absolute path: the launchd agent's PATH has no /usr/sbin, and
        # the fallback below is the hostname, which is a worse name.
        out = subprocess.run(["/usr/sbin/scutil", "--get", "ComputerName"], capture_output=True,
                             text=True, timeout=3)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return socket.gethostname().split(".")[0] or "A Mac"


# --- Talking to the service -------------------------------------------------

def _http(method: str, full_url: str, headers: dict, body: dict | None,
          timeout: float) -> tuple[int, dict]:
    """One request; status and the JSON body (or {} when there is none).

    Replaced wholesale by the tests, which is why the whole exchange goes
    through this one function.
    """
    res = requests.request(method, full_url, headers=headers, json=body, timeout=timeout)
    try:
        data = res.json()
    except ValueError:
        data = {}
    return res.status_code, data if isinstance(data, dict) else {}


transport = _http


class ServiceMoved(RuntimeError):
    """ACCOUNTS_URL no longer names the service this Mac's token belongs to."""


def _destination_for_token() -> str:
    """Where this Mac's credential may be sent, or raise.

    A device token is issued by one service and is worth something only to
    that one. ACCOUNTS_URL is an ordinary setting in an editable file, and a
    token followed it wherever it pointed: changing that one line was enough
    to make this Mac hand its bearer to somebody else's server, which could
    then spend the account's allowance and collect its Drive access token.

    So the token goes to the origin it was issued for, recorded in
    account.json when the Mac was claimed, and a setting that disagrees stops
    the request instead of redirecting it. An account file written before this
    existed carries no url; those fall back to the setting, because refusing
    would sign those Macs out rather than protect them.
    """
    here = url()
    acct = load()
    issued = (acct.url or "").strip().rstrip("/") if acct else ""
    if issued and issued != here:
        raise ServiceMoved(
            f"this Mac is signed in to {issued}, and ACCOUNTS_URL now says "
            f"{here}. Nothing was sent. Put the setting back, or sign out and "
            f"sign in again to move this Mac to the new service.")
    if not here.startswith("https://") and not _LOCAL_SERVICE.match(here):
        raise ServiceMoved(f"refusing to send this Mac's sign-in to {here}, "
                           f"which is not an https address")
    return here


def call(method: str, path: str, body: dict | None = None,
         token: str | None = None, timeout: float = TIMEOUT) -> tuple[int, dict]:
    headers = {"Accept": "application/json"}
    base = url()
    if token:
        # Only a credential-bearing request is bound: /device/start and
        # /device/poll carry nothing worth stealing and run before there is an
        # account to bind to.
        base = _destination_for_token()
        headers["Authorization"] = "Bearer " + token
    return transport(method, base + path, headers, body, timeout)


def summary() -> dict:
    """For the dashboard's status poll: no network, just the file."""
    if not enabled():
        return {"enabled": False, "signed_in": False}
    acct = load()
    out = {"enabled": True, "url": url(), "signed_in": acct is not None}
    if acct:
        out.update(acct.public())
    return out


def allowance() -> dict:
    """What is left on this account's meters this month, asked of the service.

    This is a network call, so nothing on the status poll's path may reach
    it: that poll runs every couple of seconds and is deliberately file-only.
    The panel asks for this on its own, when there is a reason to.

    Returns the service's own answer with `ok` on the front, or `ok` false
    and a reason. A panel that cannot find out what is left says nothing
    rather than guessing, so every failure here is one shape.
    """
    acct = load()
    if acct is None:
        return {"ok": False, "error": "this Mac is not signed in"}
    try:
        status, data = call("GET", "/proxy/usage", token=acct.token)
    except Exception as exc:  # network down, DNS, a 5xx that was not JSON
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if status != 200:
        return {"ok": False, "error": f"the account service answered {status}"}
    return {"ok": True, **data}


def whoami() -> tuple[str, str]:
    """Ask the service about this panel's token.

    Returns ("ok", email), ("revoked", why), or ("unreachable", why). A
    revoked token is forgotten here, so the Setup page can offer a fresh
    sign-in rather than a token the service will never accept again.
    """
    acct = load()
    if acct is None:
        return "none", "not signed in"
    try:
        status, data = call("GET", "/me", token=acct.token)
    except Exception as exc:  # network down, DNS, a 5xx that was not JSON
        return "unreachable", f"{type(exc).__name__}: {exc}"
    if status == 401:
        _say(f"the account service no longer accepts this Mac's token; forgetting it")
        forget()
        return "revoked", "this Mac was removed from the account"
    if status != 200:
        return "unreachable", f"the account service answered {status}"
    account = data.get("account") or {}
    fresh = str(account.get("email") or "").strip()
    # The service's own answer to this Mac's own token (never anything from a
    # relayed frame) is what keeps the stored email from going stale, since
    # that is the email the panel shows as "Signed in as". Only when the
    # answer is about the same account, and the file is still that account's.
    if fresh and fresh != acct.email and str(account.get("id") or acct.account_id) == acct.account_id:
        current = load()
        if current is not None and current.token == acct.token:
            current.email = fresh
            try:
                save(current)
            except OSError as exc:
                _say(f"could not update the stored account email: {exc}")
    return "ok", fresh or acct.email


def sign_out() -> None:
    """Tell the service to drop this Mac's token, then forget it here.

    Best effort on the network: a service that cannot be reached still
    leaves this Mac signed out, and the token can be removed from the
    account page later.
    """
    acct = load()
    if acct is not None:
        try:
            call("POST", "/device/revoke", {}, token=acct.token)
        except Exception as exc:
            _say(f"could not tell the account service to drop the token: {exc}")
    forget()


# --- Claiming this Mac ------------------------------------------------------

_claim: dict = {"running": False, "user_code": "", "verification_uri": "",
                "verification_uri_complete": "", "expires_at": 0.0, "error": ""}
_claim_lock = threading.Lock()


def claim_status() -> dict:
    with _claim_lock:
        out = dict(_claim)
    out["expires_in"] = max(0, round(out["expires_at"] - time.time())) if out["running"] else 0
    return out


def _set_claim(**fields) -> None:
    with _claim_lock:
        _claim.update(fields)


def start_claim(name: str | None = None, background: bool = True) -> dict:
    """Ask the service for a code and start polling for its approval.

    Returns the code and where to enter it. With background=False the poll
    loop is not started, so a caller (a test) can drive it with run_claim.
    """
    if not enabled():
        raise RuntimeError("accounts are turned off (ACCOUNTS_URL)")
    if load() is not None:
        raise RuntimeError("this Mac is already signed in")
    if claim_status()["running"]:
        raise RuntimeError("a sign-in is already waiting for its code")
    name = (name or "").strip() or device_name()
    status, data = call("POST", "/device/start",
                        {"name": name, "profile": config.PROFILE.name})
    if status != 200 or not data.get("device_code") or not data.get("user_code"):
        raise RuntimeError(f"the account service could not start a sign-in ({status})")
    expires_at = time.time() + int(data.get("expires_in") or DEFAULT_CODE_SECONDS)
    interval = int(data.get("interval") or DEFAULT_INTERVAL)
    _set_claim(running=True, user_code=data["user_code"],
               verification_uri=data.get("verification_uri", url() + "/device"),
               verification_uri_complete=data.get("verification_uri_complete", ""),
               expires_at=expires_at, error="")
    _say(f"sign-in started: enter {data['user_code']} at {url()}/device")
    if background:
        threading.Thread(target=run_claim, args=(data["device_code"], name, interval, expires_at),
                         daemon=True).start()
    return {"device_code": data["device_code"], "name": name, "interval": interval,
            **claim_status()}


def run_claim(device_code: str, name: str, interval: int, expires_at: float,
              sleep=time.sleep, now=time.time) -> Account | None:
    """Poll until the code is approved, expires, or is refused.

    Network trouble is not a refusal: the service may be briefly unreachable
    and the code is still good, so polling continues until it expires.
    """
    try:
        while now() < expires_at:
            sleep(interval)
            try:
                status, data = call("POST", "/device/poll", {"device_code": device_code})
            except Exception as exc:
                _say(f"could not reach the account service; trying again: {exc}")
                continue
            if status == 200 and data.get("token"):
                acct_data = data.get("account") or {}
                dev = data.get("device") or {}
                account = Account(
                    token=str(data["token"]),
                    account_id=str(acct_data.get("id", "")),
                    email=str(acct_data.get("email", "")),
                    name=str(acct_data.get("name", "")),
                    device_id=str(dev.get("id", "")),
                    device_name=str(dev.get("name") or name),
                    profile=str(dev.get("profile") or config.PROFILE.name),
                    url=url(),
                    claimed_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
                )
                save(account)
                _say(f"this Mac is now signed in as {account.email}")
                _set_claim(running=False, error="")
                return account
            error = str(data.get("error", ""))
            if error == "authorization_pending":
                interval = int(data.get("interval") or interval)
                continue
            if error == "expired_token":
                break
            if error == "slow_down":
                interval += 5
                continue
            _set_claim(running=False, error="the account service refused the code; try again")
            return None
        _set_claim(running=False, error="the code expired before it was entered; try again")
        return None
    except Exception as exc:  # never let the thread die silently
        _set_claim(running=False, error=f"{type(exc).__name__}: {exc}")
        return None


def cancel_claim() -> None:
    """Stop showing a code. The poll thread notices on its next pass."""
    _set_claim(running=False, expires_at=0.0, error="")


# --- Google Drive through the account ------------------------------------------
#
# The account can hold the Drive grant, so every Mac signed in to it files to
# the same Drive without each running `intake login`. The refresh token
# stays on the service; this Mac asks for a one-hour access token when it
# needs one and caches it until shortly before it expires. upload.py prefers
# these credentials whenever the account has a grant, and falls back to this
# Mac's own token.json otherwise.

DRIVE_TOKEN_MARGIN = 120   # ask for a new token this many seconds early


class DriveGrantMissing(Exception):
    """The account has no Drive grant, or Google stopped honoring it."""


# account_id says whose token this is. Without it the cache would hand one
# account's live token (and Google email) to whoever signs in next.
_drive: dict = {"token": "", "expires_at": 0.0, "email": "", "account_id": ""}
_drive_lock = threading.Lock()


def _drive_cache_file():
    return config.WORK_DIR / "drive-grant.json"


def _note_drive(connected: bool, email: str = "", detail: str = "") -> None:
    """What the doctor reads: the last thing the service said about the grant."""
    try:
        data = drive_cached()
        data.update({
            "connected": connected, "google_email": email, "detail": detail,
            "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
        config.write_private(_drive_cache_file(), json.dumps(data, indent=2) + "\n")
    except OSError:
        pass


def note_drive_in_use(in_use: bool, detail: str = "") -> None:
    """Record whether the uploader is actually using the account's grant."""
    data = drive_cached()
    if data.get("in_use") == in_use and data.get("in_use_detail", "") == detail:
        return
    data.update({"in_use": in_use, "in_use_detail": detail})
    try:
        config.write_private(_drive_cache_file(), json.dumps(data, indent=2) + "\n")
    except OSError:
        pass


def drive_cached() -> dict:
    """The last known state of the account's Drive grant, without the network."""
    try:
        data = json.loads(_drive_cache_file().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def drive_token() -> tuple[str, float, str]:
    """An access token for Drive from the account: (token, expires_at, google email).

    Raises DriveGrantMissing when the account has no usable grant, and
    RuntimeError or a requests error when the service or Google cannot be
    reached. Cached until DRIVE_TOKEN_MARGIN seconds before expiry.
    """
    acct = load()
    if acct is None:
        raise DriveGrantMissing("this Mac is not signed in to an account")
    with _drive_lock:
        if _drive["account_id"] != acct.account_id:
            _drive.update(token="", expires_at=0.0, email="", account_id="")
        if _drive["token"] and time.time() < _drive["expires_at"] - DRIVE_TOKEN_MARGIN:
            return _drive["token"], _drive["expires_at"], _drive["email"]
        status, data = call("POST", "/drive/token", {}, token=acct.token)
        if status in (404, 409):
            _drive.update(token="", expires_at=0.0, email="", account_id="")
            _note_drive(False, detail=str(data.get("reason") or data.get("error") or ""))
            raise DriveGrantMissing(
                "the account has no Google Drive connection"
                + (f" ({data['reason']})" if data.get("reason") else ""))
        if status != 200 or not data.get("access_token"):
            raise RuntimeError(f"the account service answered {status} for a Drive token")
        expires_at = time.time() + float(data.get("expires_in") or 3600)
        _drive.update(token=str(data["access_token"]), expires_at=expires_at,
                      email=str(data.get("google_email") or ""), account_id=acct.account_id)
        _note_drive(True, _drive["email"])
        return _drive["token"], expires_at, _drive["email"]


def forget_drive_token() -> None:
    with _drive_lock:
        _drive.update(token="", expires_at=0.0, email="", account_id="")


def drive_status() -> dict:
    """Whether the account has a Drive grant, from the service. Raises on trouble."""
    acct = load()
    if acct is None:
        return {"connected": False}
    status, data = call("GET", "/drive/status", token=acct.token)
    if status != 200:
        raise RuntimeError(f"the account service answered {status}")
    _note_drive(bool(data.get("connected")), str(data.get("google_email") or ""),
                str(data.get("revoked_reason") or ""))
    return data


class AccountCredentials(google_credentials.Credentials):
    """Google credentials whose access token comes from the account service.

    google-auth calls refresh() before a request whenever the token is
    missing or about to expire, which is exactly when to ask the service
    for a new one. There is no refresh token here to leak: the Mac only
    ever holds the hour-long access token.
    """

    def __init__(self):
        super().__init__()
        self.token = None
        self.expiry = None
        self.google_email = ""

    def refresh(self, request=None) -> None:
        token, expires_at, email = drive_token()
        self.token = token
        # google-auth expects a naive UTC datetime here.
        self.expiry = datetime.fromtimestamp(expires_at, timezone.utc).replace(tzinfo=None)
        self.google_email = email
