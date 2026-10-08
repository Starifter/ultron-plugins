"""imagegen: make and edit pictures, on any vendor a key is held for, saved in the workspace.

A directory plugin written against `ultron.sdk` and nothing else. It brings one
tool, `image_generate` - OpenClaw's, field for field - two vendors of its own,
and a point any other plugin can put a vendor into:

- `OpenAIImages` - the Images API: words to `/images/generations` as JSON,
  pictures to `/images/edits` as multipart.
- `GoogleImages` - Gemini's `generateContent` with an `IMAGE` modality,
  pictures inline beside the prompt.
- `imagegen.backend` - every other vendor, registered by the plugin that owns
  it with `ctx.register_extension("imagegen.backend", name, build)` (SDK 1.39).
  The `xai`, `openrouter`, `together` and `fireworks` provider plugins do; the
  interface is in `PLUGIN.md`, and nothing here names them.

What the model asks for is held against what each vendor says it can do
(`capabilities`, OpenClaw's shape): a size, a shape or a resolution it does not
take is moved to the nearest one it does, a quality, a format or a background
it does not take is dropped, and the result says which. A vendor that cannot
take the pictures handed in is passed over rather than sent a request it would
answer without them. That is OpenClaw's `resolveImageGenerationOverrides`,
ported below with its geometry; the functions keep OpenClaw's names in
snake_case so the two can be read side by side.

OpenAI and Google stay here because their plugins ship inside Ultron, which
does not know imagegen exists. Their keys come from `ctx.credential`; a backend
reads its own with its own plugin's. Either way a key is read when its vendor
is reached, so a key added mid-session is the one spent and a vendor never
reached is never read. The pictures are written into the workspace, put in the
media store with `ctx.media.put` so they survive a reload, and handed back as
an `ImageResult` so the model sees them. Every vendor attempt is a `generate`
record through `ctx.audit`, beside the tool call's own record.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import inspect
import io
import json
import math
import re
import struct
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from ultron.sdk.plugin_entry import Plugin, PluginContext
from ultron.sdk.runtime import ToolError, assert_active
from ultron.sdk.tool_plugin import ImageResult, Tool, ToolResult, validate_arguments, wrap_open

# -- OpenClaw's vocabulary -------------------------------------------------------

MAX_COUNT = 4
MAX_REFERENCE_IMAGES = 16
QUALITIES = ("low", "medium", "high", "xhigh", "max", "auto")
OUTPUT_FORMATS = ("png", "jpeg", "webp")
BACKGROUNDS = ("transparent", "opaque", "auto")
OPENAI_MODERATIONS = ("low", "auto")
RESOLUTIONS = ("1K", "2K", "4K")
ASPECT_RATIOS = (
    "1:1",
    "2:1",
    "20:9",
    "19.5:9",
    "2:3",
    "3:2",
    "2.35:1",
    "3:4",
    "4:3",
    "4:5",
    "5:4",
    "9:16",
    "9:19.5",
    "9:20",
    "16:9",
    "21:9",
    "1:2",
    "4:1",
    "1:4",
    "8:1",
    "1:8",
)
ACTIONS = ("generate", "status", "list")

REFERENCE_MAX_BYTES = 50 * 1024 * 1024
"""OpenAI's per-image limit, and so the most one reference picture may weigh,
read from the workspace or fetched."""
REPLY_MAX_BYTES = 4 * 64 * 1024 * 1024
"""Up to four pictures as base64, each a third larger than its bytes."""
BUILT_IN = ("openai", "google")
"""imagegen's own vendors, tried in this order before any backend another
plugin registered, when `provider` names none."""
POINT = "imagegen.backend"
"""Where another plugin puts a vendor (`ctx.register_extension`, SDK 1.39)."""
HOSTS = {
    "openai": "api.openai.com",
    "google": "generativelanguage.googleapis.com",
}
"""Where each built-in vendor's bytes came from, for the envelope's source
label. A backend says its own with `host`."""

EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}
MODEL_ARGUMENT = (
    "Provider/model override, e.g. {example}; transparent OpenAI: openai/gpt-image-1.5. The "
    "providers here: {vendors}. If it fails, the others are tried on their own models and "
    "the result says so."
)
LISTED_MODELS = 20
"""The most extra model ids one vendor's line names."""
VENDOR_NAME = re.compile(r"[a-z0-9_-]{1,64}")
"""What the provider half of `model` may say: an extension's name is lower
case, letters, digits, `_` and `-`."""
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}")
"""What `model` may say. The model chose it and it goes into a vendor's URL
path - `models/<id>:generateContent` - so nothing that steps out of a path
segment or starts a query: no `..`, `//`, `?`, `#`, `%` or space."""


