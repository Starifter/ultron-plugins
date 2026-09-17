"""OpenRouter provider - one key, every model OpenRouter routes to.

OpenRouter speaks OpenAI's Chat Completions dialect at `https://openrouter.ai/api/v1`,
so this rides the official `openai` SDK with the base URL moved - the same shape as
the plugin Ultron ships for OpenAI, minus the parts that are OpenAI's alone. What is
OpenRouter's alone is here: model ids of the form `vendor/model`, the unified
`reasoning` parameter that stands in for every vendor's thinking control, the
`reasoning_details` a reply carries and the next request has to carry back, the
`provider` routing block, and a `GET /models` that says what each model costs.

Requires the `openai` package (`pip install openai`, or `pip install "ultron[openai]"`).
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from ultron.sdk.plugin_entry import Plugin, PluginContext
from ultron.sdk.provider import (
    CACHE_TTLS,
    DEFAULT_CACHE_TTL,
    DEFAULT_LEVEL,
    DEFAULT_MAX_TOKENS,
    ContentBlock,
    ContextOverflowError,
    Delta,
    DeltaSink,
    ImageBlock,
    MediaBlock,
    Message,
    ModelEntry,
    PriceTier,
    Pricing,
    Provider,
    Sampling,
    TextBlock,
    ThinkingBlock,
    ThinkingLevel,
    ThinkingRequiredError,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
    looks_like_overflow,
    looks_like_thinking_required,
    parse_attempted_tokens,
    split_cache_boundary,
    strip_cache_boundary,
)
from ultron.sdk.runtime import ConfigError, ProviderError
from ultron.sdk.tool_plugin import ToolSpec

BASE_URL = "https://openrouter.ai/api/v1"

DEFAULT_REFERER = "https://github.com/Starifter/ultron"
DEFAULT_TITLE = "Ultron"
"""OpenRouter's attribution headers (`HTTP-Referer`, `X-Title`): optional, and what
lists an app on their rankings. Both are settings, so an operator can name their own."""

LEVELS: tuple[ThinkingLevel, ...] = ("off", "low", "medium", "high", "max")
"""The one menu, for every model. OpenRouter's `reasoning.effort` is a single
parameter it maps onto whatever the model underneath has - a budget for Anthropic,
an effort for OpenAI, nothing for a model that does not reason - and a parameter a
provider does not take is dropped on the way through, never an error. So the menu
here is OpenRouter's rather than the model's, and `/think` is honest about that:
what `off` buys on a model whose reasoning is mandatory is the vendor's refusal,
answered once by `mark_thinking_mandatory` and then not offered again."""

EFFORT: dict[ThinkingLevel, str] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "max": "max",
}
"""Ultron's levels in OpenRouter's words. `off` is not an effort but `enabled: false`."""

CACHED_VENDORS = {
    "anthropic": CACHE_TTLS,
    "google": ("5m",),
}
"""Vendors whose models cache only where a `cache_control` breakpoint says so, and the
lifetimes each sells. OpenAI, DeepSeek, Grok and the rest cache on their own terms,
and a breakpoint sent to them is a field they ignore - so it is not sent."""

MESSAGE_BREAKPOINTS = 3
"""Breakpoints spent on the conversation, after the one on the system prefix.
Anthropic allows four a request; three on the newest turn-ends is what the shipped
Anthropic plugin spends, for the same reasons."""

MODALITIES = {
    "text": "text",
    "image": "image",
    "audio": "audio",
    "file": "document",
    "video": "video",
}
"""OpenRouter's `input_modalities` in the catalog's words."""

PER_MILLION = 1_000_000


class OpenRouterProvider(Provider):
    """Chat Completions at OpenRouter, through the `openai` SDK.

    There is no default model: naming one would be this plugin choosing for the
    user among four hundred, and the model is the user's decision. Ids are
    `vendor/model` - `anthropic/claude-sonnet-5`, `openai/gpt-5.5` - exactly as
    OpenRouter lists them, and `/model list --refresh` fetches that list.
    """

    name = "openrouter"
    api_key_env_vars = ("OPENROUTER_API_KEY",)
    thinking_levels = LEVELS
    streaming = True
    sampling = True
    """Every field of a `Sampling` is forwarded. OpenRouter drops what the model
    underneath does not take, so a temperature sent to a reasoning model costs
    nothing rather than failing the turn."""

    routing: Mapping[str, Any] = {}
    """The `provider` block sent with every request: order, fallbacks, data
    collection. Class state because a provider is registered as a class and
    built by the core, and the plugin's settings have to reach the request
    without a constructor argument the core does not know to pass."""
    referer: str = DEFAULT_REFERER
    title: str = DEFAULT_TITLE

    @classmethod
    def configured(cls, settings: Mapping[str, Any]) -> type[OpenRouterProvider]:
        """This class with the plugin's settings bound, for `register_provider`.

        A subclass rather than a partial, because `provider_class` answers
        capability questions from a *class* and a partial is not one.
        """
        routing: dict[str, Any] = {}
        order = settings.get("provider_order")
        if isinstance(order, list | tuple) and order:
            routing["order"] = [str(slug) for slug in order]
        if settings.get("allow_fallbacks") is False:
            routing["allow_fallbacks"] = False
        collection = str(settings.get("data_collection", "") or "").strip().lower()
        if collection in ("allow", "deny"):
            routing["data_collection"] = collection
        return type(
            "OpenRouterProvider",
            (cls,),
            {
                "routing": routing,
                "referer": str(settings.get("site_url", "") or DEFAULT_REFERER),
                "title": str(settings.get("app_title", "") or DEFAULT_TITLE),
            },
        )

    @classmethod
    def model_families(cls) -> tuple[str, ...]:
        # Every id OpenRouter serves has a vendor in front of it.
        return ("*/*",)

    @classmethod
    def cache_ttls_for(cls, model: str) -> tuple[str, ...]:
        return CACHED_VENDORS.get(_vendor_of(model), ())

    def __init__(
        self,
        model: str = "",
        *,
        api_key: str | None = None,
        auth_token: str | None = None,
        base_url: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        context_window: int = 0,
        thinking: ThinkingLevel = DEFAULT_LEVEL,
        thinking_levels: Sequence[ThinkingLevel] | None = None,
        fast: bool = False,
        cache_ttl: str = DEFAULT_CACHE_TTL,
        client: Any = None,
    ) -> None:
        # An empty model is refused at the first request rather than here,
        # because a listing needs no model and `ultron models refresh` builds
        # the provider with none - the list is how a person finds one.
        super().__init__(
            model,
            max_tokens=max_tokens,
            context_window=context_window,
            thinking=thinking,
            thinking_levels=thinking_levels,
            fast=fast,
            cache_ttl=cache_ttl,
        )
        # OpenRouter has one bearer channel: a token profile is sent as the key.
        self._client = (
            client
            if client is not None
            else _build_client(
                api_key or auth_token,
                base_url or BASE_URL,
                headers={"HTTP-Referer": self.referer, "X-Title": self.title},
            )
        )

    # -- the listing ------------------------------------------------------------

    async def list_models(self) -> Sequence[ModelEntry] | None:
        """`GET /models`: every id OpenRouter routes to, with what the listing says
        about each - window, reply ceiling, price, what it takes, when it came.

        The core keeps ids, numbers and dates and drops the rest (C8); the prose
        OpenRouter writes about a model never reaches the store.
        """
        entries: list[ModelEntry] = []
        async for item in self._client.models.list():
            entry = _entry_of(_as_dict(item))
            if entry is not None:
                entries.append(entry)
        return entries

    # -- the request ------------------------------------------------------------

    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        on_delta: DeltaSink | None = None,
        sampling: Sampling | None = None,
    ) -> Message:
        if not self.model:
            raise ConfigError(
                "the OpenRouter provider needs an explicit model - set ULTRON_MODEL "
                "(or `model` in ~/.ultron/config.json) to an id like anthropic/claude-sonnet-5; "
                "`ultron models list` shows what OpenRouter offers"
            )
        request = self._request(system, messages, tools, sampling)
        try:
            return await self._attempt(request, on_delta)
        except ThinkingRequiredError:
            # The vendor underneath says this model has no `off`. Correct the
            # declaration and try once at the lowest level it does take.
            if not self.mark_thinking_mandatory():
                raise
            request["extra_body"] = {
                **request["extra_body"],
                "reasoning": _reasoning(self.thinking),
            }
            return await self._attempt(request, on_delta)

    def _request(
        self,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        sampling: Sampling | None = None,
    ) -> dict[str, Any]:
        control = _cache_control(self.cache_ttl)
        marked = _breakpoint_positions(messages) if control else frozenset[int]()
        payload: list[dict[str, Any]] = []
        if system.strip():
            payload.append(_system_message(system, control))
        for i, message in enumerate(messages):
            payload.extend(_to_api_messages(message, breakpoint=control if i in marked else None))

        # `reasoning` and `provider` are OpenRouter's and not in the SDK's
        # signature, so they ride `extra_body`, which the SDK sends as given.
        extra: dict[str, Any] = {"reasoning": _reasoning(self.thinking)}
        if self.routing:
            extra["provider"] = dict(self.routing)
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": payload,
            "extra_body": extra,
        }
        if tools:
            request["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": spec.name,
                        "description": spec.description,
                        "parameters": dict(spec.parameters),
                    },
                }
                for spec in tools
            ]
        if sampling:
            request.update(_sampling_request(sampling, self.max_tokens))
        return request

    async def _attempt(self, request: dict[str, Any], on_delta: DeltaSink | None) -> Message:
        if on_delta is None:
            return _from_api_response(await self._send(request))
        return await self._stream(request, on_delta)

    async def _send(self, request: dict[str, Any]) -> Any:
        try:
            return await self._client.chat.completions.create(**request)
        except Exception as exc:  # SDK raises its own hierarchy; keep ours at the seam
            raise self._failure(exc) from exc

    async def _stream(self, request: dict[str, Any], on_delta: DeltaSink) -> Message:
        """The same request, read as it arrives and put back together - including
        the `raw` assistant message the next request replays."""
        parts = _Assembly()
        try:
            stream = await self._client.chat.completions.create(
                **request, stream=True, stream_options={"include_usage": True}
            )
            async for chunk in stream:
                for delta in parts.take(chunk):
                    on_delta(delta)
        except Exception as exc:  # SDK raises its own hierarchy; keep ours at the seam
            raise self._failure(exc) from exc
        return parts.message()

    def _failure(self, exc: Exception) -> Exception:
        """One vendor error, in our words - the same for a streamed request."""
        message = str(exc)
        if looks_like_thinking_required(message):
            return ThinkingRequiredError(f"{self.model} requires reasoning: {exc}")
        if looks_like_overflow(message):
            return ContextOverflowError(
                f"OpenRouter request too long: {exc}",
                attempted=parse_attempted_tokens(message),
            )
        return ProviderError(f"OpenRouter request failed: {exc}")


# -- helpers: the request ----------------------------------------------------------


def _vendor_of(model: str) -> str:
    return model.lower().lstrip("~").partition("/")[0]


def _reasoning(level: ThinkingLevel) -> dict[str, Any]:
    """OpenRouter's `reasoning` block for one of Ultron's levels."""
    if level == "off":
        return {"enabled": False}
    return {"effort": EFFORT[level]}


def _sampling_request(sampling: Sampling, ceiling: int) -> dict[str, Any]:
    """One run's ask, in Chat Completions' own fields. A cap larger than the
    session's ceiling is the ceiling: the ask can spend less, never more."""
    out: dict[str, Any] = {}
    for name in ("temperature", "top_p", "frequency_penalty", "presence_penalty"):
        value = getattr(sampling, name)
        if value is not None:
            out[name] = value
    if sampling.seed is not None:
        out["seed"] = sampling.seed
    if sampling.stop:
        out["stop"] = list(sampling.stop)
    if sampling.max_tokens is not None:
        out["max_tokens"] = min(sampling.max_tokens, ceiling)
    return out


