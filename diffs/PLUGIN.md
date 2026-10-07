---
name: diffs
description: Diffs rendered for a person - a viewer in the web UI, a PNG or a PDF.
categories: [code, media]
logo: logo.svg
version: "1.0.0"
requires_ultron_sdk: ">=1.33,<2"
python_dependencies:
  - "pygments>=2.17"
contracts:
  tools: [diffs]
  views: [diffs]
config_schema:
  font_family:
    type: str
    default: Fira Code
    description: Font names for code, comma-separated; monospace is always last. Nothing is downloaded - a font is used if it is installed.
  font_size:
    type: int
    default: 15
    description: Code font size in pixels, 8-32.
  line_spacing:
    type: float
    default: 1.6
    description: Code line height, 1.0-3.0.
  layout:
    type: str
    default: unified
    description: unified or split - the layout a diff opens in; the viewer can switch.
  show_line_numbers:
    type: bool
    default: true
    description: Line number columns.
  diff_indicators:
    type: str
    default: bars
    description: bars (a coloured edge), classic (a +/- column) or none.
  word_wrap:
    type: bool
    default: true
    description: Wrap long lines in the unified layout; off scrolls sideways. Split and files always wrap.
  background:
    type: bool
    default: true
    description: Tint added and removed lines.
  theme:
    type: str
    default: dark
    description: dark or light - the theme a diff opens in; the viewer can switch.
  file_format:
    type: str
    default: png
    description: png or pdf, for mode file and both.
  file_quality:
    type: str
    default: standard
    description: standard (8 MP cap), hq (14 MP) or print (24 MP).
  file_scale:
    type: int
    default: 2
    description: Device pixels per CSS pixel for a PNG, 1-4.
  file_max_width:
    type: int
    default: 960
    description: The page width a file is rendered at, 640-2400 CSS pixels.
  mode:
    type: str
    default: both
    description: view, file or both, when a call does not say.
  ttl_seconds:
    type: int
    default: 1800
    description: How long a diff is kept, 60-21600 seconds.
  executable:
    type: str
    default: ""
    description: A Chromium-family browser binary to render files with, instead of the one found.
---

# diffs

A port of OpenClaw's [`diffs` tool](https://docs.openclaw.ai/tools/diffs). The model
already has the change - it wrote it or read it. This gives the person a rendering of it:
a viewer they open from the tool call's card in the web UI, a PNG or PDF the model can
send to a channel, or both. Needs SDK 1.33, where a plugin's view arrived.

```
/plugins install diffs
```

`mode=view` needs nothing more. PNG and PDF drive a headless Chromium through Playwright,
which is not installed with the plugin - into the environment Ultron runs from:

```
uv pip install playwright
playwright install chromium        # only if no Chrome, Edge, Brave or Chromium is installed
```

Without it a file request fails with a message naming both commands, and `mode=both`
still returns the viewer.

## Parameters

| Parameter | Type | Meaning |
|---|---|---|
| `before`, `after` | string, 512 KiB each | The old and new text of one file. Give both, or use `patch` |
| `patch` | string, 2 MiB | A unified diff (`git diff`, `diff -u`) covering one or more files, at most 128 files and 120,000 lines. Cannot be combined with `before`/`after` |
| `path` | string, 2048 bytes | The display name for `before`/`after`. It also picks the highlighting |
| `lang` | string, 128 bytes | The highlighting language. Aliases such as `js`, `ts`, `md`, `yml`, `bash`, `py` work. An unknown name falls back to plain text, with a note |
| `title` | string, 1024 bytes | A heading. The default is the file name, or "N files changed" |
| `mode` | `view` \| `file` \| `both` | What to make |
| `theme` | `light` \| `dark` | The theme the viewer opens in |
| `layout` | `unified` \| `split` | The layout the viewer opens in |
| `expand_unchanged` | bool | Show every unchanged line instead of folding them (`before`/`after` only) |
| `file_format` | `png` \| `pdf` | The file type |
| `file_quality` | `standard` \| `hq` \| `print` | Sets the pixel cap: 8, 14 or 24 MP. Also sets the default scale: 2, 3 or 4 |
| `file_scale` | 1-4 | Device pixels per CSS pixel for a PNG |
| `file_max_width` | 640-2400 | The page width, in CSS pixels, the file is rendered at |
| `ttl_seconds` | 60-21600 | How long the diff is kept. The default is 1800 |

