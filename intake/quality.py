"""Is a chunk hard to hear, and is the trouble the lecturer or the room?

Whisper scores every segment it transcribes, and the proxy passes those
scores back with the text (syllabus-accounts, /proxy/transcribe):

- avg_logprob: how sure the model was of the words. Clear speech sits
  around -0.2 to -0.4; a mumbled, accented or distant voice drops well below.
- no_speech_prob: how sure it was there was speech at all. A high value is
  a pause, a cough, or the room between sentences, and says nothing about
  how hard the lecturer is to follow.
- compression_ratio: how repetitive the text is. It climbs when the model
  loops ("and the and the and the") on audio it cannot make out.

An unsure segment is not always the lecturer. Students talking near the
recorder come back unsure too, and a second pass does nothing for them: the
lecture was never the problem. What tells the two apart without a speaker
model is loudness. The recorder is set up for the lecturer, so their voice is
the loud, steady one; side conversations reach it quieter. Each segment's
level is read off the audio itself (levels below), the lecturer's level is
taken from the segments the model was sure of, and an unsure segment well
under that level is counted as the room, not the lecture.

A chunk is judged hard to hear when enough of its speech is the lecturer
coming back unsure (QUALITY_HARD_SHARE). That is the one verdict that sends
it for a second pass. Every threshold here is a config constant, set from
real lectures with `intake quality` (quality_tune.py), which also writes a
page that re-scores in the browser as they move. The same rules are written
in JavaScript there; test_quality.py holds the two to the same answers.
"""

from __future__ import annotations

import math
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from intake import config, tools

# How finely the loudness is sampled. A tenth of a second is short enough to
# fall inside the shortest segment whisper writes, and an 8 minute chunk is
# still under 5,000 numbers.
LEVEL_WINDOW_SECONDS = 0.1
# Anything this quiet is silence, not a voice at some level.
SILENCE_DB = -70.0


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    avg_logprob: float
    no_speech_prob: float
    compression_ratio: float
    text: str = ""

    @property
    def seconds(self) -> float:
        return max(0.0, self.end - self.start)


def segments_from(raw) -> list[Segment]:
    """The segments out of a proxy answer, skipping any that are not whole.

    The proxy already drops a segment missing a score; this is the same rule
    again on this side, because a cached answer from an older run is read
    back through here too."""
    out: list[Segment] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        try:
            seg = Segment(
                start=float(item["start"]),
                end=float(item["end"]),
                avg_logprob=float(item["avg_logprob"]),
                no_speech_prob=float(item["no_speech_prob"]),
                compression_ratio=float(item["compression_ratio"]),
                text=str(item.get("text", "")),
            )
        except (KeyError, TypeError, ValueError):
            continue
        if all(math.isfinite(v) for v in (seg.start, seg.end, seg.avg_logprob,
                                          seg.no_speech_prob, seg.compression_ratio)) \
                and seg.end >= seg.start:
            out.append(seg)
    return out


@dataclass(frozen=True)
class Thresholds:
    """Every number the verdict turns on. The defaults are config's."""

    unsure_logprob: float = field(default_factory=lambda: config.QUALITY_UNSURE_LOGPROB)
    looping_ratio: float = field(default_factory=lambda: config.QUALITY_LOOPING_RATIO)
    no_speech: float = field(default_factory=lambda: config.QUALITY_NO_SPEECH)
    quieter_db: float = field(default_factory=lambda: config.QUALITY_QUIETER_DB)
    hard_share: float = field(default_factory=lambda: config.QUALITY_HARD_SHARE)
    min_speech_seconds: float = field(default_factory=lambda: config.QUALITY_MIN_SPEECH_SECONDS)

    def as_dict(self) -> dict:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


#: What a chunk can be judged. Only HARD sends it for a second pass.
CLEAR, HARD, NOISY_ROOM, TOO_LITTLE, UNSCORED = (
    "clear", "hard_to_hear", "noisy_room", "too_little_speech", "unscored")


@dataclass
class Verdict:
    verdict: str
    speech_seconds: float = 0.0
    lecturer_unsure_seconds: float = 0.0
    room_seconds: float = 0.0
    #: The lecturer's unsure speech as a share of all speech; what HARD is judged on.
    share: float = 0.0
    #: The lecturer's level in dBFS, or None when there was too little sure speech to set it.
    lecturer_db: float | None = None
    #: One per segment: "silence", "clear", "unsure" (the lecturer), or "room".
    labels: list[str] = field(default_factory=list)
    #: Set by transcribe.py when the second pass ran and its text was kept.
    second_pass_taken: bool = False

    @property
    def second_pass(self) -> bool:
        return self.verdict == HARD

    def summary(self) -> str:
        if self.verdict == UNSCORED:
            return "no scores came back for this part"
        if self.verdict == TOO_LITTLE:
            return f"only {self.speech_seconds:.0f}s of speech, too little to judge"
        room = f", {self.room_seconds:.0f}s of room noise set aside" if self.room_seconds >= 1 else ""
        what = {CLEAR: "clear", HARD: "hard to hear", NOISY_ROOM: "clear, noisy room"}[self.verdict]
        return (f"{what}: {self.share:.0%} of {self.speech_seconds:.0f}s of speech "
                f"came back unsure{room}")


