"""Tests for quality.py and quality_tune.py: telling a hard-to-hear lecture
from a noisy room, and the second pass that follows.

No network and no real audio: the proxy transport is a scripted fake and the
loudness reading is replaced where a test needs levels. From the project root:

    .venv/bin/python test_quality.py
"""
import json
import random
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_home import fresh_home  # noqa: E402

HOME = fresh_home()  # before config is imported

from intake import account, config, providers, quality, quality_tune  # noqa: E402
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
T = quality.Thresholds(unsure_logprob=-0.7, looping_ratio=2.4, no_speech=0.6,
                       quieter_db=8.0, hard_share=0.25, min_speech_seconds=30)


def seg(start, end, lp=-0.3, ns=0.02, cr=1.4, text=" words"):
    return quality.Segment(start, end, lp, ns, cr, text)


def lecture(unsure_every=0, seconds=480, step=6):
    """A chunk of `step`-second segments; every `unsure_every`th one unsure."""
    out = []
    for n, start in enumerate(range(0, seconds, step)):
        lp = -1.1 if unsure_every and n % unsure_every == 0 else -0.3
        out.append(seg(start, start + step, lp))
    return out


def t1():
    v = quality.judge(lecture(), [-20.0] * 80, T)
    assert v.verdict == quality.CLEAR, v
    assert v.share == 0 and not v.second_pass
results.append(run("a clear lecture is left alone", t1))


def t2():
    segs = lecture(unsure_every=2)  # half the speech unsure, all at the lecturer's level
    v = quality.judge(segs, [-20.0] * len(segs), T)
    assert v.verdict == quality.HARD, v
    assert v.second_pass
    assert abs(v.share - 0.5) < 1e-9, v.share
    assert v.room_seconds == 0
results.append(run("a lecturer who comes back unsure half the time is hard to hear", t2))


def t3():
    # The same unsure half, but 14 dB under the lecturer: students near the
    # recorder, not the lecture. No second pass; the room is named instead.
    segs = lecture(unsure_every=2)
    lv = [-34.0 if s.avg_logprob < -0.7 else -20.0 for s in segs]
    v = quality.judge(segs, lv, T)
    assert v.verdict == quality.NOISY_ROOM, v
    assert not v.second_pass
    assert v.lecturer_db == -20.0
    assert set(v.labels) == {"clear", "room"}, set(v.labels)
    # Only a little quieter is still the lecturer.
    lv = [-24.0 if s.avg_logprob < -0.7 else -20.0 for s in segs]
    assert quality.judge(segs, lv, T).verdict == quality.HARD
results.append(run("quieter unsure voices are the room, and never buy a second pass", t3))


def t4():
    # Without loudness there is no telling the two apart, and every unsure
    # second is the lecturer's: the page says "lecturer level unknown".
    segs = lecture(unsure_every=2)
    for lv in (None, [None] * len(segs), [-20.0]):  # the last is the wrong length
        v = quality.judge(segs, lv, T)
        assert v.verdict == quality.HARD and v.lecturer_db is None, (lv, v)
results.append(run("no loudness means every unsure second counts as the lecturer", t4))


def t5():
    # Nearly all unsure: too little sure speech to set the lecturer's level
    # by, so a quiet lecturer is not mistaken for the room.
    segs = lecture(unsure_every=1)
    segs[0] = seg(0, 6, -0.2)
    lv = [-20.0] + [-40.0] * (len(segs) - 1)
    v = quality.judge(segs, lv, T)
    assert v.lecturer_db is None, v.lecturer_db
    assert v.verdict == quality.HARD
results.append(run("a chunk with almost no sure speech has no level to set voices aside by", t5))


def t6():
    assert quality.judge([], None, T).verdict == quality.UNSCORED
    few = [seg(0, 10), seg(10, 20, lp=-1.2)]
    v = quality.judge(few, None, T)
    assert v.verdict == quality.TOO_LITTLE and not v.second_pass, v
    assert v.labels == ["clear", "unsure"]
    # Pauses are not speech, however unsure the model was about them.
    quiet = [seg(n, n + 5, lp=-1.5, ns=0.9) for n in range(0, 480, 5)]
    assert quality.judge(quiet, None, T).verdict == quality.TOO_LITTLE
results.append(run("breaks and empty answers are not judged", t6))


def t7():
    looping = [seg(n, n + 6, lp=-0.3, cr=3.1) for n in range(0, 480, 6)]
    assert quality.judge(looping, None, T).verdict == quality.HARD
results.append(run("a model repeating itself counts as unsure", t7))