def _cache_control(ttl: str) -> dict[str, str] | None:
    """The `cache_control` block for a lifetime, or nothing for `none`."""
    if ttl == "1h":
        return {"type": "ephemeral", "ttl": "1h"}
    if ttl == "5m":
        return {"type": "ephemeral"}
    return None


def _breakpoint_positions(messages: Sequence[Message]) -> frozenset[int]:
    """Which messages carry a breakpoint: the last few that end a turn."""
    ends = [i for i, m in enumerate(messages) if m.role == "user" and m.content]
    return frozenset(ends[-MESSAGE_BREAKPOINTS:])


def _system_message(system: str, control: Mapping[str, str] | None) -> dict[str, Any]:
    """The system prompt, as one string where nothing is cached and as parts with
    a breakpoint on the stable prefix where the vendor underneath wants one."""
    if not control:
        return {"role": "system", "content": strip_cache_boundary(system)}
    stable, volatile = split_cache_boundary(system)
    parts: list[dict[str, Any]] = [{"type": "text", "text": stable, "cache_control": dict(control)}]
    if volatile:
        parts.append({"type": "text", "text": volatile})
    return {"role": "system", "content": parts}


def _data_url(block: ImageBlock) -> str | None:
    if block.data is None:
        return None
    return f"data:{block.media_type};base64,{base64.b64encode(block.data).decode('ascii')}"


