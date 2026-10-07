---
name: videogen
description: Make videos with Google Veo, xAI, OpenRouter or Together in the background, saved in the workspace.
categories: [media, video]
version: "1.0.0"
requires_ultron_sdk: ">=1.38,<2"
vendor_credentials: [google, xai, openrouter, together]
contracts:
  tools: [generate_video, video_status]
config_schema:
  provider:
    type: str
    default: ""
    description: "google, xai, openrouter or together, tried first. Empty tries them in that order."
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
  xai_model:
    type: str
    default: grok-imagine-video-1.5
    description: "A Grok Imagine video model. The classic grok-imagine-video takes no last frame."
  openrouter_model:
    type: str
    default: google/veo-3.1
    description: "Any video model OpenRouter routes to, by its slug - bytedance/seedance-2.0, say."
  together_model:
    type: str
    default: minimax/hailuo-02
    description: "A video model Together serves, as Together writes it."
---

# videogen

Two tools. `generate_video` hands a prompt, and optionally a first and a last frame from the
workspace, to a video vendor and returns at once with a job id; the video is made in the
background - one to several minutes - and saved in the workspace when it is ready.
`video_status` lists this session's jobs, or checks one and can wait for it.

```
/plugins install videogen
```

## Keys

The ones Ultron already holds for its model providers - `ultron auth add google` (or `xai`,
`openrouter`, `together`), or the provider's variable in `~/.ultron/.env` (`GEMINI_API_KEY`,
`XAI_API_KEY`, `OPENROUTER_API_KEY`, `TOGETHER_API_KEY`). One key is enough. Google is the
bundled provider; the other three are the keys the `xai`, `openrouter` and `together`
provider plugins use, so a person who chats through one of them can make videos through it
too. The plugin reads them with `ctx.credential` (SDK 1.38), which is why this manifest
declares all four under `vendor_credentials` and `/plugins` says so before you install it. A
key is read when its vendor is reached and again when a job is picked back up in a new
session; each read is an `auth` record in the trail with a fingerprint, never the key.

OpenAI is not here: it shut down Sora 2 and its Videos API on 2026-09-24. Sora through
OpenRouter or Together went with it.

## Vendors

- **Google** (`veo-3.1-fast-generate-preview`): 4, 6 or 8 seconds, 16:9 or 9:16, 720p or
  1080p (1080p at 8 seconds only), a first frame and a last frame.
- **xAI** (`grok-imagine-video-1.5`): 1 to 15 seconds, any of the shapes, 480p to 1080p, a
  first frame and a last frame.
- **OpenRouter** (`google/veo-3.1`): whichever video model `openrouter_model` names, billed
  by OpenRouter; what it accepts is the model's business, and a refusal passes to the next
  vendor.
- **Together** (`minimax/hailuo-02`): the same, at Together, sized in pixels.

`provider` picks which is asked first; the rest follow in the order above. A vendor with no
usable key, one that cannot make what was asked for, or one that refuses the submission is
passed over and named in the result. Once a vendor has taken a job it is that vendor's: a job
that fails later is not sent anywhere else, because the first one may already have been
billed.

## How a job runs

1. **Submit.** `generate_video` checks the frames and the path, sends the job to the first
   vendor that takes it, and returns the job id (`vg-1a2b3c`), the vendor, and the file the
   video will be written to.
2. **Follow.** The plugin asks the vendor every `poll_seconds`, in the background, for up to
   `max_minutes`. A status check that fails on the network or with a 5xx is tried again; five
   in a row, or a 4xx, ends the job.
3. **Save.** The video is downloaded and written to `videos/<time>-<prompt>.mp4`, or the
   `path` the model named - never over an existing file.
4. **Tell.** On the session's next turn the model is given one line: which job finished, where
   it is, who made it, how big it is. The line carries what the plugin worked out and never a
   word a vendor wrote; why a job failed is behind `video_status`, whose result arrives inside
   an untrusted envelope. A notice never starts a turn by itself.

Jobs are kept in `<workspace>/.ultron/videogen/jobs.json`. A session that ends with a job
still running leaves it there, and the next session with the same key picks it back up, so a
video that was paid for is still collected. Nothing is sent to a vendor to resume, only asked.

Each submission is a `plugin` record with `event: submit` - vendor, model, how many frames
went, and the vendor's job id - and each ending is `event: video`, with the hash and size of
what came back or the reason it did not. The prompt is only in the tool call's own record.

Every video is a paid request, and every frame handed in is sent to that vendor.

## Not built

- **A video the model can watch.** The file is saved and its path returned; nothing is put in
  front of the model.
- **Cancelling** a job at the vendor, **extending** a video, **reference images**, and
  **audio** settings - every vendor's default audio is kept.
