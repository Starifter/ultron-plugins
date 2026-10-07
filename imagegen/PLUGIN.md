---
name: imagegen
description: Make and edit pictures with OpenAI's GPT Image or Google's Gemini/Imagen, saved in the workspace.
categories: [media, images]
version: "1.0.0"
requires_ultron_sdk: ">=1.38,<2"
vendor_credentials: [openai, google]
contracts:
  tools: [generate_image]
config_schema:
  provider:
    type: str
    default: ""
    description: "openai or google, tried first. Empty tries openai, then google."
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
---

# imagegen

One tool, `generate_image`: make a picture from a prompt, or edit pictures already in the
workspace, and save the result there. The model is shown what it made, so it can look
before telling you it is right.

```
/plugins install imagegen
```

## Keys

The ones Ultron already holds - `ultron auth add openai`, `ultron auth add google`, or
`OPENAI_API_KEY` / `GEMINI_API_KEY` in `~/.ultron/.env`. The plugin reads them with
`ctx.credential` (SDK 1.38), which is why this manifest declares `vendor_credentials:
[openai, google]` and `/plugins` says so before you install it. Each read is an `auth`
record in the trail with a fingerprint, never the key. With neither key the tool is still
there, and every call says which key is missing.

## Vendors

- **OpenAI** (`gpt-image-2`): generates, edits, and takes a mask.
- **Google** (`gemini-3.1-flash-image-preview`): generates and edits, no mask. An `imagen-*`
  model generates from words only.

`provider` picks which is asked first; the other is the fallback. A vendor with no usable key,
one that cannot do the edit asked for, or one that fails is passed over and named in the result.

## What it does

- **Generate** from a prompt, `square`, `landscape` or `portrait`.
- **Edit** up to eight workspace pictures; a `mask` (transparent = repaint) goes to OpenAI
  only. Pictures outside the workspace are refused.
- **Save** to `images/<time>-<prompt>.png`, or the `path` the model names - never over an
  existing file, and a named path that exists is refused before anything is spent.
- **Show** it to the model through Ultron's media store, inside an untrusted envelope - the
  bytes are a vendor's. With pictures off (`image_accept: false`) the file is saved and only
  the path comes back.

Each vendor attempt is a `plugin` record with `event: generate`: vendor, model, how many
pictures went in, and the hash and size of what came back. The prompt is only in the tool
call's own record.

Every picture is a paid request, and every picture handed in to edit is sent to that vendor.
