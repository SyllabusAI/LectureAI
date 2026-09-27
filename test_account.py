"""Tests for account.py: claiming this Mac into a Syllabus account.

The account service is replaced by a scripted fake transport, so nothing here
reaches the network, and the account file lands in a throwaway home. From the
project root:

    .venv/bin/python test_account.py
"""
import json
import os
import stat
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_home import fresh_home  # noqa: E402

HOME = fresh_home()  # before config is imported

from intake import account, config, doctor  # noqa: E402
from intake import transcribe as transcribe_module  # noqa: E402


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


class FakeService:
    """Answers the calls account.py makes, in the order a real one would."""

    def __init__(self):
        self.calls = []
        self.approved = False
        self.polls_before_answer = 0
        self.fail_polls = 0        # raise on this many polls first (network down)
        self.expired = False
        self.token_ok = True

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, headers, body))
        assert url.startswith(SERVICE + "/"), url
        path = url[len(SERVICE):]
        if path == "/device/start":
            return 200, {"device_code": "dc-secret", "user_code": "WXYZ-2345",
                         "verification_uri": SERVICE + "/device",
                         "verification_uri_complete": SERVICE + "/device?code=WXYZ-2345",
                         "expires_in": 900, "interval": 5}
        if path == "/device/poll":
            assert body == {"device_code": "dc-secret"}, body
            if self.fail_polls:
                self.fail_polls -= 1
                raise ConnectionError("no route to host")
            if self.expired:
                return 400, {"error": "expired_token"}
            if self.polls_before_answer:
                self.polls_before_answer -= 1
                return 400, {"error": "authorization_pending", "interval": 5}
            return 200, {"token": "syd_abc", "account": {"id": "a1", "email": "me@example.com", "name": "Me"},
                         "device": {"id": "d1", "name": body and "Kitchen iMac", "profile": "syllabus"}}
        if path == "/me":
            if headers.get("Authorization") != "Bearer syd_abc" or not self.token_ok:
                return 401, {"error": "invalid_token"}
            return 200, {"account": {"id": "a1", "email": "me@example.com"}, "device": {"id": "d1"}}
        if path == "/device/revoke":
            assert headers.get("Authorization") == "Bearer syd_abc", headers
            return 200, {"ok": True}
        raise AssertionError(f"unexpected call {method} {path}")


def reset(service=None):
    account.forget()
    account.cancel_claim()
    account._set_claim(error="")
    config.ACCOUNTS_URL = SERVICE
    account.transport = service or FakeService()
    return account.transport


def t1():
    reset()
    assert account.enabled()
    assert account.summary() == {"enabled": True, "url": SERVICE, "signed_in": False}, account.summary()
    for word in ("off", "OFF", "none", "0", "", "  "):
        config.ACCOUNTS_URL = word
        assert not account.enabled(), word
        assert account.summary() == {"enabled": False, "signed_in": False}
        assert doctor.check_account() is None, "no doctor line when accounts are off"
    config.ACCOUNTS_URL = SERVICE + "/"
    assert account.url() == SERVICE, "a trailing slash is tolerated"
results.append(run("accounts are on by default and ACCOUNTS_URL=off hides them", t1))


def t2():
    reset()
    acct = account.Account("syd_abc", "a1", "me@example.com", "Me", "d1", "Kitchen iMac",
                           "syllabus", SERVICE, "2026-09-12T17:00:00")
    account.save(acct)
    assert config.ACCOUNT_FILE.exists()
    assert stat.S_IMODE(config.ACCOUNT_FILE.stat().st_mode) == 0o600, "the token file is private"
    assert account.load() == acct
    public = acct.public()
    assert "token" not in public and public["email"] == "me@example.com", public
    assert account.summary()["signed_in"] and account.summary()["device_name"] == "Kitchen iMac"
    check = doctor.check_account()
    assert check.ok and "me@example.com" in check.detail and "Kitchen iMac" in check.detail, check
    config.ACCOUNT_FILE.write_text("not json")
    assert account.load() is None, "a broken file is nobody, not a crash"
    account.forget()
    assert not config.ACCOUNT_FILE.exists()
    assert doctor.check_account().detail.startswith("not signed in"), doctor.check_account()
results.append(run("the account file round-trips, is private, and never leaks the token", t2))


