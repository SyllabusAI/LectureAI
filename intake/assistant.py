"""Ask questions across the lectures already filed in Drive.

The pipeline deletes a recording once its summary and transcript are safely
uploaded, so everything the assistant can draw on lives in Drive. Nothing the
assistant reads is written to disk on this Mac: what it fetches is held in
memory for CACHE_TTL_SECONDS so a follow-up question does not fetch it again,
and is gone when the app quits, when the time is up, or when this Mac signs
out. (Older versions kept a plaintext copy in a .assistant folder; it is
deleted the first time the assistant runs.) What is written locally is
pipeline.log, the index below, and assistant.log, which holds counts and
lecture titles, never a question, an answer, or any lecture text.
pipeline.log is the index: one line per filed lecture, carrying
the course, the class date, the topic slug and the Doc's URL. This module
turns that index into context, asks Claude, and streams the answer back.

Two stages, because the whole cost model rests on them:

  Stage 1  Every summary for the course, as cached document blocks. A summary
           is a couple of pages; a whole course of them is still small, and
           after the first question of a session the cache serves them for a
           tenth of the price. Most questions never need more than this.

  Stage 2  Full transcripts, and only for the lectures the model actually
           names. Claude asks for them by calling the fetch_transcripts tool
           rather than us guessing, so an escalation is a decision with a
           stated reason instead of a heuristic. A transcript runs 6,000 to
           9,000 words, which is why this is not simply always on.

How often stage 2 fires is the escalation rate, modeled at 15% in
HOME-STRETCH.md and never measured. Every session writes a line to
assistant.log saying whether it escalated and what it cost, because that
number is what Pro's margin moves with and a guess is not good enough to
price on.

The model is Sonnet 5 (config.ASSISTANT_MODEL), not Opus. That is a costing
decision, not a shrug: see the note on the constant.
"""

from __future__ import annotations

import json
import re
import sys
import time
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from intake import config, insights

# A Doc URL as upload.py files it: .../document/d/<id>/edit?usp=drivesdk
DOC_ID_RE = re.compile(r"/d/([A-Za-z0-9_-]{16,})")

# Transcripts are filed one level down from the summary, by upload.py.
TRANSCRIPT_SUBFOLDER = "Transcripts"

# Guards on a single escalation. There is no entitlement to wire a cap to yet
# (P6 is unbuilt), and this path spends a personal key, so the ceiling is here
# instead: enough transcripts to answer a real exam question, not enough to
# turn one careless request into a bill worth noticing.
MAX_ESCALATION_LECTURES = 6
MAX_TRANSCRIPT_CHARS = 120_000

# Room for a study guide across several lectures without truncating mid-answer.
MAX_OUTPUT_TOKENS = 8_000

# One round of tool use is all the flow needs: ask, escalate, answer. A second
# would mean the model is fishing, and fishing through transcripts is the
# expensive failure this cap exists to prevent.
MAX_TOOL_ROUNDS = 1

SYSTEM_PROMPT = """You are the study assistant inside Syllabus, a tool that \
records a student's lectures, transcribes them, and files a summary of each one.

You are given the summaries of the lectures in one course. Answer the student's \
question from them. These are the student's own classes, so be specific: name \
the lecture and the date a point came from rather than speaking generally.

The summaries are condensed. When the question needs something a summary does \
not carry, the exact wording of a definition, an example worked in class, what \
the instructor said about an exam, call the fetch_transcripts tool with the \
lectures you need and say why. Do not call it when the summaries already \
answer the question; a transcript is thirty times the length of a summary and \
the student pays for it either way.

Cite the lecture you are drawing on. Write plainly, in the second person, and \
never pad. If the lectures do not cover what was asked, say so rather than \
filling the gap from general knowledge, and say what they do cover instead."""

FETCH_TOOL = {
    "name": "fetch_transcripts",
    "description": (
        "Fetch the full verbatim transcript of specific lectures, when their "
        "summaries are not enough to answer. Ask only for the lectures you "
        "actually need."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "lectures": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Lecture names exactly as given in the document titles, "
                    f"at most {MAX_ESCALATION_LECTURES}."
                ),
            },
            "reason": {
                "type": "string",
                "description": "Why the summaries are not sufficient here.",
            },
        },
        "required": ["lectures", "reason"],
    },
}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


