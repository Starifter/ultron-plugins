"""xAI provider - Grok, at xAI.

xAI speaks OpenAI's Chat Completions at `https://api.x.ai/v1`, so this is the SDK's
OpenAI-compatible base with xAI's own parts declared on it (`plugin-sdk.md` §6.7):
the reply ceiling under `max_completion_tokens`, `reasoning_effort` on the models that
reason - which cannot be switched off - and a listing that prices each model, with a
second rate above its long-context threshold.

Requires the `openai` package (`pip install openai`).
"""

from __future__ import annotations

import base64
import json
import re
import uuid
from collections.abc import Mapping
from typing import Any

from ultron.sdk.openai_compat import OpenAICompatProvider
from ultron.sdk.plugin_entry import Plugin, PluginContext
from ultron.sdk.provider import ModelEntry, PriceTier, Pricing, ThinkingLevel

BASE_URL = "https://api.x.ai/v1"
STT_URL = f"{BASE_URL}/stt"
DEFAULT_TRANSCRIPTION_MODEL = "grok-voice-transcribe-2.0"

AUDIO_EXTENSIONS: dict[str, str] = {
    "audio/ogg": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
    "audio/wav": "wav",
    "audio/flac": "flac",
}
"""The audio types Ultron stores that xAI documents taking. WebM is not one of
them, so a `.weba` voice note goes to another reader."""

LEVELS: tuple[ThinkingLevel, ...] = ("low", "medium", "high", "max")
"""xAI's efforts, with its `xhigh` as Ultron's `max`. There is no `off`: a Grok model
that reasons cannot be told not to, and one that does not is a different model id."""

EFFORT: dict[ThinkingLevel, str] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "max": "xhigh",
}
FROM_EFFORT: dict[str, ThinkingLevel] = {value: key for key, value in EFFORT.items()}

CENTS_PER_100M = 10_000
"""xAI prices in US cents per 100 million tokens; the catalog in dollars per million."""


def family_levels(model: str) -> tuple[ThinkingLevel, ...]:
    """The menu before a listing has said: a Grok 4 model reasons at low to high,
    and one named `non-reasoning` - or anything else - has no control."""
    name = model.lower()
    if "non-reasoning" in name:
        return ()
    if name.startswith("grok-4"):
        return ("low", "medium", "high")
    return ()


