"""videogen: make videos in the background, on any vendor a key is held for.

A directory plugin written against `ultron.sdk` and nothing else. It brings two
tools, one vendor of its own, and a point any other plugin can put a vendor
into:

- `GoogleVideo` - Veo through the Gemini API: `predictLongRunning`, then the
  operation, then the file it names.
- `videogen.backend` - every other vendor, registered by the plugin that owns
  it with `ctx.register_extension("videogen.backend", name, build)` (SDK 1.39).
  The `xai`, `openrouter` and `together` provider plugins do; the interface is
  in `PLUGIN.md`, and nothing here names them.

Google stays here because its plugin ships inside Ultron, which does not know
videogen exists; its key comes from `ctx.credential`, and a backend reads its
own with its own plugin's. Whichever it is, `Vendor` is how this module sees
it, so a backend's object is touched in one place.

A video takes minutes, so nothing waits for one. `video_generate` submits and
returns a job id; a task the plugin owns follows the job, downloads the video
and writes it into the workspace; the agent is woken to tell the person
(`ctx.wake`, SDK 1.40), and where that cannot happen a `before_prompt` hook
tells the model on the session's next turn. The job belongs to the session, not to the call that
started it - like a backgrounded `exec` - so the task runs in a context of its
own, where an abort of the turn that submitted it cannot reach. Jobs are kept in
`.ultron/videogen/jobs.json` so the next session picks up one this session did
not finish; `on_session_end` stops the tasks and leaves the jobs there.
"""

from __future__ import annotations

import asyncio
import base64
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
from urllib.parse import urlsplit

from ultron.sdk.hook import Hook, HookOutcome, HookReturn, PromptEvent
from ultron.sdk.plugin_entry import Plugin, PluginContext
from ultron.sdk.runtime import ToolError, assert_active
from ultron.sdk.tool_plugin import Tool, ToolResult, validate_arguments

ASPECTS = ("landscape", "portrait", "square")
RESOLUTIONS = ("480p", "720p", "1080p")
MAX_SECONDS = 15
"""The longest any vendor here makes; xAI's ceiling."""
MAX_FRAME_BYTES = 20 * 1024 * 1024
"""A frame is inlined as base64, and every vendor here caps a request well
below what a larger picture would make of it."""
VIDEO_MAX_BYTES = 512 * 1024 * 1024
"""What one download may weigh. Fifteen seconds of 1080p is tens of MB."""
REPLY_MAX_BYTES = 1024 * 1024
"""A submission or a status is a small JSON document."""
POLL_TIMEOUT = 30.0
RETRIES = 5
"""Status checks that fail on the network, or with a 5xx or 429, in a row."""
KEEP = 200
"""Finished jobs kept in the file; the oldest past this are dropped."""
BUILT_IN = ("google",)
"""videogen's own vendors, tried before any backend another plugin registered
when `provider` names none."""
POINT = "videogen.backend"
"""Where another plugin puts a vendor (`ctx.register_extension`, SDK 1.39)."""
GOOGLE_HOST = "generativelanguage.googleapis.com"

FRAME_TYPES = ("image/png", "image/jpeg", "image/webp")
EXTENSIONS = {"video/mp4": "mp4", "video/quicktime": "mov", "video/webm": "webm"}
REMOTE_ID = re.compile(r"[A-Za-z0-9._:/-]{1,300}")
"""A vendor's job id, which goes into a URL path. It came from the vendor and
sits in a file a person can edit, so it is checked before it is used."""
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
path - `models/<id>:predictLongRunning` - so nothing that steps out of a path
segment or starts a query: no `..`, `//`, `?`, `#`, `%` or space."""
CODE = re.compile(r"[^A-Za-z0-9_.-]+")


def sniff_frame(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def sniff_video(data: bytes) -> str:
    """The video type the bytes are, or empty. Believed over any declaration."""
    if data[4:8] == b"ftyp":
        return "video/quicktime" if data[8:12] == b"qt  " else "video/mp4"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    return ""


def _code(value: Any) -> str:
    """A vendor's error code or status as an identifier - never its prose."""
    return CODE.sub("_", str(value or "")).strip("_")[:60]


# -- what crosses to a vendor ----------------------------------------------------