def t8():
    raw = [
        {"start": 0, "end": 4, "avg_logprob": -0.3, "no_speech_prob": 0.1, "compression_ratio": 1.2, "text": "a"},
        {"start": 4, "end": 2, "avg_logprob": -0.3, "no_speech_prob": 0.1, "compression_ratio": 1.2},
        {"start": 4, "end": 8, "avg_logprob": "x", "no_speech_prob": 0.1, "compression_ratio": 1.2},
        {"start": 4, "end": 8, "avg_logprob": float("nan"), "no_speech_prob": 0.1, "compression_ratio": 1.2},
        "junk", None,
    ]
    got = quality.segments_from(raw)
    assert len(got) == 1 and got[0].text == "a", got
    assert quality.segments_from(None) == []
results.append(run("only whole segments are read", t8))


def t9():
    text = """frame:0    pts:0       pts_time:0
lavfi.astats.Overall.RMS_level=-inf
frame:1    pts:1600    pts_time:0.1
lavfi.astats.Overall.RMS_level=-22.5
frame:2    pts:3200    pts_time:0.2
lavfi.astats.Overall.RMS_level=-18.0
frame:3    pts:4800    pts_time:0.3
lavfi.astats.Overall.RMS_level=nonsense
"""
    series = quality.parse_windows(text)
    assert series == [(0.0, quality.SILENCE_DB), (0.1, -22.5), (0.2, -18.0)], series
    # Silence is left out of a segment's level; a segment with nothing but
    # silence in it has no level at all.
    lv = quality.levels([seg(0, 0.35), seg(0, 0.05)], series)
    assert lv == [-18.0, None], lv
    assert quality.levels([seg(0, 1)], None) == [None]
results.append(run("loudness is read per window and summed up per segment", t9))


def t10():
    # The page re-scores in JavaScript; it must say what judge() says.
    node = shutil.which("node")
    if not node:
        print("      (node not installed; parity not checked)")
        return
    rng = random.Random(7)
    cases = []
    for _ in range(400):
        segs, at = [], 0.0
        for _ in range(rng.randint(0, 60)):
            length = rng.choice([0.0, 0.5, 2, 4, 7, 11])
            segs.append(seg(at, at + length, lp=round(rng.uniform(-1.6, -0.05), 3),
                            ns=round(rng.uniform(0, 1), 3), cr=round(rng.uniform(1, 3.2), 3)))
            at += length + rng.choice([0, 0, 1])
        lv = rng.choice([None, [rng.choice([None, round(rng.uniform(-45, -12), 1)]) for _ in segs]])
        t = quality.Thresholds(unsure_logprob=rng.choice([-1.0, -0.7, -0.5]), looping_ratio=2.4,
                               no_speech=rng.choice([0.4, 0.6]), quieter_db=rng.choice([4.0, 8.0]),
                               hard_share=rng.choice([0.1, 0.25, 0.4]),
                               min_speech_seconds=rng.choice([0, 30]))
        cases.append((segs, lv, t))
    payload = [{"segments": [s.__dict__ for s in segs], "levels": lv, "t": t.as_dict()}
               for segs, lv, t in cases]
    script = quality_tune.JUDGE_JS + """
const cases = JSON.parse(require("fs").readFileSync(0, "utf8"));
process.stdout.write(JSON.stringify(cases.map((c) => judge(c.segments, c.levels, c.t))));
"""
    out = subprocess.run([node, "-e", script], input=json.dumps(payload),
                         capture_output=True, text=True, check=True).stdout
    for n, ((segs, lv, t), js) in enumerate(zip(cases, json.loads(out))):
        py = quality.judge(segs, lv, t)
        assert js["verdict"] == py.verdict, (n, js["verdict"], py.verdict)
        assert js["labels"] == py.labels, (n, js["labels"], py.labels)
        assert abs(js["share"] - py.share) < 1e-9, (n, js["share"], py.share)
        assert js["lecturer"] == py.lecturer_db, (n, js["lecturer"], py.lecturer_db)
results.append(run("the page's scoring agrees with judge() on 400 random chunks", t10))


# --- The proxy provider and the second pass ------------------------------

class FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def signed_in():
    account.save(account.Account(
        token="syd_abc", account_id="a1", email="me@example.com", name="Me", device_id="d1",
        device_name="This Mac", profile="syllabus", url=SERVICE, claimed_at="2026-09-15T00:00:00Z"))


def raw(segs):
    return [dict(s.__dict__) for s in segs]