def t3():
    service = reset()
    service.polls_before_answer = 2
    started = account.start_claim("Kitchen iMac", background=False)
    assert started["user_code"] == "WXYZ-2345" and started["running"], started
    assert started["verification_uri_complete"].endswith("code=WXYZ-2345")
    assert service.calls[0][3] == {"name": "Kitchen iMac", "profile": "syllabus"}, service.calls[0]
    status = account.claim_status()
    assert status["running"] and 890 < status["expires_in"] <= 900, status

    slept = []
    acct = account.run_claim(started["device_code"], "Kitchen iMac", started["interval"],
                             status["expires_at"], sleep=slept.append)
    assert acct is not None and acct.email == "me@example.com" and acct.token == "syd_abc", acct
    assert acct.device_name == "Kitchen iMac" and acct.url == SERVICE
    assert slept == [5, 5, 5], slept
    assert len([c for c in service.calls if c[1].endswith("/device/poll")]) == 3
    assert account.load() == acct, "the account is saved once approved"
    assert not account.claim_status()["running"]
results.append(run("a claim shows a code, polls while pending, and saves the account on approval", t3))


def t4():
    service = reset()
    service.fail_polls = 2
    started = account.start_claim(background=False)
    acct = account.run_claim(started["device_code"], started["name"], 5,
                             account.claim_status()["expires_at"], sleep=lambda s: None)
    assert acct is not None, "network trouble is retried, not treated as a refusal"
    assert len([c for c in service.calls if c[1].endswith("/device/poll")]) == 3
    assert started["name"], "a device name is always sent, this Mac's by default"
results.append(run("polling survives the service being briefly unreachable", t4))


def t5():
    service = reset()
    service.expired = True
    started = account.start_claim(background=False)
    acct = account.run_claim(started["device_code"], "x", 5, account.claim_status()["expires_at"],
                             sleep=lambda s: None)
    assert acct is None and account.load() is None
    status = account.claim_status()
    assert not status["running"] and "expired" in status["error"], status

    # The clock running out locally says the same thing without another poll.
    service = reset()
    service.polls_before_answer = 99
    started = account.start_claim(background=False)
    clock = [0.0]

    def now():
        clock[0] += 400
        return clock[0]
    acct = account.run_claim(started["device_code"], "x", 5, 900, sleep=lambda s: None, now=now)
    assert acct is None and "expired" in account.claim_status()["error"]
results.append(run("an expired code ends the claim with a message, not a token", t5))


def t6():
    reset()
    account.save(account.Account("syd_abc", "a1", "me@example.com", "Me", "d1", "Mac",
                                 "syllabus", SERVICE, "x"))
    try:
        account.start_claim(background=False)
        raise AssertionError("a signed-in Mac must not start another claim")
    except RuntimeError as exc:
        assert "already signed in" in str(exc), exc
    account.forget()
    account.start_claim(background=False)
    try:
        account.start_claim(background=False)
        raise AssertionError("two claims at once must be refused")
    except RuntimeError as exc:
        assert "already waiting" in str(exc), exc
    account.cancel_claim()
    assert not account.claim_status()["running"]
    config.ACCOUNTS_URL = "off"
    try:
        account.start_claim(background=False)
        raise AssertionError("claims must be refused when accounts are off")
    except RuntimeError as exc:
        assert "turned off" in str(exc), exc
results.append(run("only one claim at a time, none when signed in or turned off", t6))


def t7():
    service = reset()
    assert account.whoami() == ("none", "not signed in")
    account.save(account.Account("syd_abc", "a1", "me@example.com", "Me", "d1", "Mac",
                                 "syllabus", SERVICE, "x"))
    assert account.whoami() == ("ok", "me@example.com"), account.whoami()
    service.token_ok = False
    state, detail = account.whoami()
    assert state == "revoked", (state, detail)
    assert account.load() is None, "a token the service rejects is forgotten"

    account.save(account.Account("syd_abc", "a1", "me@example.com", "Me", "d1", "Mac",
                                 "syllabus", SERVICE, "x"))

    def down(*a, **k):
        raise ConnectionError("offline")
    account.transport = down
    state, detail = account.whoami()
    assert state == "unreachable" and "offline" in detail, (state, detail)
    assert account.load() is not None, "being offline is not being signed out"
results.append(run("whoami confirms, forgets a revoked token, and shrugs at being offline", t7))


