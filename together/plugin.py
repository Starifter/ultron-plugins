"""Together AI provider - open models, hosted at Together.

Together speaks OpenAI's Chat Completions at `https://api.together.ai/v1`, so this is
the SDK's OpenAI-compatible base with Together's own parts declared on it
(`plugin-sdk.md` §6.7): a request that would not fit is refused rather than cut, a
listing that is a bare array with a price on each row, and a cache figure reported
in either of two places.

Requires the `openai` package (`pip install openai`).
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Mapping
from typing import Any

from ultron.sdk.openai_compat import OpenAICompatProvider
from ultron.sdk.plugin_entry import Plugin, PluginContext
from ultron.sdk.provider import ModelEntry, Pricing, Usage
from ultron.sdk.runtime import ProviderError

BASE_URL = "https://api.together.ai/v1"


def _rate(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        return None
    return float(value)


def pricing_of(row: Mapping[str, Any]) -> Pricing | None:
    """USD per million, as Together lists it. A row priced at zero for both input and
    output is *unknown*, not free: Together lists models it only serves on dedicated
    hardware that way, and a free-looking turn would be a false one."""
    raw = row.get("pricing")
    if not isinstance(raw, Mapping):
        return None
    price = Pricing(
        input=_rate(raw.get("input")),
        output=_rate(raw.get("output")),
        cache_read=_rate(raw.get("cached_input")),
    )
    if price.input is None or price.output is None or (price.input == 0 and price.output == 0):
        return None
    return price


class TogetherProvider(OpenAICompatProvider):
    """Chat Completions at Together, on the SDK's OpenAI-compatible base.

    There is no default model and no `/think`: Together hosts a hundred models whose
    reasoning switches differ - `reasoning.enabled` on some, `reasoning_effort` on
    others, a chat-template flag on a third - and its listing does not say which. A
    model's own reasoning, whichever field it arrives in, is still shown as thinking.
    """

    name = "together"
    label = "Together"
    api_key_env_vars = ("TOGETHER_API_KEY",)
    base_url = BASE_URL
    streaming = True
    sampling = True
    extra_body = {"context_length_exceeded_behavior": "error"}
    """Refuse a request that does not fit, rather than let Together shorten it - an
    overflow is what Ultron's compaction answers, and it can only answer one it hears."""
    overflow_markers = ("must be less than the context length",)
    """Together's words: "Input token count + `max_tokens` parameter must be less than
    the context length"."""

    async def listing(self) -> list[Mapping[str, Any]]:
        """`GET /models`, which at Together is a bare array rather than OpenAI's
        `{data: [...]}` - so it is read raw, not through `models.list()`."""
        await self.prepare()
        try:
            rows = await self._client.get("/models", cast_to=object)
        except Exception as exc:  # the SDK raises its own hierarchy; keep ours at the seam
            raise ProviderError(f"Together's model list failed: {exc}") from exc
        if isinstance(rows, Mapping):
            rows = rows.get("data")
        return [row for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []

    def entry_of(self, row: Mapping[str, Any]) -> ModelEntry | None:
        """One chat model: the window, the price and the date. Embedding, image,
        rerank and moderation models are not models a session can talk to."""
        model_id = str(row.get("id", "") or "")
        if not model_id or row.get("type") != "chat":
            return None
        window = row.get("context_length")
        base = super().entry_of(row)
        return ModelEntry(
            id=model_id,
            context_window=window if isinstance(window, int) and window > 0 else 0,
            cost=pricing_of(row),
            released=base.released if base is not None else "",
        )

    def usage_of(self, usage: Mapping[str, Any]) -> Usage | None:
        """OpenAI's `prompt_tokens_details.cached_tokens`, or the flat
        `cached_tokens` some of Together's models report instead."""
        found = super().usage_of(usage)
        flat = usage.get("cached_tokens") if usage else None
        if found is None or found.cache_read_tokens or not isinstance(flat, int) or flat <= 0:
            return found
        return Usage(
            input_tokens=max(found.input_tokens - flat, 0),
            output_tokens=found.output_tokens,
            cache_read_tokens=flat,
        )


# -- pictures and videos, for imagegen and videogen ------------------------------
#
# Together's image and video endpoints, registered into `imagegen.backend` and
# `videogen.backend` (SDK 1.39) so those plugins reach Together without knowing it
# exists. The interfaces are theirs, written in their PLUGIN.md; nothing here
# imports either. Each builder reads the key when imagegen or videogen reaches
# Together, never before.

IMAGES_URL = f"{BASE_URL}/images/generations"
IMAGE_SIZES = {"square": (1024, 1024), "landscape": (1216, 832), "portrait": (832, 1216)}
"""Pixels, not a ratio: Together takes `width` and `height`. Near 3:2, in 64s."""
VIDEOS_URL = "https://api.together.ai/v2/videos"
"""Videos are Together's `v2`; everything else here is `v1`."""
VIDEO_SIZES = {
    ("landscape", "480p"): (854, 480),
    ("landscape", "720p"): (1280, 720),
    ("landscape", "1080p"): (1920, 1080),
    ("portrait", "480p"): (480, 854),
    ("portrait", "720p"): (720, 1280),
    ("portrait", "1080p"): (1080, 1920),
    ("square", "480p"): (480, 480),
    ("square", "720p"): (720, 720),
    ("square", "1080p"): (1080, 1080),
}
"""Pixels, not a ratio: Together takes `width` and `height`."""
PICTURE_MAX_BYTES = 64 * 1024 * 1024
"""A picture comes back as base64, a third larger than its bytes."""
VIDEO_MAX_BYTES = 512 * 1024 * 1024
"""What one download may weigh. Fifteen seconds of 1080p is tens of MB."""
STATUS_MAX_BYTES = 1024 * 1024
"""A submission or a status is a small JSON document."""
POLL_TIMEOUT = 30.0
REMOTE_ID = re.compile(r"[A-Za-z0-9._:/-]{1,300}")
"""A job id, which goes into a URL path. It came from Together and sits in a file a
person can edit, so it is checked before it is used."""
CODE = re.compile(r"[^A-Za-z0-9_.-]+")
USER_AGENT = "ultron-together"


class Made:
    """A picture, as imagegen reads one: the bytes, the model, a cost."""

    __slots__ = ("cost", "data", "model")

    def __init__(self, data: bytes, model: str = "", cost: str = "") -> None:
        self.data = data
        self.model = model
        self.cost = cost


class Status:
    """Where a video job stands, as videogen reads one."""

    __slots__ = ("cost", "error", "state", "url")

    def __init__(self, state: str, url: str = "", error: str = "", cost: str = "") -> None:
        self.state = state
        self.url = url
        self.error = error
        self.cost = cost


class Retry(Exception):
    """A request worth making again - the network, a 5xx, a 429. videogen retries an
    exception whose `retry` is true."""

    retry = True


class TogetherImages:
    """Together's `/images/generations`, words only."""

    name = "together"
    host = "api.together.ai"
    edits = False
    """`image_url` exists for some models, but nothing says it takes a data URI, and
    a workspace picture is not at a public URL."""
    masks = False

    def __init__(
        self, *, model: str = "", api_key: str | None = None, auth_token: str | None = None
    ) -> None:
        self.model = (model or "black-forest-labs/FLUX.1-schnell").strip()
        self._key = api_key or auth_token or ""

    def ready(self) -> str:
        return "" if self._key else "no together key (ultron auth add together)"

    async def generate(self, request: Any) -> Made:
        from ultron.sdk.web import post

        if not request.prompt.strip():
            raise ValueError("nothing to make: the prompt is empty")
        if request.images:
            raise ValueError(f"{self.model} at Together makes pictures from words only")
        body: dict[str, Any] = {
            "model": self.model,
            "prompt": request.prompt,
            "n": 1,
            "response_format": "base64",
            "output_format": "png",
        }
        if request.aspect in IMAGE_SIZES:
            body["width"], body["height"] = IMAGE_SIZES[request.aspect]
        response = await post(
            IMAGES_URL,
            json=body,
            headers={"Authorization": f"Bearer {self._key}"},
            timeout=request.timeout,
            max_bytes=PICTURE_MAX_BYTES,
            user_agent=USER_AGENT,
        )
        parsed = _json(response.body)
        if response.status >= 400:
            raise RuntimeError(_failure(parsed, response.status, ("code", "type")))
        return Made(_first_b64(parsed), model=self.model)


class TogetherVideo:
    """Together's `v2/videos`: a job, then its status, then a public link."""

    name = "together"
    host = "api.together.ai"

    def __init__(
        self, *, model: str = "", api_key: str | None = None, auth_token: str | None = None
    ) -> None:
        self.model = (model or "minimax/hailuo-02").strip()
        self._key = api_key or auth_token or ""

    def ready(self) -> str:
        return "" if self._key else "no together key (ultron auth add together)"

    def cannot(self, request: Any) -> str:
        return ""

    async def submit(self, request: Any) -> str:
        body: dict[str, Any] = {"model": self.model, "prompt": request.prompt}
        if request.seconds:
            body["seconds"] = str(request.seconds)
        if request.aspect or request.resolution:
            shape = (request.aspect or "landscape", request.resolution or "720p")
            body["width"], body["height"] = VIDEO_SIZES[shape]
        frames = [
            {"input_image": base64.b64encode(frame.data).decode("ascii"), "frame": kind}
            for kind, frame in (("first", request.first), ("last", request.last))
            if frame is not None
        ]
        if frames:
            body["frame_images"] = frames
        parsed = await _call(
            "POST", VIDEOS_URL, headers=self._headers(), timeout=request.timeout, body=body
        )
        return _remote(parsed.get("id"))

    async def status(self, remote: str) -> Status:
        parsed = await _call(
            "GET", f"{VIDEOS_URL}/{remote}", headers=self._headers(), timeout=POLL_TIMEOUT
        )
        state = str(parsed.get("status") or "")
        outputs = parsed.get("outputs")
        outputs = outputs if isinstance(outputs, Mapping) else {}
        if state == "completed":
            url = str(outputs.get("video_url") or "")
            cost = outputs.get("cost")
            shown = f"${cost:g}" if isinstance(cost, int | float) else ""
            if not url:
                return Status("failed", error="Together sent no video")
            return Status("done", url=url, cost=shown)
        if state in ("failed", "cancelled"):
            error = parsed.get("error")
            code = ""
            if isinstance(error, Mapping):
                code = _code(error.get("code")) or _code(error.get("type"))
            return Status("failed", error=f"Together says {state}" + (f" ({code})" if code else ""))
        return Status("running")

    async def download(self, status: Any, timeout: float) -> bytes:
        # A public link: the key stays home.
        return await _download(status.url, {}, timeout)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key}"}


