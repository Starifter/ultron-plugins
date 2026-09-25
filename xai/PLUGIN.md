---
name: xai
description: The xAI model provider - Grok, at xAI.
version: "1.0.0"
requires_ultron_sdk: ">=1.23,<2"
categories: [provider, models]
contracts:
  providers: [xai]
providers:
  xai:
    api_key_env_vars: [XAI_API_KEY]
    thinking_levels: [low, medium, high, max]
    streaming: true
    catalog: live
python_dependencies:
  - "openai>=1.66"
---

# xai

Grok, at [xAI](https://x.ai).

```
/plugins install xai
ultron auth add xai                   # a key from console.x.ai, or XAI_API_KEY in ~/.ultron/.env
```

then `provider: xai` and a `model` in `config.json`, or `--provider xai --model
grok-4.7`. **There is no default model**; `ultron models list xai` shows what xAI serves.

## The catalog is live

The manifest lists no models. `/model list --refresh` asks xAI's `GET /models`, which
says each model's window, its price - with the higher rate xAI charges above a model's
long-context threshold as a second tier, so a long prompt is priced as xAI bills it - and
which reasoning efforts it takes.

## Thinking

Grok's reasoning models cannot be told not to reason, so there is no `/think off`:
`low`, `medium` and `high`, and `max` where xAI offers `xhigh`. Each model's menu is the
listing's; before a listing has been fetched, a `grok-4` model is offered low to high
and a model whose id says `non-reasoning` is offered none - and is sent no reasoning
field. The reasoning comes back as `reasoning_content` and is shown as thinking.

Sampling (`temperature`, the penalties, `stop`) is not forwarded: xAI's reasoning models
refuse some of it outright.

## What reaches xAI

The conversation, the tool definitions, pictures, and your key. A PDF is not sent over
this API, and the row says so.

## Not tested live

This plugin is built from xAI's API documentation and tested against a fake client. It
has not been run against the real API.