def _image_part(block: ImageBlock) -> dict[str, Any] | None:
    url = _data_url(block)
    return {"type": "image_url", "image_url": {"url": url}} if url else None


_AUDIO_FORMATS = {"audio/wav": "wav", "audio/mpeg": "mp3"}


def _media_part(block: MediaBlock) -> dict[str, Any] | None:
    """One `MediaBlock` as a Chat Completions part: audio and PDF the way the
    dialect takes them, a text document as text, anything else as a line saying
    it was not sent - the row's transcript still stands."""
    if block.data is None:
        return None
    encoded = base64.b64encode(block.data).decode("ascii")
    if block.media_type in _AUDIO_FORMATS:
        return {
            "type": "input_audio",
            "input_audio": {"data": encoded, "format": _AUDIO_FORMATS[block.media_type]},
        }
    if block.media_type == "application/pdf":
        return {
            "type": "file",
            "file": {
                "filename": block.name or "document.pdf",
                "file_data": f"data:application/pdf;base64,{encoded}",
            },
        }
    if block.kind == "document":
        try:
            return {"type": "text", "text": block.data.decode("utf-8")}
        except UnicodeDecodeError:
            return None
    return {
        "type": "text",
        "text": f"[{block.kind}: {block.reference()} - not sent: openrouter does not take "
        f"{block.media_type}]",
    }


