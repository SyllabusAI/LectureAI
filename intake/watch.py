"""Orchestrator: audio file in, transcript and summary out, filed in Drive.

    intake watch --once ~/.intake/inbox/lecture.m4a   # one file, then exit
    intake watch                                          # watch inbox/ until Ctrl-C

The watcher never dies on a bad file. Anything that fails is logged and left
in inbox/ so it can be retried, and the next recording still gets processed.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import queue
import shutil
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from intake import account
from intake import config
from intake import notion_tasks
from intake import providers
from intake import summarize
from intake import transcribe
from intake import upload as drive


def acquire_single_instance_lock():
    """Fail fast if another watcher is already running.

    Two watchers on the same inbox both transcribe every file — paying twice —
    and race on the same staging paths, so whichever finishes first deletes the
    other's files mid-upload. The flock is released automatically when the
    process exits, including on a crash or SIGKILL.
    """
    # Open without truncating. Mode "w" empties the file before the flock is
    # even attempted, so a watcher that then lost the race had already wiped
    # the winner's pid: the panel read a held lock with no pid, reported the
    # watcher stopped, and every press of its button started another loser
    # that wiped it again. Only the holder may write here.
    fd = os.open(config.LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o644)
    handle = os.fdopen(fd, "r+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError(
            f"another watcher already holds {config.LOCK_FILE.name}. "
            f"Stop it first:  pkill -f 'intake.*watch'"
        )
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle  # keep a reference alive; closing it drops the lock


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", file=sys.stderr, flush=True)


def write_log_line(*fields: str) -> None:
    """Append one tab-separated record to pipeline.log.

    The recorder writes its failures to the same file (config.append_log_line),
    so a lecture that never produced audio shows up in the panel's recent
    list the same way a transcription that failed does.
    """
    config.append_log_line(*fields)


def write_status(stage: str, file: str = "", course: str = "",
                 detail: str = "", started: str = "") -> None:
    """Record what the watcher is doing, for the control panel to read.

    pipeline.log gets its line only once a lecture is finished, so without
    this an 80 minute recording spends a quarter of an hour looking like it
    is sitting untouched in the inbox.

    Written to a temp file and renamed, because the panel polls this once a
    second and must never catch a half-written file. Failures are swallowed:
    progress reporting must not be able to take down a lecture.
    """
    try:
        config.STATUS_FILE.parent.mkdir(exist_ok=True)
        payload = {
            "stage": stage, "file": file, "course": course, "detail": detail,
            "started": started or datetime.now().isoformat(timespec="seconds"),
            "updated": datetime.now().isoformat(timespec="seconds"),
            "pid": os.getpid(),
        }
        # Per writer, not a fixed name every writer shares: the watcher is
        # single-instance today, so this is the same hazard as record.py's
        # state file rather than a live bug, and it costs nothing to not have.
        temp = config.STATUS_FILE.with_suffix(f".json.{os.getpid()}.tmp")
        try:
            temp.write_text(json.dumps(payload))
            temp.replace(config.STATUS_FILE)
        finally:
            temp.unlink(missing_ok=True)
    except OSError:
        pass


def clear_status() -> None:
    """Say the watcher is idle again."""
    try:
        config.STATUS_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def is_audio(path: Path) -> bool:
    return (
        path.suffix.lower() in config.AUDIO_EXTENSIONS
        and not path.name.startswith(".")
    )


def wait_until_stable(path: Path) -> None:
    """Block until the file size holds steady, so we don't read a partial sync."""
    last_size = -1
    steady_since = None
    deadline = time.monotonic() + config.STABILITY_TIMEOUT_SECONDS

    while True:
        if not path.exists():
            raise FileNotFoundError(f"{path.name} disappeared while waiting")
        size = path.stat().st_size

        if size == last_size and size > 0:
            if steady_since is None:
                steady_since = time.monotonic()
            if time.monotonic() - steady_since >= config.STABILITY_SECONDS:
                return
        else:
            if last_size >= 0:
                log(f"  {path.name} still growing ({size / 1024 / 1024:.1f}MB) ...")
            steady_since = None
            last_size = size

        if time.monotonic() > deadline:
            raise TimeoutError(
                f"{path.name} never stopped changing after "
                f"{config.STABILITY_TIMEOUT_SECONDS}s"
            )
        time.sleep(config.STABILITY_POLL_SECONDS)


