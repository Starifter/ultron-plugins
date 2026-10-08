---
name: videogen
description: Make videos in the background with Google Veo or any vendor another plugin adds, saved in the workspace.
categories: [media, video]
version: "4.0.0"
requires_ultron_sdk: ">=1.39,<2"
vendor_credentials: [google]
wakes: true
contracts:
  tools: [video_generate]
config_schema:
  announce:
    type: str
    default: wake
    description: "How a finished job is told: wake (the agent is woken to tell the person, SDK 1.40) or notice (a line on the next turn)."
  announce_to:
    type: str
    default: ""
    description: "Where a wake's reply is also sent (SDK 1.41): channel:owner for your DM on the channel you last wrote from, or channel:<address>. Empty: where the session's replies go."
  provider:
    type: str
    default: ""
    description: "A vendor to try first - google, or a backend's name such as xai. Empty tries google, then the backends."
  poll_seconds:
    type: float
    default: 10
    description: "How often a running job is asked about, 2 to 120 seconds."
  max_minutes:
    type: float
    default: 60
    description: "How long a job is followed before it is given up on, 1 to 240 minutes."
  timeout_seconds:
    type: float
    default: 120
    description: "One request to a vendor - a submission, a status check or a download - 5 to 600 seconds, when the model gives no timeoutMs."
  output_dir:
    type: str
    default: videos
    description: "Where videos go, inside the workspace. A filename the model gives is a name in here."
  google_model:
    type: str
    default: veo-3.1-fast-generate-preview
    description: "A Veo model: veo-3.1-generate-preview, veo-3.1-fast-generate-preview or veo-3.1-lite-generate-preview."
---

# videogen

One tool, OpenClaw's `video_generate` - field for field - with three actions. `generate`
(the default) hands a prompt, and optionally pictures, videos and the shape, length and
resolution wanted, to a video vendor and returns at once with a job id; the video is made in
the background - one to several minutes - saved in the workspace when it is ready, and the
agent is woken to tell you. `status` lists this session's jobs; `list` shows the vendors and
what each takes. That is OpenClaw's split.

```
/plugins install videogen
```

**From 3.x:** the arguments are OpenClaw's now. `first_frame` and `last_frame` are `images`
with `imageRoles` (`first_frame`, `last_frame`, `reference_image`); `seconds` is
`durationSeconds`, `aspect` is `aspectRatio`, `path` is `filename` - a name in `output_dir`
that is never refused. `size`, `audio`, `watermark`, `providerOptions`, `timeoutMs`,
reference videos and pictures by URL are new. `status` takes no `job` and no `wait`: it lists
this session's jobs, as OpenClaw's shows the session's task. Veo takes one picture to start
from, as OpenClaw has it, and no last frame. A backend from before 4.0 still works (below).

**From 2.x:** `generate_video` and `video_status` are now `video_generate`.

## Keys

Google uses the key Ultron already holds for its model provider - `ultron auth add google`,
or `GEMINI_API_KEY` in `~/.ultron/.env` - read with `ctx.credential` (SDK 1.38), which is
why this manifest declares it under `vendor_credentials`. Every other vendor is a backend
(below) and reads its key itself, in its own plugin's name. A key is read when its vendor is
reached and again when a job is picked back up in a new session; each read is an `auth`
record in the trail with a fingerprint, never the key.

OpenAI is not here: it shut down Sora 2 and its Videos API on 2026-09-24. Sora through
OpenRouter or Together went with it.

## What the model can ask for

Every field is optional but the prompt.

