"""Setting the hard-to-hear thresholds from a real recording.

    intake quality lecture.m4a

Cuts the recording into the same chunks a lecture goes up in, sends each
through the account service once, scores it (quality.py), and, for the
chunks judged hard to hear, asks for the second pass too so the two
transcripts can be read side by side. Then it writes a page next to the
recording: every chunk with its audio, its words colored by how the rules
read them, and controls that re-score everything as the thresholds move.
Mark each chunk as needing a second pass or not after listening, and "Fit
to my calls" finds the thresholds that agree with you.

Everything that cost money is kept in the output folder, so running it
again on the same recording, with different thresholds or none, spends
nothing. A second pass is charged at three times the chunk's length, so the
command says what it will cost and asks before sending any.
"""

from __future__ import annotations

import argparse
import html
import json
import shutil
import sys
import tempfile
from pathlib import Path

from intake import config, providers, quality
from intake import transcribe as tx


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _read(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write(path: Path, data) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(path)


def chunks_for(src: Path, out: Path, provider) -> list[Path]:
    """The recording as the chunks it would go up in, kept in out/chunks.

    Cut once and reused: the cached answers are only good for the exact
    chunks they came back for, so a second run must not re-cut them. A
    different recording (or the same one changed) is cut afresh."""
    chunk_dir = out / "chunks"
    manifest_path = out / "manifest.json"
    stat = src.stat()
    ident = {"source": src.name, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
             "chunk_seconds": provider.max_chunk_seconds}
    manifest = _read(manifest_path)
    if isinstance(manifest, dict) and {k: manifest.get(k) for k in ident} == ident:
        chunks = [chunk_dir / name for name in manifest.get("chunks", [])]
        if chunks and all(c.is_file() for c in chunks):
            return chunks

    shutil.rmtree(chunk_dir, ignore_errors=True)
    chunk_dir.mkdir(parents=True)
    config.WORK_DIR.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="quality_", dir=config.WORK_DIR))
    try:
        audio = src
        if tx._size(audio) > provider.compress_threshold_bytes:
            audio = tx.compress(audio, work)
        seconds = tx.duration_seconds(audio) or 0
        if seconds > provider.max_chunk_seconds or tx._size(audio) > provider.max_bytes:
            pieces = tx.split(audio, work, provider.max_chunk_seconds)
        else:
            pieces = [audio]
        chunks = []
        for n, piece in enumerate(pieces, start=1):
            dest = chunk_dir / f"chunk_{n:03d}{piece.suffix or '.m4a'}"
            shutil.copy2(piece, dest)
            chunks.append(dest)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    _write(manifest_path, {**ident, "chunks": [c.name for c in chunks]})
    return chunks


def _answer(provider, chunk: Path, grade: str) -> dict:
    result = provider.transcribe_scored(chunk, quality=grade)
    return {"text": result.text, "segments": result.segments,
            "charged_seconds": result.charged_seconds, "provider": result.provider}


def _cached(path: Path) -> dict | None:
    data = _read(path)
    return data if isinstance(data, dict) and isinstance(data.get("text"), str) else None


