"""musicgen: make music in the background, on any vendor a key is held for.

A directory plugin written against `ultron.sdk` and nothing else. It brings two
tools, one vendor of its own, and a point any other plugin can put a vendor
into:

- `GoogleMusic` - Lyria through the Gemini API's Interactions endpoint, one
  request that answers with the track.
- `musicgen.backend` - every other vendor, registered by the plugin that owns
  it with `ctx.register_extension("musicgen.backend", name, build)` (SDK 1.39).
  The `openrouter` provider plugin does; the interface is in `PLUGIN.md`, and
  nothing here names it.

Google stays here for videogen's reason: its plugin ships inside Ultron, which
does not know musicgen exists. Whichever vendor it is, `Vendor` is how this
module sees it, so a backend's object is touched in one place.

videogen's shape with imagegen's vendor: every music API answers in one request,
but that request takes a minute or two, so `music_generate` starts a job and
returns. The job belongs to the session, not to the call that started it - like
a backgrounded `exec` - so the task runs in a context of its own, where an abort
of the turn that started it cannot reach. When it ends the agent is woken
(`ctx.wake`, SDK 1.40) to tell the person, and where that cannot happen a
`before_prompt` hook tells the model on the session's next turn. There is
nothing at a vendor to pick back up, so a session that ends mid-track ends the
job, and the next session says so.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import contextvars
import hashlib
import inspect
import json
import os
import re
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from ultron.sdk.hook import Hook, HookOutcome, HookReturn, PromptEvent
from ultron.sdk.plugin_entry import Plugin, PluginContext
from ultron.sdk.runtime import ToolError, assert_active
from ultron.sdk.tool_plugin import Tool, ToolResult, validate_arguments

MAX_IMAGES = 10
"""OpenClaw's: reference pictures one call may hand in."""
FORMATS = ("mp3", "wav")
MAX_IMAGE_BYTES = 20 * 1024 * 1024
"""A picture is inlined as base64, and every vendor here caps a request well
below what a larger one would make of it."""
MAX_LYRICS = 5000
AUDIO_MAX_BYTES = 64 * 1024 * 1024
"""What one track may weigh. Two minutes of 44.1 kHz stereo WAV is about 21 MB."""
REPLY_MAX_BYTES = 96 * 1024 * 1024
"""A reply carries the track as base64, a third bigger than the track."""
LYRICS_SHOWN = 4000
"""What `music_generate status` shows of a vendor's lyrics."""
KEEP = 200
"""Finished jobs kept in the file; the oldest past this are dropped."""
DUPLICATE_SECONDS = 120
"""How long a finished track answers the same request again, as OpenClaw's
music tool does: long enough to catch a model asking twice, short enough that
asking again later is a new take."""
POINT = "musicgen.backend"
"""Where another plugin puts a vendor (`ctx.register_extension`, SDK 1.39)."""
GOOGLE_HOST = "generativelanguage.googleapis.com"

IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp")
EXTENSIONS = {
    "audio/mpeg": "mp3",
    "audio/wav": "wav",
    "audio/flac": "flac",
    "audio/ogg": "ogg",
    "audio/mp4": "m4a",
}
SUFFIXES = tuple(f".{ext}" for ext in EXTENSIONS.values())
CODE = re.compile(r"[^A-Za-z0-9_.-]+")
MODEL_ARGUMENT = (
    "Which vendor and model to ask first, as provider/model - {example} - or a provider "
    "alone for the model the person configured there. The providers here: {vendors}; "
    "`action: list` shows each one's model. Leave it out for the person's choice. If it "
    "fails or cannot do what is asked, the others are tried on their own models and the "
    "result says so."
)
LISTED_MODELS = 20
"""The most extra model ids one vendor's line names."""
VENDOR_NAME = re.compile(r"[a-z0-9_-]{1,64}")
"""What `provider` may say: an extension's name is lower case, letters,
digits, `_` and `-`."""
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}")
"""What `model` may say. The model chose it and it goes into a vendor's URL
path or request - so nothing that steps out of a path
segment or starts a query: no `..`, `//`, `?`, `#`, `%` or space."""