def _to_api_messages(
    message: Message, *, breakpoint: Mapping[str, str] | None = None
) -> list[dict[str, Any]]:
    """One neutral message -> one or more Chat Completions messages.

    An assistant turn this provider produced is replayed from `raw` whole -
    `reasoning_details` included, which is what a vendor whose thinking is
    signed needs to see again. Tool results are their own `tool` messages, so
    one user turn carrying several fans out. `breakpoint` marks the last part
    of the last message, so the cache covers everything up to it.
    """
    if message.role == "assistant" and isinstance(message.raw, Mapping) and "role" in message.raw:
        return [dict(message.raw)]

    texts = [b.text for b in message.content if isinstance(b, TextBlock) and b.text.strip()]
    tool_uses = [b for b in message.content if isinstance(b, ToolUseBlock)]
    results = [b for b in message.content if isinstance(b, ToolResultBlock)]

    out: list[dict[str, Any]] = []
    for result in results:
        out.append({"role": "tool", "tool_call_id": result.tool_use_id, "content": result.content})

    if message.role == "user":
        # A user row is a list of parts, in the row's order, so an envelope's
        # closing line still follows the image it encloses; a tool's picture
        # cannot ride the `tool` message and goes in the user message after it.
        parts: list[dict[str, Any]] = []
        for block in message.content:
            if isinstance(block, TextBlock) and block.text.strip():
                parts.append({"type": "text", "text": block.text})
            elif isinstance(block, ImageBlock):
                part = _image_part(block)
                if part:
                    parts.append(part)
            elif isinstance(block, MediaBlock):
                part = _media_part(block)
                if part:
                    parts.append(part)
            elif isinstance(block, ToolResultBlock):
                parts.extend(p for p in map(_image_part, block.images) if p)
                if block.trailer:
                    parts.append({"type": "text", "text": block.trailer})
                parts.extend(p for p in map(_media_part, block.media) if p)
        if parts:
            if breakpoint:
                parts[-1] = {**parts[-1], "cache_control": dict(breakpoint)}
            out.append({"role": "user", "content": parts})
        elif breakpoint and out:
            # Nothing but tool results: the breakpoint rides the last of them.
            last = out[-1]
            out[-1] = {
                **last,
                "content": [
                    {"type": "text", "text": last["content"], "cache_control": dict(breakpoint)}
                ],
            }
        return out

    if texts or tool_uses:
        entry: dict[str, Any] = {"role": message.role, "content": "\n\n".join(texts)}
        if tool_uses:
            entry["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": json.dumps(dict(call.arguments))},
                }
                for call in tool_uses
            ]
        out.append(entry)
    return out


