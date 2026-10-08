"""Fireworks AI provider - open models, hosted at Fireworks.

Fireworks speaks OpenAI's Chat Completions at `https://api.fireworks.ai/inference/v1`,
so this is the SDK's OpenAI-compatible base with Fireworks' own parts declared on it
(`plugin-sdk.md` §6.7): every earlier turn's `reasoning_content` sent back, since a
model loses its thinking across a tool loop without it, and a request that would not
fit refused rather than shortened.

Requires the `openai` package (`pip install openai`) and Ultron's SDK 1.25, for
`replay_reasoning_as`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from ultron.sdk.openai_compat import OpenAICompatProvider
from ultron.sdk.plugin_entry import Plugin, PluginContext

BASE_URL = "https://api.fireworks.ai/inference/v1"


class FireworksProvider(OpenAICompatProvider):
    """Chat Completions at Fireworks, on the SDK's OpenAI-compatible base.

    There is no default model and no `/think`: `reasoning_effort` is accepted by some
    of Fireworks' models and not others, and neither its docs nor its listing say
    which. A model that reasons does so at its own default, and its reasoning is shown
    as thinking and sent back.
    """

    name = "fireworks"
    label = "Fireworks"
    api_key_env_vars = ("FIREWORKS_API_KEY",)
    base_url = BASE_URL
    streaming = True
    sampling = True
    replay_reasoning_as = "reasoning_content"
    """Fireworks' reasoning models keep their thinking between tool calls only if each
    earlier assistant turn sends its `reasoning_content` back."""
    extra_body = {"context_length_exceeded_behavior": "error"}
    """By default Fireworks lowers `max_tokens` to make an overflowing request fit.
    Refused instead, so Ultron's compaction hears the overflow and answers it."""


# -- pictures, for imagegen --------------------------------------------------------
#
# A FLUX `text_to_image` workflow, registered into `imagegen.backend` (SDK 1.39) so
# imagegen reaches Fireworks without knowing it exists. The interface is imagegen's,
# written in its PLUGIN.md; nothing here imports it. The builder reads the key when
# imagegen reaches Fireworks, never before.

WORKFLOWS_URL = f"{BASE_URL}/workflows"
IMAGE_ASPECTS = {"square": "1:1", "landscape": "3:2", "portrait": "2:3"}
PICTURE_MAX_BYTES = 64 * 1024 * 1024
CODE = re.compile(r"[^A-Za-z0-9_.-]+")
USER_AGENT = "ultron-fireworks"


class Made:
    """A picture, as imagegen reads one: the bytes, the model, a cost."""

    __slots__ = ("cost", "data", "model")

    def __init__(self, data: bytes, model: str = "", cost: str = "") -> None:
        self.data = data
        self.model = model
        self.cost = cost


class FireworksImages:
    """A FLUX `text_to_image` workflow, words only, the picture's bytes back."""

    name = "fireworks"
    host = "api.fireworks.ai"
    edits = False
    """Kontext edits at Fireworks are a submit-and-poll API; not built."""
    masks = False

    def __init__(
        self, *, model: str = "", api_key: str | None = None, auth_token: str | None = None
    ) -> None:
        model = (model or "flux-1-schnell-fp8").strip()
        # Fireworks writes ids in full; a bare name is one of its own models.
        self.model = (
            model if model.startswith("accounts/") else f"accounts/fireworks/models/{model}"
        )
        self._key = api_key or auth_token or ""

    def ready(self) -> str:
        return "" if self._key else "no fireworks key (ultron auth add fireworks)"

    async def generate(self, request: Any) -> Made:
        from ultron.sdk.web import post

        if not request.prompt.strip():
            raise ValueError("nothing to make: the prompt is empty")
        if request.images:
            raise ValueError("Fireworks makes pictures from words only here")
        body: dict[str, Any] = {"prompt": request.prompt}
        if request.aspect in IMAGE_ASPECTS:
            body["aspect_ratio"] = IMAGE_ASPECTS[request.aspect]
        response = await post(
            f"{WORKFLOWS_URL}/{self.model}/text_to_image",
            json=body,
            # The picture itself as the body, rather than base64 inside JSON.
            headers={"Authorization": f"Bearer {self._key}", "Accept": "image/png"},
            timeout=request.timeout,
            max_bytes=PICTURE_MAX_BYTES,
            user_agent=USER_AGENT,
        )
        if response.status >= 400:
            raise RuntimeError(_failure(_json(response.body), response.status))
        if not response.body:
            raise RuntimeError("Fireworks sent no picture")
        return Made(response.body, model=self.model.rsplit("/", 1)[-1])


def _json(raw: bytes) -> Mapping[str, Any]:
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return decoded if isinstance(decoded, Mapping) else {}


def _failure(parsed: Mapping[str, Any], status: int) -> str:
    """The status and Fireworks' error code and type - identifiers, never its prose,
    which would reach the model. One that sends `error` as a sentence gets the
    status alone."""
    error = parsed.get("error")
    said: list[str] = []
    if isinstance(error, Mapping):
        said = [_code(error.get(key)) for key in ("code", "type")]
    named = ", ".join(dict.fromkeys(part for part in said if part))
    return f"HTTP {status} from Fireworks" + (f" ({named})" if named else "")


def _code(value: Any) -> str:
    """A vendor's error code as an identifier - never its prose."""
    return CODE.sub("_", str(value or "")).strip("_")[:60]


def _key_only(credential: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in credential.items() if k in ("api_key", "auth_token")}


class FireworksPlugin(Plugin):
    """The Fireworks provider."""

    name = "fireworks"
    description = "The Fireworks AI model provider - open models, hosted at Fireworks."

    def register(self, ctx: PluginContext) -> None:
        ctx.register_provider("fireworks", FireworksProvider)
        # FLUX pictures for imagegen, when this Ultron has the point (SDK 1.39) - an
        # older one still gets the provider. The key is read when imagegen reaches
        # Fireworks.
        if hasattr(ctx, "register_extension"):
            ctx.register_extension(
                "imagegen.backend",
                "fireworks",
                lambda model="": FireworksImages(
                    model=model or str(ctx.setting("image_model", "") or ""),
                    **_key_only(ctx.credential("fireworks")),
                ),
            )
