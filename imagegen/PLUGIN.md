---
name: imagegen
description: Make and edit pictures with OpenAI, Google, or any vendor another plugin adds, saved in the workspace.
categories: [media, images]
version: "4.0.0"
requires_ultron_sdk: ">=1.39,<2"
vendor_credentials: [openai, google]
contracts:
  tools: [image_generate]
config_schema:
  provider:
    type: str
    default: ""
    description: "A vendor to try first - openai, google, or a backend's name such as xai. Empty tries openai, google, then the backends."
  timeout_seconds:
    type: float
    default: 120
    description: "One attempt at one vendor, 5 to 600 seconds, when the model gives no timeoutMs."
  output_dir:
    type: str
    default: images
    description: "Where pictures go, inside the workspace. A filename the model gives is a name in here."
  openai_model:
    type: str
    default: gpt-image-2
    description: "gpt-image-2, gpt-image-2.5-flare, gpt-image-2.5-sunburst, gpt-image-1.5 or gpt-image-1."
  openai_quality:
    type: str
    default: auto
    description: "auto, low, medium or high, sent when the model asks for no quality. Higher costs more and takes longer."
  google_model:
    type: str
    default: gemini-3.1-flash-image
    description: "A Gemini image model: gemini-3.1-flash-image or gemini-3-pro-image."
---

# imagegen

One tool, `image_generate` - OpenClaw's, field for field: make pictures from a prompt, or
edit pictures you hand it, and save them in the workspace. The model is shown what it
made, so it can look before telling you it is right.

```
/plugins install imagegen
```

**From 3.x:** the arguments are OpenClaw's now. `aspect` (square, landscape, portrait) is
`aspectRatio` with OpenClaw's 21 shapes; `path` is `filename`, a name in `output_dir` that
is never refused - a name already taken gets `-2`; `size`, `resolution`, `quality`,
`outputFormat`, `background`, `openai`, `count` and `timeoutMs` are new, and so are
`image` and pictures by URL. `mask` is gone, because OpenClaw has none, and so is Imagen,
which Google shut down. A backend from before 4.0 still works (below). Unlike OpenClaw's,
the tool still answers in the call rather than in a later turn: the model looking at what
it made, and editing it in the same turn, is the point of the tool.

**From 2.x:** the tool was `generate_image`.

## Keys

OpenAI and Google use the keys Ultron already holds for its model providers - `ultron auth
add openai` or `google`, or `OPENAI_API_KEY` / `GEMINI_API_KEY` in `~/.ultron/.env`. The
plugin reads them with `ctx.credential` (SDK 1.38), which is why this manifest declares both
under `vendor_credentials` and `/plugins` says so before you install it. Every other vendor
is a backend (below) and reads its key itself, in its own plugin's name. A key is read only
when its vendor is reached, and each read is an `auth` record in the trail with a
fingerprint, never the key. With no key anywhere the tool is still there, and every call
says which vendors were passed over and why.

## What the model can ask for

Every field is optional but the prompt.

| Field | What it takes |
|---|---|
| `prompt` | What to make, or what to change. |
| `image`, `images` | Pictures to edit or to work from, up to 16: a workspace path, a `file://` URL inside the workspace, a `data:` URL, or an http(s) URL - fetched under the same address policy as `web_fetch`. |
| `model` | `provider/model` to ask first - below. |
| `filename` | A name for the file, kept as its basename inside `output_dir`. |
| `size` | `1024x1024`, `1536x1024`, `2048x2048`, `3840x2160` and so on. |
| `aspectRatio` | One of 1:1, 2:1, 20:9, 19.5:9, 2:3, 3:2, 2.35:1, 3:4, 4:3, 4:5, 5:4, 9:16, 9:19.5, 9:20, 16:9, 21:9, 1:2, 4:1, 1:4, 8:1, 1:8. |
| `resolution` | 1K, 2K or 4K. |
| `quality` | low, medium, high, xhigh, max or auto. |
| `outputFormat` | png, jpeg or webp. |
| `background` | transparent, opaque or auto. Transparent needs png or webp. |
| `openai` | `background`, `moderation` (low, auto), `outputCompression` (0-100, jpeg/webp only) and `user`, for OpenAI only. |
| `count` | 1 to 4 pictures. |
| `timeoutMs` | One attempt at one vendor, in milliseconds, instead of `timeout_seconds`. |

