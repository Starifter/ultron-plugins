---
name: musicgen
description: Make music in the background with Google Lyria or any vendor another plugin adds, saved in the workspace.
categories: [media, audio, music]
version: "3.0.0"
requires_ultron_sdk: ">=1.39,<2"
vendor_credentials: [google]
wakes: true
contracts:
  tools: [music_generate]
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
    description: "A vendor to try first - google, or a backend's name such as openrouter. Empty tries google, then the backends."
  timeout_seconds:
    type: float
    default: 300
    description: "One attempt at one vendor, 30 to 900 seconds. A full song takes a minute or two."
  output_dir:
    type: str
    default: music
    description: "Where tracks go, inside the workspace. A filename the model gives is a name in here."
  google_model:
    type: str
    default: lyria-3.5
    description: "A Lyria model: lyria-3.5 or lyria-3-pro-preview for a full song, lyria-3-clip-preview for a 30-second clip."
---

# musicgen

One tool, OpenClaw's `music_generate` - field for field - with three actions. `generate`
(the default) hands a prompt, and optionally lyrics, a length, a format and pictures to set
the mood, to a music vendor and returns at once with a job id. The track is made in the
background and saved in the workspace when it is ready, usually in under two minutes, and the
agent is woken to tell you. `status` lists this session's jobs, with the lyrics a vendor sang;
`list` shows the vendors and their models. That is OpenClaw's split.

```
/plugins install musicgen
```

**From 2.x:** the arguments are OpenClaw's now. `seconds` is `durationSeconds`, with no
range of its own - a vendor shortens it to its longest, or drops it; `path` is `filename`, a
name in `output_dir` that is never refused. `format` (mp3 or wav), `image` and pictures by URL
are new, and lyrics with an instrumental are no longer refused. `status` takes no `job` and no
`wait`: it lists this session's jobs, with their lyrics. Lyria takes no length, as OpenClaw has
it, so a length asked of Google is dropped and said. A backend from before 3.0 still works
(below).

**From 1.x:** `generate_music` and `music_status` are now `music_generate`.

## Keys

Google uses the key Ultron already holds for its model provider - `ultron auth add google`,
or `GEMINI_API_KEY` in `~/.ultron/.env` - read with `ctx.credential` (SDK 1.38), which is
why this manifest declares it under `vendor_credentials`. Every other vendor is a backend
(below) and reads its key itself, in its own plugin's name. A key is read only when its
vendor is reached, and each read is an `auth` record in the trail with a fingerprint, never
the key.

## What the model can ask for

Every field is optional but the prompt.

| Field | What it takes |
|---|---|
| `prompt` | Style, genre, mood, purpose. |
| `lyrics` | Exact words to sing, only when the person gave them or asked for them. |
| `instrumental` | No vocals. |
| `image`, `images` | Up to 10 pictures to set the mood: a workspace path, a `file://` URL inside the workspace, a `data:` URL, or an http(s) URL fetched under the same address policy as `web_fetch`. |
| `model` | `provider/model` to ask first - below. |
| `durationSeconds` | How long; a vendor may shorten it. |
| `format` | mp3 or wav. |
| `filename` | A name for the file, kept as its basename inside `output_dir`. |

### What a vendor cannot take

OpenClaw's rules, ported (`resolveMusicGenerationOverrides`): lyrics, an instrumental, a
length or a format a vendor does not take is dropped; a length past its longest is shortened
to it; pictures it cannot take - or more than it takes - pass it over. Each vendor is asked
with what it takes, and the result says what changed: `Ignored, not supported:
durationSeconds=90.`, `durationSeconds 300 was made as 180.` `status` repeats it beside the job.

## Vendors

One is built in, with OpenClaw's limits:

- **Google** (`lyria-3.5`): lyrics and an instrumental, written into the prompt, and up to ten
  pictures; **no length** - OpenClaw sends Lyria none, so `durationSeconds` is dropped and
  said. `lyria-3-clip-preview` makes MP3 and `lyria-3-pro-preview` MP3 or WAV; any other Lyria
  model is sent the format and answers for it.

Every other vendor comes from another plugin. Enable the plugin and its vendor is here -
in this session, without a restart:

| Plugin | Vendor | What it takes | Its setting |
|---|---|---|---|
| `openrouter` | `google/lyria-3-pro-preview` | lyrics, an instrumental, up to 180 seconds, MP3 or WAV (WAV when none is asked), one picture; billed by OpenRouter | `music_model` |

`provider` picks which is asked first; then Google, and the backends in the order their
plugins install. A vendor with no usable key, or one that cannot take the pictures, is passed
over when the job starts and named in the result. One that fails while making the track is
passed over too, and the next is asked - unless it timed out, because a vendor that timed out
may still have made, and billed, the track.

## The model's choice

The model may name the vendor and model itself, as `model: "google/lyria-3-pro-preview"` - the provider, a
slash, and the id as that vendor writes it. Only the first slash splits, so
`openrouter/google/lyria-3-pro-preview` is OpenRouter's `google/lyria-3-pro-preview`. A
provider alone, `model: "openrouter"`, is that vendor's configured model. The vendor named is
asked first, before `provider`. If it is not installed, cannot do what was asked, or fails,
the others are tried on their own configured models - an id means something only at its
own vendor - and the result names each one passed over. There is no allow list: any
installed vendor and any id may be named, and the call costs money at whichever vendor
answers. The id goes into a vendor's URL, so one with `..`, `//`, `?`, `#`, `%` or a space
is refused before anything is spent.

`action: list` shows every vendor in the order it would be asked, as `model` takes it -
`google/lyria-3.5: ready` - and why one cannot be asked.
Asking each vendor whether it is ready reads its key (an `auth` record each); nothing is
sent anywhere. A vendor whose plugin names further ids lists them too. None of the vendors
here fetches its vendor's whole catalogue, so an id the list does not show may still work.

## Adding a vendor

A plugin adds a vendor by putting a builder into `musicgen.backend` (SDK 1.39):

```python
def register(self, ctx):
    if hasattr(ctx, "register_extension"):  # still loads on an Ultron before 1.39
        ctx.register_extension("musicgen.backend", "acme", lambda: AcmeMusic(ctx))
```

It needs nothing from musicgen, and musicgen needs no change: with musicgen absent the entry
sits unread. The name is the provider half of `model`, what `provider` takes, and what the job and the trail call the
vendor. A backend registered under `google` stands in for the built-in one.

**The builder** takes an optional `model` keyword - the id the model named, or nothing for your configured one - and returns a vendor: `lambda model="": AcmeX(model=model or configured)`. A builder that takes no arguments still works; when the model names one of its models, it is passed over with "update it" rather than built on the wrong model. It is called only when musicgen
reaches that vendor, so read the key there.

**The vendor** is any object with:

| | |
|---|---|
| `model` | The model it will use, for the job and the result. Optional. |
| `models` | Further ids it takes, for `action: list`. Optional; at most 20 are shown. |
| `ready()` | `""` when it can be asked, or why not - `"no acme key (ultron auth add acme)"`. Optional. |
| `capabilities` | What it takes, OpenClaw's shape - below. |
| `cannot(request)` | `""` when it can make this, or why not. Optional; asked after the capabilities had their say. |
| `async generate(request)` | Make one track. Returns an object with `data` (the audio's bytes), and optionally `model`, `cost` and `lyrics` (strings). |

`capabilities` is a dict, for the model the vendor was built on:

```python
{
    "generate": {"max_duration_seconds": 180, "supports_lyrics": True,
                 "supports_instrumental": True, "supports_duration": True,
                 "supports_format": True, "supported_formats": ("mp3", "wav")},
    "edit": {"enabled": True, "max_input_images": 1, ...the same},  # with pictures
}
```

An empty `supported_formats` with `supports_format` means the vendor checks the format itself.

`request` has `prompt`; what is left of the model's ask once your capabilities had their say -
`lyrics` (`""`), `instrumental` (`None` when not asked), `duration_seconds` (0), `format`
(`""`); `images` (a tuple of objects with `data` and `media_type`); `described` (the prompt
with the lyrics, the length and "no vocals" written into it, for a vendor whose only control
is the prompt); and `timeout` (seconds; musicgen also enforces it). Raise to fail: the
message is shown to the model, so make it an identifier - an HTTP status, a vendor's error
code - never the vendor's own prose. Make requests with `ultron.sdk.web` so the operator's
address policy applies, and send your key only to your own API's host. musicgen runs the call
in the background, caps the track at 64 MB, checks it is audio, saves it and tells the model;
a vendor does none of that.

**A vendor from before 3.0** declares no `capabilities`. It is read as what it did: lyrics, an
instrumental and a length through `described`, up to ten pictures, and no format - a format is
dropped and said. Its request still has `seconds`.

## How a job runs

1. **Start.** `music_generate` reads the pictures, holds the ask against each vendor, and
   returns the job id (`mg-1a2b3c`), the first vendor it will ask, the file the track will be
   written to, and what was changed to fit.
2. **Make.** The call to the vendor runs in a task of the session's, never of the turn's - a
   turn stopped after it started the job does not stop the track it paid for.
3. **Save.** The track is written to `music/<time>-<prompt>.mp3`, or under the `filename`
   the model gave, with the extension of what actually came back (MP3, WAV, FLAC, Ogg or M4A) - never
   over an existing file.
4. **Tell.** The agent is woken - OpenClaw's completion event, `ctx.wake` (SDK 1.40) - with
   one line per finished job: which job, where it is, who made it, how big it is. It runs as a
   turn of the session's own after whatever is running, never inside a person's turn, and its
   reply goes where the session's replies go: a song asked for in a Telegram DM is announced in
   that DM. `announce_to` sends it somewhere as well, chosen by you rather than the model:
   `channel:owner` is your DM on the channel you last wrote from, so a song started at the
   laptop is announced on your phone; `channel:<address>` is a fixed place, held to the
   `message` tool's rule (`channels_send_allow`) before the turn is spent (SDK 1.41). Several
   jobs that finish together are one wake. Where the agent cannot be woken -
   `announce: notice`, an Ultron before 1.40, a `cron` or group session, a lane busy past the
   core's wait, `plugins_no_wake` - the same line rides the session's next turn instead. The
   line carries what the plugin worked out and never a word a vendor wrote; why a job failed,
   and the lyrics the vendor sang, are behind `music_generate status`, whose result arrives
   inside an untrusted envelope.

**The same request twice** starts nothing, as OpenClaw's music tool does: while a job in
this session is being made from the same prompt, lyrics, length, instrumental switch, format
and pictures, or for two minutes after it was saved, `music_generate` answers with that job
instead. Where it is to be saved does not count - the same music to another file is still the
same music paid for twice. A job that failed does not count either, so asking again after a
failure is a retry. A different request is a new job, however many are running. The match is
a hash of the request, held in memory for the session and never written down.

Jobs are kept in `<workspace>/.ultron/musicgen/jobs.json`, so `music_generate status` still
lists them in a later session. Lyrics are not: they are a vendor's words, and a file in the
workspace is one `read_file` would hand the model without its envelope, so they are held in
memory for the session that made the track and are gone with it. Every vendor here answers in
one long request, so there is nothing to pick back up: a session that ends while a track is
being made stops it, and the job says so - the vendor may still have billed it.

Each attempt at a vendor is a `plugin` record with `event: generate` - vendor, model, how many
pictures went, whether lyrics went, the instrumental switch, length and format sent, and the hash and size of what came back or why it did not - and each ending is
`event: music`. Each wake is the core's own `event: wake` record. The prompt and the lyrics
are only in the tool call's own record.

Every track is a paid request, and every picture handed in is sent to that vendor with the
prompt. So is every wake: one model turn the person did not type. That is why the manifest
says `wakes: true` - `/plugins` shows `may start turns` before you install it - and why
`announce: notice`, or `plugins_no_wake: [musicgen]` in `config.json`, turns it off.

## Not built

- **Music the model can hear.** The file is saved and its path returned; nothing is put in
  front of the model.
- **A vendor that runs a job on its side** (submit, then poll) - every vendor here answers in
  one request. **Editing**, **extending** or **covering** a track, and a **seed**.
