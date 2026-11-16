"""Venice AI provider - open and frontier models, served privately by Venice.

Venice speaks OpenAI's Chat Completions at `https://api.venice.ai/api/v1`, so this is
the SDK's OpenAI-compatible base with Venice's own parts declared on it
(`plugin-sdk.md` §6.7): the reply ceiling under `max_completion_tokens`, Venice's own
system prompt left out, a thinking menu read per model from the listing, every earlier
turn's reasoning sent back, a cache write reported under Venice's own name, and a
listing whose every row says its window, ceiling, price and status.

Requires the `openai` package (`pip install openai`) and Ultron's SDK 1.26, for
`replay_reasoning_as` and `takes_pdf`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ultron.sdk.openai_compat import OpenAICompatProvider
from ultron.sdk.plugin_entry import Plugin, PluginContext
from ultron.sdk.provider import ModelEntry, PriceTier, Pricing, ThinkingLevel, Usage
from ultron.sdk.runtime import ProviderError

BASE_URL = "https://api.venice.ai/api/v1"

LEVELS: tuple[ThinkingLevel, ...] = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
"""Every effort Venice takes, `none` being Ultron's `off`. No model takes all of them:
each row of the listing names its own (`reasoningEffortOptions`), and that is the menu
`/think` offers for it."""

FROM_EFFORT: dict[str, ThinkingLevel] = {
    "none": "off",
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}
"""A listing's effort names in Ultron's words; anything else is dropped."""

TO_EFFORT: dict[ThinkingLevel, str] = {level: effort for effort, level in FROM_EFFORT.items()}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        return None
    return float(value)


def _usd(band: Mapping[str, Any], key: str) -> float | None:
    """One rate, in USD per million tokens. Venice also lists it in Diem, its own
    token; Ultron prices in dollars."""
    rate = band.get(key)
    return _number(rate.get("usd")) if isinstance(rate, Mapping) else None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def pricing_of(raw: Mapping[str, Any]) -> Pricing | None:
    """`model_spec.pricing`: input, output, and the cache rates where Venice lists them.

    A model with an `extended` band charges its rates for the whole request once the
    prompt passes `context_token_threshold`, which is two tiers. A tier needs every
    rate; a cache rate Venice does not list in a band is that band's input rate -
    Venice lists a cache rate only where it differs from input - so a long cached
    prompt is never priced below what it costs.
    """
    price = Pricing(
        input=_usd(raw, "input"),
        output=_usd(raw, "output"),
        cache_read=_usd(raw, "cache_input"),
        cache_write=_usd(raw, "cache_write"),
    )
    if price.input is None or price.output is None:
        return None
    extended = _mapping(raw.get("extended"))
    threshold = _count(extended.get("context_token_threshold"))
    long_in, long_out = _usd(extended, "input"), _usd(extended, "output")
    if not threshold or long_in is None or long_out is None:
        return price

    def rate(band: Mapping[str, Any], key: str, fallback: float) -> float:
        found = _usd(band, key)
        return fallback if found is None else found

    return Pricing(
        input=price.input,
        output=price.output,
        cache_read=price.cache_read,
        cache_write=price.cache_write,
        tiers=(
            PriceTier(
                0,
                threshold,
                price.input,
                price.output,
                rate(raw, "cache_input", price.input),
                rate(raw, "cache_write", price.input),
            ),
            PriceTier(
                threshold,
                None,
                long_in,
                long_out,
                rate(extended, "cache_input", long_in),
                rate(extended, "cache_write", long_in),
            ),
        ),
    )


def levels_of(capabilities: Mapping[str, Any]) -> tuple[ThinkingLevel, ...]:
    """The efforts one model takes, in menu order. Empty - no switch is sent - for a
    model that takes none, which includes a model that reasons without a dial."""
    if capabilities.get("supportsReasoningEffort") is not True:
        return ()
    options = capabilities.get("reasoningEffortOptions")
    named = (
        {FROM_EFFORT[str(o)] for o in options if str(o) in FROM_EFFORT}
        if isinstance(options, list)
        else set()
    )
    return tuple(level for level in LEVELS if level in named)


class VeniceProvider(OpenAICompatProvider):
    """Chat Completions at Venice, on the SDK's OpenAI-compatible base.

    There is no default model: Venice's line-up changes month to month, and which one
    to run is the user's decision. `/model list --refresh` fetches it.
    """

    name = "venice"
    label = "Venice"
    api_key_env_vars = ("VENICE_API_KEY",)
    base_url = BASE_URL
    thinking_levels = LEVELS
    streaming = True
    sampling = True
    max_tokens_field = "max_completion_tokens"
    """`max_tokens` is deprecated at Venice in favour of this, which counts reasoning."""
    extra_body = {"venice_parameters": {"include_venice_system_prompt": False}}
    """Venice adds a system prompt of its own beside the caller's unless told not to.
    Ultron's prompt is the whole of what the model is told; `venice_system_prompt`
    turns Venice's back on."""
    replay_reasoning_as = "reasoning_content"
    """Venice's guide: the assistant turn goes back unmodified, and on a reasoning
    model that includes its `reasoning_content`."""
    replay_reasoning = True
    """A model whose thinking is signed (Gemini, through Venice) continues a tool loop
    only if its `reasoning_details` come back exactly as received."""
    takes_pdf = True
    """Venice reads a PDF to text itself before the model sees it, for any model."""
    overflow_markers = ("exceeds the model's maximum context", "too_many_tokens")
    """Venice's words for a prompt that does not fit (400 `TOO_MANY_TOKENS`)."""

    @classmethod
    def configured(cls, settings: Mapping[str, Any]) -> type[VeniceProvider]:
        """This class with the plugin's settings bound, for `register_provider`."""
        own = settings.get("venice_system_prompt") is True
        return cls.bind(extra_body={"venice_parameters": {"include_venice_system_prompt": own}})

    @classmethod
    def levels_for(cls, model: str) -> tuple[ThinkingLevel, ...]:
        """What the listing or `model_catalog` says, and nothing before either has
        spoken: an effort sent to a model without a dial is refused, and Venice
        counts refused features against a rate limit of their own."""
        if cls.declared_entry(model).thinking_levels is None:
            return ()
        return super().levels_for(model)

    def thinking_request(self, level: ThinkingLevel) -> dict[str, Any]:
        """`reasoning_effort`, which Venice prefers over `reasoning.effort`, and only
        where this model's menu holds the level - `none` included, which is `off`."""
        if level not in self.thinking_levels or level not in TO_EFFORT:
            return {}
        return {"reasoning_effort": TO_EFFORT[level]}

    def entry_of(self, row: Mapping[str, Any]) -> ModelEntry | None:
        """One row of `GET /models`: ids, numbers, dates, the price and the status.
        A model that is offline, or that cannot call tools - and Ultron always sends
        them - is not offered. The prose Venice writes about a model is not kept."""
        model_id = str(row.get("id", "") or "")
        spec = _mapping(row.get("model_spec"))
        capabilities = _mapping(spec.get("capabilities"))
        if not model_id or row.get("type", "text") != "text" or spec.get("offline") is True:
            return None
        if capabilities.get("supportsFunctionCalling") is not True:
            return None
        deprecation = _mapping(spec.get("deprecation"))
        status = ""
        if deprecation:
            status = "deprecated"
        elif spec.get("beta") is True or spec.get("betaModel") is True:
            status = "preview"
        replacement = deprecation.get("replacementModelId")
        base = super().entry_of(row)
        return ModelEntry(
            id=model_id,
            context_window=_count(spec.get("availableContextTokens"))
            or _count(row.get("context_length")),
            max_output=_count(spec.get("maxCompletionTokens")),
            thinking_levels=levels_of(capabilities),
            cost=pricing_of(_mapping(spec.get("pricing"))),
            modalities=(
                "text",
                *(("image",) if capabilities.get("supportsVision") is True else ()),
                "document",
            ),
            status=status,
            replaced_by=replacement if isinstance(replacement, str) else "",
            released=base.released if base is not None else "",
        )

    def usage_of(self, usage: Mapping[str, Any]) -> Usage | None:
        """OpenAI's `prompt_tokens_details.cached_tokens` for a cache read, and
        Venice's `cache_creation_input_tokens` beside it for a write."""
        found = super().usage_of(usage)
        details = _mapping(usage.get("prompt_tokens_details")) if usage else {}
        written = _count(details.get("cache_creation_input_tokens"))
        if found is None or found.cache_write_tokens or not written:
            return found
        return Usage(
            input_tokens=max(found.input_tokens - written, 0),
            output_tokens=found.output_tokens,
            cache_read_tokens=found.cache_read_tokens,
            cache_write_tokens=written,
        )

    def describe_failure(self, exc: Exception) -> Exception | None:
        if getattr(exc, "status_code", None) == 402:
            return ProviderError(
                "Venice says this key cannot pay for the request - its balance is empty "
                "or it reached its spend limit; add credits or raise the limit at "
                f"venice.ai/settings/api ({exc})"
            )
        return None


class VenicePlugin(Plugin):
    """The Venice provider."""

    name = "venice"
    description = "The Venice AI model provider - open and frontier models, served privately."

    def register(self, ctx: PluginContext) -> None:
        settings = {"venice_system_prompt": ctx.setting("venice_system_prompt", False)}
        ctx.register_provider("venice", VeniceProvider.configured(settings))
