"""Tests for assistant.py: the library, the two stages, and the instrumentation.

Nothing here talks to Drive or to Anthropic. The Drive service is a stub that
answers from a dict, and the model is never called: what is tested is the
context this module builds and the bookkeeping it keeps, which is where the
cost model lives and where the bugs were.

    .venv/bin/python test_assistant.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_home import fresh_home  # noqa: E402

fresh_home()  # before config is imported, so nothing touches ~/.intake

from intake import account as _account, assistant, config  # noqa: E402

account_call = _account.call

LOG = (
    "2026-09-15T10:00:00\tACCT-4321\ta.m4a\tACCT-4321_2026-09-15_Process-Costing"
    '\thttps://docs.google.com/document/d/1aBcProcessCosting15xyzQ/edit\t\t{"terms":9,"actions":1}\n'
    "2026-09-17T10:00:00\tACCT-4321\tb.m4a\tACCT-4321_2026-09-17_Cost-Volume-Profit"
    '\thttps://docs.google.com/document/d/1aBcCostVolumeProfit17xyQ/edit\t\t{"terms":4,"actions":0}\n'
    "2026-09-18T10:00:00\tRELI-3304\tc.m4a\tRELI-3304_2026-09-18_Inspiration"
    "\thttps://docs.google.com/document/d/1aBcInspiration18xyzabcQ/edit\n"
    "2026-09-18T11:00:00\tERROR\td.m4a\tsomething went wrong\n"
)


class FakeFiles:
    """Just enough of Drive's files() for the two stages."""

    def __init__(self, store, native, calls):
        self.store, self.native, self.calls = store, native, calls

    def _exec(self, value):
        class _R:
            def execute(_):
                if isinstance(value, Exception):
                    raise value
                return value
        return _R()

    def get(self, fileId, fields=None):
        self.calls.append(("get", fileId))
        if fileId not in self.store:
            return self._exec(RuntimeError(f"no such file {fileId}"))
        kind = ("application/vnd.google-apps.document"
                if fileId in self.native else "text/markdown")
        return self._exec({"mimeType": kind})

    def export(self, fileId, mimeType):
        self.calls.append(("export", fileId))
        if fileId not in self.native:
            return self._exec(RuntimeError("Export only supports Docs Editors files."))
        return self._exec(self.store[fileId].encode())

    def get_media(self, fileId):
        self.calls.append(("get_media", fileId))
        return self._exec(self.store[fileId].encode())

    def list(self, **kw):
        self.calls.append(("list", kw.get("q", "")))
        found = [{"id": k, "name": k} for k in self.store
                 if k.startswith("TRANSCRIPT:") and k[11:] in kw.get("q", "")]
        return self._exec({"files": found})


class FakeService:
    def __init__(self, store, native=(), calls=None):
        self.calls = [] if calls is None else calls
        self._files = FakeFiles(store, set(native) or set(store), self.calls)

    def files(self):
        return self._files


def setup(log=LOG):
    """A fresh home with a known log and an empty summary cache."""
    home = fresh_home()
    config.HOME_DIR = config.BASE_DIR = home
    config.LOG_FILE = home / "pipeline.log"
    config.LOG_FILE.write_text(log)
    assistant._MEMORY.clear()
    assistant._legacy_swept = False
    log_file = home / "assistant.log"
    if log_file.exists():
        log_file.unlink()
    return home


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


def t1():
    setup()
    lecs = assistant.library()
    assert len(lecs) == 3, f"expected 3 lectures, got {len(lecs)}"
    # Newest first, and the ERROR line is not a lecture.
    assert lecs[0].date == "2026-09-18", lecs[0].date
    assert all(l.course != "ERROR" for l in lecs), "an error line became a lecture"
results.append(run("the library reads filed lectures and skips errors", t1))