async def _call(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str],
    timeout: float,
    body: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    """One JSON request about a video. A 5xx, a 429 or the network is `Retry`; any
    other 4xx is final."""
    from ultron.sdk.web import WebError, get, post

    try:
        if method == "POST":
            response = await post(
                url,
                json=body,
                headers=headers,
                timeout=timeout,
                max_bytes=STATUS_MAX_BYTES,
                user_agent=USER_AGENT,
            )
        else:
            response = await get(
                url,
                headers=headers,
                timeout=timeout,
                max_bytes=STATUS_MAX_BYTES,
                max_redirects=0,
                user_agent=USER_AGENT,
            )
    except WebError as exc:
        raise Retry(f"Together unreachable: {type(exc).__name__}") from None
    parsed = _json(response.body)
    if response.status >= 500 or response.status == 429:
        raise Retry(_failure(parsed, response.status))
    if response.status >= 400:
        raise RuntimeError(_failure(parsed, response.status))
    return parsed


async def _download(url: str, headers: Mapping[str, str], timeout: float) -> bytes:
    from ultron.sdk.web import WebError, get

    try:
        response = await get(
            url, headers=headers, timeout=timeout, max_bytes=VIDEO_MAX_BYTES, user_agent=USER_AGENT
        )
    except WebError as exc:
        raise Retry(f"Together download failed: {type(exc).__name__}") from None
    if response.status >= 500 or response.status == 429:
        raise Retry(f"HTTP {response.status} downloading from Together")
    if response.status >= 400:
        raise RuntimeError(f"HTTP {response.status} downloading from Together")
    if response.truncated:
        raise RuntimeError(f"the video is over {VIDEO_MAX_BYTES // (1024 * 1024)} MB")
    return response.body