`action: list` shows the vendors and what each takes; `action: status` says there is no
task, because the tool answers in the call.

### What a vendor cannot take

OpenClaw's rules, ported (`resolveImageGenerationOverrides`):

- **A size, a shape or a resolution** a vendor does not make is moved to the nearest one it
  does - the nearest shape, then the nearest area. A size asked of a vendor that takes only
  shapes becomes the shape of that size; a shape asked of one that takes only sizes becomes
  the nearest size of that shape.
- **A quality, a format or a background** it does not take is dropped.
- **Pictures handed in** must be taken: a vendor that does not edit, or takes fewer, is
  passed over rather than sent the prompt without them.
- **`count`** above what the vendor you named (or `provider` names) makes is refused before
  anything is spent; any other vendor makes as many as it can.
- **An edit with no size or resolution** is made at the size of the largest picture handed
  in - 4K from 3000 pixels, 2K from 1500 - for a vendor that takes a resolution.

The result says each one: `aspectRatio 2.35:1 was made as 21:9.`, `Ignored, not supported:
quality=high.` Those lines are Ultron's, outside the envelope that holds the pictures.

## Vendors

Two are built in, with OpenClaw's limits:

- **OpenAI** (`gpt-image-2`): up to 4 pictures, edits up to 5; any size within OpenAI's
  limits on gpt-image-2 and 2.5 (multiples of 16, at most 3840 a side), the listed sizes on
  1.5 and the three 1K sizes on gpt-image-1; no shape or resolution of its own, so those
  become a size; every quality (xhigh and max on 2.5 only), format and background. A
  transparent picture asked of gpt-image-2, which makes none, is made on gpt-image-1.5.
- **Google** (`gemini-3.1-flash-image`): up to 4 pictures, edits up to 5; ten shapes, 1K to
  4K, and five sizes it knows by shape; no quality, format or background.

Every other vendor comes from another plugin. Enable the plugin and its vendor is here -
in this session, without a restart:

| Plugin | Vendor | What it takes | Its setting |
|---|---|---|---|
| `xai` | `grok-imagine-image-2.0` | up to 4, edits up to 3; 13 shapes; 1K or 2K | `image_model` |
| `openrouter` | `openai/gpt-image-2` | up to 4, edits up to 5; 10 shapes; 1K to 4K; billed by OpenRouter | `image_model` |
| `together` | `black-forest-labs/FLUX.1-schnell` | up to 4 from words only, at one of three sizes | `image_model` |
| `fireworks` | `flux-1-schnell-fp8` | one from words only, in nine shapes | `image_model` |

`provider` picks which is asked first; then OpenAI, Google, and the backends in the order
their plugins install. A vendor with no usable key, one that cannot take the pictures
handed in, or one that fails is passed over and named in the result.

## The model's choice

The model may name the vendor and model itself, as `model: "openai/gpt-image-2"` - the
provider, a slash, and the id as that vendor writes it. Only the first slash splits, so
`openrouter/google/gemini-3.1-flash-image-preview` is OpenRouter's
`google/gemini-3.1-flash-image-preview`. A provider alone, `model: "xai"`, is that vendor's
configured model. The vendor named is asked first, before `provider`. If it is not
installed, cannot take the request, or fails, the others are tried on their own configured
models - an id means something only at its own vendor - and the result names each one
passed over. There is no allow list: any installed vendor and any id may be named, and the
call costs money at whichever vendor answers. The id goes into a vendor's URL, so one with
`..`, `//`, `?`, `#`, `%` or a space is refused before anything is spent.