def t2():
    setup()
    lec = [l for l in assistant.library() if l.name.endswith("Process-Costing")][0]
    assert lec.doc_id == "1aBcProcessCosting15xyzQ", lec.doc_id
    assert lec.topic == "Process Costing", lec.topic
    assert lec.title == "ACCT-4321 2026-09-15: Process Costing", lec.title
results.append(run("a lecture knows its doc id, topic, and citable title", t2))


def t3():
    setup()
    found = assistant.courses()
    assert [c["course"] for c in found] == ["ACCT-4321", "RELI-3304"], found
    assert found[0]["lectures"] == 2, found[0]
results.append(run("courses are listed by how many lectures they have", t3))


def t4():
    setup()
    svc = FakeService({"1aBcProcessCosting15xyzQ": "costing summary", "1aBcCostVolumeProfit17xyQ": "cvp summary"})
    lecs = [l for l in assistant.library() if l.course == "ACCT-4321"]
    blocks = assistant.stage_one(svc, lecs)
    assert len(blocks) == 2, f"expected 2 documents, got {len(blocks)}"
    assert all(b["citations"]["enabled"] for b in blocks), "citations are off"
    # The breakpoint goes on the last block and nowhere else, or the whole
    # prefix is not actually cached.
    cached = [i for i, b in enumerate(blocks) if "cache_control" in b]
    assert cached == [len(blocks) - 1], f"cache breakpoints at {cached}"
results.append(run("stage one builds cited documents with one cache breakpoint", t4))


def t5():
    # The bug that took the whole course down: a lecture filed before the
    # pipeline converted summaries to Docs is still text/markdown, and
    # export() refuses it.
    setup()
    svc = FakeService({"1aBcProcessCosting15xyzQ": "costing summary", "1aBcCostVolumeProfit17xyQ": "markdown summary"},
                      native=["1aBcProcessCosting15xyzQ"])
    lecs = [l for l in assistant.library() if l.course == "ACCT-4321"]
    blocks = assistant.stage_one(svc, lecs)
    assert len(blocks) == 2, f"a markdown summary was dropped: {len(blocks)} blocks"
    assert any("markdown summary" in b["source"]["data"] for b in blocks), blocks
results.append(run("a summary that is not a Doc is downloaded, not skipped", t5))


def t6():
    # One unreadable lecture must cost the student that lecture, not the course.
    setup()
    svc = FakeService({"1aBcProcessCosting15xyzQ": "costing summary"})     # 1aBcCostVolumeProfit17xyQ missing entirely
    lecs = [l for l in assistant.library() if l.course == "ACCT-4321"]
    blocks = assistant.stage_one(svc, lecs)
    assert len(blocks) == 1, f"expected the readable one, got {len(blocks)}"
results.append(run("an unreadable lecture is skipped, not raised", t6))


def t7():
    setup()
    store = {"1aBcProcessCosting15xyzQ": "s", "1aBcCostVolumeProfit17xyQ": "s"}
    calls = []
    svc = FakeService(store, calls=calls)
    lecs = [l for l in assistant.library() if l.course == "ACCT-4321"]
    assistant.stage_one(svc, lecs)
    first = len([c for c in calls if c[0] in ("export", "get_media")])
    assistant.stage_one(svc, lecs)
    second = len([c for c in calls if c[0] in ("export", "get_media")])
    assert second == first, "the second read went back to Drive instead of the cache"
results.append(run("a summary is fetched from Drive once and then cached", t7))


def t8():
    setup()
    store = {"1aBcProcessCosting15xyzQ": "s", "1aBcCostVolumeProfit17xyQ": "s",
             "TRANSCRIPT:ACCT-4321_2026-09-15_Process-Costing": "the full words"}
    svc = FakeService(store)
    lecs = [l for l in assistant.library() if l.course == "ACCT-4321"]
    blocks, used = assistant.stage_two(
        svc, lecs, ["ACCT-4321 2026-09-15: Process Costing"])
    assert used == ["ACCT-4321 2026-09-15: Process Costing"], used
    assert "the full words" in blocks[0]["source"]["data"], blocks
