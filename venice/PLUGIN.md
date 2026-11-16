---
name: venice
title: Venice AI
description: The Venice AI model provider - open and frontier models, served privately, priced from Venice's own listing.
version: "1.0.0"
requires_ultron_sdk: ">=1.26,<2"
categories: [provider, models]
logo: logo.svg
contracts:
  providers: [venice]
config_schema:
  venice_system_prompt:
    type: bool
    default: false
    description: "Whether Venice adds its own system prompt beside Ultron's. Off: the model is told only what Ultron tells it."
providers:
  venice:
    api_key_env_vars: [VENICE_API_KEY]
    thinking_levels: [off, minimal, low, medium, high, xhigh, max]
    streaming: true
    sampling: true
    catalog: live
python_dependencies:
  - "openai>=1.66"
---

# venice

Open models - GLM, Kimi, Qwen, DeepSeek, Llama - and the frontier models Venice proxies,
at [Venice AI](https://venice.ai), which keeps no record of prompts or replies.

```
/plugins install venice
ultron auth add venice                # a key from venice.ai/settings/api, or VENICE_API_KEY in ~/.ultron/.env
```

then `provider: venice` and a `model` in `config.json`, or `--provider venice --model
zai-org-glm-5-1`. **There is no default model**; `ultron models list venice` shows what
Venice serves. It needs Ultron's SDK 1.26 or later.

## The catalog is live

The manifest lists no models. `/model list --refresh` asks Venice's `GET /models`, whose
every row says the model's window, its reply ceiling, whether it takes pictures, which
reasoning efforts it takes, and its price in dollars per million tokens - cache reads and
writes where Venice charges them differently, and the higher rate a model charges for the
whole request once a prompt passes its long-context threshold. So `/status` knows what a
turn cost, and says *unknown*, never zero, for a model with no price.

Two kinds of row are left out: a model Venice marks offline, and a model that cannot call
tools, since Ultron always sends them. A model Venice is retiring is listed as
*deprecated*, with its replacement where Venice names one; a beta model as *preview*.

## Thinking

`/think` offers exactly the efforts the listing names for the model - `none` is `off` -
and sends the one chosen as `reasoning_effort`. A model that reasons without a dial (or
one not yet listed: run `/model list --refresh`) gets no `/think` and is sent no effort;
it reasons at its own default. Its reasoning is shown as thinking and sent back with each
earlier turn, as Venice's guide asks, and a signed one (Gemini's `reasoning_details`) goes
back exactly as it came.

## Venice's own system prompt is off

By default Venice adds a system prompt of its own beside the caller's. This plugin sends
`include_venice_system_prompt: false`, so the model is told only what Ultron tells it;
`venice_system_prompt: true` turns Venice's back on.

Venice's server-side web search, scraping and characters are not used: what they fetch
would reach the model without Ultron marking it as untrusted. Ultron's own `web_search`
and `web_fetch` do.

## Cache

Venice caches on its own, and adds the breakpoints itself for the Claude models it
proxies. `/status` shows the reads and writes it reports.

## Failures

A `402` - an empty balance, or the key's own spend limit - says where to fix it. A prompt
that does not fit (`TOO_MANY_TOKENS`) is an overflow Ultron answers by compacting and
trying again.

## What reaches Venice

The conversation, the tool definitions, pictures for a model that takes them, PDFs (Venice
reads them to text before the model sees them), and your key.

## Not tested live

This plugin is built from Venice's API documentation and its public model listing, and
tested against a fake client. It has not been run against the real API with a key.
