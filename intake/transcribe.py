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

from intake import config, providers, tools
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

    Results are filed under a key made from everything that decides what a
    chunk holds: the source file's size and modification time, the provider,
    its chunk length, whether the audio was compressed first, and the number
    and sizes of the chunks the split produced. A different recording, a
    different provider, or a split that cut the audio differently gets a
    different key, and never stitches in text from somebody else's chunk.
    Anything filed under another key is stale and is dropped on sight.
    """

    def __init__(self, root: Path, key: str):
        self.root = root
        self.dir = root / key

    @staticmethod
    def key(src: Path, provider: TranscriptionProvider, compressed: bool,
            chunks: list[Path]) -> str:
        stat = src.stat()
        ident = {
            "v": 2,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "provider": provider.name,
            "chunk_seconds": provider.max_chunk_seconds,
            "compressed": compressed,
            "chunks": [_size(c) for c in chunks],
        }
        raw = json.dumps(ident, sort_keys=True).encode()
        return hashlib.sha256(raw).hexdigest()[:24]

    def open(self) -> None:
        """Make this key's folder, and drop any other key's."""
        try:
            if self.root.is_dir():
                for other in self.root.iterdir():
                    if other != self.dir:
                        if other.is_dir():
                            shutil.rmtree(other, ignore_errors=True)
                        else:
                            other.unlink(missing_ok=True)
            self.dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    def _part(self, index: int) -> Path:
        return self.dir / f"part_{index:03d}.txt"

    def get(self, index: int) -> str | None:
        """The text an earlier attempt kept for this chunk, or None.

        Only a record that reads back whole counts. A part file that is empty
        or cut short (the Mac lost power after the rename but before the data
        reached the disk) is not a chunk that came back empty, and trusting
        it would file a lecture with a silent gap where that chunk's words
        belong. Such a file is sent again instead.
        """
        try:
            record = json.loads(self._part(index).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if (not isinstance(record, dict) or record.get("chunk") != index
                or not isinstance(record.get("text"), str)):
            return None
        return record["text"]

    def put(self, index: int, text: str) -> None:
        """Keep one chunk's text. Never raises: failing to save a result
        must not fail the transcription that just paid for it."""
        part = self._part(index)
        temp = part.with_name(f"{part.name}.{os.getpid()}.tmp")
        record = json.dumps({"chunk": index, "text": text})
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            # Flushed to the disk before the rename, so the name never points
            # at data that is still only in memory. get() refuses anything
            # that does not parse, for the case where it happened anyway.
            with open(temp, "w", encoding="utf-8") as handle:
                handle.write(record)
                handle.flush()
                os.fsync(handle.fileno())
            temp.replace(part)
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
            text = _transcribe_chunk(audio, provider, work_dir)
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
        for i, chunk in enumerate(chunks, start=1):
            earlier = saved.get(i) if saved else None
            if earlier is not None:
                log(f"  chunk {i}/{len(chunks)}: reusing what an earlier attempt paid for")
                parts.append(earlier)
                continue
            log(f"  chunk {i}/{len(chunks)} ...")
            progress(f"part {i} of {len(chunks)}")
            text = _transcribe_chunk(chunk, provider, work_dir)
            if saved:
                saved.put(i, text)
            parts.append(text)

        text = "\n\n".join(p for p in parts if p)
        log(f"  done: {len(text.split())} words from {len(chunks)} chunks")
        return text
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _transcribe_chunk(
    chunk: Path, provider: TranscriptionProvider, work_dir: Path, depth: int = 0
) -> str:
    """Transcribe one chunk, halving it if the output looks truncated.

    A model that truncates gives no signal when it hits its output cap, so an
    implausibly long result is the only tell. Rather than lose the tail of a
    lecture, split that chunk and try again. A provider whose
    truncation_word_threshold is None cannot truncate, and skips all of this.

    Deliberately sends no `prompt`: passing the previous chunk's tail for
    continuity makes these models re-transcribe that text at the start of the
    next chunk. Measured on a 39 min lecture, it duplicated two of five seams
    and inflated the transcript by 13%.
    """
    text = provider.transcribe_file(chunk)

    threshold = provider.truncation_word_threshold
    if threshold is None or len(text.split()) < threshold:
        return text

    seconds = duration_seconds(chunk)
    if depth >= 2 or not seconds or seconds < 120:
        log(f"    WARNING: {len(text.split())} words from {chunk.name} may be "
            f"truncated; lower {provider.name}'s max_chunk_seconds in providers.py")
        return text

    log(f"    {len(text.split())} words looks truncated, re-splitting "
        f"{seconds / 60:.0f} min chunk in half")
    halves = split(chunk, work_dir, seconds=int(seconds // 2) + 1)
    out = [_transcribe_chunk(h, provider, work_dir, depth + 1) for h in halves]
    return "\n\n".join(t for t in out if t)


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
