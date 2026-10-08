---
name: xai
description: The xAI model provider - Grok, at xAI - with Grok speech-to-text for voice notes, and Grok Imagine for imagegen and videogen.
version: "1.4.0"
requires_ultron_sdk: ">=1.23,<2"
categories: [provider, models, audio, media]
logo: logo.svg
vendor_credentials: [xai]
contracts:
  providers: [xai]
  media_readers: [xai/stt]
config_schema:
  transcription_model:
    type: str
    default: ""
    description: The model xai/stt transcribes with. Empty is grok-voice-transcribe-2.0.
  image_model:
    type: str
    default: grok-imagine-image-2.0
    description: "The Grok Imagine image model imagegen makes pictures with."
  video_model:
    type: str
    default: grok-imagine-video
    description: "The Grok Imagine video model videogen makes videos with. grok-imagine-video-1.5 only animates a first frame."
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

## Voice notes

The plugin also registers `xai/stt`, a media reader that transcribes a voice note or a
recording with xAI's speech-to-text (`grok-voice-transcribe-2.0` unless
`transcription_model` says otherwise). It uses the same key as the provider, so once
`ultron auth add xai` is done a voice note is transcribed whether or not you chat through
Grok. Its priority is 45: after `groq/whisper` (40), before `openai/whisper` (50).
`audio_reader: xai/stt` pins it. xAI does not take WebM audio, so a WebM voice note goes
to the next reader.

## Pictures and videos

With [imagegen](../imagegen/PLUGIN.md) or [videogen](../videogen/PLUGIN.md) enabled, Grok
Imagine is one of their vendors: the plugin registers itself into `imagegen.backend` and
`videogen.backend` (SDK 1.39; an older Ultron gets the provider and the transcriber only).
Neither plugin needs a setting for it, and this one does not depend on them.

Each says what it takes in imagegen's and videogen's `capabilities` - OpenClaw's xAI
providers, value for value - so a shape, a resolution or a length it cannot make is moved to
the nearest one it can, and a request it cannot take passes to the next vendor:

- **Pictures** (`image_model`, default `grok-imagine-image-2.0`): up to four at once, edits of
  up to three pictures, thirteen shapes from 1:1 to 20:9, 1K or 2K.
- **Videos** (`video_model`, default `grok-imagine-video`): up to 15 seconds, seven shapes,
  480P or 720P; from words, from one first frame, or from up to seven `reference_image`
  pictures (up to 10 seconds); and a video edited, or extended when a length is given, from an
  http(s) link to it - xAI takes no uploaded video. `grok-imagine-video-1.5` only animates one
  first frame, at up to 1080P. No last frame on either.

Both spend the provider's key, read with `ctx.credential` - which is why the manifest lists
`xai` under `vendor_credentials` - and only when imagegen or videogen reaches xAI. Each read
is an `auth` record in the trail with a fingerprint.

## What reaches xAI

The conversation, the tool definitions, pictures, and your key. A PDF is not sent over
this API, and the row says so. A voice note reaches xAI only when `xai/stt` is the reader
that transcribes it, and then only the audio and the `audio_language` hint - never the
conversation. A picture or a frame you hand imagegen or videogen reaches xAI when it is
the vendor that makes it, with the prompt.

## Not tested live

This plugin is built from xAI's API documentation and tested against a fake client. It
has not been run against the real API.