def t11():
    signed_in()
    clip = config.WORK_DIR / "chunk.m4a"
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"x" * 2048)
    sent = []

    def fake_post(url, token, path, seconds, timeout, **kw):
        sent.append(kw)
        return FakeResponse(200, {"text": " hi ", "segments": raw(lecture()), "charged_seconds": 1440,
                                  "provider": "openai-high", "quality": kw.get("quality", "standard")})

    real_post, real_duration = providers._post_audio, transcribe_module.duration_seconds
    providers._post_audio = fake_post
    transcribe_module.duration_seconds = lambda p: 480.0
    try:
        prov = providers.ProxyProvider()
        plain = prov.transcribe_scored(clip)
        high = prov.transcribe_scored(clip, quality="high")
        assert prov.transcribe_file(clip) == "hi"
    finally:
        providers._post_audio, transcribe_module.duration_seconds = real_post, real_duration
    # A standard request goes out with exactly the old arguments.
    assert sent == [{}, {"quality": "high"}, {}], sent
    assert plain.text == "hi" and len(plain.segments) == 80
    assert high.quality == "high" and high.charged_seconds == 1440
results.append(run("the provider asks for a second pass only when told, and reads the scores back", t11))


def lecture_run(second_pass, high_answer, checkpoint=None):
    """Two 8 minute chunks through transcribe(): the first clear, the second
    hard to hear. Returns (text, what was posted)."""
    signed_in()
    work = config.WORK_DIR / "quality-run"
    work.mkdir(parents=True, exist_ok=True)
    src = work / "lecture.m4a"
    # Written once: a rewritten recording is a different one to the
    # checkpoint, which is right, and would make a retry look like a fresh run.
    if not src.exists():
        src.write_bytes(b"x" * 1000)
    posted = []

    def fake_split(path, work_dir, seconds):
        out = [Path(work_dir) / f"chunk_00{n}.m4a" for n in (1, 2)]
        for p in out:
            p.write_bytes(b"x" * 1000)
        return out

    def fake_post(url, token, path, seconds, timeout, **kw):
        name, grade = Path(path).stem, kw.get("quality", "standard")
        posted.append((name, grade))
        if grade == "high":
            return high_answer()
        segs = lecture() if name == "chunk_001" else lecture(unsure_every=2)
        return FakeResponse(200, {"text": f"first pass of {name}", "segments": raw(segs)})

    saved = {name: getattr(transcribe_module, name) for name in ("split", "duration_seconds", "log")}
    real = (providers._post_audio, quality.windows, config.QUALITY_SECOND_PASS)
    transcribe_module.split = fake_split
    transcribe_module.duration_seconds = lambda p: 2.0 * config.CHUNK_SECONDS if Path(p) == src else 480.0
    transcribe_module.log = lambda msg: None
    providers._post_audio = fake_post
    quality.windows = lambda path: None
    config.QUALITY_SECOND_PASS = second_pass
    try:
        text = transcribe_module.transcribe(src, provider=providers.ProxyProvider(), checkpoint=checkpoint)
    finally:
        providers._post_audio, quality.windows, config.QUALITY_SECOND_PASS = real
        for name, value in saved.items():
            setattr(transcribe_module, name, value)
    return text, posted


def high_ok():
    return FakeResponse(200, {"text": "second pass of chunk_002", "charged_seconds": 1440,
                              "provider": "openai-high", "quality": "high"})


def t12():
    text, posted = lecture_run(True, high_ok)
    assert posted == [("chunk_001", "standard"), ("chunk_002", "standard"), ("chunk_002", "high")], posted
    assert text == "first pass of chunk_001\n\nsecond pass of chunk_002", text
results.append(run("only the hard-to-hear chunk gets a second pass, and its text is kept", t12))


def t13():
    text, posted = lecture_run(False, high_ok)
    assert ("chunk_002", "high") not in posted, posted
    assert text == "first pass of chunk_001\n\nfirst pass of chunk_002"
results.append(run("with QUALITY_SECOND_PASS off, chunks are scored and nothing more is spent", t13))


def t14():
    for answer in (
        lambda: FakeResponse(402, {"error": "allowance_exhausted", "kind": "transcribe", "used": 1, "allowance": 2}),
        lambda: FakeResponse(502, {"error": "provider_unavailable"}),
        lambda: FakeResponse(200, {"text": "   "}),
        lambda: FakeResponse(200, {"text": " ".join(["w"] * config.TRUNCATION_WORD_THRESHOLD)}),
        # "first pass of chunk_002" is four words; one is under half of it.
        lambda: FakeResponse(200, {"text": "dropped"}),
    ):
        text, posted = lecture_run(True, answer)
        assert posted[-1] == ("chunk_002", "high"), posted
        assert text == "first pass of chunk_001\n\nfirst pass of chunk_002", text
results.append(run("a refused, failed, empty or cut-off second pass keeps the first transcript", t14))


