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
but that request takes a minute or two, so `generate_music` starts a job and
returns. The job belongs to the session, not to the call that started it - like
a backgrounded `exec` - so the task runs in a context of its own, where an abort
of the turn that started it cannot reach. A `before_prompt` hook tells the model
on the session's next turn. There is nothing at a vendor to pick back up, so a
session that ends mid-track ends the job, and the next session says so.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import contextvars
import hashlib
import json
import os
import re
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

from ultron.sdk.hook import Hook, HookOutcome, HookReturn, PromptEvent
from ultron.sdk.plugin_entry import Plugin, PluginContext
from ultron.sdk.runtime import ToolError, assert_active
from ultron.sdk.tool_plugin import Tool, ToolResult, validate_arguments

MIN_SECONDS = 5
MAX_SECONDS = 600
"""What any vendor here makes: Lyria's songs run a couple of minutes, and the
ceiling is ElevenLabs' ten, for a backend that sells it."""
MAX_IMAGES = 10
"""Lyria's limit; a backend that takes fewer says so in `cannot`."""
MAX_IMAGE_BYTES = 20 * 1024 * 1024
"""A picture is inlined as base64, and every vendor here caps a request well
below what a larger one would make of it."""
MAX_LYRICS = 5000
AUDIO_MAX_BYTES = 64 * 1024 * 1024
"""What one track may weigh. Two minutes of 44.1 kHz stereo WAV is about 21 MB."""
REPLY_MAX_BYTES = 96 * 1024 * 1024
"""A reply carries the track as base64, a third bigger than the track."""
LYRICS_SHOWN = 4000
"""What `music_status` shows of a vendor's lyrics."""
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
    __slots__ = ("images", "instrumental", "lyrics", "prompt", "seconds", "timeout")

    def __init__(
        self,
        prompt: str,
        lyrics: str = "",
        instrumental: bool = False,
        seconds: int = 0,
        images: tuple[Picture, ...] = (),
        timeout: float = 300.0,
    ) -> None:
        self.prompt = prompt
        self.lyrics = lyrics
        self.instrumental = instrumental
        self.seconds = seconds
        self.images = images
        self.timeout = timeout

    @property
    def described(self) -> str:
        """The prompt with every other ask written into it, for a vendor - Lyria -
        whose only control is the words."""
        parts = [self.prompt.rstrip()]
        if self.seconds:
            parts.append(f"Length: about {_length(self.seconds)}.")
        if self.instrumental:
            parts.append("Instrumental only, no vocals.")
        if self.lyrics:
            parts.append(f"Lyrics:\n{self.lyrics.strip()}")
        return "\n\n".join(parts)

    def fingerprint(self) -> str:
        """What makes two requests the same track: everything sent to a vendor,
        the pictures by their bytes. Not where it is saved - the same music to
        another file is still the same music, paid for twice."""
        digest = hashlib.sha256()
        for part in (self.prompt, self.lyrics, str(self.instrumental), str(self.seconds)):
            digest.update(part.encode("utf-8") + b"\x00")
        for image in self.images:
            digest.update(hashlib.sha256(image.data).digest())
        return digest.hexdigest()


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
    def built(cls, name: str, builder: Callable[[], Any]) -> Vendor:
        try:
            return cls(name, builder())
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
CLIP_SECONDS = 30