class Frame:
    """A workspace picture handed to a vendor as a first or last frame.

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

    def data_uri(self) -> str:
        return f"data:{self.media_type};base64,{self.b64()}"


class Request:
    __slots__ = ("aspect", "first", "last", "prompt", "resolution", "seconds", "timeout")

    def __init__(
        self,
        prompt: str,
        first: Frame | None = None,
        last: Frame | None = None,
        seconds: int = 0,
        aspect: str = "",
        resolution: str = "",
        timeout: float = 120.0,
    ) -> None:
        self.prompt = prompt
        self.first = first
        self.last = last
        self.seconds = seconds
        self.aspect = aspect
        self.resolution = resolution
        self.timeout = timeout

    @property
    def frames(self) -> int:
        return (self.first is not None) + (self.last is not None)


class Status:
    """Where a job stands at the vendor: `running`, `done` with somewhere to
    fetch it from, or `failed` with a code. `raw` is what a backend returned,
    handed back to its own `download`."""

    __slots__ = ("cost", "error", "raw", "state", "url")

    def __init__(
        self, state: str, url: str = "", error: str = "", cost: str = "", raw: Any = None
    ) -> None:
        self.state = state
        self.url = url
        self.error = error
        self.cost = cost
        self.raw = raw


class Retry(Exception):
    """A status check worth making again: the network, a 5xx, a 429. A
    backend says the same with any exception whose `retry` is true."""

    retry = True


def _retryable(exc: BaseException) -> bool:
    return getattr(exc, "retry", False) is True


class Vendor:
    """A vendor as videogen asks one - a built-in, or another plugin's
    backend - under the name it was found by.

    A backend is code this plugin did not write, so everything it is asked goes
    through here: a missing method is a vendor that cannot, one that raises is
    one that failed, a job id is checked before it goes into a URL or a file,
    and what `status` returns is read into videogen's own `Status`."""

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

    def ready(self) -> str:
        if self.why:
            return self.why
        return self._ask("ready")

    def cannot(self, request: Request) -> str:
        return self._ask("cannot", request)

    async def submit(self, request: Request) -> str:
        return _remote(await self.impl.submit(request), self.name)

    async def status(self, remote: str) -> Status:
        said = await self.impl.status(remote)
        state = str(getattr(said, "state", "") or "")
        if state not in ("running", "done", "failed"):
            raise RuntimeError(f"{self.name} answered a status videogen does not know")
        return Status(
            state,
            url=str(getattr(said, "url", "") or ""),
            error=str(getattr(said, "error", "") or ""),
            cost=str(getattr(said, "cost", "") or ""),
            raw=said,
        )

    async def download(self, status: Status, timeout: float) -> bytes:
        data = await self.impl.download(status.raw if status.raw is not None else status, timeout)
        if not isinstance(data, bytes | bytearray):
            raise RuntimeError(f"{self.name} sent no video")
        if len(data) > VIDEO_MAX_BYTES:
            raise RuntimeError(f"the video is over {VIDEO_MAX_BYTES // (1024 * 1024)} MB")
        return bytes(data)

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
    which would reach the model. Google's shape (`error.status`) and OpenAI's
    (`error.code`, `error.type`) both."""
    error = parsed.get("error")
    said: list[str] = []
    if isinstance(error, Mapping):
        said = [_code(error.get(key)) for key in ("status", "code", "type")]
    named = ", ".join(dict.fromkeys(part for part in said if part))
    return f"HTTP {status} from {where}" + (f" ({named})" if named else "")


async def _call(
    method: str,
    url: str,
    where: str,
    *,
    headers: Mapping[str, str],
    timeout: float,
    body: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    """One JSON request. A 5xx or a 429 is `Retry`, any other 4xx is final."""
    from ultron.sdk.web import WebError, get, post

    try:
        if method == "POST":
            response = await post(
                url,
                json=body,
                headers=headers,
                timeout=timeout,
                max_bytes=REPLY_MAX_BYTES,
                user_agent="ultron-videogen",
            )
        else:
            response = await get(
                url,
                headers=headers,
                timeout=timeout,
                max_bytes=REPLY_MAX_BYTES,
                max_redirects=0,
                user_agent="ultron-videogen",
            )
    except WebError as exc:
        raise Retry(f"{where} unreachable: {type(exc).__name__}") from None
    parsed = _json(response.body)
    if response.status >= 500 or response.status == 429:
        raise Retry(_failure(parsed, response.status, where))
    if response.status >= 400:
        raise RuntimeError(_failure(parsed, response.status, where))
    return parsed


async def _download(url: str, where: str, headers: Mapping[str, str], timeout: float) -> bytes:
    from ultron.sdk.web import WebError, get

    try:
        response = await get(
            url,
            headers=headers,
            timeout=timeout,
            max_bytes=VIDEO_MAX_BYTES,
            user_agent="ultron-videogen",
        )
    except WebError as exc:
        raise Retry(f"{where} download failed: {type(exc).__name__}") from None
    if response.status >= 500 or response.status == 429:
        raise Retry(f"HTTP {response.status} downloading from {where}")
    if response.status >= 400:
        raise RuntimeError(f"HTTP {response.status} downloading from {where}")
    if response.truncated:
        raise RuntimeError(f"the video is over {VIDEO_MAX_BYTES // (1024 * 1024)} MB")
    return response.body


def _bearer(credential: Mapping[str, str]) -> str:
    """An API key or a token, either of which these vendors take as a bearer."""
    return str(credential.get("api_key") or credential.get("auth_token") or "")


def _remote(value: Any, where: str) -> str:
    remote = str(value or "")
    if not REMOTE_ID.fullmatch(remote) or ".." in remote:
        raise RuntimeError(f"{where} sent no usable job id")
    return remote


# -- Google ----------------------------------------------------------------------

GOOGLE_URL = "https://generativelanguage.googleapis.com/v1beta"
GOOGLE_ASPECTS = {"landscape": "16:9", "portrait": "9:16"}


class GoogleVideo:
    host = GOOGLE_HOST
    seconds = (4, 6, 8)

    def __init__(
        self, *, model: str = "", api_key: str | None = None, auth_token: str | None = None
    ) -> None:
        self.model = (model or "veo-3.1-fast-generate-preview").strip().removeprefix("models/")
        if auth_token and auth_token.startswith("AIza"):
            api_key, auth_token = auth_token, None
        self._key = api_key or ""
        self._signed_in = bool(auth_token) and not api_key

    def ready(self) -> str:
        if self._signed_in:
            return "a Google sign-in cannot make videos; add an AI Studio key"
        return "" if self._key else "no google key (ultron auth add google)"

    def cannot(self, request: Request) -> str:
        if request.aspect == "square":
            return "makes 16:9 and 9:16 only"
        if request.seconds and request.seconds not in self.seconds:
            return "makes 4, 6 or 8 seconds"
        if request.resolution == "480p":
            return "makes 720p or 1080p"
        if request.resolution == "1080p" and request.seconds not in (0, 8):
            return "makes 1080p at 8 seconds only"
        if request.last is not None and request.first is None:
            return "needs a first frame to go with a last one"
        return ""

    async def submit(self, request: Request) -> str:
        instance: dict[str, Any] = {"prompt": request.prompt}
        if request.first is not None:
            instance["image"] = _inline(request.first)
        if request.last is not None:
            instance["lastFrame"] = _inline(request.last)
        parameters: dict[str, Any] = {}
        if request.aspect in GOOGLE_ASPECTS:
            parameters["aspectRatio"] = GOOGLE_ASPECTS[request.aspect]
        if request.resolution:
            parameters["resolution"] = request.resolution
        seconds = request.seconds or (8 if request.resolution == "1080p" else 0)
        if seconds:
            parameters["durationSeconds"] = seconds
        body: dict[str, Any] = {"instances": [instance]}
        if parameters:
            body["parameters"] = parameters
        parsed = await _call(
            "POST",
            f"{GOOGLE_URL}/models/{self.model}:predictLongRunning",
            "Google",
            headers=self._headers(),
            timeout=request.timeout,
            body=body,
        )
        return _remote(parsed.get("name"), "Google")

    async def status(self, remote: str) -> Status:
        parsed = await _call(
            "GET", f"{GOOGLE_URL}/{remote}", "Google", headers=self._headers(), timeout=POLL_TIMEOUT
        )
        if not parsed.get("done"):
            return Status("running")
        error = parsed.get("error")
        if isinstance(error, Mapping):
            code = _code(error.get("status")) or _code(error.get("code"))
            return Status("failed", error="Google failed it" + (f" ({code})" if code else ""))
        response = parsed.get("response")
        answer = response.get("generateVideoResponse") if isinstance(response, Mapping) else None
        samples = answer.get("generatedSamples") if isinstance(answer, Mapping) else None
        first = samples[0] if isinstance(samples, list) and samples else None
        video = first.get("video") if isinstance(first, Mapping) else None
        uri = str(video.get("uri") or "") if isinstance(video, Mapping) else ""
        if uri:
            return Status("done", url=uri)
        if isinstance(answer, Mapping) and answer.get("raiMediaFilteredCount"):
            return Status("failed", error="Google's safety filter withheld the video")
        return Status("failed", error="Google finished with no video")

    async def download(self, status: Status, timeout: float) -> bytes:
        # The key goes only to the API's own host. The file endpoint redirects
        # to a signed storage link, and the core client drops `x-goog-api-key`
        # at that hop (Starifter/ultron, redirect-key-headers); an Ultron from
        # before it carries the key to Google's storage, as `curl -L` does.
        if urlsplit(status.url).hostname != GOOGLE_HOST:
            raise RuntimeError("Google named a download somewhere other than its API")
        return await _download(status.url, "Google", self._headers(), timeout)

    def _headers(self) -> dict[str, str]:
        return {"x-goog-api-key": self._key}


def _inline(frame: Frame) -> dict[str, Any]:
    return {"inlineData": {"mimeType": frame.media_type, "data": frame.b64()}}


# -- jobs ------------------------------------------------------------------------


FIELDS = (
    "id",
    "vendor",
    "model",
    "remote",
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
    """One video, from submission to a file or a reason. What is kept on disk:
    nothing here is a credential, and the prompt is not here - it is in the
    tool call's own record."""

    __slots__ = FIELDS

    def __init__(self, **values: Any) -> None:
        self.id = str(values.get("id") or "")
        self.vendor = str(values.get("vendor") or "")
        self.model = str(values.get("model") or "")
        self.remote = str(values.get("remote") or "")
        self.target = str(values.get("target") or "")
        """Where the video is to go, relative to the workspace."""
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
    """`.ultron/videogen/jobs.json`: every job this workspace's sessions started.

    Read before every write and replaced whole, so two sessions in the same
    workspace each keep the other's jobs."""

    def __init__(self, workspace: Path) -> None:
        self.path = workspace / ".ultron" / "videogen" / "jobs.json"

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