def sniff(data: bytes) -> str:
    """The image type the bytes are, or empty. Believed over any declaration."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def dimensions(data: bytes) -> tuple[int, int]:
    """A picture's width and height from its header, or (0, 0). Enough for
    `infer_resolution`; nothing is decoded."""
    try:
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return struct.unpack(">II", data[16:24])
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return struct.unpack("<HH", data[6:10])
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            chunk = data[12:16]
            if chunk == b"VP8X":
                width = int.from_bytes(data[24:27], "little") + 1
                return width, int.from_bytes(data[27:30], "little") + 1
            if chunk == b"VP8L":
                bits = int.from_bytes(data[21:25], "little")
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
            if chunk == b"VP8 ":
                width, height = struct.unpack("<HH", data[26:30])
                return width & 0x3FFF, height & 0x3FFF
        if data.startswith(b"\xff\xd8"):
            stream = io.BytesIO(data[2:])
            while True:
                marker = stream.read(2)
                if len(marker) < 2 or marker[0] != 0xFF:
                    return 0, 0
                if marker[1] in (0xD8, 0x01) or 0xD0 <= marker[1] <= 0xD7:
                    continue
                (length,) = struct.unpack(">H", stream.read(2))
                if 0xC0 <= marker[1] <= 0xCF and marker[1] not in (0xC4, 0xC8, 0xCC):
                    stream.read(1)
                    height, width = struct.unpack(">HH", stream.read(4))
                    return width, height
                stream.seek(length - 2, io.SEEK_CUR)
    except (struct.error, ValueError):
        pass
    return 0, 0


# -- geometry, OpenClaw's -------------------------------------------------------
#
# `src/media-generation/runtime-shared.ts` and `geometry-normalization.ts`:
# the closest supported aspect ratio by log distance, the closest size by shape
# then area, the closest resolution within its unit. videogen carries the same
# functions; a plugin imports `ultron.sdk` and nothing of another plugin's.

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


def infer_resolution(images: Sequence[Source]) -> str:
    """OpenClaw's `inferImageGenerationResolution`: an edit is made at the size
    of the largest picture handed in."""
    largest = max((max(dimensions(image.data)) for image in images), default=0)
    if largest >= 3000:
        return "4K"
    if largest >= 1500:
        return "2K"
    return "1K"


# -- what crosses to a vendor ----------------------------------------------------


class Source:
    """A reference picture handed to a vendor to work from.

    Plain classes rather than dataclasses throughout. Ultron registers a
    directory plugin in `sys.modules` before running it only since
    Starifter/ultron#6, and that came without an SDK bump: an install at SDK
    1.38 from before it imports this file unregistered, where `@dataclass`
    under `from __future__ import annotations` fails to load."""

    __slots__ = ("data", "media_type", "name")

    def __init__(self, data: bytes, media_type: str, name: str = "") -> None:
        self.data = data
        self.media_type = media_type
        self.name = name

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Source) and (self.data, self.media_type, self.name) == (
            other.data,
            other.media_type,
            other.name,
        )

    __hash__ = None  # type: ignore[assignment]


class Request:
    """What one vendor is asked for, after its capabilities had their say.

    `aspect` and `mask` are what a backend written for imagegen 3.x read; they
    are always empty now, so such a backend makes its default shape and is
    told about no mask."""

    __slots__ = (
        "aspect",
        "aspect_ratio",
        "background",
        "count",
        "images",
        "mask",
        "openai",
        "output_format",
        "prompt",
        "quality",
        "resolution",
        "size",
        "timeout",
    )

    def __init__(
        self,
        prompt: str,
        images: tuple[Source, ...] = (),
        *,
        count: int = 1,
        size: str = "",
        aspect_ratio: str = "",
        resolution: str = "",
        quality: str = "",
        output_format: str = "",
        background: str = "",
        openai: Mapping[str, Any] | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.prompt = prompt
        self.images = images
        self.count = count
        self.size = size
        self.aspect_ratio = aspect_ratio
        self.resolution = resolution
        self.quality = quality
        self.output_format = output_format
        self.background = background
        self.openai = dict(openai or {})
        self.timeout = timeout
        self.aspect = ""
        self.mask = None


class Made:
    """What a vendor made: one or more pictures. A backend may return any object
    with `images` (each with `data`) or, as before 4.0, a single `data`; and
    optionally `model` and `cost`. `_ask` reads it into one of these."""

    __slots__ = ("cost", "images", "model")

    def __init__(self, images: Sequence[bytes], model: str = "", cost: str = "") -> None:
        self.images = list(images)
        self.model = model
        self.cost = cost

    @property
    def data(self) -> bytes:
        return self.images[0] if self.images else b""


def _caps(vendor: Any) -> dict[str, Any]:
    """A vendor's `capabilities`, OpenClaw's shape, read defensively: another
    plugin's code may say nothing, say it badly, or raise. One written before
    4.0 declares none, and is read from what it did say - `edits` and
    `max_images` - as a vendor that makes one picture and takes no geometry,
    quality, format or background, so each of those is dropped and reported
    rather than sent to code that would not read it."""
    try:
        said = getattr(vendor, "capabilities", None)
        if isinstance(said, Mapping):
            return {key: dict(value) for key, value in said.items() if isinstance(value, Mapping)}
    except Exception:
        pass
    try:
        edits = bool(getattr(vendor, "edits", False))
        most = int(getattr(vendor, "max_images", 8) or 0)
    except Exception:
        edits, most = False, 0
    none = {"supports_size": False, "supports_aspect_ratio": False, "supports_resolution": False}
    return {
        "generate": {"max_count": 1, **none},
        "edit": {"enabled": edits, "max_count": 1, "max_input_images": most, **none},
        "geometry": {},
        "output": {},
    }


class Overrides:
    """OpenClaw's `resolveImageGenerationOverrides` for one vendor: what it is
    sent, and what the result says was moved or dropped."""

    __slots__ = ("background", "geometry", "ignored", "output_format", "quality")

    def __init__(self, geometry: Geometry, quality: str, output_format: str, background: str):
        self.geometry = geometry
        self.quality = quality
        self.output_format = output_format
        self.background = background
        self.ignored = list(geometry.ignored)


def resolve_overrides(
    caps: Mapping[str, Any],
    *,
    edit: bool,
    size: str,
    aspect_ratio: str,
    resolution: str,
    quality: str,
    output_format: str,
    background: str,
) -> Overrides:
    mode = dict(caps.get("edit" if edit else "generate") or {})
    geometry = dict(caps.get("geometry") or {})
    mode.update(
        sizes=geometry.get("sizes"),
        aspect_ratios=geometry.get("aspect_ratios"),
        resolutions=geometry.get("resolutions"),
    )
    shaped = resolve_geometry(
        size=size or None,
        aspect_ratio=aspect_ratio or None,
        resolution=resolution or None,
        caps=mode,
        fallback_sizes=geometry.get("fallback_sizes"),
    )
    output = dict(caps.get("output") or {})
    out = Overrides(shaped, quality, output_format, background)
    for key, value, offered in (
        ("quality", quality, output.get("qualities")),
        ("outputFormat", output_format, output.get("formats")),
        ("background", background, output.get("backgrounds")),
    ):
        if value and value not in (offered or ()):
            out.ignored.append((key, value))
            setattr(out, _ATTRIBUTE[key], "")
    return out


_ATTRIBUTE = {"quality": "quality", "outputFormat": "output_format", "background": "background"}


# -- OpenAI ----------------------------------------------------------------------

OPENAI_URL = "https://api.openai.com/v1/images"
OPENAI_DEFAULT_MODEL = "gpt-image-2"
OPENAI_TRANSPARENT_MODEL = "gpt-image-1.5"
"""OpenClaw's: gpt-image-2 makes no transparent background, so a transparent
picture asked of the default model is made on 1.5."""
OPENAI_SIZES = (
    "1024x1024",
    "1536x1024",
    "1024x1536",
    "2048x2048",
    "2048x1152",
    "3840x2160",
    "2160x3840",
)
OPENAI_LEGACY_SIZES = ("1024x1024", "1536x1024", "1024x1536")
OPENAI_LEGACY_MODELS = ("gpt-image-1", "gpt-image-1-mini")
OPENAI_25_MODELS = ("gpt-image-2.5-flare", "gpt-image-2.5-sunburst")
OPENAI_FLEXIBLE_MODELS = (OPENAI_DEFAULT_MODEL, *OPENAI_25_MODELS, "gpt-image-2-2026-04-21")
"""Models that take any size within OpenAI's limits, which OpenClaw does not
snap to the list."""
OPENAI_QUALITIES = ("low", "medium", "high", "auto")
OPENAI_25_QUALITIES = ("low", "medium", "high", "xhigh", "max", "auto")
OPENAI_DEFAULT_SIZE = "1024x1024"
OPENAI_MIME = {"jpeg": "image/jpeg", "webp": "image/webp", "png": "image/png"}


def _flexible_size_ok(model: str, size: str) -> bool:
    """OpenClaw's `isValidFlexibleOpenAIImageSize`."""
    if model not in OPENAI_FLEXIBLE_MODELS:
        return False
    if size == "auto":
        return model in OPENAI_25_MODELS
    parsed = parse_size(size)
    if not parsed:
        return False
    width, height, _, pixels = parsed
    return (
        width % 16 == 0
        and height % 16 == 0
        and max(width, height) <= 3840
        and 655_360 <= pixels <= 8_294_400
        and width <= height * 3
        and height <= width * 3
    )