results.append(run("stage two fetches the transcript the model named", t8))


def t9():
    # The model quotes a title back at us rather than echoing an id, so a near
    # miss still has to find the lecture.
    setup()
    store = {"1aBcProcessCosting15xyzQ": "s", "1aBcCostVolumeProfit17xyQ": "s",
             "TRANSCRIPT:ACCT-4321_2026-09-15_Process-Costing": "the full words"}
    svc = FakeService(store)
    lecs = [l for l in assistant.library() if l.course == "ACCT-4321"]
    _, used = assistant.stage_two(svc, lecs, ["acct-4321 2026-09-15: process costing"])
    assert len(used) == 1, f"a case-different title did not match: {used}"
results.append(run("a transcript is matched on a loosely quoted title", t9))


def t10():
    setup()
    store = {"1aBcProcessCosting15xyzQ": "s", "1aBcCostVolumeProfit17xyQ": "s"}
    svc = FakeService(store)
    lecs = [l for l in assistant.library() if l.course == "ACCT-4321"]
    blocks, used = assistant.stage_two(svc, lecs, ["No Such Lecture"])
    assert blocks == [] and used == [], (blocks, used)
results.append(run("a transcript that cannot be found yields nothing, not an error", t10))


def t11():
    # The escalation cap is what stands in for the entitlement that is not
    # built yet, so it has to actually hold.
    setup()
    store = {"1aBcProcessCosting15xyzQ": "s", "1aBcCostVolumeProfit17xyQ": "s"}
    lecs = [l for l in assistant.library() if l.course == "ACCT-4321"]
    for lec in lecs:
        store[f"TRANSCRIPT:{lec.name}"] = "x" * (assistant.MAX_TRANSCRIPT_CHARS)
    svc = FakeService(store)
    _, used = assistant.stage_two(svc, lecs, [l.title for l in lecs])
    assert len(used) == 1, f"the size cap let {len(used)} transcripts through"
results.append(run("the transcript size cap holds", t11))


def t12():
    setup()
    assistant.record_session("ACCT-4321", "a question", False, [], {"output_tokens": 10})
    assistant.record_session("ACCT-4321", "another", True, ["ACCT-4321 x"], {})
    rate = assistant.escalation_rate()
    assert rate == {"sessions": 2, "escalated": 1, "rate": 0.5}, rate
    line = json.loads((config.BASE_DIR / "assistant.log").read_text().splitlines()[0])
    assert line["course"] == "ACCT-4321" and line["escalated"] is False, line
results.append(run("every session is logged and the escalation rate is measured", t12))


def t13():
    setup()
    events = list(assistant.ask("anything", "PHYS-1000"))
    assert events[-1]["type"] == "error", events
    assert "PHYS-1000" in events[-1]["text"], events[-1]
    # Nothing was billed, so nothing is logged.
    assert assistant.escalation_rate()["sessions"] == 0, "an unasked question was logged"
results.append(run("asking about a course with no lectures never calls the model", t13))


def t14():
    setup()
    events = list(assistant.ask("   ", "ACCT-4321"))
    assert events == [{"type": "error", "text": "Ask a question first."}], events
results.append(run("an empty question is refused before anything is fetched", t14))


def t15():
    # The assistant's model is a costing decision; a silent change to Opus is
    # the difference between a 53% margin and a 29% one.
    assert config.ASSISTANT_MODEL == "claude-sonnet-5", config.ASSISTANT_MODEL
results.append(run("the assistant runs on Sonnet 5", t15))


# --- The managed path: the same stages through /proxy/assistant --------------

STORE = {"1aBcProcessCosting15xyzQ": "process summary",
         "1aBcCostVolumeProfit17xyQ": "cvp summary",
         "TRANSCRIPT:ACCT-4321_2026-09-15_Process-Costing": "the full words"}