def t15():
    # The second pass fails once; the retry pays for it and nothing else.
    checkpoint = config.WORK_DIR / "quality-checkpoint"
    shutil.rmtree(checkpoint, ignore_errors=True)
    _, posted = lecture_run(True, lambda: FakeResponse(502, {"error": "provider_unavailable"}), checkpoint)
    assert len(posted) == 3
    text, posted = lecture_run(True, high_ok, checkpoint)
    assert posted == [("chunk_002", "high")], posted
    assert text.endswith("second pass of chunk_002")
    # And once it has run, a third attempt spends nothing at all.
    text, posted = lecture_run(True, high_ok, checkpoint)
    assert posted == [], posted
    assert text.endswith("second pass of chunk_002")
results.append(run("a retry owes only the second pass that never came back", t15))


# --- The tuning run ------------------------------------------------------

class FakeProvider:
    name = "syllabus"
    max_chunk_seconds = config.CHUNK_SECONDS
    max_bytes = 12 * 1024 * 1024
    compress_threshold_bytes = 10 * 1024 * 1024
    truncation_word_threshold = config.TRUNCATION_WORD_THRESHOLD

    def __init__(self):
        self.calls = []

    def transcribe_scored(self, path, quality="standard"):
        self.calls.append((Path(path).name, quality))
        hard = Path(path).name == "chunk_002.m4a"
        segs = lecture(unsure_every=2) if hard else lecture()
        return providers.Scored(text="high </script> words" if quality == "high" else "first words",
                                segments=None if quality == "high" else raw(segs),
                                charged_seconds=480, provider="groq", quality=quality)


def tune(prov, out, compare="flagged", yes=True, answer="n"):
    src = config.WORK_DIR / "tune" / "hard class.m4a"
    src.parent.mkdir(parents=True, exist_ok=True)
    if not src.exists():
        src.write_bytes(b"x" * 1000)

    def fake_split(path, work_dir, seconds):
        out_ = [Path(work_dir) / f"piece_{n}.m4a" for n in (1, 2, 3)]
        for p in out_:
            p.write_bytes(b"y" * 100)
        return out_

    saved = {n: getattr(transcribe_module, n) for n in ("split", "duration_seconds", "log")}
    real_windows, real_tty = quality.windows, sys.stdin.isatty
    transcribe_module.split = fake_split
    transcribe_module.duration_seconds = lambda p: 1440.0 if Path(p) == src else 480.0
    transcribe_module.log = lambda msg: None
    quality.windows = lambda path: None
    sys.stdin.isatty = lambda: True
    try:
        return quality_tune.run(src, out, compare, yes, T, provider=prov, ask=lambda q: answer)
    finally:
        quality.windows, sys.stdin.isatty = real_windows, real_tty
        for n, v in saved.items():
            setattr(transcribe_module, n, v)


def t16():
    out = config.WORK_DIR / "tune" / "out"
    shutil.rmtree(out, ignore_errors=True)
    prov = FakeProvider()
    data = tune(prov, out)
    assert prov.calls == [("chunk_001.m4a", "standard"), ("chunk_002.m4a", "standard"),
                          ("chunk_003.m4a", "standard"), ("chunk_002.m4a", "high")], prov.calls
    assert [c["high"] is not None for c in data["chunks"]] == [False, True, False]
    assert (out / "report.html").is_file() and (out / "chunks" / "chunk_002.m4a").is_file()
    # A second run, with other thresholds or none, spends nothing.
    prov2 = FakeProvider()
    tune(prov2, out, compare="all", yes=True)
    assert prov2.calls == [("chunk_001.m4a", "high"), ("chunk_003.m4a", "high")], prov2.calls
    prov3 = FakeProvider()
    tune(prov3, out, compare="all")
    assert prov3.calls == [], prov3.calls
results.append(run("a tuning run pays for each chunk once, and second passes only for the flagged", t16))


def t17():
    out = config.WORK_DIR / "tune" / "asked"
    shutil.rmtree(out, ignore_errors=True)
    prov = FakeProvider()
    tune(prov, out, yes=False, answer="n")
    assert all(q == "standard" for _, q in prov.calls), prov.calls
    prov = FakeProvider()
    tune(prov, out, yes=False, answer="y")
    assert prov.calls == [("chunk_002.m4a", "high")], prov.calls
results.append(run("a second pass is priced and asked about before it is sent", t17))


def t18():
    page = (config.WORK_DIR / "tune" / "out" / "report.html").read_text()
    assert "high </script> words" not in page, "a transcript could end the page's script"
    assert "high <\\/script> words" in page
    assert "—" not in page, "no em dashes in copy"
    assert "function judge(" in page
results.append(run("the page carries its data safely and the same rules", t18))


failed = results.count(False)
print(f"\n{len(results) - failed}/{len(results)} passed")
sys.exit(1 if failed else 0)