def _dollars(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        return None
    return float(value) / CENTS_PER_100M


def pricing_of(row: Mapping[str, Any]) -> Pricing | None:
    """The row's text prices, with the long-context rates as a second tier from the
    threshold on. xAI sells no cache write, so it is priced as the nothing it costs."""
    base = Pricing(
        input=_dollars(row.get("prompt_text_token_price")),
        output=_dollars(row.get("completion_text_token_price")),
        cache_read=_dollars(row.get("cached_prompt_text_token_price")),
        cache_write=0.0,
    )
    if base.input is None or base.output is None:
        return None
    threshold = row.get("long_context_threshold")
    long_in = _dollars(row.get("prompt_text_token_price_long_context"))
    long_out = _dollars(row.get("completion_text_token_price_long_context"))
    if not isinstance(threshold, int) or threshold <= 0 or long_in is None or long_out is None:
        return base
    long_cached = _dollars(row.get("cached_prompt_text_token_price_long_context"))
    cached = base.cache_read if base.cache_read is not None else 0.0
    return Pricing(
        input=base.input,
        output=base.output,
        cache_read=base.cache_read,
        cache_write=0.0,
        tiers=(
            PriceTier(0, threshold, base.input, base.output, cached, 0.0),
            PriceTier(
                threshold,
                None,
                long_in,
                long_out,
                long_cached if long_cached is not None else cached,
                0.0,
            ),
        ),
    )


class XAIProvider(OpenAICompatProvider):
    """Chat Completions at xAI, on the SDK's OpenAI-compatible base."""

    name = "xai"
    label = "xAI"
    api_key_env_vars = ("XAI_API_KEY",)
    base_url = BASE_URL
    thinking_levels = LEVELS
    streaming = True
    sampling = False
    """Not declared: Grok's reasoning models refuse `presence_penalty`,
    `frequency_penalty` and `stop` with an error rather than ignoring them."""
    max_tokens_field = "max_completion_tokens"
    """`max_tokens` is deprecated at xAI in favour of this."""

    @classmethod
    def levels_for(cls, model: str) -> tuple[ThinkingLevel, ...]:
        declared = cls.declared_entry(model).thinking_levels
        return family_levels(model) if declared is None else super().levels_for(model)

    def thinking_request(self, level: ThinkingLevel) -> dict[str, Any]:
        if level not in self.thinking_levels:
            return {}
        return {"reasoning_effort": EFFORT[level]}

    def entry_of(self, row: Mapping[str, Any]) -> ModelEntry | None:
        """One row of `GET /models`: the window, the price, and the efforts the
        model takes - an empty list is a model that does not reason."""
        model_id = str(row.get("id", "") or "")
        if not model_id:
            return None
        window = row.get("context_length")
        capabilities = row.get("capabilities")
        efforts = (
            capabilities.get("reasoning_effort") if isinstance(capabilities, Mapping) else None
        )
        levels: tuple[ThinkingLevel, ...] | None = None
        if isinstance(efforts, list):
            named = {FROM_EFFORT[str(e)] for e in efforts if str(e) in FROM_EFFORT}
            levels = tuple(level for level in LEVELS if level in named)
        base = super().entry_of(row)
        return ModelEntry(
            id=model_id,
            context_window=window if isinstance(window, int) and window > 0 else 0,
            thinking_levels=levels,
            cost=pricing_of(row),
            released=base.released if base is not None else "",
        )


class XAITranscriber:
    """xAI's speech-to-text endpoint as a media reader (`media.md` §8.3).

    Named `xai/stt`, so the core hands it the key the `xai` provider would use -
    a profile, or `XAI_API_KEY` in `~/.ultron/.env` - and never reads it on the
    reader's behalf. It sees the bytes and a language hint, never the
    conversation. Priority 45: between `groq/whisper` (40) and `openai/whisper`
    (50), in the order of what an hour of audio costs at each.
    """

    name = "xai/stt"
    kinds: tuple[str, ...] = ("audio",)
    accepts: tuple[str, ...] = tuple(AUDIO_EXTENSIONS)
    priority = 45

    def __init__(
        self,
        *,
        model: str = "",
        api_key: str | None = None,
        auth_token: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.model = (model or DEFAULT_TRANSCRIPTION_MODEL).strip()
        self._key = api_key or auth_token or ""
        self._url = f"{base_url.rstrip('/')}/stt" if base_url else STT_URL

    def ready(self) -> str:
        if not self._key:
            return "no xai key (ultron auth add xai, or XAI_API_KEY in ~/.ultron/.env)"
        return ""

    async def read(self, reading: Any) -> Any:
        from ultron.sdk.media import Understood
        from ultron.sdk.web import post

        fields = [("model", self.model)]
        if reading.language:
            fields.append(("language", reading.language))
        ext = AUDIO_EXTENSIONS.get(reading.media_type, "bin")
        # xAI wants the file as the last field of the form, which `multipart`
        # always puts it.
        body, content_type = multipart(fields, f"audio.{ext}", reading.media_type, reading.data)
        response = await post(
            self._url,
            data=body,
            content_type=content_type,
            headers={"Authorization": f"Bearer {self._key}"},
            timeout=reading.timeout,
            max_bytes=4 * 1024 * 1024,
            user_agent="ultron-xai",
        )
        if response.status >= 400:
            raise RuntimeError(f"HTTP {response.status} from xAI: {_why(response.body)}")
        try:
            parsed = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"unreadable reply from xAI: {exc}") from None
        text = str(parsed.get("text", "") or "") if isinstance(parsed, dict) else ""
        duration = parsed.get("duration") if isinstance(parsed, dict) else None
        cost = f"{duration:.0f}s" if isinstance(duration, int | float) else ""
        return Understood(text, cost=cost)


def multipart(
    fields: list[tuple[str, str]], filename: str, media_type: str, data: bytes
) -> tuple[bytes, str]:
    """A `multipart/form-data` body with the file last, and its content type."""
    boundary = f"----ultron{uuid.uuid4().hex}"
    body = bytearray()
    for name, value in fields:
        body += (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
        ).encode()
    body += (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="{filename}"\r\nContent-Type: {media_type}\r\n\r\n'
    ).encode()
    body += data
    body += f"\r\n--{boundary}--\r\n".encode()
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def _why(body: bytes) -> str:
    """The vendor's own error message, if it sent one, without printing a page."""
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict) and error.get("message"):
                return str(error["message"])[:200]
            if isinstance(error, str) and error:
                return error[:200]
    except ValueError:
        pass
    return body[:200].decode("utf-8", "replace").strip()