class Service:
    """/proxy/assistant, scripted: one (status, events-or-body) per call."""

    def __init__(self, *replies):
        self.replies, self.bodies = list(replies), []

    def __call__(self, body):
        self.bodies.append(json.loads(json.dumps(body)))
        status, got = self.replies.pop(0)
        return status, (iter(got) if status == 200 else got)


def managed(service):
    setup()
    assistant._SESSIONS.clear()
    assistant.managed = lambda: True
    assistant.stream_transport = service
    return FakeService(dict(STORE))


def answer(session="sess_1", opened=True, text="Process costing pools costs."):
    return (200, [
        {"type": "session", "id": session, "opened": opened, "questions_left": 11},
        {"type": "text", "text": text},
        {"type": "citation", "title": "ACCT-4321 2026-09-15: Process Costing"},
        {"type": "done", "session_id": session, "stop_reason": "end_turn", "escalating": False,
         "cost_microusd": 41000, "input_tokens": 900, "output_tokens": 300,
         "cache_read_tokens": 0, "cache_write_tokens": 5000},
    ])


def t16():
    svc = Service(answer())
    drive = managed(svc)
    # No key is set in this home: reaching the BYO path would fail loudly.
    events = list(assistant.ask("What is process costing?", "ACCT-4321", service=drive))
    kinds = [e["type"] for e in events]
    assert kinds == ["status", "text", "citation", "done"], events
    sent = svc.bodies[0]
    assert "session_id" not in sent, sent
    assert sent["question"] == "What is process costing?", sent
    assert [d["title"] for d in sent["summaries"]] == [
        "ACCT-4321 2026-09-17: Cost Volume Profit", "ACCT-4321 2026-09-15: Process Costing"], sent
    assert all(set(d) == {"title", "context", "body"} for d in sent["summaries"]), sent
    assert events[-1]["cost_microusd"] == 41000 and events[-1]["escalated"] is False, events[-1]
    line = json.loads((config.BASE_DIR / "assistant.log").read_text().splitlines()[0])
    assert line["managed"] is True and line["cost_microusd"] == 41000, line
    assert assistant._SESSIONS["ACCT-4321"] == "sess_1", assistant._SESSIONS
results.append(run("a signed-in Mac asks through the account service, with no key", t16))


def t17():
    svc = Service(answer(), (409, {"error": "session_ended", "reason": "expired"}),
                  answer(session="sess_2"))
    drive = managed(svc)
    list(assistant.ask("first", "ACCT-4321", service=drive))
    events = list(assistant.ask("second", "ACCT-4321", service=drive))
    assert svc.bodies[1]["session_id"] == "sess_1", svc.bodies[1]
    assert "session_id" not in svc.bodies[2], "a spent session was sent again"
    assert events[-1]["type"] == "done", events
    assert assistant._SESSIONS["ACCT-4321"] == "sess_2", assistant._SESSIONS
results.append(run("follow-ups ride on the session, and a spent one is replaced once", t17))


def t18():
    continuation = [{"type": "text", "text": "Checking."},
                    {"type": "tool_use", "id": "toolu_1", "name": "fetch_transcripts",
                     "input": {"lectures": ["ACCT-4321 2026-09-15: Process Costing"], "reason": "exact words"}}]
    first = (200, [
        {"type": "session", "id": "sess_1", "opened": True},
        {"type": "text", "text": "Checking."},
        {"type": "escalate", "session_id": "sess_1", "reason": "exact words",
         "lectures": ["ACCT-4321 2026-09-15: Process Costing"], "continuation": continuation},
        {"type": "done", "stop_reason": "tool_use", "escalating": True, "cost_microusd": 30000,
         "input_tokens": 900, "output_tokens": 60, "cache_read_tokens": 0, "cache_write_tokens": 5000},
    ])
    svc = Service(first, answer(opened=False, text="The instructor said..."))
    drive = managed(svc)
    events = list(assistant.ask("the exact definition?", "ACCT-4321", service=drive))
    status = [e["text"] for e in events if e["type"] == "status"]
    assert status[-1] == "Going to the full transcripts: exact words", status
    second = svc.bodies[1]
    assert second["session_id"] == "sess_1" and second["continuation"] == continuation, second
    assert second["transcripts"][0]["body"] == "the full words", second["transcripts"]
    assert second["summaries"] == svc.bodies[0]["summaries"], "the cached prefix changed"
    done = events[-1]
    assert done["escalated"] and done["escalated_to"] == ["ACCT-4321 2026-09-15: Process Costing"], done
    assert done["cost_microusd"] == 71000 and done["output_tokens"] == 360, done
    assert assistant.escalation_rate() == {"sessions": 1, "escalated": 1, "rate": 1.0}