class GoogleMusic:
    """Lyria through the Interactions API: `POST /interactions`, answered with
    the track as base64 and whatever Lyria wrote - the lyrics, the structure -
    as text beside it."""

    host = GOOGLE_HOST

    def __init__(
        self, *, model: str = "", api_key: str | None = None, auth_token: str | None = None
    ) -> None:
        self.model = (model or "lyria-3.5").strip().removeprefix("models/")
        if auth_token and auth_token.startswith("AIza"):
            api_key, auth_token = auth_token, None
        self._key = api_key or ""
        self._signed_in = bool(auth_token) and not api_key

    @property
    def clip(self) -> bool:
        return "clip" in self.model

    def ready(self) -> str:
        if self._signed_in:
            return "a Google sign-in cannot make music; add an AI Studio key"
        return "" if self._key else "no google key (ultron auth add google)"

    def cannot(self, request: Request) -> str:
        if self.clip and request.seconds and request.seconds != CLIP_SECONDS:
            return f"{self.model} makes {CLIP_SECONDS}-second clips only"
        if len(request.images) > MAX_IMAGES:
            return f"takes up to {MAX_IMAGES} pictures"
        return ""

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
        self.vendors: Callable[[], Iterator[Vendor]] = self._vendors
        self.swept = False

    # -- settings --------------------------------------------------------------

    def timeout(self) -> float:
        try:
            value = float(self.ctx.setting("timeout_seconds", 300.0) or 300.0)
        except (TypeError, ValueError):
            value = 300.0
        return max(30.0, min(900.0, value))

    # -- vendors ---------------------------------------------------------------

    def builders(self) -> dict[str, Callable[[], Any]]:
        """Every vendor's builder by name: the built-in, then the backends other
        plugins registered, in their install order. Read now rather than at
        `register`, so a plugin enabled since is in and one disabled since is
        out. A backend registered under `google` stands in for it."""
        found: dict[str, Callable[[], Any]] = {"google": self._google}
        found.update(self.ctx.extensions_in(POINT))
        return found

    def _vendors(self) -> Iterator[Vendor]:
        """Every vendor, the preferred one first, each built with the key it
        holds now only when it is reached."""
        builders = self.builders()
        first = str(self.ctx.setting("provider", "") or "").strip().lower()
        order = [first] if first in builders else []
        order += [name for name in builders if name not in order]
        for name in order:
            yield Vendor.built(name, builders[name])

    def _google(self) -> GoogleMusic:
        credential = self.ctx.credential("google")
        key = {k: v for k, v in credential.items() if k in ("api_key", "auth_token")}
        return GoogleMusic(model=str(self.ctx.setting("google_model", "") or ""), **key)

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

    def start(self, job: Job, vendors: list[Vendor], request: Request) -> None:
        """Make `job` in a task of the session's, never of the call's.

        An empty context, so the starting call's authority does not ride along:
        a task that inherited it would see the turn's abort as its own the
        moment someone stopped that turn."""
        self.ended.setdefault(job.id, asyncio.Event())
        loop = asyncio.get_running_loop()
        task = loop.create_task(self._make(job, vendors, request), context=contextvars.Context())
        self.tasks[job.id] = task

    def close(self) -> None:
        for task in self.tasks.values():
            task.cancel()
        self.tasks.clear()

    async def _make(self, job: Job, vendors: list[Vendor], request: Request) -> None:
        passed: list[str] = []
        try:
            for vendor in vendors:
                job.vendor, job.model = vendor.name, vendor.model
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
        base = named.with_suffix("")
        target = named.with_suffix(f".{ext}")
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
                target = base.with_name(f"{base.name}-{n}").with_suffix(f".{ext}")
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

    # -- telling ---------------------------------------------------------------

    def notices(self) -> str:
        """One line per job finished since the model was last told. Facts this
        plugin worked out - a path, a size, a vendor's name - and never a word
        a vendor wrote; why one failed, and the lyrics, are behind
        `music_status`."""
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
        """`music_status` said how it ended, so no notice says it again."""
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
            line += f" music_status {job.id} has the lyrics."
        return line
    return f"Note: music {job.id} from {job.vendor} failed; music_status {job.id} says why."


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
    if request.instrumental:
        arguments["instrumental"] = True
    if request.seconds:
        arguments["seconds"] = request.seconds
    return arguments


# -- the tools -------------------------------------------------------------------