def judge(segments: list[Segment], levels: list[float | None] | None = None,
          t: Thresholds | None = None) -> Verdict:
    """The verdict on one chunk from its segments and, if known, their levels.

    Mirrored line for line by judge() in quality_tune.py's page script. Change
    one, change the other; test_quality.py fails when they disagree.
    """
    t = t or Thresholds()
    if not segments:
        return Verdict(UNSCORED)
    if levels is None or len(levels) != len(segments):
        levels = [None] * len(segments)

    speech = [s.no_speech_prob < t.no_speech and s.seconds > 0 for s in segments]
    unsure = [s.avg_logprob < t.unsure_logprob or s.compression_ratio > t.looping_ratio
              for s in segments]
    speech_seconds = sum(s.seconds for s, sp in zip(segments, speech) if sp)
    # Never judged on no speech at all, even with the minimum set to zero:
    # the share below would be a division by nothing.
    if speech_seconds <= 0 or speech_seconds < t.min_speech_seconds:
        return Verdict(TOO_LITTLE, speech_seconds=speech_seconds,
                       labels=["clear" if sp and not u else "unsure" if sp else "silence"
                               for sp, u in zip(speech, unsure)])

    # The lecturer's level: the middle of the loudness of the speech the model
    # was sure of, weighted by how long each stretch ran. Only trusted when
    # there is enough of it (a fifth of the speech, and ten seconds); a chunk
    # that is nearly all unsure has no sure voice to measure against, and
    # every unsure second in it is then taken to be the lecturer's.
    sure = [(lv, s.seconds) for s, sp, u, lv in zip(segments, speech, unsure, levels)
            if sp and not u and lv is not None]
    sure_seconds = sum(sec for _, sec in sure)
    lecturer_db = None
    if sure_seconds >= max(10.0, 0.2 * speech_seconds):
        lecturer_db = _weighted_median(sure)

    labels: list[str] = []
    lecturer_unsure = room = 0.0
    for s, sp, u, lv in zip(segments, speech, unsure, levels):
        if not sp:
            labels.append("silence")
        elif not u:
            labels.append("clear")
        elif lecturer_db is not None and lv is not None and lv < lecturer_db - t.quieter_db:
            labels.append("room")
            room += s.seconds
        else:
            labels.append("unsure")
            lecturer_unsure += s.seconds

    share = lecturer_unsure / speech_seconds
    if share >= t.hard_share:
        verdict = HARD
    elif room / speech_seconds >= t.hard_share:
        verdict = NOISY_ROOM
    else:
        verdict = CLEAR
    return Verdict(verdict, speech_seconds=speech_seconds, lecturer_unsure_seconds=lecturer_unsure,
                   room_seconds=room, share=share, lecturer_db=lecturer_db, labels=labels)


def _weighted_median(pairs: list[tuple[float, float]]) -> float:
    pairs = sorted(pairs)
    half = sum(w for _, w in pairs) / 2
    running = 0.0
    for value, weight in pairs:
        running += weight
        if running >= half:
            return value
    return pairs[-1][0]


# --- Loudness ---------------------------------------------------------------

def windows(path: Path) -> list[tuple[float, float]] | None:
    """(time, dBFS) for every LEVEL_WINDOW_SECONDS of the audio, or None.

    One ffmpeg pass: resample to 16kHz mono, cut into fixed windows, and have
    astats print each window's RMS level. None when ffmpeg is missing or
    fails; the verdict then goes on without loudness, which means every
    unsure second counts as the lecturer's.
    """
    samples = int(16000 * LEVEL_WINDOW_SECONDS)
    try:
        result = subprocess.run(
            [tools.ffmpeg(), "-hide_banner", "-loglevel", "error", "-i", str(path),
             "-vn", "-ac", "1", "-ar", "16000",
             "-af", f"asetnsamples=n={samples}:p=0,astats=metadata=1:reset=1,"
                    "ametadata=mode=print:key=lavfi.astats.Overall.RMS_level:file=-",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=300,
        )
    except (OSError, subprocess.SubprocessError, RuntimeError):
        return None
    if result.returncode != 0:
        return None
    return parse_windows(result.stdout)


def parse_windows(text: str) -> list[tuple[float, float]]:
    """ametadata's print format: a `frame:.. pts:.. pts_time:T` line, then
    `lavfi.astats.Overall.RMS_level=V`. -inf (digital silence) is SILENCE_DB."""
    out: list[tuple[float, float]] = []
    at = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("frame:"):
            at = None
            for part in line.split():
                if part.startswith("pts_time:"):
                    try:
                        at = float(part.split(":", 1)[1])
                    except ValueError:
                        at = None
        elif line.startswith("lavfi.astats.Overall.RMS_level=") and at is not None:
            raw = line.split("=", 1)[1]
            try:
                value = float(raw)
            except ValueError:
                continue
            out.append((at, value if math.isfinite(value) else SILENCE_DB))
    return out


def levels(segments: list[Segment], series: list[tuple[float, float]] | None) -> list[float | None]:
    """Each segment's level: the median of the non-silent windows inside it."""
    if not series:
        return [None] * len(segments)
    out: list[float | None] = []
    for s in segments:
        inside = sorted(v for at, v in series if s.start <= at < s.end and v > SILENCE_DB)
        out.append(inside[len(inside) // 2] if inside else None)
    return out


def assess(chunk: Path, raw_segments, t: Thresholds | None = None) -> Verdict:
    """Everything above, for one chunk file and the scores that came back for it."""
    segs = segments_from(raw_segments)
    if not segs:
        return Verdict(UNSCORED)
    return judge(segs, levels(segs, windows(chunk)), t)