results.append(run("an escalation reads the transcripts here and sends them back", t18))


def t19():
    svc = Service((402, {"error": "allowance_exhausted", "kind": "assistant", "unit": "sessions",
                         "used": 0, "allowance": 0}))
    events = list(assistant.ask("q", "ACCT-4321", service=managed(svc)))
    assert events[-1]["type"] == "error" and "Pro plan" in events[-1]["text"], events[-1]
    svc = Service((402, {"error": "allowance_exhausted", "kind": "assistant", "unit": "sessions",
                         "used": 15, "allowance": 15}))
    events = list(assistant.ask("q", "ACCT-4321", service=managed(svc)))
    assert "all 15" in events[-1]["text"], events[-1]
    assert "\u2014" not in events[-1]["text"], "an em dash in panel copy"
    assert assistant.escalation_rate()["sessions"] == 0, "a refused question was logged"
results.append(run("a plan without sessions, or a month without any left, says which", t19))


def t20():
    svc = Service((200, [{"type": "session", "id": "s"}, {"type": "text", "text": "Half"},
                         {"type": "error", "error": "provider_unavailable"}]))
    events = list(assistant.ask("q", "ACCT-4321", service=managed(svc)))
    assert events[-1] == {"type": "error",
                          "text": "The assistant could not be reached. Ask again in a moment."}, events
    assert assistant.escalation_rate()["sessions"] == 0
results.append(run("an answer the service could not finish ends in an error, not a done", t20))


class OwnKeyReached(Exception):
    """Where the BYO path would call the model. It reports it as an error event."""


def own_key(key):
    """Put a key of the Mac's own in config, and a client that records it."""
    import anthropic
    made = []

    class Client:
        def __init__(self, api_key=None, **kw):
            made.append(api_key)

        @property
        def messages(self):
            raise OwnKeyReached()

    saved = (config.ANTHROPIC_API_KEY, anthropic.Anthropic)
    config.ANTHROPIC_API_KEY = key
    anthropic.Anthropic = Client

    def restore():
        config.ANTHROPIC_API_KEY, anthropic.Anthropic = saved
    return made, restore


def through(drive):
    """ask() with the BYO path cut off where it would call the model."""
    return list(assistant.ask("q", "ACCT-4321", service=drive))


def t21():
    no_plan = (402, {"error": "allowance_exhausted", "kind": "assistant", "unit": "sessions",
                     "used": 0, "allowance": 0})
    made, restore = own_key("sk-ant-own")
    try:
        # No sessions on the plan, a key of its own: it answers on that key.
        events = through(managed(Service(no_plan)))
        statuses = [e["text"] for e in events if e["type"] == "status"]
        assert statuses == ["Reading 2 summaries", "Using your own Anthropic key"], statuses
        assert made == ["sk-ant-own"], made
        assert not any("Pro plan" in e.get("text", "") for e in events), events
        # A service without the route yet (404) does the same.
        made.clear()
        through(managed(Service((404, {}))))
        assert made == ["sk-ant-own"], made
        # A Pro month that is spent is not moved onto the person's own bill.
        made.clear()
        events = through(managed(Service((402, {"error": "allowance_exhausted", "kind": "assistant",
                                                "unit": "sessions", "used": 15, "allowance": 15}))))
        assert events[-1]["type"] == "error" and "all 15" in events[-1]["text"], events
        assert made == [], "a spent Pro month fell back to the own key"
    finally:
        restore()
    # And with no key of its own, the plan's refusal stands.
    events = through(managed(Service(no_plan)))
    assert events[-1]["type"] == "error" and "Pro plan" in events[-1]["text"], events