def t8():
    service = reset()
    account.save(account.Account("syd_abc", "a1", "me@example.com", "Me", "d1", "Mac",
                                 "syllabus", SERVICE, "x"))
    account.sign_out()
    assert account.load() is None
    assert service.calls[-1][1].endswith("/device/revoke"), service.calls
    # Offline: signed out here regardless.
    account.save(account.Account("syd_abc", "a1", "me@example.com", "Me", "d1", "Mac",
                                 "syllabus", SERVICE, "x"))

    def down(*a, **k):
        raise ConnectionError("offline")
    account.transport = down
    account.sign_out()
    assert account.load() is None
results.append(run("signing out revokes the token when it can, and forgets it either way", t8))


def t9():
    name = account.device_name()
    assert isinstance(name, str) and name.strip(), repr(name)
results.append(run("this Mac has a name to sign in under", t9))


def t11():
    service = reset()
    account.forget_drive_token()
    account.save(account.Account("syd_abc", "a1", "me@example.com", "Me", "d1", "Mac",
                                 "syllabus", SERVICE, "x"))
    grant = {"state": "ok"}

    def answers(method, url, headers, body, timeout):
        service.calls.append((method, url, headers, body))
        path = url[len(SERVICE):]
        assert headers.get("Authorization") == "Bearer syd_abc", headers
        if path == "/drive/token":
            if grant["state"] == "ok":
                return 200, {"access_token": "ya29.one", "expires_in": 3600, "google_email": "me@gmail.com"}
            if grant["state"] == "none":
                return 404, {"error": "no_grant"}
            if grant["state"] == "revoked":
                return 409, {"error": "grant_revoked", "reason": "Google no longer honors the grant"}
            return 503, {"error": "backend"}
        if path == "/drive/status":
            return 200, {"connected": grant["state"] == "ok", "google_email": "me@gmail.com"}
        raise AssertionError(path)
    account.transport = answers

    token, expires_at, email = account.drive_token()
    assert token == "ya29.one" and email == "me@gmail.com" and expires_at > time.time() + 3000
    assert account.drive_token()[0] == "ya29.one"
    assert len([c for c in service.calls if c[1].endswith("/drive/token")]) == 1, "cached until near expiry"
    assert account.drive_cached()["connected"] and account.drive_cached()["google_email"] == "me@gmail.com"

    creds = account.AccountCredentials()
    assert not creds.valid
    creds.refresh(None)
    assert creds.valid and creds.token == "ya29.one" and creds.expiry.tzinfo is None, creds.expiry
    assert creds.google_email == "me@gmail.com"

    account.forget_drive_token()
    grant["state"] = "none"
    try:
        account.drive_token()
        raise AssertionError("no grant must raise DriveGrantMissing")
    except account.DriveGrantMissing:
        pass
    assert account.drive_cached()["connected"] is False
    grant["state"] = "revoked"
    try:
        account.drive_token()
        raise AssertionError("a revoked grant must raise DriveGrantMissing")
    except account.DriveGrantMissing as exc:
        assert "no longer honors" in str(exc), exc
    grant["state"] = "down"
    try:
        account.drive_token()
        raise AssertionError("a 5xx is not a missing grant")
    except RuntimeError:
        pass
    assert account.drive_status() == {"connected": False, "google_email": "me@gmail.com"}
results.append(run("Drive tokens come from the account, are cached, and a missing grant is its own error", t11))


