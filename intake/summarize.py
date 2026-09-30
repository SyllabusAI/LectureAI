"""Transcript -> summary, key terms, and action items, via Claude.

One API call per recording. The schema the model is held to and the prompt it
is given are the active profile's (schemas.py): a study summary for Syllabus,
call notes for Sous. Everything downstream (filenames, the uploaded .md) is
built from the result plus the schedule lookup.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import anthropic

from intake import config, schemas, tasktext

MAX_SLUG_WORDS = 4

# No temperature is set here and none can be. Sonnet 5 removed the sampling
# parameters: temperature, top_p and top_k are rejected with a 400, and the
# SDK's messages.parse() does not accept them at all. So run-to-run stability
# has to come from the prompt and the schema, which is why the action item
# rules in schemas.py are written to leave no borderline case to a coin flip.
# If a future model brings temperature back, it is still not the fix; an
# instruction the model can read two ways is.


def fallback_slug() -> str:
    """Topic slug when the model gave none: Lecture-Notes, or the profile's."""
    return config.PROFILE.fallback_slug


# Kept for anything that still reads the constant; the pipeline asks the profile.
FALLBACK_SLUG = fallback_slug()


# The lecture schema and prompt, importable from here as they always were.
# summarize() itself asks the active profile, which is how Sous gets its own.
KeyTerm = schemas.KeyTerm
ActionItem = schemas.ActionItem
LectureSummary = schemas.LectureSummary
SYSTEM_PROMPT = schemas.LECTURE_SYSTEM_PROMPT

USER_TEMPLATE = """{label}: {course}
Date: {date}

{kind} transcript:

<transcript>
{transcript}
</transcript>"""


_CLOSING_TAG = re.compile(r"</(\s*)transcript", re.IGNORECASE)


def _fence(text: str) -> str:
    """Break up a literal closing tag so the transcript cannot end its own block early."""
    return _CLOSING_TAG.sub(r"<\\/\1transcript", text)


def user_message(transcript: str, course: str, date: str) -> str:
    """The transcript framed for the model, labeled the way the profile sees it."""
    return USER_TEMPLATE.format(
        label=config.PROFILE.subject_label, course=course, date=date,
        kind=config.PROFILE.filename_prefix.capitalize(),
        transcript=_fence(transcript.strip()),
    )


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _strip_fences(text: str) -> str:
    """Remove ```json ... ``` wrappers the model may add despite instructions."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def slugify_topic(raw: str) -> str:
    """Force a topic into Title-Case-With-Hyphens, at most MAX_SLUG_WORDS."""
    words = re.findall(r"[A-Za-z0-9]+", raw or "")
    if not words:
        return fallback_slug()
    return "-".join(w.capitalize() if not w.isupper() else w
                    for w in words[:MAX_SLUG_WORDS])


def build_filename(course: str, date: str, topic_slug: str) -> str:
    """{COURSE}_{YYYY-MM-DD}_{Topic-Slug} — course comes from the schedule.

    The topic slug was already sanitized and the course was not, even though
    this name becomes two files in processed/ and two more in Drive.
    """
    return f"{config.safe_course(course)}_{date}_{slugify_topic(topic_slug)}"


# The lecture kinds; the pipeline asks the profile's schema for the live set.
ACTION_KINDS = set(schemas.LectureSummary.ACTION_KINDS)


def _clean_date(raw: str) -> str:
    """A real YYYY-MM-DD, or empty. Guards against a plausible-looking date."""
    text = str(raw or "").strip()[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def normalize_actions(raw_items, course: str, date: str) -> list[dict]:
    """Clean up action items and give the undated ones a due date.

    An item the instructor never dated is dated to the next class meeting,
    since that is when a reading or problem set is usually wanted. `date_source`
    records which happened, so an assumption is never presented as something
    the instructor said.

    The task text is cleaned here rather than at each destination, so the
    short form is what gets stored, shown and compared everywhere. The schema
    asks for it plain, but a model that ignores that would otherwise put
    markdown into Notion, which shows it as literal asterisks.
    """
    items: list[dict] = []
    for raw in raw_items or []:
        if isinstance(raw, str):
            entry = {"task": raw, "due_date": "", "kind": "other"}
        elif isinstance(raw, dict):
            entry = raw
        else:  # a pydantic ActionItem
            entry = {
                "task": getattr(raw, "task", ""),
                "detail": getattr(raw, "detail", ""),
                "due_date": getattr(raw, "due_date", ""),
                "kind": getattr(raw, "kind", "other"),
            }

        task = tasktext.clean(str(entry.get("task", "")))
        if not task:
            continue

        # The detail is a sentence, not a checkbox, so it keeps its full stop
        # and its length; it only loses markup Notion would show verbatim.
        detail = tasktext.strip_markdown(str(entry.get("detail", "") or ""))

        kind = str(entry.get("kind", "other")).strip().lower()
        if kind not in config.PROFILE.summary_schema.ACTION_KINDS:
            kind = "other"

        due = _clean_date(entry.get("due_date", ""))
        if due:
            source = "stated"
        else:
            due = config.next_class_meeting(course, date) or ""
            source = "assumed" if due else "none"

        items.append({"task": task, "detail": detail, "due_date": due,
                      "kind": kind, "date_source": source})
    return items


def _save_failed_response(raw: str, course: str, date: str) -> None:
    """Keep the raw response when parsing fails, so it can be diagnosed."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = (config.WORK_DIR /
            f"summarize-failure_{config.safe_course(course)}_{date}_{stamp}.txt")
    try:
        path.write_text(raw)
        log(f"  raw response saved to {path}")
    except OSError:
        pass