class Videogen:
    """The session's jobs: submitting, following, saving and telling.

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
        self.vendors: Callable[[str, str], Iterator[Vendor]] = self._vendors
        self.interval = self._clamped("poll_seconds", 10.0, 2.0, 120.0)
        self.resumed = False
        self.waking: asyncio.Task[None] | None = None
        """The wake on its way, if one is (`announce`)."""
        self.closed = False
        """The session has ended: nothing more is woken."""

    # -- settings --------------------------------------------------------------

    def _clamped(self, key: str, default: float, low: float, high: float) -> float:
        try:
            value = float(self.ctx.setting(key, default) or default)
        except (TypeError, ValueError):
            value = default
        return max(low, min(high, value))

    def timeout(self) -> float:
        return self._clamped("timeout_seconds", 120.0, 5.0, 600.0)

    def deadline(self) -> float:
        return self._clamped("max_minutes", 60.0, 1.0, 240.0) * 60.0

    # -- vendors ---------------------------------------------------------------

    def builders(self) -> dict[str, Callable[..., Any]]:
        """Every vendor's builder by name: the built-ins, then the backends
        other plugins registered, in their install order. Read now rather than
        at `register`, so a plugin enabled since is in and one disabled since
        is out. A backend registered under `google` stands in for it."""
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

    def build(self, name: str, model: str = "") -> Vendor | None:
        """One vendor by name, as a job picked up again needs it, or `None`
        when nothing by that name is registered any more. On the job's own model
        where the builder takes one: the model may have chosen it, and a status
        asked of another model is asked of a job that is not there. A builder
        that takes none only ever made its configured model."""
        builder = self.builders().get(name)
        if builder is None:
            return None
        return Vendor.built(name, builder, model if _takes_model(builder) else "")

    def _google(self, model: str = "") -> GoogleVideo:
        credential = self.ctx.credential("google")
        key = {k: v for k, v in credential.items() if k in ("api_key", "auth_token")}
        return GoogleVideo(model=model or str(self.ctx.setting("google_model", "") or ""), **key)

    # -- the lifecycle ---------------------------------------------------------

    def mine(self) -> list[Job]:
        jobs = [job for job in self.file.load().values() if job.session == self.session]
        return sorted(jobs, key=lambda job: job.created)

    def reserved(self) -> set[str]:
        """Targets a running job has claimed, so two jobs never pick one name."""
        return {job.target for job in self.file.load().values() if job.state == "running"}

    def start(self, job: Job, vendor: Vendor) -> None:
        """Follow `job` in a task of the session's, never of the call's.

        An empty context, so the submitting call's authority does not ride
        along: a task that inherited it would see the turn's abort as its own
        the moment someone stopped that turn."""
        self.ended.setdefault(job.id, asyncio.Event())
        loop = asyncio.get_running_loop()
        task = loop.create_task(self._follow(job, vendor), context=contextvars.Context())
        self.tasks[job.id] = task

    def resume(self) -> None:
        """Once per session: pick up this session's jobs an earlier one left
        running. Only asks; nothing is submitted again."""
        if self.resumed:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self.resumed = True
        for job in self.mine():
            if job.state != "running" or job.id in self.tasks:
                continue
            vendor = self.build(job.vendor, job.model)
            if vendor is None:
                self._end(
                    job,
                    error=f"no vendor {job.vendor!r} to resume with - is its plugin enabled?",
                )
                continue
            missing = vendor.ready()
            if missing:
                self._end(job, error=f"could not resume: {missing}")
                continue
            self.ctx.audit(
                "resume", arguments={"job": job.id, "vendor": job.vendor, "remote": job.remote}
            )
            self.start(job, vendor)

    def close(self) -> None:
        self.closed = True
        for task in self.tasks.values():
            task.cancel()
        self.tasks.clear()
        if self.waking is not None:
            self.waking.cancel()

    async def _follow(self, job: Job, vendor: Vendor) -> None:
        began = time.monotonic()
        limit = self.deadline() - max(0.0, time.time() - job.created)
        failures = 0
        try:
            while True:
                if time.monotonic() - began > limit:
                    self._end(job, error=f"gave up after {self.deadline() / 60:g} minutes")
                    return
                try:
                    status = await vendor.status(job.remote)
                    failures = 0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # a 4xx, a reply of the wrong shape, or a retry
                    if not _retryable(exc):
                        self._end(job, error=_said(exc))
                        return
                    failures += 1
                    if failures >= RETRIES:
                        self._end(job, error=f"{exc}, {RETRIES} times in a row")
                        return
                    await asyncio.sleep(self.interval)
                    continue
                if status.state == "failed":
                    self._end(job, error=status.error or "the vendor failed it")
                    return
                if status.state == "done":
                    await self._collect(job, vendor, status)
                    return
                await asyncio.sleep(self.interval)
        finally:
            self.tasks.pop(job.id, None)
            self.ended.setdefault(job.id, asyncio.Event()).set()

    async def _collect(self, job: Job, vendor: Vendor, status: Status) -> None:
        data = b""
        for attempt in range(RETRIES):
            try:
                data = await vendor.download(status, max(self.timeout(), 300.0))
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not _retryable(exc) or attempt == RETRIES - 1:
                    error = str(exc) if _retryable(exc) else _said(exc)
                    self._end(job, error=error, cost=status.cost)
                    return
                await asyncio.sleep(self.interval)
        media_type = sniff_video(data)
        if not media_type:
            self._end(
                job, error="what came back is not an MP4, MOV or WebM video", cost=status.cost
            )
            return
        try:
            saved = self._write(job, data, media_type)
        except OSError as exc:
            self._end(job, error=f"made but not saved: {type(exc).__name__}", cost=status.cost)
            return
        self._end(
            job,
            saved=saved,
            size=len(data),
            media_type=media_type,
            sha256=hashlib.sha256(data).hexdigest(),
            cost=status.cost,
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
                # Something took the name while the video was being made; the
                # file beside it is the video, and nothing is overwritten.
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
            "video",
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
            lines = [_notice(job) for job in jobs]
            lines.append("Tell the person, briefly, and say where to find it.")
            woke = False
            try:
                woke = bool(await self._send_wake("\n".join(lines)))
            except asyncio.CancelledError:
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
        a vendor wrote; why one failed is behind `video_generate status`."""
        lines = []
        for job in self.mine():
            if job.state == "running" or job.notified:
                continue
            lines.append(_notice(job))
            job.notified = True
            with contextlib.suppress(OSError):
                self.file.save(job)
        return "\n".join(lines)

    def told(self, job: Job) -> None:
        """`video_generate status` said how it ended, so no notice says it again."""
        if job.state != "running" and not job.notified:
            job.notified = True
            with contextlib.suppress(OSError):
                self.file.save(job)