def sniff_image(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def sniff_audio(data: bytes) -> str:
    """The audio type the bytes are, or empty. Believed over any declaration."""
    if data.startswith(b"ID3") or (len(data) > 1 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0):
        return "audio/mpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    if data.startswith(b"fLaC"):
        return "audio/flac"
    if data.startswith(b"OggS"):
        return "audio/ogg"
    if data[4:8] == b"ftyp":
        return "audio/mp4"
    return ""


def _code(value: Any) -> str:
    """A vendor's error code or status as an identifier - never its prose."""
    return CODE.sub("_", str(value or "")).strip("_")[:60]


# -- what crosses to a vendor ----------------------------------------------------


class Picture:
    """A workspace picture handed to a vendor to set the mood.

    Plain classes rather than dataclasses throughout, for imagegen's reason: an
    install at SDK 1.38 from before Starifter/ultron#6 imports a directory
    plugin without registering it, and `@dataclass` under `from __future__
    import annotations` fails there."""

    __slots__ = ("data", "media_type")

    def __init__(self, data: bytes, media_type: str) -> None:
        self.data = data
        self.media_type = media_type

    def b64(self) -> str:
        return base64.b64encode(self.data).decode("ascii")


class Request:
    """What one vendor is asked for, after its capabilities had their say.

    `seconds` is what a backend written for musicgen 2.x read."""

    __slots__ = (
        "duration_seconds",
        "format",
        "images",
        "instrumental",
        "lyrics",
        "prompt",
        "timeout",
    )

    def __init__(
        self,
        prompt: str,
        lyrics: str = "",
        instrumental: bool | None = None,
        duration_seconds: int = 0,
        images: tuple[Picture, ...] = (),
        timeout: float = 300.0,
        format: str = "",
    ) -> None:
        self.prompt = prompt
        self.lyrics = lyrics
        self.instrumental = instrumental
        self.duration_seconds = duration_seconds
        self.images = images
        self.timeout = timeout
        self.format = format

    @property
    def seconds(self) -> int:
        return self.duration_seconds

    @property
    def described(self) -> str:
        """The prompt with every other ask written into it, for a vendor - Lyria -
        whose only control is the words. OpenClaw's `buildMusicPrompt`, with a
        length for a vendor that takes one."""
        parts = [self.prompt.rstrip()]
        if self.duration_seconds:
            parts.append(f"Length: about {_length(self.duration_seconds)}.")
        if self.instrumental:
            parts.append("Instrumental only. No vocals, no sung lyrics, no spoken word.")
        if self.lyrics:
            parts.append(f"Lyrics:\n{self.lyrics.strip()}")
        return "\n\n".join(parts)

    def fingerprint(self) -> str:
        """What makes two requests the same track: everything asked, the
        pictures by their bytes. Not where it is saved - the same music to
        another file is still the same music, paid for twice."""
        digest = hashlib.sha256()
        for part in (
            self.prompt,
            self.lyrics,
            str(self.instrumental),
            str(self.duration_seconds),
            self.format,
        ):
            digest.update(part.encode("utf-8") + b"\x00")
        for image in self.images:
            digest.update(hashlib.sha256(image.data).digest())
        return digest.hexdigest()


def music_capabilities(vendor: Any) -> dict[str, Any]:
    """A vendor's `capabilities`, OpenClaw's shape, read defensively. One
    written before 3.0 declares none and is read as what it did: lyrics, an
    instrumental and a length written into its prompt, pictures up to ten, and
    no format - so a format is dropped and reported rather than sent to code
    that would not read it."""
    try:
        said = getattr(vendor, "capabilities", None)
        if isinstance(said, Mapping):
            return dict(said)
    except Exception:
        pass
    mode = {"supports_lyrics": True, "supports_instrumental": True, "supports_duration": True}
    return {
        "generate": mode,
        "edit": {"enabled": True, "max_input_images": MAX_IMAGES, **mode},
    }


def music_failure(caps: Mapping[str, Any], images: int) -> str:
    """OpenClaw's `resolveReferenceImageCapabilityError`: pictures handed in
    must be taken, or the vendor is passed over."""
    if not images:
        return ""
    edit = caps.get("edit") or {}
    if not edit.get("enabled"):
        return "takes no pictures"
    most = int(edit.get("max_input_images") or 0)
    if images > most:
        return f"takes up to {most} picture{'s' if most != 1 else ''}"
    return ""


def resolve_music_overrides(
    caps: Mapping[str, Any],
    *,
    images: int,
    lyrics: str,
    instrumental: bool | None,
    duration_seconds: int,
    format: str,
) -> tuple[str, bool | None, int, str, list[str]]:
    """OpenClaw's `resolveMusicGenerationOverrides`: lyrics, an instrumental, a
    length or a format the vendor does not take is dropped; a length past its
    longest is shortened to it. Returns what to send and the result's notes."""
    mode = caps.get("edit" if images else "generate")
    notes: list[str] = []
    if not isinstance(mode, Mapping):
        return lyrics, instrumental, duration_seconds, format, notes
    ignored: list[str] = []
    if lyrics and not mode.get("supports_lyrics"):
        ignored.append("lyrics")
        lyrics = ""
    if instrumental is not None and not mode.get("supports_instrumental"):
        ignored.append(f"instrumental={str(instrumental).lower()}")
        instrumental = None
    if duration_seconds and not mode.get("supports_duration"):
        ignored.append(f"durationSeconds={duration_seconds}")
        duration_seconds = 0
    elif duration_seconds:
        most = mode.get("max_duration_seconds")
        if isinstance(most, int | float) and most > 0 and duration_seconds > most:
            notes.append(f"durationSeconds {duration_seconds} was made as {round(most)}.")
            duration_seconds = max(1, round(most))
    if format:
        offered = mode.get("supported_formats") or ()
        # An empty list means the vendor checks the format itself.
        if not mode.get("supports_format") or (offered and format not in offered):
            ignored.append(f"format={format}")
            format = ""
    if ignored:
        notes.append(f"Ignored, not supported: {', '.join(ignored)}.")
    return lyrics, instrumental, duration_seconds, format, notes


class Made:
    """A track as musicgen reads one, from a built-in or a backend."""

    __slots__ = ("cost", "data", "lyrics", "model")

    def __init__(self, data: bytes, model: str = "", cost: str = "", lyrics: str = "") -> None:
        self.data = data
        self.model = model
        self.cost = cost
        self.lyrics = lyrics


class Vendor:
    """A vendor as musicgen asks one - a built-in, or another plugin's
    backend - under the name it was found by.

    A backend is code this plugin did not write, so everything it is asked goes
    through here: a missing method is a vendor that cannot, one that raises is
    one that failed, and what `generate` returns is read into musicgen's own
    `Made`."""

    __slots__ = ("impl", "name", "why")

    def __init__(self, name: str, impl: Any, why: str = "") -> None:
        self.name = name
        self.impl = impl
        self.why = why

    @classmethod
    def built(cls, name: str, builder: Callable[..., Any], model: str = "") -> Vendor:
        """`name`'s vendor, on `model` when one is named. A builder written
        before the model could choose takes no arguments, and building it would
        be the configured model under the name of the one asked for - so it is
        a vendor that cannot, and the next is asked."""
        if model and not _takes_model(builder):
            return cls(name, None, f"its plugin cannot be asked for {model}; update it")
        try:
            return cls(name, builder(model=model) if model else builder())
        except Exception as exc:  # another plugin's code fails as a vendor fails
            return cls(name, None, f"could not be built: {_said(exc)}")

    @property
    def model(self) -> str:
        return str(getattr(self.impl, "model", "") or "")

    def made_by(self, model: str = "") -> str:
        model = model or self.model
        return self.name + (f" ({model})" if model else "")

    def ready(self) -> str:
        if self.why:
            return self.why
        return self._ask("ready")

    def cannot(self, request: Request) -> str:
        return self._ask("cannot", request)

    async def generate(self, request: Request) -> Made:
        said = await self.impl.generate(request)
        data = getattr(said, "data", None)
        if not isinstance(data, bytes | bytearray) or not data:
            raise RuntimeError(f"{self.name} sent no audio")
        if len(data) > AUDIO_MAX_BYTES:
            raise RuntimeError(f"the track is over {AUDIO_MAX_BYTES // (1024 * 1024)} MB")
        return Made(
            bytes(data),
            model=str(getattr(said, "model", "") or ""),
            cost=str(getattr(said, "cost", "") or ""),
            lyrics=str(getattr(said, "lyrics", "") or ""),
        )

    def _ask(self, method: str, *arguments: Any) -> str:
        found = getattr(self.impl, method, None)
        if found is None:
            return ""
        try:
            return str(found(*arguments) or "")
        except Exception as exc:
            return f"{method}() failed: {type(exc).__name__}"


def _json(raw: bytes) -> Mapping[str, Any]:
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return decoded if isinstance(decoded, Mapping) else {}


def _failure(parsed: Mapping[str, Any], status: int, where: str) -> str:
    """The status and the vendor's error code - identifiers, never its prose,
    which would reach the model."""
    error = parsed.get("error")
    said: list[str] = []
    if isinstance(error, Mapping):
        said = [_code(error.get(key)) for key in ("status", "code", "type")]
    named = ", ".join(dict.fromkeys(part for part in said if part))
    return f"HTTP {status} from {where}" + (f" ({named})" if named else "")


# -- Google ----------------------------------------------------------------------

GOOGLE_URL = "https://generativelanguage.googleapis.com/v1beta"


class GoogleMusic:
    """Lyria through the Interactions API: `POST /interactions`, answered with
    the track as base64 and whatever Lyria wrote - the lyrics, the structure -
    as text beside it."""

    host = GOOGLE_HOST
    models = ("lyria-3-clip-preview", "lyria-3-pro-preview")

    def __init__(
        self, *, model: str = "", api_key: str | None = None, auth_token: str | None = None
    ) -> None:
        self.model = (model or "lyria-3.5").strip().removeprefix("models/")
        if auth_token and auth_token.startswith("AIza"):
            api_key, auth_token = auth_token, None
        self._key = api_key or ""
        self._signed_in = bool(auth_token) and not api_key

    def ready(self) -> str:
        if self._signed_in:
            return "a Google sign-in cannot make music; add an AI Studio key"
        return "" if self._key else "no google key (ultron auth add google)"

    @property
    def capabilities(self) -> dict[str, Any]:
        """OpenClaw's Google music provider: lyrics and an instrumental in the
        words, no length, MP3 from a clip model and MP3 or WAV from the pro
        one; a model OpenClaw does not list checks its format itself."""
        formats = {
            "lyria-3-clip-preview": ("mp3",),
            "lyria-3-pro-preview": ("mp3", "wav"),
        }.get(self.model, ())
        mode = {
            "max_tracks": 1,
            "supports_lyrics": True,
            "supports_instrumental": True,
            "supports_format": True,
            "supported_formats": formats,
        }
        return {"generate": mode, "edit": {"enabled": True, "max_input_images": MAX_IMAGES, **mode}}

    async def generate(self, request: Request) -> Made:
        from ultron.sdk.web import WebError, post

        described = request.described
        content: Any = described
        if request.images:
            content = [{"type": "text", "text": described}] + [
                {"type": "image", "mime_type": image.media_type, "data": image.b64()}
                for image in request.images
            ]
        try:
            response = await post(
                f"{GOOGLE_URL}/interactions",
                json={"model": self.model, "input": content},
                headers={"x-goog-api-key": self._key},
                timeout=request.timeout,
                max_bytes=REPLY_MAX_BYTES,
                user_agent="ultron-musicgen",
            )
        except WebError as exc:
            raise RuntimeError(f"Google unreachable: {type(exc).__name__}") from None
        parsed = _json(response.body)
        if response.status >= 400:
            raise RuntimeError(_failure(parsed, response.status, "Google"))
        if response.truncated:
            raise RuntimeError(f"the reply is over {REPLY_MAX_BYTES // (1024 * 1024)} MB")
        audio, text = _interaction(parsed)
        if not audio:
            status = _code(parsed.get("status"))
            raise RuntimeError("Google sent no audio" + (f" ({status})" if status else ""))
        return Made(audio, model=self.model, lyrics=text)


def _interaction(parsed: Mapping[str, Any]) -> tuple[bytes, str]:
    """The audio and the text of a finished interaction. Read from `steps`, and
    from `outputs`, the shape the Interactions API answered in before it."""
    blocks: list[Mapping[str, Any]] = []
    for step in parsed.get("steps") or ():
        if isinstance(step, Mapping) and step.get("type") in (None, "model_output"):
            blocks += [b for b in step.get("content") or () if isinstance(b, Mapping)]
    blocks += [b for b in parsed.get("outputs") or () if isinstance(b, Mapping)]
    audio = b""
    text: list[str] = []
    for block in blocks:
        if block.get("type") == "audio" and not audio and block.get("data"):
            with contextlib.suppress(ValueError, TypeError):
                audio = base64.b64decode(str(block["data"]), validate=False)
        elif block.get("type") == "text" and block.get("text"):
            text.append(str(block["text"]))
    return audio, "\n\n".join(text).strip()


# -- jobs ------------------------------------------------------------------------


FIELDS = (
    "id",
    "vendor",
    "model",
    "target",
    "session",
    "state",
    "created",
    "finished",
    "error",
    "saved",
    "bytes",
    "media_type",
    "cost",
    "notified",
    "notes",
)


class Job:
    """One track, from the start to a file or a reason. What is kept on disk:
    nothing here is a credential, and neither the prompt nor the lyrics are
    here - the prompt is in the tool call's own record, and the lyrics are a
    vendor's words, held in memory only."""

    __slots__ = FIELDS

    def __init__(self, **values: Any) -> None:
        self.id = str(values.get("id") or "")
        self.vendor = str(values.get("vendor") or "")
        """The vendor asked now, or the one that made it."""
        self.model = str(values.get("model") or "")
        self.target = str(values.get("target") or "")
        """Where the track is to go, relative to the workspace."""
        self.session = str(values.get("session") or "")
        self.state = str(values.get("state") or "running")
        self.created = float(values.get("created") or 0.0)
        self.finished = float(values.get("finished") or 0.0)
        self.error = str(values.get("error") or "")
        self.saved = str(values.get("saved") or "")
        """Where it went - the target, or beside it when something took that name."""
        self.bytes = int(values.get("bytes") or 0)
        self.media_type = str(values.get("media_type") or "")
        self.cost = str(values.get("cost") or "")
        self.notified = bool(values.get("notified"))
        self.notes = str(values.get("notes") or "")
        """What the vendor was sent instead of what was asked - Ultron's words."""

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in FIELDS}

    def made_by(self) -> str:
        return self.vendor + (f" ({self.model})" if self.model else "")