def _parse(raw: str) -> dict:
    """Parse the model's JSON, degrading gracefully rather than raising."""
    try:
        data = json.loads(_strip_fences(raw))
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
    except (json.JSONDecodeError, ValueError) as exc:
        log(f"  WARNING: could not parse JSON ({exc}); keeping raw text as summary")
        return {
            "summary_md": raw.strip(),
            "topic_slug": fallback_slug(),
            "key_terms": [],
            "action_items": [],
        }

    terms = []
    for item in data.get("key_terms") or []:
        if isinstance(item, dict) and item.get("term"):
            terms.append({
                "term": str(item["term"]).strip(),
                "definition": str(item.get("definition", "")).strip(),
            })
        elif isinstance(item, str) and item.strip():
            terms.append({"term": item.strip(), "definition": ""})

    return {
        "summary_md": str(data.get("summary_md", "")).strip(),
        "topic_slug": slugify_topic(str(data.get("topic_slug", ""))),
        "key_terms": terms,
        # Left raw here; summarize() normalizes once it knows course and date.
        "action_items": data.get("action_items") or [],
    }


def shape(payload: dict, course: str, date: str) -> dict:
    """The model's answer as the rest of the pipeline wants it.

    Takes a plain dict so the two ways an answer can arrive, the schema-parsed
    object from this Mac's own Anthropic call and the JSON the proxy sends
    back, land in exactly one piece of code. A field the model left out is
    missing, not a crash.
    """
    terms = []
    for term in payload.get("key_terms") or []:
        if not isinstance(term, dict):
            continue
        name = str(term.get("term", "")).strip()
        if name:
            terms.append({"term": name,
                          "definition": str(term.get("definition", "")).strip()})
    return {
        "summary_md": str(payload.get("summary_md", "")).strip(),
        "topic_slug": slugify_topic(str(payload.get("topic_slug", ""))),
        "key_terms": terms,
        "action_items": normalize_actions(payload.get("action_items"), course, date),
    }


# The schema's own field names, as the model closes them when it writes its
# whole answer into the first field instead of filling the other three. No
# lecture summary contains these, so finding one is proof of the failure
# rather than a guess at it.
LEAKED_FIELD_TAGS = ("</summary_md>", "<topic_slug>", "<key_terms>",
                     "<action_items>", "</record_summary>")