## Adding a vendor

A plugin adds a vendor by putting a builder into `imagegen.backend` (SDK 1.39):

```python
def register(self, ctx):
    if hasattr(ctx, "register_extension"):  # still loads on an Ultron before 1.39
        ctx.register_extension("imagegen.backend", "acme", lambda model="": AcmeImages(ctx, model))
```

It needs nothing from imagegen - not an import, not `requires_plugins` - and imagegen needs
no change: with imagegen absent the entry sits unread. The name is the provider half of
`model`, what `provider` takes, and what the result and the trail call the vendor. A backend
registered under `openai` or `google` stands in for the built-in one.

**The builder** takes an optional `model` keyword - the id the model named, or nothing for
your configured one. A builder that takes no arguments still works; when the model names one
of its models, it is passed over with "update it" rather than built on the wrong model. It
is called only when imagegen reaches that vendor, so read the key there, with your own
plugin's `ctx.credential` (and your vendor under your manifest's `vendor_credentials`) or
from the environment.

**The vendor** is any object with:

| | |
|---|---|
| `model` | The model it will use, for the result and `action: list`. Optional. |
| `models` | Further ids it takes, for `action: list`. Optional; at most 20 are shown. |
| `ready()` | `""` when it can be asked, or why not. Optional. |
| `capabilities` | What it takes, OpenClaw's shape - below. |
| `host` | Where the bytes come from, for the result's envelope. Optional, default the name. |
| `async generate(request)` | Make the pictures. Returns an object with `images` (each with `data`, the picture's bytes) - or, as before 4.0, one `data` - and optionally `model` and `cost` (strings). |

`capabilities` is a dict, for the model the vendor was built on:

```python
{
    "generate": {"max_count": 4, "supports_size": True, "supports_aspect_ratio": False,
                 "supports_resolution": False},
    "edit": {"enabled": True, "max_count": 4, "max_input_images": 5, ...same supports_*},
    "geometry": {"sizes": (...), "aspect_ratios": (...), "resolutions": ("1K", "2K"),
                 "fallback_sizes": (...)},   # an empty `sizes` means any size
    "output": {"qualities": (...), "formats": (...), "backgrounds": (...)},
}
```

`request` has `prompt`, `images` (objects with `data`, `media_type` and `name`), `count`,
and what is left of the model's ask once your capabilities had their say: `size`,
`aspect_ratio`, `resolution`, `quality`, `output_format`, `background` (each `""` when not
sent), `openai` (a dict, for OpenAI's own options) and `timeout` (seconds; imagegen also
enforces it). Raise to fail: the exception's message is shown to the model, so make it an
identifier - an HTTP status, a vendor's error code - and never the vendor's own prose. Make
requests with `ultron.sdk.web.post` so the operator's address policy applies. imagegen
checks what comes back is a picture, saves it, envelopes it and records the attempt; a
vendor does none of that.

**A vendor from before 4.0** declares no `capabilities`. It is read from what it did say -
`edits` and `max_images` - as one that makes a picture at a time and takes no size, shape,
resolution, quality, format or background, so each of those is dropped and reported rather
than sent to code that would not read it. Its request still has `aspect` (always `""`) and
`mask` (always `None`).

## What it does

- **Generate** up to four pictures from a prompt.
- **Edit** up to sixteen pictures, from the workspace or a URL, at a vendor that takes that
  many.
- **Save** to `images/<time>-<prompt>.png`, or to the `filename` the model gives - never over
  an existing file.
- **Show** them to the model through Ultron's media store, inside an untrusted envelope -
  the bytes are a vendor's. With pictures off (`image_accept: false`) the files are saved and
  only the paths come back.

Each vendor attempt is a `plugin` record with `event: generate`: vendor, model, how many
pictures went in, what was sent of size, shape, resolution, quality, format and background,
and the hash and size of each picture that came back. The prompt is only in the tool call's
own record.

Every picture is a paid request, and every picture handed in is sent to that vendor.