class JobFile:
    """`.ultron/musicgen/jobs.json`: every job this workspace's sessions started.

    Read before every write and replaced whole, so two sessions in the same
    workspace each keep the other's jobs."""

    def __init__(self, workspace: Path) -> None:
        self.path = workspace / ".ultron" / "musicgen" / "jobs.json"

    def load(self) -> dict[str, Job]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        rows = raw.get("jobs") if isinstance(raw, Mapping) else None
        jobs: dict[str, Job] = {}
        for row in rows if isinstance(rows, list) else ():
            if isinstance(row, Mapping) and row.get("id"):
                with contextlib.suppress(TypeError, ValueError):
                    job = Job(**row)
                    jobs[job.id] = job
        return jobs

    def save(self, job: Job) -> None:
        jobs = self.load()
        jobs[job.id] = job
        finished = [j for j in jobs.values() if j.state != "running"]
        finished.sort(key=lambda j: j.finished or j.created)
        for old in finished[: max(0, len(finished) - KEEP)]:
            jobs.pop(old.id, None)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps({"jobs": [j.to_dict() for j in jobs.values()]}, indent=1),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)


def inside(workspace: Path, path: str) -> Path:
    """`path` resolved against the workspace, refusing anything that escapes it."""
    root = workspace.resolve()
    candidate = Path(path) if Path(path).is_absolute() else root / path
    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise ToolError(f"path {path!r} is outside the workspace ({root})")
    return resolved