def require_written(result: dict) -> dict:
    """A summary with no summary in it is a failed call, not a thin one.

    Three shapes of failure, all of which used to be filed as a lecture:

    An EMPTY summary. A tool_use block whose input carried none of the fields
    (seen once in six runs against a 5,500 word transcript), or a parsed
    object of empty strings.

    A summary that swallowed the other fields. On 2026-09-23 the model wrote
    the notes, the slug, seven key terms and two to-dos into summary_md as
    tag-delimited text and left the other three fields empty. Every check
    passed: the summary was long and real, so it was filed under the fallback
    slug with no terms and an "Action items: none" section, while that
    Friday's reading sat in the body as markup. Nothing said so.

    A summary with NOTHING around it. No slug, no terms, no to-dos together
    mean the model answered the prose and skipped the structure. Any one of
    the three alone is a thin lecture and normal; all three at once is not.

    The recording is still on disk in every case, so failing here costs a
    retry and nothing else. Filing the wrong thing costs a lecture quietly.
    """
    if not result.get("summary_md", "").strip():
        raise RuntimeError(
            "the model returned an empty summary; nothing was written. "
            "The recording is untouched, so this can be run again."
        )
    leaked = next((t for t in LEAKED_FIELD_TAGS if t in result["summary_md"]), "")
    if leaked:
        raise RuntimeError(
            f"the model wrote its other fields into the summary ({leaked} is in "
            f"the text); nothing was written. The recording is untouched, so "
            f"this can be run again."
        )
    if (result.get("topic_slug") == fallback_slug()
            and not result.get("key_terms") and not result.get("action_items")):
        raise RuntimeError(
            "the model returned a summary with no topic, no key terms and no "
            "action items; nothing was written. The recording is untouched, "
            "so this can be run again."
        )
    return result


def _summarize_via_proxy(transcript: str, course: str, date: str) -> dict:
    """Summarize on the service's key instead of one from this Mac's .env.

    The prompt, the schema and the model all live in the proxy; this sends the
    transcript and the two labels that frame it and nothing else. Which
    profile's prompt runs is decided there too, from this device's own row, so
    a Sous Mac gets call notes without asking for them.
    """
    from intake import account, providers

    acct = account.load()
    if not acct or not acct.token:
        raise RuntimeError("this Mac is not signed in to a Syllabus account; "
                           "run: intake login")
    log(f"summarizing {len(transcript.split())} words for {course} on {date} "
        f"via the Syllabus account service")
    status, data = account.call(
        "POST", "/proxy/summarize",
        {"transcript": transcript, "subject": course, "date": date},
        token=acct.token, timeout=account.SLOW_TIMEOUT,
    )
    if status != 200:
        raise providers.ProxyRefused(
            # Written from the whole body, not the error code: this path is
            # where a refusal about tokens used to describe itself as a
            # transcription allowance.
            providers.refusal_reason(data)
            or f"the account service refused to summarize: {data.get('error', status)}",
            error=str(data.get("error", "")), status=status, detail=data,
        )
    payload = data.get("summary")
    if not isinstance(payload, dict):
        raise RuntimeError("the account service sent back no summary")
    result = require_written(shape(payload, course, date))
    log(f"  topic: {result['topic_slug']} | {len(result['key_terms'])} terms | "
        f"{len(result['action_items'])} action items | "
        f"{data.get('tokens', 0)} tokens")
    return result