class GenerateMusic(Tool):
    name = "generate_music"
    untrusted = True
    """When every vendor passes, the result names each one's refusal, and a
    refusal can carry a backend's words."""

    def __init__(self, runner: Musicgen) -> None:
        self.runner = runner
        self.workspace = runner.workspace

    @property
    def description(self) -> str:  # type: ignore[override]
        return (
            "Start making a piece of music - a song, an instrumental, a jingle or a "
            "soundtrack - saved in the workspace when it is ready. It returns at once with a "
            "job id; the track takes up to a couple of minutes, and you are told on a later "
            "turn when it is saved, so tell the person it is on its way and carry on - do not "
            "call music_status in a loop. Describe the music in the prompt: genre, mood, "
            "tempo, instruments, the voice that sings. Give `lyrics` only when the person "
            "wrote them or asked you to; leave them out and the vendor writes its own. "
            "`images` are workspace pictures that set the mood. Each call is one track and "
            "costs money: do not make variations nobody asked for. The same request while "
            "its track is being made, or within two minutes of it being saved, starts "
            "nothing and answers with that job."
        )

    @property
    def parameters(self) -> dict[str, Any]:  # type: ignore[override]
        return {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "What the music should be."},
                "lyrics": {
                    "type": "string",
                    "description": "Words to sing. Leave it out for the vendor's own.",
                },
                "instrumental": {
                    "type": "boolean",
                    "description": "True for no vocals. Not with lyrics.",
                },
                "seconds": {
                    "type": "integer",
                    "minimum": MIN_SECONDS,
                    "maximum": MAX_SECONDS,
                    "description": "About how long. Leave it out for the vendor's default.",
                },
                "images": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": MAX_IMAGES,
                    "description": "Workspace PNG, JPEG or WebP pictures to set the mood.",
                },
                "path": {
                    "type": "string",
                    "description": "Where to save it, relative to the workspace. Leave it out "
                    "for a new file. Never an existing file.",
                },
            },
            "required": ["prompt"],
        }

    def validate(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        checked = validate_arguments(self.parameters, arguments, tool=self.name)
        prompt = str(checked.get("prompt", "") or "").strip()
        if not prompt:
            raise ToolError("generate_music needs a prompt")
        lyrics = str(checked.get("lyrics", "") or "").strip()
        if len(lyrics) > MAX_LYRICS:
            raise ToolError(f"lyrics are over {MAX_LYRICS} characters")
        instrumental = bool(checked.get("instrumental"))
        if instrumental and lyrics:
            raise ToolError("an instrumental has no lyrics; give one or the other")
        seconds = int(checked.get("seconds") or 0)
        if seconds and not MIN_SECONDS <= seconds <= MAX_SECONDS:
            raise ToolError(f"seconds must be {MIN_SECONDS} to {MAX_SECONDS}")
        images = [str(each or "").strip() for each in checked.get("images") or ()]
        images = [each for each in images if each]
        if len(images) > MAX_IMAGES:
            raise ToolError(f"at most {MAX_IMAGES} images")
        path = str(checked.get("path", "") or "").strip()
        for named in (*images, path):
            if named:
                inside(self.workspace, named)
        return {
            "prompt": prompt,
            "lyrics": lyrics,
            "instrumental": instrumental,
            "seconds": seconds,
            "images": images,
            "path": path,
        }

    async def run(  # type: ignore[override]
        self,
        prompt: str,
        lyrics: str = "",
        instrumental: bool = False,
        seconds: int = 0,
        images: list[str] | None = None,
        path: str = "",
    ) -> ToolResult:
        runner = self.runner
        runner.sweep()
        try:
            pictures = tuple(self._picture(named) for named in images or ())
        except ToolError as exc:
            return ToolResult.error(str(exc))
        request = Request(prompt, lyrics, instrumental, seconds, pictures, runner.timeout())
        fingerprint = request.fingerprint()
        duplicate = runner.duplicate(fingerprint)
        if duplicate is not None:
            return ToolResult.ok(_duplicate(duplicate))
        try:
            target = self._target(path, prompt)
        except ToolError as exc:
            return ToolResult.error(str(exc))
        able: list[Vendor] = []
        passed: list[str] = []
        for vendor in runner.vendors():
            missing = vendor.ready() or vendor.cannot(request)
            if missing:
                passed.append(f"{vendor.name}: {missing}")
            else:
                able.append(vendor)
        if not able:
            failure = "; ".join(passed) or "no vendor is configured"
            return ToolResult.error(f"no music started: {failure}")
        assert_active()  # the last moment before money is spent
        first = able[0]
        job = Job(
            id=f"mg-{uuid.uuid4().hex[:6]}",
            vendor=first.name,
            model=first.model,
            target=target,
            session=runner.session,
            state="running",
            created=time.time(),
        )
        try:
            runner.file.save(job)
        except OSError as exc:
            # Made anyway: it only will not be listed in a later session.
            passed.append(f"not kept for a later session: {type(exc).__name__}")
        runner.asked[job.id] = fingerprint
        runner.start(job, able, request)
        line = (
            f"Started music {job.id} with {first.made_by()}; it will be saved to {target} "
            "when it is ready, usually within a couple of minutes. You will be told on a "
            "later turn; music_status checks on it or waits for it."
        )
        if len(able) > 1:
            line += f" If {first.name} fails, {', '.join(v.name for v in able[1:])} is next."
        if passed:
            line += f" Passed over {'; '.join(passed)}."
        return ToolResult.ok(line)

    def _picture(self, named: str) -> Picture:
        target = inside(self.workspace, named)
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

    def _target(self, path: str, prompt: str) -> str:
        """Where the track will go, decided now so the result can say so. A
        named path that exists, or that a running job has claimed, is refused
        before anything is spent."""
        reserved = self.runner.reserved()
        if path:
            named = inside(self.workspace, path)
            if named.suffix.lower() not in SUFFIXES:
                named = named.with_suffix(".mp3")
            shown = named.relative_to(self.workspace).as_posix()
            if named.exists() or shown in reserved:
                raise ToolError(f"{shown} already exists; name a new file")
            return shown
        folder = str(self.runner.ctx.setting("output_dir", "music") or "music")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        slug = re.sub(r"[^a-z0-9]+", "-", prompt.lower()).strip("-")[:40].strip("-") or "music"
        base = inside(self.workspace, folder) / f"{stamp}-{slug}"
        target = base.with_suffix(".mp3")
        n = 2
        while target.exists() or target.relative_to(self.workspace).as_posix() in reserved:
            target = base.with_name(f"{base.name}-{n}").with_suffix(".mp3")
            n += 1
        return target.relative_to(self.workspace).as_posix()


class MusicStatus(Tool):
    name = "music_status"
    untrusted = True
    """Why a job failed is a vendor's error code, and the lyrics are its words."""

    MAX_WAIT = 900

    def __init__(self, runner: Musicgen) -> None:
        self.runner = runner

    @property
    def description(self) -> str:  # type: ignore[override]
        return (
            "This session's music jobs from generate_music: running, saved, or failed and "
            "why. Name a `job` to see one, with the lyrics the vendor sang when it said them; "
            "give `wait` to wait up to that many seconds for it to finish - only when the "
            "person is waiting on it, since you are told when a job finishes anyway."
        )

    @property
    def parameters(self) -> dict[str, Any]:  # type: ignore[override]
        return {
            "type": "object",
            "properties": {
                "job": {"type": "string", "description": "A job id, mg-..."},
                "wait": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": self.MAX_WAIT,
                    "description": "Seconds to wait for the job to finish. Needs `job`.",
                },
            },
        }

    def validate(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        checked = validate_arguments(self.parameters, arguments, tool=self.name)
        job = str(checked.get("job", "") or "").strip()
        wait = int(checked.get("wait") or 0)
        if wait and not job:
            raise ToolError("wait needs a job to wait for")
        return {"job": job, "wait": max(0, min(self.MAX_WAIT, wait))}

    async def run(self, job: str = "", wait: int = 0) -> ToolResult:  # type: ignore[override]
        runner = self.runner
        runner.sweep()
        if not job:
            jobs = runner.mine()[-10:]
            if not jobs:
                return ToolResult.ok("No music jobs in this session.")
            for each in jobs:
                runner.told(each)
            return ToolResult.ok("\n".join(_line(each) for each in reversed(jobs)))
        found = {each.id: each for each in runner.mine()}.get(job)
        if found is None:
            return ToolResult.error(f"no music job {job} in this session")
        if wait and found.state == "running" and job in runner.tasks:
            ended = runner.ended.setdefault(job, asyncio.Event())
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(ended.wait()), timeout=wait)
            found = {each.id: each for each in runner.mine()}.get(job, found)
        runner.told(found)
        line = _line(found)
        lyrics = runner.lyrics.get(job, "")
        if lyrics:
            shown = lyrics[:LYRICS_SHOWN] + ("\n[...]" if len(lyrics) > LYRICS_SHOWN else "")
            line += f"\nWhat {found.vendor} said with it:\n{shown}"
        return ToolResult.ok(line)


def _line(job: Job) -> str:
    if job.state == "running":
        age = _age(time.time() - job.created)
        return f"{job.id}: running for {age} at {job.made_by()}, to be saved to {job.target}"
    if job.state == "done":
        cost = f", {job.cost}" if job.cost else ""
        return (
            f"{job.id}: saved to {job.saved} by {job.made_by()} "
            f"({job.media_type}, {_human(job.bytes)}{cost})"
        )
    return f"{job.id}: failed at {job.made_by()} - {job.error}"


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
        ctx.register_tool(GenerateMusic(runner))
        ctx.register_tool(MusicStatus(runner))
        # A session with no hook registry still makes music; it is told by
        # `music_status` rather than on its next turn.
        if ctx.accepts_hooks:
            ctx.register_hook(Notifier(runner))