class Musicgen:
    """The session's jobs: making, saving and telling.

    One per install, shared by both tools and both hooks. The tasks are the
    session's; nothing a turn does stops one, and `close` is the session's end.
    """

    def __init__(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self.workspace = Path(ctx.workspace).resolve()
        self.file = JobFile(self.workspace)
        self.session = str(getattr(ctx, "session_key", "") or "")
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.ended: dict[str, asyncio.Event] = {}
        self.lyrics: dict[str, str] = {}
        """What a vendor sang, by job, for this session only (see `Job`)."""
        self.asked: dict[str, str] = {}
        """Each job's request fingerprint, in memory only: a hash of the prompt
        is not the prompt, but it is no business of `jobs.json` either."""
        self.vendors: Callable[[str, str], Iterator[Vendor]] = self._vendors
        self.swept = False
        self.waking: asyncio.Task[None] | None = None
        """The wake on its way, if one is (`announce`)."""
        self.closed = False
        """The session has ended: nothing more is woken."""

    # -- settings --------------------------------------------------------------

    def timeout(self, timeout_ms: int = 0) -> float:
        if timeout_ms:
            return max(1.0, min(3600.0, timeout_ms / 1000))
        try:
            value = float(self.ctx.setting("timeout_seconds", 300.0) or 300.0)
        except (TypeError, ValueError):
            value = 300.0
        return max(30.0, min(900.0, value))

    # -- vendors ---------------------------------------------------------------

    def builders(self) -> dict[str, Callable[..., Any]]:
        """Every vendor's builder by name: the built-in, then the backends other
        plugins registered, in their install order. Read now rather than at
        `register`, so a plugin enabled since is in and one disabled since is
        out. A backend registered under `google` stands in for it."""
        found: dict[str, Callable[..., Any]] = {"google": self._google}
        found.update(self.ctx.extensions_in(POINT))
        return found

    def _vendors(self, provider: str = "", model: str = "") -> Iterator[Vendor]:
        """Every vendor: the one the model named, then the one the person
        configured, then the rest - each built with the key it holds now only
        when it is reached. `model` goes to the vendor the model named and to no
        other: an id means something only at its own vendor. A named vendor
        that is not here is passed over like one with no key."""
        builders = self.builders()
        configured = str(self.ctx.setting("provider", "") or "").strip().lower()
        if provider and provider not in builders:
            yield Vendor(provider, None, f"not here - the vendors are {', '.join(builders)}")
        order = [name for name in dict.fromkeys((provider, configured)) if name in builders]
        order += [name for name in builders if name not in order]
        for name in order:
            yield Vendor.built(name, builders[name], model if name == provider else "")

    def _google(self, model: str = "") -> GoogleMusic:
        credential = self.ctx.credential("google")
        key = {k: v for k, v in credential.items() if k in ("api_key", "auth_token")}
        return GoogleMusic(model=model or str(self.ctx.setting("google_model", "") or ""), **key)

    # -- the lifecycle ---------------------------------------------------------

    def mine(self) -> list[Job]:
        jobs = [job for job in self.file.load().values() if job.session == self.session]
        return sorted(jobs, key=lambda job: job.created)

    def duplicate(self, fingerprint: str) -> Job | None:
        """This session's job for the same request, if one is still being made
        or was saved in the last `DUPLICATE_SECONDS`. A failed one is not: asking
        again after a failure is a retry, not a duplicate."""
        ids = [job_id for job_id, seen in self.asked.items() if seen == fingerprint]
        if not ids:
            return None
        jobs = {job.id: job for job in self.mine()}
        now = time.time()
        for job_id in reversed(ids):
            job = jobs.get(job_id)
            if job is None:
                continue
            if job.state == "running" and job_id in self.tasks:
                return job
            if job.state == "done" and now - job.finished <= DUPLICATE_SECONDS:
                return job
        return None

    def reserved(self) -> set[str]:
        """Targets a running job has claimed, so two jobs never pick one name."""
        return {job.target for job in self.file.load().values() if job.state == "running"}

    def sweep(self) -> None:
        """Once per session: end this session's jobs an earlier one left
        running. Nothing at a vendor outlives the request that was making the
        track, so there is nothing to pick back up - only something to say."""
        if self.swept:
            return
        self.swept = True
        for job in self.mine():
            if job.state == "running" and job.id not in self.tasks:
                self._end(
                    job, error="the session ended while it was being made; it may have been billed"
                )

    def start(self, job: Job, vendors: list[tuple[Vendor, Request, str]]) -> None:
        """Make `job` in a task of the session's, never of the call's.

        An empty context, so the starting call's authority does not ride along:
        a task that inherited it would see the turn's abort as its own the
        moment someone stopped that turn."""
        self.ended.setdefault(job.id, asyncio.Event())
        loop = asyncio.get_running_loop()
        task = loop.create_task(self._make(job, vendors), context=contextvars.Context())
        self.tasks[job.id] = task

    def close(self) -> None:
        self.closed = True
        for task in self.tasks.values():
            task.cancel()
        self.tasks.clear()
        if self.waking is not None:
            self.waking.cancel()

    async def _make(self, job: Job, vendors: list[tuple[Vendor, Request, str]]) -> None:
        passed: list[str] = []
        try:
            for vendor, request, notes in vendors:
                job.vendor, job.model, job.notes = vendor.name, vendor.model, notes
                started = time.monotonic()
                try:
                    made = await asyncio.wait_for(vendor.generate(request), timeout=request.timeout)
                except asyncio.CancelledError:
                    self._attempt(vendor, request, started, error="stopped")
                    self._end(
                        job,
                        error="the session ended while it was being made; it may have been billed",
                    )
                    raise
                except TimeoutError:
                    # Not passed on: the vendor may have made, and billed, it.
                    error = f"timed out after {request.timeout:g}s; it may have been billed"
                    self._attempt(vendor, request, started, error=error)
                    self._end(job, error=f"{vendor.name}: {error}")
                    return
                except Exception as exc:  # a refusal; the next vendor is asked
                    error = _said(exc)
                    self._attempt(vendor, request, started, error=error)
                    passed.append(f"{vendor.name}: {error}")
                    continue
                self._attempt(vendor, request, started, made=made)
                self._collect(job, vendor, made)
                return
            self._end(job, error="; ".join(passed) or "no vendor could make it")
        finally:
            self.tasks.pop(job.id, None)
            self.ended.setdefault(job.id, asyncio.Event()).set()

    def _attempt(
        self,
        vendor: Vendor,
        request: Request,
        started: float,
        *,
        made: Made | None = None,
        error: str = "",
    ) -> None:
        arguments = _asked(vendor, request)
        if made is not None:
            arguments.update(bytes=len(made.data), sha256=hashlib.sha256(made.data).hexdigest())
            if made.model:
                arguments["model"] = made.model
        self.ctx.audit(
            "generate",
            error,
            outcome="error" if error else "ok",
            arguments=arguments,
            duration_ms=(time.monotonic() - started) * 1000,
        )

    def _collect(self, job: Job, vendor: Vendor, made: Made) -> None:
        job.model = made.model or job.model
        media_type = sniff_audio(made.data)
        if not media_type:
            # Not passed on: something was made, and may have been billed.
            self._end(job, error="what came back is not MP3, WAV, FLAC, Ogg or M4A audio")
            return
        try:
            saved = self._write(job, made.data, media_type)
        except OSError as exc:
            self._end(job, error=f"made but not saved: {type(exc).__name__}", cost=made.cost)
            return
        if made.lyrics:
            self.lyrics[job.id] = made.lyrics
        self._end(
            job,
            saved=saved,
            size=len(made.data),
            media_type=media_type,
            sha256=hashlib.sha256(made.data).hexdigest(),
            cost=made.cost,
        )

    def _write(self, job: Job, data: bytes, media_type: str) -> str:
        ext = EXTENSIONS[media_type]
        named = inside(self.workspace, job.target)
        # Joined as text, not `with_suffix`: a name with a dot in it ("v1.2")
        # would lose its tail.
        stem = named.name.removesuffix(named.suffix)
        target = named.parent / f"{stem}.{ext}"
        n = 2
        while True:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as handle:
                    handle.write(data)
                break
            except FileExistsError:
                # Something took the name while the track was being made; the
                # file beside it is the track, and nothing is overwritten.
                target = named.parent / f"{stem}-{n}.{ext}"
                n += 1
        return target.relative_to(self.workspace).as_posix()

    def _end(
        self,
        job: Job,
        *,
        error: str = "",
        saved: str = "",
        size: int = 0,
        media_type: str = "",
        sha256: str = "",
        cost: str = "",
    ) -> None:
        job.state = "failed" if error else "done"
        job.finished = time.time()
        job.error = error
        job.saved = saved
        job.bytes = size
        job.media_type = media_type
        job.cost = cost
        with contextlib.suppress(OSError):
            self.file.save(job)
        arguments: dict[str, Any] = {"job": job.id, "vendor": job.vendor, "model": job.model}
        if not error:
            arguments.update(path=saved, bytes=size, media_type=media_type, sha256=sha256)
        self.ctx.audit(
            "music",
            error or cost,
            outcome="error" if error else "ok",
            arguments=arguments,
            duration_ms=max(0.0, job.finished - job.created) * 1000,
        )
        self.ended.setdefault(job.id, asyncio.Event()).set()
        self.announce()

    # -- telling ---------------------------------------------------------------

    def wakes(self) -> bool:
        """Whether a finished job wakes the agent: `announce: wake` (the
        default) on an Ultron that has `ctx.wake` (SDK 1.40). Whether this
        session has anybody to wake is the core's answer, at the wake."""
        announce = str(self.ctx.setting("announce", "wake") or "wake").strip().lower()
        return announce == "wake" and hasattr(self.ctx, "wake") and not self.closed

    def announce(self) -> None:
        """Wake the agent about what finished, unless a wake is already on its
        way - that one says everything finished by the time it is sent."""
        if not self.wakes() or (self.waking is not None and not self.waking.done()):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self.waking = loop.create_task(self._wake(), context=contextvars.Context())

    async def _send_wake(self, text: str) -> bool:
        """`ctx.wake`, with `deliver` when a person set `announce_to` - the
        owner's DM or a named place (SDK 1.41). An Ultron before 1.41 refuses
        the keyword, and the next turn is told instead."""
        to = str(self.ctx.setting("announce_to", "") or "").strip()
        if to:
            return bool(await self.ctx.wake(text, deliver=to))
        return bool(await self.ctx.wake(text))

    async def _wake(self) -> None:
        """OpenClaw's completion event: a turn of the session's own that says
        which jobs finished, so the agent tells the person without waiting for
        them to speak. The jobs are marked told *before* the wake, so the woken
        turn's own `before_prompt` does not say them a second time; a wake that
        did not run - a busy lane, nobody there, the operator's no - unmarks
        them, and the next turn is told instead."""
        while True:
            jobs = [job for job in self.mine() if job.state != "running" and not job.notified]
            if not jobs:
                return
            for job in jobs:
                job.notified = True
                with contextlib.suppress(OSError):
                    self.file.save(job)
            lines = [_notice(job, job.id in self.lyrics) for job in jobs]
            lines.append("Tell the person, briefly, and say where to find it.")
            try:
                woke = bool(await self._send_wake("\n".join(lines)))
            except asyncio.CancelledError:
                woke = False
                raise
            except Exception:  # an Ultron that refuses the wake still tells the next turn
                woke = False
            finally:
                if not woke:
                    for job in jobs:
                        job.notified = False
                        with contextlib.suppress(OSError):
                            self.file.save(job)
            if not woke:
                return

    def notices(self) -> str:
        """One line per job finished since the model was last told. Facts this
        plugin worked out - a path, a size, a vendor's name - and never a word
        a vendor wrote; why one failed, and the lyrics, are behind
        `music_generate status`."""
        lines = []
        for job in self.mine():
            if job.state == "running" or job.notified:
                continue
            lines.append(_notice(job, job.id in self.lyrics))
            job.notified = True
            with contextlib.suppress(OSError):
                self.file.save(job)
        return "\n".join(lines)

    def told(self, job: Job) -> None:
        """`music_generate status` said how it ended, so no notice says it again."""
        if job.state != "running" and not job.notified:
            job.notified = True
            with contextlib.suppress(OSError):
                self.file.save(job)


def _notice(job: Job, lyrics: bool) -> str:
    if job.state == "done":
        line = (
            f"Note: music {job.id} is ready - saved to {job.saved} "
            f"({job.made_by()}, {job.media_type}, {_human(job.bytes)})."
        )
        if lyrics:
            line += f" music_generate status {job.id} has the lyrics."
        return line
    return (
        f"Note: music {job.id} from {job.vendor} failed; music_generate status {job.id} says why."
    )


def _duplicate(job: Job) -> str:
    """What a repeated request is told: the job it already is, and nothing
    started or spent."""
    if job.state == "running":
        age = _age(time.time() - job.created)
        return (
            f"Not started again: music {job.id} is the same request, running for {age} at "
            f"{job.made_by()}, to be saved to {job.target}. You will be told when it is ready."
        )
    age = _age(time.time() - job.finished)
    return (
        f"Not made again: music {job.id} is the same request, saved {age} ago to "
        f"{job.saved} by {job.made_by()}. For another take, change the request."
    )


def _choice(checked: Mapping[str, Any]) -> tuple[str, str]:
    """`model` as the model wrote it, split at its first `/` into a vendor's
    name and that vendor's own id - which may hold more slashes, as
    `openrouter/google/veo-3.1` does. A vendor alone is its configured model."""
    named = str(checked.get("model", "") or "").strip()
    provider, _, model = named.partition("/")
    provider = provider.strip().lower()
    if named and not VENDOR_NAME.fullmatch(provider):
        raise ToolError(f"model {named!r} is not provider/model - google/lyria-3-pro-preview, say")
    if model and (not MODEL_ID.fullmatch(model) or ".." in model or "//" in model):
        raise ToolError(f"{model!r} is not a model id")
    return provider, model


def _listed(vendor: Vendor) -> str:
    """One vendor's line for `action: list`: `model` as it would take it,
    whether it can be asked, and any further ids it names (`models`, optional).
    Facts the vendors' plugins hold - asking each `ready()` reads its key, and
    nothing is sent anywhere."""
    model = vendor.model
    line = f"- {vendor.name}/{model}" if model else f"- {vendor.name}"
    missing = vendor.ready()
    line += f": cannot be asked - {missing}" if missing else ": ready"
    others = [f"{vendor.name}/{each}" for each in _models(vendor.impl) if each != model]
    if others:
        line += f"; also {', '.join(others)}"
    return line


def _models(impl: Any) -> list[str]:
    """The ids a vendor says it also takes, checked as `model` would check
    them, at most `LISTED_MODELS`. Another plugin's code: one that is not a list
    of ids, or that raises, names none."""
    try:
        said = list(getattr(impl, "models", ()) or ())
    except Exception:
        return []
    ids = [str(each).strip() for each in said if isinstance(each, str)]
    ids = [each for each in ids if MODEL_ID.fullmatch(each) and ".." not in each]
    return list(dict.fromkeys(ids))[:LISTED_MODELS]


def _takes_model(builder: Callable[..., Any]) -> bool:
    """Whether a builder takes `model=` - one written before the model could
    choose takes no arguments."""
    try:
        parameters = inspect.signature(builder).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == "model" or p.kind is p.VAR_KEYWORD for p in parameters)