def summarize(transcript: str, course: str, date: str) -> dict:
    """Summarize a transcript. Returns summary_md, topic_slug, key_terms, action_items."""
    if not transcript.strip():
        raise ValueError("transcript is empty")

    from intake import account

    if account.managed():
        return _summarize_via_proxy(transcript, course, date)

    client = anthropic.Anthropic(api_key=config.require("ANTHROPIC_API_KEY"))
    log(f"summarizing {len(transcript.split())} words for {course} on {date} "
        f"via {config.CLAUDE_MODEL}")

    response = client.messages.parse(
        model=config.CLAUDE_MODEL,
        max_tokens=16000,
        system=config.PROFILE.summary_prompt,
        messages=[{
            "role": "user",
            "content": user_message(transcript, course, date),
        }],
        output_format=config.PROFILE.summary_schema,
    )

    parsed = getattr(response, "parsed_output", None)
    if parsed is not None:
        result = require_written(shape(parsed.model_dump(), course, date))
    else:
        # Shouldn't happen with a schema, but a truncated or refused response
        # still needs to degrade rather than crash.
        log(f"  WARNING: no parsed output (stop_reason={response.stop_reason}); "
            f"falling back to raw text")
        raw = "".join(b.text for b in response.content if b.type == "text")
        if not raw.strip():
            raise RuntimeError(
                f"model returned nothing usable (stop_reason={response.stop_reason})"
            )
        result = _parse(raw)
        result["action_items"] = normalize_actions(
            result["action_items"], course, date
        )
        _save_failed_response(raw, course, date)
    log(f"  topic: {result['topic_slug']} | "
        f"{len(result['key_terms'])} terms | "
        f"{len(result['action_items'])} action items | "
        f"{response.usage.input_tokens} in / {response.usage.output_tokens} out tokens")
    return result


def render_action(item: dict | str) -> str:
    """One action item as a line of text, with its date and how we got it."""
    if isinstance(item, str):
        return item
    task = item.get("task", "").strip()
    due = item.get("due_date", "")
    if not due:
        return task
    if item.get("date_source") == "assumed":
        return f"{task} (due {due}, assumed: next class)"
    return f"{task} (due {due})"


def render_markdown(result: dict, course: str, date: str) -> str:
    """Assemble the .md document that gets uploaded to Drive."""
    lines = [
        f"# {course}: {date}",
        "",
        f"**Topic:** {result['topic_slug'].replace('-', ' ')}",
        "",
        result["summary_md"],
        "",
    ]

    if result["key_terms"]:
        lines += ["## Key terms", ""]
        for t in result["key_terms"]:
            lines.append(f"- **{t['term']}**: {t['definition']}" if t["definition"]
                         else f"- **{t['term']}**")
        lines.append("")

    lines += ["## Action items", ""]
    if result["action_items"]:
        for a in result["action_items"]:
            lines.append(f"- [ ] {render_action(a)}")
            # The context was deliberately kept out of the task so the Notion
            # checkbox stays one line. It has to survive somewhere, and this
            # document is what that checkbox links to.
            detail = (a.get("detail") or "").strip() if isinstance(a, dict) else ""
            if detail:
                lines.append(f"      {detail}")
    else:
        lines.append(f"_None mentioned in this {config.PROFILE.filename_prefix}._")
    lines.append("")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Summarize a transcript. Markdown prints to stdout."
    )
    parser.add_argument("transcript", help="path to a transcript .txt")
    parser.add_argument("course", help="course code, e.g. ACCT-4321")
    parser.add_argument("date", help="lecture date, YYYY-MM-DD")
    parser.add_argument("--json", action="store_true",
                        help="print the raw result dict instead of markdown")
    args = parser.parse_args(argv)

    try:
        text = Path(args.transcript).expanduser().read_text()
        result = summarize(text, args.course, args.date)
        log(f"  filename: {build_filename(args.course, args.date, result['topic_slug'])}")
        print(json.dumps(result, indent=2) if args.json
              else render_markdown(result, args.course, args.date))
    except FileNotFoundError:
        log(f"error: no such transcript: {args.transcript}")
        return 1
    except anthropic.AuthenticationError:
        log("error: ANTHROPIC_API_KEY is invalid")
        return 1
    except anthropic.RateLimitError:
        log("error: rate limited by the Claude API; try again shortly")
        return 1
    except anthropic.APIStatusError as exc:
        log(f"error: Claude API returned {exc.status_code}: {exc.message}")
        return 1
    except Exception as exc:
        log(f"error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