def t12():
    from intake import upload
    import json as _json
    service = reset()
    account.forget_drive_token()
    upload._said_no_grant = False
    grant = {"state": "ok", "down": False}

    def answers(method, url, headers, body, timeout):
        if grant["down"]:
            raise ConnectionError("offline")
        path = url[len(SERVICE):]
        if path == "/drive/token":
            if grant["state"] == "ok":
                return 200, {"access_token": "ya29.acct", "expires_in": 3600, "google_email": "me@gmail.com"}
            return 404, {"error": "no_grant"}
        raise AssertionError(path)
    account.transport = answers
    config.TOKEN_FILE.unlink(missing_ok=True)

    # Not signed in: the account is not consulted, and with no token.json and
    # no browser allowed there is nothing to do but say so.
    account.forget()
    try:
        upload.get_credentials(interactive=False)
        raise AssertionError("no token and no browser must raise")
    except RuntimeError as exc:
        assert "intake login" in str(exc), exc

    account.save(account.Account("syd_abc", "a1", "me@example.com", "Me", "d1", "Mac",
                                 "syllabus", SERVICE, "x"))
    creds = upload.get_credentials(interactive=False)
    assert isinstance(creds, account.AccountCredentials) and creds.token == "ya29.acct"
    assert account.drive_cached().get("in_use") is True

    # The account has no grant: this Mac's own token is used when it has one.
    account.forget_drive_token()
    grant["state"] = "none"
    config.TOKEN_FILE.write_text(_json.dumps({
        "token": "ya29.local", "refresh_token": "1//local", "client_id": "c", "client_secret": "s",
        "token_uri": "https://oauth2.googleapis.com/token", "scopes": config.DRIVE_SCOPES,
        "expiry": "2099-01-01T00:00:00Z",
    }))
    creds = upload.get_credentials(interactive=False)
    assert not isinstance(creds, account.AccountCredentials) and creds.token == "ya29.local"

    # The account has a grant, but it cannot see the folder this Mac already
    # files into (made by another Cloud project): this Mac's token is kept,
    # unless DRIVE_SOURCE=account says to start over under the account.
    grant["state"] = "ok"
    account.forget_drive_token()
    upload._said_folder = False
    config.DRIVE_ROOT_CACHE.write_text("root-123\n")
    seen = {"visible": False, "checks": 0}

    def fake_root_visible(creds):
        seen["checks"] += 1
        return seen["visible"]
    real_root_visible = upload._root_visible
    upload._root_visible = fake_root_visible
    try:
        creds = upload.get_credentials(interactive=False)
        assert creds.token == "ya29.local" and seen["checks"] == 1, (creds.token, seen)
        cached = account.drive_cached()
        assert cached["connected"] and cached["in_use"] is False and "cannot see" in cached["in_use_detail"], cached
        check = doctor.check_drive_token()
        assert check.ok and "not used because" in check.detail, check
        config.DRIVE_SOURCE = "account"
        creds = upload.get_credentials(interactive=False)
        assert isinstance(creds, account.AccountCredentials), "DRIVE_SOURCE=account forces the grant"
        assert seen["checks"] == 1, "no folder check when forced"
        config.DRIVE_SOURCE = "local"
        assert upload.get_credentials(interactive=False).token == "ya29.local", "DRIVE_SOURCE=local forces the token"
        config.DRIVE_SOURCE = "auto"
        seen["visible"] = True
        creds = upload.get_credentials(interactive=False)
        assert isinstance(creds, account.AccountCredentials), "a grant that sees the folder is used"
        assert account.drive_cached()["in_use"] is True
        # With no token.json there is nothing to protect: the grant is used as is.
        seen["visible"] = False
        config.TOKEN_FILE.unlink()
        assert isinstance(upload.get_credentials(interactive=False), account.AccountCredentials)
        assert seen["checks"] == 2, "no folder check without a local token to fall back to"
        config.TOKEN_FILE.write_text(_json.dumps({
            "token": "ya29.local", "refresh_token": "1//local", "client_id": "c", "client_secret": "s",
            "token_uri": "https://oauth2.googleapis.com/token", "scopes": config.DRIVE_SCOPES,
            "expiry": "2099-01-01T00:00:00Z",
        }))
    finally:
        upload._root_visible = real_root_visible
        config.DRIVE_ROOT_CACHE.unlink(missing_ok=True)
        config.DRIVE_SOURCE = "auto"

    # The service is unreachable: the local token still works.
    account.forget_drive_token()
    grant["state"] = "ok"
    grant["down"] = True
    creds = upload.get_credentials(interactive=False)
    assert creds.token == "ya29.local", "offline falls back to this Mac's token"
    # Unreachable and no local token: an error that says why.
    config.TOKEN_FILE.unlink()
    try:
        upload.get_credentials(interactive=False)
        raise AssertionError("offline with nothing to fall back to must raise")
    except RuntimeError as exc:
        assert "account service" in str(exc) and "token.json" in str(exc), exc
    grant["down"] = False
    account.forget_drive_token()
results.append(run("the uploader prefers the account's grant and falls back to this Mac's token", t12))


# --- managed keys: the panel spending the service's keys, not its own -------
#
# Signed in, there is no OPENAI_API_KEY on this Mac and nothing is wrong with
# that. These cover the switch itself, the limits the two repos have to agree
# on, and what a refusal turns into for whoever has to read it.

from intake import gui, providers, summarize, watch  # noqa: E402