def _said(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


def _human(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _length(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} seconds"
    minutes, rest = divmod(seconds, 60)
    shown = f"{minutes} minute" + ("s" if minutes != 1 else "")
    return shown + (f" {rest} seconds" if rest else "")


def _asked(vendor: Vendor, request: Request) -> dict[str, Any]:
    """What an attempt record says. Never the prompt or the lyrics: they are
    the tool call's own arguments, already in that record."""
    arguments: dict[str, Any] = {
        "vendor": vendor.name,
        "model": vendor.model,
        "images": len(request.images),
        "lyrics": bool(request.lyrics),
    }
    if request.instrumental is not None:
        arguments["instrumental"] = request.instrumental
    if request.duration_seconds:
        arguments["durationSeconds"] = request.duration_seconds
    if request.format:
        arguments["format"] = request.format
    return arguments


# -- the tools -------------------------------------------------------------------


ACTIONS = ("generate", "status", "list")


class MusicGenerate(Tool):
    """OpenClaw's `music_generate`: one tool, three actions - start a track,
    see this session's jobs, list the vendors."""

    name = "music_generate"
    untrusted = True
    """When every vendor passes, the result names each one's refusal, and a
    refusal can carry a backend's words; why a job failed is a vendor's error
    code, and the lyrics are its words."""

    def __init__(self, runner: Musicgen) -> None:
        self.runner = runner
        self.workspace = runner.workspace

    @property
    def description(self) -> str:  # type: ignore[override]
        return (
            "Generate music or audio with lyrics, instrumental, durationSeconds and format. "
            "Runs in the background: call once per request; you are told when it is saved, so "
            "give a short ack and carry on - no poll. Describe the music in the prompt: genre, "
            "mood, tempo, instruments, the voice that sings. A value a vendor cannot take is "
            "dropped and the result says which. Each track costs money: no variations nobody "
            "asked for; the same request while it is being made, or within two minutes of it "
            "being saved, starts nothing. status shows this session's jobs and the lyrics a "
            "vendor sang; list shows the vendors."
        )

    @property
    def parameters(self) -> dict[str, Any]:  # type: ignore[override]
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": '"generate" default, "status" active task, "list" '
                    "providers/models.",
                },
                "prompt": {
                    "type": "string",
                    "description": "Music prompt: style, genre, mood, purpose.",
                },
                "lyrics": {
                    "type": "string",
                    "description": "Exact sung lyrics only when the user supplies lyrics or asks "
                    "for vocal words. For song/style requests, use prompt instead.",
                },
                "instrumental": {
                    "type": "boolean",
                    "description": "Instrumental-only toggle.",
                },
                "image": {
                    "type": "string",
                    "description": "One reference image path/URL.",
                },
                "images": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": f"Reference images; max {MAX_IMAGES}.",
                },
                "model": {
                    "type": "string",
                    "description": MODEL_ARGUMENT.format(
                        vendors=", ".join(self.runner.builders()),
                        example="google/lyria-3-pro-preview",
                    ),
                },
                "durationSeconds": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Target seconds; provider may clamp.",
                },
                "format": {
                    "type": "string",
                    "enum": list(FORMATS),
                    "description": "Output format: mp3, wav.",
                },
                "filename": {
                    "type": "string",
                    "description": "Output filename hint; basename preserved in managed media dir.",
                },
            },
        }

    def validate(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        checked = validate_arguments(self.parameters, arguments, tool=self.name)
        action = str(checked.get("action", "") or "generate").strip().lower()
        if action not in ACTIONS:
            raise ToolError(f"action must be one of: {', '.join(ACTIONS)}")
        if action != "generate":
            return {"action": action}
        prompt = str(checked.get("prompt", "") or "").strip()
        if not prompt:
            raise ToolError("generate needs a prompt")
        lyrics = str(checked.get("lyrics", "") or "").strip()
        if len(lyrics) > MAX_LYRICS:
            raise ToolError(f"lyrics are over {MAX_LYRICS} characters")
        duration = checked.get("durationSeconds")
        if duration is not None and int(duration) < 1:
            raise ToolError("durationSeconds must be a positive integer")
        format = str(checked.get("format", "") or "").strip().lower()
        if format and format not in FORMATS:
            raise ToolError('format must be one of "mp3" or "wav"')
        named = []
        if isinstance(checked.get("image"), str):
            named.append(checked["image"])
        named += [each for each in checked.get("images") or () if isinstance(each, str)]
        images = list(dict.fromkeys(e.strip().removeprefix("@").strip() for e in named if e))
        images = [each for each in images if each]
        if len(images) > MAX_IMAGES:
            raise ToolError(
                f"Too many reference images: {len(images)} provided, maximum is {MAX_IMAGES}."
            )
        for each in images:
            if not _is_url(each) and not each.startswith("data:"):
                inside(self.workspace, _local(each))
        instrumental = checked.get("instrumental")
        return {
            "action": action,
            "prompt": prompt,
            "lyrics": lyrics,
            "instrumental": instrumental if isinstance(instrumental, bool) else None,
            "duration_seconds": int(duration) if duration is not None else 0,
            "format": format,
            "images": images,
            "filename": str(checked.get("filename", "") or "").strip(),
            **dict(zip(("provider", "model"), _choice(checked), strict=True)),
        }

    async def run(  # type: ignore[override]
        self,
        action: str = "generate",
        prompt: str = "",
        lyrics: str = "",
        instrumental: bool | None = None,
        duration_seconds: int = 0,
        format: str = "",
        images: list[str] | None = None,
        filename: str = "",
        provider: str = "",
        model: str = "",
    ) -> ToolResult:
        self.runner.sweep()
        if action == "list":
            return self._list()
        if action == "status":
            return self._jobs()
        runner = self.runner
        try:
            pictures = tuple([await self._picture(named) for named in images or ()])
        except ToolError as exc:
            return ToolResult.error(str(exc))
        asked = Request(
            prompt, lyrics, instrumental, duration_seconds, pictures, runner.timeout(), format
        )
        # The same music from another vendor or model is not the same track.
        fingerprint = f"{asked.fingerprint()}:{provider}:{model}"
        duplicate = runner.duplicate(fingerprint)
        if duplicate is not None:
            return ToolResult.ok(_duplicate(duplicate))
        target = self._target(filename, prompt, format)
        able: list[tuple[Vendor, Request, str]] = []
        passed: list[str] = []
        for vendor in runner.vendors(provider, model):
            missing = vendor.ready()
            caps = music_capabilities(vendor.impl) if not missing else {}
            missing = missing or music_failure(caps, len(pictures))
            if missing:
                passed.append(f"{vendor.name}: {missing}")
                continue
            sent_lyrics, sent_instrumental, seconds, sent_format, notes = resolve_music_overrides(
                caps,
                images=len(pictures),
                lyrics=lyrics,
                instrumental=instrumental,
                duration_seconds=duration_seconds,
                format=format,
            )
            request = Request(
                prompt,
                sent_lyrics,
                sent_instrumental,
                seconds,
                pictures,
                asked.timeout,
                sent_format,
            )
            refused = vendor.cannot(request)
            if refused:
                passed.append(f"{vendor.name}: {refused}")
                continue
            able.append((vendor, request, " ".join(notes)))
        if not able:
            failure = "; ".join(passed) or "no vendor is configured"
            return ToolResult.error(f"no music started: {failure}")
        assert_active()  # the last moment before money is spent
        first, _, notes = able[0]
        made = Job(
            id=f"mg-{uuid.uuid4().hex[:6]}",
            vendor=first.name,
            model=first.model,
            target=target,
            session=runner.session,
            state="running",
            created=time.time(),
            notes=notes,
        )
        try:
            runner.file.save(made)
        except OSError as exc:
            # Made anyway: it only will not be listed in a later session.
            passed.append(f"not kept for a later session: {type(exc).__name__}")
        runner.asked[made.id] = fingerprint
        runner.start(made, able)
        line = (
            f"Started music {made.id} with {first.made_by()}; it will be saved to {target} "
            "when it is ready, usually within a couple of minutes, and you will be told then."
        )
        if notes:
            line += f" {notes}"
        if len(able) > 1:
            line += f" If {first.name} fails, {', '.join(v.name for v, _, _ in able[1:])} is next."
        if passed:
            line += f" Passed over {'; '.join(passed)}."
        return ToolResult.ok(line)

    async def _picture(self, named: str) -> Picture:
        """A reference picture: a workspace path, a `file://` URL inside the
        workspace, a `data:` URL, or an http(s) URL fetched under the
        operator's address policy - OpenClaw's four."""
        if named.startswith("data:"):
            data = _data_url(named)
        elif _is_url(named):
            data = await _fetch(named)
        else:
            target = inside(self.workspace, _local(named))
            try:
                data = target.read_bytes()
            except FileNotFoundError:
                raise ToolError(f"no such file: {named}") from None
            except (IsADirectoryError, PermissionError):
                raise ToolError(f"cannot read {named}") from None
        if len(data) > MAX_IMAGE_BYTES:
            raise ToolError(f"{named} is over {MAX_IMAGE_BYTES // (1024 * 1024)} MB")
        media_type = sniff_image(data)
        if media_type not in IMAGE_TYPES:
            raise ToolError(f"{named} is not a PNG, JPEG or WebP picture")
        return Picture(data, media_type)

    def _target(self, filename: str, prompt: str, format: str = "") -> str:
        """Where the track will go, decided now so the result can say so:
        `output_dir`, under the basename of the `filename` hint (OpenClaw's
        managed media dir) or a name made from the time and the prompt. A name
        already taken, or claimed by a running job, gets `-2`, `-3`. The real
        extension is the audio's own, set when it is saved."""
        reserved = self.runner.reserved()
        folder = inside(
            self.workspace, str(self.runner.ctx.setting("output_dir", "music") or "music")
        )
        stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(filename.replace("\\", "/")).stem)
        stem = stem.strip(".-")[:80]
        if not stem:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            slug = re.sub(r"[^a-z0-9]+", "-", prompt.lower()).strip("-")[:40].strip("-")
            stem = f"{stamp}-{slug or 'music'}"
        suffix = f".{format or 'mp3'}"
        # Joined as text, not `with_suffix`: a stem with a dot in it ("v1.2")
        # would lose its tail, and every `-n` would come back the same name.
        target = folder / f"{stem}{suffix}"
        n = 2
        while _taken(target, reserved, self.workspace):
            target = folder / f"{stem}-{n}{suffix}"
            n += 1
        return target.relative_to(self.workspace).as_posix()

    # -- status and list ---------------------------------------------------------

    def _list(self) -> ToolResult:
        """The vendors, as `model` takes them - OpenClaw's `list`."""
        lines = [
            "Music vendors, in the order they are asked. `model` takes provider/model; a "
            "provider alone is the model shown."
        ]
        lines += [_listed(vendor) for vendor in self.runner.vendors("", "")]
        return ToolResult.ok("\n".join(lines))

    def _jobs(self) -> ToolResult:
        """This session's jobs, newest first, with the lyrics a vendor sang -
        OpenClaw's `status`, the session's task."""
        runner = self.runner
        jobs = runner.mine()[-10:]
        if not jobs:
            return ToolResult.ok("No music jobs in this session.")
        lines = []
        for each in reversed(jobs):
            runner.told(each)
            line = _line(each)
            lyrics = runner.lyrics.get(each.id, "")
            if lyrics:
                shown = lyrics[:LYRICS_SHOWN] + ("\n[...]" if len(lyrics) > LYRICS_SHOWN else "")
                line += f"\nWhat {each.vendor} said with it:\n{shown}"
            lines.append(line)
        return ToolResult.ok("\n".join(lines))


