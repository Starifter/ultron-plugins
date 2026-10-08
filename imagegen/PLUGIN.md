---
name: imagegen
description: Make and edit pictures with OpenAI, Google, or any vendor another plugin adds, saved in the workspace.
categories: [media, images]
version: "3.1.0"
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

One tool, `image_generate`: make a picture from a prompt, or edit pictures already in the
workspace, and save the result there. The model is shown what it made, so it can look
before telling you it is right.

```
/plugins install imagegen
```

**From 2.x:** the tool was `generate_image`. It is `image_generate` now, OpenClaw's name, beside
`music_generate` and `video_generate`; nothing else about it changed. It stays one call that
answers with the picture - no job, no wake - because the model looking at what it made, and
editing it in the same turn, is the point of the tool.

## Keys

OpenAI and Google use the keys Ultron already holds for its model providers - `ultron auth
add openai` or `google`, or `OPENAI_API_KEY` / `GEMINI_API_KEY` in `~/.ultron/.env`. The
plugin reads them with `ctx.credential` (SDK 1.38), which is why this manifest declares both
under `vendor_credentials` and `/plugins` says so before you install it. Every other vendor
is a backend (below) and reads its key itself, in its own plugin's name. A key is read only
when its vendor is reached, and each read is an `auth` record in the trail with a
fingerprint, never the key. With no key anywhere the tool is still there, and every call
says which vendors were passed over and why.

## Vendors

Two are built in:

- **OpenAI** (`gpt-image-2`): generates, edits, and takes a mask.
- **Google** (`gemini-3.1-flash-image-preview`): generates and edits, no mask. An `imagen-*`
  model generates from words only.

Every other vendor comes from another plugin. Enable the plugin and its vendor is here -
in this session, without a restart:

| Plugin | Vendor | What it does | Its setting |
|---|---|---|---|
| `xai` | `grok-imagine-image-2.0` | generates, edits one picture at a time, no mask | `image_model` |
| `openrouter` | `openai/gpt-image-2` | generates and edits, no mask, billed by OpenRouter | `image_model` |
| `together` | `black-forest-labs/FLUX.1-schnell` | generates from words only | `image_model` |
| `fireworks` | `flux-1-schnell-fp8` | generates from words only | `image_model` |

`provider` picks which is asked first; then OpenAI, Google, and the backends in the order
their plugins install. A vendor with no usable key, one that cannot do the edit asked for,
or one that fails is passed over and named in the result.

**From 1.x:** the four vendors above used to live here, read with this plugin's credential
and set with `xai_model`, `openrouter_model`, `together_model` and `fireworks_model`. Those
settings are gone - `/plugins imagegen` warns about any still set - and each vendor now
needs its own plugin enabled, with its model in that plugin's `image_model`.

## The model's choice

The model may name the vendor and model itself, as `model: "openai/gpt-image-2"` - the provider, a
slash, and the id as that vendor writes it. Only the first slash splits, so
`openrouter/google/lyria-3-pro-preview` is OpenRouter's `google/lyria-3-pro-preview`. A
provider alone, `model: "xai"`, is that vendor's configured model. The vendor named is
asked first, before `provider`. If it is not installed, cannot do what was asked, or fails,
the others are tried on their own configured models - an id means something only at its
own vendor - and the result names each one passed over. There is no allow list: any
installed vendor and any id may be named, and the call costs money at whichever vendor
answers. The id goes into a vendor's URL, so one with `..`, `//`, `?`, `#`, `%` or a space
is refused before anything is spent.

`action: list` shows every vendor in the order it would be asked, as `model` takes it -
`openai/gpt-image-2: ready; edits, masks` - and why one cannot be asked.
Asking each vendor whether it is ready reads its key (an `auth` record each); nothing is
sent anywhere. A vendor whose plugin names further ids lists them too. None of the vendors
here fetches its vendor's whole catalogue, so an id the list does not show may still work.

## Adding a vendor

A plugin adds a vendor by putting a builder into `imagegen.backend` (SDK 1.39):

```python
def register(self, ctx):
    if hasattr(ctx, "register_extension"):  # still loads on an Ultron before 1.39
        ctx.register_extension("imagegen.backend", "acme", lambda: AcmeImages(ctx))
```

It needs nothing from imagegen - not an import, not `requires_plugins` - and imagegen needs
no change: with imagegen absent the entry sits unread. The name is the provider half of `model`, what `provider` takes, and
what the result and the trail call the vendor. A backend registered under `openai` or
`google` stands in for the built-in one.

**The builder** takes an optional `model` keyword - the id the model named, or nothing for your configured one - and returns a vendor: `lambda model="": AcmeX(model=model or configured)`. A builder that takes no arguments still works; when the model names one of its models, it is passed over with "update it" rather than built on the wrong model. It is called only when imagegen
reaches that vendor, so read the key there, with your own plugin's `ctx.credential` (and
your vendor under your manifest's `vendor_credentials`) or from the environment.

**The vendor** is any object with:

| | |
|---|---|
| `model` | The model it will use, for the result and `action: list`. Optional. |
| `models` | Further ids it takes, for `action: list`. Optional; at most 20 are shown. |
| `ready()` | `""` when it can be asked, or why not - `"no acme key (ultron auth add acme)"`. Optional. |
| `edits` | `True` if it takes pictures to work from. Optional, default `False`. |
| `masks` | `True` if it takes a mask. Optional, default `False`. |
| `max_images` | How many pictures one edit takes. Optional, default 8. |
| `host` | Where the bytes come from, for the result's envelope - `api.acme.ai`. Optional, default the name. |
| `async generate(request)` | Make one picture. Returns an object with `data` (the picture's bytes), and optionally `model` and `cost` (both strings). |

`request` has `prompt` (a string), `images` (a tuple of objects with `data`, `media_type`
and `name`), `mask` (one of those, or `None`), `aspect` (`""`, `square`, `landscape` or
`portrait`) and `timeout` (seconds; imagegen also enforces it). Raise to fail: the
exception's message is shown to the model, so make it an identifier - an HTTP status, a
vendor's error code - and never the vendor's own prose. Make requests with
`ultron.sdk.web.post` so the operator's address policy applies. imagegen checks what comes
back is a picture, saves it, envelopes it and records the attempt; a vendor does none of
that.

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