The result is a set of plain lines: `changed`, `artifact_id`, `title`, `expires_at`,
`input_kind`, `file_count`, `additions`, `deletions` and `mode`. For a file the result
also has `file_path` (workspace-relative), `file_bytes`, `file_format`, `file_quality`,
`file_scale` and `file_max_width`. In `both` mode, a file that fails to render still
returns the viewer, with the reason in `file_error`.

## Settings

Under `plugins_settings.diffs` in `config.json`; `/plugins diffs` shows them. A value that
cannot be used falls back to its default and adds a note to the result instead of
refusing.

## How it works

- **Engine**: `before`/`after` go through `difflib`, and both whole sides are kept, so
  folded runs of unchanged lines can be expanded. A patch is parsed hunk by hunk using
  its line counts, and handles renames, copies, new and deleted files, mode changes and
  binary files. The gaps between hunks show as counts.
- **Page**: one self-contained HTML document. It has a header with totals, a
  changed-files card for a multi-file patch, and a panel for each file with its own
  counts and badges. It has no scripts and no external URLs, and the only styles are
  inline CSS. The controls are plain CSS: `<details>` folds a file or a run of unchanged
  lines, and radio inputs with `:checked` switch between unified and split and between
  light and dark. Syntax highlighting uses Pygments tokens mapped to the plugin's own
  classes. Each side is tokenised whole, so a multi-line string is coloured correctly.
  Changed words within a changed line are marked.
- **Storage**: artifacts live in `<workspace>/.ultron/diffs/<id>/`: `view.html.gz`,
  `meta.json`, and `diff.png` or `diff.pdf`. An id is `token_urlsafe(16)` and must match
  that exact pattern before it touches a path. An expired artifact is never served. Each
  new diff triggers a sweep that removes every expired artifact, and every artifact
  older than 24 hours regardless of its expiry.
- **Files**: the static page (one layout, one theme, nothing folded) is loaded with
  `set_content` into a headless Chromium. The renderer finds a browser by checking, in
  order: the `executable` setting, `ULTRON_BROWSER_EXECUTABLE_PATH`,
  `BROWSER_EXECUTABLE_PATH`, `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH`, installed Chrome,
  Edge, Brave or Chromium, and finally Playwright's own Chromium. Each launch starts one
  browser, which is closed in a `finally`, including when the call is cancelled.

The code is `plugin.py` and the `lib/` package beside it, which `plugin.py` loads from its
own directory under a name of its own - never from `sys.path`.

## Security notes

- Every piece of text that came from the model is escaped: code, paths, titles and hunk
  headings. Control characters are shown as their visible pictures. Bidirectional
  override characters are shown as `U+202E`-style code points, so a diff cannot hide
  what it changes.
- In the web UI the page runs in `<iframe sandbox>` with no scripts and an opaque
  origin, under a policy the core injects that blocks every fetch.
- The renderer has no network. The browser is launched on `scrubbed_environment()`.
  Every request the page makes is aborted, the page carries the same no-fetch policy,
  and Chromium's proxy is set to a port nothing listens on, so background traffic has
  nowhere to go.
- Diffs are written to the workspace and shown to whoever opens them. Keep secrets out
  of `before`, `after` and `patch`. The tool description tells the model this too.
- `assert_active()` runs before anything is written. A render is recorded in the audit
  trail as a `plugin` record (`render`) with its outcome and duration, and so is any
  sweep that removed something.

## Differences from OpenClaw

- **No URL viewer and no `baseUrl`.** The viewer is Ultron's web UI, opened from the
  tool call's card through the core's `plugins.view` method. There is no HTTP route and
  no shareable link.
- **No script in the page.** OpenClaw's viewer is a scripted page. Here every control
  is plain CSS, because the sandbox runs no script.
- **The split layout and files always wrap long lines.** `word_wrap` applies only to
  the unified viewer.
- **A diff with more than 20,000 rows** is written in one layout only, and the layout
  toggle is hidden.
- **Over-cap files are refused, not shrunk.** A PNG over its quality's pixel cap, or a
  PDF over 50 pages, returns a clear error that suggests a lower scale, a different
  quality, or a PDF.
- **Nothing is guessed.** The highlighting language comes from `lang` or the file name,
  never from sniffing the content.
