---
name: openrouter
description: The OpenRouter model provider - one key, every model it routes to.
version: "1.0.0"
requires_ultron_sdk: ">=1.3,<2"
categories: [provider, models]
logo: logo.svg
contracts:
  providers: [openrouter]
providers:
  openrouter:
    api_key_env_vars: [OPENROUTER_API_KEY]
    thinking_levels: [off, low, medium, high, max]
    streaming: true
    sampling: true
    catalog: live
config_schema:
  provider_order:
    type: list
    description: "Upstream providers to try, in order, by OpenRouter slug - e.g. [anthropic, google-vertex]. Empty lets OpenRouter choose."
  allow_fallbacks:
    type: bool
    default: true
    description: "Whether OpenRouter may fall back to another upstream when the ones in provider_order are unavailable."
  data_collection:
    type: str
    default: allow
    description: "`allow` or `deny`: whether the request may go to an upstream that stores prompts."
  site_url:
    type: str
    description: "Sent as HTTP-Referer, OpenRouter's app attribution. Defaults to Ultron's repository."
  app_title:
    type: str
    default: Ultron
    description: "Sent as X-Title, the name OpenRouter lists the app under."
python_dependencies:
  - "openai>=1.66"
---

# openrouter

One key, every model OpenRouter routes to: Claude, GPT, Gemini, DeepSeek, Qwen, Llama
and the rest, by the ids OpenRouter lists them under - `anthropic/claude-sonnet-5`,
`openai/gpt-5.5`, `google/gemini-3.1-pro-preview`, `deepseek/deepseek-v4-pro`.

```
/plugins install openrouter
ultron auth add openrouter            # or OPENROUTER_API_KEY in ~/.ultron/.env
```

then `provider: openrouter` and a `model` in `config.json`, or `--provider openrouter
--model anthropic/claude-sonnet-5`. **There is no default model.** OpenRouter lists four
hundred, and naming one here would be this plugin choosing for you.

OpenRouter speaks OpenAI's Chat Completions dialect, so this rides the `openai` package
with the base URL moved - `pip install openai` if `/plugins openrouter` says it is
missing. The key is a `openrouter` auth profile like any provider's, rotated by failover
like any provider's, and never a setting.

## The catalog is live

The manifest lists no models, on purpose. `catalog: live` means `/model list --refresh`
(or `ultron models refresh openrouter`) asks `GET /models` and keeps what a listing may
say about each id: the context window, the reply ceiling, the price per million with
OpenRouter's long-prompt surcharge as a second tier, what the model takes (image, audio,
document, video) and when it appeared. A `:free` variant is priced at zero because it
is; a price OpenRouter will not quote is *unknown*, never zero. So `/status` knows what a
turn cost, and `/model show` says which listing said so and how old it is.

## Thinking

`/think off|low|medium|high|max` is one menu for every model, because OpenRouter's
`reasoning` parameter is one parameter: it becomes a budget on Claude, an effort on GPT,
and nothing on a model that does not reason - a parameter the upstream does not take is
dropped on the way through, never refused. That means the menu is OpenRouter's rather
than the model's. A model whose reasoning cannot be switched off refuses `off` once,
and the plugin stops offering it for the rest of the session. Where you know a model's
real menu, `model_catalog` in `config.json` is the place to say so and it wins over
this plugin's word.

What a model reasons is shown as it streams and kept as `reasoning_details` on the turn,
replayed unmodified on the next request - which is what a vendor whose thinking is
signed (Claude, with tools) requires before it will continue.

## Caching

A `cache_control` breakpoint goes on the system prompt's stable prefix and on the last
three turn-ends for `anthropic/*` models (`cache_ttl: 5m|1h|none` in config, as the shipped
Anthropic plugin sends them) and for `google/*`. Every other upstream caches on its own terms, and
is sent no breakpoint - and `cache_ttl_seconds` is zero for it, so the compaction gate has
nothing to wait on.

## Settings

Under `plugins_settings.openrouter`:

```jsonc
{
  "provider_order": ["anthropic", "google-vertex"],  // upstreams to try, in order
  "allow_fallbacks": false,                          // and nothing else
  "data_collection": "deny",                         // only upstreams that keep nothing
  "app_title": "my agent"                            // OpenRouter's X-Title attribution
}
```

`provider_order`, `allow_fallbacks` and `data_collection` are OpenRouter's `provider`
routing block, sent with every request. Nothing here is a credential; the key is the
profile's.

## What is not here

No embedder, no fast mode, no login: OpenRouter sells none of those. A `Sampling` is
forwarded whole - OpenRouter drops what the upstream does not take.