def run(src: Path, out: Path, compare: str, yes: bool, t: quality.Thresholds,
        provider=None, ask=input) -> dict:
    """Everything the page shows, gathered and cached. Returns the page data."""
    provider = provider or providers.get()
    if not hasattr(provider, "transcribe_scored"):
        raise SystemExit("error: tuning runs through the account service; sign this Mac "
                         "in first with: intake login")
    out.mkdir(parents=True, exist_ok=True)
    chunks = chunks_for(src, out, provider)
    log(f"{src.name}: {len(chunks)} part(s), results in {out}")

    rows = []
    offset = 0.0
    for n, chunk in enumerate(chunks, start=1):
        seconds = tx.duration_seconds(chunk) or 0.0
        std_path = chunk.with_suffix(".standard.json")
        std = _cached(std_path)
        if std is None:
            log(f"  part {n}/{len(chunks)}: transcribing ({seconds / 60:.0f} min of allowance)")
            std = _answer(provider, chunk, "standard")
            _write(std_path, std)
        segs = quality.segments_from(std.get("segments"))
        lv_path = chunk.with_suffix(".levels.json")
        lv = _read(lv_path)
        if not isinstance(lv, list) or len(lv) != len(segs):
            lv = quality.levels(segs, quality.windows(chunk))
            _write(lv_path, lv)
        verdict = quality.judge(segs, lv, t)
        rows.append({"n": n, "chunk": chunk, "seconds": seconds, "offset": offset,
                     "standard": std, "segments": segs, "levels": lv, "verdict": verdict})
        offset += seconds
        log(f"  part {n}: {verdict.summary()}")

    # The second passes, only once asked and priced.
    if compare != "none":
        wanted = [r for r in rows if compare == "all" or r["verdict"].second_pass]
        todo = [r for r in wanted if _cached(r["chunk"].with_suffix(".high.json")) is None]
        if todo:
            minutes = sum(r["seconds"] for r in todo) * 3 / 60
            log(f"\nA second pass on {len(todo)} part(s) uses about {minutes:.0f} min of "
                f"this month's allowance (three times their length).")
            go = yes
            if not go and sys.stdin.isatty():
                go = ask("Send them? [y/N] ").strip().lower() in ("y", "yes")
            if not go:
                log("  skipped; run again with --yes to send them")
                todo = []
            for r in todo:
                log(f"  part {r['n']}: second pass ...")
                try:
                    _write(r["chunk"].with_suffix(".high.json"), _answer(provider, r["chunk"], "high"))
                except providers.ProxyRefused as exc:
                    log(f"  part {r['n']}: not sent: {exc}")
                    break

    data = {
        "recording": src.name,
        "thresholds": t.as_dict(),
        "defaults": quality.Thresholds().as_dict(),
        "min_word_ratio": config.QUALITY_MIN_WORD_RATIO,
        "max_second_ratio": config.QUALITY_MAX_SECOND_PASS_RATIO,
        "chunks": [],
    }
    for r in rows:
        high = _cached(r["chunk"].with_suffix(".high.json"))
        data["chunks"].append({
            "n": r["n"],
            "audio": f"chunks/{r['chunk'].name}",
            "offset": r["offset"],
            "seconds": r["seconds"],
            "provider": r["standard"].get("provider", ""),
            "text": r["standard"]["text"],
            "segments": [{"start": s.start, "end": s.end, "avg_logprob": s.avg_logprob,
                          "no_speech_prob": s.no_speech_prob,
                          "compression_ratio": s.compression_ratio, "text": s.text}
                         for s in r["segments"]],
            "levels": r["levels"],
            "high": high["text"] if high else None,
            "high_ratio": quality.compression_ratio(high["text"]) if high else None,
        })
    _write(out / "data.json", data)
    page = out / "report.html"
    page.write_text(render(data), encoding="utf-8")
    return data


