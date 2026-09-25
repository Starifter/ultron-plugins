"""Groq provider - open models on Groq's LPUs, fast.

Groq speaks OpenAI's Chat Completions at `https://api.groq.com/openai/v1`, so this
is the SDK's OpenAI-compatible base with Groq's own parts declared on it
(`plugin-sdk.md` §6.7): the reply ceiling under `max_completion_tokens`, a thinking
switch that differs by model family, and a listing that says each model's window
and reply ceiling.

Requires the `openai` package (`pip install openai`).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ultron.sdk.openai_compat import OpenAICompatProvider
from ultron.sdk.plugin_entry import Plugin, PluginContext
from ultron.sdk.provider import ModelEntry, ThinkingLevel

BASE_URL = "https://api.groq.com/openai/v1"

EFFORT_ONLY: tuple[ThinkingLevel, ...] = ("low", "medium", "high")
"""GPT-OSS: `reasoning_effort` low to high, and no off - `include_reasoning: false`
only hides the reasoning, it does not stop it."""

SWITCHED: tuple[ThinkingLevel, ...] = ("off", "low", "medium", "high")
"""Qwen: the same efforts, and `none` turns reasoning off."""


def family_levels(model: str) -> tuple[ThinkingLevel, ...]:
    """The menu a model's family takes, by its id. Empty for a model Groq serves
    without a thinking control - Llama, and anything not named here."""
    name = model.lower()
    if "gpt-oss" in name:
        return EFFORT_ONLY
    if "qwen3" in name:
        return SWITCHED
    return ()


class GroqProvider(OpenAICompatProvider):
    """Chat Completions at Groq, on the SDK's OpenAI-compatible base.

    There is no default model: Groq's line-up changes month to month, and which
    one to run is the user's decision. `/model list --refresh` fetches it.
    """

    name = "groq"
    label = "Groq"
    api_key_env_vars = ("GROQ_API_KEY",)
    base_url = BASE_URL
    thinking_levels = SWITCHED
    streaming = True
    sampling = True
    max_tokens_field = "max_completion_tokens"
    """`max_tokens` is deprecated at Groq in favour of this."""
    reasoning_fields = ("reasoning", "reasoning_content")
    """`reasoning` is Groq's field, with `reasoning_format: parsed`."""
    overflow_markers = ("context_length_exceeded", "reduce the length of the messages")
    """Groq's own words for a prompt that does not fit."""

    @classmethod
    def levels_for(cls, model: str) -> tuple[ThinkingLevel, ...]:
        """What `model_catalog` or the listing says, else the family's menu."""
        declared = cls.declared_entry(model).thinking_levels
        return family_levels(model) if declared is None else super().levels_for(model)

    def thinking_request(self, level: ThinkingLevel) -> dict[str, Any]:
        """`reasoning_effort` where the model has a menu, and nothing where it
        does not - a Llama model is never sent a reasoning field."""
        if level not in self.thinking_levels:
            return {}
        if "gpt-oss" in self.model.lower():
            return {"reasoning_effort": level}
        # Parsed, because `raw` puts the reasoning inside the reply's text as
        # <think> tags, and Groq refuses `raw` alongside tools anyway.
        effort = "none" if level == "off" else level
        return {"reasoning_effort": effort, "extra_body": {"reasoning_format": "parsed"}}

    def entry_of(self, row: Mapping[str, Any]) -> ModelEntry | None:
        """One row of `GET /models`: the id, the window, the reply ceiling and the
        date. A model Groq marks inactive is not offered."""
        model_id = str(row.get("id", "") or "")
        if not model_id or row.get("active") is False:
            return None
        window = row.get("context_window")
        ceiling = row.get("max_completion_tokens")
        base = super().entry_of(row)
        return ModelEntry(
            id=model_id,
            context_window=window if isinstance(window, int) and window > 0 else 0,
            max_output=ceiling if isinstance(ceiling, int) and ceiling > 0 else 0,
            thinking_levels=family_levels(model_id),
            released=base.released if base is not None else "",
        )


class GroqPlugin(Plugin):
    """The Groq provider."""

    name = "groq"
    description = "The Groq model provider - open models, served fast."

    def register(self, ctx: PluginContext) -> None:
        ctx.register_provider("groq", GroqProvider)