@dataclass(frozen=True)
class Lecture:
    """One filed lecture, as pipeline.log knows it."""
    course: str
    date: str
    name: str          # ACCT-4321_2026-09-17_Process-Costing-And-CVP
    url: str
    terms: int = 0
    actions: int = 0

    @property
    def doc_id(self) -> str:
        found = DOC_ID_RE.search(self.url)
        return found.group(1) if found else ""

    @property
    def topic(self) -> str:
        """Process Costing And CVP, from the stem's third part."""
        parts = self.name.split("_", 2)
        return parts[2].replace("-", " ") if len(parts) > 2 else self.name

    @property
    def title(self) -> str:
        """How the model is asked to refer to it, and how it cites it back."""
        return f"{self.course} {self.date}: {self.topic}"


def library() -> list[Lecture]:
    """Every lecture filed so far, newest first.

    Read from pipeline.log rather than from Drive, because the log is local
    and instant and Drive is neither. A lecture the student deleted in Drive
    is still listed here; it fails when fetched, which is handled where the
    fetching happens.
    """
    path = config.LOG_FILE
    if not path.exists():
        return []
    rows = insights.parse_log(path.read_text(errors="replace"))
    out: list[Lecture] = []
    seen: set[str] = set()
    for row in rows:
        if row.get("error") or not row.get("url") or not row.get("name"):
            continue
        if row["name"] in seen:      # a re-run filed the same lecture twice
            continue
        seen.add(row["name"])
        out.append(Lecture(
            course=row["course"], date=row["date"], name=row["name"],
            url=row["url"], terms=row.get("terms", 0),
            actions=row.get("actions", 0),
        ))
    out.sort(key=lambda l: l.date, reverse=True)
    return out


def courses() -> list[dict]:
    """Courses that have at least one filed lecture, most lectures first."""
    counts: dict[str, list[Lecture]] = {}
    for lec in library():
        counts.setdefault(lec.course, []).append(lec)
    out = [{"course": code, "lectures": len(lecs), "latest": lecs[0].date}
           for code, lecs in counts.items()]
    out.sort(key=lambda c: (-c["lectures"], c["course"]))
    return out


# --- Drive ------------------------------------------------------------------
#
# Everything here reads files this app created, which is all the drive.file
# scope grants and all it needs. Nothing new is asked of the student's Google
# account, so the assistant works the moment it ships.


# How long text fetched from Drive is remembered, in memory only. Long enough
# for a study session's follow-ups, short enough that closing the books on a
# course does not leave it sitting in a running app. Measured from the fetch,
# not from the last use, so a question every few minutes cannot keep it alive.
CACHE_TTL_SECONDS = 30 * 60

_MEMORY: dict[str, tuple[float, str]] = {}
_legacy_swept = False


def _cached(key: str) -> str | None:
    entry = _MEMORY.get(key)
    if entry is None:
        return None
    stored, text = entry
    if time.monotonic() - stored >= CACHE_TTL_SECONDS:
        _MEMORY.pop(key, None)
        return None
    return text


def _cache(key: str, text: str) -> None:
    now = time.monotonic()
    for old in [k for k, (stored, _) in _MEMORY.items() if now - stored >= CACHE_TTL_SECONDS]:
        del _MEMORY[old]
    _MEMORY[key] = (now, text)


def remove_legacy_cache() -> None:
    """Delete the plaintext `.assistant` folders that older versions left behind.

    They held every summary and transcript the assistant had ever fetched, in
    files anyone with access to the account could read, with no expiry. Looked
    for in the active home and in every profile's home, plus the root, where
    the layout from before profiles kept it.
    """
    from intake import profiles
    homes = {config.BASE_DIR, config.ROOT_DIR}
    homes.update(config.profile_home(p) for p in profiles.PROFILES.values())
    for home in homes:
        target = home / ".assistant"
        try:
            if target.is_symlink():
                target.unlink()
            elif target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
        except OSError:
            pass      # nothing to fetch a second time is worth failing a question over


def sweep_legacy_once() -> None:
    """remove_legacy_cache(), the first time this process asks for it."""
    global _legacy_swept
    if not _legacy_swept:
        _legacy_swept = True
        remove_legacy_cache()