def _json(raw: bytes) -> Mapping[str, Any]:
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return decoded if isinstance(decoded, Mapping) else {}


def _failure(
    parsed: Mapping[str, Any], status: int, keys: tuple[str, ...] = ("status", "code", "type")
) -> str:
    """The status and Together's error code - identifiers, never its prose, which
    would reach the model. One that sends `error` as a sentence gets the status alone."""
    error = parsed.get("error")
    said: list[str] = []
    if isinstance(error, Mapping):
        said = [_code(error.get(key)) for key in keys]
    named = ", ".join(dict.fromkeys(part for part in said if part))
    return f"HTTP {status} from Together" + (f" ({named})" if named else "")


def _first_b64(parsed: Mapping[str, Any]) -> bytes:
    """The first picture of an OpenAI-shaped `data: [{b64_json}]` reply."""
    rows = parsed.get("data")
    first = rows[0] if isinstance(rows, list) and rows else None
    encoded = first.get("b64_json") if isinstance(first, Mapping) else None
    if not encoded:
        raise RuntimeError("Together sent no picture")
    return base64.b64decode(str(encoded))


def _code(value: Any) -> str:
    """A vendor's error code or status as an identifier - never its prose."""
    return CODE.sub("_", str(value or "")).strip("_")[:60]


def _remote(value: Any) -> str:
    remote = str(value or "")
    if not REMOTE_ID.fullmatch(remote) or ".." in remote:
        raise RuntimeError("Together sent no usable job id")
    return remote


def _key_only(credential: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in credential.items() if k in ("api_key", "auth_token")}


class TogetherPlugin(Plugin):
    """The Together provider."""

    name = "together"
    description = "The Together AI model provider - open models, hosted at Together."

    def register(self, ctx: PluginContext) -> None:
        ctx.register_provider("together", TogetherProvider)
        # Together's pictures and videos for imagegen and videogen, when this Ultron
        # has the points (SDK 1.39) - an older one still gets the provider. The key
        # is read when one of them reaches Together.
        if hasattr(ctx, "register_extension"):
            ctx.register_extension(
                "imagegen.backend",
                "together",
                lambda: TogetherImages(
                    model=str(ctx.setting("image_model", "") or ""),
                    **_key_only(ctx.credential("together")),
                ),
            )
            ctx.register_extension(
                "videogen.backend",
                "together",
                lambda: TogetherVideo(
                    model=str(ctx.setting("video_model", "") or ""),
                    **_key_only(ctx.credential("together")),
                ),
            )