# -- helpers: the reply --------------------------------------------------------------


def _as_dict(value: Any) -> Mapping[str, Any]:
    """One SDK object as a mapping. The SDK types what the platform API sends
    and keeps the rest as it parsed it; OpenRouter sends fields the SDK has no
    name for, and this reads them all the same way."""
    if isinstance(value, Mapping):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        dumped = dump(exclude_none=True)
        return dumped if isinstance(dumped, Mapping) else {}
    return dict(vars(value)) if hasattr(value, "__dict__") else {}


def _reasoning_text(delta: Mapping[str, Any]) -> str:
    """What a message or a delta shows of its thinking. `reasoning` is
    OpenRouter's field; `reasoning_content` is the alias other proxies use."""
    return str(delta.get("reasoning") or delta.get("reasoning_content") or "")


def _parse_tool_call(name: str, arguments: str) -> dict[str, Any]:
    try:
        parsed = json.loads(arguments or "{}")
    except json.JSONDecodeError as exc:
        raise ProviderError(f"model returned unparseable arguments for {name!r}: {exc}") from exc
    return parsed if isinstance(parsed, dict) else {}


def _from_api_response(response: Any) -> Message:
    """A Chat Completions response -> our neutral assistant turn."""
    choices = getattr(response, "choices", None) or []
    if not choices:
        raise ProviderError("OpenRouter answered with no choices")
    choice = _as_dict(choices[0].message)
    blocks: list[ContentBlock] = []
    reasoning = _reasoning_text(choice)
    if reasoning.strip():
        blocks.append(ThinkingBlock(reasoning))
    text = str(choice.get("content") or "")
    if text.strip():
        blocks.append(TextBlock(text))
    calls = choice.get("tool_calls")
    for call in calls if isinstance(calls, list) else []:
        entry = _as_dict(call)
        function = _as_dict(entry.get("function"))
        name = str(function.get("name", "") or "")
        blocks.append(
            ToolUseBlock(
                id=str(entry.get("id", "") or ""),
                name=name,
                arguments=_parse_tool_call(name, str(function.get("arguments") or "")),
            )
        )
    return Message(
        role="assistant",
        content=tuple(blocks),
        raw=_replayable(choice),
        usage=_usage_of(_as_dict(getattr(response, "usage", None))),
    )


def _replayable(choice: Mapping[str, Any]) -> dict[str, Any]:
    """The assistant message as the next request sends it back: role, content,
    tool calls and `reasoning_details` unmodified - the whole sequence, in the
    order it came, which is OpenRouter's condition for a signed block to be
    accepted again. The display-only `reasoning` string stays out."""
    raw: dict[str, Any] = {"role": "assistant", "content": str(choice.get("content") or "")}
    calls = choice.get("tool_calls")
    if isinstance(calls, list) and calls:
        raw["tool_calls"] = [
            {
                "id": str(_as_dict(c).get("id", "") or ""),
                "type": "function",
                "function": {
                    "name": str(_as_dict(_as_dict(c).get("function")).get("name", "") or ""),
                    "arguments": str(
                        _as_dict(_as_dict(c).get("function")).get("arguments") or "{}"
                    ),
                },
            }
            for c in calls
        ]
    details = choice.get("reasoning_details")
    if isinstance(details, list) and details:
        raw["reasoning_details"] = [dict(_as_dict(d)) for d in details]
    return raw


def _usage_of(usage: Mapping[str, Any]) -> Usage | None:
    """`prompt_tokens` already includes cached input, so cache is split out."""
    if not usage:
        return None
    prompt = int(usage.get("prompt_tokens", 0) or 0)
    details = _as_dict(usage.get("prompt_tokens_details"))
    cached = int(details.get("cached_tokens", 0) or 0)
    written = int(details.get("cache_write_tokens", 0) or 0)
    return Usage(
        input_tokens=max(prompt - cached - written, 0),
        output_tokens=int(usage.get("completion_tokens", 0) or 0),
        cache_read_tokens=cached,
        cache_write_tokens=written,
    )