results.append(run("a signed-in Mac with its own key keeps answering when the plan has no sessions", t21))


# --- what stays on this Mac ----------------------------------------------------
#
# Fake data only, in a throwaway INTAKE_HOME. The promise under test: fetched
# Drive text is held in memory for CACHE_TTL_SECONDS and is never written to
# disk; the plaintext folder older versions left is deleted; signing out (or a
# revoked token) drops everything held.

SECRET_SUMMARY = "SECRET-SUMMARY-zebra-4471"
SECRET_WORDS = "SECRET-TRANSCRIPT-walrus-9052"
SECRET_STORE = {"1aBcProcessCosting15xyzQ": SECRET_SUMMARY,
                "1aBcCostVolumeProfit17xyQ": "cvp " + SECRET_SUMMARY,
                "TRANSCRIPT:ACCT-4321_2026-09-15_Process-Costing": SECRET_WORDS}


def files_holding(marker, root):
    """Every file under root whose bytes contain marker, and every path that names it."""
    hits = []
    for path in [root, *root.rglob("*")]:
        if marker.encode() in str(path).encode():
            hits.append(path)
        elif path.is_file() and not path.is_symlink() and marker.encode() in path.read_bytes():
            hits.append(path)
    return hits


def fake_account():
    return _account.Account(token="tok_fake", account_id="acct_1", email="a@example.com", name="A",
                            device_id="dev_1", device_name="Test Mac", profile="syllabus",
                            url="https://accounts.invalid", claimed_at="2026-09-29T00:00:00")