# -- pictures and videos, for imagegen and videogen ------------------------------
#
# Grok Imagine, registered into `imagegen.backend` and `videogen.backend` (SDK
# 1.39) so those plugins reach it without knowing xAI exists. The interfaces are
# theirs, written in their PLUGIN.md; nothing here imports either. Each builder
# reads the key when imagegen or videogen reaches xAI, never before.

IMAGES_URL = f"{BASE_URL}/images"
VIDEOS_URL = f"{BASE_URL}/videos"
PICTURE_MAX_BYTES = 64 * 1024 * 1024
"""A picture comes back as base64, a third larger than its bytes."""
VIDEO_MAX_BYTES = 512 * 1024 * 1024
STATUS_MAX_BYTES = 1024 * 1024
POLL_TIMEOUT = 30.0
REMOTE_ID = re.compile(r"[A-Za-z0-9._:/-]{1,300}")
CODE = re.compile(r"[^A-Za-z0-9_.-]+")


class Pictures:
    """What imagegen reads back: `images` (each with `data`), the model, a cost.
    `data` is the first, for an imagegen before 4.0."""

    __slots__ = ("cost", "images", "model")

    def __init__(self, images: list[bytes], model: str = "", cost: str = "") -> None:
        self.images = [Picture(data) for data in images]
        self.model = model
        self.cost = cost

    @property
    def data(self) -> bytes:
        return self.images[0].data


class Picture:
    __slots__ = ("data",)

    def __init__(self, data: bytes) -> None:
        self.data = data


class Status:
    """Where a video job stands, as videogen reads one."""

    __slots__ = ("cost", "error", "state", "url")

    def __init__(self, state: str, url: str = "", error: str = "", cost: str = "") -> None:
        self.state = state
        self.url = url
        self.error = error
        self.cost = cost


class Retry(Exception):
    """A request worth making again - the network, a 5xx, a 429. videogen
    retries an exception whose `retry` is true."""

    retry = True


XAI_IMAGE_ASPECT_RATIOS = (
    "1:1",
    "16:9",
    "9:16",
    "4:3",
    "3:4",
    "3:2",
    "2:3",
    "2:1",
    "1:2",
    "19.5:9",
    "9:19.5",
    "20:9",
    "9:20",
)
XAI_VIDEO_ASPECT_RATIOS = ("1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3")
XAI_VIDEO_15_MODELS = (
    "grok-imagine-video-1.5",
    "grok-imagine-video-1.5-preview",
    "grok-imagine-video-1.5-2026-05-30",
)


def _wants(request: Any, name: str, default: Any = "") -> Any:
    """A field of imagegen's or videogen's request, or its default when an
    older one sent a request without it."""
    return getattr(request, name, default)


