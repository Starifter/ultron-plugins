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
import binascii
import contextlib
import contextvars
import hashlib
import inspect
import json
import math
import os
import re
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from ultron.sdk.hook import Hook, HookOutcome, HookReturn, PromptEvent
from ultron.sdk.plugin_entry import Plugin, PluginContext
from ultron.sdk.runtime import ToolError, assert_active
from ultron.sdk.tool_plugin import Tool, ToolResult, validate_arguments

MAX_REFERENCES = {"image": 9, "video": 4, "audio": 3}
"""OpenClaw's: reference images, videos and audios one call may hand in."""
ROLES = {
    "image": ("first_frame", "last_frame", "reference_image"),
    "video": ("reference_video",),
    "audio": ("reference_audio",),
}
"""OpenClaw's canonical roles. A vendor may take other strings; they are passed
through as written."""
REFERENCE_MAX_BYTES = {
    "image": 20 * 1024 * 1024,
    "video": 100 * 1024 * 1024,
    "audio": 50 * 1024 * 1024,
}
"""What one reference may weigh. Each is inlined as base64 to most vendors, and
every vendor here caps a request well below what more would make of it."""
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
VIDEO_SUFFIXES = tuple(f".{ext}" for ext in EXTENSIONS.values())
REMOTE_ID = re.compile(r"[A-Za-z0-9._:/-]{1,300}")
"""A vendor's job id, which goes into a URL path. It came from the vendor and
sits in a file a person can edit, so it is checked before it is used."""
MODEL_ARGUMENT = (
    "Provider/model override, e.g. {example}. The providers here: {vendors}. If it fails "
    "or cannot take the request, the others are tried on their own models and the result "
    "says so."
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


def sniff_audio(data: bytes) -> str:
    if data.startswith(b"ID3") or (len(data) > 1 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0):
        return "audio/mpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    if data.startswith(b"OggS"):
        return "audio/ogg"
    if data.startswith(b"fLaC"):
        return "audio/flac"
    if data[4:8] == b"ftyp":
        return "audio/mp4"
    return ""


SNIFF = {"image": sniff_frame, "video": sniff_video, "audio": sniff_audio}
KINDS = {
    "image": "a PNG, JPEG or WebP picture",
    "video": "an MP4, MOV or WebM video",
    "audio": "an MP3, WAV, OGG, M4A or FLAC file",
}


# -- geometry, OpenClaw's -------------------------------------------------------
#
# `src/media-generation/runtime-shared.ts` and `geometry-normalization.ts`:
# the closest supported aspect ratio by log distance, the closest size by shape
# then area, the closest resolution within its unit. A copy of imagegen's: a
# plugin imports `ultron.sdk` and nothing of another plugin's.

ASPECT = re.compile(r"^(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)$")
SIZE = re.compile(r"^(\d+)\s*x\s*(\d+)$", re.IGNORECASE)
RANK = re.compile(r"^(\d+(?:\.\d+)?)([kp])$", re.IGNORECASE)


def parse_aspect_ratio(raw: str | None) -> tuple[float, float, float] | None:
    match = ASPECT.match((raw or "").strip())
    if not match:
        return None
    width, height = float(match[1]), float(match[2])
    if width <= 0 or height <= 0:
        return None
    return width, height, width / height


def parse_size(raw: str | None) -> tuple[int, int, float, int] | None:
    match = SIZE.match((raw or "").strip())
    if not match:
        return None
    width, height = int(match[1]), int(match[2])
    if width <= 0 or height <= 0:
        return None
    return width, height, width / height, width * height


def derive_aspect_ratio(size: str | None) -> str | None:
    parsed = parse_size(size)
    if not parsed:
        return None
    divisor = math.gcd(parsed[0], parsed[1]) or 1
    return f"{parsed[0] // divisor}:{parsed[1] // divisor}"


def _closest(
    values: Sequence[str], score: Callable[[str], tuple[float, float] | None]
) -> str | None:
    best: tuple[float, float, str] | None = None
    for value in values:
        scored = score(value)
        if scored is None:
            continue
        candidate = (scored[0], scored[1], value)
        if best is None or candidate < best:
            best = candidate
    return best[2] if best else None


def closest_aspect_ratio(
    requested: str | None, size: str | None, supported: Sequence[str] | None
) -> str | None:
    values = [each for each in supported or () if each.strip()]
    if not values:
        return requested or derive_aspect_ratio(size)
    if requested and requested in values:
        return requested
    wanted = parse_aspect_ratio(requested) or parse_aspect_ratio(derive_aspect_ratio(size))
    if not wanted:
        return None

    def score(candidate: str) -> tuple[float, float] | None:
        parsed = parse_aspect_ratio(candidate)
        if not parsed:
            return None
        return (
            abs(math.log(parsed[2] / wanted[2])),
            abs(parsed[0] * wanted[1] - wanted[0] * parsed[1]),
        )

    return _closest(values, score)


def closest_size(
    requested: str | None, aspect_ratio: str | None, supported: Sequence[str] | None
) -> str | None:
    values = [each for each in supported or () if each.strip()]
    if not values:
        return requested
    if requested and requested in values:
        return requested
    wanted = parse_size(requested)
    shape = parse_aspect_ratio(aspect_ratio)
    if not wanted and not shape:
        return None
    ratio = wanted[2] if wanted else shape[2]  # type: ignore[index]

    def score(candidate: str) -> tuple[float, float] | None:
        parsed = parse_size(candidate)
        if not parsed:
            return None
        area = abs(math.log(parsed[3] / wanted[3])) if wanted else float(parsed[3])
        return abs(math.log(parsed[2] / ratio)), area

    return _closest(values, score)


def _rank(resolution: str | None) -> tuple[float, str] | None:
    match = RANK.match((resolution or "").strip())
    if not match:
        return None
    unit = match[2].upper()
    value = float(match[1])
    return (value * 1000 if unit == "K" else value), unit


def closest_resolution(requested: str | None, supported: Sequence[str] | None) -> str | None:
    values = [each for each in supported or () if each.strip()]
    if not values:
        return requested
    if requested and requested in values:
        return requested
    wanted = _rank(requested)
    if not wanted:
        return None

    def score(candidate: str) -> tuple[float, float] | None:
        rank = _rank(candidate)
        if not rank or rank[1] != wanted[1]:
            return None
        return abs(rank[0] - wanted[0]), 1.0 if rank[0] < wanted[0] else 0.0

    return _closest(values, score)


class Geometry:
    """What `resolve_geometry` settled on: the values to send, what was moved
    (`normalized`: key to (requested, applied, derived_from)), and what was
    dropped (`ignored`: key and value)."""

    __slots__ = ("aspect_ratio", "ignored", "normalized", "resolution", "size")

    def __init__(self) -> None:
        self.size: str | None = None
        self.aspect_ratio: str | None = None
        self.resolution: str | None = None
        self.ignored: list[tuple[str, Any]] = []
        self.normalized: dict[str, tuple[Any, Any, str]] = {}


def resolve_geometry(
    *,
    size: str | None,
    aspect_ratio: str | None,
    resolution: str | None,
    caps: Mapping[str, Any] | None,
    fallback_sizes: Sequence[str] | None = None,
    report_unrecognized: bool = False,
    aspect_for_size: bool = False,
) -> Geometry:
    """OpenClaw's `resolveMediaGeometryOverrides`, line for line."""
    out = Geometry()
    if caps is None:
        out.size, out.aspect_ratio, out.resolution = size, aspect_ratio, resolution
        return out
    sizes = caps.get("sizes")
    ratios = caps.get("aspect_ratios")
    resolutions = caps.get("resolutions")
    supports_size = bool(caps.get("supports_size"))
    supports_ratio = bool(caps.get("supports_aspect_ratio"))
    supports_resolution = bool(caps.get("supports_resolution"))

    if size and sizes and supports_size:
        moved = closest_size(size, aspect_ratio if aspect_for_size else None, sizes)
        if moved and moved != size:
            out.normalized["size"] = (size, moved, "")
        size = moved

    if size and not supports_size:
        # A size alone can still be honoured by a vendor that takes a shape.
        translated = closest_aspect_ratio(aspect_ratio, size, ratios) if supports_ratio else None
        if translated:
            aspect_ratio = translated
            out.normalized["aspectRatio"] = (None, translated, "size")
        else:
            out.ignored.append(("size", size))
        size = None

    if aspect_ratio and ratios and supports_ratio:
        moved = closest_aspect_ratio(aspect_ratio, size, ratios)
        if moved and moved != aspect_ratio:
            out.normalized["aspectRatio"] = (aspect_ratio, moved, "")
        elif not moved and report_unrecognized:
            out.ignored.append(("aspectRatio", aspect_ratio))
        aspect_ratio = moved
    elif aspect_ratio and not supports_ratio:
        translated = None
        if supports_size and not size:
            # An empty list means sizes are free, not that there are no hints.
            pool = fallback_sizes if sizes is not None and len(sizes) == 0 else sizes
            translated = closest_size(None, aspect_ratio, pool)
        if translated:
            size = translated
            out.normalized["size"] = (None, translated, "aspectRatio")
        else:
            out.ignored.append(("aspectRatio", aspect_ratio))
        aspect_ratio = None

    if resolution and resolutions and supports_resolution:
        moved = closest_resolution(resolution, resolutions)
        if moved and moved != resolution:
            out.normalized["resolution"] = (resolution, moved, "")
        elif not moved and report_unrecognized:
            out.ignored.append(("resolution", resolution))
        resolution = moved
    elif resolution and not supports_resolution:
        out.ignored.append(("resolution", resolution))
        resolution = None

    out.size, out.aspect_ratio, out.resolution = size, aspect_ratio, resolution
    return out


def normalize_resolution(raw: str) -> str:
    """OpenClaw's `normalizeResolution`: `720p` is `720P`, `4k` is `4K`, and
    anything else - a vendor's own word - is kept as written."""
    value = raw.strip()
    upper = value.upper()
    return upper if re.fullmatch(r"\d+[PK]", upper) else value


def select_duration(seconds: int, supported: Sequence[int]) -> int:
    """OpenClaw's `selectSupportedVideoDuration`: the nearest, the longer on a tie."""
    return min(supported, key=lambda each: (abs(each - seconds), -each))


def _code(value: Any) -> str:
    """A vendor's error code or status as an identifier - never its prose."""
    return CODE.sub("_", str(value or "")).strip("_")[:60]


# -- what crosses to a vendor ----------------------------------------------------


class Asset:
    """A reference handed to a vendor: a picture, a video or a sound, with the
    role it was given (`imageRoles` and the rest), or none.

    Plain classes rather than dataclasses throughout, for imagegen's reason: an
    install at SDK 1.38 from before Starifter/ultron#6 imports a directory
    plugin without registering it, and `@dataclass` under `from __future__
    import annotations` fails there."""

    __slots__ = ("data", "media_type", "name", "role", "url")

    def __init__(
        self, data: bytes, media_type: str, role: str = "", name: str = "", url: str = ""
    ) -> None:
        self.data = data
        self.media_type = media_type
        self.role = role
        self.name = name
        self.url = url
        """Where it was fetched from, for a vendor that takes a link and not
        bytes - xAI's video edits. Empty for a workspace file."""

    def b64(self) -> str:
        return base64.b64encode(self.data).decode("ascii")

    def data_uri(self) -> str:
        return f"data:{self.media_type};base64,{self.b64()}"


Frame = Asset
"""The name a backend written for videogen 3.x knew."""


class Request:
    """What one vendor is asked for, after its capabilities had their say.

    `first`, `last`, `seconds` and `aspect` are what a backend written for
    videogen 3.x read: the pictures given the first and last frame (or, with no
    roles, the first and second), the duration, and an empty shape."""

    __slots__ = (
        "aspect_ratio",
        "audio",
        "audios",
        "duration_seconds",
        "images",
        "prompt",
        "provider_options",
        "resolution",
        "size",
        "timeout",
        "videos",
        "watermark",
    )

    def __init__(
        self,
        prompt: str,
        images: tuple[Asset, ...] = (),
        videos: tuple[Asset, ...] = (),
        audios: tuple[Asset, ...] = (),
        *,
        size: str = "",
        aspect_ratio: str = "",
        resolution: str = "",
        duration_seconds: int = 0,
        audio: bool | None = None,
        watermark: bool | None = None,
        provider_options: Mapping[str, Any] | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.prompt = prompt
        self.images = images
        self.videos = videos
        self.audios = audios
        self.size = size
        self.aspect_ratio = aspect_ratio
        self.resolution = resolution
        self.duration_seconds = duration_seconds
        self.audio = audio
        self.watermark = watermark
        self.provider_options = dict(provider_options or {})
        self.timeout = timeout

    def _role(self, role: str, fallback: int) -> Asset | None:
        for image in self.images:
            if image.role == role:
                return image
        unroled = [image for image in self.images if not image.role]
        return unroled[fallback] if len(unroled) > fallback else None

    @property
    def first(self) -> Asset | None:
        return self._role("first_frame", 0)

    @property
    def last(self) -> Asset | None:
        if any(image.role == "first_frame" for image in self.images):
            return self._role("last_frame", 0)
        return self._role("last_frame", 1)

    @property
    def seconds(self) -> int:
        return self.duration_seconds

    @property
    def aspect(self) -> str:
        return ""

    @property
    def frames(self) -> int:
        return len(self.images)


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

    @property
    def capabilities(self) -> dict[str, Any]:
        """The vendor's `capabilities`, OpenClaw's shape, read defensively. One
        written before 4.0 declares none and is read as what it was: a vendor
        that takes a first and a last frame, no reference video or sound, and no
        size, shape, resolution, sound or watermark it could be told about - so
        each of those is dropped and reported rather than sent to code that
        would not read it. Its own `cannot` still has the last word."""
        try:
            said = getattr(self.impl, "capabilities", None)
            if isinstance(said, Mapping):
                return dict(said)
        except Exception:
            pass
        return {
            "legacy": True,
            "generate": {},
            "image_to_video": {"enabled": True, "max_input_images": 2},
        }

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
GOOGLE_DURATIONS = (4, 6, 8)


def _google_common() -> dict[str, Any]:
    """OpenClaw's `createGoogleVideoCommonCapabilities`."""
    return {
        "max_duration_seconds": 8,
        "supported_duration_seconds": GOOGLE_DURATIONS,
        "aspect_ratios": ("16:9", "9:16"),
        "resolutions": ("720P", "1080P"),
        "supports_aspect_ratio": True,
        "supports_resolution": True,
        "supports_size": True,
        "supports_audio": False,
    }


def _google_aspect(aspect_ratio: str, size: str) -> str:
    if aspect_ratio in ("16:9", "9:16"):
        return aspect_ratio
    parsed = parse_size(size)
    if not parsed:
        return ""
    return "16:9" if parsed[0] >= parsed[1] else "9:16"


def _google_resolution(resolution: str, size: str) -> str:
    if resolution in ("720P", "1080P"):
        return resolution.lower()
    parsed = parse_size(size)
    if not parsed:
        return ""
    edge = max(parsed[0], parsed[1])
    return "1080p" if edge >= 1920 else "720p" if edge >= 1280 else ""


class GoogleVideo:
    host = GOOGLE_HOST
    models = (
        "veo-3.1-fast-generate-preview",
        "veo-3.1-generate-preview",
        "veo-3.1-lite-generate-preview",
    )
    capabilities = {
        "generate": {"max_videos": 1, **_google_common()},
        "image_to_video": {
            "enabled": True,
            "max_videos": 1,
            "max_input_images": 1,
            **_google_common(),
        },
        "video_to_video": {
            "enabled": True,
            "max_videos": 1,
            "max_input_videos": 1,
            **_google_common(),
        },
    }
    """OpenClaw's Google video provider: one picture to start from, or one video
    to extend, never both."""

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

    async def submit(self, request: Request) -> str:
        instance: dict[str, Any] = {"prompt": request.prompt}
        if request.images:
            instance["image"] = _inline(request.images[0])
        if request.videos:
            instance["video"] = _inline(request.videos[0])
        parameters: dict[str, Any] = {}
        aspect = _google_aspect(request.aspect_ratio, request.size)
        if aspect:
            parameters["aspectRatio"] = aspect
        resolution = _google_resolution(request.resolution, request.size)
        if resolution:
            parameters["resolution"] = resolution
        if request.duration_seconds:
            parameters["durationSeconds"] = select_duration(
                min(8, max(4, request.duration_seconds)), GOOGLE_DURATIONS
            )
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


def _inline(asset: Asset) -> dict[str, Any]:
    return {"inlineData": {"mimeType": asset.media_type, "data": asset.b64()}}


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
    "notes",
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
        self.notes = str(values.get("notes") or "")
        """What the vendor was sent instead of what was asked - Ultron's words."""

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

    def timeout(self, timeout_ms: int = 0) -> float:
        """`timeoutMs` when the model gave one, else the person's
        `timeout_seconds`: one request to a vendor."""
        if timeout_ms:
            return max(1.0, min(3600.0, timeout_ms / 1000))
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
                # Something took the name while the video was being made; the
                # file beside it is the video, and nothing is overwritten.
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


MODE_LABELS = {
    "generate": "text-to-video generation",
    "image_to_video": "image-to-video generation",
    "video_to_video": "video-to-video generation",
}


def video_mode(images: int, videos: int) -> str:
    """OpenClaw's `resolveVideoGenerationMode`; empty for pictures and videos
    both, which only a video-to-video mode that also takes pictures can do."""
    if images and videos:
        return ""
    if videos:
        return "video_to_video"
    if images:
        return "image_to_video"
    return "generate"


def mode_capabilities(
    caps: Mapping[str, Any], images: int, videos: int
) -> Mapping[str, Any] | None:
    """OpenClaw's `resolveVideoGenerationModeCapabilities`."""
    mode = video_mode(images, videos)
    if mode:
        found = caps.get(mode)
        return found if isinstance(found, Mapping) else None
    both = caps.get("video_to_video")
    if (
        isinstance(both, Mapping)
        and both.get("enabled")
        and (both.get("max_input_images") or 0) > 0
    ):
        return both
    return None


def capability_failure(
    label: str, caps: Mapping[str, Any], images: tuple[Asset, ...], videos: int, audios: int
) -> str:
    """OpenClaw's `buildVideoGenerationCapabilityFailure`: a vendor that would
    drop a reference handed in is passed over, never sent a request it would
    answer without it."""
    mode = video_mode(len(images), videos)
    mode_caps = mode_capabilities(caps, len(images), videos)
    modes = caps.get("modes")
    if mode and isinstance(modes, list | tuple) and mode not in modes:
        return f"{label} does not support {MODE_LABELS[mode]}; skipping"
    if images or videos:
        what = (
            "combined image/video reference inputs"
            if images and videos
            else "reference image inputs"
            if images
            else "reference video inputs"
        )
        if mode_caps is None or not mode_caps.get("enabled"):
            return f"{label} does not support {what}; skipping to avoid silent reference drop"
    for kind, count, key in (
        ("image", len(images), "max_input_images"),
        ("video", videos, "max_input_videos"),
        ("audio", audios, "max_input_audios"),
    ):
        limit = int((mode_caps or {}).get(key) or caps.get(key) or 0)
        if count > limit:
            if limit == 0:
                return (
                    f"{label} does not support reference {kind} inputs; skipping to avoid "
                    f"silent {kind} drop"
                )
            return (
                f"{label} supports at most {limit} reference {kind}(s), {count} requested; skipping"
            )
    if caps.get("legacy") and any(
        image.role not in ("", "first_frame", "last_frame") for image in images
    ):
        return (
            f"{label} takes a first and a last frame only; skipping to avoid silent reference drop"
        )
    return ""


def options_failure(
    label: str, options: Mapping[str, Any], declared: Mapping[str, str] | None
) -> str:
    """OpenClaw's `validateProviderOptionsAgainstDeclaration`: a vendor that
    declares nothing takes options as they come; one that declares an empty set
    takes none; one that declares keys takes those, of those types."""
    if not options or declared is None:
        return ""
    if not declared:
        supplied = ", ".join(options)
        return f"{label} does not accept providerOptions (caller supplied: {supplied}); skipping"
    unknown = [key for key in options if key not in declared]
    if unknown:
        return (
            f"{label} does not accept providerOptions keys: {', '.join(unknown)} "
            f"(accepted: {', '.join(declared)}); skipping"
        )
    for key, value in options.items():
        expected = declared[key]
        if expected == "number" and (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
        ):
            return f"{label} expects providerOptions.{key} to be a finite number; skipping"
        if expected == "boolean" and not isinstance(value, bool):
            return f"{label} expects providerOptions.{key} to be a boolean; skipping"
        if expected == "string" and not isinstance(value, str):
            return f"{label} expects providerOptions.{key} to be a string; skipping"
    return ""


def supported_durations(mode_caps: Mapping[str, Any] | None) -> tuple[int, ...]:
    values = (mode_caps or {}).get("supported_duration_seconds") or ()
    return tuple(
        sorted({round(each) for each in values if isinstance(each, int | float) and each > 0})
    )


class VideoOverrides:
    """OpenClaw's `resolveVideoGenerationOverrides` for one vendor."""

    __slots__ = ("audio", "duration_seconds", "geometry", "ignored", "notes", "watermark")

    def __init__(self) -> None:
        self.geometry = Geometry()
        self.duration_seconds = 0
        self.audio: bool | None = None
        self.watermark: bool | None = None
        self.ignored: list[tuple[str, Any]] = []
        self.notes: list[str] = []


def resolve_video_overrides(
    mode_caps: Mapping[str, Any] | None,
    *,
    size: str,
    aspect_ratio: str,
    resolution: str,
    duration_seconds: int,
    audio: bool | None,
    watermark: bool | None,
) -> VideoOverrides:
    out = VideoOverrides()
    out.geometry = resolve_geometry(
        size=size or None,
        aspect_ratio=aspect_ratio or None,
        resolution=resolution or None,
        caps=mode_caps,
        report_unrecognized=True,
        aspect_for_size=True,
    )
    out.ignored = list(out.geometry.ignored)
    out.audio, out.watermark = audio, watermark
    if mode_caps is not None and audio is not None and not mode_caps.get("supports_audio"):
        out.ignored.append(("audio", audio))
        out.audio = None
    if mode_caps is not None and watermark is not None and not mode_caps.get("supports_watermark"):
        out.ignored.append(("watermark", watermark))
        out.watermark = None
    if duration_seconds:
        supported = supported_durations(mode_caps)
        applied = select_duration(duration_seconds, supported) if supported else duration_seconds
        if applied != duration_seconds:
            listed = "/".join(map(str, supported))
            out.notes.append(
                f"durationSeconds {duration_seconds} was made as {applied} (it makes {listed})."
            )
        out.duration_seconds = applied
    for key, (requested, applied, derived) in out.geometry.normalized.items():
        if derived == "size":
            out.notes.append(f"aspectRatio {applied} was used for size {size}.")
        elif derived:
            out.notes.append(f"{key} {applied} was used for aspectRatio.")
        else:
            out.notes.append(f"{key} {requested} was made as {applied}.")
    if out.ignored:
        dropped = ", ".join(f"{key}={_shown_value(value)}" for key, value in out.ignored)
        out.notes.append(f"Ignored, not supported: {dropped}.")
    return out


def _shown_value(value: Any) -> str:
    return str(value).lower() if isinstance(value, bool) else str(value)


def reference_inputs(arguments: Mapping[str, Any], kind: str) -> tuple[list[str], list[str]]:
    """OpenClaw's `readVideoReferenceInputs`: the singular then the plural, an
    `@` ignored, repeats kept (a role is by position), too many refused, and the
    roles beside them - an empty slot leaves a role unset."""
    single = "audioRef" if kind == "audio" else kind
    plural = f"{single}s"
    named = []
    if isinstance(arguments.get(single), str):
        named.append(arguments[single])
    named += [each for each in arguments.get(plural) or () if isinstance(each, str)]
    found = [each.strip().removeprefix("@").strip() for each in named]
    found = [each for each in found if each]
    if len(found) > MAX_REFERENCES[kind]:
        raise ToolError(
            f"Too many reference {plural}: {len(found)} provided, "
            f"maximum is {MAX_REFERENCES[kind]}."
        )
    raw_roles = arguments.get(f"{kind}Roles")
    if raw_roles is None:
        return found, []
    if not isinstance(raw_roles, list):
        raise ToolError(
            f"{kind}Roles must be a JSON array of role strings, parallel to the reference list."
        )
    roles = [each.strip() if isinstance(each, str) else "" for each in raw_roles]
    if len(roles) > len(found):
        raise ToolError(
            f"{kind}Roles has {len(roles)} entries but only {len(found)} reference "
            f"{kind}{'' if len(found) == 1 else 's'} were provided; extra roles cannot be "
            "aligned positionally."
        )
    return found, roles


class VideoGenerate(Tool):
    """OpenClaw's `video_generate`: one tool, three actions - start a video,
    see this session's jobs, list the vendors."""

    name = "video_generate"
    untrusted = True
    """When every vendor passes, the result names each one's refusal, and a
    refusal carries a vendor's error code; why a job failed is one too."""

    def __init__(self, runner: Videogen) -> None:
        self.runner = runner
        self.workspace = runner.workspace

    def _audio_references(self) -> bool:
        """OpenClaw shows the reference-audio fields only when a vendor here
        takes them. Asked of the builders, never of a built vendor: building
        one reads its key, and a schema is not a reason to read a key. A
        backend says so with `reference_audio = True` on its builder."""
        return any(
            getattr(builder, "reference_audio", False) is True
            for builder in self.runner.builders().values()
        )

    @property
    def description(self) -> str:  # type: ignore[override]
        audio = "; audio refs condition sound" if self._audio_references() else ""
        return (
            "Create video, incl. image-to-video: image refs take first_frame/last_frame/"
            f"reference_image roles; video refs condition style{audio}. resolution up to 4K; "
            "audio/watermark toggles. action=list discovers providers/models. Runs in the "
            "background: call once per request; you are told when it is saved, so give a short "
            "ack and carry on - no poll. status shows this session's jobs. Duration may round "
            "to provider value. Each video costs money: no variations nobody asked for."
        )

    @property
    def parameters(self) -> dict[str, Any]:  # type: ignore[override]
        properties: dict[str, Any] = {
            "action": {
                "type": "string",
                "description": '"generate" default, "status" active task, "list" providers/models.',
            },
            "prompt": {"type": "string", "description": "Video prompt."},
            "image": {"type": "string", "description": "One reference image path/URL."},
            "images": {
                "type": "array",
                "items": {"type": "string"},
                "description": f"Reference images; max {MAX_REFERENCES['image']}.",
            },
            "imageRoles": {
                "type": "array",
                "items": {"type": "string"},
                "description": "`image` + `images` roles by index. Values: first_frame, "
                "last_frame, reference_image; empty string leaves unset.",
            },
            "video": {"type": "string", "description": "One reference video path/URL."},
            "videos": {
                "type": "array",
                "items": {"type": "string"},
                "description": f"Reference videos; max {MAX_REFERENCES['video']}.",
            },
            "videoRoles": {
                "type": "array",
                "items": {"type": "string"},
                "description": "`video` + `videos` roles by index. Value: reference_video; "
                "empty string leaves unset.",
            },
            "audioRef": {
                "type": "string",
                "description": "One reference audio path/URL, e.g. music.",
            },
            "audioRefs": {
                "type": "array",
                "items": {"type": "string"},
                "description": f"Reference audios; max {MAX_REFERENCES['audio']}.",
            },
            "audioRoles": {
                "type": "array",
                "items": {"type": "string"},
                "description": "`audioRef` + `audioRefs` roles by index. Value: "
                "reference_audio; empty string leaves unset.",
            },
            "model": {
                "type": "string",
                "description": MODEL_ARGUMENT.format(
                    vendors=", ".join(self.runner.builders()),
                    example="google/veo-3.1-generate-preview",
                ),
            },
            "filename": {
                "type": "string",
                "description": "Output filename hint; basename preserved in managed media dir.",
            },
            "size": {"type": "string", "description": "Size hint, e.g. 1280x720, 1920x1080."},
            "aspectRatio": {
                "type": "string",
                "description": 'Aspect ratio: 1:1, 16:9, 9:16, "adaptive", or provider value; '
                "unsupported normalized/ignored.",
            },
            "resolution": {
                "type": "string",
                "description": "Resolution: 360P, 480P, 540P, 720P, 768P, 1080P, 4K, or "
                "provider value; unsupported normalized/ignored.",
            },
            "durationSeconds": {
                "type": "integer",
                "minimum": 1,
                "description": "Target seconds; may round to nearest supported duration.",
            },
            "audio": {"type": "boolean", "description": "Generated-audio toggle."},
            "watermark": {"type": "boolean", "description": "Watermark toggle."},
            "providerOptions": {
                "type": "object",
                "description": 'Provider JSON options, e.g. {"seed":42}. Keys/types must match '
                "provider capabilities; mismatch skips candidate. Use action=list for "
                "accepted keys.",
            },
            "timeoutMs": {
                "type": "integer",
                "minimum": 1,
                "description": "Provider timeout ms.",
            },
        }
        if not self._audio_references():
            for key in ("audioRef", "audioRefs", "audioRoles"):
                del properties[key]
        return {"type": "object", "properties": properties}

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
        references = {kind: reference_inputs(checked, kind) for kind in ("image", "video", "audio")}
        for kind, (inputs, _) in references.items():
            for named in inputs:
                if not _is_url(named) and not named.startswith("data:"):
                    inside(self.workspace, _local(named, kind))
        duration = checked.get("durationSeconds")
        if duration is not None and int(duration) < 1:
            raise ToolError("durationSeconds must be a positive integer")
        options = checked.get("providerOptions")
        if options is not None and not isinstance(options, Mapping):
            raise ToolError(
                "providerOptions must be a JSON object keyed by provider-specific option name."
            )
        timeout_ms = checked.get("timeoutMs")
        if timeout_ms is not None and int(timeout_ms) < 1:
            raise ToolError("timeoutMs must be a positive integer in milliseconds.")
        provider, model = _choice(checked)
        return {
            "action": action,
            "prompt": prompt,
            "references": {kind: [list(a), list(b)] for kind, (a, b) in references.items()},
            "size": str(checked.get("size", "") or "").strip(),
            "aspect_ratio": str(checked.get("aspectRatio", "") or "").strip(),
            "resolution": normalize_resolution(str(checked.get("resolution", "") or "")),
            "duration_seconds": int(duration) if duration is not None else 0,
            "audio": checked.get("audio"),
            "watermark": checked.get("watermark"),
            "provider_options": dict(options or {}),
            "timeout_ms": int(timeout_ms) if timeout_ms is not None else 0,
            "filename": str(checked.get("filename", "") or "").strip(),
            "provider": provider,
            "model": model,
        }

    async def run(  # type: ignore[override]
        self,
        action: str = "generate",
        prompt: str = "",
        references: Mapping[str, Any] | None = None,
        size: str = "",
        aspect_ratio: str = "",
        resolution: str = "",
        duration_seconds: int = 0,
        audio: bool | None = None,
        watermark: bool | None = None,
        provider_options: Mapping[str, Any] | None = None,
        timeout_ms: int = 0,
        filename: str = "",
        provider: str = "",
        model: str = "",
    ) -> ToolResult:
        self.runner.resume()
        if action == "list":
            return self._list()
        if action == "status":
            return self._jobs()
        runner = self.runner
        try:
            loaded = {
                kind: tuple(
                    [
                        await self._asset(kind, named, roles[index] if index < len(roles) else "")
                        for index, named in enumerate(inputs)
                    ]
                )
                for kind, (inputs, roles) in (references or {}).items()
            }
            target = self._target(filename, prompt)
        except ToolError as exc:
            return ToolResult.error(str(exc))
        images = loaded.get("image", ())
        videos = loaded.get("video", ())
        audios = loaded.get("audio", ())
        timeout = runner.timeout(timeout_ms)
        passed: list[str] = []
        for vendor in runner.vendors(provider, model):
            label = f"{vendor.name}/{vendor.model}" if vendor.model else vendor.name
            missing = vendor.ready()
            if missing:
                passed.append(f"{vendor.name}: {missing}")
                continue
            caps = vendor.capabilities
            mode_caps = mode_capabilities(caps, len(images), len(videos))
            declared = (mode_caps or {}).get("provider_options", caps.get("provider_options"))
            skip = capability_failure(label, caps, images, len(videos), len(audios))
            skip = skip or options_failure(
                label, provider_options or {}, declared if isinstance(declared, Mapping) else None
            )
            most = (mode_caps or {}).get("max_duration_seconds", caps.get("max_duration_seconds"))
            if (
                not skip
                and duration_seconds
                and not supported_durations(mode_caps)
                and isinstance(most, int | float)
                and duration_seconds > most
            ):
                skip = (
                    f"{label} supports at most {most:g}s per video, {duration_seconds}s "
                    "requested; skipping"
                )
            if skip:
                passed.append(skip)
                continue
            shaped = resolve_video_overrides(
                mode_caps,
                size=size,
                aspect_ratio=aspect_ratio,
                resolution=resolution,
                duration_seconds=duration_seconds,
                audio=audio,
                watermark=watermark,
            )
            request = Request(
                prompt,
                images,
                videos,
                audios,
                size=shaped.geometry.size or "",
                aspect_ratio=shaped.geometry.aspect_ratio or "",
                resolution=shaped.geometry.resolution or "",
                duration_seconds=shaped.duration_seconds,
                audio=shaped.audio,
                watermark=shaped.watermark,
                provider_options=provider_options,
                timeout=timeout,
            )
            refused = vendor.cannot(request)
            if refused:
                passed.append(f"{vendor.name}: {refused}")
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
                return self._submitted(
                    vendor, remote, request, target, passed, started, " ".join(shaped.notes)
                )
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
        notes: str = "",
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
            notes=notes,
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
            "when it is ready, usually in one to several minutes, and you will be told then."
        )
        if notes:
            line += f" {notes}"
        if passed:
            line += f" Passed over {'; '.join(passed)}."
        return ToolResult.ok(line)

    async def _asset(self, kind: str, named: str, role: str) -> Asset:
        """A reference: a workspace path, a `file://` URL inside the workspace,
        a `data:` URL, or an http(s) URL fetched under the operator's address
        policy - OpenClaw's four. The bytes go to a vendor and never to the
        model."""
        most = REFERENCE_MAX_BYTES[kind]
        url = ""
        if named.startswith("data:"):
            data, label = _data_url(named), "data URL"
        elif _is_url(named):
            data, label, url = await _fetch(named, most), urlsplit(named).hostname or "URL", named
        else:
            target = inside(self.workspace, _local(named, kind))
            try:
                data = target.read_bytes()
            except FileNotFoundError:
                raise ToolError(f"no such file: {named}") from None
            except (IsADirectoryError, PermissionError):
                raise ToolError(f"cannot read {named}") from None
            label = target.name
        if len(data) > most:
            raise ToolError(f"{named} is over {most // (1024 * 1024)} MB")
        media_type = SNIFF[kind](data)
        if not media_type or (kind == "image" and media_type not in FRAME_TYPES):
            raise ToolError(f"{named} is not {KINDS[kind]}")
        return Asset(data, media_type, role, label, url)

    def _target(self, filename: str, prompt: str) -> str:
        """Where the video will go, decided now so the result can say so:
        `output_dir`, under the basename of the `filename` hint (OpenClaw's
        managed media dir) or a name made from the time and the prompt. A name
        already taken, or claimed by a running job, gets `-2`, `-3`."""
        reserved = self.runner.reserved()
        folder = inside(
            self.workspace, str(self.runner.ctx.setting("output_dir", "videos") or "videos")
        )
        stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(filename.replace("\\", "/")).stem)
        stem = stem.strip(".-")[:80]
        if not stem:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            slug = re.sub(r"[^a-z0-9]+", "-", prompt.lower()).strip("-")[:40].strip("-")
            stem = f"{stamp}-{slug or 'video'}"
        # Joined as text, not `with_suffix`: a stem with a dot in it ("v1.2")
        # would lose its tail, and every `-n` would come back the same name.
        target = folder / f"{stem}.mp4"
        n = 2
        while _taken(target, reserved, self.workspace, VIDEO_SUFFIXES):
            target = folder / f"{stem}-{n}.mp4"
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
        """This session's jobs, newest first - OpenClaw's `status`, the
        session's task."""
        runner = self.runner
        jobs = runner.mine()[-10:]
        if not jobs:
            return ToolResult.ok("No video jobs in this session.")
        for each in jobs:
            runner.told(each)
        return ToolResult.ok("\n".join(_line(each) for each in reversed(jobs)))