def clear_cache() -> None:
    """Forget everything held for the assistant: fetched text and session ids.

    Called when this Mac signs out or its sign-in is revoked, so the next
    person to use it starts with nothing of the last one's.
    """
    global _legacy_swept
    _MEMORY.clear()
    _SESSIONS.clear()
    remove_legacy_cache()
    _legacy_swept = True


# Native Google formats are exported; anything else is downloaded as it is.
GOOGLE_NATIVE = "application/vnd.google-apps."


def _decode(raw) -> str:
    return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)


def summary_text(service, lec: Lecture) -> str:
    """One lecture's summary as plain text, whatever Drive is holding it as.

    Remembered in memory under the file's id for CACHE_TTL_SECONDS. A summary
    is rewritten only when the lecture is processed again, which files a new
    line in the log, so a stale entry is not a case that arises in practice;
    the id changes with the document.

    Not every filed summary is a Doc. Lectures from before the pipeline
    started converting on upload are still sitting in Drive as text/markdown,
    and export() refuses anything that is not a Docs Editors file. So the
    file's type decides which call to make, rather than the age of the
    lecture deciding whether the assistant can read it at all.
    """
    if not lec.doc_id:
        return ""
    hit = _cached(lec.doc_id)
    if hit is not None:
        return hit
    meta = service.files().get(fileId=lec.doc_id, fields="mimeType").execute()
    if str(meta.get("mimeType", "")).startswith(GOOGLE_NATIVE):
        raw = service.files().export(fileId=lec.doc_id, mimeType="text/plain").execute()
    else:
        raw = service.files().get_media(fileId=lec.doc_id).execute()
    text = _decode(raw)
    _cache(lec.doc_id, text)
    return text


def _find_transcript(service, lec: Lecture) -> str | None:
    """The file id of a lecture's transcript, searched by name.

    The summary and the transcript share a stem; they are told apart by type,
    because the summary was converted to a native Doc on upload and only the
    transcript is still text/plain. That makes the mimeType filter the whole
    of the disambiguation, and the Transcripts subfolder need not be walked.
    """
    from intake import upload
    query = (
        f"name contains '{upload._escape(lec.name)}' and "
        f"mimeType = 'text/plain' and trashed = false"
    )
    found = service.files().list(
        q=query, fields="files(id, name)", pageSize=5,
        spaces="drive", supportsAllDrives=True,
    ).execute().get("files", [])
    return found[0]["id"] if found else None


def transcript_text(service, lec: Lecture) -> str:
    """One lecture's verbatim transcript, or "" when it cannot be found."""
    key = f"{lec.name}.transcript"
    hit = _cached(key)
    if hit is not None:
        return hit
    file_id = _find_transcript(service, lec)
    if not file_id:
        return ""
    text = _decode(service.files().get_media(fileId=file_id).execute())
    _cache(key, text)
    return text


# --- Context ----------------------------------------------------------------


def document_block(title: str, body: str, context: str, cache: bool = False) -> dict:
    """One source document, quotable with a citation back to this title."""
    block = {
        "type": "document",
        "title": title,
        "context": context,
        "source": {"type": "text", "media_type": "text/plain", "data": body},
        "citations": {"enabled": True},
    }
    if cache:
        block["cache_control"] = {"type": "ephemeral"}
    return block


def summary_docs(service, lectures: list[Lecture]) -> list[dict]:
    """The summaries that could be read, as {title, context, body}.

    The one shape both paths start from: the BYO path turns these into
    document blocks itself, and the managed path sends them to the account
    service, which builds the blocks on its side.
    """
    docs = []
    for lec in lectures:
        # One unreadable lecture must not cost the student the other twenty.
        # A summary deleted in Drive, or filed in a format that will not come
        # back as text, is dropped with a note in the log rather than raised.
        try:
            body = summary_text(service, lec).strip()
        except Exception as exc:              # noqa: BLE001 - skipped, not fatal
            log(f"  skipping {lec.name}: {exc}")
            continue
        if not body:
            continue
        docs.append({"title": lec.title, "body": body,
                     "context": f"Summary of the {lec.course} lecture on {lec.date}."})
    return docs