class _Assembly:
    """A streamed reply, being put back together.

    Text and tool calls arrive as Chat Completions sends them; reasoning arrives
    twice - `reasoning`, a string to show, and `reasoning_details`, the blocks to
    replay. The blocks come in pieces that share an `index`, and are merged by it:
    the text of one block concatenated in order, its signature the last one sent.
    """

    def __init__(self) -> None:
        self.text: list[str] = []
        self.reasoning: list[str] = []
        self.details: dict[int, dict[str, Any]] = {}
        self.calls: dict[int, dict[str, Any]] = {}
        self.usage: Mapping[str, Any] = {}

    def take(self, chunk: Any) -> list[Delta]:
        """Fold one chunk in, and say what of it was worth showing."""
        data = _as_dict(chunk)
        usage = _as_dict(data.get("usage"))
        if usage:
            self.usage = usage
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return []
        delta = _as_dict(_as_dict(choices[0]).get("delta"))
        if not delta:
            return []
        out: list[Delta] = []
        reasoning = _reasoning_text(delta)
        if reasoning:
            self.reasoning.append(reasoning)
            out.append(Delta(kind="thinking", text=reasoning))
        details = delta.get("reasoning_details")
        for piece in details if isinstance(details, list) else []:
            self._merge_detail(_as_dict(piece))
        content = str(delta.get("content") or "")
        if content:
            self.text.append(content)
            out.append(Delta(kind="text", text=content))
        calls = delta.get("tool_calls")
        for call in calls if isinstance(calls, list) else []:
            entry = _as_dict(call)
            slot = self.calls.setdefault(
                int(entry.get("index", 0) or 0), {"id": "", "name": "", "arguments": []}
            )
            if entry.get("id"):
                slot["id"] = str(entry["id"])
            function = _as_dict(entry.get("function"))
            if function.get("name"):
                slot["name"] = str(function["name"])
            if function.get("arguments"):
                slot["arguments"].append(str(function["arguments"]))
        return out

    def _merge_detail(self, piece: Mapping[str, Any]) -> None:
        index = piece.get("index")
        key = (
            int(index)
            if isinstance(index, int) and not isinstance(index, bool)
            else len(self.details)
        )
        slot = self.details.get(key)
        if slot is None:
            self.details[key] = dict(piece)
            return
        for field in ("text", "summary", "data"):
            if piece.get(field):
                slot[field] = str(slot.get(field) or "") + str(piece[field])
        for field in ("signature", "id", "format", "type"):
            if piece.get(field):
                slot[field] = piece[field]

    def message(self) -> Message:
        text = "".join(self.text)
        reasoning = "".join(self.reasoning)
        blocks: list[ContentBlock] = []
        if reasoning.strip():
            blocks.append(ThinkingBlock(reasoning))
        if text.strip():
            blocks.append(TextBlock(text))
        calls: list[dict[str, Any]] = []
        for index in sorted(self.calls):
            slot = self.calls[index]
            arguments = "".join(slot["arguments"]) or "{}"
            blocks.append(
                ToolUseBlock(
                    id=slot["id"],
                    name=slot["name"],
                    arguments=_parse_tool_call(slot["name"], arguments),
                )
            )
            calls.append(
                {
                    "id": slot["id"],
                    "type": "function",
                    "function": {"name": slot["name"], "arguments": arguments},
                }
            )
        choice: dict[str, Any] = {"content": text}
        if calls:
            choice["tool_calls"] = calls
        if self.details:
            choice["reasoning_details"] = [self.details[i] for i in sorted(self.details)]
        return Message(
            role="assistant",
            content=tuple(blocks),
            raw=_replayable(choice),
            usage=_usage_of(self.usage),
        )


# -- helpers: the listing --------------------------------------------------------------


def _rate(value: Any) -> float | None:
    """A listing's per-token price, as USD per million. OpenRouter writes prices
    as strings; `-1` is its word for a price it will not quote, which is *unknown*
    here and never zero (C6). `0` is a real price: the `:free` variants."""
    try:
        per_token = float(value)
    except (TypeError, ValueError):
        return None
    if per_token < 0:
        return None
    return per_token * PER_MILLION


