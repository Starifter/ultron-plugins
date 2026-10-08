---
name: fireworks
description: The Fireworks AI model provider - open models, hosted at Fireworks - and FLUX pictures for imagegen.
version: "1.1.0"
requires_ultron_sdk: ">=1.25,<2"
categories: [provider, models, media]
logo: logo.svg
vendor_credentials: [fireworks]
contracts:
  providers: [fireworks]
config_schema:
  image_model:
    type: str
    default: flux-1-schnell-fp8
    description: "The FLUX text-to-image model imagegen makes pictures with at Fireworks - flux-1-dev-fp8, say - or its full accounts/... id."
providers:
  fireworks:
    api_key_env_vars: [FIREWORKS_API_KEY]
    streaming: true
    sampling: true
    catalog: live
python_dependencies:
  - "openai>=1.66"
---

# fireworks

Open models - Kimi, DeepSeek, Qwen, GLM, GPT-OSS - hosted at
[Fireworks AI](https://fireworks.ai).

```
/plugins install fireworks
ultron auth add fireworks             # a key from fireworks.ai, or FIREWORKS_API_KEY in ~/.ultron/.env
```

then `provider: fireworks` and a `model` in `config.json`, with the id as Fireworks
writes it: `accounts/fireworks/models/kimi-k2p6`. **There is no default model**;
`ultron models list fireworks` shows what Fireworks serves. It needs Ultron's SDK 1.25
or later.

## The catalog is live

The manifest lists no models. `/model list --refresh` asks Fireworks' `GET /models`.
Prices come from the catalog Ultron publishes, hydrated from
[models.dev](https://models.dev), so `/status` knows what a turn cost - and says
*unknown*, never zero, for a model it has no price for.

## A request that does not fit is refused

By default Fireworks lowers a request's reply ceiling to make an overflowing prompt fit.
This plugin asks it not to (`context_length_exceeded_behavior: error`), so an overflow is
an error Ultron hears and answers by compacting and trying again.

## Thinking

There is no `/think`: `reasoning_effort` is taken by some of Fireworks' models and not
others, and neither its documentation nor its listing says which, so a menu here would
be a guess. A model that reasons does so at its own default. Its reasoning is shown as
thinking and - since a Fireworks model loses its thinking between tool calls otherwise -
sent back with each earlier turn, which is what Ultron's SDK 1.25 added.

## Pictures

With [imagegen](../imagegen/PLUGIN.md) enabled, Fireworks is one of its vendors: the plugin
registers itself into `imagegen.backend` (SDK 1.39; an older Ultron gets the provider only).
imagegen needs no setting for it, and this plugin does not depend on it.

- **Pictures** (`image_model`, default `flux-1-schnell-fp8`): a FLUX `text_to_image`
  workflow, generating from words only. A bare name is one of Fireworks' own models; a full
  `accounts/...` id is used as written.

It spends the provider's key, read with `ctx.credential` - which is why the manifest lists
`fireworks` under `vendor_credentials` - and only when imagegen reaches Fireworks. Each read
is an `auth` record in the trail with a fingerprint.

## What reaches Fireworks

The conversation, the tool definitions, pictures for a model that takes them, and your
key. A PDF is not sent - Fireworks asks for pages as pictures - and the row says so. When
imagegen makes a picture at Fireworks, the prompt is sent and nothing else.

## Not tested live

This plugin is built from Fireworks' API documentation and tested against a fake client.
It has not been run against the real API.
