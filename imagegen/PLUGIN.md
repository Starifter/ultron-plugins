---
name: imagegen
description: Make and edit pictures with OpenAI, Google, xAI, OpenRouter, Together or Fireworks, saved in the workspace.
categories: [media, images]
version: "1.1.0"
requires_ultron_sdk: ">=1.38,<2"
vendor_credentials: [openai, google, xai, openrouter, together, fireworks]
contracts:
  tools: [generate_image]
config_schema:
  provider:
    type: str
    default: ""
    description: "openai, google, xai, openrouter, together or fireworks, tried first. Empty tries them in that order."
  timeout_seconds:
    type: float
    default: 120
    description: "One attempt at one vendor, 5 to 600 seconds."
  output_dir:
    type: str
    default: images
    description: "Where a picture goes, inside the workspace, when the model names no path."
  openai_model:
    type: str
    default: gpt-image-2
    description: "gpt-image-2, gpt-image-1.5 or gpt-image-1."
  openai_quality:
    type: str
    default: auto
    description: "auto, low, medium or high. Higher costs more and takes longer."
  google_model:
    type: str
    default: gemini-3.1-flash-image-preview
    description: "A Gemini image model, or an imagen-* model, which makes pictures from words only."
  xai_model:
    type: str
    default: grok-imagine-image-2.0
    description: "A Grok Imagine image model."
  openrouter_model:
    type: str
    default: openai/gpt-image-2
    description: "Any image model OpenRouter routes to, by its slug - bytedance-seed/seedream-4.5, say."
  together_model:
    type: str
    default: black-forest-labs/FLUX.1-schnell
    description: "An image model Together serves, as Together writes it."
  fireworks_model:
    type: str
    default: flux-1-schnell-fp8
    description: "A FLUX text-to-image model at Fireworks - flux-1-dev-fp8, say - or its full accounts/... id."
---

# imagegen

One tool, `generate_image`: make a picture from a prompt, or edit pictures already in the
workspace, and save the result there. The model is shown what it made, so it can look
before telling you it is right.

```
/plugins install imagegen
```

## Keys

The ones Ultron already holds for its model providers - `ultron auth add openai` (or
`google`, `xai`, `openrouter`, `together`, `fireworks`), or the provider's variable in
`~/.ultron/.env` (`OPENAI_API_KEY`, `GEMINI_API_KEY`, `XAI_API_KEY`, `OPENROUTER_API_KEY`,
`TOGETHER_API_KEY`, `FIREWORKS_API_KEY`). One key is enough; the last four are the keys the
`xai`, `openrouter`, `together` and `fireworks` provider plugins use, so a person who chats
through one of them can make pictures through it too. The plugin reads them with
`ctx.credential` (SDK 1.38), which is why this manifest declares all six under
`vendor_credentials` and `/plugins` says so before you install it. A key is read only when
its vendor is reached, and each read is an `auth` record in the trail with a fingerprint,
never the key. With no key the tool is still there, and every call says which keys are
missing.

## Vendors

- **OpenAI** (`gpt-image-2`): generates, edits, and takes a mask.
- **Google** (`gemini-3.1-flash-image-preview`): generates and edits, no mask. An `imagen-*`
  model generates from words only.
- **xAI** (`grok-imagine-image-2.0`): generates, and edits one picture at a time, no mask.
- **OpenRouter** (`openai/gpt-image-2`): generates and edits, no mask - whichever image model
  `openrouter_model` names, billed by OpenRouter.
- **Together** (`black-forest-labs/FLUX.1-schnell`): generates from words only.
- **Fireworks** (`flux-1-schnell-fp8`): generates from words only.

`provider` picks which is asked first; the rest follow in the order above. A vendor with no
usable key, one that cannot do the edit asked for, or one that fails is passed over and named
in the result.

## What it does

- **Generate** from a prompt, `square`, `landscape` or `portrait`.
- **Edit** up to eight workspace pictures (one at xAI); a `mask` (transparent = repaint)
  goes to OpenAI only. Pictures outside the workspace are refused.
- **Save** to `images/<time>-<prompt>.png`, or the `path` the model names - never over an
  existing file, and a named path that exists is refused before anything is spent.
- **Show** it to the model through Ultron's media store, inside an untrusted envelope - the
  bytes are a vendor's. With pictures off (`image_accept: false`) the file is saved and only
  the path comes back.

Each vendor attempt is a `plugin` record with `event: generate`: vendor, model, how many
pictures went in, and the hash and size of what came back. The prompt is only in the tool
call's own record.

Every picture is a paid request, and every picture handed in to edit is sent to that vendor.