def _clock(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}" if seconds >= 3600 \
        else f"{seconds // 60}:{seconds % 60:02d}"


def table(data: dict, t: quality.Thresholds) -> str:
    lines = [f"{'part':>4}  {'time':>15}  {'verdict':<18} {'unsure':>6} {'speech':>7} "
             f"{'room':>6}  second pass"]
    for c in data["chunks"]:
        v = quality.judge(quality.segments_from(c["segments"]), c["levels"], t)
        span = f"{_clock(c['offset'])} to {_clock(c['offset'] + c['seconds'])}"
        second = "-" if c["high"] is None else f"{len(c['high'].split())} words vs {len(c['text'].split())}"
        lines.append(f"{c['n']:>4}  {span:>15}  {v.verdict:<18} {v.share:>6.0%} "
                     f"{v.speech_seconds:>6.0f}s {v.room_seconds:>5.0f}s  {second}")
    return "\n".join(lines)


# --- The page ---------------------------------------------------------------

# The verdict rules again, for the page to re-score as the controls move.
# Mirrors quality.judge line for line; test_quality.py runs both on the same
# chunks and fails on any difference.
JUDGE_JS = r"""
function judge(segments, levels, t) {
  if (!segments.length) return { verdict: "unscored", speech: 0, unsure: 0, room: 0, share: 0, lecturer: null, labels: [] };
  if (!levels || levels.length !== segments.length) levels = segments.map(() => null);
  const secs = (s) => Math.max(0, s.end - s.start);
  const speech = segments.map((s) => secs(s) > 0 && (s.text || "").trim() !== ""
    && !(s.no_speech_prob >= t.no_speech && s.avg_logprob < t.silence_logprob));
  const unsure = segments.map((s) => s.avg_logprob < t.unsure_logprob || s.compression_ratio > t.looping_ratio);
  let speechSeconds = 0;
  segments.forEach((s, i) => { if (speech[i]) speechSeconds += secs(s); });
  if (speechSeconds <= 0 || speechSeconds < t.min_speech_seconds) {
    return { verdict: "too_little_speech", speech: speechSeconds, unsure: 0, room: 0, share: 0, lecturer: null,
      labels: segments.map((s, i) => (speech[i] && !unsure[i] ? "clear" : speech[i] ? "unsure" : "silence")) };
  }
  const sure = [];
  segments.forEach((s, i) => { if (speech[i] && !unsure[i] && levels[i] !== null) sure.push([levels[i], secs(s)]); });
  let sureSeconds = 0;
  sure.forEach((p) => { sureSeconds += p[1]; });
  let lecturer = null;
  if (sureSeconds >= Math.max(10, 0.2 * speechSeconds)) {
    sure.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    let half = 0;
    sure.forEach((p) => { half += p[1]; });
    half = half / 2;
    let running = 0;
    lecturer = sure[sure.length - 1][0];
    for (const p of sure) { running += p[1]; if (running >= half) { lecturer = p[0]; break; } }
  }
  const labels = [];
  let lecturerUnsure = 0, room = 0;
  segments.forEach((s, i) => {
    const lv = levels[i];
    if (!speech[i]) labels.push("silence");
    else if (!unsure[i]) labels.push("clear");
    else if (lecturer !== null && lv !== null && lv < lecturer - t.quieter_db) { labels.push("room"); room += secs(s); }
    else { labels.push("unsure"); lecturerUnsure += secs(s); }
  });
  const share = lecturerUnsure / speechSeconds;
  let verdict;
  if (share >= t.hard_share) verdict = "hard_to_hear";
  else if (room / speechSeconds >= t.hard_share) verdict = "noisy_room";
  else verdict = "clear";
  return { verdict, speech: speechSeconds, unsure: lecturerUnsure, room, share, lecturer, labels };
}
"""


def render(data: dict) -> str:
    # "</" cannot appear inside the script's JSON, or a transcript containing
    # "</script>" would end the script there.
    blob = json.dumps(data).replace("</", "<\\/")
    return (PAGE.replace("__TITLE__", html.escape(data["recording"]))
                .replace("__JUDGE__", JUDGE_JS)
                .replace("__DATA__", blob))


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hearing check · __TITLE__</title>
<style>
:root {
  --bg: #f7f6f3; --panel: #ffffff; --ink: #1d1d1b; --muted: #6b6a66; --line: #e3e1dc;
  --clear: transparent; --unsure: #f6c9b8; --room: #d5dcf2; --silence: #ecebe7;
  --hard: #b8431f; --ok: #2f7a4b; --noisy: #3b5bab; --accent: #1d1d1b;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #171716; --panel: #212120; --ink: #ecebe8; --muted: #9c9a95; --line: #34332f;
    --unsure: #6b3526; --room: #2c3a66; --silence: #2a2a28; --hard: #f08a64; --ok: #72c493; --noisy: #93a9f0; --accent: #ecebe8;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
main { max-width: 1100px; margin: 0 auto; padding: 24px 16px 80px; }
h1 { font-size: 22px; margin: 0 0 4px; }
.sub { color: var(--muted); margin: 0 0 20px; }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 16px; margin-bottom: 16px; }
.controls { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 12px 20px; }
.controls label { display: block; font-size: 13px; color: var(--muted); }
.controls input[type=range] { width: 100%; }
.controls output { font-variant-numeric: tabular-nums; color: var(--ink); font-weight: 600; }
.row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
button { font: inherit; padding: 6px 12px; border-radius: 7px; border: 1px solid var(--line); background: var(--panel); color: var(--ink); cursor: pointer; }
button.on { background: var(--accent); color: var(--bg); border-color: var(--accent); }
.stat { font-size: 14px; }
.pill { display: inline-block; padding: 1px 9px; border-radius: 99px; font-size: 13px; font-weight: 600; border: 1px solid currentColor; }
.pill.hard_to_hear { color: var(--hard); } .pill.clear { color: var(--ok); } .pill.noisy_room { color: var(--noisy); }
.pill.too_little_speech, .pill.unscored { color: var(--muted); }
.chunk h2 { font-size: 17px; margin: 0; }
.chunk audio { width: 100%; margin: 10px 0; }
.facts { color: var(--muted); font-size: 13px; }
.texts { display: grid; grid-template-columns: 1fr; gap: 14px; }
.texts.two { grid-template-columns: 1fr 1fr; }
@media (max-width: 760px) { .texts.two { grid-template-columns: 1fr; } }
.texts h3 { font-size: 13px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); margin: 0 0 6px; }
.words { max-height: 340px; overflow-y: auto; padding-right: 6px; }
.words, .facts, h3, h2 { overflow-wrap: anywhere; }
.texts > div, .controls > div { min-width: 0; }
.seg { cursor: pointer; border-radius: 3px; padding: 0 1px; }
.seg.unsure { background: var(--unsure); } .seg.room { background: var(--room); } .seg.silence { background: var(--silence); color: var(--muted); }
.seg:hover { outline: 1px solid var(--muted); }
.key span { display: inline-block; padding: 0 6px; border-radius: 3px; margin-right: 6px; font-size: 13px; }
.mine { margin-top: 10px; }
textarea { width: 100%; min-height: 110px; font: 12px/1.4 ui-monospace, Menlo, monospace; background: var(--bg); color: var(--ink); border: 1px solid var(--line); border-radius: 7px; padding: 8px; }
.agree { font-weight: 600; }
</style>
</head>
<body>
<main>
  <h1>Hearing check</h1>
  <p class="sub" id="sub"></p>

  <section class="panel">
    <div class="controls" id="controls"></div>
    <p class="stat" id="summary"></p>
    <div class="row">
      <button id="fit">Fit to my calls</button>
      <button id="reset">Back to the defaults</button>
      <span class="stat agree" id="agree"></span>
    </div>
    <p class="facts key">
      <span class="seg unsure">lecturer, unsure</span><span class="seg room">quieter voice, set aside</span><span class="seg silence">no speech</span>
      Click any words to hear them.
    </p>
  </section>

  <div id="chunks"></div>

  <section class="panel">
    <h2 style="font-size:17px;margin:0 0 6px">Settings to keep</h2>
    <p class="facts">These are the values in <code>intake/config.py</code> for the thresholds above, with your calls. Paste this back into the chat.</p>
    <textarea id="export" readonly></textarea>
  </section>