def stage_one(service, lectures: list[Lecture]) -> list[dict]:
    """The summaries, as document blocks, with the last one marking the cache.

    The breakpoint goes on the final block so everything above it, the system
    prompt included, is served from cache on every later turn of the session.
    That is what makes a follow-up question cost a fraction of the first.
    """
    blocks = [document_block(d["title"], d["body"], context=d["context"])
              for d in summary_docs(service, lectures)]
    if blocks:
        blocks[-1]["cache_control"] = {"type": "ephemeral"}
    return blocks


def transcript_docs(service, lectures: list[Lecture], wanted: list[str]) -> tuple[list[dict], list[str]]:
    """Transcripts for the lectures the model named, as {title, context, body}.

    Matched loosely, because the model is quoting a title back to us rather
    than echoing an id, and a near miss should still find the lecture. Capped
    on both count and total size: this is the expensive path.
    """
    by_name = {lec.title.lower(): lec for lec in lectures}
    picked: list[Lecture] = []
    for want in wanted[:MAX_ESCALATION_LECTURES]:
        needle = want.strip().lower()
        match = by_name.get(needle)
        if match is None:
            for title, lec in by_name.items():
                if needle in title or lec.name.lower() in needle:
                    match = lec
                    break
        if match is not None and match not in picked:
            picked.append(match)

    docs, used, total = [], [], 0
    for lec in picked:
        body = transcript_text(service, lec).strip()
        if not body:
            continue
        if total + len(body) > MAX_TRANSCRIPT_CHARS:
            break
        total += len(body)
        used.append(lec.title)
        docs.append({"title": f"{lec.title} (full transcript)", "body": body,
                     "context": f"Verbatim transcript of the {lec.course} lecture on {lec.date}."})
    return docs, used


def stage_two(service, lectures: list[Lecture], wanted: list[str]) -> tuple[list[dict], list[str]]:
    """Transcripts for the lectures the model named. Returns blocks and names."""
    docs, used = transcript_docs(service, lectures, wanted)
    return [document_block(d["title"], d["body"], context=d["context"]) for d in docs], used


# --- Instrumentation --------------------------------------------------------


def record_session(course: str, question: str, escalated: bool,
                   lectures: list[str], usage: dict) -> None:
    """One line per session in assistant.log: the escalation rate, measured.

    HOME-STRETCH.md prices Pro on a 15% escalation rate that has never been
    observed. This is the observation. Written as JSON so the rate can be read
    straight off the file rather than parsed out of prose.
    """
    line = json.dumps({
        "when": datetime.now().isoformat(timespec="seconds"),
        "course": course,
        "question_chars": len(question),
        "escalated": escalated,
        "escalated_to": lectures,
        **usage,
    })
    try:
        with (config.BASE_DIR / "assistant.log").open("a") as handle:
            handle.write(line + "\n")
    except OSError:
        pass      # never let bookkeeping take down an answer