def signed_in(**over):
    """Put a claimed account on disk so account.managed() is true."""
    account.save(account.Account(
        token=over.get("token", "syd_abc"), account_id="a1", email="me@example.com",
        name="Me", device_id="d1", device_name="This Mac", profile="syllabus",
        url=SERVICE, claimed_at="2026-09-15T00:00:00Z"))


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def t13():
    # Signed in, the proxy is the provider, whatever TRANSCRIBE_MODEL says.
    signed_in()
    assert account.managed()
    chosen = providers.get()
    assert isinstance(chosen, providers.ProxyProvider), chosen
    assert chosen.name == "syllabus"
    # It brings its own credential, so there is no .env key to demand.
    assert chosen.api_key_setting == ""
    # Naming one explicitly still wins, for a one-off comparison.
    assert providers.get("whisper-1").name == "whisper-1"
    # Signed out, nothing changed: TRANSCRIBE_MODEL decides as it always did.
    account.forget()
    assert not account.managed()
    assert providers.get().name == config.TRANSCRIBE_MODEL
    signed_in()
results.append(run("a signed-in Mac transcribes through the proxy, a signed-out one does not", t13))


def t14():
    # The numbers the two repos have to agree on. If syllabus-accounts moves
    # MAX_AUDIO_BYTES or MAX_CHUNK_SECONDS, this is what should fail first.
    prov = providers.ProxyProvider()
    assert prov.MAX_AUDIO_BYTES == 12 * 1024 * 1024, "proxy.ts MAX_AUDIO_BYTES"
    assert prov.MAX_CHUNK_SECONDS == 1800, "proxy.ts MAX_CHUNK_SECONDS"
    assert prov.max_bytes == prov.MAX_AUDIO_BYTES
    assert prov.compress_threshold_bytes < prov.max_bytes, "compress before it is refused"
    # The binding limit is the upstream model's, not the proxy's: the proxy
    # would meter 30 minutes, but it transcribes on Groq with OpenAI behind
    # it, and any chunk may land on that fallback leg without this Mac being
    # told. So the limit has to hold for the upstream that truncates.
    assert prov.max_chunk_seconds == config.CHUNK_SECONDS
    assert prov.max_chunk_seconds < prov.MAX_CHUNK_SECONDS
    assert prov.truncation_word_threshold == config.TRUNCATION_WORD_THRESHOLD
results.append(run("the proxy provider's limits are the smaller of the proxy's and its model's", t14))


def t15():
    signed_in()
    clip = config.WORK_DIR / "chunk.m4a"
    clip.write_bytes(b"x" * 2048)
    sent = {}

    def fake_post(url, token, path, seconds, timeout):
        sent.update(url=url, token=token, name=Path(path).name, seconds=seconds)
        return FakeResponse(200, {"text": "  hello from the proxy  ", "audio_seconds": 61})

    real_post, real_duration = providers._post_audio, transcribe_module.duration_seconds
    providers._post_audio = fake_post
    transcribe_module.duration_seconds = lambda p: 60.5
    try:
        text = providers.ProxyProvider().transcribe_file(clip)
    finally:
        providers._post_audio, transcribe_module.duration_seconds = real_post, real_duration
    assert text == "hello from the proxy", repr(text)
    assert sent["url"] == SERVICE + "/proxy/transcribe", sent
    assert sent["token"] == "syd_abc", "the device token is the whole credential"
    assert sent["seconds"] == 60.5, "the proxy meters on a duration we send"
    clip.unlink()
results.append(run("a chunk goes up with the device token and the duration it is metered on", t15))


def t16():
    signed_in()
    clip = config.WORK_DIR / "chunk.m4a"
    clip.write_bytes(b"x" * 2048)
    real_post, real_duration = providers._post_audio, transcribe_module.duration_seconds
    transcribe_module.duration_seconds = lambda p: 60.0
    cases = [
        (402, {"error": "allowance_exhausted", "used": 17000, "allowance": 18000},
         "allowance_exhausted", "4.7"),
        (429, {"error": "rate_limited", "limit": 20}, "rate_limited", "retry"),
        (502, {"error": "provider_unavailable"}, "provider_unavailable", "could not be reached"),
        (500, None, "http_500", "refused"),
    ]
    try:
        for status, payload, expect_error, expect_text in cases:
            providers._post_audio = lambda *a, s=status, p=payload, **k: FakeResponse(s, p)
            try:
                providers.ProxyProvider().transcribe_file(clip)
            except providers.ProxyRefused as exc:
                assert exc.error == expect_error, (exc.error, expect_error)
                assert exc.status == status, exc.status
                assert expect_text in str(exc), (str(exc), expect_text)
            else:
                raise AssertionError(f"{status} should have raised")
    finally:
        providers._post_audio, transcribe_module.duration_seconds = real_post, real_duration
    clip.unlink()
