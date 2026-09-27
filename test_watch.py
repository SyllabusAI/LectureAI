"""Tests for watch.process(), the orchestrator that turns one recording into
filed notes.

Nothing here reaches a network, a provider, or Google Drive: transcription,
summarization and the upload are replaced with fakes that record what they
were asked to do. The files are real, inside a throwaway $INTAKE_HOME. From
the project root:

    .venv/bin/python test_watch.py

process() had no coverage at all before this file, which is how a handful of
defects in it went unnoticed by a green suite. It is the one place a lecture
can be lost, paid for twice, or filed under the wrong class, so the fakes
below count calls as well as returning values: several of the things worth
asserting here are about how often an expensive step runs, not about what it
returns.
"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_home import fresh_home  # noqa: E402

HOME = fresh_home()  # before config is imported, so nothing touches ~/.intake

from intake import config  # noqa: E402
from intake import notion_tasks, record, summarize, transcribe, watch  # noqa: E402
from intake import upload as drive  # noqa: E402

# Tuesday. The sample schedule puts ACCT-4321 at 14:00 and ENTR-3306 at 12:00,
# and no class meets on a Sunday, which is how the "no class matches" cases
# below get a timestamp that cannot accidentally hit one.
TUESDAY_2PM_END = "2026-09-15 15:00"   # a 60 minute class ending at 15:00
SUNDAY = "2026-09-13 15:00"


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


def when(text: str) -> float:
    """'2026-09-15 15:00' as an mtime."""
    from datetime import datetime
    return datetime.strptime(text, "%Y-%m-%d %H:%M").timestamp()


class Fakes:
    """Stands in for everything process() would otherwise spend money on.

    Installed by replacing attributes on the real modules rather than on
    `watch`, because process() reaches them through the module object at call
    time. restore() puts the originals back so one test cannot leak into the
    next.
    """

    def __init__(self, *, transcript="hello there lecture", duration=3600.0,
                 topic="Theories-Of-Leadership", actions=None, terms=None,
                 upload_error=None, notion=False):
        self.transcript = transcript
        self.duration = duration
        self.topic = topic
        self.actions = actions if actions is not None else []
        self.terms = terms if terms is not None else []
        self.upload_error = upload_error
        self.notion = notion
        self.transcribed = 0
        self.summarized = 0
        self.uploads = []       # one dict per drive.upload call
        self._saved = {}

    def _remember(self, module, name, value):
        self._saved.setdefault((module, name), getattr(module, name))
        setattr(module, name, value)

    def install(self):
        def fake_duration(path):
            return self.duration

        def fake_transcribe(path, on_progress=None, checkpoint=None):
            self.transcribed += 1
            self.checkpoint = checkpoint
            if on_progress:
                on_progress("working")
            return self.transcript

        def fake_summarize(text, course, date):
            self.summarized += 1
            return {
                "summary_md": f"# {course} {date}\n\nnotes",
                "topic_slug": self.topic,
                "key_terms": list(self.terms),
                "action_items": list(self.actions),
            }

        def fake_render(result, course, date):
            return result["summary_md"]

        def fake_upload(local_path, course, interactive=True, *, subfolder="",
                        as_google_doc=False, name=None, recording_key=None,
                        time_suffix=""):
            call = {
                "path": Path(local_path), "course": course,
                "subfolder": subfolder, "as_google_doc": as_google_doc,
                "name": name, "recording_key": recording_key,
                "time_suffix": time_suffix,
                "existed": Path(local_path).is_file(),
                "body": Path(local_path).read_text() if Path(local_path).is_file() else None,
            }
            self.uploads.append(call)
            if self.upload_error and len(self.uploads) == self.upload_error[0]:
                raise RuntimeError(self.upload_error[1])
            final = name or Path(local_path).name
            return drive.Upload(url=f"https://drive.test/{final}", name=final)

        self._remember(transcribe, "duration_seconds", fake_duration)
        self._remember(transcribe, "transcribe", fake_transcribe)
        self._remember(summarize, "summarize", fake_summarize)
        self._remember(summarize, "render_markdown", fake_render)
        self._remember(drive, "upload", fake_upload)
        self._remember(notion_tasks, "enabled", lambda: self.notion)
        return self

    def restore(self):
        for (module, name), value in self._saved.items():
            setattr(module, name, value)
        self._saved.clear()

    def __enter__(self):
        return self.install()

    def __exit__(self, *exc):
        self.restore()
        return False


def recording(name="ACCT-4321_2026-09-15_1400.m4a", *, at=TUESDAY_2PM_END,
              body="AUDIO", where=None):
    """Put a fake recording in the inbox with a chosen mtime."""
    import os
    directory = where if where is not None else config.INBOX_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(body)
    stamp = when(at)
    os.utime(path, (stamp, stamp))
    return path


def log_lines():
    if not config.LOG_FILE.is_file():
        return []
    return [line.split("\t") for line in
            config.LOG_FILE.read_text().splitlines() if line.strip()]


def clear():
    """Empty the directories process() writes to, between tests.

    The checkpoints go too. They are keyed by the recording, and these tests
    reuse a handful of timestamps, so a slot one test left behind would let
    the next one skip the transcription it is trying to count.
    """
    import shutil
    for directory in (config.INBOX_DIR, config.PROCESSED_DIR):
        if directory.is_dir():
            for child in directory.iterdir():
                if child.is_file():
                    child.unlink()
    shutil.rmtree(config.WORK_DIR / "resume", ignore_errors=True)
    config.LOG_FILE.unlink(missing_ok=True)


results = []


# 1. The ordinary case, end to end: one class recording becomes a summary and
#    a transcript in Drive, a line in pipeline.log, and no leftovers on disk.
def t1():
    clear()
    audio = recording()
    with Fakes() as fake:
        out = watch.process(audio, interactive=False)
    assert out["course"] == "ACCT-4321", out
    assert out["date"] == "2026-09-15", out
    assert fake.transcribed == 1, fake.transcribed
    assert fake.summarized == 1, fake.summarized
    assert len(fake.uploads) == 2, fake.uploads
    assert out["summary_url"].startswith("https://drive.test/"), out
results.append(run("a scheduled recording is transcribed, summarized and filed", t1))


# 2. Both staged copies are written before the upload and removed after it, so
#    a finished lecture leaves processed/ clean.
def t2():
    clear()
    audio = recording()
    with Fakes() as fake:
        watch.process(audio, interactive=False)
    assert all(call["existed"] for call in fake.uploads), fake.uploads
    leftovers = sorted(p.name for p in config.PROCESSED_DIR.glob("*"))
    assert leftovers == [], leftovers
results.append(run("staged copies exist for the upload and are cleared after", t2))


# 3. The summary goes to the course folder, the transcript one level down, and
#    the transcript takes its name from whatever the summary ended up called:
#    a renamed pair has to stay a pair.
def t3():
    clear()
    audio = recording()
    with Fakes() as fake:
        watch.process(audio, interactive=False)
    summary, transcript = fake.uploads
    assert summary["subfolder"] == "", summary
    assert transcript["subfolder"] == config.TRANSCRIPT_SUBFOLDER, transcript
    assert transcript["name"] == f"{summary['name']}.txt", (summary, transcript)
results.append(run("transcript is filed under the summary's final name", t3))


# 4. Both uploads carry the recording's identity, which is what lets a re-run
#    replace its own past output instead of a different lecture's.
def t4():
    clear()
    audio = recording()
    with Fakes() as fake:
        watch.process(audio, interactive=False)
    key = config.recording_key(when(TUESDAY_2PM_END), 3600.0)
    suffix = config.recording_time_suffix(when(TUESDAY_2PM_END), 3600.0)
    for call in fake.uploads:
        assert call["recording_key"] == key, (call, key)
        assert call["time_suffix"] == suffix, (call, suffix)
results.append(run("both uploads carry the recording key and time suffix", t4))


# 5. The seventh field the panel's dashboard reads.
def t5():
    clear()
    audio = recording()
    with Fakes(transcript="one two three", actions=["a", "b"], terms=["x"]):
        watch.process(audio, interactive=False)
    line = log_lines()[-1]
    measures = json.loads(line[6])
    assert measures["seconds"] == 3600, measures
    assert measures["words"] == 3, measures
    assert measures["actions"] == 2, measures
    assert measures["terms"] == 1, measures
results.append(run("the log line carries seconds, words, actions and terms", t5))


# 6. A recording whose timestamp matches no class falls back to the course in
#    its filename, which is what saves a file copied off a phone.
def t6():
    clear()
    audio = recording("ENTR-3306_2026-09-13_1400.m4a", at=SUNDAY)
    with Fakes():
        out = watch.process(audio, interactive=False)
    assert out["course"] == "ENTR-3306", out
results.append(run("no class at that hour falls back to the filename", t6))


# 7. Neither signal identifies a course, so it files under UNKNOWN rather than
#    guessing or refusing.
def t7():
    clear()
    audio = recording("voice-memo-4.m4a", at=SUNDAY)
    with Fakes():
        out = watch.process(audio, interactive=False)
    assert out["course"] == config.UNKNOWN_COURSE, out
results.append(run("an unidentifiable recording files under UNKNOWN", t7))


# 8. A Drive failure must not cost the recording: the original stays where it
#    was so the file can be tried again.
def t8():
    clear()
    audio = recording()
    with Fakes(upload_error=(1, "drive is down")) as fake:
        try:
            watch.process(audio, interactive=False)
        except RuntimeError as exc:
            assert "drive is down" in str(exc), exc
        else:
            raise AssertionError("the upload failure was swallowed")
    assert audio.is_file(), "the original recording was lost on an upload failure"
    assert fake.transcribed == 1, fake.transcribed
results.append(run("an upload failure leaves the original recording in place", t8))


# 9. The expensive output survives that same failure, in the recording's own
#    checkpoint rather than in processed/ under a stem another lecture of the
#    same class on the same day would also claim.
def t9():
    clear()
    audio = recording()
    with Fakes(upload_error=(1, "drive is down")):
        try:
            watch.process(audio, interactive=False)
        except RuntimeError:
            pass
    slot = watch.Resume(config.recording_key(when(TUESDAY_2PM_END), 3600.0)).dir
    kept = sorted(p.name for p in slot.glob("*"))
    assert kept == ["summary.json", "summary.md", "transcript.txt"], kept
    assert slot.joinpath("transcript.txt").read_text() == "hello there lecture"
    # And nothing is left lying in processed/ under a shared name.
    assert sorted(p.name for p in config.PROCESSED_DIR.glob("*")) == []
results.append(run("a failed upload keeps the paid work in the recording's checkpoint", t9))


# 10. With the shipped default the original is deleted once both uploads land.
def t10():
    clear()
    audio = recording()
    with Fakes():
        out = watch.process(audio, interactive=False)
    assert not audio.exists(), "the original should be gone after a clean run"
    assert out["original"] == "(deleted)", out
results.append(run("the original is deleted after a clean run", t10))


# 11. With the archive setting instead, the original lands in processed/.
def t11():
    clear()
    audio = recording()
    keep = config.DELETE_ORIGINAL_AFTER_UPLOAD
    config.DELETE_ORIGINAL_AFTER_UPLOAD = False
    try:
        with Fakes():
            out = watch.process(audio, interactive=False)
    finally:
        config.DELETE_ORIGINAL_AFTER_UPLOAD = keep
    archived = config.PROCESSED_DIR / audio.name
    assert archived.is_file(), sorted(p.name for p in config.PROCESSED_DIR.glob("*"))
    assert out["original"] == str(archived), out
results.append(run("archiving keeps the original in processed/", t11))


# 12. Notion is off, so nothing is pushed, but the action items are still
#     counted: the panel has to be able to show what was found.
def t12():
    clear()
    audio = recording()
    with Fakes(actions=["read chapter 4"], notion=False):
        watch.process(audio, interactive=False)
    line = log_lines()[-1]
    assert line[5] == "", f"a Notion warning appeared with Notion off: {line[5]!r}"
    assert json.loads(line[6])["actions"] == 1, line
results.append(run("action items are counted with Notion switched off", t12))


# 13. A path that is not a file is refused before anything is spent on it.
def t13():
    clear()
    missing = config.INBOX_DIR / "not-here.m4a"
    with Fakes() as fake:
        try:
            watch.process(missing, interactive=False)
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("a missing file was accepted")
    assert fake.transcribed == 0, fake.transcribed
results.append(run("a missing file is refused before transcription", t13))


# 14. The status file is what the panel polls while a lecture is in flight;
#     it has to name the stage and the course, not just say "busy".
def t14():
    clear()
    audio = recording()
    seen = []
    with Fakes() as fake:
        original = watch.write_status

        def spy(stage, file="", course="", detail="", started=""):
            seen.append((stage, course))
            return original(stage, file, course, detail, started)

        watch.write_status = spy
        try:
            watch.process(audio, interactive=False)
        finally:
            watch.write_status = original
    stages = [stage for stage, _ in seen]
    assert "transcribing" in stages, stages
    assert "summarizing" in stages, stages
    assert "uploading" in stages, stages
    assert all(course == "ACCT-4321" for _, course in seen), seen
results.append(run("progress is reported for each stage with the course", t14))


# 15. The finding itself: a Drive failure must not make the next attempt pay
#     for transcription and summarization all over again.
def t15():
    clear()
    audio = recording()
    first = Fakes(upload_error=(1, "drive is down"))
    with first:
        try:
            watch.process(audio, interactive=False)
        except RuntimeError:
            pass
    assert first.transcribed == 1 and first.summarized == 1, first.__dict__
    second = Fakes()
    with second:
        out = watch.process(audio, interactive=False)
    assert second.transcribed == 0, "the retry paid for transcription again"
    assert second.summarized == 0, "the retry paid for summarization again"
    assert out["summary_url"].startswith("https://drive.test/"), out
results.append(run("a retry after a failed upload pays for neither stage again", t15))


# 16. The bigger half, which the report did not reach: staging happened after
#     BOTH stages, so a summary that failed threw away a transcript that had
#     just been paid for.
def t16():
    clear()
    audio = recording()

    class Boom(Fakes):
        def install(self):
            out = super().install()
            broken = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("the model refused"))
            self._remember(summarize, "summarize", broken)
            return out

    first = Boom()
    with first:
        try:
            watch.process(audio, interactive=False)
        except RuntimeError as exc:
            assert "refused" in str(exc), exc
        else:
            raise AssertionError("the summary failure was swallowed")
    assert first.transcribed == 1, first.transcribed
    second = Fakes()
    with second:
        watch.process(audio, interactive=False)
    assert second.transcribed == 0, "the transcript was thrown away with the failed summary"
    assert second.summarized == 1, "the summary still had to be produced"
results.append(run("a failed summary keeps the transcript it was given", t16))


# 17. Two recordings of one class on one day build the same stem. Staged under
#     it, the second replaced the first's copy while both were still waiting
#     on Drive.
def t17():
    clear()
    first = recording("ACCT-4321_2026-09-15_1400.m4a", at=TUESDAY_2PM_END, body="FIRST")
    with Fakes(transcript="the first lecture", upload_error=(1, "drive is down")):
        try:
            watch.process(first, interactive=False)
        except RuntimeError:
            pass
    second = recording("ACCT-4321_2026-09-15_1600.m4a", at="2026-09-15 17:00", body="SECOND")
    with Fakes(transcript="the second lecture", upload_error=(1, "drive is down")):
        try:
            watch.process(second, interactive=False)
        except RuntimeError:
            pass
    one = watch.Resume(config.recording_key(when(TUESDAY_2PM_END), 3600.0)).dir
    two = watch.Resume(config.recording_key(when("2026-09-15 17:00"), 3600.0)).dir
    assert one != two, one
    assert one.joinpath("transcript.txt").read_text() == "the first lecture"
    assert two.joinpath("transcript.txt").read_text() == "the second lecture"
results.append(run("two lectures sharing a stem keep their own paid work", t17))


# 18. With the archive setting, an original must not land on top of an older
#     one. A phone hands back the same filename every time.
def t18():
    clear()
    keep = config.DELETE_ORIGINAL_AFTER_UPLOAD
    config.DELETE_ORIGINAL_AFTER_UPLOAD = False
    try:
        config.PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
        older = config.PROCESSED_DIR / "lecture.m4a"
        older.write_text("OLD RECORDING")
        audio = recording("lecture.m4a", body="NEW RECORDING")
        with Fakes():
            out = watch.process(audio, interactive=False)
        assert older.read_text() == "OLD RECORDING", "the older original was overwritten"
        assert Path(out["original"]).read_text() == "NEW RECORDING", out
        assert Path(out["original"]) != older, out
    finally:
        config.DELETE_ORIGINAL_AFTER_UPLOAD = keep
results.append(run("archiving never lands on top of an older recording", t18))


# 19. Work nobody came back for is not kept forever.
def t19():
    clear()
    root = config.WORK_DIR / "resume"
    fresh = root / "2026-09-15T14-00"
    stale = root / "2020-01-01T09-00"
    for slot in (fresh, stale):
        slot.mkdir(parents=True, exist_ok=True)
        (slot / "transcript.txt").write_text("words")
    import os
    old = time.time() - 30 * 86400
    os.utime(stale, (old, old))
    assert watch.sweep_resume() == 1
    assert fresh.is_dir() and not stale.exists()
results.append(run("checkpoints nobody came back to are swept", t19))


# 20. The course picker promised a destination that processing overruled. A
#     lecture recorded under one course during another's scheduled hour was
#     filed, named, foldered and pushed to Notion under the scheduled one.
def t20():
    clear()
    # ACCT-4321 owns Tuesday 14:00 in the sample schedule. Record under
    # ENTR-3306 anyway, the way the picker lets you.
    staging = config.WORK_DIR / "staged.m4a"
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.write_text("AUDIO")
    from datetime import datetime as dt
    audio = record._file_into_inbox(staging, dt(2026, 9, 15, 14, 0), "ENTR-3306")
    import os
    stamp = when(TUESDAY_2PM_END)
    os.utime(audio, (stamp, stamp))
    with Fakes() as fake:
        out = watch.process(audio, interactive=False)
    assert out["course"] == "ENTR-3306", f"the schedule overruled the pick: {out}"
    assert out["stem"].startswith("ENTR-3306"), out["stem"]
    assert all(call["course"] == "ENTR-3306" for call in fake.uploads), fake.uploads
    assert log_lines()[-1][1] == "ENTR-3306", log_lines()[-1]
results.append(run("a course chosen at record time survives processing", t20))


# 21. The other two roads have to keep working: an inferred course leaves no
#     note behind, so the schedule still decides, and a file copied in with
#     no note still falls back to its own name.
def t21():
    clear()
    staging = config.WORK_DIR / "staged2.m4a"
    staging.write_text("AUDIO")
    from datetime import datetime as dt
    import os
    inferred = record._file_into_inbox(staging, dt(2026, 9, 15, 14, 0), None)
    stamp = when(TUESDAY_2PM_END)
    os.utime(inferred, (stamp, stamp))
    assert not config.course_note(inferred).exists(), "an inferred course left a note"
    with Fakes():
        out = watch.process(inferred, interactive=False)
    assert out["course"] == "ACCT-4321", out

    clear()
    copied = recording("ENTR-3306_2026-09-13_1400.m4a", at=SUNDAY)
    with Fakes():
        out = watch.process(copied, interactive=False)
    assert out["course"] == "ENTR-3306", out
results.append(run("an inferred course still defers to the schedule", t21))


# 22. The note is about one recording and must not outlive it.
def t22():
    clear()
    staging = config.WORK_DIR / "staged3.m4a"
    staging.write_text("AUDIO")
    from datetime import datetime as dt
    import os
    audio = record._file_into_inbox(staging, dt(2026, 9, 15, 14, 0), "ENTR-3306")
    stamp = when(TUESDAY_2PM_END)
    os.utime(audio, (stamp, stamp))
    assert config.course_note(audio).is_file()
    with Fakes():
        watch.process(audio, interactive=False)
    assert not config.course_note(audio).exists(), "the note outlived the recording"
    # And it is not audio, so the watcher must never try to process one.
    assert config.course_note(audio).suffix not in config.AUDIO_EXTENSIONS
results.append(run("the note is cleared once the recording is filed", t22))


# 23 to 27. A transcription that fails part way through must not bill the
#    chunks that already succeeded a second time. Resume used to keep the
#    transcript only once every chunk was back, so a failure on the last chunk
#    re-sent all of them (one 73 minute lecture was billed three times). These
#    run the real transcribe() with ffmpeg and the network removed, and count
#    what reaches the provider.
class ChunkProvider:
    """A provider that records every chunk it is sent, and can fail on one."""

    def __init__(self, fail_on=None, name="stub/chunked", chunk_seconds=600):
        self.name = name
        self.max_bytes = 25 * 1024 * 1024
        self.compress_threshold_bytes = 24 * 1024 * 1024
        self.max_chunk_seconds = chunk_seconds
        self.truncation_word_threshold = None
        self.fail_on = fail_on      # 1-based chunk number to fail on, or None
        self.sent = []              # chunk numbers, in the order sent

    def transcribe_file(self, path):
        number = int(Path(path).stem.rsplit("_", 1)[1])
        self.sent.append(number)
        if number == self.fail_on:
            raise RuntimeError(f"upload failed on chunk {number}")
        return f"words of part {number}"


class ChunkedAudio:
    """Replaces transcribe's ffmpeg calls: `seconds` of audio, split for real
    arithmetic but without touching any audio."""

    def __init__(self, seconds=3000.0):
        self.seconds = seconds
        self._saved = {}

    def __enter__(self):
        import math

        def fake_split(src, work_dir, seconds):
            count = max(1, math.ceil(self.seconds / seconds))
            return [work_dir / f"chunk_{i:03d}.m4a" for i in range(1, count + 1)]

        for name, value in (
            ("_size", lambda path: 1000),
            ("duration_seconds", lambda path: self.seconds),
            ("split", fake_split),
            ("compress", lambda src, work_dir: src),
            ("log", lambda msg: None),
        ):
            self._saved[name] = getattr(transcribe, name)
            setattr(transcribe, name, value)
        return self

    def __exit__(self, *exc):
        for name, value in self._saved.items():
            setattr(transcribe, name, value)
        return False


def t23():
    clear()
    audio = recording(body="CHUNKED AUDIO")
    checkpoint = config.WORK_DIR / "chunk-test"
    import shutil
    shutil.rmtree(checkpoint, ignore_errors=True)
    with ChunkedAudio(seconds=3000.0):   # five 10 minute chunks
        failing = ChunkProvider(fail_on=4)
        try:
            transcribe.transcribe(audio, provider=failing, checkpoint=checkpoint)
        except RuntimeError as exc:
            assert "chunk 4" in str(exc), exc
        else:
            raise AssertionError("the failure on chunk 4 was swallowed")
        assert failing.sent == [1, 2, 3, 4], failing.sent

        retry = ChunkProvider()
        text = transcribe.transcribe(audio, provider=retry, checkpoint=checkpoint)
    assert retry.sent == [4, 5], f"the retry re-sent paid chunks: {retry.sent}"
    expected = "\n\n".join(f"words of part {n}" for n in range(1, 6))
    assert text == expected, text
    shutil.rmtree(checkpoint, ignore_errors=True)
results.append(run("a retry after a failure on chunk k sends only chunks k..n", t23))


def t24():
    clear()
    audio = recording(body="CHUNKED AUDIO")
    checkpoint = config.WORK_DIR / "chunk-test"
    import os
    import shutil
    shutil.rmtree(checkpoint, ignore_errors=True)
    with ChunkedAudio(seconds=3000.0):
        try:
            transcribe.transcribe(audio, provider=ChunkProvider(fail_on=3),
                                  checkpoint=checkpoint)
        except RuntimeError:
            pass
        # A different chunk length cuts the audio differently: part 1 of a
        # 25 minute split is not part 1 of a 10 minute one.
        wider = ChunkProvider(chunk_seconds=1500)
        transcribe.transcribe(audio, provider=wider, checkpoint=checkpoint)
        assert wider.sent == [1, 2], f"stale chunks were reused: {wider.sent}"

        try:
            transcribe.transcribe(audio, provider=ChunkProvider(fail_on=3),
                                  checkpoint=checkpoint)
        except RuntimeError:
            pass
        # A different provider is a different transcript.
        other = ChunkProvider(name="stub/other")
        transcribe.transcribe(audio, provider=other, checkpoint=checkpoint)
        assert other.sent == [1, 2, 3, 4, 5], other.sent

        try:
            transcribe.transcribe(audio, provider=ChunkProvider(fail_on=3),
                                  checkpoint=checkpoint)
        except RuntimeError:
            pass
        # The file changed underneath: a new recording in the same place.
        audio.write_text("A DIFFERENT RECORDING")
        stamp = when(TUESDAY_2PM_END) + 60
        os.utime(audio, (stamp, stamp))
        changed = ChunkProvider()
        transcribe.transcribe(audio, provider=changed, checkpoint=checkpoint)
        assert changed.sent == [1, 2, 3, 4, 5], changed.sent
    # Only the current key's results are kept; the stale ones were dropped.
    assert len([p for p in checkpoint.iterdir()]) == 1, list(checkpoint.iterdir())
    shutil.rmtree(checkpoint, ignore_errors=True)
results.append(run("chunks from another file, provider or split are never reused", t24))


def t25():
    clear()
    audio = recording(body="CHUNKED AUDIO")
    with Fakes() as fake:
        # The real transcribe, over fake audio: only the expensive leg is fake.
        transcribe.transcribe = fake._saved[(transcribe, "transcribe")]
        with ChunkedAudio(seconds=3600.0):   # six chunks
            failing = ChunkProvider(fail_on=5)
            fake._remember(watch.providers, "get", lambda name=None: failing)
            try:
                watch.process(audio, interactive=False)
            except RuntimeError as exc:
                assert "chunk 5" in str(exc), exc
            else:
                raise AssertionError("the failure on chunk 5 was swallowed")
            slot = watch.Resume(config.recording_key(when(TUESDAY_2PM_END), 3600.0))
            assert slot.transcript() is None, "a partial transcript was saved whole"
            assert sorted(p.name for p in slot.chunks_dir().glob("*/part_*.txt")) == [
                "part_001.txt", "part_002.txt", "part_003.txt", "part_004.txt"]

            retry = ChunkProvider()
            watch.providers.get = lambda name=None: retry
            out = watch.process(audio, interactive=False)
    assert retry.sent == [5, 6], f"process re-billed paid chunks: {retry.sent}"
    assert fake.summarized == 1, fake.summarized
    body = fake.uploads[1]["body"]
    assert body.startswith("words of part 1") and body.endswith("words of part 6"), body
    assert out["stem"], out
    assert not slot.dir.exists(), "the checkpoint outlived a finished lecture"
results.append(run("process() resumes a half-billed transcription chunk by chunk", t25))


def t26():
    clear()
    audio = recording()
    # A slot written before per-chunk results existed: a transcript and
    # nothing else, no chunks folder.
    slot = watch.Resume(config.recording_key(when(TUESDAY_2PM_END), 3600.0))
    slot.dir.mkdir(parents=True, exist_ok=True)
    (slot.dir / "transcript.txt").write_text("the transcript an old build saved")
    with Fakes() as fake:
        watch.process(audio, interactive=False)
    assert fake.transcribed == 0, "an old slot's transcript was paid for again"
    assert fake.uploads[1]["body"] == "the transcript an old build saved"

    clear()
    audio = recording()
    # An old slot holding only a summary still transcribes, and the new code
    # hands the transcription somewhere to keep its chunks.
    slot.dir.mkdir(parents=True, exist_ok=True)
    (slot.dir / "summary.json").write_text(json.dumps({
        "summary_md": "# old", "topic_slug": "Old-Topic",
        "key_terms": [], "action_items": []}))
    with Fakes() as fake:
        out = watch.process(audio, interactive=False)
    assert fake.transcribed == 1 and fake.summarized == 0, (fake.transcribed, fake.summarized)
    assert fake.checkpoint == slot.chunks_dir(), fake.checkpoint
    assert "Old-Topic" in out["stem"], out
results.append(run("a resume slot from before per-chunk results still works", t26))


def t27():
    clear()
    audio = recording(body="CHUNKED AUDIO")
    checkpoint = config.WORK_DIR / "chunk-test"
    import shutil
    shutil.rmtree(checkpoint, ignore_errors=True)
    # A lecture short enough to go up whole, and one with no checkpoint at
    # all (the command line), both behave exactly as before.
    with ChunkedAudio(seconds=300.0):
        whole = ChunkProvider()
        text = transcribe.transcribe(audio, provider=whole, checkpoint=checkpoint)
    assert len(whole.sent) == 1, whole.sent
    assert not checkpoint.exists(), "a single request left chunk state behind"
    with ChunkedAudio(seconds=3000.0):
        plain = ChunkProvider()
        text = transcribe.transcribe(audio, provider=plain)
    assert plain.sent == [1, 2, 3, 4, 5], plain.sent
    assert text.endswith("words of part 5"), text
results.append(run("no checkpoint, or no split, transcribes as it always did", t27))


def t28():
    clear()
    audio = recording(body="CHUNKED AUDIO")
    checkpoint = config.WORK_DIR / "chunk-test"
    import shutil
    shutil.rmtree(checkpoint, ignore_errors=True)
    with ChunkedAudio(seconds=3000.0):
        try:
            transcribe.transcribe(audio, provider=ChunkProvider(fail_on=4),
                                  checkpoint=checkpoint)
        except RuntimeError:
            pass
        (slot,) = [p for p in checkpoint.iterdir() if p.is_dir()]
        # The Mac lost power after a rename reached the disk but the data did
        # not: part 1 is empty, part 2 is cut off partway through its record,
        # and part 3 is some other chunk's record under this chunk's name.
        # None of them is a chunk that came back empty, and trusting any of
        # them files the lecture with a silent gap.
        (slot / "part_001.txt").write_text("")
        whole = (slot / "part_002.txt").read_text()
        (slot / "part_002.txt").write_text(whole[: len(whole) // 2])
        (slot / "part_003.txt").write_text(json.dumps({"chunk": 7, "text": "elsewhere"}))
        retry = ChunkProvider()
        text = transcribe.transcribe(audio, provider=retry, checkpoint=checkpoint)
    assert retry.sent == [1, 2, 3, 4, 5], f"a damaged part was trusted: {retry.sent}"
    expected = "\n\n".join(f"words of part {n}" for n in range(1, 6))
    assert text == expected, text
    shutil.rmtree(checkpoint, ignore_errors=True)
results.append(run("a part file cut short by a crash is sent again, not trusted", t28))


# 29 to 34. A chunk whose transcript looks truncated is cut in half and each
#    half sent on its own, and each of those is billed as it lands too. Only
#    the finished chunk used to be kept, so a failure on the second half made
#    the retry pay again for the truncated whole AND the half that had
#    already come back. Every part is now kept under its own name ("2",
#    "2.0", "2.1", "2.1.0"), and a part found truncated is kept as split.
def part_of(path):
    """'chunk_002.1.m4a' -> '2.1'. The recording itself, sent whole, is '1'."""
    name = Path(path).name
    if not name.startswith("chunk_"):
        return "1"
    head, _, rest = name[len("chunk_"):-len(".m4a")].partition(".")
    return str(int(head)) + (f".{rest}" if rest else "")


class HalvingProvider(ChunkProvider):
    """Sends back too many words for the parts in `truncate`, fails on the
    ones in `fail`, and records every part it is sent."""

    def __init__(self, truncate=(), fail=(), **kw):
        super().__init__(**kw)
        self.truncation_word_threshold = 10
        self.truncate = set(truncate)
        self.fail = set(fail)

    def transcribe_file(self, path):
        part = part_of(path)
        self.sent.append(part)
        if part in self.fail:
            raise RuntimeError(f"upload failed on part {part}")
        if part in self.truncate:
            return " ".join(["word"] * 20)
        return f"words of part {part}"


class HalvingAudio(ChunkedAudio):
    """ChunkedAudio whose chunks can be halved, and halved again: a half of
    chunk_002.m4a is chunk_002.0.m4a, and lasts half as long."""

    def __init__(self, seconds, chunk_seconds=600):
        super().__init__(seconds)
        self.chunk_seconds = chunk_seconds

    def __enter__(self):
        super().__enter__()
        import math

        def fake_split(src, work_dir, seconds):
            src = Path(src)
            if seconds == self.chunk_seconds:
                count = max(1, math.ceil(self.seconds / seconds))
                return [work_dir / f"chunk_{i:03d}.m4a" for i in range(1, count + 1)]
            base = src.stem if src.name.startswith("chunk_") else "chunk_001"
            return [work_dir / f"{base}.{n}.m4a" for n in range(2)]

        def fake_duration(path):
            name = Path(path).name
            if not name.startswith("chunk_"):
                return self.seconds
            whole = min(self.seconds, self.chunk_seconds)
            return whole / 2 ** name[len("chunk_"):-len(".m4a")].count(".")

        transcribe.split = fake_split
        transcribe.duration_seconds = fake_duration
        return self


def halving_run(provider, audio, checkpoint, expect_error):
    try:
        transcribe.transcribe(audio, provider=provider, checkpoint=checkpoint)
    except RuntimeError as exc:
        assert expect_error in str(exc), exc
    else:
        raise AssertionError(f"the failure on {expect_error} was swallowed")


def t29():
    clear()
    audio = recording(body="CHUNKED AUDIO")
    checkpoint = config.WORK_DIR / "chunk-test"
    import shutil
    shutil.rmtree(checkpoint, ignore_errors=True)
    with HalvingAudio(seconds=1800.0):   # three 10 minute chunks
        failing = HalvingProvider(truncate={"2"}, fail={"2.1"})
        halving_run(failing, audio, checkpoint, "part 2.1")
        assert failing.sent == ["1", "2", "2.0", "2.1"], failing.sent
        (slot,) = [p for p in checkpoint.iterdir() if p.is_dir()]
        assert sorted(p.name for p in slot.glob("part_*.txt")) == [
            "part_001.txt", "part_002.0.txt", "part_002.txt"], list(slot.iterdir())

        retry = HalvingProvider(truncate={"2"})
        text = transcribe.transcribe(audio, provider=retry, checkpoint=checkpoint)
    # Exactly one more request: the half that failed. Not the whole chunk
    # again to learn it truncates, and not the half that already came back.
    assert retry.sent == ["2.1", "3"], f"the retry re-sent paid parts: {retry.sent}"
    expected = "\n\n".join(f"words of part {p}" for p in ("1", "2.0", "2.1", "3"))
    assert text == expected, text
    shutil.rmtree(checkpoint, ignore_errors=True)
results.append(run("a retry after half B of a truncated chunk fails sends only half B", t29))


def t30():
    clear()
    audio = recording(body="CHUNKED AUDIO")
    checkpoint = config.WORK_DIR / "chunk-test"
    import shutil
    shutil.rmtree(checkpoint, ignore_errors=True)
    with HalvingAudio(seconds=1800.0):
        # Chunk 2 truncates, and so does its second half, whose second
        # quarter then fails. Quarters are as deep as the halving goes.
        failing = HalvingProvider(truncate={"2", "2.1"}, fail={"2.1.1"})
        halving_run(failing, audio, checkpoint, "part 2.1.1")
        assert failing.sent == ["1", "2", "2.0", "2.1", "2.1.0", "2.1.1"], failing.sent

        retry = HalvingProvider(truncate={"2", "2.1"})
        text = transcribe.transcribe(audio, provider=retry, checkpoint=checkpoint)
        assert retry.sent == ["2.1.1", "3"], f"the retry re-sent paid parts: {retry.sent}"
        expected = "\n\n".join(f"words of part {p}"
                               for p in ("1", "2.0", "2.1.0", "2.1.1", "3"))
        assert text == expected, text

        # Once chunk 2 is back whole, its own record is its stitched text
        # rather than the split marker, and a third run pays for nothing.
        (slot,) = [p for p in checkpoint.iterdir() if p.is_dir()]
        record = json.loads((slot / "part_002.txt").read_text())
        assert record["text"] == "\n\n".join(
            f"words of part {p}" for p in ("2.0", "2.1.0", "2.1.1")), record
        again = HalvingProvider(truncate={"2", "2.1"})
        assert transcribe.transcribe(audio, provider=again, checkpoint=checkpoint) == expected
        assert again.sent == [], again.sent
    shutil.rmtree(checkpoint, ignore_errors=True)
results.append(run("halves of halves are kept too, and reused at every depth", t30))


def t31():
    clear()
    audio = recording(body="CHUNKED AUDIO")
    checkpoint = config.WORK_DIR / "chunk-test"
    import shutil
    shutil.rmtree(checkpoint, ignore_errors=True)
    with HalvingAudio(seconds=1800.0):
        halving_run(HalvingProvider(truncate={"2"}, fail={"2.1"}), audio,
                    checkpoint, "part 2.1")
        (slot,) = [p for p in checkpoint.iterdir() if p.is_dir()]
        # Power lost after the rename: half 2.0 is cut off partway through.
        whole = (slot / "part_002.0.txt").read_text()
        (slot / "part_002.0.txt").write_text(whole[: len(whole) // 2])
        retry = HalvingProvider(truncate={"2"})
        text = transcribe.transcribe(audio, provider=retry, checkpoint=checkpoint)
        assert retry.sent == ["2.0", "2.1", "3"], f"a damaged half was trusted: {retry.sent}"
        expected = "\n\n".join(f"words of part {p}" for p in ("1", "2.0", "2.1", "3"))
        assert text == expected, text

        shutil.rmtree(checkpoint, ignore_errors=True)
        halving_run(HalvingProvider(truncate={"2"}, fail={"2.1"}), audio,
                    checkpoint, "part 2.1")
        (slot,) = [p for p in checkpoint.iterdir() if p.is_dir()]
        # The split marker is cut short, and the half filed as 2.0 is some
        # other part's record, then one cut from audio of a different size.
        (slot / "part_002.txt").write_text('{"part": "2", "by')
        (slot / "part_002.0.txt").write_text(json.dumps(
            {"part": "2.1", "bytes": 1000, "text": "elsewhere"}))
        retry = HalvingProvider(truncate={"2"}, fail={"2.1"})
        halving_run(retry, audio, checkpoint, "part 2.1")
        assert retry.sent == ["2", "2.0", "2.1"], f"a damaged part was trusted: {retry.sent}"
        (slot / "part_002.0.txt").write_text(json.dumps(
            {"part": "2.0", "bytes": 999, "text": "cut differently"}))
        retry = HalvingProvider(truncate={"2"})
        text = transcribe.transcribe(audio, provider=retry, checkpoint=checkpoint)
        assert retry.sent == ["2.0", "2.1", "3"], f"a half of another size was trusted: {retry.sent}"
        assert "cut differently" not in text and "elsewhere" not in text, text
    shutil.rmtree(checkpoint, ignore_errors=True)
results.append(run("a sub-part file cut short or filed wrong is sent again, not trusted", t31))


def t32():
    clear()
    audio = recording(body="CHUNKED AUDIO")
    checkpoint = config.WORK_DIR / "chunk-test"
    import shutil
    shutil.rmtree(checkpoint, ignore_errors=True)
    # Short enough to go up whole, but a fast talker trips the truncation
    # check: the halves are billed one by one here as well.
    with HalvingAudio(seconds=300.0):
        halving_run(HalvingProvider(truncate={"1"}, fail={"1.1"}), audio,
                    checkpoint, "part 1.1")
        retry = HalvingProvider(truncate={"1"})
        text = transcribe.transcribe(audio, provider=retry, checkpoint=checkpoint)
    assert retry.sent == ["1.1"], f"the retry re-sent paid parts: {retry.sent}"
    assert text == "words of part 1.0\n\nwords of part 1.1", text
    shutil.rmtree(checkpoint, ignore_errors=True)
results.append(run("a recording sent whole keeps its halves when it truncates", t32))


def t33():
    clear()
    audio = recording(body="CHUNKED AUDIO")
    with Fakes() as fake:
        transcribe.transcribe = fake._saved[(transcribe, "transcribe")]
        with HalvingAudio(seconds=3600.0):   # six chunks
            slot = watch.Resume(config.recording_key(when(TUESDAY_2PM_END), 3600.0))
            # Another key's leftovers, halves and all: a different provider
            # or split. They must go, not be stitched in.
            stale = slot.chunks_dir() / "0123456789abcdef01234567"
            stale.mkdir(parents=True)
            for name in ("part_001.txt", "part_002.txt", "part_002.0.txt",
                         "part_002.1.0.txt"):
                (stale / name).write_text("{}")

            failing = HalvingProvider(truncate={"3", "3.0"}, fail={"3.1"})
            fake._remember(watch.providers, "get", lambda name=None: failing)
            try:
                watch.process(audio, interactive=False)
            except RuntimeError as exc:
                assert "part 3.1" in str(exc), exc
            else:
                raise AssertionError("the failure on part 3.1 was swallowed")
            assert not stale.exists(), "another key's sub-parts outlived it"
            assert sorted(p.name for p in slot.chunks_dir().glob("*/part_*.txt")) == [
                "part_001.txt", "part_002.txt", "part_003.0.0.txt", "part_003.0.1.txt",
                "part_003.0.txt", "part_003.txt"], list(slot.chunks_dir().glob("*/*"))

            retry = HalvingProvider(truncate={"3", "3.0"})
            watch.providers.get = lambda name=None: retry
            watch.process(audio, interactive=False)
    assert retry.sent == ["3.1", "4", "5", "6"], f"process re-billed paid parts: {retry.sent}"
    body = fake.uploads[1]["body"]
    order = ["1", "2", "3.0.0", "3.0.1", "3.1", "4", "5", "6"]
    assert body == "\n\n".join(f"words of part {p}" for p in order), body
    assert not slot.chunks_dir().exists(), "sub-part files outlived a finished lecture"
    assert not slot.dir.exists(), "the checkpoint outlived a finished lecture"
results.append(run("process() resumes a halved chunk and cleans up every part after", t33))


print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