class OpenAIImages:
    name = "openai"

    def __init__(
        self,
        *,
        model: str = "",
        quality: str = "",
        api_key: str | None = None,
        auth_token: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.model = (model or OPENAI_DEFAULT_MODEL).strip()
        self.quality = (quality or "").strip().lower()
        """The person's `openai_quality`, sent when the model asks for none."""
        self._key = api_key or ""
        self._subscription = bool(auth_token) and not api_key
        self._url = f"{base_url.rstrip('/')}/images" if base_url else OPENAI_URL

    @property
    def models(self) -> tuple[str, ...]:
        return (OPENAI_DEFAULT_MODEL, *OPENAI_25_MODELS, OPENAI_TRANSPARENT_MODEL, "gpt-image-1")

    @property
    def capabilities(self) -> dict[str, Any]:
        """OpenClaw's OpenAI image provider, for this model."""
        mode = {
            "supports_size": True,
            "supports_aspect_ratio": False,
            "supports_resolution": False,
        }
        if self.model in OPENAI_FLEXIBLE_MODELS:
            sizes: tuple[str, ...] = ()
        elif self.model in OPENAI_LEGACY_MODELS:
            sizes = OPENAI_LEGACY_SIZES
        else:
            sizes = OPENAI_SIZES
        return {
            "generate": {"max_count": 4, **mode},
            "edit": {"enabled": True, "max_count": 4, "max_input_images": 5, **mode},
            "geometry": {"sizes": sizes, "fallback_sizes": OPENAI_SIZES},
            "output": {
                "formats": OUTPUT_FORMATS,
                "qualities": OPENAI_25_QUALITIES
                if self.model in OPENAI_25_MODELS
                else OPENAI_QUALITIES,
                "backgrounds": BACKGROUNDS,
            },
        }

    def ready(self) -> str:
        if self._subscription:
            return "a ChatGPT subscription cannot make pictures; add an OpenAI API key"
        if not self._key:
            return "no openai key (ultron auth add openai)"
        if self.quality and self.quality not in OPENAI_QUALITIES:
            return f"openai_quality {self.quality!r} is not one of: {', '.join(OPENAI_QUALITIES)}"
        return ""

    def _model_for(self, request: Request) -> str:
        background = request.openai.get("background") or request.background
        if self.model == OPENAI_DEFAULT_MODEL and background == "transparent":
            return OPENAI_TRANSPARENT_MODEL
        return self.model

    def _size_for(self, model: str, request: Request) -> str:
        """OpenClaw's `resolveOpenAIImageRequestSize`: a flexible model keeps a
        size within OpenAI's limits; anything else goes to the nearest one the
        model makes."""
        size = request.size or OPENAI_DEFAULT_SIZE
        if _flexible_size_ok(model, size):
            return size
        native = OPENAI_LEGACY_SIZES if model in OPENAI_LEGACY_MODELS else OPENAI_SIZES
        return closest_size(size, None, native) or OPENAI_DEFAULT_SIZE

    async def generate(self, request: Request) -> Made:
        from ultron.sdk.web import post

        if not request.prompt.strip():
            raise ValueError("nothing to make: the prompt is empty")
        model = self._model_for(request)
        options = request.openai
        background = options.get("background") or request.background
        fields: dict[str, Any] = {
            "model": model,
            "prompt": request.prompt,
            "size": self._size_for(model, request),
        }
        quality = request.quality or self.quality
        if quality:
            fields["quality"] = quality
        if request.output_format:
            fields["output_format"] = request.output_format
        if background:
            fields["background"] = background
        if options.get("moderation"):
            fields["moderation"] = options["moderation"]
        if request.output_format in ("jpeg", "webp") and "outputCompression" in options:
            fields["output_compression"] = options["outputCompression"]
        if options.get("user"):
            fields["user"] = options["user"]
        headers = {"Authorization": f"Bearer {self._key}"}
        n = max(1, min(4, request.count))
        if request.images:
            boundary = f"----ultron{uuid.uuid4().hex}"
            body = bytearray()
            for name, value in {**fields, "n": str(n)}.items():
                body += (
                    f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
                    f"\r\n\r\n{value}\r\n"
                ).encode()
            for index, image in enumerate(request.images):
                ext = EXTENSIONS.get(image.media_type, "png")
                body += (
                    f'--{boundary}\r\nContent-Disposition: form-data; name="image[]"; '
                    f'filename="image{index}.{ext}"\r\nContent-Type: {image.media_type}'
                    "\r\n\r\n"
                ).encode()
                body += image.data
                body += b"\r\n"
            body += f"--{boundary}--\r\n".encode()
            response = await post(
                f"{self._url}/edits",
                data=bytes(body),
                content_type=f"multipart/form-data; boundary={boundary}",
                headers=headers,
                timeout=request.timeout,
                max_bytes=REPLY_MAX_BYTES,
                user_agent="ultron-imagegen",
            )
        else:
            response = await post(
                f"{self._url}/generations",
                json={**fields, "n": n},
                headers=headers,
                timeout=request.timeout,
                max_bytes=REPLY_MAX_BYTES,
                user_agent="ultron-imagegen",
            )
        parsed = _json(response.body)
        if response.status >= 400:
            raise RuntimeError(_openai_error(parsed, response.status))
        usage = parsed.get("usage")
        cost = ""
        if isinstance(usage, Mapping) and usage.get("total_tokens") is not None:
            cost = f"{usage['total_tokens']} tokens"
        return Made(_all_b64(parsed, "the images endpoint"), model=model, cost=cost)


def _openai_error(body: Mapping[str, Any], status: int, where: str = "the images endpoint") -> str:
    """The status and the vendor's error code and type - identifiers, never its
    prose, which would reach the model as a tool result. OpenAI's shape, which
    the OpenAI-compatible vendors share; one that sends `error` as a sentence
    gets the status alone."""
    error = body.get("error")
    said = []
    if isinstance(error, Mapping):
        said = [str(error.get(key) or "") for key in ("code", "type")]
    named = ", ".join(dict.fromkeys(part for part in said if part))
    return f"HTTP {status} from {where}" + (f" ({named})" if named else "")


def _all_b64(parsed: Mapping[str, Any], where: str) -> list[bytes]:
    """Every picture of an OpenAI-shaped `data: [{b64_json}]` reply."""
    rows = parsed.get("data")
    found = (
        [
            base64.b64decode(str(row["b64_json"]))
            for row in rows or ()
            if isinstance(row, Mapping) and row.get("b64_json")
        ]
        if isinstance(rows, list)
        else []
    )
    if not found:
        raise RuntimeError(f"{where} sent no picture")
    return found


# -- Google ----------------------------------------------------------------------

GOOGLE_URL = "https://generativelanguage.googleapis.com/v1beta"
GOOGLE_DEFAULT_MODEL = "gemini-3.1-flash-image"
GOOGLE_SIZES = ("1024x1024", "1024x1536", "1536x1024", "1024x1792", "1792x1024")
GOOGLE_ASPECT_RATIOS = ("1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9")
GOOGLE_SIZE_SHAPES = {
    "1024x1024": "1:1",
    "1024x1536": "2:3",
    "1536x1024": "3:2",
    "1024x1792": "9:16",
    "1792x1024": "16:9",
}


def _google_image_config(size: str) -> dict[str, str]:
    """OpenClaw's `mapSizeToImageConfig`: a size Gemini knows by its shape, and
    a long edge from 1536 up as 2K, from 3072 up as 4K."""
    parsed = parse_size(size)
    if not parsed:
        return {}
    config: dict[str, str] = {}
    shape = GOOGLE_SIZE_SHAPES.get(size.lower())
    if shape:
        config["aspectRatio"] = shape
    edge = max(parsed[0], parsed[1])
    if edge >= 3072:
        config["imageSize"] = "4K"
    elif edge >= 1536:
        config["imageSize"] = "2K"
    return config


class GoogleImages:
    name = "google"
    models = (GOOGLE_DEFAULT_MODEL, "gemini-3-pro-image")
    capabilities = {
        "generate": {
            "max_count": 4,
            "supports_size": True,
            "supports_aspect_ratio": True,
            "supports_resolution": True,
        },
        "edit": {
            "enabled": True,
            "max_count": 4,
            "max_input_images": 5,
            "supports_size": True,
            "supports_aspect_ratio": True,
            "supports_resolution": True,
        },
        "geometry": {
            "sizes": GOOGLE_SIZES,
            "aspect_ratios": GOOGLE_ASPECT_RATIOS,
            "resolutions": RESOLUTIONS,
        },
        "output": {},
    }
    """OpenClaw's Google image provider."""

    def __init__(
        self,
        *,
        model: str = "",
        api_key: str | None = None,
        auth_token: str | None = None,
    ) -> None:
        self.model = (model or GOOGLE_DEFAULT_MODEL).strip().removeprefix("models/")
        if auth_token and auth_token.startswith("AIza"):
            api_key, auth_token = auth_token, None
        self._key = api_key or ""
        self._signed_in = bool(auth_token) and not api_key

    def ready(self) -> str:
        if self._signed_in:
            return "a Google sign-in cannot make pictures; add an AI Studio key"
        if not self._key:
            return "no google key (ultron auth add google)"
        return ""

    async def generate(self, request: Request) -> Made:
        from ultron.sdk.web import post

        prompt = request.prompt.strip()
        if not prompt:
            raise ValueError("nothing to make: the prompt is empty")
        parts: list[dict[str, Any]] = [
            {
                "inlineData": {
                    "mimeType": image.media_type,
                    "data": base64.b64encode(image.data).decode("ascii"),
                }
            }
            for image in request.images
        ]
        parts.append({"text": prompt})
        image_config = _google_image_config(request.size)
        if request.aspect_ratio:
            image_config["aspectRatio"] = request.aspect_ratio
        if request.resolution:
            image_config["imageSize"] = request.resolution
        config: dict[str, Any] = {"responseModalities": ["TEXT", "IMAGE"]}
        if image_config:
            config["imageConfig"] = image_config
        response = await post(
            f"{GOOGLE_URL}/models/{self.model}:generateContent",
            json={"contents": [{"role": "user", "parts": parts}], "generationConfig": config},
            headers={"x-goog-api-key": self._key},
            timeout=request.timeout,
            max_bytes=REPLY_MAX_BYTES,
            user_agent="ultron-imagegen",
        )
        parsed = _json(response.body)
        if response.status >= 400:
            error = parsed.get("error")
            status = error.get("status", "") if isinstance(error, Mapping) else ""
            raise RuntimeError(
                f"HTTP {response.status} from Google" + (f" ({status})" if status else "")
            )
        found = []
        for part in _gemini_parts(parsed):
            inline = part.get("inlineData")
            if (
                isinstance(inline, Mapping)
                and inline.get("data")
                and not part.get("thought")
                and str(inline.get("mimeType", "")).startswith("image/")
            ):
                found.append(base64.b64decode(str(inline["data"])))
        if not found:
            raise RuntimeError(f"{self.model} sent no picture")
        return Made(found, model=self.model)


def _gemini_parts(reply: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    candidates = reply.get("candidates")
    if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], Mapping):
        return []
    content = candidates[0].get("content")
    parts = content.get("parts") if isinstance(content, Mapping) else None
    return [p for p in parts if isinstance(p, Mapping)] if isinstance(parts, list) else []