def _notice(job: Job) -> str:
    if job.state == "done":
        return (
            f"Note: video {job.id} is ready - saved to {job.saved} "
            f"({job.made_by()}, {job.media_type}, {_human(job.bytes)})."
        )
    return (
        f"Note: video {job.id} from {job.vendor} failed; video_generate status {job.id} says why."
    )


def _choice(checked: Mapping[str, Any]) -> tuple[str, str]:
    """`model` as the model wrote it, split at its first `/` into a vendor's
    name and that vendor's own id - which may hold more slashes, as
    `openrouter/google/veo-3.1` does. A vendor alone is its configured model."""
    named = str(checked.get("model", "") or "").strip()
    provider, _, model = named.partition("/")
    provider = provider.strip().lower()
    if named and not VENDOR_NAME.fullmatch(provider):
        raise ToolError(
            f"model {named!r} is not provider/model - google/veo-3.1-generate-preview, say"
        )
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


# -- the tools -------------------------------------------------------------------


ACTIONS = ("generate", "status", "list")


class VideoGenerate(Tool):
    """OpenClaw's `video_generate`: one tool, three actions - start a video,
    look at one, list them."""

    name = "video_generate"
    untrusted = True
    """When every vendor passes, the result names each one's refusal, and a
    refusal carries a vendor's error code; why a job failed is one too."""

    MAX_WAIT = 600

    def __init__(self, runner: Videogen) -> None:
        self.runner = runner
        self.workspace = runner.workspace

    @property
    def description(self) -> str:  # type: ignore[override]
        return (
            "Make a video, saved in the workspace. Use it when the person asks for a video, a "
            "clip or an animation. `action: generate` (the default) starts it and returns at "
            "once with a job id; the video takes one to several minutes and you are told when "
            "it is saved, so tell the person it is on its way and carry on - do not check on it "
            "in a loop. Describe the shot in the prompt: subject, action, camera, style, and "
            "any sound or speech. To animate a picture, name a workspace image as "
            "`first_frame`; `last_frame` is where the video should end. Not every vendor takes "
            "every length, shape or frame; one that cannot is passed over. Each call is one "
            "video and costs money: do not make variations nobody asked for. `action: status` "
            "with a `job` shows one - running, saved, or failed and why - and `wait` waits up to "
            "that many seconds for it, only when the person is waiting on it; with no `job` it "
            "lists this session's jobs. `action: list` shows the vendors and their models."
        )

    @property
    def parameters(self) -> dict[str, Any]:  # type: ignore[override]
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(ACTIONS),
                    "description": "generate (the default), status or list.",
                },
                "prompt": {
                    "type": "string",
                    "description": "What happens in the video. Needed to generate.",
                },
                "first_frame": {
                    "type": "string",
                    "description": "A workspace PNG, JPEG or WebP the video starts from.",
                },
                "last_frame": {
                    "type": "string",
                    "description": "A workspace PNG, JPEG or WebP the video ends on.",
                },
                "seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_SECONDS,
                    "description": "How long. Leave it out for the vendor's default.",
                },
                "aspect": {
                    "type": "string",
                    "enum": list(ASPECTS),
                    "description": "landscape is 16:9, portrait 9:16. Leave it out for the "
                    "vendor's default.",
                },
                "resolution": {
                    "type": "string",
                    "enum": list(RESOLUTIONS),
                    "description": "Leave it out for the vendor's default.",
                },
                "path": {
                    "type": "string",
                    "description": "Where to save it, relative to the workspace. Leave it out "
                    "for a new file. Never an existing file.",
                },
                "model": {
                    "type": "string",
                    "description": MODEL_ARGUMENT.format(
                        vendors=", ".join(self.runner.builders()),
                        example="google/veo-3.1-generate-preview",
                    ),
                },
                "job": {
                    "type": "string",
                    "description": "A job id, vg-... For status; leave it out for all of "
                    "this session's jobs.",
                },
                "wait": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": self.MAX_WAIT,
                    "description": "Seconds to wait for the job to finish. For status.",
                },
            },
        }

    def validate(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        checked = validate_arguments(self.parameters, arguments, tool=self.name)
        action = str(checked.get("action", "") or "generate").strip().lower()
        if action not in ACTIONS:
            raise ToolError(f"action must be one of: {', '.join(ACTIONS)}")
        if action == "list":
            return {"action": action}
        if action == "status":
            job = str(checked.get("job", "") or "").strip()
            wait = int(checked.get("wait") or 0) if job else 0
            return {"action": action, "job": job, "wait": max(0, min(self.MAX_WAIT, wait))}
        prompt = str(checked.get("prompt", "") or "").strip()
        if not prompt:
            raise ToolError("generate needs a prompt")
        first = str(checked.get("first_frame", "") or "").strip()
        last = str(checked.get("last_frame", "") or "").strip()
        seconds = int(checked.get("seconds") or 0)
        if seconds and not 1 <= seconds <= MAX_SECONDS:
            raise ToolError(f"seconds must be 1 to {MAX_SECONDS}")
        aspect = str(checked.get("aspect", "") or "").strip().lower()
        if aspect and aspect not in ASPECTS:
            raise ToolError(f"aspect must be one of: {', '.join(ASPECTS)}")
        resolution = str(checked.get("resolution", "") or "").strip().lower()
        if resolution and resolution not in RESOLUTIONS:
            raise ToolError(f"resolution must be one of: {', '.join(RESOLUTIONS)}")
        path = str(checked.get("path", "") or "").strip()
        for named in (first, last, path):
            if named:
                inside(self.workspace, named)
        return {
            "action": action,
            "prompt": prompt,
            "first_frame": first,
            "last_frame": last,
            "seconds": seconds,
            "aspect": aspect,
            "resolution": resolution,
            "path": path,
            **dict(zip(("provider", "model"), _choice(checked), strict=True)),
        }

    async def run(  # type: ignore[override]
        self,
        action: str = "generate",
        prompt: str = "",
        first_frame: str = "",
        last_frame: str = "",
        seconds: int = 0,
        aspect: str = "",
        resolution: str = "",
        path: str = "",
        job: str = "",
        wait: int = 0,
        provider: str = "",
        model: str = "",
    ) -> ToolResult:
        self.runner.resume()
        if action == "list":
            return self._list()
        if action == "status":
            return await self._status(job, wait)
        return await self._generate(
            prompt, first_frame, last_frame, seconds, aspect, resolution, path, provider, model
        )

    # -- generate ----------------------------------------------------------------

    async def _generate(
        self,
        prompt: str,
        first_frame: str,
        last_frame: str,
        seconds: int,
        aspect: str,
        resolution: str,
        path: str,
        provider: str = "",
        model: str = "",
    ) -> ToolResult:
        runner = self.runner
        try:
            first = self._frame(first_frame) if first_frame else None
            last = self._frame(last_frame) if last_frame else None
            target = self._target(path, prompt)
        except ToolError as exc:
            return ToolResult.error(str(exc))
        request = Request(prompt, first, last, seconds, aspect, resolution, runner.timeout())
        passed: list[str] = []
        for vendor in runner.vendors(provider, model):
            missing = vendor.ready() or vendor.cannot(request)
            if missing:
                passed.append(f"{vendor.name}: {missing}")
                continue
            assert_active()  # the last moment before money is spent
            started = time.monotonic()
            try:
                remote = await asyncio.wait_for(vendor.submit(request), timeout=request.timeout)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                error = f"timed out after {request.timeout:g}s"
            except Exception as exc:  # a refusal; the next vendor is asked
                error = _said(exc)
            else:
                return self._submitted(vendor, remote, request, target, passed, started)
            runner.ctx.audit(
                "submit",
                error,
                outcome="error",
                arguments=_submission(vendor, request),
                duration_ms=(time.monotonic() - started) * 1000,
            )
            passed.append(f"{vendor.name}: {error}")
        failure = "; ".join(passed) or "no vendor is configured"
        return ToolResult.error(f"no video started: {failure}")

    def _submitted(
        self,
        vendor: Vendor,
        remote: str,
        request: Request,
        target: str,
        passed: list[str],
        started: float,
    ) -> ToolResult:
        runner = self.runner
        job = Job(
            id=f"vg-{uuid.uuid4().hex[:6]}",
            vendor=vendor.name,
            model=vendor.model,
            remote=remote,
            target=target,
            session=runner.session,
            state="running",
            created=time.time(),
        )
        runner.ctx.audit(
            "submit",
            outcome="ok",
            arguments={**_submission(vendor, request), "job": job.id, "remote": remote},
            duration_ms=(time.monotonic() - started) * 1000,
        )
        try:
            runner.file.save(job)
        except OSError as exc:
            # Followed anyway: the video was paid for. It is only not resumable.
            passed.append(f"not kept for a later session: {type(exc).__name__}")
        runner.start(job, vendor)
        line = (
            f"Started video {job.id} with {job.made_by()}; it will be saved to {target} "
            "when it is ready, usually in one to several minutes, and you will be told then. "
            f"video_generate status {job.id} checks on it or waits for it."
        )
        if passed:
            line += f" Passed over {'; '.join(passed)}."
        return ToolResult.ok(line)

    def _frame(self, named: str) -> Frame:
        target = inside(self.workspace, named)
        try:
            data = target.read_bytes()
        except FileNotFoundError:
            raise ToolError(f"no such file: {named}") from None
        except (IsADirectoryError, PermissionError):
            raise ToolError(f"cannot read {named}") from None
        if len(data) > MAX_FRAME_BYTES:
            raise ToolError(f"{named} is over {MAX_FRAME_BYTES // (1024 * 1024)} MB")
        media_type = sniff_frame(data)
        if media_type not in FRAME_TYPES:
            raise ToolError(f"{named} is not a PNG, JPEG or WebP picture")
        return Frame(data, media_type)

    def _target(self, path: str, prompt: str) -> str:
        """Where the video will go, decided now so the result can say so. A
        named path that exists, or that a running job has claimed, is refused
        before anything is spent."""
        reserved = self.runner.reserved()
        if path:
            named = inside(self.workspace, path)
            shown = named.relative_to(self.workspace).as_posix()
            if named.suffix.lower() not in (".mp4", ".mov", ".webm"):
                named = named.with_suffix(".mp4")
                shown = named.relative_to(self.workspace).as_posix()
            if named.exists() or shown in reserved:
                raise ToolError(f"{shown} already exists; name a new file")
            return shown
        folder = str(self.runner.ctx.setting("output_dir", "videos") or "videos")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        slug = re.sub(r"[^a-z0-9]+", "-", prompt.lower()).strip("-")[:40].strip("-") or "video"
        base = inside(self.workspace, folder) / f"{stamp}-{slug}"
        target = base.with_suffix(".mp4")
        n = 2
        while target.exists() or target.relative_to(self.workspace).as_posix() in reserved:
            target = base.with_name(f"{base.name}-{n}").with_suffix(".mp4")
            n += 1
        return target.relative_to(self.workspace).as_posix()

    # -- status and list ---------------------------------------------------------

    def _list(self) -> ToolResult:
        """The vendors, as `model` takes them - OpenClaw's `list`."""
        lines = [
            "Video vendors, in the order they are asked. `model` takes provider/model; a "
            "provider alone is the model shown."
        ]
        lines += [_listed(vendor) for vendor in self.runner.vendors("", "")]
        return ToolResult.ok("\n".join(lines))

    def _jobs(self) -> ToolResult:
        """This session's jobs, newest first - `status` with no job, as
        OpenClaw's `status` is the session's task."""
        runner = self.runner
        jobs = runner.mine()[-10:]
        if not jobs:
            return ToolResult.ok("No video jobs in this session.")
        for each in jobs:
            runner.told(each)
        return ToolResult.ok("\n".join(_line(each) for each in reversed(jobs)))

    async def _status(self, job: str, wait: int) -> ToolResult:
        if not job:
            return self._jobs()
        runner = self.runner
        found = {each.id: each for each in runner.mine()}.get(job)
        if found is None:
            return ToolResult.error(f"no video job {job} in this session")
        if wait and found.state == "running" and job in runner.tasks:
            ended = runner.ended.setdefault(job, asyncio.Event())
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(ended.wait()), timeout=wait)
            found = {each.id: each for each in runner.mine()}.get(job, found)
        runner.told(found)
        return ToolResult.ok(_line(found))