def escalating_session():
    continuation = [{"type": "tool_use", "id": "toolu_1", "name": "fetch_transcripts",
                     "input": {"lectures": ["ACCT-4321 2026-09-15: Process Costing"], "reason": "words"}}]
    first = (200, [
        {"type": "session", "id": "sess_1", "opened": True},
        {"type": "escalate", "session_id": "sess_1", "reason": "words",
         "lectures": ["ACCT-4321 2026-09-15: Process Costing"], "continuation": continuation},
        {"type": "done", "stop_reason": "tool_use", "escalating": True, "cost_microusd": 1,
         "input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0, "cache_write_tokens": 0},
    ])
    svc = Service(first, answer(opened=False, text="Answered."))
    setup()
    assistant._SESSIONS.clear()
    assistant.managed = lambda: True
    assistant.stream_transport = svc
    return svc, FakeService(dict(SECRET_STORE))


def t22():
    svc, drive = escalating_session()
    events = list(assistant.ask("the exact words?", "ACCT-4321", service=drive))
    assert events[-1]["type"] == "done" and events[-1]["escalated"], events
    # The round trip still carried the transcript back to the service.
    assert svc.bodies[1]["transcripts"][0]["body"] == SECRET_WORDS, svc.bodies[1]
    for marker in (SECRET_SUMMARY, SECRET_WORDS):
        found = files_holding(marker, config.ROOT_DIR)
        assert not found, f"lecture text reached disk after a completed session: {found}"
    assert not (config.BASE_DIR / ".assistant").exists(), "the cache folder came back"
    # What is written is counts and titles, and no question text.
    log = (config.BASE_DIR / "assistant.log").read_text()
    assert "the exact words?" not in log and SECRET_WORDS not in log, log
results.append(run("a completed escalated session leaves no lecture text on disk", t22))


def t23():
    setup()
    calls = []
    drive = FakeService(dict(SECRET_STORE), calls=calls)
    lecs = [l for l in assistant.library() if l.course == "ACCT-4321"]
    assistant.stage_one(drive, lecs)
    fetched = len([c for c in calls if c[0] in ("export", "get_media")])
    assistant.stage_one(drive, lecs)
    assert len([c for c in calls if c[0] in ("export", "get_media")]) == fetched, "no reuse inside the TTL"
    # Past the TTL the entry is dropped and Drive is asked again.
    for key, (stored, text) in list(assistant._MEMORY.items()):
        assistant._MEMORY[key] = (stored - assistant.CACHE_TTL_SECONDS - 1, text)
    assert all(assistant._cached(k) is None for k in list(assistant._MEMORY)), "an expired entry was served"
    assistant.stage_one(drive, lecs)
    assert len([c for c in calls if c[0] in ("export", "get_media")]) == 2 * fetched, "expiry did not refetch"
    # And expired entries are pruned on the next write rather than kept forever.
    for key, (stored, text) in list(assistant._MEMORY.items()):
        assistant._MEMORY[key] = (stored - assistant.CACHE_TTL_SECONDS - 1, text)
    assistant._cache("fresh", "x")
    assert list(assistant._MEMORY) == ["fresh"], list(assistant._MEMORY)
results.append(run("fetched text is reused inside the TTL, refetched after it, and pruned", t23))


def t24():
    setup()
    # What 0.5.x left behind: a 0644 file of plaintext in each place it could be.
    from intake import profiles
    old = [config.BASE_DIR / ".assistant", config.ROOT_DIR / ".assistant"]
    old += [config.profile_home(p) / ".assistant" for p in profiles.PROFILES.values()]
    for folder in old:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "1aBcProcessCosting15xyzQ.txt").write_text(SECRET_SUMMARY)
        (folder / "x.transcript.txt").write_text(SECRET_WORDS)
    outside = config.ROOT_DIR / "keep-me.txt"
    outside.write_text("not the assistant's")
    # Asking with no lectures for the course still sweeps first.
    list(assistant.ask("hello", "NOPE-0000", service=FakeService({})))
    left = [f for f in old if f.exists()]
    assert not left, f"old plaintext folders survived: {left}"
    assert outside.read_text() == "not the assistant's", "the sweep deleted something that is not the cache"
results.append(run("the plaintext folder older versions left is deleted the first time it runs", t24))


def t25():
    svc, drive = escalating_session()
    list(assistant.ask("q", "ACCT-4321", service=drive))
    assert assistant._MEMORY and assistant._SESSIONS, "nothing was held, so nothing was tested"
    legacy = config.BASE_DIR / ".assistant"
    legacy.mkdir(exist_ok=True)
    (legacy / "k.txt").write_text(SECRET_SUMMARY)
    account = _account
    account.save(fake_account())
    account.call = lambda *a, **k: (200, {})   # no network
    try:
        account.sign_out()
    finally:
        account.call = account_call
    assert not assistant._MEMORY, "fetched text survived sign-out"
    assert not assistant._SESSIONS, "session ids survived sign-out"
    assert not legacy.exists(), "the plaintext folder survived sign-out"
    assert account.load() is None
results.append(run("signing out clears what the assistant holds", t25))


def t26():
    setup()
    assistant._cache("k", SECRET_SUMMARY)
    assistant._SESSIONS["ACCT-4321"] = "sess_1"
    account = _account
    account.save(fake_account())
    account.forget()      # what a revoked token does
    assert not assistant._MEMORY and not assistant._SESSIONS
results.append(run("a revoked sign-in clears it too", t26))


def t27():
    setup()
    legacy = config.BASE_DIR / ".assistant"
    legacy.mkdir()
    (legacy / "k.txt").write_text(SECRET_SUMMARY)
    assistant.sweep_legacy_once()      # what the panel's load does, before any question
    assert not legacy.exists(), "opening the panel left the old plaintext cache in place"
results.append(run("the old cache goes when the panel loads, without a question being asked", t27))

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