def _json(raw: bytes) -> Mapping[str, Any]:
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return decoded if isinstance(decoded, Mapping) else {}


# -- the tool --------------------------------------------------------------------


def inside(workspace: Path, path: str) -> Path:
    """`path` resolved against the workspace, refusing anything that escapes it."""
    root = workspace.resolve()
    candidate = Path(path) if Path(path).is_absolute() else root / path
    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise ToolError(f"path {path!r} is outside the workspace ({root})")
    return resolved


def reference_inputs(
    arguments: Mapping[str, Any], single: str, plural: str, most: int
) -> list[str]:
    """OpenClaw's `normalizeMediaReferenceInputs`: `image` then `images`, an
    `@` in front ignored, a repeat dropped, and too many refused."""
    named = []
    if isinstance(arguments.get(single), str):
        named.append(arguments[single])
    named += [each for each in arguments.get(plural) or () if isinstance(each, str)]
    seen: set[str] = set()
    found: list[str] = []
    for each in named:
        key = each.strip().removeprefix("@").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        found.append(key)
    if len(found) > most:
        raise ToolError(f"Too many reference images: {len(found)} provided, maximum is {most}.")
    return found


class ImageGenerate(Tool):
    name = "image_generate"
    untrusted = True
    """The picture is a vendor's bytes. A result with a picture carries its own
    envelope (`_deliver`); any other result the executor wraps whole."""

    def __init__(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self.workspace = Path(ctx.workspace).resolve()
        self.vendors: Callable[[str, str], Iterable[tuple[str, Any]]] = self._vendors

    @property
    def description(self) -> str:  # type: ignore[override]
        return (
            "Create/edit images. Batch via count; aspectRatio and resolution up to 4K. Runs in "
            "the call: you are shown what you made - look before saying it is right - and the "
            "result names the files; give the person those paths. A value a vendor cannot take "
            "is moved to its nearest or dropped, and the result says which. Transparent: "
            'outputFormat png|webp + background="transparent"; OpenAI also openai.background, '
            "default gpt-image-1.5. Each picture costs money: no variations nobody asked for. "
            "action=list providers/models/readiness; status active task."
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
                "prompt": {"type": "string", "description": "Image prompt."},
                "image": {
                    "type": "string",
                    "description": "Reference image path/URL for edit.",
                },
                "images": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Reference images for edit or style reference; max "
                    f"{MAX_REFERENCE_IMAGES}.",
                },
                "model": {
                    "type": "string",
                    "description": MODEL_ARGUMENT.format(
                        vendors=", ".join(self._names()), example="openai/gpt-image-2"
                    ),
                },
                "filename": {
                    "type": "string",
                    "description": "Output filename hint; basename preserved in managed media dir.",
                },
                "size": {
                    "type": "string",
                    "description": "Size hint: 1024x1024, 1536x1024, 1024x1536, 2048x2048, "
                    "3840x2160.",
                },
                "aspectRatio": {
                    "type": "string",
                    "description": f"Aspect ratio: {', '.join(ASPECT_RATIOS)}.",
                },
                "resolution": {
                    "type": "string",
                    "description": "Resolution: 1K, 2K, 4K; useful for Google.",
                },
                "quality": {
                    "type": "string",
                    "enum": list(QUALITIES),
                    "description": "Quality: low, medium, high, xhigh, max, auto; model-specific.",
                },
                "outputFormat": {
                    "type": "string",
                    "enum": list(OUTPUT_FORMATS),
                    "description": "Output format: png, jpeg, webp.",
                },
                "background": {
                    "type": "string",
                    "enum": list(BACKGROUNDS),
                    "description": "Background: transparent, opaque, auto. Transparent needs "
                    "png/webp output.",
                },
                "openai": {
                    "type": "object",
                    "properties": {
                        "background": {
                            "type": "string",
                            "enum": list(BACKGROUNDS),
                            "description": "OpenAI background: transparent, opaque, auto. "
                            "Transparent needs png/webp; default model routes to gpt-image-1.5.",
                        },
                        "moderation": {
                            "type": "string",
                            "enum": list(OPENAI_MODERATIONS),
                            "description": "OpenAI moderation: low, auto.",
                        },
                        "outputCompression": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 100,
                            "description": "OpenAI jpeg/webp compression 0-100.",
                        },
                        "user": {
                            "type": "string",
                            "description": "OpenAI stable end-user id.",
                        },
                    },
                },
                "count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_COUNT,
                    "description": f"Image count 1-{MAX_COUNT}.",
                },
                "timeoutMs": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Provider timeout ms (300000 tends to be a safe amount).",
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
        prompt = str(checked.get("prompt", "") or "")
        if not prompt.strip():
            raise ToolError("image_generate needs a prompt")
        references = reference_inputs(checked, "image", "images", MAX_REFERENCE_IMAGES)
        aspect_ratio = str(checked.get("aspectRatio", "") or "").strip()
        if aspect_ratio and aspect_ratio not in ASPECT_RATIOS:
            raise ToolError(f"aspectRatio must be one of: {', '.join(ASPECT_RATIOS)}")
        resolution = str(checked.get("resolution", "") or "").strip().upper()
        if resolution and resolution not in RESOLUTIONS:
            raise ToolError("resolution must be one of 1K, 2K, or 4K")
        openai = checked.get("openai") or {}
        for key, value, allowed in (
            ("quality", checked.get("quality"), QUALITIES),
            ("outputFormat", checked.get("outputFormat"), OUTPUT_FORMATS),
            ("background", checked.get("background"), BACKGROUNDS),
            ("openai.background", openai.get("background"), BACKGROUNDS),
            ("openai.moderation", openai.get("moderation"), OPENAI_MODERATIONS),
        ):
            if value and value not in allowed:
                raise ToolError(f"{key} must be one of: {', '.join(allowed)}")
        compression = openai.get("outputCompression")
        if compression is not None and not 0 <= int(compression) <= 100:
            raise ToolError("openai.outputCompression must be between 0 and 100")
        count = int(checked.get("count") or 1)
        if not 1 <= count <= MAX_COUNT:
            raise ToolError(f"count must be between 1 and {MAX_COUNT}")
        timeout_ms = checked.get("timeoutMs")
        if timeout_ms is not None and int(timeout_ms) < 1:
            raise ToolError("timeoutMs must be a positive integer in milliseconds.")
        filename = str(checked.get("filename", "") or "").strip()
        for named in references:
            if not _remote(named) and not named.startswith("data:"):
                inside(self.workspace, _local(named))
        provider, model = _choice(checked)
        return {
            "action": action,
            "prompt": prompt,
            "references": references,
            "size": str(checked.get("size", "") or "").strip(),
            "aspect_ratio": aspect_ratio,
            "resolution": resolution,
            "quality": str(checked.get("quality", "") or ""),
            "output_format": str(checked.get("outputFormat", "") or ""),
            "background": str(checked.get("background", "") or ""),
            "openai": {key: value for key, value in openai.items() if value is not None},
            "count": count,
            "timeout_ms": int(timeout_ms) if timeout_ms is not None else 0,
            "filename": filename,
            "provider": provider,
            "model": model,
        }

    async def run(  # type: ignore[override]
        self,
        action: str = "generate",
        prompt: str = "",
        references: list[str] | None = None,
        size: str = "",
        aspect_ratio: str = "",
        resolution: str = "",
        quality: str = "",
        output_format: str = "",
        background: str = "",
        openai: Mapping[str, Any] | None = None,
        count: int = 1,
        timeout_ms: int = 0,
        filename: str = "",
        provider: str = "",
        model: str = "",
    ) -> ToolResult:
        if action == "list":
            return ToolResult.ok(self._listing())
        if action == "status":
            return ToolResult.ok(
                "No image task is running: image_generate makes its pictures in the call."
            )
        try:
            sources = tuple([await self._source(named) for named in references or ()])
        except ToolError as exc:
            return ToolResult.error(str(exc))
        timeout = self._timeout(timeout_ms)
        edit = bool(sources)
        inferred = "" if size or resolution or not edit else infer_resolution(sources)
        chosen = provider or str(self.ctx.setting("provider", "") or "").strip().lower()
        passed: list[str] = []
        for name, vendor in self.vendors(provider, model):
            missing = _unready(vendor)
            caps = _caps(vendor)
            mode = caps.get("edit" if edit else "generate") or {}
            most = int(mode.get("max_count") or MAX_COUNT)
            if not missing and name == chosen and count > most:
                # OpenClaw's: the vendor asked for by name or by the person is held
                # to its own limit, before anything is spent anywhere.
                kind = "edit" if edit else "generate"
                return ToolResult.error(
                    f"{name} {kind} supports at most {most} output image{'s' if most != 1 else ''}."
                )
            missing = missing or _cannot(caps, len(sources))
            if missing:
                passed.append(f"{name}: {missing}")
                continue
            # OpenClaw's: an inferred resolution goes only to a vendor that may
            # take one, so it is never reported as something the model asked for.
            offered = (caps.get("geometry") or {}).get("resolutions")
            takes = mode.get("supports_resolution") is not False and (
                offered is None or len(offered) > 0
            )
            asked_resolution = resolution or (inferred if takes else "")
            shaped = resolve_overrides(
                caps,
                edit=edit,
                size=size,
                aspect_ratio=aspect_ratio,
                resolution=asked_resolution,
                quality=quality,
                output_format=output_format,
                background=background,
            )
            request = Request(
                prompt,
                sources,
                count=min(count, most),
                size=shaped.geometry.size or "",
                aspect_ratio=shaped.geometry.aspect_ratio or "",
                resolution=shaped.geometry.resolution or "",
                quality=shaped.quality,
                output_format=shaped.output_format,
                background=shaped.background,
                openai=openai,
                timeout=timeout,
            )
            assert_active()
            made, error, took = await self._ask(vendor, request)
            kinds = [sniff(each) for each in made.images] if made is not None else []
            if made is not None and not error and not all(kinds):
                error = "what came back is not a PNG, JPEG, GIF or WebP picture"
            self._audit(name, request, made, kinds, error, took)
            if made is None or error:
                passed.append(f"{name}: {error}")
                continue
            host = str(getattr(vendor, "host", "") or HOSTS.get(name, name))
            notes = _adjusted(shaped, size)
            return self._deliver(made, kinds, name, host, prompt, filename, passed, notes, count)
        failure = "; ".join(passed) or "no vendor is configured"
        return ToolResult.error(f"no picture made: {failure}")

    # -- the parts -------------------------------------------------------------

    def _builders(self) -> dict[str, Callable[..., Any]]:
        """Every vendor's builder by name: the built-ins, then the backends
        other plugins registered, in their install order. Read now, not at
        `register`: a plugin installed after this one, or enabled since, is in;
        one disabled since is out. A backend registered under a built-in's name
        stands in for it."""
        builders: dict[str, Callable[..., Any]] = {name: self._builder(name) for name in BUILT_IN}
        builders.update(self.ctx.extensions_in(POINT))
        return builders

    def _names(self) -> list[str]:
        return list(self._builders())

    def _listing(self) -> str:
        lines = [
            "Image vendors, in the order they are asked. `model` takes provider/model; a "
            "provider alone is the model shown."
        ]
        for name, vendor in self.vendors("", ""):
            lines.append(_listed(name, vendor, _abilities(_caps(vendor))))
        return "\n".join(lines)

    def _vendors(self, provider: str = "", model: str = "") -> Iterator[tuple[str, Any]]:
        """Every vendor by name: the one the model named, then the one the
        person configured, then the rest in `_builders` order. Each is built
        with the key it holds now only when it is reached - a vendor after the
        one that answered is never built, so its key is never read.

        `model` goes to the vendor the model named and to no other: an id means
        something only at its own vendor. A named vendor that is not here, or
        whose builder cannot take a model, is passed over like one with no key."""
        builders = self._builders()
        configured = str(self.ctx.setting("provider", "") or "").strip().lower()
        if provider and provider not in builders:
            yield provider, _Broken(f"not here - the vendors are {', '.join(builders)}")
        order = [name for name in dict.fromkeys((provider, configured)) if name in builders]
        order += [name for name in builders if name not in order]
        for name in order:
            chosen = model if name == provider else ""
            if chosen and not _takes_model(builders[name]):
                yield name, _Broken(f"its plugin cannot be asked for {chosen}; update it")
                continue
            try:
                yield name, builders[name](model=chosen) if chosen else builders[name]()
            except Exception as exc:  # another plugin's code; it fails as a vendor fails
                yield name, _Broken(f"could not be built: {type(exc).__name__}: {exc}")

    def _builder(self, name: str) -> Callable[..., Any]:
        ctx = self.ctx

        def configured() -> str:
            return str(ctx.setting(f"{name}_model", "") or "")

        if name == "openai":
            return lambda model="": OpenAIImages(
                model=model or configured(),
                quality=str(ctx.setting("openai_quality", "") or ""),
                **ctx.credential("openai"),
            )
        return lambda model="": GoogleImages(
            model=model or configured(), **_key_only(ctx.credential("google"))
        )

    def _timeout(self, timeout_ms: int = 0) -> float:
        """`timeoutMs` when the model gave one, else the person's
        `timeout_seconds`; either way one attempt at one vendor, 1 to 3600
        seconds."""
        if timeout_ms:
            return max(1.0, min(3600.0, timeout_ms / 1000))
        try:
            seconds = float(self.ctx.setting("timeout_seconds", 120) or 120)
        except (TypeError, ValueError):
            seconds = 120.0
        return max(5.0, min(600.0, seconds))

    async def _source(self, named: str) -> Source:
        """A reference picture: a workspace path, a `file://` URL inside the
        workspace, a `data:` URL, or an http(s) URL fetched under the
        operator's address policy - OpenClaw's four. The bytes are sent to a
        vendor and never shown to the model, so a fetched one needs no
        envelope here."""
        if named.startswith("data:"):
            data = _data_url(named)
            label = "data URL"
        elif _remote(named):
            data = await _fetch(named)
            label = urlsplit(named).hostname or "URL"
        else:
            target = inside(self.workspace, _local(named))
            try:
                data = target.read_bytes()
            except FileNotFoundError:
                raise ToolError(f"no such file: {named}") from None
            except (IsADirectoryError, PermissionError):
                raise ToolError(f"cannot read {named}") from None
            label = target.name
        if len(data) > REFERENCE_MAX_BYTES:
            raise ToolError(f"{named} is over {REFERENCE_MAX_BYTES // (1024 * 1024)} MB")
        media_type = sniff(data)
        if not media_type:
            raise ToolError(f"{named} is not a PNG, JPEG, GIF or WebP picture")
        return Source(data, media_type, label)

    async def _ask(self, vendor: Any, request: Request) -> tuple[Made | None, str, float]:
        started = time.monotonic()
        try:
            made = await asyncio.wait_for(vendor.generate(request), timeout=request.timeout)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return None, f"timed out after {request.timeout:g}s", time.monotonic() - started
        except Exception as exc:  # a vendor may fail in any way; the next one is asked
            message = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            return None, message, time.monotonic() - started
        # Read into imagegen's own shape: a backend's object is another
        # plugin's, and nothing past this line should depend on what it is.
        images = getattr(made, "images", None)
        if isinstance(images, list | tuple):
            found = [getattr(each, "data", each) for each in images]
        else:
            found = [getattr(made, "data", None)]
        found = [bytes(each) for each in found if isinstance(each, bytes | bytearray) and each]
        if not found:
            return None, "nothing came back", time.monotonic() - started
        made = Made(
            found[:MAX_COUNT],
            model=str(getattr(made, "model", "") or ""),
            cost=str(getattr(made, "cost", "") or ""),
        )
        return made, "", time.monotonic() - started

    def _audit(
        self,
        vendor: str,
        request: Request,
        made: Made | None,
        kinds: list[str],
        error: str,
        took: float,
    ) -> None:
        """One record per vendor asked. Never the prompt: it is the tool call's
        own argument, already in that record."""
        import hashlib

        arguments: dict[str, Any] = {
            "vendor": vendor,
            "images": len(request.images),
            "count": request.count,
        }
        for key, value in (
            ("size", request.size),
            ("aspectRatio", request.aspect_ratio),
            ("resolution", request.resolution),
            ("quality", request.quality),
            ("outputFormat", request.output_format),
            ("background", request.background),
        ):
            if value:
                arguments[key] = value
        if made is not None and made.model:
            arguments["model"] = made.model
        if made is not None and not error:
            arguments["made"] = [
                {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "media_type": kind}
                for data, kind in zip(made.images, kinds, strict=True)
            ]
        self.ctx.audit(
            "generate",
            error or (made.cost if made is not None else ""),
            outcome="error" if error else "ok",
            arguments=arguments,
            duration_ms=took * 1000,
        )

    def _deliver(
        self,
        made: Made,
        kinds: list[str],
        vendor: str,
        host: str,
        prompt: str,
        filename: str,
        passed: list[str],
        notes: list[str],
        asked: int,
    ) -> ToolResult:
        saved: list[Path] = []
        assert_active()
        for data, kind in zip(made.images, kinds, strict=True):
            target = self._target(filename, prompt, kind)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as handle:
                    handle.write(data)
            except OSError as exc:
                if not saved:
                    return ToolResult.error(f"made by {vendor} but not saved: {exc}")
                break
            saved.append(target)
        who = vendor + (f" ({made.model})" if made.model else "")
        total = sum(len(data) for data in made.images[: len(saved)])
        what = "picture" if len(saved) == 1 else f"{len(saved)} pictures"
        line = f"[{what} made by {who}, {', '.join(dict.fromkeys(kinds))}, {_human(total)}]"
        line += " saved to " + ", ".join(self._shown(path) for path in saved)
        if len(saved) < asked:
            line += f"; {asked} were asked for"
        if passed:
            line += f"; passed over {'; '.join(passed)}"
        if notes:
            line += ". " + " ".join(notes)
        store = self.ctx.media
        if store is None:
            return ToolResult.ok(f"{line}. Pictures are off here, so they are not shown.")
        blocks = []
        for data in made.images[: len(saved)]:
            try:
                blocks.append(store.put(data, source=f"made by {vendor}"))
            except Exception as exc:  # over images_max_bytes, or pictures off at the door
                return ToolResult.ok(f"{line}. Not shown: {exc}")
        # The pictures inside an envelope, the line about them outside: the same
        # shape `view_image` gives a fetched picture. Wrapped here because the
        # executor, wrapping an untrusted result itself, would drop `images`.
        envelope = wrap_open("", source=host)
        return ImageResult(content=line, images=tuple(blocks), envelope=envelope, wrapped=True)

    def _target(self, filename: str, prompt: str, media_type: str) -> Path:
        """Where a picture goes: `output_dir`, under the basename of the
        `filename` hint (OpenClaw's managed media dir), or a name made from the
        time and the prompt. Never an existing file: a name already taken gets
        `-2`, `-3`."""
        ext = EXTENSIONS.get(media_type, "png")
        folder = inside(self.workspace, str(self.ctx.setting("output_dir", "images") or "images"))
        stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(filename.replace("\\", "/")).stem)
        stem = stem.strip(".-")[:80]
        if not stem:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            slug = re.sub(r"[^a-z0-9]+", "-", prompt.lower()).strip("-")[:40].strip("-")
            stem = f"{stamp}-{slug or 'image'}"
        # Joined as text, not `with_suffix`: a stem with a dot in it ("v1.2")
        # would lose its tail, and every `-n` would come back the same name.
        target = folder / f"{stem}.{ext}"
        n = 2
        while target.exists():
            target = folder / f"{stem}-{n}.{ext}"
            n += 1
        return target

    def _shown(self, path: Path) -> str:
        try:
            return path.relative_to(self.workspace).as_posix()
        except ValueError:
            return str(path)