results.append(run("a refusal says which one it was, and the allowance one says how much is left", t16))


def t16b():
    # A truncated chunk's halves each go up metered on their own length, and
    # a retry after the second half fails meters that half and nothing more:
    # not the whole chunk again, not the half that already came back.
    signed_in()
    work = config.WORK_DIR / "metered"
    work.mkdir(parents=True, exist_ok=True)
    src = work / "lecture.m4a"
    src.write_bytes(b"x" * 1000)
    checkpoint = work / "checkpoint"
    chunk = config.CHUNK_SECONDS
    long_text = " ".join(["word"] * config.TRUNCATION_WORD_THRESHOLD)
    posted = []

    def fake_split(path, work_dir, seconds):
        path = Path(path)
        if path == src:
            names = ["chunk_001", "chunk_002"]
        else:
            names = [f"{path.stem}.{n}" for n in range(2)]
        out = [Path(work_dir) / f"{n}.m4a" for n in names]
        for p in out:
            p.write_bytes(b"x" * 1000)
        return out

    def fake_duration(path):
        name = Path(path).stem
        if not name.startswith("chunk_"):
            return 2.0 * chunk
        return chunk / 2 ** name.count(".")

    def fake_post(fail):
        def post(url, token, path, seconds, timeout):
            name = Path(path).stem
            posted.append((name, seconds))
            if name == fail:
                return FakeResponse(502, {"error": "provider_unavailable"})
            return FakeResponse(200, {"text": long_text if name == "chunk_002"
                                      else f"words of {name}"})
        return post

    saved = {name: getattr(transcribe_module, name)
             for name in ("split", "duration_seconds", "log")}
    real_post = providers._post_audio
    transcribe_module.split = fake_split
    transcribe_module.duration_seconds = fake_duration
    transcribe_module.log = lambda msg: None
    try:
        providers._post_audio = fake_post("chunk_002.1")
        try:
            transcribe_module.transcribe(src, provider=providers.ProxyProvider(),
                                         checkpoint=checkpoint)
        except providers.ProxyRefused:
            pass
        else:
            raise AssertionError("the refusal on the second half was swallowed")
        assert posted == [("chunk_001", chunk), ("chunk_002", chunk),
                          ("chunk_002.0", chunk / 2), ("chunk_002.1", chunk / 2)], posted
        posted.clear()
        providers._post_audio = fake_post(None)
        text = transcribe_module.transcribe(src, provider=providers.ProxyProvider(),
                                            checkpoint=checkpoint)
    finally:
        providers._post_audio = real_post
        for name, value in saved.items():
            setattr(transcribe_module, name, value)
    assert posted == [("chunk_002.1", chunk / 2)], f"the retry re-metered: {posted}"
    assert text == "words of chunk_001\n\nwords of chunk_002.0\n\nwords of chunk_002.1", text
    import shutil
    shutil.rmtree(work, ignore_errors=True)
results.append(run("a truncated chunk's halves are metered on their own length, once", t16b))


def t17():
    signed_in()
    seen = {}

    def answers(method, url, headers, body, timeout):
        seen.update(path=url[len(SERVICE):], body=body, auth=headers.get("Authorization"),
                    timeout=timeout)
        return 200, {"summary": {
            "summary_md": "  ## Notes  ", "topic_slug": "Job Order Costing",
            "key_terms": [{"term": " POHR ", "definition": " a rate "}, {"term": "", "definition": "x"}],
            "action_items": [{"task": "Read chapter 3", "detail": "", "due_date": "",
                              "kind": "reading"}],
        }, "tokens": 14883}

    real = account.transport
    account.transport = answers
    config.set_schedule({("Tue", 14): "ACCT-4321", ("Thu", 14): "ACCT-4321"})
    try:
        result = summarize.summarize("a real transcript", "ACCT-4321", "2026-09-01")
    finally:
        account.transport = real
        config.set_schedule(None)
    assert seen["path"] == "/proxy/summarize", seen
    assert seen["auth"] == "Bearer syd_abc", seen
    assert seen["body"]["subject"] == "ACCT-4321" and seen["body"]["date"] == "2026-09-01"
    assert "transcript" in seen["body"]
    # Claude reading a whole lecture outlasts the control-plane timeout, which
    # is how a summary got paid for and then thrown away in testing.
    assert seen["timeout"] == account.SLOW_TIMEOUT > account.TIMEOUT, seen["timeout"]
    # The proxy's JSON is shaped by exactly the code the local path uses.
    assert result["summary_md"] == "## Notes"
    assert result["topic_slug"] == "Job-Order-Costing"
    assert result["key_terms"] == [{"term": "POHR", "definition": "a rate"}]
    assert len(result["action_items"]) == 1
    assert result["action_items"][0]["date_source"] == "assumed", result["action_items"]