| Field | What it takes |
|---|---|
| `prompt` | What happens in the video. |
| `image`, `images` | Up to 9 pictures; `imageRoles` gives each, by position, `first_frame`, `last_frame` or `reference_image`, and an empty string leaves one unset. |
| `video`, `videos` | Up to 4 videos to condition or extend; `videoRoles` the same, with `reference_video`. |
| `audioRef`, `audioRefs`, `audioRoles` | Up to 3 sounds - shown to the model only when a vendor here takes them (none does yet). |
| `model` | `provider/model` to ask first - below. |
| `filename` | A name for the file, kept as its basename inside `output_dir`. |
| `size` | `1280x720`, `1920x1080` and so on. |
| `aspectRatio` | `16:9`, `9:16`, `1:1`, or a vendor's own value. |
| `resolution` | `480P`, `720P`, `1080P`, `4K`, or a vendor's own value. |
| `durationSeconds` | How long. |
| `audio`, `watermark` | On or off, where a vendor has the switch. |
| `providerOptions` | A vendor's own options, such as `{"seed": 42}`; `action: list` names what each takes. |
| `timeoutMs` | One request to a vendor, in milliseconds, instead of `timeout_seconds`. |

A reference is a workspace path, a `file://` URL inside the workspace, a `data:` URL, or an
http(s) URL fetched under the same address policy as `web_fetch`.

### What a vendor cannot take

OpenClaw's rules, ported (`resolveVideoGenerationOverrides` and the fallback's checks):

- **References** must be taken. A vendor that cannot start from a picture, take a video, or
  take as many as were handed in is passed over - never sent the prompt without them.
- **`providerOptions`** must be ones the vendor declares, of the types it declares; a vendor
  that declares none of its own takes them as they come, one that declares an empty set takes
  none, and a mismatch passes it over.
- **A length** past a vendor's longest, where it lists no lengths, passes it over; where it
  lists lengths, the nearest is made - the longer on a tie.
- **A size, a shape or a resolution** it does not make is moved to the nearest one it does,
  or dropped when there is none.
- **`audio` or `watermark`** a vendor has no switch for is dropped.

The result says each one - `durationSeconds 5 was made as 6 (it makes 4/6/8).`, `Ignored,
not supported: audio=true.` - and `status` repeats it beside the job.

## Vendors

One is built in, with OpenClaw's limits:

- **Google** (`veo-3.1-fast-generate-preview`): 4, 6 or 8 seconds, 16:9 or 9:16, 720P or
  1080P, a size read as its shape and resolution; one picture to start from, or one video to
  extend, never both; no audio switch.

Every other vendor comes from another plugin. Enable the plugin and its vendor is here -
in this session, without a restart:

| Plugin | Vendor | What it takes | Its setting |
|---|---|---|---|
| `xai` | `grok-imagine-video` | up to 15 seconds, 7 shapes, 480P or 720P; a first frame or up to 7 reference pictures; a video by link to edit (up to 10 seconds) | `video_model` |
| `xai` | `grok-imagine-video-1.5` | one first frame only, up to 1080P | `video_model` |
| `openrouter` | `google/veo-3.1` | 4, 6 or 8 seconds, 16:9 or 9:16, 720P or 1080P, a size, the audio switch, up to 4 pictures, a `seed`; billed by OpenRouter | `video_model` |
| `together` | `minimax/hailuo-02` | up to 10 seconds, sized in pixels; a picture to start from on `Wan-AI/Wan2.2-I2V-A14B` | `video_model` |

`provider` picks which is asked first; then Google, and the backends in the order their
plugins install. A vendor with no usable key, one that cannot take the request, or one that
refuses the submission is passed over and named in the result. Once a vendor has taken a job
it is that vendor's: a job that fails later is not sent anywhere else, because the first one
may already have been billed.

**From 1.x:** the three vendors above used to live here, read with this plugin's
credential and set with `xai_model`, `openrouter_model` and `together_model`. Those
settings are gone - `/plugins videogen` warns about any still set - and each vendor now
needs its own plugin enabled, with its model in that plugin's `video_model`. A job a 1.x
session left running is still collected, as long as its vendor's plugin is enabled.

## The model's choice

