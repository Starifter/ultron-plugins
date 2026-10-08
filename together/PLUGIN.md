---
name: together
description: The Together AI model provider - open models, hosted at Together - and Together's image and video models for imagegen and videogen.
version: "1.2.0"
requires_ultron_sdk: ">=1.23,<2"
categories: [provider, models, media]
logo: logo.svg
vendor_credentials: [together]
contracts:
  providers: [together]
config_schema:
  image_model:
    type: str
    default: black-forest-labs/FLUX.1-schnell
    description: "The image model imagegen makes pictures with at Together, as Together writes it."
  video_model:
    type: str
    default: minimax/hailuo-02
    description: "The video model videogen makes videos with at Together, as Together writes it."
providers:
  together:
    api_key_env_vars: [TOGETHER_API_KEY]
    streaming: true
    sampling: true
    catalog: live
python_dependencies:
  - "openai>=1.66"
---

# together

Open models - DeepSeek, Qwen, Kimi, GLM, Llama, GPT-OSS - hosted at
[Together AI](https://together.ai).

```
/plugins install together
ultron auth add together              # a key from api.together.ai, or TOGETHER_API_KEY in ~/.ultron/.env
```

then `provider: together` and a `model` in `config.json`, or `--provider together --model`
with an id as Together lists it (`deepseek-ai/DeepSeek-V4-Pro`, say). **There is no
default model**; `ultron models list together` shows the chat models Together serves.

## The catalog is live

The manifest lists no models. `/model list --refresh` asks Together's `GET /models`, which
says each chat model's window and price - so `/status` knows what a turn cost. A model
Together lists at zero for both input and output is priced *unknown*, not free: that is
how it lists models it serves only on dedicated hardware.

## A request that does not fit is refused

Together can quietly shorten a request that would overflow a model's context. This
plugin asks it not to (`context_length_exceeded_behavior: error`), so an overflow is an
error Ultron hears and answers the way it answers any other - by compacting and trying
again - rather than a reply built on a conversation Together cut.

## Thinking

There is no `/think`. Together's models switch reasoning three different ways, and its
listing does not say which model takes which, so a menu here would be a guess. A model
that reasons does so at its own default, and its reasoning is shown as thinking whichever
field it arrives in.

## Pictures and videos

With [imagegen](../imagegen/PLUGIN.md) or [videogen](../videogen/PLUGIN.md) enabled,
Together is one of their vendors: the plugin registers itself into `imagegen.backend` and
`videogen.backend` (SDK 1.39; an older Ultron gets the provider only). Neither plugin needs
a setting for it, and this one does not depend on them.

- **Pictures** (`image_model`, default `black-forest-labs/FLUX.1-schnell`): generates from
  words only, sized in pixels.
- **Videos** (`video_model`, default `minimax/hailuo-02`): whichever video model
  `video_model` names, sized in pixels, with a first and a last frame; what it accepts is
  the model's business, and a refusal passes to the next vendor. The finished video is a
  public link, fetched without the key.

Both spend the provider's key, read with `ctx.credential` - which is why the manifest lists
`together` under `vendor_credentials` - and only when imagegen or videogen reaches Together.
Each read is an `auth` record in the trail with a fingerprint.

## What reaches Together

The conversation, the tool definitions, pictures for a model that takes them, and your
key. A PDF is not sent, and the row says so. A frame you hand videogen reaches Together
when it is the vendor that makes the video, with the prompt; imagegen sends Together the
prompt only.

## Not tested live

This plugin is built from Together's API documentation and tested against a fake client.
It has not been run against the real API.