def _adjusted(shaped: Overrides, size: str) -> list[str]:
    """What the result says the vendor was sent instead, Ultron's words about
    Ultron's decisions."""
    notes = []
    for key, (requested, applied, derived) in shaped.geometry.normalized.items():
        if derived == "size":
            notes.append(f"aspectRatio {applied} was used for size {size}.")
        elif derived:
            notes.append(f"{key} {applied} was used for aspectRatio.")
        else:
            notes.append(f"{key} {requested} was made as {applied}.")
    if shaped.ignored:
        dropped = ", ".join(f"{key}={value}" for key, value in shaped.ignored)
        notes.append(f"Ignored, not supported: {dropped}.")
    return notes


def _remote(named: str) -> bool:
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
            url, max_bytes=REFERENCE_MAX_BYTES, timeout=60.0, user_agent="ultron-imagegen"
        )
    except Exception as exc:  # the address policy, or the network
        raise ToolError(f"could not fetch {url}: {type(exc).__name__}") from None
    if response.status >= 400:
        raise ToolError(f"HTTP {response.status} fetching {url}")
    if response.truncated:
        raise ToolError(f"{url} is over {REFERENCE_MAX_BYTES // (1024 * 1024)} MB")
    return response.body


class _Broken:
    """A backend whose builder raised: passed over, with why, like any vendor
    that is not ready."""

    def __init__(self, why: str) -> None:
        self.why = why

    def ready(self) -> str:
        return self.why