The model may name the vendor and model itself, as `model: "google/veo-3.1-generate-preview"` - the provider, a
slash, and the id as that vendor writes it. Only the first slash splits, so
`openrouter/google/lyria-3-pro-preview` is OpenRouter's `google/lyria-3-pro-preview`. A
provider alone, `model: "xai"`, is that vendor's configured model. The vendor named is
asked first, before `provider`. If it is not installed, cannot do what was asked, or fails,
the others are tried on their own configured models - an id means something only at its
own vendor - and the result names each one passed over. There is no allow list: any
installed vendor and any id may be named, and the call costs money at whichever vendor
answers. The id goes into a vendor's URL, so one with `..`, `//`, `?`, `#`, `%` or a space
is refused before anything is spent.

A job keeps the model it was started on, and a later session that picks it up asks the
vendor on that model again.

`action: list` shows every vendor in the order it would be asked, as `model` takes it -
`google/veo-3.1-fast-generate-preview: ready` - and why one cannot be asked.
Asking each vendor whether it is ready reads its key (an `auth` record each); nothing is
sent anywhere. A vendor whose plugin names further ids lists them too. None of the vendors
here fetches its vendor's whole catalogue, so an id the list does not show may still work.

## Adding a vendor

A plugin adds a vendor by putting a builder into `videogen.backend` (SDK 1.39):

```python
def register(self, ctx):
    if hasattr(ctx, "register_extension"):  # still loads on an Ultron before 1.39
        ctx.register_extension("videogen.backend", "acme", lambda: AcmeVideo(ctx))
```

It needs nothing from videogen, and videogen needs no change. The name is what `provider`
takes, what the job file records, and how a job is found again in a later session - so keep
it stable. A backend registered under `google` stands in for the built-in one.

**The builder** takes an optional `model` keyword - the id the model named, or nothing for your configured one - and returns a vendor: `lambda model="": AcmeX(model=model or configured)`. A builder that takes no arguments still works; when the model names one of its models, it is passed over with "update it" rather than built on the wrong model. It is called when videogen reaches
that vendor and again when it resumes one of its jobs, so read the key there.

**The vendor** is any object with:

| | |
|---|---|
| `model` | The model it will use, for the job record and the result. Optional. |
| `models` | Further ids it takes, for `action: list`. Optional; at most 20 are shown. |
| `host` | Where the video comes from. Optional. |
| `ready()` | `""` when it can be asked, or why not. Optional. |
| `capabilities` | What it takes, OpenClaw's shape - below. |
| `cannot(request)` | `""` when it can make this, or why not. Optional; asked after the capabilities had their say. |
| `async submit(request)` | Start the job; return the vendor's job id (letters, digits, `._:/-`, at most 300). |
| `async status(job_id)` | Return an object with `state` - `running`, `done` or `failed` - and `url`, `error` and `cost` (strings, any may be empty). |
| `async download(status, timeout)` | The video's bytes. `status` is the object your `status` returned. |

`capabilities` is a dict, for the model the vendor was built on - one entry per mode, each
with the keys it needs:

```python
{
    "generate": {"max_duration_seconds": 15, "supported_duration_seconds": (4, 6, 8),
                 "sizes": (...), "aspect_ratios": ("16:9", "9:16"), "resolutions": ("720P",),
                 "supports_size": True, "supports_aspect_ratio": True,
                 "supports_resolution": True, "supports_audio": False,
                 "supports_watermark": False},
    "image_to_video": {"enabled": True, "max_input_images": 1, ...},
    "video_to_video": {"enabled": True, "max_input_videos": 1, ...},
    "provider_options": {"seed": "number"},   # number, boolean or string
    "modes": ("image_to_video",),             # optional: the only modes it makes
}
```

A builder that sets `reference_audio = True` on itself says its vendor takes sounds, and the
`audioRef` fields appear.