def _taken(target: Path, reserved: set[str], workspace: Path) -> bool:
    """Whether `target`'s name is spoken for under any extension the track
    could come back as. The format is the vendor's to decide, so a name is
    free only when no file and no running job holds it as any of them - else
    the result would name one file and the save, finding the real extension
    taken, would write another."""
    stem = target.name.removesuffix(target.suffix)
    rel = target.parent.relative_to(workspace).as_posix()
    for suffix in SUFFIXES:
        if (target.parent / f"{stem}{suffix}").exists() or f"{rel}/{stem}{suffix}" in reserved:
            return True
    return False


def _is_url(named: str) -> bool:
    return named.lower().startswith(("http://", "https://"))


def _local(named: str) -> str:
    """A path, or a `file://` URL's path."""
    if named.lower().startswith("file://"):
        path = unquote(urlsplit(named).path)
        # file:///C:/x on Windows is the path C:/x.
        return path[1:] if re.match(r"^/[A-Za-z]:", path) else path
    if re.match(r"^[a-z][a-z0-9+.-]*:", named, re.IGNORECASE) and not re.match(
        r"^[a-z]:[\\/]", named, re.IGNORECASE
    ):
        raise ToolError(
            f"Unsupported image reference: {named}. Use a file path, a file:// URL, a data: "
            "URL, or an http(s) URL."
        )
    return named