def _unready(vendor: Any) -> str:
    """Why a vendor cannot be asked, or empty. A backend is another plugin's
    code: one with no `ready` is ready, and one whose `ready` raises is not."""
    ready = getattr(vendor, "ready", None)
    if ready is None:
        return ""
    try:
        return str(ready() or "")
    except Exception as exc:
        return f"ready() failed: {type(exc).__name__}"


def _cannot(caps: Mapping[str, Any], references: int) -> str:
    """OpenClaw's `resolveReferenceImageCapabilityError`: pictures handed in
    must be taken, or the vendor is passed over - a vendor that dropped them
    would look like it had done the edit."""
    if not references:
        return ""
    edit = caps.get("edit") or {}
    if not edit.get("enabled"):
        return "does not edit pictures"
    most = int(edit.get("max_input_images") or 0)
    if references > most:
        return f"edits {most} picture{'s' if most != 1 else ''} at a time"
    return ""


def _abilities(caps: Mapping[str, Any]) -> list[str]:
    edit = caps.get("edit") or {}
    geometry = caps.get("geometry") or {}
    output = caps.get("output") or {}
    generate = caps.get("generate") or {}
    said = [
        f"edits up to {edit.get('max_input_images')}" if edit.get("enabled") else "",
        f"up to {generate.get('max_count', 1)} at once",
    ]
    if generate.get("supports_size"):
        sizes = geometry.get("sizes")
        said.append(f"sizes {'/'.join(sizes)}" if sizes else "any size")
    if generate.get("supports_aspect_ratio"):
        said.append(f"aspectRatio {'/'.join(geometry.get('aspect_ratios') or ()) or 'any'}")
    if generate.get("supports_resolution"):
        said.append(f"resolution {'/'.join(geometry.get('resolutions') or ()) or 'any'}")
    for key, label in (
        ("qualities", "quality"),
        ("formats", "outputFormat"),
        ("backgrounds", "background"),
    ):
        if output.get(key):
            said.append(f"{label} {'/'.join(output[key])}")
    return said


