"""imagegen: make and edit pictures, on OpenAI or Google, saved in the workspace.

A directory plugin written against `ultron.sdk` and nothing else. It brings one
tool, `generate_image`, and two vendors behind it:

- `OpenAIImages` - the Images API: words to `/images/generations` as JSON,
  pictures and a mask to `/images/edits` as multipart.
- `GoogleImages` - Gemini's `generateContent` with an `IMAGE` modality, pictures
  inline beside the prompt; an `imagen-*` model goes to Imagen's `:predict`.

Keys come from `ctx.credential`, asked at each call so a key added mid-session
is the one spent. The picture is written into the workspace, put in the media
store with `ctx.media.put` so it survives a reload, and handed back as an
`ImageResult` so the model sees it. Every vendor attempt is a `generate`
record through `ctx.audit`, beside the tool call's own record.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ultron.sdk.plugin_entry import Plugin, PluginContext
from ultron.sdk.runtime import ToolError, assert_active
from ultron.sdk.tool_plugin import ImageResult, Tool, ToolResult, validate_arguments, wrap_open

ASPECTS = ("square", "landscape", "portrait")
MAX_INPUTS = 8
"""Pictures one call may hand a vendor. Each is bytes leaving the machine."""
MAX_INPUT_BYTES = 50 * 1024 * 1024
"""OpenAI's per-image limit, and so the most one picture to edit may weigh."""
REPLY_MAX_BYTES = 64 * 1024 * 1024
"""A picture comes back as base64, a third larger than its bytes."""
VENDORS = ("openai", "google")
HOSTS = {"openai": "api.openai.com", "google": "generativelanguage.googleapis.com"}
"""Where each vendor's bytes came from, for the envelope's source label."""

EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}


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


# -- what crosses to a vendor ----------------------------------------------------


class Source:
    """A workspace picture handed to a vendor to work from.

    Plain classes rather than dataclasses throughout: Ultron imports a
    directory plugin without putting it in `sys.modules`, and `@dataclass`
    looks its module up there."""

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
    __slots__ = ("aspect", "images", "mask", "prompt", "timeout")

    def __init__(
        self,
        prompt: str,
        images: tuple[Source, ...] = (),
        mask: Source | None = None,
        aspect: str = "",
        timeout: float = 120.0,
    ) -> None:
        self.prompt = prompt
        self.images = images
        self.mask = mask
        self.aspect = aspect
        self.timeout = timeout


class Made:
    __slots__ = ("cost", "data", "model")

    def __init__(self, data: bytes, model: str = "", cost: str = "") -> None:
        self.data = data
        self.model = model
        self.cost = cost


# -- OpenAI ----------------------------------------------------------------------

OPENAI_URL = "https://api.openai.com/v1/images"
OPENAI_SIZES = {"square": "1024x1024", "landscape": "1536x1024", "portrait": "1024x1536"}
OPENAI_QUALITIES = ("auto", "low", "medium", "high")