def escalation_rate() -> dict:
    """What assistant.log says the rate actually is, for comparing to 15%."""
    path = config.BASE_DIR / "assistant.log"
    if not path.exists():
        return {"sessions": 0, "escalated": 0, "rate": None}
    total = hits = 0
    for line in path.read_text(errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        total += 1
        hits += 1 if row.get("escalated") else 0
    return {"sessions": total, "escalated": hits,
            "rate": round(hits / total, 3) if total else None}


def _usage(totals: dict, usage) -> dict:
    """Accumulate a response's token counts, cache lines included."""
    for key, attr in (
        ("input_tokens", "input_tokens"),
        ("output_tokens", "output_tokens"),
        ("cache_read_tokens", "cache_read_input_tokens"),
        ("cache_write_tokens", "cache_creation_input_tokens"),
    ):
        totals[key] = totals.get(key, 0) + (getattr(usage, attr, 0) or 0)
    return totals


# --- Asking -----------------------------------------------------------------


def ask(question: str, course: str, *, service=None, client=None):
    """Answer a question about one course, streaming as it is written.

    Yields dicts the panel turns into SSE events:
        {"type": "status",   "text": ...}   what it is doing right now
        {"type": "text",     "text": ...}   a fragment of the answer
        {"type": "citation", "title": ...}  a lecture the answer drew on
        {"type": "done",     ...}           usage, escalation, timing
        {"type": "error",    "text": ...}   readable, and the end of the stream
    """
    import anthropic

    question = question.strip()
    if not question:
        yield {"type": "error", "text": "Ask a question first."}
        return

    sweep_legacy_once()

    lectures = [l for l in library() if l.course == course]
    if not lectures:
        yield {"type": "error",
               "text": f"No lectures filed for {course} yet. "
                       f"Record one and it will be here when it finishes."}
        return

    started = time.time()
    if service is None:
        from intake import upload
        service = upload.get_service(interactive=False)
    # A signed-in Mac asks through the account service, on its key and its
    # session count. A client handed in is the BYO path by definition, which
    # is how the tests below reach it on a Mac that happens to be signed in.
    fell_back = False
    if client is None and managed():
        for event in ask_managed(question, course, lectures, service, started):
            if event["type"] == "_own_key":
                fell_back = True
                break
            yield event
        if not fell_back:
            return
        # The account cannot answer (no study sessions on its plan, or a
        # service without the route yet) and this Mac has a key of its own,
        # which is what answered before it signed in. Keep answering on it.
        yield {"type": "status", "text": "Using your own Anthropic key"}
    if client is None:
        client = anthropic.Anthropic(api_key=config.require("ANTHROPIC_API_KEY"))

    if not fell_back:  # the managed path already said so
        yield {"type": "status",
               "text": f"Reading {len(lectures)} "
                       f"{'summary' if len(lectures) == 1 else 'summaries'}"}

    try:
        blocks = stage_one(service, lectures)
    except Exception as exc:
        yield {"type": "error", "text": f"Could not read your notes from Drive: {exc}"}
        return
    if not blocks:
        yield {"type": "error",
               "text": f"The {course} lectures are filed, but their summaries "
                       f"could not be read from Drive."}
        return

    content = blocks + [{"type": "text", "text": question}]
    messages = [{"role": "user", "content": content}]
    totals: dict = {}
    escalated_to: list[str] = []
    cited: set[str] = set()

    for round_no in range(MAX_TOOL_ROUNDS + 1):
        tools = [FETCH_TOOL] if round_no < MAX_TOOL_ROUNDS else []
        reply: list[dict] = []
        tool_use = None
        try:
            with client.messages.stream(
                model=config.ASSISTANT_MODEL,
                max_tokens=MAX_OUTPUT_TOKENS,
                system=SYSTEM_PROMPT,
                messages=messages,
                **({"tools": tools} if tools else {}),
            ) as stream:
                for event in stream.text_stream:
                    yield {"type": "text", "text": event}
                answer = stream.get_final_message()
        except Exception as exc:
            yield {"type": "error", "text": f"The assistant could not finish: {exc}"}
            return

        _usage(totals, answer.usage)
        for block in answer.content:
            # Rebuilt field by field rather than dumped wholesale. The SDK
            # hangs its own attributes on a parsed block (parsed_output among
            # them) and the API rejects the message when they are handed back,
            # which is a 400 that only ever shows up on an escalation.
            if block.type == "tool_use":
                tool_use = block
                reply.append({"type": "tool_use", "id": block.id,
                              "name": block.name, "input": block.input})
            elif block.type == "text":
                reply.append({"type": "text", "text": block.text})
            if block.type == "text":
                for cite in (getattr(block, "citations", None) or []):
                    title = getattr(cite, "document_title", "")
                    if title and title not in cited:
                        cited.add(title)
                        yield {"type": "citation", "title": title}

        if tool_use is None:
            break

        wanted = list(tool_use.input.get("lectures", []))
        reason = str(tool_use.input.get("reason", "")).strip()
        yield {"type": "status",
               "text": f"Going to the full transcripts: {reason}" if reason
                       else "Going to the full transcripts"}

        try:
            docs, used = stage_two(service, lectures, wanted)
        except Exception as exc:
            docs, used = [], []
            log(f"  transcript fetch failed: {exc}")
        escalated_to.extend(used)

        result: list[dict] = docs or [{
            "type": "text",
            "text": ("Those transcripts could not be retrieved. Answer from "
                     "the summaries you already have, and say that the "
                     "verbatim wording was not available."),
        }]
        messages.append({"role": "assistant", "content": reply})
        messages.append({"role": "user", "content": [{
            "type": "tool_result",
            "tool_use_id": tool_use.id,
            "content": result,
        }]})

    record_session(course, question, bool(escalated_to), escalated_to, totals)
    yield {"type": "done",
           "escalated": bool(escalated_to),
           "escalated_to": escalated_to,
           "seconds": round(time.time() - started, 1),
           **totals}


# --- Asking through the account service --------------------------------------
#
# The same two stages, with the model on the other side of
# syllabus-accounts' /proxy/assistant. This Mac still reads everything from
# Drive; what moved is the key, the prompt, and the count of sessions, none of
# which a Mac should be trusted to hold for somebody else's bill.
#
# A session is the service's, not ours: it opens on the first question and
# carries follow-ups until it runs out of questions, time, or money. The id is
# kept here, one per course, only so the next question can ride on it. Losing
# it (a restart) costs a session, never an answer.

_SESSIONS: dict[str, str] = {}

ASSISTANT_PATH = "/proxy/assistant"

#: What a refusal from /proxy/assistant means to somebody at the panel.
ASSISTANT_REASONS = {
    "not_a_device": "This Mac's sign-in was not accepted. Sign in again.",
    "rate_limited": "Too many questions at once. Wait a minute and ask again.",
    "provider_busy": "The assistant is busy right now. Ask again in a moment.",
    "provider_unavailable": "The assistant could not be reached. Ask again in a moment.",
    "service_ceiling": "The study assistant is paused for everyone this month. We are on it.",
    "too_large": "That is more than one question can carry. Try a narrower question.",
    "already_escalated": "That question already went to the full transcripts.",
}


def managed() -> bool:
    from intake import account
    return account.managed()


def _stream(body: dict):
    """POST to /proxy/assistant. Returns (status, events or refusal body).

    On 200 the second item is an iterator over the service's events, one
    dict each. Anything else is its JSON body. Replaced wholesale by the tests.
    """
    import requests
    from intake import account

    acct = account.load()
    if not acct or not acct.token:
        return 401, {"error": "not_a_device"}
    base = account._destination_for_token()
    res = requests.post(
        base + ASSISTANT_PATH, json=body, stream=True,
        headers={"Authorization": "Bearer " + acct.token,
                 "Accept": "text/event-stream"},
        # The read timeout is between bytes, not for the whole answer, and the
        # model can think for a while before it writes its first word.
        timeout=(account.TIMEOUT, account.SLOW_TIMEOUT),
    )
    if res.status_code != 200:
        try:
            data = res.json()
        except ValueError:
            data = {}
        return res.status_code, data if isinstance(data, dict) else {}

    def events():
        with res:
            for line in res.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data:"):
                    continue
                try:
                    yield json.loads(line[5:].strip())
                except ValueError:
                    continue
    return 200, events()


stream_transport = _stream


def own_key_answers(status: int, data: dict) -> bool:
    """Whether a refusal should fall back to this Mac's own ANTHROPIC_API_KEY.

    Only when the account cannot answer at all: a plan with no study sessions
    (allowance 0, every account until Pro is sold), or a service that has no
    /proxy/assistant yet (404). A Pro account that has used its month is not
    moved onto the person's own bill without asking.
    """
    if not config.ANTHROPIC_API_KEY:
        return False
    if status == 404:
        return True
    return (data.get("error") == "allowance_exhausted" and data.get("unit") == "sessions"
            and not data.get("allowance"))


def refusal_text(status: int, data: dict) -> str:
    """What the service said, in words for the panel."""
    error = str(data.get("error", ""))
    if error == "allowance_exhausted" and data.get("unit") == "sessions":
        allowed = data.get("allowance")
        if not allowed:
            return "Study sessions come with the Pro plan. Upgrade from your account page to use the assistant."
        return (f"You have used all {allowed} of this month's study sessions. "
                f"They start again next month.")
    if error == "allowance_exhausted":
        return "This account has used its study assistant allowance for the month."
    return ASSISTANT_REASONS.get(error) or f"The account service refused the question ({error or status})."


def _round(body: dict, course: str, totals: dict, cost: list):
    """One call to the service. Yields panel events; the last is the outcome.

    The final item is {"type": "_outcome", ...}: an escalation to act on, or
    nothing, or the error that ended it. It is consumed by ask_managed and
    never reaches the panel.
    """
    status, got = stream_transport(body)
    if status == 409 and got.get("error") == "session_ended" and body.get("session_id") \
            and not body.get("continuation"):
        # The session ran its course. A new question opens a new one, which
        # is the service's call to make and to charge for.
        _SESSIONS.pop(course, None)
        body = {k: v for k, v in body.items() if k != "session_id"}
        status, got = stream_transport(body)
    if status != 200:
        yield {"type": "_outcome", "error": refusal_text(status, got),
               "own_key": own_key_answers(status, got)}
        return

    escalate = None
    for event in got:
        kind = event.get("type")
        if kind == "session":
            _SESSIONS[course] = str(event.get("id", ""))
        elif kind == "text":
            yield {"type": "text", "text": str(event.get("text", ""))}
        elif kind == "citation":
            yield {"type": "citation", "title": str(event.get("title", ""))}
        elif kind == "escalate":
            escalate = event
        elif kind == "done":
            # The same four keys _usage() keeps on the BYO path, so one
            # assistant.log line reads the same whichever path wrote it.
            for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
                totals[key] = totals.get(key, 0) + int(event.get(key, 0) or 0)
            cost[0] += int(event.get("cost_microusd", 0) or 0)
        elif kind == "error":
            yield {"type": "_outcome", "error": refusal_text(502, {"error": event.get("error", "")})}
            return
    yield {"type": "_outcome", "escalate": escalate}


def ask_managed(question: str, course: str, lectures: list[Lecture], service, started: float):
    """ask(), on the account service's key. Same events, same log line."""
    yield {"type": "status",
           "text": f"Reading {len(lectures)} "
                   f"{'summary' if len(lectures) == 1 else 'summaries'}"}
    try:
        summaries = summary_docs(service, lectures)
    except Exception as exc:
        yield {"type": "error", "text": f"Could not read your notes from Drive: {exc}"}
        return
    if not summaries:
        yield {"type": "error",
               "text": f"The {course} lectures are filed, but their summaries "
                       f"could not be read from Drive."}
        return

    body = {"question": question, "summaries": summaries}
    if _SESSIONS.get(course):
        body["session_id"] = _SESSIONS[course]
    totals: dict = {}
    cost = [0]
    escalated_to: list[str] = []

    outcome: dict = {}
    for event in _round(body, course, totals, cost):
        if event["type"] == "_outcome":
            outcome = event
        else:
            yield event
    if outcome.get("own_key"):
        # Refused before a word was written, so ask() can start over on the
        # Mac's own key. Consumed by ask() and never reaches the panel.
        yield {"type": "_own_key"}
        return
    if outcome.get("error"):
        yield {"type": "error", "text": outcome["error"]}
        return

    escalate = outcome.get("escalate")
    if escalate:
        reason = str(escalate.get("reason", "")).strip()
        yield {"type": "status",
               "text": f"Going to the full transcripts: {reason}" if reason
                       else "Going to the full transcripts"}
        try:
            docs, used = transcript_docs(service, lectures, list(escalate.get("lectures", [])))
        except Exception as exc:
            docs, used = [], []
            log(f"  transcript fetch failed: {exc}")
        escalated_to.extend(used)
        body = {"question": question, "summaries": summaries,
                "session_id": str(escalate.get("session_id", "")),
                "continuation": escalate.get("continuation", []),
                "transcripts": docs}
        outcome = {}
        for event in _round(body, course, totals, cost):
            if event["type"] == "_outcome":
                outcome = event
            else:
                yield event
        if outcome.get("error"):
            yield {"type": "error", "text": outcome["error"]}
            return

    record_session(course, question, bool(escalated_to), escalated_to,
                   {**totals, "cost_microusd": cost[0], "managed": True})
    yield {"type": "done",
           "escalated": bool(escalated_to),
           "escalated_to": escalated_to,
           "seconds": round(time.time() - started, 1),
           "cost_microusd": cost[0],
           **totals}