def _data_url(named: str) -> bytes:
    head, _, payload = named.partition(",")
    if not head.endswith(";base64"):
        raise ToolError("a data: URL reference must be base64")
    try:
        return base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        raise ToolError("a data: URL reference is not valid base64") from None


async def _fetch(url: str) -> bytes:
    from ultron.sdk.web import get

    try:
        response = await get(
            url, max_bytes=MAX_IMAGE_BYTES, timeout=60.0, user_agent="ultron-musicgen"
        )
    except Exception as exc:  # the address policy, or the network
        raise ToolError(f"could not fetch {url}: {type(exc).__name__}") from None
    if response.status >= 400:
        raise ToolError(f"HTTP {response.status} fetching {url}")
    if response.truncated:
        raise ToolError(f"{url} is over {MAX_IMAGE_BYTES // (1024 * 1024)} MB")
    return response.body


def _line(job: Job) -> str:
    if job.state == "running":
        age = _age(time.time() - job.created)
        line = f"{job.id}: running for {age} at {job.made_by()}, to be saved to {job.target}"
    elif job.state == "done":
        cost = f", {job.cost}" if job.cost else ""
        line = (
            f"{job.id}: saved to {job.saved} by {job.made_by()} "
            f"({job.media_type}, {_human(job.bytes)}{cost})"
        )
    else:
        line = f"{job.id}: failed at {job.made_by()} - {job.error}"
    return f"{line}. {job.notes}" if job.notes else line