# How long a half-finished lecture's working files are kept. Long enough to
# survive a weekend of Drive being unreachable, short enough that abandoned
# work does not accumulate forever.
RESUME_DAYS = 14


class Resume:
    """What an attempt at one recording has already paid for.

    Transcription and summarization both cost money and minutes. The code
    here used to stage its output only after both had finished, and then
    never read it again, so the comment promising that "a retry costs nothing
    but the upload" was not true of any path: a failed upload re-ran both
    stages, and a failed summary threw away a transcript that had just been
    paid for.

    Keyed by the recording rather than by the filename, because the topic slug
    comes out of the summary and the filename changes with it.
    """

    def __init__(self, key: str):
        # The key is a timestamp, but a colon is a path separator's cousin on
        # some filesystems and this becomes a directory name.
        self.dir = config.WORK_DIR / "resume" / key.replace(":", "-")

    def _read(self, name: str):
        try:
            return (self.dir / name).read_text()
        except OSError:
            return None

    def _write(self, name: str, text: str) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / name
        path.write_text(text)
        return path

    def transcript(self) -> str | None:
        return self._read("transcript.txt")

    def save_transcript(self, text: str) -> Path:
        # Written before summarizing, not after. A summary that fails used to
        # discard the whole transcript with it.
        return self._write("transcript.txt", text)

    def chunks_dir(self) -> Path:
        """Where each chunk's transcript waits until the whole one is saved.

        A slot written before this existed simply has no such folder, and
        transcribes from the start exactly as it used to.
        """
        return self.dir / "chunks"

    def clear_chunks(self) -> None:
        shutil.rmtree(self.chunks_dir(), ignore_errors=True)

    def transcript_path(self) -> Path:
        """The transcript on disk, which is the file the upload sends."""
        return self.dir / "transcript.txt"

    def summary(self) -> dict | None:
        raw = self._read("summary.json")
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def save_summary(self, result: dict) -> None:
        self._write("summary.json", json.dumps(result))

    def markdown(self, text: str) -> Path:
        """The rendered summary, as the file the upload actually sends."""
        return self._write("summary.md", text)

    def done(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


def sweep_resume(days: int = RESUME_DAYS) -> int:
    """Drop working files from attempts nobody came back to. Returns how many."""
    root = config.WORK_DIR / "resume"
    if not root.is_dir():
        return 0
    cutoff = time.time() - days * 86400
    dropped = 0
    for slot in root.iterdir():
        try:
            if slot.is_dir() and slot.stat().st_mtime < cutoff:
                shutil.rmtree(slot, ignore_errors=True)
                dropped += 1
        except OSError:
            continue
    return dropped


def preflight(interactive: bool) -> None:
    """Fail before spending money on transcription if something obvious is off."""
    config.schedule()
    # A provider with no key setting carries its own credential: the managed
    # proxy signs with this Mac's device token, and there is no .env key to
    # check. Otherwise it is whichever key that provider actually spends,
    # which is not always OpenAI's.
    setting = providers.get().api_key_setting
    if setting:
        config.require(setting)
    if not account.managed():
        config.require("ANTHROPIC_API_KEY")
    drive.get_service(interactive)


def process(audio_path: str | Path, interactive: bool = True) -> dict:
    """Run the full chain on one recording. Returns a summary of what happened."""
    path = Path(audio_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"no such audio file: {path}")

    sweep_resume()
    mtime = path.stat().st_mtime
    length = transcribe.duration_seconds(path)
    course, source = config.resolve_course(path, mtime, length)
    date = config.lecture_date(mtime, length)
    log(f"processing {path.name} -> {course} on {date} (matched by {source})")
    if source == "chosen":
        log("  filed under the course chosen when the recording was started")
    elif source == "filename":
        log("  timestamp matched no class; took the course from the filename")
    elif course == config.UNKNOWN_COURSE:
        log("  neither the timestamp nor the filename identifies a course; "
            "filing under UNKNOWN")

    began = datetime.now().isoformat(timespec="seconds")
    status = lambda stage, detail="": write_status(  # noqa: E731
        stage, path.name, course, detail, began)

    # Identifies this recording on the Drive files it produces, so re-running
    # it replaces its own past output instead of colliding with a different
    # lecture that happens to share a name. It also names the folder holding
    # what an earlier attempt already paid for.
    rec_key = config.recording_key(mtime, length)
    time_suffix = config.recording_time_suffix(mtime, length)
    saved = Resume(rec_key)

    status("transcribing", "starting")
    transcript = saved.transcript()
    if transcript is None:
        # Each chunk is kept as it lands, so a failure on the fifth of six
        # does not bill the first four again on the retry.
        transcript = transcribe.transcribe(
            path, on_progress=lambda detail: status("transcribing", detail),
            checkpoint=saved.chunks_dir())
        saved.save_transcript(transcript)
        saved.clear_chunks()
    else:
        log(f"  reusing the transcript an earlier attempt paid for "
            f"({len(transcript.split()):,} words)")

    status("summarizing", f"{len(transcript.split()):,} words")
    result = saved.summary()
    if result is None:
        result = summarize.summarize(transcript, course, date)
        saved.save_summary(result)
    else:
        log("  reusing the summary an earlier attempt paid for")
    stem = summarize.build_filename(course, date, result["topic_slug"])

    # The files the upload sends live beside the checkpoint rather than in
    # processed/. Two lectures of one class on one day produce the same stem,
    # and staging them under it meant the second overwrote the first's copy
    # while both were still waiting on Drive.
    txt_path = saved.transcript_path()
    md_path = saved.markdown(summarize.render_markdown(result, course, date))

    # The summary goes in the course folder as a Doc; the raw transcript goes
    # one level down, so the course folder stays a clean list of study notes.
    status("uploading", stem)
    summary = drive.upload(
        md_path, course, interactive,
        as_google_doc=config.SUMMARY_AS_GOOGLE_DOC,
        # Named explicitly in both cases: md_path is the checkpoint's
        # summary.md now, and passing that through would file every lecture
        # in Drive under the same name.
        name=stem if config.SUMMARY_AS_GOOGLE_DOC else f"{stem}.md",
        recording_key=rec_key, time_suffix=time_suffix,
    )
    md_url = summary.url

    # Name the transcript after whatever the summary ended up called, so a
    # renamed pair stays a pair.
    final_stem = summary.name.removesuffix(".md")
    transcript_upload = drive.upload(
        txt_path, course, interactive, subfolder=config.TRANSCRIPT_SUBFOLDER,
        name=f"{final_stem}.txt",
        recording_key=rec_key, time_suffix=time_suffix,
    )
    txt_url = transcript_upload.url

    # Both uploads landed, so there is nothing left to resume.
    saved.done()

    # Notion comes last and never raises. The lecture is already safe in Drive
    # by this point, so a Notion outage or a misconfigured database must not
    # cost the recording, and the tasks are recoverable from the summary Doc.
    notion_result = None
    notion_warning = ""
    if notion_tasks.enabled():
        status("notion", f"{len(result['action_items'])} action items")
        try:
            notion_result = notion_tasks.push(
                result["action_items"], course, source_url=md_url
            )
            log(f"  notion: {notion_result['added']} added, "
                f"{notion_result['skipped']} already there, "
                f"{notion_result['failed']} failed")
            notion_warning = notion_tasks.outcome_warning(
                notion_result, len(result["action_items"]))
        except Exception as exc:
            log(f"  notion: skipped ({exc})")
            notion_warning = f"Notion was not reached: {exc}"
        # Recorded with the lecture, not just logged to stderr: a dropped
        # to-do is silent otherwise, and the panel is where it gets noticed.
        notion_warning = " ".join(notion_warning.split())[:300]
    elif result["action_items"]:
        log(f"  {len(result['action_items'])} action items (Notion not configured)")

    if config.DELETE_ORIGINAL_AFTER_UPLOAD:
        path.unlink()
        log(f"  deleted original {path.name}")
        original = "(deleted)"
    else:
        destination = config.PROCESSED_DIR / path.name
        if destination.resolve() != path:
            # Not onto whatever is already sitting there. A phone hands back
            # the same filename every time, and the older lecture's audio was
            # replaced without a word.
            destination = config.free_path(config.PROCESSED_DIR, path.name)
            shutil.move(str(path), str(destination))
        original = str(destination)
    # The recording has been filed, so the note about which course somebody
    # picked for it has nothing left to answer.
    config.forget_course(path)

    # Fields six and seven: what did not reach Notion (blank when everything
    # did), then a small JSON object of measurements the panel's dashboard
    # reads: how much audio the lecture held, how many words the transcript
    # ran to, how many to-dos and key terms came out of it. Lines written
    # before either field existed have five (or six) fields and are read
    # exactly as before; they simply have nothing measured.
    measures = {
        "seconds": round(length or 0),
        "words": len(transcript.split()),
        "actions": len(result.get("action_items") or []),
        "terms": len(result.get("key_terms") or []),
    }
    write_log_line(course, path.name, final_stem, md_url, notion_warning,
                   json.dumps(measures, separators=(",", ":")))
    log(f"  done: {final_stem}")
    log(f"  summary:    {md_url}")
    log(f"  transcript: {txt_url}")

    return {
        "course": course, "date": date, "stem": final_stem,
        "summary_url": md_url, "transcript_url": txt_url,
        "original": original, "notion_warning": notion_warning,
    }


class InboxHandler(FileSystemEventHandler):
    """Queue new audio files for the main thread to process one at a time."""

    def __init__(self, work_queue: queue.Queue):
        self.queue = work_queue

    def _enqueue(self, raw_path: str) -> None:
        path = Path(raw_path)
        if is_audio(path):
            log(f"noticed {path.name}")
            self.queue.put(path)

    def on_created(self, event):
        if not event.is_directory:
            self._enqueue(event.src_path)

    def on_moved(self, event):
        # Some sync clients write a temp file and rename it into place.
        if not event.is_directory:
            self._enqueue(event.dest_path)


def run_watcher() -> int:
    lock = acquire_single_instance_lock()  # noqa: F841 — held for process life
    preflight(interactive=False)

    work: queue.Queue = queue.Queue()
    seen: set[Path] = set()

    for existing in sorted(config.INBOX_DIR.iterdir()):
        if is_audio(existing):
            log(f"found waiting: {existing.name}")
            work.put(existing)

    observer = Observer()
    observer.schedule(InboxHandler(work), str(config.INBOX_DIR), recursive=False)
    observer.start()
    log(f"watching {config.INBOX_DIR} for "
        f"{', '.join(sorted(config.AUDIO_EXTENSIONS))}; Ctrl-C to stop")

    try:
        while True:
            try:
                path = work.get(timeout=1)
            except queue.Empty:
                continue

            resolved = path.resolve()
            if resolved in seen or not path.exists():
                continue
            seen.add(resolved)

            try:
                write_status("waiting", path.name, detail="waiting for the file to finish copying")
                wait_until_stable(path)
                process(path, interactive=False)
            except Exception as exc:
                # One bad file must never take the watcher down.
                log(f"ERROR on {path.name}: {exc}")
                traceback.print_exc(file=sys.stderr)
                write_log_line("ERROR", path.name, str(exc))
                if path.exists():
                    log(f"  left {path.name} in inbox for a retry")
                seen.discard(resolved)
            finally:
                # Idle again either way, so the panel stops showing a stage
                # that finished or died.
                clear_status()
    except KeyboardInterrupt:
        log("stopping")
    finally:
        observer.stop()
        observer.join()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="intake watch",
        description="Process lecture recordings into transcripts and summaries."
    )
    parser.add_argument("--once", metavar="FILE",
                        help="process a single recording and exit")
    args = parser.parse_args(argv)

    if args.once:
        try:
            lock = acquire_single_instance_lock()  # noqa: F841
            preflight(interactive=True)
            process(args.once, interactive=True)
        except Exception as exc:
            log(f"error: {exc}")
            traceback.print_exc(file=sys.stderr)
            return 1
        finally:
            clear_status()
        return 0

    try:
        return run_watcher()
    except RuntimeError as exc:
        log(f"error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