class XAIImages:
    """Grok Imagine pictures: `/images/generations`, and `/images/edits` as
    JSON with the pictures as data URIs - OpenClaw's xAI image provider."""

    host = "api.x.ai"
    capabilities = {
        "generate": {
            "max_count": 4,
            "supports_size": False,
            "supports_aspect_ratio": True,
            "supports_resolution": True,
        },
        "edit": {
            "enabled": True,
            "max_count": 4,
            "max_input_images": 3,
            "supports_size": False,
            "supports_aspect_ratio": True,
            "supports_resolution": True,
        },
        "geometry": {"aspect_ratios": XAI_IMAGE_ASPECT_RATIOS, "resolutions": ("1K", "2K")},
        "output": {},
    }
    edits = True
    max_images = 3
    """What an imagegen before 4.0 read."""

    def __init__(
        self, *, model: str = "", api_key: str | None = None, auth_token: str | None = None
    ) -> None:
        self.model = (model or "grok-imagine-image-2.0").strip()
        self._key = api_key or auth_token or ""

    def ready(self) -> str:
        return "" if self._key else "no xai key (ultron auth add xai)"

    async def generate(self, request: Any) -> Pictures:
        if not request.prompt.strip():
            raise ValueError("nothing to make: the prompt is empty")
        body: dict[str, Any] = {
            "model": self.model,
            "prompt": request.prompt,
            "n": max(1, min(4, int(_wants(request, "count", 1) or 1))),
            "response_format": "b64_json",
        }
        aspect = _wants(request, "aspect_ratio")
        if aspect in XAI_IMAGE_ASPECT_RATIOS:
            body["aspect_ratio"] = aspect
        resolution = _wants(request, "resolution")
        if resolution:
            body["resolution"] = str(resolution).lower()
        images = list(request.images)
        if len(images) > 1:
            body["images"] = [
                {"url": _data_uri(image.data, image.media_type), "type": "image_url"}
                for image in images
            ]
        elif images:
            # JSON, not multipart: xAI refuses the form OpenAI's edits take.
            body["image"] = {
                "url": _data_uri(images[0].data, images[0].media_type),
                "type": "image_url",
            }
        url = f"{IMAGES_URL}/edits" if images else f"{IMAGES_URL}/generations"
        parsed = await _send(
            "POST", url, self._key, request.timeout, body=body, max_bytes=PICTURE_MAX_BYTES
        )
        return Pictures(_all_b64(parsed), model=self.model)


def _xai_video_mode(resolutions: tuple[str, ...], most_images: int) -> dict[str, Any]:
    return {
        "max_videos": 1,
        "max_duration_seconds": 15,
        "aspect_ratios": XAI_VIDEO_ASPECT_RATIOS,
        "resolutions": resolutions,
        "supports_aspect_ratio": True,
        "supports_resolution": True,
        **({"enabled": True, "max_input_images": most_images} if most_images else {}),
    }