</main>
<script>
const DATA = __DATA__;
__JUDGE__
const CONTROLS = [
  ["unsure_logprob", "Unsure below this confidence (avg log-prob)", -1.5, -0.1, 0.05],
  ["hard_share", "Hard to hear at this share of speech", 0.05, 0.8, 0.05],
  ["quieter_db", "A voice this many dB quieter is the room", 2, 20, 1],
  ["no_speech", "Silent at or above this no-speech probability", 0.2, 0.95, 0.05],
  ["silence_logprob", "...and below this confidence", -2, -0.3, 0.05],
  ["looping_ratio", "Repeating itself above this ratio", 1.8, 4, 0.1],
  ["min_speech_seconds", "Too little to judge under (seconds)", 0, 120, 5],
];
const NAMES = { hard_to_hear: "hard to hear", clear: "clear", noisy_room: "clear, noisy room", too_little_speech: "too little speech", unscored: "not scored" };
const store = { get(k) { try { return JSON.parse(localStorage.getItem(k)); } catch (e) { return null; } },
                set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} } };
const KEY = "hearing-check:" + DATA.recording;
let t = Object.assign({}, DATA.thresholds);
let calls = store.get(KEY) || {};

const clock = (s) => { s = Math.floor(s); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(x).padStart(2, "0")}` : `${m}:${String(x).padStart(2, "0")}`; };
const el = (tag, attrs = {}, text) => { const e = document.createElement(tag); Object.assign(e, attrs); if (text !== undefined) e.textContent = text; return e; };

document.getElementById("sub").textContent =
  `${DATA.recording} · ${DATA.chunks.length} part(s) · ${clock(DATA.chunks.reduce((a, c) => a + c.seconds, 0))} of audio`;

const controls = document.getElementById("controls");
for (const [key, label, min, max, step] of CONTROLS) {
  const wrap = el("div");
  const lab = el("label");
  lab.append(label + " ", el("output", { id: "o-" + key }));
  const input = el("input", { type: "range", min, max, step, value: t[key], id: "i-" + key });
  input.addEventListener("input", () => { t[key] = Number(input.value); update(); });
  wrap.append(lab, input);
  controls.append(wrap);
}

const cards = [];
for (const c of DATA.chunks) {
  const card = el("section", { className: "panel chunk" });
  const head = el("div", { className: "row" });
  head.append(el("h2", {}, `Part ${c.n} · ${clock(c.offset)} to ${clock(c.offset + c.seconds)}`));
  const pill = el("span", { className: "pill" });
  head.append(pill);
  const facts = el("p", { className: "facts" });
  const audio = el("audio", { controls: true, preload: "none", src: c.audio });
  const texts = el("div", { className: "texts" + (c.high !== null ? " two" : "") });
  const first = el("div");
  first.append(el("h3", {}, `First pass (${c.provider || "standard"})`));
  const words = el("div", { className: "words" });
  const spans = c.segments.map((s) => {
    const span = el("span", { className: "seg" }, s.text);
    span.addEventListener("click", () => { audio.currentTime = s.start; audio.play(); });
    words.append(span);
    return span;
  });
  if (!c.segments.length) words.textContent = c.text || "(no text)";
  first.append(words);
  texts.append(first);
  if (c.high !== null) {
    const second = el("div");
    const count = (x) => x.split(/\s+/).filter(Boolean).length;
    const few = count(c.high) < DATA.min_word_ratio * count(c.text);
    const loops = c.high_ratio > DATA.max_second_ratio;
    second.append(el("h3", {}, `Second pass (gpt-4o-transcribe) · ${count(c.high)} words against ${count(c.text)}` +
      (few ? " · too few, the first pass would be kept" : "") +
      (loops ? " · repeats itself, the first pass would be kept" : "")), el("div", { className: "words" }, c.high));
    texts.append(second);
  }
  const mine = el("div", { className: "row mine" });
  mine.append(el("span", { className: "facts" }, "Your call after listening:"));
  const yes = el("button", {}, "Needs a second pass");
  const no = el("button", {}, "Fine as it is");
  const setCall = (v) => { if (calls[c.n] === v) delete calls[c.n]; else calls[c.n] = v; store.set(KEY, calls); update(); };
  yes.addEventListener("click", () => setCall(true));
  no.addEventListener("click", () => setCall(false));
  mine.append(yes, no);
  card.append(head, facts, audio, texts, mine);
  document.getElementById("chunks").append(card);
  cards.push({ c, pill, facts, spans, yes, no });
}

function tip(s, lv, label) {
  const level = lv === null || lv === undefined ? "unknown" : lv.toFixed(1) + " dB";
  return `${clock(s.start)} · ${label} · confidence ${s.avg_logprob.toFixed(2)} · no-speech ${s.no_speech_prob.toFixed(2)} · repetition ${s.compression_ratio.toFixed(2)} · level ${level}`;
}

function agreement(th) {
  let marked = 0, match = 0;
  for (const c of DATA.chunks) {
    if (!(c.n in calls)) continue;
    marked++;
    if ((judge(c.segments, c.levels, th).verdict === "hard_to_hear") === calls[c.n]) match++;
  }
  return { marked, match };
}

function update() {
  for (const [key] of CONTROLS) {
    document.getElementById("o-" + key).textContent = t[key];
    document.getElementById("i-" + key).value = t[key];
  }
  let flagged = 0, minutes = 0;
  for (const card of cards) {
    const v = judge(card.c.segments, card.c.levels, t);
    card.pill.className = "pill " + v.verdict;
    card.pill.textContent = NAMES[v.verdict];
    if (v.verdict === "hard_to_hear") { flagged++; minutes += card.c.seconds * 3 / 60; }
    card.facts.textContent = v.verdict === "unscored" ? "No scores came back for this part (the fallback model answered)."
      : `${Math.round(v.share * 100)}% of ${Math.round(v.speech)}s of speech was the lecturer coming back unsure` +
        (v.room >= 1 ? ` · ${Math.round(v.room)}s of quieter voices set aside` : "") +
        (v.lecturer !== null ? ` · lecturer at ${v.lecturer.toFixed(1)} dB` : " · lecturer level unknown");
    card.spans.forEach((span, i) => {
      const label = v.labels[i] || "clear";
      span.className = "seg " + label;
      span.title = tip(card.c.segments[i], card.c.levels[i], label);
    });
    card.yes.className = calls[card.c.n] === true ? "on" : "";
    card.no.className = calls[card.c.n] === false ? "on" : "";
  }
  document.getElementById("summary").textContent =
    `${flagged} of ${DATA.chunks.length} part(s) would get a second pass, about ${Math.round(minutes)} min of allowance on top of the first pass.`;
  const a = agreement(t);
  document.getElementById("agree").textContent = a.marked ? `Matches your calls on ${a.match} of ${a.marked}` : "";
  const cfg = { thresholds: t, calls, recording: DATA.recording };
  document.getElementById("export").value =
    `QUALITY_UNSURE_LOGPROB = ${t.unsure_logprob}\nQUALITY_LOOPING_RATIO = ${t.looping_ratio}\nQUALITY_NO_SPEECH = ${t.no_speech}\nQUALITY_SILENCE_LOGPROB = ${t.silence_logprob}\n` +
    `QUALITY_QUIETER_DB = ${t.quieter_db}\nQUALITY_HARD_SHARE = ${t.hard_share}\nQUALITY_MIN_SPEECH_SECONDS = ${t.min_speech_seconds}\n\n` +
    JSON.stringify(cfg);
}

// The grid the fit searches. Of the settings that agree with the most calls,
// the one nearest the defaults wins, so a few calls do not drag every
// threshold to an extreme.
document.getElementById("fit").addEventListener("click", () => {
  if (!Object.keys(calls).length) { alert("Mark a few parts first."); return; }
  const d = DATA.defaults;
  const range = (a, b, s) => { const out = []; for (let x = a; x <= b + 1e-9; x += s) out.push(Math.round(x * 100) / 100); return out; };
  let best = null;
  for (const lp of range(-1.5, -0.2, 0.05)) for (const hs of range(0.05, 0.6, 0.05)) for (const q of range(4, 16, 2)) {
    const th = Object.assign({}, t, { unsure_logprob: lp, hard_share: hs, quieter_db: q });
    const a = agreement(th);
    const dist = Math.abs(lp - d.unsure_logprob) / 1.3 + Math.abs(hs - d.hard_share) / 0.55 + Math.abs(q - d.quieter_db) / 12;
    if (!best || a.match > best.match || (a.match === best.match && dist < best.dist)) best = { match: a.match, dist, th };
  }
  t = best.th;
  update();
});
document.getElementById("reset").addEventListener("click", () => { t = Object.assign({}, DATA.defaults); update(); });
update();
</script>
</body>
</html>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="intake quality",
        description="Score a recording for how hard it is to hear, and write a page to tune the thresholds on.",
    )
    parser.add_argument("recording", help="the lecture recording (.m4a, .mp3, .wav)")
    parser.add_argument("--out", help="where to put the results (default: next to the recording)")
    parser.add_argument("--compare", choices=("flagged", "all", "none"), default="flagged",
                        help="which parts get a second pass to compare against (default: flagged)")
    parser.add_argument("--yes", action="store_true", help="send second passes without asking")
    d = quality.Thresholds()
    for name in d.as_dict():
        parser.add_argument("--" + name.replace("_", "-"), type=float, default=getattr(d, name),
                            help=f"default {getattr(d, name)}")
    args = parser.parse_args(argv)

    src = Path(args.recording).expanduser().resolve()
    if not src.is_file():
        log(f"error: no such file: {src}")
        return 1
    out = Path(args.out).expanduser().resolve() if args.out else src.parent / f"{src.stem} hearing check"
    t = quality.Thresholds(**{name: getattr(args, name) for name in d.as_dict()})
    try:
        data = run(src, out, args.compare, args.yes, t)
    except providers.ProxyRefused as exc:
        log(f"error: {exc}")
        return 1
    print(table(data, t))
    print(f"\nOpen {out / 'report.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
