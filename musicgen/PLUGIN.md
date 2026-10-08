---
name: musicgen
description: Make music in the background with Google Lyria or any vendor another plugin adds, saved in the workspace.
categories: [media, audio, music]
version: "2.1.0"
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
    description: "Where a track goes, inside the workspace, when the model names no path."
  google_model:
    type: str
    default: lyria-3.5
    description: "A Lyria model: lyria-3.5 for a full song, or lyria-3-clip-preview for a 30-second clip."
---

# musicgen

One tool, OpenClaw's `music_generate`, with three actions. `generate` (the default) hands a
prompt, and optionally lyrics, a length and pictures from the workspace to set the mood, to a
music vendor and returns at once with a job id. The track is made in the background and saved
in the workspace when it is ready, usually in under two minutes, and the agent is woken to
tell you. `status` checks one job and can wait for it; `list` lists this session's jobs.

```
/plugins install musicgen
```

**From 1.x:** `generate_music` and `music_status` are now `music_generate` with
`action: generate`, `action: status` and `action: list`.

## Keys

Google uses the key Ultron already holds for its model provider - `ultron auth add google`,
or `GEMINI_API_KEY` in `~/.ultron/.env` - read with `ctx.credential` (SDK 1.38), which is
why this manifest declares it under `vendor_credentials`. Every other vendor is a backend
(below) and reads its key itself, in its own plugin's name. A key is read only when its
vendor is reached, and each read is an `auth` record in the trail with a fingerprint, never
the key.

## Vendors

One is built in:

- **Google** (`lyria-3.5`): a full song of a couple of minutes, with vocals or without,
  lyrics of its own or the ones you give it, and up to ten pictures to set the mood.
  `lyria-3-clip-preview` makes 30-second clips only. Lyria takes its length, its lyrics and
  "no vocals" as words in the prompt, so musicgen writes them into the prompt for it.

Every other vendor comes from another plugin. Enable the plugin and its vendor is here -
in this session, without a restart:

| Plugin | Vendor | What it makes | Its setting |
|---|---|---|---|
| `openrouter` | `google/lyria-3-pro-preview` | Lyria routed through OpenRouter, one picture at most, billed by OpenRouter | `music_model` |

`provider` picks which is asked first; then Google, and the backends in the order their
plugins install. A vendor with no usable key, or one that cannot make what was asked for,
is passed over when the job starts and named in the result. One that fails while making the
track is passed over too, and the next is asked - unless it timed out, because a vendor that
timed out may still have made, and billed, the track.

## Adding a vendor

A plugin adds a vendor by putting a builder into `musicgen.backend` (SDK 1.39):

```python
def register(self, ctx):
    if hasattr(ctx, "register_extension"):  # still loads on an Ultron before 1.39
        ctx.register_extension("musicgen.backend", "acme", lambda: AcmeMusic(ctx))
```

It needs nothing from musicgen, and musicgen needs no change: with musicgen absent the entry
sits unread. The name is what `provider` takes and what the job and the trail call the
vendor. A backend registered under `google` stands in for the built-in one.

**The builder** takes no arguments and returns a vendor. It is called only when musicgen
reaches that vendor, so read the key there.

**The vendor** is any object with:

| | |
|---|---|
| `model` | The model it will use, for the job and the result. Optional. |
| `ready()` | `""` when it can be asked, or why not - `"no acme key (ultron auth add acme)"`. Optional. |
| `cannot(request)` | `""` when it can make this, or why not - `"makes 30-second clips only"`. Optional. |
| `async generate(request)` | Make one track. Returns an object with `data` (the audio's bytes), and optionally `model`, `cost` and `lyrics` (strings). |

`request` has `prompt` (what the model asked for), `lyrics` (`""` for the vendor's own),
`instrumental` (a bool), `seconds` (0 for the vendor's default), `images` (a tuple of objects
with `data` and `media_type`), `described` (the prompt with the lyrics, the length and "no
vocals" written into it, for a vendor whose only control is the prompt) and `timeout`
(seconds; musicgen also enforces it). Raise to fail: the message is shown to the model, so
make it an identifier - an HTTP status, a vendor's error code - never the vendor's own
prose. Make requests with `ultron.sdk.web` so the operator's address policy applies, and
send your key only to your own API's host. musicgen runs the call in the background, caps
the track at 64 MB, checks it is audio, saves it and tells the model; a vendor does none of
that.

## How a job runs

1. **Start.** `music_generate` checks the pictures and the path, finds the vendors that can
   make it, and returns the job id (`mg-1a2b3c`), the first vendor it will ask, and the file
   the track will be written to.
2. **Make.** The call to the vendor runs in a task of the session's, never of the turn's - a
   turn stopped after it started the job does not stop the track it paid for.
3. **Save.** The track is written to `music/<time>-<prompt>.mp3`, or the `path` the model
   named, with the extension of what actually came back (MP3, WAV, FLAC, Ogg or M4A) - never
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
this session is being made from the same prompt, lyrics, length, instrumental switch and
pictures, or for two minutes after it was saved, `music_generate` answers with that job
instead. Where it is to be saved does not count - the same music to another file is still the
same music paid for twice. A job that failed does not count either, so asking again after a
failure is a retry. A different request is a new job, however many are running. The match is
a hash of the request, held in memory for the session and never written down.

Jobs are kept in `<workspace>/.ultron/musicgen/jobs.json`, so `music_generate list` still
lists them in a later session. Lyrics are not: they are a vendor's words, and a file in the
workspace is one `read_file` would hand the model without its envelope, so they are held in
memory for the session that made the track and are gone with it. Every vendor here answers in
one long request, so there is nothing to pick back up: a session that ends while a track is
being made stops it, and the job says so - the vendor may still have billed it.

Each attempt at a vendor is a `plugin` record with `event: generate` - vendor, model, how many
pictures went, and the hash and size of what came back or why it did not - and each ending is
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
  one request. **Editing**, **extending** or **covering** a track, a **seed**, and choosing
  **WAV** over MP3: each vendor's default format is kept.
