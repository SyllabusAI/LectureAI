"""Audio file -> transcript text, via whichever transcription provider is set.

Handles the two ways a lecture recording breaks a provider's request limit:
compress first, then split if compression wasn't enough.

Every limit that decides any of that comes from the provider (see
providers.py), never from a constant in here: the 25MB cap, the duration a
chunk may run to, and whether the model can silently truncate at all are
properties of the model, and they change when it does.

Progress goes to stderr, the transcript goes to stdout, so this works:
    python transcribe.py lecture.m4a > transcript.txt
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from intake import config, providers, quality, tools
from intake.providers import TranscriptionProvider


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _size(path: Path) -> int:
    return path.stat().st_size


def _mb(n_bytes: int) -> str:
    return f"{n_bytes / 1024 / 1024:.1f}MB"


def _ffmpeg(args: list[str], what: str) -> None:
    """Run ffmpeg quietly; raise with its stderr if it fails."""
    result = subprocess.run(
        [tools.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        tail = result.stderr.strip().splitlines()[-5:]
        raise RuntimeError(f"ffmpeg failed while {what}:\n" + "\n".join(tail))


def duration_seconds(path: Path) -> float | None:
    """Length of the audio, or None if ffprobe can't tell."""
    result = subprocess.run(
        [
            tools.ffprobe(), "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


def audio_codec(path: Path) -> str:
    """Codec name of the first audio stream, or "" if ffprobe can't tell."""
    result = subprocess.run(
        [
            tools.ffprobe(), "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=codec_name",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def compress(src: Path, work_dir: Path) -> Path:
    """Re-encode to mono 64kbps AAC, which is plenty for speech."""
    dest = work_dir / f"{src.stem}_compressed.m4a"
    log(f"  compressing {_mb(_size(src))} -> mono 64kbps m4a ...")
    _ffmpeg(
        ["-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
         "-c:a", "aac", "-b:a", "64k", str(dest)],
        f"compressing {src.name}",
    )
    log(f"  compressed to {_mb(_size(dest))}")
    return dest


def split(src: Path, work_dir: Path, seconds: int) -> list[Path]:
    """Cut the audio into ~`seconds` pieces, in order."""
    # Its own directory with a fixed chunk name, so a stem that happens to
    # prefix another file's chunks can't pull them into this glob.
    chunk_dir = Path(tempfile.mkdtemp(prefix="chunks_", dir=work_dir))
    pattern = str(chunk_dir / "chunk_%03d.m4a")

    # Stream-copy when the source is already AAC (fast, lossless). Anything
    # else has to be encoded, since an m4a container won't hold raw PCM.
    if audio_codec(src) == "aac":
        codec_args = ["-c", "copy"]
        how = "stream copy"
    else:
        codec_args = ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "aac", "-b:a", "64k"]
        how = "re-encode"

    log(f"  splitting into ~{seconds // 60} minute chunks ({how}) ...")
    _ffmpeg(
        ["-i", str(src), "-f", "segment", "-segment_time", str(seconds),
         "-reset_timestamps", "1", *codec_args, pattern],
        f"splitting {src.name}",
    )
    chunks = sorted(chunk_dir.glob("chunk_*.m4a"))
    if not chunks:
        raise RuntimeError(f"splitting {src.name} produced no chunks")
    log(f"  {len(chunks)} chunks: " + ", ".join(_mb(_size(c)) for c in chunks))
    return chunks


class ChunkCheckpoint:
    """Each chunk's transcript, on disk the moment the provider returns it.

    A lecture long enough to split goes up as several requests, each billed as
    it lands. Resume used to keep the transcript only once every chunk had
    succeeded, so a failure on the last chunk threw away the ones before it
    and the retry paid for all of them again: one 73 minute lecture was billed
    three times. Now a retry sends only the chunks that never came back.

    The same holds inside a chunk. One whose transcript looks truncated is cut
    in half and each half sent on its own (see _transcribe_chunk), and those
    are requests billed as they land too. Each result is filed under its
    part: "3" for chunk 3, "3.0" and "3.1" for its halves, "3.1.0" for the
    first half of the second half. A part that was found truncated is filed
    as split, so a retry goes straight to its halves rather than paying for
    the whole chunk again only to learn what it already knew.

    Results are filed under a key made from everything that decides what a
    chunk holds: the source file's size and modification time, the provider,
    its chunk length, whether the audio was compressed first, and the number
    and sizes of the chunks the split produced. A different recording, a
    different provider, or a split that cut the audio differently gets a
    different key, and never stitches in text from somebody else's chunk.
    Anything filed under another key is stale and is dropped on sight. The
    halves are cut on this Mac at retry time, so each record also carries the
    size of the audio it came from, and a half that was cut differently this
    time is sent again rather than trusted.
    """

    def __init__(self, root: Path, key: str):
        self.root = root
        self.dir = root / key

    @staticmethod
    def key(src: Path, provider: TranscriptionProvider, compressed: bool,
            chunks: list[Path], whole: bool = False) -> str:
        stat = src.stat()
        ident = {
            # 3: records are filed by part ("3", "3.0", "3.1") and carry the
            # size of the audio they came from, and a part can be filed as
            # split. A version 2 record is none of that.
            "v": 3,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "provider": provider.name,
            "chunk_seconds": provider.max_chunk_seconds,
            "compressed": compressed,
            "whole": whole,
            "chunks": [_size(c) for c in chunks],
        }
        raw = json.dumps(ident, sort_keys=True).encode()
        return hashlib.sha256(raw).hexdigest()[:24]

    def open(self, create: bool = True) -> None:
        """Drop any other key's folder, and make this key's unless told not to.

        A recording that goes up whole passes create=False: it only ever
        files anything if its one request comes back truncated, and put()
        makes the folder on the first thing it files."""
        try:
            if self.root.is_dir():
                for other in self.root.iterdir():
                    if other != self.dir:
                        if other.is_dir():
                            shutil.rmtree(other, ignore_errors=True)
                        else:
                            other.unlink(missing_ok=True)
            if create:
                self.dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    def _part(self, part: str) -> Path:
        head, _, rest = part.partition(".")
        return self.dir / (f"part_{int(head):03d}" + (f".{rest}" if rest else "") + ".txt")

    def _record(self, part: str, audio: Path) -> dict | None:
        """The record filed for this part of this audio, or None.

        Only a record that reads back whole counts. A part file that is empty
        or cut short (the Mac lost power after the rename but before the data
        reached the disk) is not a chunk that came back empty, and trusting
        it would file a lecture with a silent gap where that chunk's words
        belong. Nor is a record naming another part, or one cut from audio of
        a different size. Any of those is sent again instead.
        """
        try:
            record = json.loads(self._part(part).read_text(encoding="utf-8"))
            size = _size(audio)
        except (OSError, ValueError):
            return None
        if (not isinstance(record, dict) or record.get("part") != part
                or record.get("bytes") != size):
            return None
        return record

    def record(self, part: str, audio: Path) -> dict | None:
        """The whole record kept for this part, for what it says beyond the
        text: the scores it came back with, and whether it is a second pass."""
        return self._record(part, audio)

    def get(self, part: str, audio: Path) -> str | None:
        """The text an earlier attempt kept for this part, or None."""
        record = self._record(part, audio)
        if record is None or not isinstance(record.get("text"), str):
            return None
        return record["text"]

    def was_split(self, part: str, audio: Path) -> bool:
        """Whether an earlier attempt found this part truncated and split it."""
        record = self._record(part, audio)
        return record is not None and record.get("split") is True

    def put(self, part: str, audio: Path, text: str, **extra) -> None:
        """Keep one part's text, and anything else worth knowing on a retry
        (`segments`, the scores a second pass is decided on; `quality`,
        "high" once a second pass has replaced the text)."""
        self._write(part, {"part": part, "bytes": _size(audio), "text": text,
                           **{k: v for k, v in extra.items() if v is not None}})

    def put_split(self, part: str, audio: Path) -> None:
        """Keep that this part came back truncated and is being halved. Its
        text, once the halves are back, replaces this."""
        self._write(part, {"part": part, "bytes": _size(audio), "split": True})

    def _write(self, part: str, record: dict) -> None:
        """Never raises: failing to save a result must not fail the
        transcription that just paid for it."""
        path = self._part(part)
        temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            # Flushed to the disk before the rename, so the name never points
            # at data that is still only in memory. _record() refuses anything
            # that does not parse, for the case where it happened anyway.
            with open(temp, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(record))
                handle.flush()
                os.fsync(handle.fileno())
            temp.replace(path)
        except OSError:
            temp.unlink(missing_ok=True)


def transcribe(
    path: str | Path,
    on_progress=None,
    provider: TranscriptionProvider | None = None,
    checkpoint: Path | None = None,
) -> str:
    """Transcribe an audio file, compressing and splitting as needed.

    Two separate limits force a split, and both belong to the provider: the
    request size cap, and the output token cap that makes some models truncate
    long audio without raising. Which one binds depends on the model. On
    gpt-4o-mini-transcribe duration binds in practice; on Deepgram neither
    does, and a whole lecture goes up in one request.

    on_progress, if given, is called with a short human-readable string as the
    work moves along. A 75 minute lecture takes many minutes and, on the
    default provider, ten API calls, so something has to be able to say how
    far in it is.

    checkpoint, if given, is a folder where each chunk's transcript is kept
    as soon as it succeeds, so that a retry after a failure part way through
    pays only for the chunks that never came back (see ChunkCheckpoint). The
    caller removes it once the whole transcript is safely stored.
    """
    def progress(detail: str) -> None:
        if on_progress:
            try:
                on_progress(detail)
            except Exception:
                # Reporting progress must never break a transcription.
                pass

    src = Path(path).expanduser().resolve()
    if not src.is_file():
        raise FileNotFoundError(f"no such audio file: {src}")

    provider = provider or providers.get()
    total_seconds = duration_seconds(src)
    log(f"transcribing {src.name} ({_mb(_size(src))}"
        + (f", {total_seconds / 60:.0f} min" if total_seconds else "")
        + f") via {provider.name}")

    work_dir = Path(tempfile.mkdtemp(prefix=f"{src.stem}_", dir=config.WORK_DIR))
    try:
        audio = src
        compressed = _size(audio) > provider.compress_threshold_bytes
        if compressed:
            progress("compressing the audio")
            audio = compress(audio, work_dir)

        seconds = duration_seconds(audio) or total_seconds
        too_long = seconds is not None and seconds > provider.max_chunk_seconds
        too_big = _size(audio) > provider.max_bytes

        if not (too_long or too_big):
            progress("transcribing")
            # Through _transcribe_chunk, not straight to the provider. The
            # truncation check lives in there, and a recording short enough to
            # go up whole was the one case that skipped it: a fast talker in a
            # 40 minute class can pass the output cap without going anywhere
            # near the duration limit that triggers a split.
            #
            # The checkpoint is only written to if that happens: the one
            # request that came back whole is returned straight to the caller,
            # but halves of a truncated one are billed one by one like chunks.
            saved = None
            if checkpoint is not None:
                saved = ChunkCheckpoint(
                    Path(checkpoint),
                    ChunkCheckpoint.key(src, provider, compressed, [audio], whole=True))
                saved.open(create=False)
            scores: dict = {}
            text = _transcribe_chunk(audio, provider, work_dir, saved, "1", scores=scores)
            verdicts: list[quality.Verdict] = []
            text = _second_pass(audio, provider, text, scores.get("segments"), None, "1", verdicts)
            log(f"  done: {len(text.split())} words")
            return text

        why = "duration" if too_long else "file size"
        log(f"  splitting on {why}")
        parts: list[str] = []
        progress("splitting the audio")
        chunks = split(audio, work_dir, provider.max_chunk_seconds)
        saved = None
        if checkpoint is not None:
            saved = ChunkCheckpoint(
                Path(checkpoint),
                ChunkCheckpoint.key(src, provider, compressed, chunks))
            saved.open()
        verdicts: list[quality.Verdict] = []
        for i, chunk in enumerate(chunks, start=1):
            earlier = saved.get(str(i), chunk) if saved else None
            if earlier is not None:
                log(f"  chunk {i}/{len(chunks)}: reusing what an earlier attempt paid for")
                # A first pass kept with its scores and never replaced may
                # still be owed its second pass: the failure being retried
                # can have been that very call.
                record = saved.record(str(i), chunk) or {}
                if record.get("quality") != "high":
                    earlier = _second_pass(chunk, provider, earlier, record.get("segments"),
                                           saved, str(i), verdicts)
                parts.append(earlier)
                continue
            log(f"  chunk {i}/{len(chunks)} ...")
            progress(f"part {i} of {len(chunks)}")
            scores: dict = {}
            text = _transcribe_chunk(chunk, provider, work_dir, saved, str(i), scores=scores)
            # Kept before any second pass is tried, so a failure in that one
            # never costs the first pass again.
            if saved:
                saved.put(str(i), chunk, text, segments=scores.get("segments"))
            text = _second_pass(chunk, provider, text, scores.get("segments"), saved, str(i), verdicts)
            parts.append(text)

        text = "\n\n".join(p for p in parts if p)
        log(f"  done: {len(text.split())} words from {len(chunks)} chunks")
        if verdicts:
            hard = [v for v in verdicts if v.second_pass]
            taken = sum(v.second_pass_taken for v in hard)
            noisy = sum(v.verdict == quality.NOISY_ROOM for v in verdicts)
            log(f"  quality: {len(verdicts)} parts scored, {len(hard)} hard to hear"
                + (f" ({taken} given a second pass)" if hard else "")
                + (f", {noisy} with a noisy room" if noisy else ""))
        return text
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _call(provider: TranscriptionProvider, chunk: Path) -> tuple[str, list | None]:
    """One request, with the provider's segment scores when it has any."""
    scored = getattr(provider, "transcribe_scored", None)
    if scored is None:
        return provider.transcribe_file(chunk), None
    result = scored(chunk)
    return result.text, result.segments


def _second_pass(
    chunk: Path, provider: TranscriptionProvider, text: str, segments: list | None,
    saved: ChunkCheckpoint | None, part: str, verdicts: list,
) -> str:
    """Score one chunk and, when it is hard to hear, try the stronger model.

    Whatever goes wrong with the second pass, the first transcript stands: an
    allowance that cannot cover three times the chunk, a provider that is
    down, an answer that came back empty or cut short. A second pass is an
    improvement on a transcript that already exists, never a reason to lose
    it. See quality.py for how the verdict is reached.
    """
    if segments is None:
        return text
    verdict = quality.assess(chunk, segments)
    verdicts.append(verdict)
    log(f"    quality: {verdict.summary()}")
    if not verdict.second_pass:
        return text
    if not config.QUALITY_SECOND_PASS:
        log("    a second pass would run here; QUALITY_SECOND_PASS is off")
        return text
    try:
        better = provider.transcribe_scored(chunk, quality="high")
    except Exception as exc:
        log(f"    second pass not taken, first transcript kept: {exc}")
        return text
    new = better.text.strip()
    threshold = provider.truncation_word_threshold
    if not new or (threshold is not None and len(new.split()) >= threshold):
        log("    second pass came back empty or cut short; first transcript kept")
        return text
    if len(new.split()) < config.QUALITY_MIN_WORD_RATIO * len(text.split()):
        log(f"    second pass has {len(new.split())} words against {len(text.split())}; "
            f"it may have dropped lecture, so the first transcript is kept")
        return text
    cost = (f", {better.charged_seconds / 60:.0f} min of allowance"
            if better.charged_seconds else "")
    log(f"    second pass: {len(new.split())} words against {len(text.split())}{cost}")
    if saved:
        saved.put(part, chunk, new, quality="high")
    verdict.second_pass_taken = True
    return new


def _transcribe_chunk(
    chunk: Path, provider: TranscriptionProvider, work_dir: Path,
    saved: ChunkCheckpoint | None = None, part: str = "1", depth: int = 0,
    scores: dict | None = None,
) -> str:
    """Transcribe one chunk, halving it if the output looks truncated.

    A model that truncates gives no signal when it hits its output cap, so an
    implausibly long result is the only tell. Rather than lose the tail of a
    lecture, split that chunk and try again. A provider whose
    truncation_word_threshold is None cannot truncate, and skips all of this.

    Every request in here is billed as it lands, so with a checkpoint each
    half's text is kept under its own part the moment it comes back, and a
    chunk found truncated is kept as split before its halves go up (see
    ChunkCheckpoint). A retry after half B fails sends half B and nothing
    else. `part` names this chunk there; the caller keeps the text of the
    top-level one, which is the same text as its halves stitched together.

    Deliberately sends no `prompt`: passing the previous chunk's tail for
    continuity makes these models re-transcribe that text at the start of the
    next chunk. Measured on a 39 min lecture, it duplicated two of five seams
    and inflated the transcript by 13%.
    """
    if depth and saved:
        earlier = saved.get(part, chunk)
        if earlier is not None:
            log(f"    part {part}: reusing what an earlier attempt paid for")
            return earlier

    seconds = None
    if saved and saved.was_split(part, chunk):
        seconds = duration_seconds(chunk)
    if seconds:
        log(f"    part {part} came back truncated before; going straight to its halves")
    else:
        text, segments = _call(provider, chunk)
        # Only a chunk that came back in one piece is scored: halves each
        # have their own scores, and nothing here stitches them together.
        if depth == 0 and scores is not None:
            scores["segments"] = segments

        threshold = provider.truncation_word_threshold
        if threshold is None or len(text.split()) < threshold:
            if depth and saved:
                saved.put(part, chunk, text)
            return text

        if scores is not None:
            scores.pop("segments", None)
        seconds = duration_seconds(chunk)
        if depth >= 2 or not seconds or seconds < 120:
            log(f"    WARNING: {len(text.split())} words from {chunk.name} may be "
                f"truncated; lower {provider.name}'s max_chunk_seconds in providers.py")
            if depth and saved:
                saved.put(part, chunk, text)
            return text

        log(f"    {len(text.split())} words looks truncated, re-splitting "
            f"{seconds / 60:.0f} min chunk in half")
        if saved:
            saved.put_split(part, chunk)

    halves = split(chunk, work_dir, seconds=int(seconds // 2) + 1)
    out = [_transcribe_chunk(h, provider, work_dir, saved, f"{part}.{n}", depth + 1)
           for n, h in enumerate(halves)]
    text = "\n\n".join(t for t in out if t)
    if depth and saved:
        saved.put(part, chunk, text)
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Transcribe a lecture recording. Transcript prints to stdout."
    )
    parser.add_argument("audio", help="path to .m4a / .mp3 / .wav")
    parser.add_argument(
        "--model", default=None,
        help=f"transcription provider, default {config.TRANSCRIBE_MODEL}. One of: "
             + ", ".join(sorted(providers.PROVIDERS)),
    )
    args = parser.parse_args(argv)

    try:
        print(transcribe(args.audio, provider=providers.get(args.model)))
    except Exception as exc:
        log(f"error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