class XAIVideo:
    """Grok Imagine videos: `/videos/generations`, `/videos/edits` and
    `/videos/extensions`, then `/videos/{id}` - OpenClaw's xAI video provider."""

    host = "api.x.ai"

    def __init__(
        self, *, model: str = "", api_key: str | None = None, auth_token: str | None = None
    ) -> None:
        self.model = (model or "grok-imagine-video").strip()
        self._key = api_key or auth_token or ""

    @property
    def models(self) -> tuple[str, ...]:
        return ("grok-imagine-video", "grok-imagine-video-1.5")

    @property
    def latest(self) -> bool:
        return self.model in XAI_VIDEO_15_MODELS

    @property
    def capabilities(self) -> dict[str, Any]:
        """OpenClaw's: the classic model makes from words, from a first frame or
        up to seven reference pictures, and edits or extends a video; 1.5 only
        animates one first frame, but at up to 1080P."""
        if self.latest:
            return {
                "modes": ("image_to_video",),
                "image_to_video": _xai_video_mode(("480P", "720P", "1080P"), 1),
                "video_to_video": {"enabled": False},
            }
        return {
            "generate": _xai_video_mode(("480P", "720P"), 0),
            "image_to_video": _xai_video_mode(("480P", "720P"), 7),
            "video_to_video": {
                "enabled": True,
                "max_videos": 1,
                "max_input_videos": 1,
                "max_duration_seconds": 10,
                "supports_aspect_ratio": False,
                "supports_resolution": False,
            },
        }

    def ready(self) -> str:
        return "" if self._key else "no xai key (ultron auth add xai)"

    def cannot(self, request: Any) -> str:
        """What OpenClaw's `prepareCreateRequest` refuses before anything is spent."""
        images = list(getattr(request, "images", ()) or ())
        videos = list(getattr(request, "videos", ()) or ())
        references = [image for image in images if getattr(image, "role", "") == "reference_image"]
        if references and len(references) != len(images):
            return "reference pictures cannot be mixed with a first frame"
        if not references and len(images) > 1:
            return "takes one first-frame picture"
        if self.latest and any(
            getattr(image, "role", "") not in ("", "first_frame") for image in images
        ):
            return f"{self.model} takes only a first-frame picture"
        if videos and not getattr(videos[0], "url", ""):
            return "edits a video only from an http(s) link to it, not a file"
        return ""

    async def submit(self, request: Any) -> str:
        images = list(getattr(request, "images", ()) or ())
        videos = list(getattr(request, "videos", ()) or ())
        duration = int(_wants(request, "duration_seconds", 0) or getattr(request, "seconds", 0))
        body: dict[str, Any] = {"model": self.model, "prompt": request.prompt}
        if videos:
            body["video"] = {"url": videos[0].url}
            if duration:
                body["duration"] = max(2, min(10, duration))
            endpoint = "extensions" if duration else "edits"
            parsed = await _send(
                "POST", f"{VIDEOS_URL}/{endpoint}", self._key, request.timeout, body=body
            )
            return _remote(parsed.get("request_id"))
        references = images and all(getattr(i, "role", "") == "reference_image" for i in images)
        if references:
            body["reference_images"] = [{"url": _data_uri(i.data, i.media_type)} for i in images]
        elif images:
            body["image"] = {"url": _data_uri(images[0].data, images[0].media_type)}
        body["duration"] = max(1, min(10 if references else 15, duration)) if duration else 8
        aspect = _wants(request, "aspect_ratio")
        if aspect in XAI_VIDEO_ASPECT_RATIOS or not (images and not references):
            body["aspect_ratio"] = aspect if aspect in XAI_VIDEO_ASPECT_RATIOS else "16:9"
        resolution = str(_wants(request, "resolution") or "").lower()
        if resolution == "1080p" and (references or not self.latest):
            resolution = "720p"
        body["resolution"] = resolution if resolution in ("480p", "720p", "1080p") else "480p"
        parsed = await _send(
            "POST", f"{VIDEOS_URL}/generations", self._key, request.timeout, body=body
        )
        return _remote(parsed.get("request_id"))

    async def status(self, remote: str) -> Status:
        parsed = await _send("GET", f"{VIDEOS_URL}/{remote}", self._key, POLL_TIMEOUT)
        state = str(parsed.get("status") or "")
        if state == "done":
            video = parsed.get("video")
            url = str(video.get("url") or "") if isinstance(video, Mapping) else ""
            return Status("done", url=url) if url else Status("failed", error="xAI sent no video")
        if state in ("failed", "expired"):
            error = parsed.get("error")
            code = _code(error.get("code")) if isinstance(error, Mapping) else ""
            return Status("failed", error=f"xAI says {state}" + (f" ({code})" if code else ""))
        return Status("running")

    async def download(self, status: Any, timeout: float) -> bytes:
        # A public link: the key stays home.
        return await _download(status.url, timeout)


