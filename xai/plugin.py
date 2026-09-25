"""xAI provider - Grok, at xAI.

xAI speaks OpenAI's Chat Completions at `https://api.x.ai/v1`, so this is the SDK's
OpenAI-compatible base with xAI's own parts declared on it (`plugin-sdk.md` §6.7):
the reply ceiling under `max_completion_tokens`, `reasoning_effort` on the models that
reason - which cannot be switched off - and a listing that prices each model, with a
second rate above its long-context threshold.

Requires the `openai` package (`pip install openai`).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ultron.sdk.openai_compat import OpenAICompatProvider
from ultron.sdk.plugin_entry import Plugin, PluginContext
from ultron.sdk.provider import ModelEntry, PriceTier, Pricing, ThinkingLevel

BASE_URL = "https://api.x.ai/v1"

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


class XAIPlugin(Plugin):
    """The xAI provider."""

    name = "xai"
    description = "The xAI model provider - Grok, at xAI."

    def register(self, ctx: PluginContext) -> None:
        ctx.register_provider("xai", XAIProvider)