def _choice(checked: Mapping[str, Any]) -> tuple[str, str]:
    """`model` as the model wrote it, split at its first `/` into a vendor's
    name and that vendor's own id - which may hold more slashes, as
    `openrouter/google/gemini-3.1-flash-image-preview` does. A vendor alone is
    its configured model."""
    named = str(checked.get("model", "") or "").strip()
    provider, _, model = named.partition("/")
    provider = provider.strip().lower()
    if named and not VENDOR_NAME.fullmatch(provider):
        raise ToolError(f"model {named!r} is not provider/model - openai/gpt-image-2, say")
    if model and (not MODEL_ID.fullmatch(model) or ".." in model or "//" in model):
        raise ToolError(f"{model!r} is not a model id")
    return provider, model


def _listed(name: str, vendor: Any, abilities: Iterable[str] = ()) -> str:
    """One vendor's line for `action: list`: `model` as it would take it,
    whether it can be asked, what it does, and any further ids it names.
    Facts the vendors' plugins hold - asking each `ready()` reads its key, and
    nothing is sent anywhere."""
    model = str(getattr(vendor, "model", "") or "")
    line = f"- {name}/{model}" if model else f"- {name}"
    missing = _unready(vendor)
    line += f": cannot be asked - {missing}" if missing else ": ready"
    said = [each for each in abilities if each]
    if said and not missing:
        line += f"; {', '.join(said)}"
    others = [f"{name}/{each}" for each in _models(vendor) if each != model]
    if others:
        line += f"; also {', '.join(others)}"
    return line


