---
name: videogen
description: Make videos in the background with Google Veo or any vendor another plugin adds, saved in the workspace.
categories: [media, video]
version: "3.2.0"
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
    description: "One request to a vendor - a submission, a status check or a download - 5 to 600 seconds."
  output_dir:
    type: str
    default: videos
    description: "Where a video goes, inside the workspace, when the model names no path."
  google_model:
    type: str
    default: veo-3.1-fast-generate-preview
    description: "A Veo model: veo-3.1-generate-preview, veo-3.1-fast-generate-preview or veo-3.1-lite-generate-preview."
---

# videogen

One tool, OpenClaw's `video_generate`, with three actions. `generate` (the default) hands a
prompt, and optionally a first and a last frame from the workspace, to a video vendor and
returns at once with a job id; the video is made in the background - one to several minutes -
saved in the workspace when it is ready, and the agent is woken to tell you. `status` checks
one job and can wait for it, or with no job lists this session's jobs; `list` shows the
vendors and their models. That is OpenClaw's split.

```
/plugins install videogen
```

**From 2.x:** `generate_video` and `video_status` are now `video_generate` with
`action: generate`, `action: status` and `action: list`.

**From 3.1:** `action: list` listed this session's jobs; it lists the vendors now, and
`action: status` with no `job` lists the jobs - OpenClaw's meaning of each.

## Keys

Google uses the key Ultron already holds for its model provider - `ultron auth add google`,
or `GEMINI_API_KEY` in `~/.ultron/.env` - read with `ctx.credential` (SDK 1.38), which is
why this manifest declares it under `vendor_credentials`. Every other vendor is a backend
(below) and reads its key itself, in its own plugin's name. A key is read when its vendor is
reached and again when a job is picked back up in a new session; each read is an `auth`
record in the trail with a fingerprint, never the key.

OpenAI is not here: it shut down Sora 2 and its Videos API on 2026-09-24. Sora through
OpenRouter or Together went with it.

## Vendors

One is built in:

- **Google** (`veo-3.1-fast-generate-preview`): 4, 6 or 8 seconds, 16:9 or 9:16, 720p or
  1080p (1080p at 8 seconds only), a first frame and a last frame.

Every other vendor comes from another plugin. Enable the plugin and its vendor is here -
in this session, without a restart:

| Plugin | Vendor | What it makes | Its setting |
|---|---|---|---|
| `xai` | `grok-imagine-video-1.5` | 1 to 15 seconds, any shape, 480p to 1080p, first and last frame | `video_model` |
| `openrouter` | `google/veo-3.1` | whatever the routed model accepts, billed by OpenRouter | `video_model` |
| `together` | `minimax/hailuo-02` | the same, at Together, sized in pixels | `video_model` |

`provider` picks which is asked first; then Google, and the backends in the order their
plugins install. A vendor with no usable key, one that cannot make what was asked for, or
one that refuses the submission is passed over and named in the result. Once a vendor has
taken a job it is that vendor's: a job that fails later is not sent anywhere else, because
the first one may already have been billed.

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
| `cannot(request)` | `""` when it can make this, or why not - `"makes 16:9 and 9:16 only"`. Optional. |
| `async submit(request)` | Start the job; return the vendor's job id (letters, digits, `._:/-`, at most 300). |
| `async status(job_id)` | Return an object with `state` - `running`, `done` or `failed` - and `url`, `error` and `cost` (strings, any may be empty). |
| `async download(status, timeout)` | The video's bytes. `status` is the object your `status` returned. |

`request` has `prompt`, `first` and `last` (each `None` or an object with `data` and
`media_type`), `seconds` (0 for the vendor's default), `aspect` (`""`, `landscape`,
`portrait` or `square`), `resolution` (`""`, `480p`, `720p` or `1080p`) and `timeout`.
Raise to fail. An exception whose `retry` attribute is true - a dropped connection, a 5xx,
a 429 - is tried again, up to five in a row; any other ends the job. Messages and `error`
are shown to the model, so make them identifiers - an HTTP status, a vendor's error code -
never the vendor's own prose. Make requests with `ultron.sdk.web` so the operator's address
policy applies, and send your key only to your own API's host. videogen polls, retries,
caps the download at 512 MB, checks it is a video, saves it and tells the model; a vendor
does none of that.

## How a job runs

1. **Submit.** `video_generate` checks the frames and the path, sends the job to the first
   vendor that takes it, and returns the job id (`vg-1a2b3c`), the vendor, and the file the
   video will be written to.
2. **Follow.** The plugin asks the vendor every `poll_seconds`, in the background, for up to
   `max_minutes`. A status check that fails on the network or with a 5xx is tried again; five
   in a row, or a 4xx, ends the job.
3. **Save.** The video is downloaded and written to `videos/<time>-<prompt>.mp4`, or the
   `path` the model named - never over an existing file.
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

Each submission is a `plugin` record with `event: submit` - vendor, model, how many frames
went, and the vendor's job id - and each ending is `event: video`, with the hash and size of
what came back or the reason it did not. Each wake is the core's own `event: wake` record. The
prompt is only in the tool call's own record.

Every video is a paid request, and every frame handed in is sent to that vendor. So is every
wake: one model turn the person did not type. That is why the manifest says `wakes: true` -
`/plugins` shows `may start turns` before you install it - and why `announce: notice`, or
`plugins_no_wake: [videogen]` in `config.json`, turns it off.

## Not built

- **A video the model can watch.** The file is saved and its path returned; nothing is put in
  front of the model.
- **Cancelling** a job at the vendor, **extending** a video, **reference images**, and
  **audio** settings - every vendor's default audio is kept.