def _submission(vendor: Vendor, request: Request) -> dict[str, Any]:
    """What a submission record says. Never the prompt: it is the tool call's
    own argument, already in that record."""
    arguments: dict[str, Any] = {
        "vendor": vendor.name,
        "model": vendor.model,
        "frames": request.frames,
    }
    for key in ("seconds", "aspect", "resolution"):
        if getattr(request, key):
            arguments[key] = getattr(request, key)
    return arguments


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
    """Picks up an earlier session's jobs, tells the model what finished, and
    stops the tasks when the session ends. `before_prompt` adds to the user
    message and never to the system prompt, for the reason the core's
    `compaction_notifier` gives: the prefix is cached."""

    name = "notifier"
    description = "Tells the model on its next turn when a video job has finished."
    events = ("session_start", "before_prompt", "on_session_end")

    def __init__(self, runner: Videogen) -> None:
        self.runner = runner

    def session_start(self, event: Any) -> HookReturn:
        self.runner.resume()
        return None

    def before_prompt(self, event: PromptEvent) -> HookReturn:
        self.runner.resume()
        line = self.runner.notices()
        return HookOutcome.add_context(line) if line else None

    def on_session_end(self, session_id: str, *, turns: int = 0, reason: str = "") -> None:
        self.runner.close()


class VideogenPlugin(Plugin):
    name = "videogen"
    description = "Make videos in the background with any video vendor a key is held for."

    def register(self, ctx: PluginContext) -> None:
        runner = Videogen(ctx)
        ctx.register_tool(VideoGenerate(runner))
        # A session with no hook registry still makes videos; it is told by
        # `video_generate status` rather than on its next turn, and picks up an earlier
        # session's jobs the first time a tool is called.
        if ctx.accepts_hooks:
            ctx.register_hook(Notifier(runner))
