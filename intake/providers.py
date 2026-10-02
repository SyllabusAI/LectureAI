"""Transcription providers: a model, and the limits that belong to it.

The chunking rules are not properties of audio. They are properties of the
model on the other end of the call:

- `max_bytes` is the API's request cap. OpenAI rejects anything over 25MB;
  Deepgram takes 2GB.
- `max_chunk_seconds` exists because the gpt-4o transcribe models cap their
  OUTPUT near 2000 tokens and truncate silently rather than erroring, so long
  audio has to be split on duration as well as size. Whisper has no such cap,
  and Deepgram has no duration limit worth naming.
- `truncation_word_threshold` is the tell for that silent truncation. A model
  that cannot truncate sets it to None and the check is skipped entirely.

Keeping these next to the model means swapping the model swaps its limits with
it. Before this module they lived in config.py as bare constants, and setting
TRANSCRIBE_MODEL = "whisper-1" left CHUNK_SECONDS at 8 minutes: two and a half
times the API calls that model needs, at no benefit.

The config constants are still the source of the default provider's values, so
a value changed there still changes what the default provider does.

Adding a provider: subclass nothing, implement the protocol, register it in
PROVIDERS. transcribe.py asks the provider for its limits and never reads a
config constant.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from intake import config

# A provider with no meaningful duration limit still needs a number, because
# the split decision is a comparison. A day of audio is not a real lecture.
NO_DURATION_LIMIT = 24 * 60 * 60


@runtime_checkable
class TranscriptionProvider(Protocol):
    """One transcription backend and the limits transcribe.py must respect."""

    #: How this provider is named in logs and in TRANSCRIBE_MODEL.
    name: str
    #: Hard request cap. A chunk over this is an error, not a retry.
    max_bytes: int
    #: Re-encode to mono 64kbps before uploading anything bigger than this.
    compress_threshold_bytes: int
    #: Split audio longer than this, whatever it weighs.
    max_chunk_seconds: int
    #: A chunk at or above this word count probably got cut off. None when
    #: the model has no output cap and so cannot truncate.
    truncation_word_threshold: int | None
    #: Whether the provider labels speakers natively. Nothing reads this yet;
    #: it is what a diarization roadmap item would turn on.
    diarization: bool
    #: The .env setting holding this provider's key, so a preflight check can
    #: ask for the right one rather than always asking for OpenAI's. Empty
    #: when the provider brings its own credential and needs nothing from
    #: .env, which is how the managed proxy works.
    api_key_setting: str

    def transcribe_file(self, path: Path, prompt: str = "") -> str:
        """One API call. `prompt` is optional context; providers may ignore it."""


def _too_big(provider: TranscriptionProvider, path: Path, size: int) -> RuntimeError:
    def mb(n: int) -> str:
        return f"{n / 1024 / 1024:.1f}MB"

    return RuntimeError(
        f"{path.name} is {mb(size)}, over {provider.name}'s {mb(provider.max_bytes)} "
        f"API limit even after compression and splitting"
    )


class OpenAIProvider:
    """Any endpoint shaped like OpenAI's /v1/audio/transcriptions.

    That covers OpenAI itself and the OpenAI-compatible hosts, Groq among
    them, which differ only in base URL, key, and limits.

    The defaults are the config constants, so OpenAIProvider() with no
    arguments is exactly what this program did before providers existed.
    """

    def __init__(
        self,
        model: str = "",
        *,
        name: str = "",
        base_url: str | None = None,
        api_key_setting: str = "OPENAI_API_KEY",
        max_bytes: int | None = None,
        compress_threshold_bytes: int | None = None,
        max_chunk_seconds: int | None = None,
        truncation_word_threshold: int | None = config.TRUNCATION_WORD_THRESHOLD,
        diarization: bool = False,
    ) -> None:
        self.model = model or config.TRANSCRIBE_MODEL
        self.name = name or self.model
        self.base_url = base_url
        self.api_key_setting = api_key_setting
        self.max_bytes = config.WHISPER_LIMIT_BYTES if max_bytes is None else max_bytes
        self.compress_threshold_bytes = (
            config.COMPRESS_THRESHOLD_BYTES
            if compress_threshold_bytes is None
            else compress_threshold_bytes
        )
        self.max_chunk_seconds = (
            config.CHUNK_SECONDS if max_chunk_seconds is None else max_chunk_seconds
        )
        self.truncation_word_threshold = truncation_word_threshold
        self.diarization = diarization
        self._cached_client = None

    def client(self):
        """The SDK client, built on first use so importing costs no key."""
        if self._cached_client is None:
            from openai import OpenAI

            kwargs = {"api_key": config.require(self.api_key_setting)}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self._cached_client = OpenAI(**kwargs)
        return self._cached_client

    def transcribe_file(self, path: Path, prompt: str = "") -> str:
        size = path.stat().st_size
        if size > self.max_bytes:
            raise _too_big(self, path, size)
        with path.open("rb") as fh:
            kwargs = {"model": self.model, "file": fh, "response_format": "text"}
            if prompt:
                kwargs["prompt"] = prompt
            result = self.client().audio.transcriptions.create(**kwargs)
        # response_format="text" gives a plain string; object form has .text
        return (result if isinstance(result, str) else result.text).strip()


class DeepgramProvider:
    """Deepgram's prerecorded REST endpoint.

    The reason a second provider exists in this slice: nothing about Deepgram
    is OpenAI-shaped. No SDK, a different auth header, JSON out rather than
    text, 2GB rather than 25MB, and no output cap at all, so a 39 minute
    lecture is one request instead of five. If transcribe.py still has an
    OpenAI-specific assumption in it, this class is what finds it.

    Speakers are labeled natively here (`diarize=true`), which is why this is
    the provider a later diarization item would build on.
    """

    URL = "https://api.deepgram.com/v1/listen"

    def __init__(
        self,
        model: str = "nova-3",
        *,
        name: str = "",
        api_key_setting: str = "DEEPGRAM_API_KEY",
        timeout: int = 600,
    ) -> None:
        self.model = model
        self.name = name or f"deepgram/{model}"
        self.api_key_setting = api_key_setting
        self.timeout = timeout
        # Deepgram documents 2GB for a prerecorded request. Nothing a lecture
        # produces comes near it, so the split path never runs here.
        self.max_bytes = 2 * 1024 * 1024 * 1024
        # Not a limit, a courtesy: past 100MB the upload is the slow part, and
        # mono 64kbps loses nothing a speech model uses.
        self.compress_threshold_bytes = 100 * 1024 * 1024
        self.max_chunk_seconds = NO_DURATION_LIMIT
        # Deepgram streams its output; there is no token cap to truncate at.
        self.truncation_word_threshold = None
        self.diarization = True

    def transcribe_file(self, path: Path, prompt: str = "") -> str:
        import requests

        size = path.stat().st_size
        if size > self.max_bytes:
            raise _too_big(self, path, size)
        # `prompt` has no equivalent here. Deepgram's keyterm boosting takes a
        # word list, not prose, and transcribe.py sends no prompt anyway.
        response = requests.post(
            self.URL,
            params={"model": self.model, "smart_format": "true", "punctuate": "true"},
            headers={
                "Authorization": f"Token {config.require(self.api_key_setting)}",
                "Content-Type": "audio/m4a",
            },
            data=path.read_bytes(),
            timeout=self.timeout,
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"{self.name} returned {response.status_code}: "
                f"{response.text.strip()[:300]}"
            )
        payload = response.json()
        try:
            alternatives = payload["results"]["channels"][0]["alternatives"]
        except (KeyError, IndexError, TypeError):
            raise RuntimeError(f"{self.name} returned no transcript: {payload!r}"[:300])
        return (alternatives[0].get("transcript", "") if alternatives else "").strip()


class ProxyRefused(RuntimeError):
    """The managed proxy declined, in its own words rather than a provider's.

    `error` is the machine-readable reason ("allowance_exhausted",
    "rate_limited", "provider_unavailable", ...) and `detail` is the whole
    payload, so the panel can say how much of the month is left rather than
    just that something failed.
    """

    def __init__(self, message: str, error: str = "", status: int = 0,
                 detail: dict | None = None) -> None:
        super().__init__(message)
        self.error = error
        self.status = status
        self.detail = detail or {}


def _post_audio(url: str, token: str, path: Path, seconds: float, timeout: int):
    """One multipart POST to the proxy. Replaced wholesale by the tests."""
    import requests

    with path.open("rb") as fh:
        return requests.post(
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            files={"audio": (path.name, fh, "audio/mp4")},
            data={"duration_seconds": f"{seconds:.3f}"},
            timeout=timeout,
        )


class ProxyProvider:
    """Transcription through the Syllabus account service, on managed keys.

    The point of this one is that no key lives on the Mac. The device token
    from `intake login` is the whole credential, the account service holds the
    provider key, and it meters what the account has spent before it spends
    any more (see syllabus-accounts/src/proxy.ts).

    The limits below are the proxy's, not any model's, and they are the one
    place these two repos have to agree. They are deliberately the SMALLER of
    what the proxy accepts and what its upstream model needs, so a proxy that
    later swaps its upstream cannot make this Mac send chunks the new model
    would truncate. Raising them belongs with a proxy that advertises its own
    limits; until it does, they are pinned here and asserted in the tests.
    """

    #: syllabus-accounts: MAX_AUDIO_BYTES, the per-chunk cap the proxy refuses above.
    MAX_AUDIO_BYTES = 12 * 1024 * 1024
    #: syllabus-accounts: MAX_CHUNK_SECONDS, the longest chunk it will meter.
    MAX_CHUNK_SECONDS = 1800

    def __init__(self, *, timeout: int = 600) -> None:
        self.name = "syllabus"
        self.timeout = timeout
        # No .env key at all: the device token is the credential.
        self.api_key_setting = ""
        self.max_bytes = self.MAX_AUDIO_BYTES
        # Comfortably under the cap, so a chunk that encodes fatter than
        # expected is compressed here rather than refused there.
        self.compress_threshold_bytes = 10 * 1024 * 1024
        # The proxy would meter a 30 minute chunk, but it transcribes on Groq
        # with OpenAI behind it, and gpt-4o-mini-transcribe truncates past
        # about 8 minutes. Any chunk may end up on the fallback leg without
        # this Mac being told, so the binding limit is the one that holds for
        # EITHER upstream, not the one Groq alone would allow.
        self.max_chunk_seconds = config.CHUNK_SECONDS
        self.truncation_word_threshold = config.TRUNCATION_WORD_THRESHOLD
        self.diarization = False

    def transcribe_file(self, path: Path, prompt: str = "") -> str:
        # Imported here rather than at module scope: transcribe.py imports
        # this module, so the dependency only runs one way at import time.
        from intake import account
        from intake.transcribe import duration_seconds

        size = path.stat().st_size
        if size > self.max_bytes:
            raise _too_big(self, path, size)

        acct = account.load()
        if not acct or not acct.token:
            raise ProxyRefused(
                "this Mac is not signed in to a Syllabus account; run: intake login",
                error="not_signed_in",
            )
        seconds = duration_seconds(path)
        if not seconds or seconds <= 0:
            raise ProxyRefused(
                f"could not read how long {path.name} is, so it cannot be metered",
                error="unreadable_audio",
            )
        # `prompt` is deliberately dropped: the proxy fixes the upstream call
        # and sends no prompt, for the reason in _transcribe_chunk.
        response = _post_audio(
            f"{account.url()}/proxy/transcribe", acct.token, path, seconds, self.timeout
        )
        if response.status_code != 200:
            raise _proxy_refusal(response)
        try:
            return str(response.json().get("text", "")).strip()
        except ValueError:
            raise ProxyRefused("the account service sent back something unreadable",
                               error="bad_response", status=response.status_code)


#: What each proxy refusal means to somebody reading a log or a panel.
#: `allowance_exhausted` is deliberately absent: it needs the refusal body to
#: say anything true, and refusal_reason below is what writes it.
PROXY_REASONS = {
    "not_a_device": "this Mac's sign-in was not accepted; sign in again",
    "rate_limited": "too many requests at once; this will retry",
    "provider_busy": "the transcription provider is busy; this will retry",
    "provider_unavailable": "the transcription provider could not be reached",
    # The service's own monthly cap, shared by every account (GLOBAL_CEILING in
    # syllabus-accounts). Nothing this account did, and nothing it can do.
    "service_ceiling": "Syllabus has paused processing for everyone until its "
                       "monthly limit is raised; the recording is kept in the "
                       "inbox and runs once it is",
    # The two refusals a `quality=high` second pass can get from the service
    # (syllabus-accounts F-21). Either way the first transcript stands.
    "quality_unavailable": "the clearer second pass is not switched on yet; "
                           "the first transcript is kept",
    "first_pass_required": "the clearer second pass needs this part's first pass "
                           "from the last day; the first transcript is kept",
    "too_large": "the chunk is bigger than the service accepts",
    "too_long": "the chunk is longer than the service accepts",
    # Both mean the model answered and the answer was not usable, which is
    # worth retrying: the same transcript has come back right on the next run.
    "no_summary": "the model returned an empty summary; this can be run again",
    "malformed_summary": "the model returned a summary it had packed its other "
                         "fields into; this can be run again",
}


def refusal_reason(payload: dict) -> str:
    """What a refusal means, said in the units it is actually about.

    The service meters two things and refuses them with one error code:
    transcription in audio seconds, summaries in tokens. The message used to
    be looked up from that code alone, so a summary that ran out of tokens
    told its owner the transcription allowance was gone, and sent them
    looking at the wrong meter. The numbers in the body were read on the
    transcribe path only, where dividing by 3600 is right; the same arithmetic
    on a token count would have reported 123,427 tokens as 34.3 hours.

    The body carries `kind`, so read it and say which allowance ran out.
    """
    error = str(payload.get("error", ""))
    if error != "allowance_exhausted":
        return PROXY_REASONS.get(error, "")
    used, allowed = payload.get("used"), payload.get("allowance")
    known = isinstance(used, (int, float)) and isinstance(allowed, (int, float))
    if str(payload.get("kind", "")) == "summarize":
        if known:
            return (f"this account has used {used:,.0f} of its {allowed:,.0f} "
                    f"summary tokens for the month")
        return "this account has used its summary allowance for the month"
    if known:
        return (f"this account has used {used / 3600:.1f} of its "
                f"{allowed / 3600:.1f} transcription hours for the month")
    return "this account has used its transcription allowance for the month"


def _proxy_refusal(response) -> ProxyRefused:
    """A proxy error response as an exception that says what happened."""
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    error = str(payload.get("error", "")) or f"http_{response.status_code}"
    return ProxyRefused(refusal_reason(payload) or f"the account service refused: {error}",
                        error=error, status=response.status_code, detail=payload)


# Every provider this program knows how to reach, keyed by what you would put
# in TRANSCRIBE_MODEL. The values are factories: building one asks config for a
# key, and importing this module must not.
PROVIDERS: dict[str, callable] = {
    # The default. Cheap and accurate, and the only one here that truncates.
    "gpt-4o-mini-transcribe": lambda: OpenAIProvider("gpt-4o-mini-transcribe"),
    "gpt-4o-transcribe": lambda: OpenAIProvider("gpt-4o-transcribe"),
    # Whisper has no output cap, so it splits on size alone in practice: a
    # 20 minute chunk is a request-size choice, not a truncation guard.
    "whisper-1": lambda: OpenAIProvider(
        "whisper-1",
        max_chunk_seconds=20 * 60,
        truncation_word_threshold=None,
    ),
    # Whisper again, hosted by Groq: same model, different endpoint and key.
    # 100MB is Groq's developer-tier request cap; the free tier is 25MB.
    "groq/whisper-large-v3": lambda: OpenAIProvider(
        "whisper-large-v3",
        name="groq/whisper-large-v3",
        base_url="https://api.groq.com/openai/v1",
        api_key_setting="GROQ_API_KEY",
        max_bytes=100 * 1024 * 1024,
        compress_threshold_bytes=99 * 1024 * 1024,
        max_chunk_seconds=20 * 60,
        truncation_word_threshold=None,
    ),
    # Turbo: 2.8x cheaper again ($0.04 an hour against $0.111), at 12% WER
    # against large-v3's 10.3% on Groq's own figures. Registered so a real
    # lecture can settle whether that trade is worth taking; the managed
    # proxy runs large-v3 until it is.
    "groq/whisper-large-v3-turbo": lambda: OpenAIProvider(
        "whisper-large-v3-turbo",
        name="groq/whisper-large-v3-turbo",
        base_url="https://api.groq.com/openai/v1",
        api_key_setting="GROQ_API_KEY",
        max_bytes=100 * 1024 * 1024,
        compress_threshold_bytes=99 * 1024 * 1024,
        max_chunk_seconds=20 * 60,
        truncation_word_threshold=None,
    ),
    "deepgram/nova-3": lambda: DeepgramProvider("nova-3"),
    "deepgram/nova-2": lambda: DeepgramProvider("nova-2"),
}


def get(name: str | None = None) -> TranscriptionProvider:
    """The provider to transcribe with.

    A Mac signed in to a Syllabus account uses the proxy, whatever
    TRANSCRIBE_MODEL says: the model is the service's choice there, not this
    machine's, and no key exists here to call anything else with. Naming a
    provider explicitly (transcribe.py --model) still wins, so a managed Mac
    can still be pointed at a model for a one-off comparison.

    Otherwise it is TRANSCRIBE_MODEL, and an unregistered name is assumed to
    be an OpenAI model, which is what that setting meant before this module.

    There is deliberately no fall back from the proxy to a local key. A Mac
    that is signed in should never quietly start spending its owner's own
    OpenAI credit because the service was unreachable for a minute.
    """
    if name is None:
        from intake import account

        if account.managed():
            return ProxyProvider()
    name = name or config.TRANSCRIBE_MODEL
    factory = PROVIDERS.get(name)
    return factory() if factory else OpenAIProvider(name)