# -- the hooks -------------------------------------------------------------------


class Notifier(Hook):
    """Tells the model what finished, and stops the tasks when the session ends.
    `before_prompt` adds to the user message and never to the system prompt,
    for the reason the core's `compaction_notifier` gives: the prefix is cached."""

    name = "notifier"
    description = "Tells the model on its next turn when a music job has finished."
    events = ("session_start", "before_prompt", "on_session_end")

    def __init__(self, runner: Musicgen) -> None:
        self.runner = runner

    def session_start(self, event: Any) -> HookReturn:
        self.runner.sweep()
        return None

    def before_prompt(self, event: PromptEvent) -> HookReturn:
        self.runner.sweep()
        line = self.runner.notices()
        return HookOutcome.add_context(line) if line else None

    def on_session_end(self, session_id: str, *, turns: int = 0, reason: str = "") -> None:
        self.runner.close()


class MusicgenPlugin(Plugin):
    name = "musicgen"
    description = "Make music in the background with any music vendor a key is held for."

    def register(self, ctx: PluginContext) -> None:
        runner = Musicgen(ctx)
        ctx.register_tool(MusicGenerate(runner))
        # A session with no hook registry still makes music; it is told by
        # `music_generate status` rather than on its next turn.
        if ctx.accepts_hooks:
            ctx.register_hook(Notifier(runner))