results.append(run("a summary comes back through the proxy, shaped like a local one", t17))


def t18():
    # Nothing may ask a managed Mac for a key it is not supposed to have.
    signed_in()
    config.set_schedule({("Tue", 14): "ACCT-4321"})
    saved = config.OPENAI_API_KEY, config.ANTHROPIC_API_KEY
    config.OPENAI_API_KEY = config.ANTHROPIC_API_KEY = ""
    real = watch.drive.get_service
    watch.drive.get_service = lambda interactive: None
    try:
        watch.preflight(False)          # must not raise
        assert gui._configured(), "a signed-in Mac with classes is ready"
        assert gui._integrations()["managed"] is True
        assert gui._integrations()["openai"] is True, "shown as provided, not missing"
        assert [c for c in doctor._key_checks() if c.ok and "managed" in c.detail], \
            "doctor must not report a missing key as a fault"
        # Signed out with no keys, the old demand is back.
        account.forget()
        assert not gui._configured()
        try:
            watch.preflight(False)
        except RuntimeError:
            pass
        else:
            raise AssertionError("a signed-out Mac with no keys must still be stopped")
    finally:
        watch.drive.get_service = real
        config.OPENAI_API_KEY, config.ANTHROPIC_API_KEY = saved
        config.set_schedule(None)
        signed_in()
results.append(run("a managed Mac is never asked for a key, a signed-out one still is", t18))


def t19():
    # The proxy now refuses a blank, but this Mac must not file one either:
    # shaped rather than checked, an empty payload becomes a note with no
    # words in it, named for the fallback slug, uploaded to Drive.
    signed_in()
    for payload in ({}, {"topic_slug": "Job-Order-Costing", "key_terms": []},
                    {"summary_md": "   "}):
        real = account.transport
        account.transport = lambda *a, p=payload, **k: (200, {"summary": p, "tokens": 12})
        try:
            summarize.summarize("a real transcript", "ACCT-4321", "2026-09-01")
        except RuntimeError as exc:
            assert "empty summary" in str(exc), exc
            assert "run again" in str(exc), "say that the recording survived"
        else:
            raise AssertionError(f"{payload!r} should not become a note")
        finally:
            account.transport = real
    # And a real one still passes straight through.
    real = account.transport
    account.transport = lambda *a, **k: (200, {"summary": {
        "summary_md": "## Real notes", "topic_slug": "Job-Order-Costing",
        "key_terms": [], "action_items": []}, "tokens": 12})
    try:
        assert summarize.summarize("t", "ACCT-4321", "2026-09-01")["summary_md"]
    finally:
        account.transport = real
results.append(run("an empty summary is an error, not a blank note filed to Drive", t19))


def t19b():
    """The 2026-09-23 ENTR-4306 lecture, which was filed rather than refused.

    The model wrote the notes, the slug, the key terms and both of that
    Friday's to-dos into summary_md as tag-delimited text and left the other
    three fields empty. summary_md was long and real, so every check passed
    and the reading went to Drive as markup in the body instead of to Notion.
    """
    signed_in()
    leaked = {
        "summary_md": "The lecture builds on the execution engine. "
                      "</summary_md> <topic_slug>Execution-Vs-Innovation</topic_slug>",
        "topic_slug": "", "key_terms": [], "action_items": [],
    }
    hollow = {"summary_md": "## Real notes, and nothing around them",
              "topic_slug": "", "key_terms": [], "action_items": []}
    for payload, expected in ((leaked, "wrote its other fields"),
                              (hollow, "no topic, no key terms")):
        real = account.transport
        account.transport = lambda *a, p=payload, **k: (200, {"summary": p, "tokens": 12})
        try:
            summarize.summarize("a real transcript", "ENTR-4306", "2026-09-23")
        except RuntimeError as exc:
            assert expected in str(exc), exc
            assert "run again" in str(exc), "say that the recording survived"
        else:
            raise AssertionError(f"{payload!r} should not become a note")
        finally:
            account.transport = real
    # A thin lecture is still a lecture: any ONE of the three being empty is
    # normal, and a summary that named its topic keeps going.
    real = account.transport
    account.transport = lambda *a, **k: (200, {"summary": {
        "summary_md": "## Short class", "topic_slug": "Process-Costing",
        "key_terms": [], "action_items": []}, "tokens": 12})
    try:
        assert summarize.summarize("t", "ACCT-4321", "2026-09-01")["summary_md"]
    finally:
        account.transport = real