def _pricing_of(pricing: Mapping[str, Any]) -> Pricing | None:
    """The four rates the catalog prices by, with a long-prompt `override` as a
    second tier where the listing declares one."""
    base = Pricing(
        input=_rate(pricing.get("prompt")),
        output=_rate(pricing.get("completion")),
        cache_read=_rate(pricing.get("input_cache_read")),
        cache_write=_rate(pricing.get("input_cache_write")),
    )
    if base.unknown:
        return None
    tiers: list[PriceTier] = []
    overrides = pricing.get("overrides")
    for override in overrides if isinstance(overrides, list) else []:
        band = _as_dict(override)
        start = band.get("min_prompt_tokens")
        rates = (
            _rate(band.get("prompt", pricing.get("prompt"))),
            _rate(band.get("completion", pricing.get("completion"))),
            _rate(band.get("input_cache_read", pricing.get("input_cache_read"))),
            _rate(band.get("input_cache_write", pricing.get("input_cache_write"))),
        )
        if not isinstance(start, int) or isinstance(start, bool) or start <= 0:
            continue
        if any(rate is None for rate in rates[:2]):
            continue
        tiers.append(
            PriceTier(
                from_tokens=start,
                to_tokens=None,
                input=rates[0] or 0.0,
                output=rates[1] or 0.0,
                cache_read=rates[2] if rates[2] is not None else 0.0,
                cache_write=rates[3] if rates[3] is not None else 0.0,
            )
        )
    if not tiers or base.input is None or base.output is None:
        return base
    # A tiered price has no holes: the base rates fill everything below the
    # first override, and the overrides run in the order the listing gave.
    tiers.sort(key=lambda tier: tier.from_tokens)
    schedule = [
        PriceTier(
            from_tokens=0,
            to_tokens=tiers[0].from_tokens,
            input=base.input,
            output=base.output,
            cache_read=base.cache_read if base.cache_read is not None else 0.0,
            cache_write=base.cache_write if base.cache_write is not None else 0.0,
        )
    ]
    for i, tier in enumerate(tiers):
        following = tiers[i + 1].from_tokens if i + 1 < len(tiers) else None
        schedule.append(
            PriceTier(
                from_tokens=tier.from_tokens,
                to_tokens=following,
                input=tier.input,
                output=tier.output,
                cache_read=tier.cache_read,
                cache_write=tier.cache_write,
            )
        )
    return Pricing(
        input=base.input,
        output=base.output,
        cache_read=base.cache_read,
        cache_write=base.cache_write,
        tiers=tuple(schedule),
    )


def _entry_of(item: Mapping[str, Any]) -> ModelEntry | None:
    """One row of `GET /models` as a catalog entry - ids, numbers and dates."""
    model_id = str(item.get("id", "") or "")
    if not model_id:
        return None
    context = int(item.get("context_length") or 0)
    top = _as_dict(item.get("top_provider"))
    max_output = int(top.get("max_completion_tokens") or 0)
    architecture = _as_dict(item.get("architecture"))
    inputs = architecture.get("input_modalities")
    modalities = tuple(
        MODALITIES[str(kind)]
        for kind in (inputs if isinstance(inputs, list) else [])
        if str(kind) in MODALITIES
    )
    created = item.get("created")
    released = ""
    if isinstance(created, int) and not isinstance(created, bool) and created > 0:
        released = datetime.fromtimestamp(created, tz=UTC).strftime("%Y-%m-%d")
    return ModelEntry(
        id=model_id,
        context_window=context,
        max_output=max_output,
        cost=_pricing_of(_as_dict(item.get("pricing"))),
        modalities=modalities,
        released=released,
    )


def _build_client(api_key: str | None, base_url: str, *, headers: Mapping[str, str]) -> Any:
    try:
        from openai import AsyncOpenAI
    except ImportError as exc:  # pragma: no cover - depends on what is installed
        raise ProviderError(
            "the OpenRouter provider needs the openai package: pip install openai"
        ) from exc
    kwargs: dict[str, Any] = {"base_url": base_url, "default_headers": dict(headers)}
    if api_key:
        kwargs["api_key"] = api_key
    return AsyncOpenAI(**kwargs)


class OpenRouterPlugin(Plugin):
    """The OpenRouter provider: one key, every model it routes to."""

    name = "openrouter"
    description = "The OpenRouter model provider - one key, every model it routes to."

    def register(self, ctx: PluginContext) -> None:
        settings = {
            key: ctx.setting(key)
            for key in (
                "provider_order",
                "allow_fallbacks",
                "data_collection",
                "site_url",
                "app_title",
            )
        }
        ctx.register_provider("openrouter", OpenRouterProvider.configured(settings))