`request` has `prompt`; `images`, `videos` and `audios` (objects with `data`, `media_type`,
`role`, `name`, and `url` when it came from a link); what is left of the model's ask once your
capabilities had their say - `size`, `aspect_ratio`, `resolution` (each `""` when not sent),
`duration_seconds` (0), `audio` and `watermark` (`None` when not sent); `provider_options`;
and `timeout`. Raise to fail. An exception whose `retry` attribute is true - a dropped
connection, a 5xx, a 429 - is tried again, up to five in a row; any other ends the job.
Messages and `error` are shown to the model, so make them identifiers - an HTTP status, a
vendor's error code - never the vendor's own prose. Make requests with `ultron.sdk.web` so the
operator's address policy applies, and send your key only to your own API's host. videogen
polls, retries, caps the download at 512 MB, checks it is a video, saves it and tells the
model; a vendor does none of that.

**A vendor from before 4.0** declares no `capabilities`. It is read as what it was: a first and
a last frame (`request.first`, `request.last`, by role or in order), `request.seconds`, and no
size, shape, resolution, sound or watermark - those are dropped and reported - and it is passed
over for a reference picture or a video.

## How a job runs

1. **Submit.** `video_generate` reads the references, holds the ask against each vendor in
   turn, sends the job to the first that takes it, and returns the job id (`vg-1a2b3c`), the
   vendor, the file the video will be written to, and what was changed to fit.
2. **Follow.** The plugin asks the vendor every `poll_seconds`, in the background, for up to
   `max_minutes`. A status check that fails on the network or with a 5xx is tried again; five
   in a row, or a 4xx, ends the job.
3. **Save.** The video is downloaded and written to `videos/<time>-<prompt>.mp4`, or under
   the `filename` the model gave - never over an existing file.
4. **Tell.** The agent is woken - OpenClaw's completion event, `ctx.wake` (SDK 1.40) - with
   one line per finished job: which job, where it is, who made it, how big it is. It runs as a
   turn of the session's own after whatever is running, never inside a person's turn, and its
   reply goes where the session's replies go: a video asked for in a Telegram DM is announced
   in that DM. `announce_to` sends it somewhere as well, chosen by you rather than the model:
   `channel:owner` is your DM on the channel you last wrote from, so a video started at the
   laptop is announced on your phone; `channel:<address>` is a fixed place, held to the
   `message` tool's rule (`channels_send_allow`) before the turn is spent (SDK 1.41). Several
   jobs that finish together are one wake. Where the agent cannot be woken
   - `announce: notice`, an Ultron before 1.40, a `cron` or group session, a lane busy past the
   core's wait, `plugins_no_wake` - the same line rides the session's next turn instead. The
   line carries what the plugin worked out and never a word a vendor wrote; why a job failed is
   behind `video_generate status`, whose result arrives inside an untrusted envelope.

Jobs are kept in `<workspace>/.ultron/videogen/jobs.json`. A session that ends with a job
still running leaves it there, and the next session with the same key picks it back up, so a
video that was paid for is still collected - and the next session is the one woken when it is.
Nothing is sent to a vendor to resume, only asked.

Each submission is a `plugin` record with `event: submit` - vendor, model, how many pictures,
videos and sounds went, what was sent of size, shape, resolution, length, audio and watermark,
the names of any `providerOptions`, and the vendor's job id - and each ending is `event: video`, with the hash and size of
what came back or the reason it did not. Each wake is the core's own `event: wake` record. The
prompt is only in the tool call's own record.

Every video is a paid request, and every reference handed in is sent to that vendor. So is every
wake: one model turn the person did not type. That is why the manifest says `wakes: true` -
`/plugins` shows `may start turns` before you install it - and why `announce: notice`, or
`plugins_no_wake: [videogen]` in `config.json`, turns it off.

## Not built

- **A video the model can watch.** The file is saved and its path returned; nothing is put in
  front of the model.
- **Cancelling** a job at the vendor.
- **A vendor that takes sounds** - the `audioRef` fields are there for one, and none here
  does yet.
- **OpenRouter's `callback_url`**, which OpenClaw passes on: it has OpenRouter post to an
  address the model chose, a request no address policy here would see.