results.append(run("a summary that swallowed the other fields is refused, not filed", t19b))


def t20():
    """A refusal names the meter that ran out, in that meter's own units.

    The morning this was written, a summary refused for want of 1,902 tokens
    was reported as "this account has used its transcription allowance for
    the month", and the transcription allowance was two thirds spent. The
    message came from the error code, and both meters refuse with the same
    one.
    """
    signed_in()
    refused = {"error": "allowance_exhausted", "kind": "summarize", "unit": "tokens",
               "used": 123427, "requested": 28475, "allowance": 150000}
    real = account.transport
    account.transport = lambda *a, **k: (402, refused)
    try:
        summarize.summarize("a real transcript", "ENTR-3306", "2026-09-17")
    except providers.ProxyRefused as exc:
        said = str(exc)
        assert "summary tokens" in said, said
        assert "123,427" in said and "150,000" in said, said
        assert "transcription" not in said, said
        assert "hours" not in said, said
        assert exc.error == "allowance_exhausted" and exc.status == 402
    else:
        raise AssertionError("an exhausted summary allowance must refuse")
    finally:
        account.transport = real

    # The transcribe side keeps its own units, and now says which meter too.
    audio = providers.refusal_reason(
        {"error": "allowance_exhausted", "kind": "transcribe", "unit": "audio_seconds",
         "used": 17000, "allowance": 18000})
    assert "4.7" in audio and "5.0" in audio and "transcription hours" in audio, audio
    # A body with no numbers still has to say which one, not guess at both.
    bare = providers.refusal_reason({"error": "allowance_exhausted", "kind": "summarize"})
    assert bare == "this account has used its summary allowance for the month", bare
results.append(run("a refusal names the meter that ran out, in that meter's units", t20))


# --- SEC-02: a credential goes where it was issued, or nowhere -------------

def t_bound_destination():
    """ACCOUNTS_URL cannot redirect this Mac's bearer to another service.

    The setting lives in an editable file, and the token used to follow it
    wherever it pointed, so one line changed by a settings-injection bug was
    enough to hand the bearer to somebody else's server.
    """
    sent = []
    real = account.transport
    saved_url = config.ACCOUNTS_URL
    try:
        account.transport = lambda m, u, h, b, t: (
            sent.append((u, h.get("Authorization", ""))), (200, {}))[1]
        account.save(account.Account(
            token="syd_SECRET", account_id="a", email="e@x", name="n",
            device_id="d", device_name="m", profile="syllabus",
            url="https://real.example", claimed_at="now"))

        config.ACCOUNTS_URL = "https://real.example"
        account.call("GET", "/me", token="syd_SECRET")
        assert sent[-1][0] == "https://real.example/me", sent[-1]
        assert sent[-1][1] == "Bearer syd_SECRET", sent[-1]

        config.ACCOUNTS_URL = "https://collector.invalid"
        before = len(sent)
        try:
            account.call("GET", "/me", token="syd_SECRET")
            raise AssertionError("the bearer was sent to the new address")
        except account.ServiceMoved as exc:
            assert "real.example" in str(exc), exc
        assert len(sent) == before, "a request was made despite the refusal"

        # A call with nothing to steal still follows the setting: the claim
        # flow runs before there is an account to bind to.
        account.call("POST", "/device/start", {"name": "x"})
        assert sent[-1][0] == "https://collector.invalid/device/start", sent[-1]
        assert sent[-1][1] == "", sent[-1]

        # Plain http is refused even when it is what the account was claimed at.
        account.save(account.Account(
            token="syd_SECRET", account_id="a", email="e@x", name="n",
            device_id="d", device_name="m", profile="syllabus",
            url="http://plain.example", claimed_at="now"))
        config.ACCOUNTS_URL = "http://plain.example"
        before = len(sent)
        try:
            account.call("GET", "/me", token="syd_SECRET")
            raise AssertionError("the bearer went out over plain http")
        except account.ServiceMoved as exc:
            assert "https" in str(exc), exc
        assert len(sent) == before
    finally:
        account.transport = real
        config.ACCOUNTS_URL = saved_url
        account.forget()
results.append(run("a device token only goes to the service that issued it",
                   t_bound_destination))


print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