class OpenAIImages:
    name = "openai"
    edits = True
    masks = True

    def __init__(
        self,
        *,
        model: str = "",
        quality: str = "",
        api_key: str | None = None,
        auth_token: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.model = (model or "gpt-image-2").strip()
        self.quality = (quality or "auto").strip().lower()
        self._key = api_key or ""
        self._subscription = bool(auth_token) and not api_key
        self._url = f"{base_url.rstrip('/')}/images" if base_url else OPENAI_URL

    def ready(self) -> str:
        if self._subscription:
            return "a ChatGPT subscription cannot make pictures; add an OpenAI API key"
        if not self._key:
            return "no openai key (ultron auth add openai)"
        if self.quality not in OPENAI_QUALITIES:
            return f"openai_quality {self.quality!r} is not one of: {', '.join(OPENAI_QUALITIES)}"
        return ""

    async def generate(self, request: Request) -> Made:
        from ultron.sdk.web import post

        if not request.prompt.strip():
            raise ValueError("nothing to make: the prompt is empty")
        fields = {
            "model": self.model,
            "prompt": request.prompt,
            "size": OPENAI_SIZES.get(request.aspect, "auto"),
            "quality": self.quality,
        }
        headers = {"Authorization": f"Bearer {self._key}"}
        if request.images:
            boundary = f"----ultron{uuid.uuid4().hex}"
            body = bytearray()
            for name, value in {**fields, "n": "1"}.items():
                body += (
                    f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
                    f"\r\n\r\n{value}\r\n"
                ).encode()
            uploads = [("image[]", image) for image in request.images]
            if request.mask is not None:
                uploads.append(("mask", request.mask))
            for index, (field, image) in enumerate(uploads):
                ext = EXTENSIONS.get(image.media_type, "png")
                body += (
                    f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; '
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
                json={**fields, "n": 1},
                headers=headers,
                timeout=request.timeout,
                max_bytes=REPLY_MAX_BYTES,
                user_agent="ultron-imagegen",
            )
        parsed = _json(response.body)
        if response.status >= 400:
            raise RuntimeError(_openai_error(parsed, response.status))
        rows = parsed.get("data")
        first = rows[0] if isinstance(rows, list) and rows else None
        encoded = first.get("b64_json") if isinstance(first, Mapping) else None
        if not encoded:
            raise RuntimeError("the images endpoint sent no picture")
        usage = parsed.get("usage")
        cost = ""
        if isinstance(usage, Mapping) and usage.get("total_tokens") is not None:
            cost = f"{usage['total_tokens']} tokens"
        return Made(base64.b64decode(str(encoded)), model=self.model, cost=cost)


def _openai_error(body: Mapping[str, Any], status: int) -> str:
    """The status and OpenAI's error code and type - identifiers, never its
    prose, which would reach the model as a tool result."""
    error = body.get("error")
    said = []
    if isinstance(error, Mapping):
        said = [str(error.get(key) or "") for key in ("code", "type")]
    named = ", ".join(dict.fromkeys(part for part in said if part))
    return f"HTTP {status} from the images endpoint" + (f" ({named})" if named else "")


# -- Google ----------------------------------------------------------------------

GOOGLE_URL = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_ASPECTS = {"square": "1:1", "landscape": "3:2", "portrait": "2:3"}
IMAGEN_ASPECTS = {"square": "1:1", "landscape": "4:3", "portrait": "3:4"}
"""Imagen takes no 3:2; its nearest is 4:3."""


class GoogleImages:
    name = "google"
    masks = False

    def __init__(
        self,
        *,
        model: str = "",
        api_key: str | None = None,
        auth_token: str | None = None,
    ) -> None:
        self.model = (model or "gemini-3.1-flash-image-preview").strip().removeprefix("models/")
        if auth_token and auth_token.startswith("AIza"):
            api_key, auth_token = auth_token, None
        self._key = api_key or ""
        self._signed_in = bool(auth_token) and not api_key

    @property
    def imagen(self) -> bool:
        return self.model.startswith("imagen-")

    @property
    def edits(self) -> bool:
        return not self.imagen

    def ready(self) -> str:
        if self._signed_in:
            return "a Google sign-in cannot make pictures; add an AI Studio key"
        if not self._key:
            return "no google key (ultron auth add google)"
        return ""

    async def generate(self, request: Request) -> Made:
        prompt = request.prompt.strip()
        if not prompt:
            raise ValueError("nothing to make: the prompt is empty")
        if request.mask is not None:
            raise ValueError("a Google image model takes no mask")
        if self.imagen:
            if request.images:
                raise ValueError(f"{self.model} makes pictures from words only")
            parameters: dict[str, Any] = {"sampleCount": 1}
            if request.aspect in IMAGEN_ASPECTS:
                parameters["aspectRatio"] = IMAGEN_ASPECTS[request.aspect]
            reply = await self._call(
                f"{GOOGLE_URL}/models/{self.model}:predict",
                {"instances": [{"prompt": prompt}], "parameters": parameters},
                request.timeout,
            )
            rows = reply.get("predictions")
            first = rows[0] if isinstance(rows, list) and rows else None
            if isinstance(first, Mapping) and first.get("bytesBase64Encoded"):
                return Made(base64.b64decode(str(first["bytesBase64Encoded"])), model=self.model)
            raise RuntimeError(f"{self.model} sent no picture")
        parts: list[dict[str, Any]] = [{"text": prompt}]
        parts += [
            {
                "inlineData": {
                    "mimeType": image.media_type,
                    "data": base64.b64encode(image.data).decode("ascii"),
                }
            }
            for image in request.images
        ]
        config: dict[str, Any] = {"responseModalities": ["TEXT", "IMAGE"]}
        if request.aspect in GEMINI_ASPECTS:
            config["imageConfig"] = {"aspectRatio": GEMINI_ASPECTS[request.aspect]}
        reply = await self._call(
            f"{GOOGLE_URL}/models/{self.model}:generateContent",
            {"contents": [{"role": "user", "parts": parts}], "generationConfig": config},
            request.timeout,
        )
        for part in _gemini_parts(reply):
            inline = part.get("inlineData")
            if (
                isinstance(inline, Mapping)
                and inline.get("data")
                and not part.get("thought")
                and str(inline.get("mimeType", "")).startswith("image/")
            ):
                return Made(base64.b64decode(str(inline["data"])), model=self.model)
        raise RuntimeError(f"{self.model} sent no picture")

    async def _call(self, url: str, body: Mapping[str, Any], timeout: float) -> Mapping[str, Any]:
        from ultron.sdk.web import post

        response = await post(
            url,
            json=body,
            headers={"x-goog-api-key": self._key},
            timeout=timeout,
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
        return parsed


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


class GenerateImage(Tool):
    name = "generate_image"
    untrusted = True
    """The picture is a vendor's bytes. A result with a picture carries its own
    envelope (`_deliver`); any other result the executor wraps whole."""

    def __init__(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self.workspace = Path(ctx.workspace).resolve()
        self.vendors: Callable[[], list[Any]] = self._vendors

    @property
    def description(self) -> str:  # type: ignore[override]
        return (
            "Make a picture with OpenAI or Google and save it in the workspace. Use it when "
            "the person asks for an image, an illustration, a logo, or a change to a picture "
            "they have. Describe the picture in the prompt - subject, style, composition, any "
            "text it must show. To edit or work from existing pictures, name workspace files "
            "in `images`; with a `mask` (OpenAI only), only its transparent area of the first "
            "image is repainted. Each call is one picture and costs money: do not make "
            "variations nobody asked for. You are shown the picture you made - look before "
            "saying it is right - and the result names the file; give the person that path."
        )

    @property
    def parameters(self) -> dict[str, Any]:  # type: ignore[override]
        return {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "What to make, or what to change in the pictures given.",
                },
                "images": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": f"Up to {MAX_INPUTS} workspace image paths to edit or work "
                    "from. Leave it out to make a picture from the prompt alone.",
                },
                "mask": {
                    "type": "string",
                    "description": "A workspace PNG the size of the first image; its "
                    "transparent pixels are the area to repaint. Only with `images`.",
                },
                "aspect": {
                    "type": "string",
                    "enum": list(ASPECTS),
                    "description": "The picture's shape. Leave it out for the vendor's default.",
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
        prompt = str(checked.get("prompt", "") or "")
        if not prompt.strip():
            raise ToolError("generate_image needs a prompt")
        images = [str(p).strip() for p in checked.get("images") or () if str(p).strip()]
        if len(images) > MAX_INPUTS:
            raise ToolError(f"generate_image takes at most {MAX_INPUTS} images, not {len(images)}")
        mask = str(checked.get("mask", "") or "").strip()
        if mask and not images:
            raise ToolError("a mask needs the image it masks in `images`")
        aspect = str(checked.get("aspect", "") or "").strip().lower()
        if aspect and aspect not in ASPECTS:
            raise ToolError(f"aspect must be one of: {', '.join(ASPECTS)}")
        path = str(checked.get("path", "") or "").strip()
        for named in (*images, *([mask] if mask else []), *([path] if path else [])):
            inside(self.workspace, named)
        return {"prompt": prompt, "images": images, "mask": mask, "aspect": aspect, "path": path}

    async def run(  # type: ignore[override]
        self,
        prompt: str,
        images: list[str] | None = None,
        mask: str = "",
        aspect: str = "",
        path: str = "",
    ) -> ToolResult:
        try:
            sources = tuple(self._source(named) for named in images or ())
            masked = self._source(mask) if mask else None
        except ToolError as exc:
            return ToolResult.error(str(exc))
        if path and inside(self.workspace, path).exists():
            return ToolResult.error(f"{path} already exists; name a new file")
        request = Request(prompt, sources, masked, aspect, self._timeout())
        passed: list[str] = []
        for vendor in self.vendors():
            missing = vendor.ready() or _cannot(vendor, request)
            if missing:
                passed.append(f"{vendor.name}: {missing}")
                continue
            assert_active()
            made, error, took = await self._ask(vendor, request)
            media_type = sniff(made.data) if made is not None else ""
            if made is not None and not error and not media_type:
                error = "what came back is not a PNG, JPEG, GIF or WebP picture"
            self._audit(vendor.name, request, made, media_type, error, took)
            if made is None or error:
                passed.append(f"{vendor.name}: {error}")
                continue
            return self._deliver(made, media_type, vendor.name, prompt, path, passed)
        failure = "; ".join(passed) or "no vendor is configured"
        return ToolResult.error(f"no picture made: {failure}")

    # -- the parts -------------------------------------------------------------

    def _vendors(self) -> list[Any]:
        """Both vendors, built fresh with the key each holds now, the preferred
        one first."""
        ctx = self.ctx
        built = {
            "openai": OpenAIImages(
                model=str(ctx.setting("openai_model", "") or ""),
                quality=str(ctx.setting("openai_quality", "") or ""),
                **ctx.credential("openai"),
            ),
            "google": GoogleImages(
                model=str(ctx.setting("google_model", "") or ""),
                **_key_only(ctx.credential("google")),
            ),
        }
        first = str(ctx.setting("provider", "") or "").strip().lower()
        order = [first] if first in built else []
        order += [name for name in VENDORS if name not in order]
        return [built[name] for name in order]

    def _timeout(self) -> float:
        try:
            seconds = float(self.ctx.setting("timeout_seconds", 120) or 120)
        except (TypeError, ValueError):
            seconds = 120.0
        return max(5.0, min(600.0, seconds))

    def _source(self, named: str) -> Source:
        target = inside(self.workspace, named)
        try:
            data = target.read_bytes()
        except FileNotFoundError:
            raise ToolError(f"no such file: {named}") from None
        except (IsADirectoryError, PermissionError):
            raise ToolError(f"cannot read {named}") from None
        if len(data) > MAX_INPUT_BYTES:
            raise ToolError(f"{named} is over {MAX_INPUT_BYTES // (1024 * 1024)} MB")
        media_type = sniff(data)
        if not media_type:
            raise ToolError(f"{named} is not a PNG, JPEG, GIF or WebP picture")
        return Source(data, media_type, target.name)

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
        if not isinstance(made, Made) or not made.data:
            return None, "nothing came back", time.monotonic() - started
        return made, "", time.monotonic() - started

    def _audit(
        self,
        vendor: str,
        request: Request,
        made: Made | None,
        media_type: str,
        error: str,
        took: float,
    ) -> None:
        """One record per vendor asked. Never the prompt: it is the tool call's
        own argument, already in that record."""
        import hashlib

        arguments: dict[str, Any] = {
            "vendor": vendor,
            "images": len(request.images),
            "mask": request.mask is not None,
        }
        if request.aspect:
            arguments["aspect"] = request.aspect
        if made is not None and made.model:
            arguments["model"] = made.model
        if made is not None and not error:
            arguments.update(
                sha256=hashlib.sha256(made.data).hexdigest(),
                bytes=len(made.data),
                media_type=media_type,
            )
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
        media_type: str,
        vendor: str,
        prompt: str,
        path: str,
        passed: list[str],
    ) -> ToolResult:
        target = self._target(path, prompt, media_type)
        assert_active()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as handle:
                handle.write(made.data)
        except FileExistsError:
            return ToolResult.error(f"{self._shown(target)} already exists; name a new file")
        except OSError as exc:
            return ToolResult.error(f"made by {vendor} but not saved: {exc}")
        who = vendor + (f" ({made.model})" if made.model else "")
        line = f"[picture made by {who}, {media_type}, {_human(len(made.data))}]"
        line += f" saved to {self._shown(target)}"
        if passed:
            line += f"; passed over {'; '.join(passed)}"
        store = self.ctx.media
        if store is None:
            return ToolResult.ok(f"{line}. Pictures are off here, so it is not shown.")
        try:
            block = store.put(made.data, source=f"made by {vendor}")
        except Exception as exc:  # over images_max_bytes, or pictures off at the door
            return ToolResult.ok(f"{line}. Not shown: {exc}")
        # The picture inside an envelope, the line about it outside: the same
        # shape `view_image` gives a fetched picture. Wrapped here because the
        # executor, wrapping an untrusted result itself, would drop `images`.
        envelope = wrap_open("", source=HOSTS.get(vendor, vendor))
        return ImageResult(content=line, images=(block,), envelope=envelope, wrapped=True)

    def _target(self, path: str, prompt: str, media_type: str) -> Path:
        ext = EXTENSIONS.get(media_type, "png")
        if path:
            named = inside(self.workspace, path)
            if named.suffix.lower().lstrip(".") in (ext, "jpeg" if ext == "jpg" else ext):
                return named
            return named.with_suffix(f".{ext}")
        folder = str(self.ctx.setting("output_dir", "images") or "images")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        slug = re.sub(r"[^a-z0-9]+", "-", prompt.lower()).strip("-")[:40].strip("-") or "image"
        base = inside(self.workspace, folder) / f"{stamp}-{slug}"
        target = base.with_suffix(f".{ext}")
        n = 2
        while target.exists():
            target = base.with_name(f"{base.name}-{n}").with_suffix(f".{ext}")
            n += 1
        return target

    def _shown(self, path: Path) -> str:
        try:
            return path.relative_to(self.workspace).as_posix()
        except ValueError:
            return str(path)


def _cannot(vendor: Any, request: Request) -> str:
    if request.images and not getattr(vendor, "edits", False):
        return "does not edit pictures"
    if request.mask is not None and not getattr(vendor, "masks", False):
        return "does not take a mask"
    return ""


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
    description = "Make and edit pictures with OpenAI or Google, saved in the workspace."

    def register(self, ctx: PluginContext) -> None:
        ctx.register_tool(GenerateImage(ctx))