async def _send(
    method: str,
    url: str,
    key: str,
    timeout: float,
    *,
    body: Mapping[str, Any] | None = None,
    max_bytes: int = STATUS_MAX_BYTES,
) -> Mapping[str, Any]:
    """One JSON request to xAI. A 5xx, a 429 or the network is `Retry`; any
    other 4xx is final. Either way the message is the status and xAI's error
    code - identifiers, never its prose, which would reach the model."""
    from ultron.sdk.web import WebError, get, post

    headers = {"Authorization": f"Bearer {key}"}
    try:
        if method == "POST":
            response = await post(
                url,
                json=body,
                headers=headers,
                timeout=timeout,
                max_bytes=max_bytes,
                user_agent="ultron-xai",
            )
        else:
            response = await get(
                url,
                headers=headers,
                timeout=timeout,
                max_bytes=max_bytes,
                max_redirects=0,
                user_agent="ultron-xai",
            )
    except WebError as exc:
        raise Retry(f"xAI unreachable: {type(exc).__name__}") from None
    try:
        parsed = json.loads(response.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        parsed = {}
    parsed = parsed if isinstance(parsed, Mapping) else {}
    if response.status >= 400:
        error = parsed.get("error")
        said = [_code(error.get(k)) for k in ("code", "type")] if isinstance(error, Mapping) else []
        named = ", ".join(dict.fromkeys(part for part in said if part))
        failure = f"HTTP {response.status} from xAI" + (f" ({named})" if named else "")
        if response.status >= 500 or response.status == 429:
            raise Retry(failure)
        raise RuntimeError(failure)
    return parsed


async def _download(url: str, timeout: float) -> bytes:
    from ultron.sdk.web import WebError, get

    try:
        response = await get(
            url, timeout=timeout, max_bytes=VIDEO_MAX_BYTES, user_agent="ultron-xai"
        )
    except WebError as exc:
        raise Retry(f"xAI download failed: {type(exc).__name__}") from None
    if response.status >= 500 or response.status == 429:
        raise Retry(f"HTTP {response.status} downloading from xAI")
    if response.status >= 400:
        raise RuntimeError(f"HTTP {response.status} downloading from xAI")
    if response.truncated:
        raise RuntimeError(f"the video is over {VIDEO_MAX_BYTES // (1024 * 1024)} MB")
    return response.body


def _data_uri(data: bytes, media_type: str) -> str:
    return f"data:{media_type};base64,{base64.b64encode(data).decode('ascii')}"


def _all_b64(parsed: Mapping[str, Any]) -> list[bytes]:
    """Every picture of an OpenAI-shaped `data: [{b64_json}]` reply."""
    rows = parsed.get("data")
    found = [
        base64.b64decode(str(row["b64_json"]))
        for row in (rows if isinstance(rows, list) else ())
        if isinstance(row, Mapping) and row.get("b64_json")
    ]
    if not found:
        raise RuntimeError("xAI sent no picture")
    return found


def _code(value: Any) -> str:
    """A vendor's error code or status as an identifier - never its prose."""
    return CODE.sub("_", str(value or "")).strip("_")[:60]


def _remote(value: Any) -> str:
    remote = str(value or "")
    if not REMOTE_ID.fullmatch(remote) or ".." in remote:
        raise RuntimeError("xAI sent no usable job id")
    return remote


def _key_only(credential: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in credential.items() if k in ("api_key", "auth_token")}


class XAIPlugin(Plugin):
    """The xAI provider."""

    name = "xai"
    description = "The xAI model provider - Grok, at xAI."

    def register(self, ctx: PluginContext) -> None:
        ctx.register_provider("xai", XAIProvider)
        # The transcriber: xAI's speech-to-text under the key the provider uses,
        # which the core hands in because the reader is named for the vendor.
        transcription_model = str(ctx.setting("transcription_model", "") or "")
        ctx.register_media_reader(
            "xai/stt",
            lambda **kwargs: XAITranscriber(model=transcription_model, **kwargs),
        )
        # Grok Imagine for imagegen and videogen, when this Ultron has the points
        # (SDK 1.39) - an older one still gets the provider and the transcriber.
        if hasattr(ctx, "register_extension"):
            ctx.register_extension(
                "imagegen.backend",
                "xai",
                lambda model="": XAIImages(
                    model=model or str(ctx.setting("image_model", "") or ""),
                    **_key_only(ctx.credential("xai")),
                ),
            )
            ctx.register_extension(
                "videogen.backend",
                "xai",
                lambda model="": XAIVideo(
                    model=model or str(ctx.setting("video_model", "") or ""),
                    **_key_only(ctx.credential("xai")),
                ),
            )