def _models(vendor: Any) -> list[str]:
    """The ids a vendor says it also takes (`models`, optional), checked as
    `model` would check them, at most `LISTED_MODELS`. Another plugin's code: one
    that is not a list of ids, or that raises, names none."""
    try:
        said = list(getattr(vendor, "models", ()) or ())
    except Exception:
        return []
    ids = [str(each).strip() for each in said if isinstance(each, str)]
    ids = [each for each in ids if MODEL_ID.fullmatch(each) and ".." not in each]
    return list(dict.fromkeys(ids))[:LISTED_MODELS]


def _takes_model(builder: Callable[..., Any]) -> bool:
    """Whether a builder takes `model=`. A backend written before the model
    could choose takes no arguments, and asking it for one would build the
    vendor's configured model under the name of the one asked for."""
    try:
        parameters = inspect.signature(builder).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == "model" or p.kind is p.VAR_KEYWORD for p in parameters)


def _key_only(credential: Mapping[str, str]) -> dict[str, str]:
    """Google's constructor takes `api_key` and `auth_token`, nothing else."""
    return {k: v for k, v in credential.items() if k in ("api_key", "auth_token")}


def _human(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


class ImagegenPlugin(Plugin):
    name = "imagegen"
    description = "Make and edit pictures with any image vendor a key is held for."

    def register(self, ctx: PluginContext) -> None:
        ctx.register_tool(ImageGenerate(ctx))