def _taken(target: Path, reserved: set[str], workspace: Path, suffixes: tuple[str, ...]) -> bool:
    """Whether `target`'s name is spoken for under any extension the file
    could come back as. The extension is the vendor's to decide, so a name is
    free only when no file and no running job holds it as any of them - else
    the result would name one file and the save, finding the real extension
    taken, would write another."""
    stem = target.name.removesuffix(target.suffix)
    rel = target.parent.relative_to(workspace).as_posix()
    for suffix in suffixes:
        if (target.parent / f"{stem}{suffix}").exists() or f"{rel}/{stem}{suffix}" in reserved:
            return True
    return False


def _is_url(named: str) -> bool:
    return named.lower().startswith(("http://", "https://"))


def _local(named: str, kind: str) -> str:
    """A path, or a `file://` URL's path."""
    if named.lower().startswith("file://"):
        path = unquote(urlsplit(named).path)
        # file:///C:/x on Windows is the path C:/x.
        return path[1:] if re.match(r"^/[A-Za-z]:", path) else path
    if re.match(r"^[a-z][a-z0-9+.-]*:", named, re.IGNORECASE) and not re.match(
        r"^[a-z]:[\\/]", named, re.IGNORECASE
    ):
        raise ToolError(
            f"Unsupported {kind} reference: {named}. Use a file path, a file:// URL, a data: "
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


async def _fetch(url: str, most: int) -> bytes:
    from ultron.sdk.web import get

    try:
        response = await get(url, max_bytes=most, timeout=120.0, user_agent="ultron-videogen")
    except Exception as exc:  # the address policy, or the network
        raise ToolError(f"could not fetch {url}: {type(exc).__name__}") from None
    if response.status >= 400:
        raise ToolError(f"HTTP {response.status} fetching {url}")
    if response.truncated:
        raise ToolError(f"{url} is over {most // (1024 * 1024)} MB")
    return response.body


def _submission(vendor: Vendor, request: Request) -> dict[str, Any]:
    """What a submission record says. Never the prompt: it is the tool call's
    own argument, already in that record."""
    arguments: dict[str, Any] = {
        "vendor": vendor.name,
        "model": vendor.model,
        "images": len(request.images),
        "videos": len(request.videos),
        "audios": len(request.audios),
    }
    for key, value in (
        ("size", request.size),
        ("aspectRatio", request.aspect_ratio),
        ("resolution", request.resolution),
        ("durationSeconds", request.duration_seconds),
        ("audio", request.audio),
        ("watermark", request.watermark),
    ):
        if value or value is False:
            arguments[key] = value
    if request.provider_options:
        arguments["providerOptions"] = sorted(request.provider_options)
    return arguments


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
